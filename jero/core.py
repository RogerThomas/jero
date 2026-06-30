"""jero — an opinionated, msgspec-first ASGI micro-framework.

The contract:

- Resources are plain classes with any of the CRUD methods ``create`` / ``read_one`` /
  ``read_many`` / ``update`` / ``partial_update`` / ``delete``, mapped to POST / GET (item) /
  GET (collection) / PUT / PATCH / DELETE on the path given to ``include_resource``. ``read_many``
  serves the mount path itself and cannot extend it with trailing segments — items belong to
  ``read_one``.
- The mount path is a template: static segments plus ``{slot}`` params (snake_case, matching the
  Struct field names). Handlers bind path values via a ``path`` Struct whose fields must cover every
  template slot; fields beyond the slots extend the route with trailing segments (the item id).
  Path Struct fields cannot have defaults.
- Handler arguments bind by name: ``json`` (request body), ``params`` (query string), ``path``
  (URL segments), ``headers``, and ``user`` (the result of auth). Each must be annotated with a
  msgspec Struct. A handler may instead take the raw body as ``content: bytes`` (mutually exclusive
  with ``json``). Returns are a Struct, ``list[Struct]``, ``bytes`` (sent as
  application/octet-stream), or a ``BytesResponse`` / ``JSONResponse`` to control response headers.
  msgspec ``rename`` is honored everywhere (e.g. ``Struct, rename="camel"`` for camelCase on the
  wire, snake_case in code) — define your own base Struct for the wire convention.
- Auth is an object passed to ``include_resource`` implementing
  ``authenticate(headers: SomeStruct) -> UserStruct``; raise ``HTTPError(401, ...)`` to reject. When
  set, it runs for every method on the resource, before the body is decoded. Handlers that declare
  ``user`` receive its result; the annotation is checked against the authenticator's return type at
  startup.
- Dependencies are wired by hand in the overridden ``BaseApp.wire`` method (runs once at startup).
  Open resources with ``self.aenter(cm)`` / ``self.enter(cm)`` — the app holds them on exit stacks
  and closes them (reverse order) at shutdown. No ``yield``, no DI container.

All introspection happens once, at ``include_resource`` time. Routing is dict lookups: static
routes match exactly; templated routes are bucketed by (method, segment count) and matched on their
static segments — no regexes, no route-table scans, no ordering rules.

Error semantics follow REST/HTTP: an unmatched URL, or a path value that fails conversion, -> 404;
malformed query/headers -> 400; malformed JSON body -> 400; a well-formed body failing the schema
-> 422; auth failure -> 401; wrong method -> 405 with ``Allow``. HEAD is served from GET routes with
the body suppressed, and OPTIONS answers 204 with ``Allow``.
"""

import asyncio
import contextlib
import inspect
import os
import sys
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
    ExitStack,
)
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from types import NoneType, UnionType, get_original_bases
from typing import (
    Any,
    ClassVar,
    Literal,
    Protocol,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)
from urllib.parse import parse_qsl, unquote

from msgspec import DecodeError, Struct, ValidationError, convert, to_builtins
from msgspec.json import Decoder
from msgspec.structs import fields as struct_fields

from jero.codecs import msgspec_encoder
from jero.forms import FilePart, FormPart
from jero.headers import RawHeaders
from jero.links import (
    Link,
    Location,
    OperationTarget,
    PathTarget,
    Target,
    URLTarget,
    validate_path_params,
)
from jero.multipart import MultipartError, MultipartParser, parse_options_header
from jero.streaming import (
    NDJSONStreamingResponse,
    ServerSentEvent,
    SSEResponse,
    StreamingResponse,
    encode_sse,
)

# annotationlib is 3.14+. On 3.14 inspect.signature evaluates annotations by default
# (PEP 649), so instantiate_factory asks for the FORWARDREF format instead. Pre-3.14
# signature never evaluates annotations, so the format argument isn't needed there.
if sys.version_info >= (3, 14):
    from annotationlib import Format

type Scope = dict[str, Any]
type Receive = Callable[[], Awaitable[dict[str, Any]]]
type Send = Callable[[dict[str, Any]], Awaitable[None]]

# A compiled per-request handler: decode -> call -> encode.
type _Handler = Callable[[Scope, Receive, Send, dict[str, str]], Awaitable[None]]
# A template segment: (is_param, static_value_or_slot_name).
type _Segment = tuple[bool, str]
type _StaticRoutes = dict[tuple[str, str], _Handler]
type _DynamicRoutes = dict[tuple[_HttpMethod, int], list[_Pattern]]
# HTTP methods the framework speaks. GET/POST/PUT/PATCH/DELETE are handler-declarable
# (see the METHODS tables); HEAD/OPTIONS are synthesized for the Allow header.
type _HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
type _AllowedMethods = dict[str, list[_HttpMethod]]
# How a handler's return value is encoded onto the wire; see _return_kind / _result_sender.
type _ReturnKind = Literal[
    "json",
    "json-response",
    "bytes",
    "bytes-response",
    "stream-bytes",
    "stream-ndjson",
    "stream-sse",
]
# How a multipart form field's body is decoded; see _payload_kind / _decode_form_payload.
type _PayloadKind = Literal["bytes", "struct", "scalar"]
# Resolves a Struct type to its reusable typed JSON decoder (the app's per-type cache).
type _DecoderFor = Callable[[type[Struct]], Decoder[Struct]]

# Argument names the binder understands, shared by every handler kind.
_SOURCES = frozenset(
    {"json", "content", "form", "params", "path", "headers", "user", "raw_headers"}
)
# HTTP verbs that forbid a request body, whatever the handler is named.
_BODYLESS_VERBS = frozenset({"GET", "DELETE"})


@dataclass(frozen=True, slots=True)
class _Verb:
    """How one handler method maps onto HTTP."""

    method: _HttpMethod
    success_status: int
    extends_path: bool  # may path fields beyond the template slots extend the URL?


class WiringError(TypeError):
    """A router does not meet the framework contract. Raised at startup."""


