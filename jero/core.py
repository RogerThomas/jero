"""jero — an opinionated, msgspec-first ASGI micro-framework.

The contract:

- Resources are plain classes with any of the CRUD methods ``create`` /
  ``read_one`` / ``read_many`` / ``update`` / ``partial_update`` /
  ``delete``, mapped to POST / GET (item) / GET (collection) / PUT /
  PATCH / DELETE on the path given to ``_include_resource``.
  ``read_many`` serves the mount path itself and cannot extend it with
  trailing segments — items belong to ``read_one``.
- The mount path is a template: static segments plus ``{slot}`` params
  (snake_case, matching Struct field names). Handlers bind path values
  via a ``path`` Struct whose fields must cover every template slot;
  fields beyond the slots extend the route with trailing segments (the
  item id). Path Struct fields cannot have defaults.
- Handler arguments are bound by name: ``json`` (request body),
  ``params`` (query string), ``path`` (URL segments),
  ``headers``, and ``user`` (the result of auth). Each must be annotated
  with a msgspec Struct. Handlers may instead take the raw body as
  ``content: bytes`` (mutually exclusive with ``json``). Returns are a
  Struct, list[Struct], ``bytes`` (sent as application/octet-stream), or
  a ``BytesResponse`` / ``JSONResponse`` to control response headers.
  msgspec
  ``rename`` is honored everywhere (e.g. ``Struct, rename="camel"`` for
  camelCase on the wire, snake_case in code) — define your own base
  Struct for your wire convention.
- Auth is an object passed to ``_include_resource`` implementing
  ``authenticate(headers: SomeStruct) -> UserStruct``; raise
  ``HTTPError(401, ...)`` to reject. When auth is set it runs for every
  method on the resource, before the body is decoded. Handlers that
  declare ``user`` receive its result; the annotation is checked against
  the authenticator's return type at startup.
- Dependencies are wired by hand in the overridden ``BaseApp._wire`` method
  (runs once at startup). Open resources with ``self._aenter(cm)`` /
  ``self._enter(cm)`` — the app holds them on exit stacks and closes them
  (reverse order) at shutdown. No ``yield``, no DI container.

All introspection happens once, at ``_include_resource`` time. Routing is
dict lookups: static routes match exactly; templated routes are bucketed
by (method, segment count) and matched on their static segments — no
regexes, no route-table scans, no ordering rules.

Error semantics follow REST/HTTP: unmatched URL or path value that fails
conversion -> 404; malformed query/headers -> 400; malformed JSON body
-> 400; well-formed body failing the schema -> 422; auth failure -> 401;
wrong method -> 405 with ``Allow``. HEAD is served from GET routes with
the body suppressed, and OPTIONS answers 204 with ``Allow``.
"""

import asyncio
import contextlib
import inspect
from annotationlib import Format
from collections import defaultdict
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
    ExitStack,
)
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from types import NoneType, get_original_bases
from typing import Any, ClassVar, Literal, Protocol, cast, get_args, get_origin, get_type_hints
from urllib.parse import parse_qsl, unquote

import multipart as _multipart  # pyright: ignore[reportMissingTypeStubs]
from msgspec import DecodeError, Struct, ValidationError, convert
from msgspec.json import Decoder, Encoder
from msgspec.json import decode as json_decode
from msgspec.structs import fields as struct_fields

from jero.forms import FilePart, FormPart, NoHeaders
from jero.streaming import (
    NDJSONStreamingResponse,
    ServerSentEvent,
    SSEResponse,
    StreamingResponse,
)

# Reusable msgspec codecs. Building these once and reusing them is faster than the
# module-level ``msgspec.json.encode`` / ``decode`` helpers, which construct a
# throwaway codec on every call. Exported via the jero API for app code to reuse too.
# The Encoder reuses an internal buffer and is not safe for concurrent use across
# threads — fine here, since jero runs on a single async event loop per worker. The
# Decoder is untyped: typed request bodies are decoded against their own Struct at the
# call site (an untyped decode + ``convert`` would weaken validation).
msgspec_encoder = Encoder()
msgspec_decoder = Decoder()

type Scope = dict[str, Any]
type Receive = Callable[[], Awaitable[dict[str, Any]]]
type Send = Callable[[dict[str, Any]], Awaitable[None]]

# A compiled per-request handler: decode -> call -> encode.
type _Handler = Callable[[Scope, Receive, Send, dict[str, str]], Awaitable[None]]
# A template segment: (is_param, static_value_or_slot_name).
type _Segment = tuple[bool, str]
type _StaticRoutes = dict[tuple[str, str], _Handler]
type _DynamicRoutes = dict[tuple[str, int], list[_Pattern]]
type _AllowedMethods = dict[str, list[str]]
type _MultipartOptionsParser = Callable[[str], tuple[str, dict[str, str]]]

# Argument names the binder understands, shared by every handler kind.
_SOURCES = ("json", "content", "form", "params", "path", "headers", "user")
# HTTP verbs that forbid a request body, whatever the handler is named.
_BODYLESS_VERBS = frozenset({"GET", "DELETE"})


@dataclass(frozen=True, slots=True)
class _Verb:
    """How one handler method maps onto HTTP."""

    http: str
    status: int
    extends_path: bool  # may path fields beyond the template slots extend the URL?


class WiringError(TypeError):
    """A router does not meet the framework contract. Raised at startup."""


class HTTPError(Exception):
    """Raise from a handler to return a JSON error response."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class BaseResponse(Struct, kw_only=True):
    """Base for handler returns that control response headers.

    Return one of the concrete subclasses; the status code is still the
    verb's (201 for create, 200 otherwise). ``content-length`` is managed
    by the framework and ignored if supplied in ``headers``.
    """

    headers: dict[str, str] | None = None


class BytesResponse(BaseResponse):
    """Raw bytes; content-type defaults to application/octet-stream."""

    content: bytes


class JSONResponse(BaseResponse):
    """A Struct encoded as JSON; content-type defaults to application/json."""

    json: Struct


class Resource:
    """One REST resource: subclass and define any of the CRUD methods.

    ``read_one`` is the item route (its ``path`` may extend the mount with
    the item id); ``read_many`` is the collection (its path is exact).
    """

    METHODS: ClassVar[dict[str, _Verb]] = {
        "create": _Verb("POST", 201, extends_path=True),
        "read_one": _Verb("GET", 200, extends_path=True),
        "read_many": _Verb("GET", 200, extends_path=False),
        "update": _Verb("PUT", 200, extends_path=True),
        "partial_update": _Verb("PATCH", 200, extends_path=True),
        "delete": _Verb("DELETE", 200, extends_path=True),
    }


class Endpoint:
    """One HTTP endpoint at a single path: subclass and define any of
    ``get`` / ``post`` / ``put`` / ``patch`` / ``delete``.

    Unlike :class:`Resource` there are no CRUD semantics — the method name
    *is* the verb, every verb returns 200, and the path is exact (no
    trailing extension). A different path is a different ``Endpoint``.
    """

    METHODS: ClassVar[dict[str, _Verb]] = {
        "get": _Verb("GET", 200, extends_path=False),
        "post": _Verb("POST", 200, extends_path=False),
        "put": _Verb("PUT", 200, extends_path=False),
        "patch": _Verb("PATCH", 200, extends_path=False),
        "delete": _Verb("DELETE", 200, extends_path=False),
    }


class Auth[THeaders: Struct, TUser: Struct](Protocol):
    """Implement ``authenticate``; raise ``HTTPError(401, ...)`` to reject.

    ``headers`` is bound from the request headers into your declared
    Struct (header names map ``x-trace-id`` -> ``x_trace_id``). The
    returned Struct is what handlers receive as ``user``.
    """

    def authenticate(self, headers: THeaders) -> TUser | Awaitable[TUser]:
        """Validate ``headers`` and return the user Struct; raise ``HTTPError(401)`` to reject."""
        ...  # pylint: disable=unnecessary-ellipsis  # Protocol stub; pyright needs the body


class _StreamResult(Protocol):
    stream: Any
    headers: dict[str, str] | None
    status: int | None


class _MultipartPart(Protocol):
    name: str | None
    filename: str | None
    headerlist: list[tuple[str, str]]
    raw: bytes


class _MultipartParserFactory(Protocol):
    def __call__(
        self, stream: BytesIO, boundary: str, *, strict: bool
    ) -> Iterable[_MultipartPart]: ...


_parse_options_header = cast(
    "_MultipartOptionsParser",
    _multipart.parse_options_header,  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
)
_MultipartParser = cast("_MultipartParserFactory", _multipart.MultipartParser)
_MultipartError = cast(
    "type[Exception]",
    _multipart.MultipartError,  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
)


def _allow_header(allowed: Sequence[str]) -> bytes:
    methods = [*allowed]  # copy: HEAD/OPTIONS are appended without mutating the caller's list
    if "GET" in methods:
        methods.append("HEAD")
    methods.append("OPTIONS")
    return ", ".join(methods).encode()


@dataclass(slots=True)
class _SuppressBody:
    """Wraps a ``send`` to drop the response body (HEAD semantics)."""

    _send: Send

    async def __call__(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            message = {"type": "http.response.body", "body": b""}
        await self._send(message)


def _raw_headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").replace("-", "_"): v.decode("latin-1") for k, v in scope["headers"]}


def _mangle_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key.lower().replace("-", "_"): value for key, value in headers.items()}


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    return chunks[0] if len(chunks) == 1 else b"".join(chunks)


async def _send_json(
    send: Send,
    status: int,
    payload: bytes,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    if extra_headers:
        headers += extra_headers
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


def _convert_source(
    raw: dict[str, str],
    struct_type: type[Struct],
    status: int,
    detail: str | None = None,
) -> Struct:
    """Convert one request source to its Struct, mapping failure to an HTTP status."""
    try:
        return convert(raw, struct_type, strict=False)
    except ValidationError as exc:
        raise HTTPError(status, str(exc) if detail is None else detail) from None


def _decode_json_body(body: bytes, struct_type: type[Struct]) -> Struct:
    try:
        return json_decode(body, type=struct_type)
    except ValidationError as exc:
        raise HTTPError(422, str(exc)) from None
    except DecodeError as exc:
        raise HTTPError(400, str(exc)) from None


def _is_none_type(ann: object) -> bool:
    return ann is None or ann is NoneType


def _alias_value(ann: object) -> object:
    value = getattr(ann, "__value__", None)
    return ann if value is None else value


def _is_struct_type(ann: object) -> bool:
    return isinstance(ann, type) and issubclass(ann, Struct)


def _is_struct_payload(ann: object) -> bool:
    ann = _alias_value(ann)
    if _is_struct_type(ann):
        return True
    args = get_args(ann)
    return (
        bool(args)
        and any(_is_none_type(arg) for arg in args) is False
        and all(_is_struct_type(arg) for arg in args)
    )


def _is_scalar_payload(ann: object) -> bool:
    ann = _alias_value(ann)
    if ann is str or ann is int or ann is float or ann is bool:
        return True
    if isinstance(ann, type) and issubclass(ann, Enum):
        return True
    return get_origin(ann) is Literal


def _form_part_types(ann: object) -> tuple[object, object] | None:
    origin = get_origin(ann)
    if origin is FormPart:
        args = get_args(ann)
        return (args[0], args[1]) if len(args) == 2 else None
    if origin is FilePart:
        args = get_args(ann)
        return (bytes, args[0]) if len(args) == 1 else None
    if ann is FilePart:
        return bytes, FilePart.__type_params__[0].__default__
    if ann is not FormPart:
        return None
    return None


def _strip_optional(ann: object) -> tuple[object, bool]:
    args = get_args(ann)
    if len(args) != 2 or not any(_is_none_type(arg) for arg in args):
        return ann, False
    payload = next(arg for arg in args if not _is_none_type(arg))
    return payload, True


def _strip_list(ann: object) -> tuple[object, bool]:
    if get_origin(ann) is not list:
        return ann, False
    args = get_args(ann)
    if len(args) != 1:
        return ann, False
    return args[0], True


def _payload_kind(cls: type, method: str, field_name: str, ann: object) -> str:
    if ann is bytes:
        return "bytes"
    if _is_struct_payload(ann):
        return "struct"
    if _is_scalar_payload(ann):
        return "scalar"
    raise WiringError(
        f"{cls.__name__}.{method}: form field {field_name!r} has unsupported payload "
        f"type {ann!r}; expected bytes, a msgspec.Struct, or a scalar",
    )


@dataclass(frozen=True, slots=True)
class _FormField:
    name: str
    wire_name: str
    payload_type: object
    headers_type: type[Struct]
    payload_kind: str
    required: bool
    repeated: bool
    enveloped: bool
    file: bool


@dataclass(frozen=True, slots=True)
class _FormSpec:
    struct_type: type[Struct]
    fields: tuple[_FormField, ...]


@dataclass(frozen=True, slots=True)
class _Part:
    name: str
    filename: str | None
    content_type: str | None
    headers: dict[str, str]
    body: bytes


def _compile_form(cls: type, method: str, form_type: type[Struct]) -> _FormSpec:
    descriptors: list[_FormField] = []
    for field in struct_fields(form_type):
        field_type, optional = _strip_optional(field.type)
        item_type, repeated = _strip_list(field_type)
        part_types = _form_part_types(item_type)
        enveloped = part_types is not None
        file = item_type is FilePart or get_origin(item_type) is FilePart
        if part_types is None:
            payload_type = item_type
            headers_type = NoHeaders
        else:
            payload_type = part_types[0]
            headers_type = _struct_annotation(cls, method, f"{field.name}.headers", part_types[1])
        descriptors.append(
            _FormField(
                name=field.name,
                wire_name=field.encode_name,
                payload_type=payload_type,
                headers_type=headers_type,
                payload_kind=_payload_kind(cls, method, field.name, payload_type),
                required=field.required and not optional and not repeated,
                repeated=repeated,
                enveloped=enveloped,
                file=file,
            )
        )
    return _FormSpec(form_type, tuple(descriptors))


def _content_type_header(headers: dict[str, str]) -> tuple[str, str] | None:
    value = headers.get("content_type")
    if value is None:
        return None
    media_type, options = _parse_options_header(value)
    boundary = options.get("boundary")
    if boundary is None:
        return media_type, ""
    return media_type, boundary


def _part_headers(headerlist: list[tuple[str, str]]) -> dict[str, str]:
    return dict(headerlist)


def _part_content_type(headers: dict[str, str]) -> str | None:
    for name, value in headers.items():
        if name.lower() == "content-type":
            return value
    return None


def _parse_form_parts(body: bytes, raw_headers: dict[str, str]) -> dict[str, list[_Part]]:
    parsed = _content_type_header(raw_headers)
    if parsed is None or parsed[0] != "multipart/form-data" or not parsed[1]:
        raise HTTPError(415, "unsupported media type")

    parts: dict[str, list[_Part]] = defaultdict(list)
    try:
        for raw_part in _MultipartParser(BytesIO(body), parsed[1], strict=True):
            if raw_part.name is None:
                raise HTTPError(400, "malformed multipart body")
            headers = _part_headers(raw_part.headerlist)
            parts[raw_part.name].append(
                _Part(
                    name=raw_part.name,
                    filename=raw_part.filename,
                    content_type=_part_content_type(headers),
                    headers=headers,
                    body=raw_part.raw,
                )
            )
    except _MultipartError as exc:
        raise HTTPError(400, str(exc)) from None
    return parts


def _decode_form_payload(field: _FormField, part: _Part) -> object:
    if field.payload_kind == "bytes":
        return part.body
    if field.payload_kind == "struct":
        try:
            return json_decode(part.body, type=field.payload_type)
        except ValidationError as exc:
            raise HTTPError(422, str(exc)) from None
        except DecodeError as exc:
            raise HTTPError(400, str(exc)) from None
    try:
        return convert(part.body.decode(), field.payload_type, strict=False)
    except UnicodeDecodeError as exc:
        raise HTTPError(422, str(exc)) from None
    except ValidationError as exc:
        raise HTTPError(422, str(exc)) from None


def _decode_form_value(field: _FormField, part: _Part) -> object:
    data = _decode_form_payload(field, part)
    if not field.enveloped:
        return data
    headers = (
        NoHeaders()
        if field.headers_type is NoHeaders
        else _convert_source(_mangle_headers(part.headers), field.headers_type, 400)
    )
    if field.file:
        if part.filename is None:
            raise HTTPError(422, f"form field {field.wire_name!r} requires a filename")
        return FilePart(
            data=cast("bytes", data),
            content_type=part.content_type,
            headers=headers,
            filename=part.filename,
        )
    return FormPart(data=data, content_type=part.content_type, headers=headers)


def _decode_form_body(body: bytes, raw_headers: dict[str, str], spec: _FormSpec) -> Struct:
    parts = _parse_form_parts(body, raw_headers)
    values: dict[str, object] = {}
    for field in spec.fields:
        matched = parts[field.wire_name]
        if field.repeated:
            values[field.wire_name] = [_decode_form_value(field, part) for part in matched]
            continue
        if not matched:
            if field.required:
                raise HTTPError(422, f"form field {field.wire_name!r} is required")
            values[field.wire_name] = None
            continue
        values[field.wire_name] = _decode_form_value(field, matched[-1])
    try:
        return convert(values, spec.struct_type, strict=False)
    except ValidationError as exc:
        raise HTTPError(422, str(exc)) from None


def _struct_annotation(cls: type, method: str, name: str, ann: object) -> type[Struct]:
    if not (isinstance(ann, type) and issubclass(ann, Struct)):
        raise WiringError(
            f"{cls.__name__}.{method}: {name!r} must be annotated with a "
            f"msgspec.Struct subclass, got {ann!r}",
        )
    return ann


@dataclass(slots=True)
class _Sources:
    """The resolved Struct types for one handler's arguments."""

    json: type[Struct] | None = None
    form: _FormSpec | None = None
    params: type[Struct] | None = None
    path: type[Struct] | None = None
    headers: type[Struct] | None = None
    user: type[Struct] | None = None
    content: bool = False
    return_kind: str = "json"