class HTTPError(Exception):
    """Raise from a handler to return a JSON error response."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(kw_only=True, slots=True)
class BaseResponse[H: Struct | None = None]:
    """Base for handler returns that control response headers and status.

    Return one of the concrete subclasses. ``content-type`` defaults per kind and
    ``content-length`` is managed by the framework (ignored if supplied).

    The header *type* is a parameter ``H`` so it is known statically (and to the
    OpenAPI spec), mirroring how a handler *receives* typed headers:

    - ``headers`` — a typed Struct (parameterize as ``JSONResponse[Body, Headers]``).
      Field names map to wire names by the inverse of the request mangle
      (``x_trace_id`` -> ``x-trace-id``); scalar values are stringified (``bool`` as
      ``true``/``false``), Struct/list values are JSON-encoded; None-valued fields
      are omitted. ``H`` defaults to ``None`` (no typed headers).
    - ``raw_headers`` — the escape hatch for exotic names: literal underscores,
      specific casing, or repeats (e.g. multiple ``Set-Cookie``). A plain mapping,
      or a ``RawHeaders`` (pass the request's straight through to forward it,
      repeats and all).

    When both are given, the typed ``headers`` are emitted first, then
    ``raw_headers`` is appended, so its repeats survive.

    ``status_code`` overrides the verb's default status (201 for create, else 200)
    when set.

    ``location`` emits a ``Location`` header and ``links`` a single ``Link`` header,
    each reverse-routed to a mounted operation (see :mod:`jero.links`). The URLs are
    relative unless ``JERO_BASE_URL`` / ``JERO_TRUST_FORWARDED`` is set in the environment,
    which makes them absolute (a static origin, or one rebuilt from ``X-Forwarded-*``).
    """

    headers: H | None = None
    raw_headers: RawHeaders | Mapping[str, str] | None = None
    status_code: int | None = None
    location: Location | None = None
    links: Sequence[Link] = ()


@dataclass(kw_only=True, slots=True)
class BytesResponse[H: Struct | None = None](BaseResponse[H]):
    """Raw bytes; content-type defaults to application/octet-stream."""

    content: bytes


@dataclass(kw_only=True, slots=True)
class JSONResponse[T: Struct, H: Struct | None = None](BaseResponse[H]):
    """A Struct encoded as JSON; content-type defaults to application/json."""

    json: T


class EndpointMeta(Struct):
    """OpenAPI metadata shared by all of an ``Endpoint``'s operations."""

    tags: Sequence[str] = ()


class ResourceMeta(Struct):
    """OpenAPI metadata shared by all of a ``Resource``'s operations."""

    tags: Sequence[str] = ()


class OperationMeta(Struct):
    """OpenAPI metadata for a single operation (``meta_get``, ``meta_create``, …).

    ``operation_id`` lives here, never on the class-level ``meta`` — operation ids must
    be unique, so they can't sensibly cascade to every operation.
    """

    tags: Sequence[str] = ()
    operation_id: str | None = None


def _validate_meta(
    cls: type,
    meta: object,
    class_meta_type: type,
    operations: dict[str, object],
) -> None:
    """Fail loud if a shape is given the wrong meta type — ``EndpointMeta`` only on an
    ``Endpoint``, ``ResourceMeta`` only on a ``Resource``, ``OperationMeta`` per operation.
    """
    if meta is not None and not isinstance(meta, class_meta_type):
        raise WiringError(
            f"{cls.__name__}: meta must be {class_meta_type.__name__}, got {type(meta).__name__}",
        )
    for name, value in operations.items():
        if value is not None and not isinstance(value, OperationMeta):
            raise WiringError(
                f"{cls.__name__}: {name} must be OperationMeta, got {type(value).__name__}",
            )


class _Routable:
    """Base for the route-defining shapes (``Resource`` / ``Endpoint``).

    A concrete class declares its mount path at definition time —
    ``class Widgets(Resource, path="/widgets")`` — and it's read off the class at
    wiring. ``path`` is required on the concrete shapes (omitting it is a type error);
    it stays *optional* here only so the ``Resource`` / ``Endpoint`` base definitions
    themselves type-check. The class-path is the single source of truth that URL
    reversal (``Link`` / ``Location``) reads.

    ``ref`` is an optional string handle for that reversal: set it to address the class
    from ``Link.from_ref("name.operation")`` when importing the class would form an
    import cycle. Prefer ``from_operation`` everywhere else.
    """

    path: ClassVar[str]
    ref: ClassVar[str | None] = None

    def __init_subclass__(
        cls, *, path: str | None = None, ref: str | None = None, **kwargs: object
    ) -> None:
        super().__init_subclass__(**kwargs)
        if path is not None:
            cls.path = path
        if ref is not None:
            cls.ref = ref


class Resource(_Routable):
    """One REST resource: subclass and define any of the CRUD methods.

    ``read_one`` is the item route (its ``path`` may extend the mount with
    the item id); ``read_many`` is the collection (its path is exact).

    Optional OpenAPI metadata is declared at class definition: ``meta`` applies to every
    operation, ``meta_<op>`` to one (``meta_create``, ``meta_read_one``, …).
    """

    METHODS: ClassVar[dict[str, _Verb]] = {
        "create": _Verb("POST", 201, extends_path=True),
        "read_one": _Verb("GET", 200, extends_path=True),
        "read_many": _Verb("GET", 200, extends_path=False),
        "update": _Verb("PUT", 200, extends_path=True),
        "partial_update": _Verb("PATCH", 200, extends_path=True),
        "delete": _Verb("DELETE", 200, extends_path=True),
    }

    meta: ClassVar[ResourceMeta | None] = None
    meta_create: ClassVar[OperationMeta | None] = None
    meta_read_one: ClassVar[OperationMeta | None] = None
    meta_read_many: ClassVar[OperationMeta | None] = None
    meta_update: ClassVar[OperationMeta | None] = None
    meta_partial_update: ClassVar[OperationMeta | None] = None
    meta_delete: ClassVar[OperationMeta | None] = None

    def __init_subclass__(
        cls,
        *,
        path: str,
        ref: str | None = None,
        meta: ResourceMeta | None = None,
        meta_create: OperationMeta | None = None,
        meta_read_one: OperationMeta | None = None,
        meta_read_many: OperationMeta | None = None,
        meta_update: OperationMeta | None = None,
        meta_partial_update: OperationMeta | None = None,
        meta_delete: OperationMeta | None = None,
        **kwargs: object,
    ) -> None:
        # path / ref handling lives on _Routable
        super().__init_subclass__(path=path, ref=ref, **kwargs)
        _validate_meta(
            cls,
            meta,
            ResourceMeta,
            {
                "meta_create": meta_create,
                "meta_read_one": meta_read_one,
                "meta_read_many": meta_read_many,
                "meta_update": meta_update,
                "meta_partial_update": meta_partial_update,
                "meta_delete": meta_delete,
            },
        )
        cls.meta = meta
        cls.meta_create = meta_create
        cls.meta_read_one = meta_read_one
        cls.meta_read_many = meta_read_many
        cls.meta_update = meta_update
        cls.meta_partial_update = meta_partial_update
        cls.meta_delete = meta_delete