def _return_kind(ann: object) -> str | None:  # noqa: C901
    if isinstance(ann, type):
        if issubclass(ann, StreamingResponse):
            return "stream_bytes"
        if issubclass(ann, NDJSONStreamingResponse):
            return "stream_ndjson"
        if issubclass(ann, SSEResponse):
            return "stream_sse"
        if issubclass(ann, BytesResponse):
            return "bytes_response"
        if issubclass(ann, JSONResponse):
            return "json_response"
        if issubclass(ann, BaseResponse):
            return None  # the base is abstract; return a concrete subclass
        if issubclass(ann, Struct):
            return "json"
        if ann is bytes:
            return "bytes"
    args = get_args(ann)
    origin = get_origin(ann)
    if origin is StreamingResponse:
        return "stream_bytes"
    if origin is NDJSONStreamingResponse:
        return "stream_ndjson"
    if origin is SSEResponse:
        return "stream_sse"
    if (
        origin is list
        and len(args) == 1
        and isinstance(args[0], type)
        and issubclass(args[0], Struct)
    ):
        return "json"
    return None


def _bind_sources(cls: type, name: str, fn: Callable[..., Any], http_method: str) -> _Sources:
    """Resolve and validate the Struct types for a handler's arguments."""
    hints = get_type_hints(fn)
    types: dict[str, type[Struct]] = {}
    form: _FormSpec | None = None
    wants_content = False

    for param in inspect.signature(fn).parameters.values():
        if param.name not in _SOURCES:
            raise WiringError(
                f"{cls.__name__}.{name}: unsupported argument {param.name!r}; "
                f"allowed names are {', '.join(_SOURCES)}",
            )
        if param.name in ("json", "content", "form") and http_method in _BODYLESS_VERBS:
            raise WiringError(
                f"{cls.__name__}.{name}: {http_method} handlers cannot take {param.name!r}",
            )
        if param.name == "content":
            if hints.get("content") is not bytes:
                raise WiringError(
                    f"{cls.__name__}.{name}: 'content' must be annotated as bytes",
                )
            wants_content = True
            continue
        source_type = _struct_annotation(cls, name, param.name, hints.get(param.name))
        if param.name == "form":
            form = _compile_form(cls, name, source_type)
            continue
        types[param.name] = source_type

    body_sources = int(wants_content) + int(types.get("json") is not None) + int(form is not None)
    if body_sources > 1:
        raise WiringError(
            f"{cls.__name__}.{name}: only one of 'json', 'content', or 'form' is allowed",
        )

    return_kind = _return_kind(hints.get("return"))
    if return_kind is None:
        raise WiringError(
            f"{cls.__name__}.{name} must declare a return type of Struct, list[Struct], "
            f"bytes, BytesResponse, JSONResponse, or a streaming response, "
            f"got {hints.get('return')!r}",
        )
    if return_kind == "stream_sse" and http_method != "GET":
        raise WiringError(f"{cls.__name__}.{name}: SSEResponse is only allowed on GET handlers")

    return _Sources(**types, form=form, content=wants_content, return_kind=return_kind)


def _parse_template(path: str) -> list[_Segment]:
    """Parse a mount path like ``/collections/{collection_id}/pokemon``."""
    if not path.startswith("/"):
        raise WiringError(f"path {path!r} must start with '/'")

    segments: list[_Segment] = []
    slots: set[str] = set()
    for raw in path.split("/"):
        if raw.startswith("{") and raw.endswith("}"):
            slot = raw[1:-1]
            if not slot.isidentifier():
                raise WiringError(f"path {path!r}: slot {raw!r} is not a valid identifier")
            if slot in slots:
                raise WiringError(f"path {path!r}: duplicate slot {raw!r}")
            slots.add(slot)
            segments.append((True, slot))
        elif "{" in raw or "}" in raw:
            raise WiringError(f"path {path!r}: malformed segment {raw!r}")
        else:
            segments.append((False, raw))
    return segments


def _template_str(segments: list[_Segment]) -> str:
    return "/".join(f"{{{value}}}" if is_param else value for is_param, value in segments)


def _route_segments(
    cls: type,
    name: str,
    template: list[_Segment],
    path_type: type[Struct] | None,
    *,
    extends_path: bool,
) -> list[_Segment]:
    """Combine the mount template with a handler's path Struct."""
    slots = [value for is_param, value in template if is_param]
    if path_type is None:
        if slots:
            raise WiringError(
                f"{cls.__name__}.{name} must declare 'path' covering "
                f"template slots: {', '.join(slots)}",
            )
        return list(template)

    if path_type.__struct_defaults__:
        raise WiringError(
            f"{cls.__name__}.{name}: path fields cannot have defaults ({path_type.__name__})",
        )
    fields = path_type.__struct_fields__
    missing = [slot for slot in slots if slot not in fields]
    if missing:
        raise WiringError(
            f"{cls.__name__}.{name}: path {path_type.__name__} is missing "
            f"template slots: {', '.join(missing)}",
        )
    trailing = [field for field in fields if field not in slots]
    if trailing and not extends_path:
        if name == "read_many":
            raise WiringError(
                f"{cls.__name__}.read_many: collections live at the mount path; "
                f"trailing path fields ({', '.join(trailing)}) belong on read_one",
            )
        raise WiringError(
            f"{cls.__name__}.{name}: path {path_type.__name__} declares fields beyond the "
            f"template slots ({', '.join(trailing)}); this route's path is exact — add them "
            f"to the mount template",
        )

    # Bind by encode_name so renamed Structs (e.g. CamelStruct) work: the
    # values dict we hand to convert() must use the wire names it expects.
    encode_names = {f.name: f.encode_name for f in struct_fields(path_type)}
    segments: list[_Segment] = [
        (True, encode_names[value]) if is_param else (is_param, value)
        for is_param, value in template
    ]
    segments += ((True, encode_names[field]) for field in trailing)
    return segments


@dataclass(frozen=True, slots=True)
class _Pattern:
    """A compiled templated route within one (method, segment-count) bucket."""

    statics: tuple[tuple[int, str], ...]  # (position, required value)
    params: tuple[tuple[int, str], ...]  # (position, slot name)
    handler: _Handler

    def matches(self, segments: list[str]) -> bool:
        """Whether this pattern's static segments all equal the request path's."""
        return all(segments[i] == value for i, value in self.statics)