class Endpoint(_Routable):
    """One HTTP endpoint at a single path: subclass and define any of
    ``get`` / ``post`` / ``put`` / ``patch`` / ``delete``.

    Unlike :class:`Resource` there are no CRUD semantics — the method name
    *is* the verb, every verb returns 200, and the path is exact (no
    trailing extension). A different path is a different ``Endpoint``.

    Optional OpenAPI metadata is declared at class definition: ``meta`` applies to every
    operation, ``meta_<verb>`` to one (``meta_get``, ``meta_post``, …).
    """

    METHODS: ClassVar[dict[str, _Verb]] = {
        "get": _Verb("GET", 200, extends_path=False),
        "post": _Verb("POST", 200, extends_path=False),
        "put": _Verb("PUT", 200, extends_path=False),
        "patch": _Verb("PATCH", 200, extends_path=False),
        "delete": _Verb("DELETE", 200, extends_path=False),
    }

    meta: ClassVar[EndpointMeta | None] = None
    meta_get: ClassVar[OperationMeta | None] = None
    meta_post: ClassVar[OperationMeta | None] = None
    meta_put: ClassVar[OperationMeta | None] = None
    meta_patch: ClassVar[OperationMeta | None] = None
    meta_delete: ClassVar[OperationMeta | None] = None

    def __init_subclass__(
        cls,
        *,
        path: str,
        ref: str | None = None,
        meta: EndpointMeta | None = None,
        meta_get: OperationMeta | None = None,
        meta_post: OperationMeta | None = None,
        meta_put: OperationMeta | None = None,
        meta_patch: OperationMeta | None = None,
        meta_delete: OperationMeta | None = None,
        **kwargs: object,
    ) -> None:
        # path / ref handling lives on _Routable
        super().__init_subclass__(path=path, ref=ref, **kwargs)
        _validate_meta(
            cls,
            meta,
            EndpointMeta,
            {
                "meta_get": meta_get,
                "meta_post": meta_post,
                "meta_put": meta_put,
                "meta_patch": meta_patch,
                "meta_delete": meta_delete,
            },
        )
        cls.meta = meta
        cls.meta_get = meta_get
        cls.meta_post = meta_post
        cls.meta_put = meta_put
        cls.meta_patch = meta_patch
        cls.meta_delete = meta_delete


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
    headers: Struct | None
    raw_headers: RawHeaders | Mapping[str, str] | None
    status_code: int | None
    location: Location | None
    links: Sequence[Link]


def _allow_header(allowed: Sequence[_HttpMethod]) -> bytes:
    # copy: HEAD/OPTIONS are appended below without mutating the caller's list
    methods: list[_HttpMethod] = [*allowed]
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


def _wire_header_pairs(scope: Scope) -> list[tuple[str, str]]:
    """Header pairs with real wire names preserved, for the opaque RawHeaders bag.

    Distinct from _raw_headers, which snake_cases names for msgspec ``convert``.
    """
    return [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]]


def _mangle_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key.lower().replace("-", "_"): value for key, value in headers.items()}


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


def _decode_json_body(body: bytes, decoder: Decoder[Struct]) -> Struct:
    try:
        return decoder.decode(body)
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


def _form_part_types(ann: object) -> tuple[object, object | None] | None:
    origin = get_origin(ann)
    if origin is FormPart:
        args = get_args(ann)
        return args[0], args[1]
    if origin is FilePart:
        args = get_args(ann)
        return (bytes, args[0]) if len(args) == 1 else None
    if ann is FilePart:
        return bytes, FilePart.__type_params__[0].__default__
    if ann is not FormPart:
        return None
    return None


def _strip_optional(ann: object) -> tuple[object, bool]:
    # `X | None` is a types.UnionType, but get_type_hints/msgspec may resolve the same
    # annotation to typing.Optional (origin typing.Union) on older Pythons — accept both.
    origin = get_origin(ann)
    if origin is not UnionType and origin is not Union:
        return ann, False
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


def _payload_kind(cls: type, method: str, field_name: str, ann: object) -> _PayloadKind:
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
    headers_type: type[Struct] | None
    payload_kind: _PayloadKind
    decoder: Decoder[Struct] | None  # reusable typed decoder; set iff payload_kind == "struct"
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
    raw_headers: RawHeaders
    body: bytes