class _CompiledAuth:
    """An authenticator introspected once at registration time.

    Deliberately not a dataclass: every attribute is derived by
    introspection, so a plain ``__init__`` is the honest shape.
    """

    __slots__ = ("_fn", "_is_async", "headers_type", "owner", "returns")

    def __init__(self, auth: Auth[Any, Any]) -> None:
        self.owner = type(auth).__name__
        fn = getattr(auth, "authenticate", None)
        if not callable(fn):
            raise WiringError(f"{self.owner} must define an 'authenticate' method")

        params = list(inspect.signature(fn).parameters.values())
        if len(params) != 1 or params[0].name != "headers":
            raise WiringError(
                f"{self.owner}.authenticate must take exactly one argument named 'headers'",
            )
        hints = get_type_hints(fn)
        self.headers_type = _struct_annotation(
            type(auth), "authenticate", "headers", hints.get("headers")
        )

        returns = hints.get("return")
        if not (isinstance(returns, type) and issubclass(returns, Struct)):
            raise WiringError(
                f"{self.owner}.authenticate must declare a msgspec.Struct return type, "
                f"got {returns!r}",
            )
        self.returns: type[Struct] = returns
        self._fn: Callable[..., Any] = fn
        self._is_async = inspect.iscoroutinefunction(fn)

    async def __call__(self, raw_headers: dict[str, str]) -> Struct:
        try:
            credentials = convert(raw_headers, self.headers_type, strict=False)
        except ValidationError as exc:
            raise HTTPError(401, str(exc)) from None
        result = self._fn(credentials)
        return (await result) if self._is_async else result


class _Binder:
    """Resolved per-source binding for one handler; builds its kwargs per request."""

    __slots__ = (
        "_auth",
        "_form_spec",
        "_headers_type",
        "_json_type",
        "_needs_raw",
        "_params_type",
        "_path_type",
        "_wants_content",
        "_wants_user",
    )

    def __init__(self, sources: _Sources, auth: _CompiledAuth | None) -> None:
        self._json_type = sources.json
        self._form_spec = sources.form
        self._params_type = sources.params
        self._path_type = sources.path
        self._headers_type = sources.headers
        self._auth = auth
        self._wants_content = sources.content
        self._wants_user = sources.user is not None
        self._needs_raw = (
            auth is not None or sources.headers is not None or sources.form is not None
        )

    async def __call__(
        self, scope: Scope, receive: Receive, path_values: dict[str, str]
    ) -> dict[str, object]:
        kwargs: dict[str, object] = {}
        raw_headers = _raw_headers(scope) if self._needs_raw else {}
        if self._auth is not None:
            user = await self._auth(raw_headers)
            if self._wants_user:
                kwargs["user"] = user
        if self._path_type is not None:
            # A path value that fails conversion does not identify a resource.
            kwargs["path"] = _convert_source(path_values, self._path_type, 404, "not found")
        if self._headers_type is not None:
            kwargs["headers"] = _convert_source(raw_headers, self._headers_type, 400)
        if self._params_type is not None:
            raw_query = dict(parse_qsl(scope["query_string"].decode("latin-1")))
            kwargs["params"] = _convert_source(raw_query, self._params_type, 400)
        if self._json_type is not None:
            kwargs["json"] = _decode_json_body(await _read_body(receive), self._json_type)
        if self._form_spec is not None:
            kwargs["form"] = _decode_form_body(
                await _read_body(receive), raw_headers, self._form_spec
            )
        if self._wants_content:
            kwargs["content"] = await _read_body(receive)
        return kwargs


type _Sender = Callable[[Scope, Receive, Send, Any], Awaitable[None]]


def _response_headers(
    user_headers: dict[str, str] | None,
    default_content_type: bytes,
    payload_length: int,
) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    has_content_type = False
    for key, value in (user_headers or {}).items():
        lower = key.lower()
        if lower == "content-length":
            continue
        if lower == "content-type":
            has_content_type = True
        headers.append((key.encode("latin-1"), value.encode("latin-1")))
    if not has_content_type:
        headers.append((b"content-type", default_content_type))
    headers.append((b"content-length", str(payload_length).encode()))
    return headers


async def _send_payload(
    send: Send, status: int, payload: bytes, headers: list[tuple[bytes, bytes]]
) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


@dataclass(slots=True)
class _BytesSender:
    _status: int

    async def __call__(self, scope: Scope, receive: Receive, send: Send, result: bytes) -> None:
        _ = (scope, receive)
        headers = _response_headers(None, b"application/octet-stream", len(result))
        await _send_payload(send, self._status, result, headers)


@dataclass(slots=True)
class _BytesResponseSender:
    _status: int

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, result: BytesResponse
    ) -> None:
        _ = (scope, receive)
        headers = _response_headers(
            result.headers, b"application/octet-stream", len(result.content)
        )
        await _send_payload(send, self._status, result.content, headers)


@dataclass(slots=True)
class _JSONResponseSender:
    _status: int

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, result: JSONResponse
    ) -> None:
        _ = (scope, receive)
        payload = msgspec_encoder.encode(result.json)
        headers = _response_headers(result.headers, b"application/json", len(payload))
        await _send_payload(send, self._status, payload, headers)


@dataclass(slots=True)
class _JSONSender:
    _status: int

    async def __call__(self, scope: Scope, receive: Receive, send: Send, result: object) -> None:
        _ = (scope, receive)
        await _send_json(send, self._status, msgspec_encoder.encode(result))


def _stream_headers(
    user_headers: dict[str, str] | None,
    default_content_type: bytes,
) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    has_content_type = False
    for key, value in (user_headers or {}).items():
        lower = key.lower()
        if lower == "content-length":
            continue
        if lower == "content-type":
            has_content_type = True
        headers.append((key.encode("latin-1"), value.encode("latin-1")))
    if not has_content_type:
        headers.append((b"content-type", default_content_type))
    return headers