def _compile_form(
    cls: type, method: str, form_type: type[Struct], decoder_for: _DecoderFor
) -> _FormSpec:
    descriptors: list[_FormField] = []
    for field in struct_fields(form_type):
        field_type, optional = _strip_optional(field.type)
        item_type, repeated = _strip_list(field_type)
        part_types = _form_part_types(item_type)
        enveloped = part_types is not None
        file = item_type is FilePart or get_origin(item_type) is FilePart
        if part_types is None:
            payload_type = item_type
            headers_type = None
        else:
            payload_type = part_types[0]
            headers_ann = part_types[1]
            headers_type = (
                None
                if _is_none_type(headers_ann)
                else _struct_annotation(cls, method, f"{field.name}.headers", headers_ann)
            )
        payload_kind = _payload_kind(cls, method, field.name, payload_type)
        decoder = (
            decoder_for(cast("type[Struct]", payload_type)) if payload_kind == "struct" else None
        )
        descriptors.append(
            _FormField(
                name=field.name,
                wire_name=field.encode_name,
                payload_type=payload_type,
                headers_type=headers_type,
                payload_kind=payload_kind,
                decoder=decoder,
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
    media_type, options = parse_options_header(value)
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
        for raw_part in MultipartParser(BytesIO(body), parsed[1], strict=True):
            if raw_part.name is None:
                raise HTTPError(400, "malformed multipart body")
            headers = _part_headers(raw_part.headerlist)
            parts[raw_part.name].append(
                _Part(
                    name=raw_part.name,
                    filename=raw_part.filename,
                    content_type=_part_content_type(headers),
                    headers=headers,
                    raw_headers=RawHeaders(raw_part.headerlist),
                    body=raw_part.raw,
                )
            )
    except MultipartError as exc:
        raise HTTPError(400, str(exc)) from None
    return parts


def _decode_form_payload(field: _FormField, part: _Part) -> object:
    if field.payload_kind == "bytes":
        return part.body
    if field.decoder is not None:  # struct payload — reuse the prebuilt typed decoder
        try:
            return field.decoder.decode(part.body)
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
        None
        if field.headers_type is None
        else _convert_source(_mangle_headers(part.headers), field.headers_type, 400)
    )
    if field.file:
        if part.filename is None:
            raise HTTPError(422, f"form field {field.wire_name!r} requires a filename")
        return FilePart(
            data=cast("bytes", data),
            content_type=part.content_type,
            headers=headers,
            raw_headers=part.raw_headers,
            filename=part.filename,
        )
    return FormPart(
        data=data,
        content_type=part.content_type,
        headers=headers,
        raw_headers=part.raw_headers,
    )


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
    json_decoder: Decoder[Struct] | None = None  # prebuilt decoder for the json body type
    form: _FormSpec | None = None
    params: type[Struct] | None = None
    path: type[Struct] | None = None
    headers: type[Struct] | None = None
    user: type[Struct] | None = None
    content: bool = False
    raw_headers: bool = False
    return_kind: _ReturnKind = "json"
    arity: int = 0  # number of binding args the handler declares


def _return_kind(ann: object) -> _ReturnKind | None:  # noqa: C901
    if isinstance(ann, type):
        if issubclass(ann, StreamingResponse):
            return "stream-bytes"
        if issubclass(ann, NDJSONStreamingResponse):
            return "stream-ndjson"
        if issubclass(ann, SSEResponse):
            return "stream-sse"
        if issubclass(ann, BytesResponse):
            return "bytes-response"
        if issubclass(ann, JSONResponse):
            return "json-response"
        if issubclass(ann, BaseResponse):
            return None  # the base is abstract; return a concrete subclass
        if issubclass(ann, Struct):
            return "json"
        if ann is bytes:
            return "bytes"
    args = get_args(ann)
    origin = get_origin(ann)
    if origin is StreamingResponse:
        return "stream-bytes"
    if origin is NDJSONStreamingResponse:
        return "stream-ndjson"
    if origin is SSEResponse:
        return "stream-sse"
    if origin is BytesResponse:
        return "bytes-response"
    if origin is JSONResponse:
        return "json-response"
    if (
        origin is list
        and len(args) == 1
        and isinstance(args[0], type)
        and issubclass(args[0], Struct)
    ):
        return "json"
    return None


def _bind_sources(  # noqa: C901
    cls: type, name: str, fn: Callable[..., Any], http_method: _HttpMethod, decoder_for: _DecoderFor
) -> _Sources:
    """Resolve and validate the Struct types for a handler's arguments."""
    hints = get_type_hints(fn)
    types: dict[str, type[Struct]] = {}
    form: _FormSpec | None = None
    wants_content = False
    wants_raw_headers = False

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
        if param.name == "raw_headers":
            if hints.get("raw_headers") is not RawHeaders:
                raise WiringError(
                    f"{cls.__name__}.{name}: 'raw_headers' must be annotated as RawHeaders",
                )
            wants_raw_headers = True
            continue
        source_type = _struct_annotation(cls, name, param.name, hints.get(param.name))
        if param.name == "form":
            form = _compile_form(cls, name, source_type, decoder_for)
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
    if return_kind == "stream-sse" and http_method != "GET":
        raise WiringError(f"{cls.__name__}.{name}: SSEResponse is only allowed on GET handlers")

    json_type = types.get("json")
    json_decoder = decoder_for(json_type) if json_type is not None else None
    arity = len(types) + (form is not None) + wants_content + wants_raw_headers

    return _Sources(
        **types,
        json_decoder=json_decoder,
        form=form,
        content=wants_content,
        raw_headers=wants_raw_headers,
        return_kind=return_kind,
        arity=arity,
    )


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
        "_arity",
        "_auth",
        "_form_spec",
        "_headers_type",
        "_json_decoder",
        "_needs_body",
        "_needs_raw",
        "_params_type",
        "_path_type",
        "_wants_content",
        "_wants_raw_headers",
        "_wants_user",
    )

    def __init__(self, sources: _Sources, auth: _CompiledAuth | None) -> None:
        self._json_decoder = sources.json_decoder
        self._form_spec = sources.form
        self._params_type = sources.params
        self._path_type = sources.path
        self._headers_type = sources.headers
        self._auth = auth
        self._wants_content = sources.content
        self._wants_raw_headers = sources.raw_headers
        self._wants_user = sources.user is not None
        self._arity = sources.arity
        self._needs_raw = (
            auth is not None or sources.headers is not None or sources.form is not None
        )
        # The three body sources are mutually exclusive (checked at wiring); read once.
        self._needs_body = (
            sources.json_decoder is not None or sources.form is not None or sources.content
        )

    def _one(
        self,
        scope: Scope,
        raw_headers: dict[str, str],
        path_values: dict[str, str],
        user: Struct | None,
        body: bytes,
    ) -> object:
        """Resolve the single declared binding source, skipping the kwargs dict."""
        if self._json_decoder is not None:
            return _decode_json_body(body, self._json_decoder)
        if self._form_spec is not None:
            return _decode_form_body(body, raw_headers, self._form_spec)
        if self._wants_content:
            return body
        if self._path_type is not None:
            return _convert_source(path_values, self._path_type, 404, "not found")
        if self._headers_type is not None:
            return _convert_source(raw_headers, self._headers_type, 400)
        if self._params_type is not None:
            raw_query = dict(parse_qsl(scope["query_string"].decode("latin-1")))
            return _convert_source(raw_query, self._params_type, 400)
        if self._wants_raw_headers:
            return RawHeaders(_wire_header_pairs(scope))
        return user

    async def __call__(  # noqa: C901  — flat per-source binding; inlined body read (perf)
        self, scope: Scope, receive: Receive, path_values: dict[str, str]
    ) -> object:
        raw_headers = _raw_headers(scope) if self._needs_raw else {}
        user = await self._auth(raw_headers) if self._auth is not None else None
        body = b""
        if self._needs_body:
            chunks: list[bytes] = []  # inlined body read (was _read_body) to save a coroutine hop
            while True:
                message = await receive()
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    break
            body = chunks[0] if len(chunks) == 1 else b"".join(chunks)
        # 0- or 1-source handlers skip the kwargs dict: call positionally (see _Route).
        if self._arity == 0:
            return None
        if self._arity == 1:
            return self._one(scope, raw_headers, path_values, user, body)
        kwargs: dict[str, object] = {}
        if self._wants_user:
            kwargs["user"] = user
        if self._path_type is not None:
            kwargs["path"] = _convert_source(path_values, self._path_type, 404, "not found")
        if self._headers_type is not None:
            kwargs["headers"] = _convert_source(raw_headers, self._headers_type, 400)
        if self._params_type is not None:
            raw_query = dict(parse_qsl(scope["query_string"].decode("latin-1")))
            kwargs["params"] = _convert_source(raw_query, self._params_type, 400)
        if self._json_decoder is not None:
            kwargs["json"] = _decode_json_body(body, self._json_decoder)
        elif self._form_spec is not None:
            kwargs["form"] = _decode_form_body(body, raw_headers, self._form_spec)
        elif self._wants_content:
            kwargs["content"] = body
        if self._wants_raw_headers:
            kwargs["raw_headers"] = RawHeaders(_wire_header_pairs(scope))
        return kwargs