async def _close_async_iter(iterator: AsyncIterator[object]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


async def _anext[T](iterator: AsyncIterator[T]) -> T:
    return await anext(iterator)


async def _receive(receive: Receive) -> dict[str, Any]:
    return await receive()


async def _next_or_disconnect[T](
    iterator: AsyncIterator[T],
    receive: Receive,
) -> tuple[str, T | None]:
    next_task: asyncio.Task[T] = asyncio.create_task(_anext(iterator))
    try:
        while True:
            receive_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(_receive(receive))
            tasks: set[asyncio.Task[Any]] = {next_task, receive_task}
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if receive_task in done:
                message = receive_task.result()
                if message["type"] == "http.disconnect":
                    next_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await next_task
                    return "disconnect", None
                continue
            receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receive_task
            try:
                return "item", next_task.result()
            except StopAsyncIteration:
                return "done", None
    except Exception:
        next_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await next_task
        raise


async def _resolve_stream[T](
    stream: AsyncIterable[T],
) -> tuple[AsyncIterator[T], T | None, AsyncIterator[AsyncIterable[T]] | None]:
    outer = aiter(stream)
    try:
        first = await anext(outer)
    except StopAsyncIteration:
        return outer, None, None
    if isinstance(first, AsyncIterable):
        inner = aiter(cast("AsyncIterable[T]", first))
        lifecycle = cast("AsyncIterator[AsyncIterable[T]]", outer)
        return inner, None, lifecycle
    return outer, first, None


async def _finish_lifecycle[T](lifecycle: AsyncIterator[AsyncIterable[T]] | None) -> None:
    if lifecycle is None:
        return
    try:
        await anext(lifecycle)
    except StopAsyncIteration:
        return
    await _close_async_iter(cast("AsyncIterator[object]", lifecycle))
    raise RuntimeError("streaming lifecycle must yield exactly one stream")


def _sse_data_lines(data: Struct | str) -> list[str]:
    if isinstance(data, str):
        return data.splitlines() or [""]
    return msgspec_encoder.encode(data).decode().splitlines()


def _encode_sse(item: Struct | str | ServerSentEvent[Any]) -> bytes:
    event = item if isinstance(item, ServerSentEvent) else ServerSentEvent(data=item)
    lines: list[str] = []
    if event.event is not None:
        lines.append(f"event: {event.event}")
    if event.id is not None:
        lines.append(f"id: {event.id}")
    if event.retry is not None:
        lines.append(f"retry: {event.retry}")
    lines += [f"data: {line}" for line in _sse_data_lines(event.data)]
    return ("\n".join(lines) + "\n\n").encode()


@dataclass(slots=True)
class _StreamSender:
    _status: int
    _content_type: bytes

    def _chunk(self, item: object) -> bytes:
        if not isinstance(item, bytes):
            raise TypeError("StreamingResponse items must be bytes")
        return item

    async def _send_chunk(self, send: Send, chunk: bytes) -> None:
        await send({"type": "http.response.body", "body": chunk, "more_body": True})

    async def _send_setup_error(self, send: Send, exc: Exception) -> None:
        if isinstance(exc, HTTPError):
            await _send_json(send, exc.status, msgspec_encoder.encode({"error": exc.detail}))
            return
        await _send_json(send, 500, msgspec_encoder.encode({"error": "internal server error"}))

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        result: _StreamResult,
    ) -> None:
        status = result.status if result.status is not None else self._status
        headers = _stream_headers(result.headers, self._content_type)
        if scope["method"] == "HEAD":
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return
        try:
            iterator, first, lifecycle = await _resolve_stream(result.stream)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            await self._send_setup_error(send, exc)
            return
        await send({"type": "http.response.start", "status": status, "headers": headers})
        try:
            if first is not None:
                await self._send_chunk(send, self._chunk(first))
            while True:
                state, item = await _next_or_disconnect(iterator, receive)
                if state == "done":
                    break
                if state == "disconnect":
                    await _close_async_iter(cast("AsyncIterator[object]", iterator))
                    return
                if item is not None:
                    await self._send_chunk(send, self._chunk(item))
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            return
        finally:
            with contextlib.suppress(Exception):
                await _finish_lifecycle(lifecycle)
        await send({"type": "http.response.body", "body": b"", "more_body": False})


@dataclass(slots=True)
class _NDJSONStreamSender(_StreamSender):
    def _chunk(self, item: object) -> bytes:
        if not isinstance(item, Struct):
            raise TypeError("NDJSONStreamingResponse items must be msgspec.Struct instances")
        return msgspec_encoder.encode(item) + b"\n"


@dataclass(slots=True)
class _SSEStreamSender(_StreamSender):
    async def _cancel_keepalive(self, keepalive_task: asyncio.Task[None] | None) -> None:
        if keepalive_task is not None:
            keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive_task

    async def _cancel_receive(self, receive_task: asyncio.Task[dict[str, Any]]) -> None:
        receive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receive_task

    async def _stream_chunks(
        self,
        iterator: AsyncIterator[object],
        receive: Receive,
        send: Send,
        keepalive: float | None,
    ) -> None:
        next_task: asyncio.Task[object] = asyncio.create_task(_anext(iterator))
        try:
            while True:
                receive_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(_receive(receive))
                keepalive_task = (
                    asyncio.create_task(asyncio.sleep(keepalive)) if keepalive is not None else None
                )
                tasks: set[asyncio.Task[Any]] = {next_task, receive_task}
                if keepalive_task is not None:
                    tasks.add(keepalive_task)
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if receive_task in done:
                    message = receive_task.result()
                    if message["type"] == "http.disconnect":
                        await self._cancel_keepalive(keepalive_task)
                        next_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await next_task
                        await _close_async_iter(iterator)
                        return
                    await self._cancel_keepalive(keepalive_task)
                    continue
                await self._cancel_receive(receive_task)
                if keepalive_task is not None and keepalive_task in done:
                    await self._send_chunk(send, b": ping\n\n")
                    continue
                await self._cancel_keepalive(keepalive_task)
                try:
                    item = next_task.result()
                except StopAsyncIteration:
                    return
                await self._send_chunk(send, self._chunk(item))
                next_task = asyncio.create_task(_anext(iterator))
        except Exception:
            next_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await next_task
            raise

    def _chunk(self, item: object) -> bytes:
        if not isinstance(item, (Struct, ServerSentEvent, str)):
            raise TypeError("SSEResponse items must be Struct, str, or ServerSentEvent")
        event = cast("Struct | ServerSentEvent[Any] | str", item)
        return _encode_sse(event)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        result: _StreamResult,
    ) -> None:
        sse = cast("SSEResponse[Any]", result)
        status = result.status if result.status is not None else self._status
        headers = _stream_headers(result.headers, self._content_type)
        if scope["method"] == "HEAD":
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return
        try:
            iterator, first, lifecycle = await _resolve_stream(result.stream)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            await self._send_setup_error(send, exc)
            return
        await send({"type": "http.response.start", "status": status, "headers": headers})
        try:
            if first is not None:
                await self._send_chunk(send, self._chunk(first))
            await self._stream_chunks(
                cast("AsyncIterator[object]", iterator),
                receive,
                send,
                sse.keepalive,
            )
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            return
        finally:
            with contextlib.suppress(Exception):
                await _finish_lifecycle(lifecycle)
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def _result_sender(kind: str, status: int) -> _Sender:
    if kind == "bytes":
        return _BytesSender(status)
    if kind == "bytes_response":
        return _BytesResponseSender(status)
    if kind == "json_response":
        return _JSONResponseSender(status)
    if kind == "stream_bytes":
        return _StreamSender(status, b"application/octet-stream")
    if kind == "stream_ndjson":
        return _NDJSONStreamSender(status, b"application/x-ndjson")
    if kind == "stream_sse":
        return _SSEStreamSender(status, b"text/event-stream")
    return _JSONSender(status)


class _Route:
    """A compiled handler: bind sources, call the user fn, send the result."""

    __slots__ = ("_bind", "_fn", "_is_async", "_send_result")

    def __init__(
        self,
        fn: Callable[..., Any],
        status: int,
        sources: _Sources,
        auth: _CompiledAuth | None,
    ) -> None:
        self._fn = fn
        self._is_async = inspect.iscoroutinefunction(fn)
        self._bind = _Binder(sources, auth)
        self._send_result = _result_sender(sources.return_kind, status)

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, path_values: dict[str, str]
    ) -> None:
        try:
            kwargs = await self._bind(scope, receive, path_values)
            result = await self._fn(**kwargs) if self._is_async else self._fn(**kwargs)
        except HTTPError as exc:
            await _send_json(send, exc.status, msgspec_encoder.encode({"error": exc.detail}))
            return
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            await _send_json(send, 500, msgspec_encoder.encode({"error": "internal server error"}))
            return
        await self._send_result(scope, receive, send, result)


class _StackScope:
    """Exit-stack helpers shared by ``BaseApp`` (owns the stacks) and
    ``BaseFactory`` (borrows them): open a resource for the owner's lifetime."""

    _stack: ExitStack
    _astack: AsyncExitStack

    def _enter[T](self, cm: AbstractContextManager[T]) -> T:
        """Open a sync context manager; closed at shutdown."""
        return self._stack.enter_context(cm)

    async def _aenter[T](self, cm: AbstractAsyncContextManager[T]) -> T:
        """Open an async context manager; closed at shutdown."""
        return await self._astack.enter_async_context(cm)


class BaseFactory(_StackScope):
    """Base for an app's factory. Subclass and add ``create_*`` methods that
    build services with ``self._enter`` / ``self._aenter``.

    The app injects its exit stacks (``es`` / ``aes``); anything opened via the
    helpers is closed when the app shuts down.
    """

    def __init__(self, es: ExitStack, aes: AsyncExitStack) -> None:
        self._stack = es
        self._astack = aes


def instantiate_factory[F](factory_cls: type[F], stack: ExitStack, astack: AsyncExitStack) -> F:
    """Build a factory, injecting whichever of ``es`` / ``aes`` its __init__ names.

    Shared by ``BaseApp`` (live wiring) and the test ``FactoryHarness`` so a
    factory is constructed identically in both — given the stacks it opens
    resources on. Package-internal (not exported) but un-underscored: it
    deliberately crosses the core/testing module boundary.
    """
    stacks = {"es": stack, "aes": astack}
    # FORWARDREF: we only need parameter names, so don't evaluate the __init__
    # annotations (they may reference TYPE_CHECKING-only imports).
    params = inspect.signature(factory_cls, annotation_format=Format.FORWARDREF).parameters
    return factory_cls(**{name: s for name, s in stacks.items() if name in params})