type _Sender = Callable[[Scope, Receive, Send, Any], Awaitable[None]]


def _encode_header_value(value: object) -> str:
    """One header value as a string: scalars plain (``bool`` as ``true``/``false``,
    stringy types like UUID/datetime/Decimal as their bare text), Struct/list values
    JSON-encoded.

    The fallback routes through ``to_builtins`` so an extended scalar collapses to a
    string (and is emitted bare), while a genuinely structured value becomes a
    dict/list that is then JSON-encoded — never a quoted JSON scalar."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Enum):
        return _encode_header_value(value.value)
    builtin = to_builtins(value)
    if isinstance(builtin, str):
        return builtin
    return msgspec_encoder.encode(builtin).decode()


# (attr name, inverse-mangled wire name) per header Struct type — computed once,
# not per request. `msgspec.structs.fields` is ~µs-expensive, so never call it on the
# hot path; the field set and wire names are fixed per type.
_HEADER_FIELDS: dict[type[Struct], tuple[tuple[str, str], ...]] = {}


def _header_fields(struct_type: type[Struct]) -> tuple[tuple[str, str], ...]:
    if struct_type not in _HEADER_FIELDS:
        _HEADER_FIELDS[struct_type] = tuple(
            (field.name, field.name.replace("_", "-")) for field in struct_fields(struct_type)
        )
    return _HEADER_FIELDS[struct_type]


def _typed_header_items(headers: Struct | None) -> list[tuple[str, str]]:
    """A typed header Struct as wire pairs: field name inverse-mangled
    (``x_trace_id`` -> ``x-trace-id``), value encoded. None-valued fields omitted."""
    if headers is None:
        return []
    items: list[tuple[str, str]] = []
    for attr, wire in _header_fields(type(headers)):
        value = getattr(headers, attr)
        if value is None:
            continue
        items.append((wire, _encode_header_value(value)))
    return items


def _user_header_items(
    raw_headers: RawHeaders | Mapping[str, str] | None,
) -> list[tuple[str, str]]:
    """The raw_headers escape-hatch pairs. A RawHeaders forwards every pair (repeats
    included, e.g. Set-Cookie); a plain mapping yields its items."""
    if raw_headers is None:
        return []
    if isinstance(raw_headers, RawHeaders):
        return raw_headers.multi_items()
    return list(raw_headers.items())


def _header_items(
    typed: Struct | None, raw: RawHeaders | Mapping[str, str] | None
) -> list[tuple[str, str]]:
    """Combined response header pairs: typed Struct first, then raw_headers appended."""
    return _typed_header_items(typed) + _user_header_items(raw)


def _response_headers(
    typed: Struct | None,
    raw: RawHeaders | Mapping[str, str] | None,
    default_content_type: bytes,
    payload_length: int,
) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    has_content_type = False
    for key, value in _header_items(typed, raw):
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


@dataclass(frozen=True, slots=True)
class _RouteRef:
    """A mounted operation, as the reverse registry stores it: the compiled path
    segments (template slots + any trailing id, by wire name) and the path Struct type."""

    segments: tuple[_Segment, ...]
    path_type: type[Struct] | None


def _build_url(route_ref: _RouteRef, path: Struct | None) -> str:
    segments = route_ref.segments
    # path is None only for slot-less routes: both validators reject a None against a
    # route with path slots before we get here, so there is never a param to fill.
    if path is None:
        return "/".join(value for _, value in segments)
    data = to_builtins(path)
    return "/".join(
        _encode_header_value(data[value]) if is_param else value for is_param, value in segments
    )


def _forwarded_first(scope: Scope, name: bytes) -> str | None:
    """The first value of a (lowercased) request header from the ASGI scope — the
    left-most entry of any comma list, i.e. the original client-facing value."""
    for key, value in scope["headers"]:
        if key == name:
            return value.decode("latin-1").split(",")[0].strip()
    return None


def _public_base(scope: Scope) -> str | None:
    """The public origin (``scheme://host[:port]``) the client used, reconstructed from
    the ``X-Forwarded-*`` headers (falling back to ``Host``). ``None`` when no host can be
    determined — the caller then stays relative rather than emit a hostless URL. Caller has
    already decided the proxy is trusted."""
    host = _forwarded_first(scope, b"x-forwarded-host") or _forwarded_first(scope, b"host")
    if host is None:
        return None
    proto = _forwarded_first(scope, b"x-forwarded-proto") or scope.get("scheme") or "http"
    port = _forwarded_first(scope, b"x-forwarded-port")
    default_port = (proto == "https" and port == "443") or (proto == "http" and port == "80")
    if port and ":" not in host and not default_port:
        host = f"{host}:{port}"
    return f"{proto}://{host}"


def _forwarded_prefix(scope: Scope) -> str:
    """The ``X-Forwarded-Prefix`` path the proxy stripped (e.g. ``/api``), or empty."""
    prefix = _forwarded_first(scope, b"x-forwarded-prefix")
    return prefix.rstrip("/") if prefix is not None else ""


def _forwarded_config_from_env() -> tuple[str | None, bool]:
    """Read the reverse-routing URL base from the environment — available before the
    factory exists, so it sidesteps the settings-only-in-the-factory ordering problem.

    ``JERO_BASE_URL`` is a static public origin (absolute URLs against it, no header
    trust); ``JERO_TRUST_FORWARDED`` (truthy) rebuilds the origin per request from the
    ``X-Forwarded-*`` headers. They are mutually exclusive — one source for the base."""
    base_url = os.environ.get("JERO_BASE_URL")
    trust = os.environ.get("JERO_TRUST_FORWARDED", "").lower() in {"1", "true", "yes", "on"}
    if base_url is not None and trust:
        raise WiringError(
            "JERO_BASE_URL and JERO_TRUST_FORWARDED are mutually exclusive — set one, not "
            "both (they are two sources for the same reverse-routed URL base)",
        )
    return (base_url.rstrip("/") if base_url is not None else None, trust)


class _Reverser:
    """The wiring-time reverse registry: maps each mounted operation (by its function,
    and by its optional ``ref`` name) to the path it resolves to. Built as routes are
    included; queried at response send to turn a ``Location`` / ``Link`` target into a
    URL. Deliberately not a dataclass — it owns two mutating indexes filled at wiring.

    The URL base is read once from the environment (see :func:`_forwarded_config_from_env`):
    ``base_url`` → a static absolute origin; ``trust_forwarded`` → the public origin rebuilt
    per request from ``X-Forwarded-*``; neither → a relative path."""

    def __init__(self, *, base_url: str | None, trust_forwarded: bool) -> None:
        self._base_url = base_url
        self._trust_forwarded = trust_forwarded
        self._ops: dict[Callable[..., object], _RouteRef] = {}
        self._refs: dict[tuple[str, str], _RouteRef] = {}

    def register(
        self,
        operation: Callable[..., object],
        ref_name: str | None,
        op_name: str,
        route_ref: _RouteRef,
    ) -> None:
        """Index one mounted operation, by its function and (if set) its ``ref`` name.
        A function mounted at two paths, or a duplicate ``ref``, is a loud ``WiringError``."""
        existing = self._ops.get(operation)
        if existing is not None and existing != route_ref:
            raise WiringError(
                f"ambiguous reverse target: {operation.__qualname__} is mounted at more "
                f"than one path (shared via a mixin?); use ref= to address it instead",
            )
        self._ops[operation] = route_ref
        if ref_name is not None:
            key = (ref_name, op_name)
            if key in self._refs:
                raise WiringError(f"duplicate ref {ref_name!r} for operation {op_name!r}")
            self._refs[key] = route_ref

    def _public_prefix(self, scope: Scope) -> str:
        """The string prepended to a reversed path: a static ``base_url``, else the
        proxy's public origin + ``X-Forwarded-Prefix`` when trusted, else empty (relative)."""
        if self._base_url is not None:
            return self._base_url
        if self._trust_forwarded:
            return (_public_base(scope) or "") + _forwarded_prefix(scope)
        return ""

    def resolve(self, target: Target, scope: Scope) -> str:
        """Turn a ``Location`` / ``Link`` target into a URL: an absolute literal passes
        through verbatim; a relative literal, an operation, or a ref all pick up the app's
        URL base (static origin, or the trusted proxy's, plus prefix) when configured."""
        if isinstance(target, URLTarget):
            return target.url
        if isinstance(target, PathTarget):
            return self._public_prefix(scope) + target.path
        if isinstance(target, OperationTarget):
            if target.operation not in self._ops:
                raise WiringError(f"{target.operation.__qualname__} is not a mounted operation")
            route_ref = self._ops[target.operation]
        else:
            key = (target.name, target.operation)
            if key not in self._refs:
                raise WiringError(
                    f"no mounted operation for ref {target.name!r}.{target.operation!r}"
                )
            route_ref = self._refs[key]
            # from_ref can't carry the type statically, so its path check is deferred to
            # here — the same exact-type validator from_operation runs at construction.
            validate_path_params(
                route_ref.path_type, target.path, f"{target.name}.{target.operation}"
            )
        return self._public_prefix(scope) + _build_url(route_ref, target.path)


def _format_link(url: str, link: Link) -> str:
    parts = [f"<{url}>", f'rel="{link.rel}"']
    if link.title is not None:
        parts.append(f'title="{link.title}"')
    if link.media_type is not None:
        parts.append(f'type="{link.media_type}"')
    return "; ".join(parts)


def _link_header_pairs(
    reverser: _Reverser, scope: Scope, location: Location | None, links: Sequence[Link]
) -> list[tuple[bytes, bytes]]:
    """The resolved ``Location`` / ``Link`` header pairs for a response (empty when the
    response sets neither). Links join into one header value, per RFC 8288."""
    pairs: list[tuple[bytes, bytes]] = []
    if location is not None:
        pairs.append((b"location", reverser.resolve(location.target, scope).encode("latin-1")))
    if links:
        value = ", ".join(
            _format_link(reverser.resolve(link.target, scope), link) for link in links
        )
        pairs.append((b"link", value.encode("latin-1")))
    return pairs


@dataclass(slots=True)
class _BytesSender:
    _status: int

    async def __call__(self, scope: Scope, receive: Receive, send: Send, result: bytes) -> None:
        _ = (scope, receive)
        headers = _response_headers(None, None, b"application/octet-stream", len(result))
        await _send_payload(send, self._status, result, headers)


@dataclass(slots=True)
class _BytesResponseSender:
    _status: int
    _reverser: _Reverser

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, result: BytesResponse[Any]
    ) -> None:
        _ = receive
        status = result.status_code if result.status_code is not None else self._status
        headers = _response_headers(
            result.headers, result.raw_headers, b"application/octet-stream", len(result.content)
        )
        headers += _link_header_pairs(self._reverser, scope, result.location, result.links)
        await _send_payload(send, status, result.content, headers)