class BaseApp[FactoryT = None](_StackScope):
    """Subclass and override ``_wire`` to open resources and include resources/endpoints.

    The app owns the two exit stacks. Parameterize with a factory class —
    ``class MyApp(BaseApp[MyFactory])`` — and the app builds it at construction,
    injecting the stacks the factory's ``__init__`` names (``es`` for the
    ExitStack, ``aes`` for the AsyncExitStack). The built factory is then
    ``self._factory`` (typed as ``MyFactory``) inside ``_wire``, and any resource
    it registers on those stacks is closed at shutdown.

    Pass ``factory=`` to supply a prebuilt factory instead of building one — the
    seam for tests, which inject a ``create_autospec`` stand-in
    (``MyApp(factory=mock_factory)``) so the real services are never constructed.
    """

    def __init__(self, *, factory: FactoryT | None = None) -> None:
        self._static: _StaticRoutes = {}
        self._dynamic: _DynamicRoutes = {}
        self._allowed: _AllowedMethods = {}
        self._allow_cache: dict[str, bytes] = {}
        self._stack = ExitStack()
        self._astack = AsyncExitStack()
        self._factory: FactoryT = factory if factory is not None else self._make_factory()

    def _resolve_factory_type(self) -> type | None:
        """The factory class from ``BaseApp[...]``, or None if unparameterized."""
        for base in get_original_bases(type(self)):
            if get_origin(base) is BaseApp:
                args = get_args(base)
                if args and isinstance(args[0], type) and args[0] is not type(None):
                    return args[0]
        return None

    def _make_factory(self) -> FactoryT:
        factory_type = self._resolve_factory_type()
        if factory_type is None:
            return cast("FactoryT", None)
        return cast("FactoryT", instantiate_factory(factory_type, self._stack, self._astack))

    async def _wire(self) -> None:
        """Override to open resources (via ``_enter`` / ``_aenter``) and include them.

        Runs once at startup. Anything entered via the helpers is torn
        down (in reverse order) at shutdown.
        """

    def _register(self, method: str, segments: list[_Segment], handler: _Handler) -> None:
        params = tuple((i, value) for i, (is_param, value) in enumerate(segments) if is_param)
        if not params:
            route_path = "/".join(value for _, value in segments)
            if (method, route_path) in self._static:
                raise WiringError(f"{method} {route_path} is already registered")
            self._static[(method, route_path)] = handler
            self._allowed.setdefault(route_path, []).append(method)
            return

        statics = tuple((i, value) for i, (is_param, value) in enumerate(segments) if not is_param)
        bucket = self._dynamic.setdefault((method, len(segments)), [])
        if any(pattern.statics == statics for pattern in bucket):
            raise WiringError(f"{method} {_template_str(segments)} is already registered")
        bucket.append(_Pattern(statics, params, handler))

    @staticmethod
    def _check_user_source(
        resource_cls: type,
        name: str,
        user_type: type[Struct] | None,
        auth: _CompiledAuth | None,
    ) -> None:
        if user_type is None:
            return
        if auth is None:
            raise WiringError(
                f"{resource_cls.__name__}.{name} declares 'user' but no auth was given",
            )
        if not issubclass(auth.returns, user_type):
            raise WiringError(
                f"{resource_cls.__name__}.{name}: 'user' expects {user_type.__name__} "
                f"but {auth.owner}.authenticate returns {auth.returns.__name__}",
            )

    def _include(
        self,
        obj: Resource | Endpoint,
        methods: dict[str, _Verb],
        *,
        path: str,
        auth: Auth[Any, Any] | None,
    ) -> None:
        cls = type(obj)
        template = _parse_template(path)
        compiled_auth = _CompiledAuth(auth) if auth is not None else None

        registered = False
        for name, verb in methods.items():
            fn = getattr(obj, name, None)
            if fn is None:
                continue
            sources = _bind_sources(cls, name, fn, verb.http)
            self._check_user_source(cls, name, sources.user, compiled_auth)
            segments = _route_segments(
                cls, name, template, sources.path, extends_path=verb.extends_path
            )
            handler = _Route(fn, verb.status, sources, compiled_auth)
            self._register(verb.http, segments, handler)
            registered = True

        if not registered:
            raise WiringError(f"{cls.__name__} defines none of: {', '.join(methods)}")

    def _include_resource[THeaders: Struct, TUser: Struct](
        self,
        resource: Resource,
        *,
        path: str,
        auth: Auth[THeaders, TUser] | None = None,
    ) -> None:
        self._include(resource, Resource.METHODS, path=path, auth=auth)

    def _include_endpoint[THeaders: Struct, TUser: Struct](
        self,
        endpoint: Endpoint,
        *,
        path: str,
        auth: Auth[THeaders, TUser] | None = None,
    ) -> None:
        self._include(endpoint, Endpoint.METHODS, path=path, auth=auth)

    def _resolve(self, method: str, path: str) -> tuple[_Handler, dict[str, str]] | None:
        handler = self._static.get((method, path))
        if handler is not None:
            return handler, {}
        segments = path.split("/")
        for pattern in self._dynamic.get((method, len(segments)), ()):
            if pattern.matches(segments):
                values = {name: unquote(segments[i]) for i, name in pattern.params}
                return pattern.handler, values
        return None

    def _allowed_methods(self, path: str) -> tuple[str, ...]:
        allowed = list(self._allowed.get(path, ()))
        segments = path.split("/")
        for (method, count), bucket in self._dynamic.items():
            if (
                count == len(segments)
                and method not in allowed
                and any(pattern.matches(segments) for pattern in bucket)
            ):
                allowed.append(method)
        return tuple(allowed)

    def _allow_for(self, path: str) -> bytes | None:
        """The Allow header for a path, or None if no route shape matches it."""
        cached = self._allow_cache.get(path)
        if cached is not None:
            return cached
        allowed = self._allowed_methods(path)
        return _allow_header(allowed) if allowed else None

    def _finalize(self) -> None:
        """Precompute Allow headers for all static paths; runs once after wiring."""
        self._allow_cache = {
            path: _allow_header(self._allowed_methods(path)) for path in self._allowed
        }

    async def _handle_http(self, scope: Scope, receive: Receive, send: Send) -> None:
        method: str = scope["method"]
        path: str = scope["path"]

        # GET implies HEAD: serve from the GET route with the body suppressed.
        resolved = self._resolve("GET" if method == "HEAD" else method, path)
        if resolved is not None:
            handler, path_values = resolved
            await handler(
                scope, receive, _SuppressBody(send) if method == "HEAD" else send, path_values
            )
            return

        allow = self._allow_for(path)
        if allow is None:
            await _send_json(send, 404, b'{"error":"not found"}')
        elif method == "OPTIONS":
            await send(
                {"type": "http.response.start", "status": 204, "headers": [(b"allow", allow)]}
            )
            await send({"type": "http.response.body", "body": b""})
        else:
            await _send_json(send, 405, b'{"error":"method not allowed"}', [(b"allow", allow)])

    async def _close_resources(self) -> None:
        await self._astack.aclose()
        self._stack.close()

    async def _handle_lifespan(self, receive: Receive, send: Send) -> None:
        await receive()  # lifespan.startup
        try:
            await self._wire()
        except BaseException as exc:
            await self._close_resources()  # release anything entered before the failure
            await send(
                {
                    "type": "lifespan.startup.failed",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
            raise
        self._finalize()
        await send({"type": "lifespan.startup.complete"})

        await receive()  # lifespan.shutdown
        try:
            await self._close_resources()
        except BaseException as exc:
            await send(
                {
                    "type": "lifespan.shutdown.failed",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
            raise
        await send({"type": "lifespan.shutdown.complete"})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._handle_http(scope, receive, send)
        elif scope["type"] == "lifespan":
            await self._handle_lifespan(receive, send)
        else:
            raise RuntimeError(f"unsupported scope type {scope['type']!r}")