@dataclass(slots=True)
class _JSONResponseSender:
    _status: int
    _reverser: _Reverser

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, result: JSONResponse[Any, Any]
    ) -> None:
        _ = receive
        status = result.status_code if result.status_code is not None else self._status
        payload = msgspec_encoder.encode(result.json)
        headers = _response_headers(
            result.headers, result.raw_headers, b"application/json", len(payload)
        )
        headers += _link_header_pairs(self._reverser, scope, result.location, result.links)
        await _send_payload(send, status, payload, headers)


@dataclass(slots=True)
class _JSONSender:
    _status: int

    async def __call__(self, scope: Scope, receive: Receive, send: Send, result: object) -> None:
        # Inlines _send_json (kept for error paths) to save a coroutine hop on the
        # hot JSON response path.
        _ = (scope, receive)
        payload = msgspec_encoder.encode(result)
        await send(
            {
                "type": "http.response.start",
                "status": self._status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def _stream_headers(
    typed: Struct | None,
    raw: RawHeaders | Mapping[str, str] | None,
    default_content_type: bytes,
) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    has_content_type = False
    for key, value in _header_items(typed, raw):
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


async def _cancel_if_task(task: asyncio.Task[Any] | None) -> None:
    """Cancel a task (if there is one) and await it, swallowing the CancelledError."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _next_or_disconnect[T](
    iterator: AsyncIterator[T],
    receive: Receive,
) -> tuple[str, T | None]:
    next_task: asyncio.Task[T] = asyncio.create_task(_anext(iterator))
    try:
        while True:
            receive_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(_receive(receive))
            tasks: set[asyncio.Task[Any]] = {next_task, receive_task}
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if receive_task in done:
                message = receive_task.result()
                if message["type"] == "http.disconnect":
                    await _cancel_if_task(next_task)
                    return "disconnect", None
                continue
            await _cancel_if_task(receive_task)
            try:
                return "item", next_task.result()
            except StopAsyncIteration:
                return "done", None
    except Exception:
        await _cancel_if_task(next_task)
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
        inner = aiter(cast(AsyncIterable[T], first))
        lifecycle = cast(AsyncIterator[AsyncIterable[T]], outer)
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


@dataclass(slots=True)
class _StreamSender:
    _status: int
    _content_type: bytes
    _reverser: _Reverser

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
        status = result.status_code if result.status_code is not None else self._status
        headers = _stream_headers(result.headers, result.raw_headers, self._content_type)
        headers += _link_header_pairs(self._reverser, scope, result.location, result.links)
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
                        await _cancel_if_task(keepalive_task)
                        await _cancel_if_task(next_task)
                        await _close_async_iter(iterator)
                        return
                    await _cancel_if_task(keepalive_task)
                    continue
                await _cancel_if_task(receive_task)
                if keepalive_task is not None and keepalive_task in done:
                    await self._send_chunk(send, b": ping\n\n")
                    continue
                await _cancel_if_task(keepalive_task)
                try:
                    item = next_task.result()
                except StopAsyncIteration:
                    return
                await self._send_chunk(send, self._chunk(item))
                next_task = asyncio.create_task(_anext(iterator))
        except Exception:
            await _cancel_if_task(next_task)
            raise

    def _chunk(self, item: object) -> bytes:
        if not isinstance(item, (Struct, ServerSentEvent, str)):
            raise TypeError("SSEResponse items must be Struct, str, or ServerSentEvent")
        event = cast("Struct | ServerSentEvent[Any] | str", item)
        return encode_sse(event)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        result: _StreamResult,
    ) -> None:
        sse = cast("SSEResponse[Any]", result)
        status = result.status_code if result.status_code is not None else self._status
        headers = _stream_headers(result.headers, result.raw_headers, self._content_type)
        headers += _link_header_pairs(self._reverser, scope, result.location, result.links)
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


def _result_sender(kind: _ReturnKind, status: int, reverser: _Reverser) -> _Sender:
    if kind == "bytes":
        return _BytesSender(status)
    if kind == "bytes-response":
        return _BytesResponseSender(status, reverser)
    if kind == "json-response":
        return _JSONResponseSender(status, reverser)
    if kind == "stream-bytes":
        return _StreamSender(status, b"application/octet-stream", reverser)
    if kind == "stream-ndjson":
        return _NDJSONStreamSender(status, b"application/x-ndjson", reverser)
    if kind == "stream-sse":
        return _SSEStreamSender(status, b"text/event-stream", reverser)
    return _JSONSender(status)


class _Route:
    """A compiled handler: bind sources, call the user fn, send the result."""

    __slots__ = ("_arity", "_bind", "_fn", "_is_async", "_send_result")

    def __init__(
        self,
        fn: Callable[..., Any],
        status: int,
        sources: _Sources,
        auth: _CompiledAuth | None,
        reverser: _Reverser,
    ) -> None:
        self._fn = fn
        self._is_async = inspect.iscoroutinefunction(fn)
        self._bind = _Binder(sources, auth)
        self._send_result = _result_sender(sources.return_kind, status, reverser)
        self._arity = sources.arity

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, path_values: dict[str, str]
    ) -> None:
        try:
            bound = await self._bind(scope, receive, path_values)
            # 0/1-source handlers are called positionally (no kwargs dict); see _Binder.
            if self._arity >= 2:
                kwargs = cast("dict[str, object]", bound)
                result = await self._fn(**kwargs) if self._is_async else self._fn(**kwargs)
            elif self._arity == 1:
                result = await self._fn(bound) if self._is_async else self._fn(bound)
            else:
                result = await self._fn() if self._is_async else self._fn()
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

    def enter[T](self, cm: AbstractContextManager[T]) -> T:
        """Open a sync context manager; closed at shutdown."""
        return self._stack.enter_context(cm)

    async def aenter[T](self, cm: AbstractAsyncContextManager[T]) -> T:
        """Open an async context manager; closed at shutdown."""
        return await self._astack.enter_async_context(cm)


class BaseFactory(_StackScope):
    """Base for an app's factory. Subclass and add ``create_*`` methods that
    build services with ``self.enter`` / ``self.aenter``.

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
    # We only need parameter names, so don't evaluate the __init__ annotations (they may
    # reference TYPE_CHECKING-only imports). On 3.14 that means asking signature for the
    # FORWARDREF format; pre-3.14 signature never evaluates annotations to begin with.
    if sys.version_info >= (3, 14):
        params = inspect.signature(factory_cls, annotation_format=Format.FORWARDREF).parameters
    else:
        params = inspect.signature(factory_cls).parameters
    return factory_cls(**{name: s for name, s in stacks.items() if name in params})


class BaseApp[FactoryT = None](_StackScope, ABC):
    """Subclass and override ``wire`` to open resources and include resources/endpoints.

    The app owns the two exit stacks. Parameterize with a factory class —
    ``class MyApp(BaseApp[MyFactory])`` — and the app builds it at construction,
    injecting the stacks the factory's ``__init__`` names (``es`` for the
    ExitStack, ``aes`` for the AsyncExitStack). The built factory is then
    ``self.factory`` (typed as ``MyFactory``) inside ``wire``, and any resource
    it registers on those stacks is closed at shutdown.

    Pass ``factory=`` to supply a prebuilt factory instead of building one — the
    seam for tests, which inject a ``create_autospec`` stand-in
    (``MyApp(factory=mock_factory)``) so the real services are never constructed.

    Reverse-routed ``Location`` / ``Link`` URLs are relative unless the environment sets
    ``JERO_BASE_URL`` (a static public origin) or ``JERO_TRUST_FORWARDED`` (rebuild the
    origin per request from ``X-Forwarded-*``); see :func:`_forwarded_config_from_env`.
    """

    def __init__(self, *, factory: FactoryT | None = None) -> None:
        self._static: _StaticRoutes = {}
        self._dynamic: _DynamicRoutes = {}
        self._allowed: _AllowedMethods = {}
        self._allow_cache: dict[str, bytes] = {}
        self._decoders: dict[type[Struct], Decoder[Struct]] = {}
        base_url, trust_forwarded = _forwarded_config_from_env()
        self._reverser = _Reverser(base_url=base_url, trust_forwarded=trust_forwarded)
        self._stack = ExitStack()
        self._astack = AsyncExitStack()
        self.factory: FactoryT = factory if factory is not None else self._make_factory()

    def _decoder(self, struct_type: type[Struct]) -> Decoder[Struct]:
        """The reusable typed JSON decoder for ``struct_type``, built once per app.

        Decoders are keyed by type, so models shared across handlers (a ``WidgetIn``
        used by both ``create`` and ``update``) share one decoder. Populated only at
        wiring time; the binder holds the resolved decoder, so the request path does
        no lookup.
        """
        if struct_type not in self._decoders:
            self._decoders[struct_type] = Decoder(struct_type)
        return self._decoders[struct_type]

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

    @abstractmethod
    async def wire(self) -> None:
        """Override to open resources (via ``enter`` / ``aenter``) and include them.

        Runs once at startup. Anything entered via the helpers is torn
        down (in reverse order) at shutdown.

        Abstract: every ``BaseApp`` subclass must implement it. A subclass that
        omits it is flagged at its instantiation site by the type checker.
        """

    def _register(self, method: _HttpMethod, segments: list[_Segment], handler: _Handler) -> None:
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
        auth: Auth[Any, Any] | None,
    ) -> None:
        cls = type(obj)
        path = getattr(cls, "path", None)
        if path is None:
            raise WiringError(
                f"{cls.__name__}: no path — declare it on the class, "
                f"e.g. `class {cls.__name__}(..., path='/...')`.",
            )
        template = _parse_template(path)
        compiled_auth = _CompiledAuth(auth) if auth is not None else None

        registered = False
        for name, verb in methods.items():
            fn = getattr(obj, name, None)
            if fn is None:
                continue
            sources = _bind_sources(cls, name, fn, verb.method, self._decoder)
            self._check_user_source(cls, name, sources.user, compiled_auth)
            segments = _route_segments(
                cls, name, template, sources.path, extends_path=verb.extends_path
            )
            handler = _Route(fn, verb.success_status, sources, compiled_auth, self._reverser)
            self._register(verb.method, segments, handler)
            self._reverser.register(
                fn.__func__, cls.ref, name, _RouteRef(tuple(segments), sources.path)
            )
            registered = True

        if not registered:
            raise WiringError(f"{cls.__name__} defines none of: {', '.join(methods)}")

    def include_resource[THeaders: Struct, TUser: Struct](
        self,
        resource: Resource,
        *,
        auth: Auth[THeaders, TUser] | None = None,
    ) -> None:
        """Register a ``Resource``'s CRUD methods as routes, optionally behind ``auth``."""
        self._include(resource, Resource.METHODS, auth=auth)

    def include_endpoint[THeaders: Struct, TUser: Struct](
        self,
        endpoint: Endpoint,
        *,
        auth: Auth[THeaders, TUser] | None = None,
    ) -> None:
        """Register an ``Endpoint``'s verb methods as routes, optionally behind ``auth``."""
        self._include(endpoint, Endpoint.METHODS, auth=auth)

    def _resolve(self, method: str, path: str) -> tuple[_Handler, dict[str, str]] | None:
        # Static hit is the hot path: look it up directly, before narrowing the verb
        # (a non-route method simply misses). The cast is paid only on the dynamic path.
        handler = self._static.get((method, path))
        if handler is not None:
            return handler, {}
        segments = path.split("/")
        verb = cast("_HttpMethod", method)
        for pattern in self._dynamic.get((verb, len(segments)), ()):
            if pattern.matches(segments):
                values = {name: unquote(segments[i]) for i, name in pattern.params}
                return pattern.handler, values
        return None

    def _allowed_methods(self, path: str) -> tuple[_HttpMethod, ...]:
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

    async def _close_resources(self) -> None:
        await self._astack.aclose()
        self._stack.close()

    async def _handle_lifespan(self, receive: Receive, send: Send) -> None:
        await receive()  # lifespan.startup
        try:
            await self.wire()
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
        if scope["type"] != "http":
            if scope["type"] == "lifespan":
                await self._handle_lifespan(receive, send)
                return
            raise RuntimeError(f"unsupported scope type {scope['type']!r}")

        # HTTP is the hot path; inlined here (was _handle_http) to save a coroutine hop.
        method: str = scope["method"]
        path: str = scope["path"]
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
