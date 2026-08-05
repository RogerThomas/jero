"""jero — an opinionated, msgspec-first ASGI micro-framework.

The contract:

- Resources are plain classes with any of the CRUD methods ``create`` / ``read_one`` /
  ``read_many`` / ``update_full`` / ``update_partial`` / ``delete``, mapped to POST / GET (item) /
  GET (collection) / PUT / PATCH / DELETE on the path given to ``_include_resource``. ``read_many``
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
- Auth is an object passed to ``_include_resource`` implementing
  ``authenticate(headers: SomeStruct) -> UserStruct``; raise an ``HTTPError`` subclass
  to reject. When set, it runs for every method on the resource, before the body is decoded.
  Handlers that declare
  ``user`` receive its result; the annotation is checked against the authenticator's return type at
  startup. An authenticator declaring ``-> UserStruct | None`` makes credentials an input
  rather than a gate: returning ``None`` reports that none were presented and the route's
  handlers — which must all declare ``user: UserStruct | None`` — serve the caller
  anonymously, while invalid credentials are still a 401.
- Dependencies are wired by hand in the overridden ``BaseApp.wire`` method (runs once at startup).
  Open resources with ``self._aenter(cm)`` / ``self._enter(cm)`` — the app holds them on exit stacks
  and closes them (reverse order) at shutdown. No ``yield``, no DI container.

All introspection happens once, at ``_include_resource`` time. Routing is dict lookups: static
routes match exactly; templated routes are bucketed by (method, segment count) and matched on their
static segments — no regexes, no route-table scans, no ordering rules.

Error semantics follow REST/HTTP: an unmatched URL, or a path value that fails conversion, -> 404;
malformed query/headers -> 400; malformed JSON body -> 400; a well-formed body failing the schema
-> 422; auth failure -> 401; wrong method -> 405 with ``Allow``. HEAD is served from GET routes with
the body suppressed, and OPTIONS answers 204 with ``Allow``.
"""

import asyncio
import contextlib
import html
import inspect
import logging
import os
import sys
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
    ExitStack,
    asynccontextmanager,
)
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from pathlib import Path
from time import perf_counter
from types import NoneType, UnionType, get_original_bases
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    Protocol,
    Self,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)
from urllib.parse import unquote, unquote_plus

from msgspec import DecodeError, Struct, ValidationError, convert, to_builtins
from msgspec.json import Decoder
from msgspec.structs import fields as struct_fields

from jero._exception_handlers import (
    CompiledExceptionHandler,
    ExceptionHandler,
    ExceptionResponse,
    invoke_exception_handler,
)
from jero._middleware import (
    CORS,
    CompiledCORS,
    CompiledMiddleware,
    HeadersHook,
    HTTPMethod,
    InterceptHook,
    ObserveHook,
    requested_method,
)
from jero._openapi_wiring import operation_input
from jero._wiring_types import (
    AuthMode,
    EndpointMeta,
    FormField,
    FormSpec,
    OperationMeta,
    OperationSpec,
    PayloadKind,
    ResourceMeta,
    ResponseMember,
    ReturnKind,
    Sources,
    WiringError,
    is_struct_type,
    strip_list,
    unwrap_alias,
)
from jero.background import BackgroundTasks
from jero.codecs import msgspec_encoder
from jero.errors import (
    AuthenticationRequiredError,
    BaseHTTPError,
    ErrorBodyAdapter,
    ErrorReason,
    HTTPError,
    InternalServerError,
    MalformedRequestError,
    MethodNotAllowedError,
    NotFoundError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)
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
from jero.openapi import (
    Info,
    OpenAPINameConflictError,
    OperationInput,
    ScalarConfig,
    SecurityScheme,
    Tag,
    build_openapi,
)
from jero.streaming import (
    NDJSONStreamingResponse,
    ServerSentEvent,
    SSEResponse,
    StreamingResponse,
    encode_sse,
)
from jero.websockets import CompiledWebSocket, WebSocket, compile_websocket

# annotationlib is 3.14+. On 3.14 inspect.signature evaluates annotations by default
# (PEP 649), so _instantiate_factory asks for the FORWARDREF format instead. Pre-3.14
# signature never evaluates annotations, so the format argument isn't needed there.
if sys.version_info >= (3, 14):
    from annotationlib import Format

# The package-level logger (not ``jero.core``): ``core`` is an internal module name, while
# ``jero`` is the stable, user-facing namespace to configure. (``jero.background`` keeps its
# own child name, since background tasks are a user-facing subsystem.)
logger = logging.getLogger("jero")

# The ASGI triple. ``Scope``/``Receive`` are the ecosystem-standard ``MutableMapping``
# (asgiref, Starlette, httpx) rather than ``dict``: jero *consumes* those, so it accepts
# the widest mapping shape a caller may hand it, and narrowing them to ``dict`` would
# make ``__call__`` un-assignable to every standard-typed ``ASGIApp`` alias.
#
# ``Send`` is deliberately *not* widened to match. There jero is the producer: it only
# ever passes dict literals, so ``dict`` is the weakest precondition it can place on the
# callable it is given, and it accepts strictly more senders than ``MutableMapping``
# would (a ``send`` annotated ``dict`` is not assignable to a ``MutableMapping``
# parameter). Only ``[]`` and ``.get`` are ever used on a scope or a received message,
# so the wide reads are honest; see ``tests/test_asgi_typing.py``.
type Scope = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
type Send = Callable[[dict[str, Any]], Awaitable[None]]

# A compiled per-request handler: decode -> call -> encode.
type _Handler = Callable[[Scope, Receive, Send, dict[str, str]], Awaitable[None]]
type _WebSocketHandler = Callable[[Scope, Receive, Send, dict[str, str]], Awaitable[None]]
# A template segment: (is_param, static_value_or_slot_name).
type _Segment = tuple[bool, str]
type _StaticRoutes = dict[tuple[str, str], _Handler]
type _DynamicRoutes = dict[tuple[HTTPMethod, int], list[_Pattern]]
type _AllowedMethods = dict[str, list[HTTPMethod]]
# Resolves a Struct type to its reusable typed JSON decoder (the app's per-type cache).
# ReturnKind / PayloadKind (the other wire-shape aliases) live in jero._wiring_types,
# shared with the OpenAPI generator.
type _DecoderFor = Callable[[type[Struct]], Decoder[Struct]]


class _WebSocketInterceptRunner(Protocol):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> bool: ...


# Argument names the binder understands, shared by every handler kind.
_SOURCES = frozenset(
    {"json", "content", "form", "params", "path", "headers", "user", "raw_headers"}
)
# HTTP verbs that forbid a request body, whatever the handler is named.
_BODYLESS_VERBS = frozenset({"GET", "DELETE"})


@dataclass(frozen=True, slots=True)
class _Verb:
    """How one handler method maps onto HTTP."""

    method: HTTPMethod
    success_status: int
    extends_path: bool  # may path fields beyond the template slots extend the URL?


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

    ``status_code`` overrides the status this response would otherwise send — the verb's
    default (201 for create, else 200), or the fixed status of a wrapper that has one
    (:class:`NoContent` 204, :class:`Created` 201, :class:`Accepted` 202).

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


@dataclass(kw_only=True, slots=True)
class NoContent[H: Struct | None = None](BaseResponse[H]):
    """204, no body. Carries typed/raw headers, ``location``, and ``links`` like any
    response — a 204 may legitimately carry a ``Location`` or ``Link`` (RFC 9110 §15.3.5).
    At 204 it emits neither ``content-type`` nor ``content-length``, whatever ``headers``
    supplies; override ``status_code`` to a status that permits them and the body is still
    empty, so ``content-length: 0`` frames it. See :func:`_no_content_headers`."""


@dataclass(kw_only=True, slots=True)
class Created[T: Struct, H: Struct | None = None](BaseResponse[H]):
    """201 + a JSON body, whatever status the verb would otherwise default to.

    Deliberately a *sibling* of :class:`JSONResponse` rather than a subclass — it promises
    a status ``JSONResponse`` does not, so it is not substitutable for one. As a subclass,
    ``-> JSONResponse[T]`` would statically accept a returned ``Created`` and then send the
    verb's status: a 200 from an object whose type says 201, invisible to every type
    checker. The repeated ``json`` field is the price of that being a type error instead.
    """

    json: T


@dataclass(kw_only=True, slots=True)
class Accepted[T: Struct, H: Struct | None = None](BaseResponse[H]):
    """202 + a JSON body, whatever status the verb would otherwise default to.

    A sibling of :class:`JSONResponse`, not a subclass, for the reason given on
    :class:`Created`."""

    json: T


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
        "update_full": _Verb("PUT", 200, extends_path=True),
        "update_partial": _Verb("PATCH", 200, extends_path=True),
        "delete": _Verb("DELETE", 200, extends_path=True),
    }

    meta: ClassVar[ResourceMeta | None] = None
    meta_create: ClassVar[OperationMeta | None] = None
    meta_read_one: ClassVar[OperationMeta | None] = None
    meta_read_many: ClassVar[OperationMeta | None] = None
    meta_update_full: ClassVar[OperationMeta | None] = None
    meta_update_partial: ClassVar[OperationMeta | None] = None
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
        meta_update_full: OperationMeta | None = None,
        meta_update_partial: OperationMeta | None = None,
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
                "meta_update_full": meta_update_full,
                "meta_update_partial": meta_update_partial,
                "meta_delete": meta_delete,
            },
        )
        cls.meta = meta
        cls.meta_create = meta_create
        cls.meta_read_one = meta_read_one
        cls.meta_read_many = meta_read_many
        cls.meta_update_full = meta_update_full
        cls.meta_update_partial = meta_update_partial
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


class WebSocketEndpoint(_Routable):
    """One typed WebSocket protocol at one class-declared path.

    Subclasses define exactly one async ``handle`` method whose first argument is
    ``websocket: WebSocket[Inbound, Outbound]``. Handshake sources use the existing
    ``path`` / ``params`` / ``headers`` / ``raw_headers`` / ``user`` vocabulary.
    """

    def __init_subclass__(cls, *, path: str) -> None:
        super().__init_subclass__(path=path)


class Auth[THeaders: Struct, TUser: Struct](Protocol):
    """Implement ``authenticate``; raise an ``HTTPError`` subclass to reject.

    ``headers`` is bound from the request headers into your declared
    Struct (header names map ``x-trace-id`` -> ``x_trace_id``). The
    returned Struct is what handlers receive as ``user``.

    **The return type is the route's auth policy.** Declaring ``-> TUser`` gates the routes
    it is mounted on: a caller without valid credentials never reaches a handler. Declaring
    ``-> TUser | None`` makes credentials an *input* instead — returning ``None`` reports
    that the caller presented none, and the handler is invoked with ``user=None``. Raising
    is rejection in both cases, so *invalid* credentials are always a 401. Handlers must
    match: ``user: TUser`` against the first, ``user: TUser | None`` against the second,
    checked at startup.

    An app that wants both usually defines two authenticators over one shared resolution
    step (``TokenAuth`` / ``OptionalTokenAuth``), so which policy a route gets is visible in
    what its mount passes.

    ``authenticate`` only sees credentials your ``THeaders`` Struct can bind: a Struct
    whose fields are all required makes a credential-less request a 401 before your code
    runs. Give the field a ``| None`` default (``authorization: str | None = None``) to
    have absence reach ``authenticate`` and become your decision.
    """

    def authenticate(self, headers: THeaders) -> TUser | Awaitable[TUser | None] | None:
        """Validate ``headers`` and return the user Struct; raise ``HTTPError`` to reject.

        Return ``None`` only to report that no credentials were presented (see the class
        docstring); it is never a way to say "these credentials are bad".
        """
        ...  # pylint: disable=unnecessary-ellipsis  # Protocol stub; pyright needs the body


class BearerAuth[THeaders: Struct, TUser: Struct](Auth[THeaders, TUser]):
    """An ``Auth`` whose operations advertise HTTP bearer in the OpenAPI spec.

    Sugar over the ``openapi_security`` attribute the spec generator reads — subclass
    this instead of writing the attribute by hand. Implement ``authenticate`` as usual.
    """

    openapi_security: ClassVar[SecurityScheme] = SecurityScheme.http_bearer()


class BasicAuth[THeaders: Struct, TUser: Struct](Auth[THeaders, TUser]):
    """An ``Auth`` whose operations advertise HTTP basic in the OpenAPI spec."""

    openapi_security: ClassVar[SecurityScheme] = SecurityScheme.http_basic()


class _StreamResult(Protocol):
    stream: Any
    headers: Struct | None
    raw_headers: RawHeaders | Mapping[str, str] | None
    status_code: int | None
    location: Location | None
    links: Sequence[Link]


def _allow_header(allowed: Sequence[HTTPMethod]) -> bytes:
    # copy: HEAD/OPTIONS are appended below without mutating the caller's list
    methods: list[HTTPMethod] = [*allowed]
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


@dataclass(slots=True)
class _WebSocketDenialSend:
    """Translate an HTTP middleware answer into ASGI's pre-upgrade denial events."""

    _send: Send
    _supported: bool
    _closed: bool = False

    async def __call__(self, message: dict[str, Any]) -> None:
        if not self._supported:
            if not self._closed:
                self._closed = True
                await self._send(
                    {"type": "websocket.close", "code": 1008, "reason": "handshake rejected"}
                )
            return
        event_type = message["type"]
        if event_type == "http.response.start":
            message = {**message, "type": "websocket.http.response.start"}
        elif event_type == "http.response.body":
            message = {**message, "type": "websocket.http.response.body"}
        await self._send(message)


def _supports_websocket_denial(scope: Scope) -> bool:
    """Whether the server advertised ASGI's optional denial-response extension."""
    extensions = scope.get("extensions")
    return isinstance(extensions, Mapping) and "websocket.http.response" in extensions


async def _send_websocket_rejection(scope: Scope, send: Send, status: int, payload: bytes) -> None:
    """Reject before upgrade, preserving a body only when the server supports it."""
    if not _supports_websocket_denial(scope):
        await send({"type": "websocket.close", "code": 1008, "reason": "handshake rejected"})
        return
    await send(
        {
            "type": "websocket.http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "websocket.http.response.body", "body": payload})


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


def _parse_query(query_string: bytes) -> dict[str, str]:
    """The query string as a last-wins dict, mirroring ``dict(parse_qsl(...))`` semantics
    (pairs with a blank or missing value are skipped) but ~3x faster: ``parse_qsl`` is a
    generic generator, and most query strings need no percent-decoding at all — only pay
    ``unquote_plus`` when a pair actually contains ``%`` or ``+``."""
    if not query_string:
        return {}
    values: dict[str, str] = {}
    for pair in query_string.decode("latin-1").split("&"):
        key, _, value = pair.partition("=")
        if not value:
            continue
        if "%" in pair or "+" in pair:
            key = unquote_plus(key)
            value = unquote_plus(value)
        values[key] = value
    return values


def _convert_source(
    raw: dict[str, str],
    struct_type: type[Struct],
    status: int,
) -> Struct:
    """Convert one request source to its Struct, mapping failure to an HTTP status."""
    try:
        return convert(raw, struct_type, strict=False)
    except ValidationError as e:
        if status == 404:
            raise NotFoundError() from None
        raise MalformedRequestError(ErrorReason(reason=str(e))) from e


def _decode_json_body(body: bytes, decoder: Decoder[Struct]) -> Struct:
    try:
        return decoder.decode(body)
    except ValidationError as e:
        raise ValidationFailedError(ErrorReason(reason=str(e))) from e
    except DecodeError as e:
        raise MalformedRequestError(ErrorReason(reason=str(e))) from e


def _is_none_type(ann: object) -> bool:
    return ann is None or ann is NoneType


def _alias_value(ann: object) -> object:
    value = getattr(ann, "__value__", None)
    return ann if value is None else value


def _strip_annotated(ann: object) -> object:
    """The underlying type of an ``Annotated[T, ...]``. Used only to *classify* a payload —
    the original annotation (with its ``msgspec.Meta``) is kept for conversion and schema,
    so constraints/description/examples are still enforced and documented."""
    return get_args(ann)[0] if get_origin(ann) is Annotated else ann


def _is_struct_payload(ann: object) -> bool:
    ann = _alias_value(ann)
    if is_struct_type(ann):
        return True
    args = get_args(ann)
    return (
        bool(args)
        and any(_is_none_type(arg) for arg in args) is False
        and all(is_struct_type(arg) for arg in args)
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


def _payload_kind(cls: type, method: str, field_name: str, ann: object) -> PayloadKind:
    bare = _strip_annotated(ann)  # classify by the underlying type; Meta stays on `ann`
    if bare is bytes:
        return "bytes"
    if _is_struct_payload(bare):
        return "struct"
    if _is_scalar_payload(bare):
        return "scalar"
    raise WiringError(
        f"{cls.__name__}.{method}: form field {field_name!r} has unsupported payload "
        f"type {ann!r}; expected bytes, a msgspec.Struct, or a scalar",
    )


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
) -> FormSpec:
    descriptors: list[FormField] = []
    for field in struct_fields(form_type):
        field_type, optional = _strip_optional(field.type)
        item_type, repeated = strip_list(field_type)
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
            FormField(
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
    return FormSpec(form_type, tuple(descriptors))


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
        raise UnsupportedMediaTypeError()

    parts: dict[str, list[_Part]] = defaultdict(list)
    try:
        for raw_part in MultipartParser(BytesIO(body), parsed[1], strict=True):
            if raw_part.name is None:
                raise MalformedRequestError(ErrorReason(reason="multipart part is missing a name"))
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
    except MultipartError as e:
        raise MalformedRequestError(
            ErrorReason(reason=f"malformed multipart form data: {e}")
        ) from e
    return parts


def _decode_form_payload(field: FormField, part: _Part) -> object:
    if field.payload_kind == "bytes":
        return part.body
    if field.decoder is not None:  # struct payload — reuse the prebuilt typed decoder
        try:
            return field.decoder.decode(part.body)
        except ValidationError as e:
            raise ValidationFailedError(ErrorReason(reason=str(e))) from e
        except DecodeError as e:
            raise MalformedRequestError(ErrorReason(reason=str(e))) from e
    try:
        return convert(part.body.decode(), field.payload_type, strict=False)
    except UnicodeDecodeError as e:
        raise ValidationFailedError(ErrorReason(reason=f"part is not valid UTF-8: {e}")) from e
    except ValidationError as e:
        raise ValidationFailedError(ErrorReason(reason=str(e))) from e


def _decode_form_value(field: FormField, part: _Part) -> object:
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
            raise ValidationFailedError(
                ErrorReason(reason=f"file part {field.wire_name!r} is missing a filename")
            )
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


def _decode_form_body(body: bytes, raw_headers: dict[str, str], spec: FormSpec) -> Struct:
    parts = _parse_form_parts(body, raw_headers)
    values: dict[str, object] = {}
    for field in spec.fields:
        matched = parts[field.wire_name]
        if field.repeated:
            values[field.wire_name] = [_decode_form_value(field, part) for part in matched]
            continue
        if not matched:
            if field.required:
                raise ValidationFailedError(
                    ErrorReason(reason=f"missing required form field {field.wire_name!r}")
                )
            values[field.wire_name] = None
            continue
        values[field.wire_name] = _decode_form_value(field, matched[-1])
    try:
        return convert(values, spec.struct_type, strict=False)
    except ValidationError as e:
        raise ValidationFailedError(ErrorReason(reason=str(e))) from e


def _struct_annotation(cls: type, method: str, name: str, ann: object) -> type[Struct]:
    if not (isinstance(ann, type) and issubclass(ann, Struct)):
        raise WiringError(
            f"{cls.__name__}.{method}: {name!r} must be annotated with a "
            f"msgspec.Struct subclass, got {ann!r}",
        )
    return ann


def _wrapper_kind(cls: type) -> ReturnKind | None:
    """The return kind ``cls`` is a response wrapper for, or None if it is not one.

    Resolved by subclass, so a user's own subclass of a wrapper classifies as the wrapper does.
    The wrappers are flat siblings (none is a subclass of another), so this order is arbitrary;
    the abstract bases are deliberately absent, leaving a bare ``BaseResponse`` unrecognized."""
    if issubclass(cls, StreamingResponse):
        return "stream-bytes"
    if issubclass(cls, NDJSONStreamingResponse):
        return "stream-ndjson"
    if issubclass(cls, SSEResponse):
        return "stream-sse"
    if issubclass(cls, NoContent):
        return "no-content"
    if issubclass(cls, Created):
        return "created"
    if issubclass(cls, Accepted):
        return "accepted"
    if issubclass(cls, BytesResponse):
        return "bytes-response"
    if issubclass(cls, JSONResponse):
        return "json-response"
    return None


def _return_kind(ann: object) -> ReturnKind | None:
    origin = get_origin(ann)
    # A subscripted annotation is classified by its origin, so ``JSONResponse[Widget]`` lands on
    # the same kind as the bare class does — and so does a user's own generic subclass,
    # ``class Envelope[T: Struct](JSONResponse[T, CacheHeaders])`` used as ``Envelope[Widget]``.
    # The type arguments themselves are read later, by the OpenAPI layer.
    wrapper = ann if isinstance(ann, type) else origin
    if isinstance(wrapper, type) and (kind := _wrapper_kind(wrapper)) is not None:
        return kind
    if isinstance(ann, type):
        if issubclass(ann, BaseResponse):
            return None  # the base is abstract; return a concrete subclass
        if issubclass(ann, Struct):
            return "json"
        if ann is bytes:
            return "bytes"
    args = get_args(ann)
    if (
        origin is list
        and len(args) == 1
        and isinstance(args[0], type)
        and issubclass(args[0], Struct)
    ):
        return "json"
    return None


# The generic wrapper class each return kind's type arguments resolve against. Handed to the
# OpenAPI layer through the wiring contracts so it can read a wrapper's ``(T, H)`` positionally
# without importing these classes: ``core`` imports ``_openapi_wiring``, never the reverse. The
# plain kinds (json, bytes) have no wrapper and are absent.
_WRAPPER_TYPES: dict[ReturnKind, type] = {
    "stream-bytes": StreamingResponse,
    "stream-ndjson": NDJSONStreamingResponse,
    "stream-sse": SSEResponse,
    "no-content": NoContent,
    "created": Created,
    "accepted": Accepted,
    "bytes-response": BytesResponse,
    "json-response": JSONResponse,
}


# The status a return kind fixes regardless of the verb's own default; a kind absent here
# (json, json-response, bytes, bytes-response, the streams) takes the verb's default status.
_FIXED_STATUS: dict[ReturnKind, int] = {"no-content": 204, "created": 201, "accepted": 202}


def _effective_status(kind: ReturnKind, verb_status: int) -> int:
    """The status a return kind actually sends/documents: its fixed status if it has one,
    else the verb's own default (200, or 201 for ``create``)."""
    return _FIXED_STATUS.get(kind, verb_status)


# Kinds a union return member may resolve to: every buffered kind, plain returns included —
# a bare ``Struct`` / ``list[Struct]`` / ``bytes`` member simply takes the verb's default
# status, exactly as it does when it is a handler's sole return, so ``-> Widget | NoContent``
# needs no wrapper. Only the streaming kinds are excluded: their senders own the response
# lifecycle (disconnect handling, mid-stream failure) and cannot be chosen after the fact.
_UNION_MEMBER_KINDS: frozenset[ReturnKind] = frozenset(
    {"json", "bytes", "no-content", "created", "accepted", "json-response", "bytes-response"}
)


def _union_args(ann: object) -> tuple[object, ...] | None:
    """``ann``'s union members, or ``None`` if ``ann`` is not a union (of any arity)."""
    origin = get_origin(ann)
    if origin is not UnionType and origin is not Union:
        return None
    return get_args(ann)


def _flatten_union_members(members: tuple[object, ...]) -> tuple[object, ...]:
    """Union members with every ``type`` alias resolved, and a member that resolves to a union
    itself spliced in.

    Python flattens ``A | (B | C)`` as it builds the union, but not when the inner union arrives
    behind an alias: ``type Kinds = A | B`` in ``Kinds | Created[C]`` stays a single member until
    the alias is resolved, and would then be classified as one unrecognized return type. Recursive
    so an alias of an alias of a union flattens too."""
    flat: list[object] = []
    for member in members:
        resolved = unwrap_alias(member)
        inner = _union_args(resolved)
        flat.extend(_flatten_union_members(inner) if inner is not None else [resolved])
    return tuple(flat)


def _dispatch_type(ann: object) -> type:
    """The class a union member is matched against at request time. Subscripted
    annotations dispatch on their origin (``JSONResponse[W]`` -> ``JSONResponse``,
    ``list[W]`` -> ``list``), since a subscripted generic is not ``isinstance``-able."""
    origin = get_origin(ann)
    return cast("type", origin if origin is not None else ann)


def _union_return_members(
    label: str, members: tuple[object, ...], verb_status: int
) -> tuple[ResponseMember, ...]:
    """Resolve and validate a union return annotation's members: each must be a recognized,
    non-streaming return kind.

    Members **may** share a status: OpenAPI keys one response per status, so those merge
    into it — bodies as one ``anyOf``, header maps unioned. Whether a given group actually
    merges is a question about the *document*, so it is settled where the document is built
    (``_openapi_wiring._merge_status_group``), alongside the item-type checks — not here.
    Runtime dispatch needs no such rule at all: every member resolves its own sender from
    its own kind and status (``bytes | JSONResponse[W]`` at 200 gets two *different* sender
    classes), so whether two of them happen to share a status is simply irrelevant here.
    """
    resolved: list[ResponseMember] = []
    for member in members:
        kind = _return_kind(member)
        if kind is None or kind not in _UNION_MEMBER_KINDS:
            raise WiringError(
                f"{label}: union return members must each be a recognized, "
                f"non-streaming return type (a Struct, list[Struct], bytes, NoContent, "
                f"Created, Accepted, JSONResponse, or BytesResponse); got {member!r}",
            )
        status = _effective_status(kind, verb_status)
        resolved.append(
            ResponseMember(_dispatch_type(member), member, kind, status, _WRAPPER_TYPES.get(kind))
        )
    return tuple(resolved)


def _resolve_return(
    cls: type, name: str, http_method: HTTPMethod, verb_status: int, return_hint: object
) -> tuple[ReturnKind, tuple[ResponseMember, ...]]:
    """The handler's return kind, and — for a union — its resolved members."""
    union_args = _union_args(return_hint)
    if union_args is not None:
        union_args = _flatten_union_members(union_args)
        if any(_is_none_type(arg) for arg in union_args):
            raise WiringError(
                f"{cls.__name__}.{name}: a handler cannot return None — did you mean "
                f"'| NoContent' for a 204?",
            )
        return "union", _union_return_members(f"{cls.__name__}.{name}", union_args, verb_status)
    kind = _return_kind(return_hint)
    if kind is None:
        raise WiringError(
            f"{cls.__name__}.{name} must declare a return type of Struct, list[Struct], "
            f"bytes, BytesResponse, JSONResponse, NoContent, Created, Accepted, or a "
            f"streaming response, got {return_hint!r}",
        )
    if kind == "stream-sse" and http_method != "GET":
        raise WiringError(f"{cls.__name__}.{name}: SSEResponse is only allowed on GET handlers")
    return kind, ()


def _bind_sources(  # noqa: C901
    cls: type, name: str, fn: Callable[..., Any], verb: _Verb, decoder_for: _DecoderFor
) -> Sources:
    """Resolve and validate the Struct types for a handler's arguments."""
    http_method = verb.method
    hints = get_type_hints(fn)
    types: dict[str, type[Struct]] = {}
    form: FormSpec | None = None
    wants_content = False
    wants_raw_headers = False
    user_optional = False

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
        annotation = hints.get(param.name)
        if param.name == "user":
            # 'user' is the one source that may be optional: `UserStruct | None` declares the
            # handler serves anonymous callers too (cross-checked against the authenticator).
            annotation, user_optional = _strip_optional(annotation)
        source_type = _struct_annotation(cls, name, param.name, annotation)
        if param.name == "form":
            form = _compile_form(cls, name, source_type, decoder_for)
            continue
        types[param.name] = source_type

    body_sources = int(wants_content) + int(types.get("json") is not None) + int(form is not None)
    if body_sources > 1:
        raise WiringError(
            f"{cls.__name__}.{name}: only one of 'json', 'content', or 'form' is allowed",
        )

    # A PEP 695 ``type`` alias is resolved to what it aliases before anything classifies it, and
    # the resolved form is what Sources stores — so the OpenAPI layer derives its schema from the
    # real annotation rather than from an opaque alias object.
    return_hint = unwrap_alias(hints.get("return"))
    return_kind, return_members = _resolve_return(
        cls, name, http_method, verb.success_status, return_hint
    )

    json_type = types.get("json")
    json_decoder = decoder_for(json_type) if json_type is not None else None
    arity = len(types) + (form is not None) + wants_content + wants_raw_headers

    return Sources(
        **types,
        json_decoder=json_decoder,
        form=form,
        user_optional=user_optional,
        content=wants_content,
        raw_headers=wants_raw_headers,
        return_kind=return_kind,
        return_annotation=return_hint,
        return_wrapper=_WRAPPER_TYPES.get(return_kind),
        return_members=return_members,
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


@dataclass(frozen=True, slots=True)
class _WebSocketPattern:
    statics: tuple[tuple[int, str], ...]
    params: tuple[tuple[int, str], ...]
    handler: _WebSocketHandler


def _bind_websocket_sources(cls: type, fn: Callable[..., Any]) -> tuple[Sources, object, object]:
    """Compile a WebSocket handler's framing and handshake-only sources."""
    hints = get_type_hints(fn)
    params = list(inspect.signature(fn).parameters.values())
    if not params or params[0].name != "websocket":
        raise WiringError(f"{cls.__name__}.handle must take 'websocket' as its first argument")
    websocket_hint = hints.get("websocket")
    if get_origin(websocket_hint) is not WebSocket or len(get_args(websocket_hint)) != 2:
        raise WiringError(
            f"{cls.__name__}.handle: 'websocket' must be annotated WebSocket[Inbound, Outbound]",
        )
    if hints.get("return") not in (None, NoneType):
        raise WiringError(f"{cls.__name__}.handle must return None")
    if not inspect.iscoroutinefunction(fn):
        raise WiringError(f"{cls.__name__}.handle must be async")

    types: dict[str, type[Struct]] = {}
    wants_raw_headers = False
    user_optional = False
    allowed = frozenset({"params", "path", "headers", "user", "raw_headers"})
    for param in params[1:]:
        if param.name not in allowed:
            raise WiringError(
                f"{cls.__name__}.handle: unsupported argument {param.name!r}; "
                "allowed handshake sources are path, params, headers, raw_headers, and user",
            )
        if param.name == "raw_headers":
            if hints.get("raw_headers") is not RawHeaders:
                raise WiringError(
                    f"{cls.__name__}.handle: 'raw_headers' must be annotated as RawHeaders",
                )
            wants_raw_headers = True
            continue
        annotation = hints.get(param.name)
        if param.name == "user":
            annotation, user_optional = _strip_optional(annotation)
        types[param.name] = _struct_annotation(cls, "handle", param.name, annotation)
    sources = Sources(
        params=types.get("params"),
        path=types.get("path"),
        headers=types.get("headers"),
        user=types.get("user"),
        user_optional=user_optional,
        raw_headers=wants_raw_headers,
        return_kind="bytes",
        return_annotation=NoneType,
        arity=len(types) + wants_raw_headers,
    )
    inbound, outbound = get_args(websocket_hint)
    return sources, unwrap_alias(inbound), unwrap_alias(outbound)


class _CompiledAuth:
    """An authenticator introspected once at registration time.

    Deliberately not a dataclass: every attribute is derived by
    introspection, so a plain ``__init__`` is the honest shape.
    """

    __slots__ = ("_fn", "_is_async", "headers_type", "owner", "reports_absence", "returns")

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
        # `-> TUser | None` is the authenticator declaring that its routes accept anonymous
        # callers: returning None reports absent credentials rather than rejecting them.
        user_type, self.reports_absence = _strip_optional(returns)
        if not (isinstance(user_type, type) and issubclass(user_type, Struct)):
            raise WiringError(
                f"{self.owner}.authenticate must declare a msgspec.Struct return type "
                f"(or 'Struct | None' to accept anonymous callers), got {returns!r}",
            )
        self.returns: type[Struct] = user_type
        self._fn: Callable[..., Any] = fn
        self._is_async = inspect.iscoroutinefunction(fn)

    async def __call__(self, raw_headers: dict[str, str]) -> Struct | None:
        try:
            credentials = convert(raw_headers, self.headers_type, strict=False)
        except ValidationError:
            raise AuthenticationRequiredError() from None
        result = self._fn(credentials)
        user = (await result) if self._is_async else result
        # None means "no credentials presented" — anonymous on a route whose authenticator
        # declares it. Without that declaration a None return contradicts the annotation the
        # handlers were checked against, so reject rather than bind it.
        if user is None and not self.reports_absence:
            raise AuthenticationRequiredError()
        return user


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
        "awaits_only_body",
        "is_sync",
    )

    def __init__(self, sources: Sources, auth: _CompiledAuth | None) -> None:
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
        # With no auth to run and no body to read, binding never awaits: callers use
        # bind_sync and skip a per-request coroutine. With no auth but a body, only the
        # body read awaits: callers read it inline and use bind_with_body.
        self.is_sync = auth is None and not self._needs_body
        self.awaits_only_body = auth is None and self._needs_body

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
            return _convert_source(path_values, self._path_type, 404)
        if self._headers_type is not None:
            return _convert_source(raw_headers, self._headers_type, 400)
        if self._params_type is not None:
            return _convert_source(_parse_query(scope["query_string"]), self._params_type, 400)
        if self._wants_raw_headers:
            return RawHeaders(_wire_header_pairs(scope))
        return user

    def _finish(  # noqa: C901  — flat per-source binding
        self,
        scope: Scope,
        raw_headers: dict[str, str],
        path_values: dict[str, str],
        user: Struct | None,
        body: bytes,
    ) -> object:
        # 0- or 1-source handlers skip the kwargs dict: call positionally (see _Route).
        if self._arity == 0:
            return None
        if self._arity == 1:
            return self._one(scope, raw_headers, path_values, user, body)
        kwargs: dict[str, object] = {}
        if self._wants_user:
            kwargs["user"] = user
        if self._path_type is not None:
            kwargs["path"] = _convert_source(path_values, self._path_type, 404)
        if self._headers_type is not None:
            kwargs["headers"] = _convert_source(raw_headers, self._headers_type, 400)
        if self._params_type is not None:
            kwargs["params"] = _convert_source(
                _parse_query(scope["query_string"]), self._params_type, 400
            )
        if self._json_decoder is not None:
            kwargs["json"] = _decode_json_body(body, self._json_decoder)
        elif self._form_spec is not None:
            kwargs["form"] = _decode_form_body(body, raw_headers, self._form_spec)
        elif self._wants_content:
            kwargs["content"] = body
        if self._wants_raw_headers:
            kwargs["raw_headers"] = RawHeaders(_wire_header_pairs(scope))
        return kwargs

    def bind_sync(self, scope: Scope, path_values: dict[str, str]) -> object:
        """``__call__`` without the awaits, valid whenever ``is_sync`` (no auth to run,
        no body to read) — the caller skips a per-request coroutine."""
        raw_headers = _raw_headers(scope) if self._needs_raw else {}
        return self._finish(scope, raw_headers, path_values, None, b"")

    def bind_with_body(self, scope: Scope, path_values: dict[str, str], body: bytes) -> object:
        """``__call__`` for a caller that read the body itself, valid whenever
        ``awaits_only_body`` (no auth to run) — the caller skips a per-request coroutine."""
        raw_headers = _raw_headers(scope) if self._needs_raw else {}
        return self._finish(scope, raw_headers, path_values, None, body)

    async def __call__(self, scope: Scope, receive: Receive, path_values: dict[str, str]) -> object:
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
        return self._finish(scope, raw_headers, path_values, user, body)


class _WebSocketRoute:
    """A compiled handshake binder followed by one accepted WebSocket handler."""

    __slots__ = (
        "_arity",
        "_bind",
        "_exceptions",
        "_fn",
        "_intercepts",
        "_spec",
        "_tail",
    )

    def __init__(
        self,
        fn: Callable[..., Awaitable[None]],
        *,
        sources: Sources,
        inbound: object,
        outbound: object,
        auth: _CompiledAuth | None,
        exceptions: "_ExceptionHandlers",
        intercepts: tuple[_WebSocketInterceptRunner, ...],
        tail: "_RouteTail",
        max_frame_size: int,
    ) -> None:
        spec: CompiledWebSocket = compile_websocket(
            inbound, outbound, max_frame_size=max_frame_size
        )
        self._fn = fn
        self._bind = _Binder(sources, auth)
        self._exceptions = exceptions
        self._intercepts = intercepts
        self._spec = spec
        self._tail = tail
        self._arity = sources.arity

    async def _reject(self, scope: Scope, send: Send, error: BaseHTTPError) -> None:
        payload = self._exceptions.encode_error(error)
        await _send_websocket_rejection(scope, send, error.status, payload)

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, path_values: dict[str, str]
    ) -> None:
        if self._intercepts and await _run_intercepts(
            self._intercepts,
            scope,
            receive,
            _WebSocketDenialSend(send, _supports_websocket_denial(scope)),
            exceptions=self._exceptions,
            tail=self._tail,
        ):
            return
        try:
            bound = (
                None
                if self._arity == 0
                else await self._bind(scope, receive, path_values)
                if not self._bind.is_sync
                else self._bind.bind_sync(scope, path_values)
            )
        except BaseHTTPError as error:
            await self._reject(scope, send, error)
            return
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("error binding WebSocket handshake for %s", scope["path"])
            await self._reject(scope, send, InternalServerError())
            return
        await send({"type": "websocket.accept"})
        websocket: WebSocket[object, object] = self._spec.open(receive, send)
        try:
            if self._arity >= 2:
                kwargs = cast("dict[str, object]", bound)
                await self._fn(websocket=websocket, **kwargs)
            elif self._arity == 1:
                await self._fn(websocket, bound)
            else:
                await self._fn(websocket)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("error in WebSocket handler for %s", scope["path"])
            with contextlib.suppress(OSError):
                await websocket.close(code=1011, reason="internal error")
            return
        # The peer may disappear after the handler returns but before this best-effort
        # close frame reaches the transport; an ordinary disconnect is not an app error.
        with contextlib.suppress(OSError):
            await websocket.close()


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


def _no_content_headers(
    typed: Struct | None, raw: RawHeaders | Mapping[str, str] | None, status: int
) -> list[tuple[bytes, bytes]]:
    """Header pairs for a bodyless response.

    At 204/304/1xx neither ``content-type`` nor ``content-length`` may appear at all
    (RFC 9110 §15.3.5, §15.4.5), whatever ``typed``/``raw`` supply. At any other status —
    reachable through ``NoContent(status_code=...)`` — that exemption does not apply, and
    the body is still empty, so ``content-length: 0`` is emitted to frame it rather than
    leaving the framing for the server to guess at."""
    forbids_content = status in (204, 304) or 100 <= status < 200
    dropped = {"content-length", "content-type"} if forbids_content else {"content-length"}
    headers = [
        (key.encode("latin-1"), value.encode("latin-1"))
        for key, value in _header_items(typed, raw)
        if key.lower() not in dropped
    ]
    if not forbids_content:
        headers.append((b"content-length", b"0"))
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


# A per-request producer of extra response-header pairs (origin echo, a middleware's
# ``response_headers`` method), compiled at wiring and invoked as a response's headers
# are assembled — before ``http.response.start``, so a failure can still become a
# proper error response.
type _TailHook = Callable[[Scope], Sequence[tuple[bytes, bytes]]]


@dataclass(slots=True)
class _RouteTail:
    """The response-header tail appended to every response that leaves a route — CORS
    pairs and middleware ``response_headers``, resolved once at ``__finalize`` (empty
    until then, and forever on routes nothing covers).

    ``pairs`` is the constant tier: pairs known at wiring, appended with one list
    concat. ``dynamic`` hooks compute per-request pairs. One instance is shared by an
    include's routes and their senders, so the finalize-time fill is visible everywhere
    without rebuilding the compiled routes. ``active`` is precomputed when the tail is
    filled, so the per-request guard on every sender is a two-load falsy check."""

    pairs: tuple[tuple[bytes, bytes], ...] = ()
    dynamic: tuple[_TailHook, ...] = ()
    active: bool = False

    def extend_dynamic(self, headers: list[tuple[bytes, bytes]], scope: Scope) -> None:
        """Append each dynamic hook's pairs straight into ``headers`` — no intermediate
        list. Strict: a hook failure propagates, entering the exception funnel exactly
        like a handler failure (nothing has been sent yet — senders assemble headers
        before ``http.response.start``)."""
        for hook in self.dynamic:
            headers += hook(scope)

    def extend(self, headers: list[tuple[bytes, bytes]], scope: Scope) -> None:
        """Append the tail to a response's header list: constant pairs, then the dynamic
        hooks' pairs (strict, via :meth:`extend_dynamic`). Callers guard with
        ``if tail.active`` so an empty tail costs nothing."""
        if self.pairs:
            headers += self.pairs
        self.extend_dynamic(headers, scope)

    def collect_contained(self, scope: Scope) -> list[tuple[bytes, bytes]]:
        """The hook loop for the error path: a hook that fails while an *error* response is
        being assembled is logged and skipped — the error must still leave, and raising
        here would recurse into the funnel that is already sending it."""
        pairs: list[tuple[bytes, bytes]] = []
        for hook in self.dynamic:
            try:
                pairs += hook(scope)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception(
                    "response header hook failed on the error path for %s %s; skipped",
                    scope["method"],
                    scope["path"],
                )
        return pairs

    def contained_extra(self, scope: Scope) -> list[tuple[bytes, bytes]] | None:
        """The full tail as extra pairs (constants + *contained* dynamic hooks), or
        ``None`` when the tail is empty — the shape ``_send_json`` expects. For
        responses where a hook failure must not replace the response: the error
        funnel, and the framework's own routes (the docs/spec pages)."""
        if not self.active:
            return None
        return list(self.pairs) + self.collect_contained(scope)


@dataclass(slots=True)
class _IncludeRecord:
    """One ``_include_resource`` / ``_include_endpoint`` call: its compiled routes, the
    shared header tail, and the per-include policy passed at the call. Collected during
    ``wire`` and resolved at ``__finalize``, so ordering among the include calls (and
    ``_include_cors``) never matters."""

    tail: _RouteTail
    routes: list[tuple[HTTPMethod, _Handler]]
    cors: CompiledCORS | None  # explicit per-include policy; None = inherit the app default
    cors_off: bool  # True when cors=CORS.OFF opted this include out
    middleware: tuple[CompiledMiddleware, ...]  # include-scoped, in the order passed


@dataclass(slots=True)
class _BytesSender:
    _status: int
    _tail: _RouteTail

    async def __call__(self, scope: Scope, receive: Receive, send: Send, result: bytes) -> None:
        _ = receive
        headers = _response_headers(None, None, b"application/octet-stream", len(result))
        tail = self._tail
        if tail.active:
            tail.extend(headers, scope)
        await _send_payload(send, self._status, result, headers)


@dataclass(slots=True)
class _BytesResponseSender:
    _status: int
    _reverser: _Reverser
    _tail: _RouteTail

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, result: BytesResponse[Any]
    ) -> None:
        _ = receive
        status = result.status_code if result.status_code is not None else self._status
        headers = _response_headers(
            result.headers, result.raw_headers, b"application/octet-stream", len(result.content)
        )
        headers += _link_header_pairs(self._reverser, scope, result.location, result.links)
        tail = self._tail
        if tail.active:
            tail.extend(headers, scope)
        await _send_payload(send, status, result.content, headers)


@dataclass(slots=True)
class _JSONResponseSender:
    _status: int
    _reverser: _Reverser
    _tail: _RouteTail

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        # Also the sender for Created / Accepted, which are deliberately *not* JSONResponse
        # subclasses (see Created) — they are named here rather than left to the `_Sender`
        # alias's Any, which would quietly say something untrue on two of the three paths.
        result: "JSONResponse[Any, Any] | Created[Any, Any] | Accepted[Any, Any]",
    ) -> None:
        _ = receive
        status = result.status_code if result.status_code is not None else self._status
        payload = msgspec_encoder.encode(result.json)
        headers = _response_headers(
            result.headers, result.raw_headers, b"application/json", len(payload)
        )
        headers += _link_header_pairs(self._reverser, scope, result.location, result.links)
        tail = self._tail
        if tail.active:
            tail.extend(headers, scope)
        await _send_payload(send, status, payload, headers)


@dataclass(slots=True)
class _JSONSender:
    _status: int
    _tail: _RouteTail

    async def __call__(self, scope: Scope, receive: Receive, send: Send, result: object) -> None:
        # Inlines _send_json (kept for error paths) to save a coroutine hop on the
        # hot JSON response path.
        _ = receive
        payload = msgspec_encoder.encode(result)
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ]
        tail = self._tail
        if tail.active:
            tail.extend(headers, scope)
        await send(
            {
                "type": "http.response.start",
                "status": self._status,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": payload})


@dataclass(slots=True)
class _NoContentSender:
    _status: int
    _reverser: _Reverser
    _tail: _RouteTail

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, result: "NoContent[Any]"
    ) -> None:
        _ = receive
        status = result.status_code if result.status_code is not None else self._status
        headers = _no_content_headers(result.headers, result.raw_headers, status)
        headers += _link_header_pairs(self._reverser, scope, result.location, result.links)
        tail = self._tail
        if tail.active:
            tail.extend(headers, scope)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b""})


class _ExceptionHandlers:
    """App-local custom exception registry and response dispatcher."""

    def __init__(self, reverser: _Reverser) -> None:
        self._handlers: dict[type[Exception], CompiledExceptionHandler] = {}
        self._resolved: dict[type[Exception], CompiledExceptionHandler | None] = {}
        self._reverser = reverser
        # The app-wide renderer for the Problem family (see _include_error_adapter);
        # None means the family renders its own Problem body.
        self.adapter: ErrorBodyAdapter[Any] | None = None

    def encode_error(self, error: BaseHTTPError) -> bytes:
        """Encode an error's wire body: the adapter's composition for the Problem family
        when one is registered, else the error's own ``response_body``. An adapter failure
        is contained — logged, with the Problem body sent instead — so a rendering bug
        can't take down error handling itself."""
        if self.adapter is not None and isinstance(error, HTTPError):
            try:
                return msgspec_encoder.encode(self.adapter.compose_wire(error))
            except Exception:  # pylint: disable=broad-exception-caught
                logger.error(
                    "error body adapter %s failed composing %s; sending the Problem body",
                    type(self.adapter).__name__,
                    type(error).__name__,
                    exc_info=True,
                )
        return msgspec_encoder.encode(error.response_body)

    def _resolve(self, exception_type: type[Exception]) -> CompiledExceptionHandler | None:
        if exception_type in self._resolved:
            return self._resolved[exception_type]
        handler = next(
            (
                self._handlers[ancestor]
                for ancestor in exception_type.__mro__
                if ancestor in self._handlers
            ),
            None,
        )
        self._resolved[exception_type] = handler
        return handler

    async def _send_response(
        self,
        scope: Scope,
        send: Send,
        response: ExceptionResponse[Struct, Struct | None],
        tail: _RouteTail,
    ) -> None:
        payload = msgspec_encoder.encode(response.json)
        headers = _response_headers(
            response.headers,
            response.raw_headers,
            b"application/json",
            len(payload),
        )
        headers += _link_header_pairs(
            self._reverser,
            scope,
            response.location,
            response.links,
        )
        if tail.pairs:
            headers += tail.pairs
        if tail.dynamic:
            headers += tail.collect_contained(scope)
        await _send_payload(send, response.status_code, payload, headers)

    @staticmethod
    def _log_unexpected(scope: Scope, exception: BaseException) -> None:
        # An unexpected error is swallowed to a clean 500 (its internals never reach the
        # client), so this is the only place operators can see it — log the traceback or
        # it's lost. A deliberately raised jero error is expected control flow, never an
        # operator concern, so it is skipped: this stays safe to call with any exception.
        if isinstance(exception, BaseHTTPError):
            return
        logger.error(
            "unhandled error handling %s %s",
            scope["method"],
            scope["path"],
            exc_info=exception,
        )

    async def _send_default(
        self, scope: Scope, send: Send, exception: Exception, tail: _RouteTail
    ) -> None:
        extra = tail.contained_extra(scope)
        if isinstance(exception, BaseHTTPError):
            await _send_json(send, exception.status, self.encode_error(exception), extra)
            return
        self._log_unexpected(scope, exception)
        error = InternalServerError()
        await _send_json(send, error.status, self.encode_error(error), extra)

    def register(self, handler: object) -> None:
        """Register one compiled handler per exact exception type."""
        compiled = CompiledExceptionHandler(handler)
        existing = self._handlers.get(compiled.exception_type)
        if existing is not None:
            raise WiringError(
                f"exception handler for {compiled.exception_type.__name__} is already "
                f"registered by {existing.owner}; cannot also register {compiled.owner}",
            )
        self._handlers[compiled.exception_type] = compiled
        self._resolved.clear()

    async def send(self, scope: Scope, send: Send, exception: Exception, tail: _RouteTail) -> None:
        """Run the nearest handler, then send its response or the default problem.

        ``tail`` is the failing route's response-header tail (CORS pairs, middleware
        headers) — an error body a browser page must be able to *read* still needs the
        route's CORS pairs on it. Dynamic hooks are contained here (logged, skipped on
        failure), since this funnel is already sending the error response."""
        handler = self._resolve(type(exception))
        if handler is None:
            await self._send_default(scope, send, exception, tail)
            return
        # A user handler is an isolation boundary: its own ordinary failure must not
        # escape the app or recursively dispatch through the registry. gather's
        # return_exceptions mode contains that failure without another broad-except
        # suppression; process-control BaseExceptions still propagate below.
        (outcome,) = await asyncio.gather(
            invoke_exception_handler(handler, exception),
            return_exceptions=True,
        )
        if isinstance(outcome, Exception):
            # The handler itself failed. Surface both faults — the handler's own crash (the
            # immediate cause of this 500) and the original exception it was meant to
            # translate — since neither reaches the client, then send a generic 500.
            logger.error(
                "exception handler %s raised handling %s %s",
                handler.owner,
                scope["method"],
                scope["path"],
                exc_info=outcome,
            )
            self._log_unexpected(scope, exception)
            await self._send_default(scope, send, InternalServerError(), tail)
            return
        if isinstance(outcome, BaseException):
            raise outcome
        result = outcome.value
        if result is None:
            await self._send_default(scope, send, exception, tail)
            return
        if isinstance(result, BaseHTTPError):
            await self._send_default(scope, send, result, tail)
            return
        if not isinstance(result, ExceptionResponse):
            # The handler returned a value outside its declared contract (its return is
            # validated at wiring, so this is a runtime-only breach) — name the handler and
            # the bad type, and surface the original exception it was meant to translate
            # (whose 500 the client now sees), then send a generic 500.
            logger.error(
                "exception handler %s returned an invalid %s handling %s %s",
                handler.owner,
                type(result).__name__,
                scope["method"],
                scope["path"],
            )
            self._log_unexpected(scope, exception)
            await self._send_default(scope, send, InternalServerError(), tail)
            return
        await self._send_response(
            scope,
            send,
            cast("ExceptionResponse[Struct, Struct | None]", result),
            tail,
        )


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


async def _receive(receive: Receive) -> MutableMapping[str, Any]:
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
            receive_task: asyncio.Task[MutableMapping[str, Any]] = asyncio.create_task(
                _receive(receive)
            )
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
    _exceptions: _ExceptionHandlers
    _tail: _RouteTail

    def _chunk(self, item: object) -> bytes:
        if not isinstance(item, bytes):
            raise TypeError("StreamingResponse items must be bytes")
        return item

    async def _send_chunk(self, send: Send, chunk: bytes) -> None:
        await send({"type": "http.response.body", "body": chunk, "more_body": True})

    async def _send_setup_error(self, scope: Scope, send: Send, exc: Exception) -> None:
        # Route through the exception registry: a custom handler or a raised HTTPError becomes
        # its Problem response, and an unexpected setup failure is logged and mapped to a 500
        # (the logging lives in _ExceptionHandlers, the single funnel for both paths).
        await self._exceptions.send(scope, send, exc, self._tail)

    def _response_start(
        self, scope: Scope, result: _StreamResult
    ) -> tuple[int, list[tuple[bytes, bytes]]]:
        """The response status and assembled header list (typed/raw headers, links, tail)."""
        status = result.status_code if result.status_code is not None else self._status
        headers = _stream_headers(result.headers, result.raw_headers, self._content_type)
        headers += _link_header_pairs(self._reverser, scope, result.location, result.links)
        tail = self._tail
        if tail.active:
            tail.extend(headers, scope)
        return status, headers

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        result: _StreamResult,
    ) -> None:
        status, headers = self._response_start(scope, result)
        if scope["method"] == "HEAD":
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return
        try:
            iterator, first, lifecycle = await _resolve_stream(result.stream)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            await self._send_setup_error(scope, send, exc)
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
        except Exception:  # pylint: disable=broad-exception-caught
            # A client disconnect is handled above; reaching here means the stream itself
            # faulted (the source raised, or a chunk wouldn't encode). The 200 is already
            # sent, so the status can't change — log and stop.
            logger.exception("error streaming response for %s %s", scope["method"], scope["path"])
            return
        finally:
            try:
                await _finish_lifecycle(lifecycle)
            except Exception:  # pylint: disable=broad-exception-caught
                # Teardown failure: still swallowed so it can't crash the worker, but
                # logged now rather than lost.
                logger.exception(
                    "error tearing down stream for %s %s", scope["method"], scope["path"]
                )
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
                receive_task: asyncio.Task[MutableMapping[str, Any]] = asyncio.create_task(
                    _receive(receive)
                )
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
        status, headers = self._response_start(scope, result)
        if scope["method"] == "HEAD":
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return
        try:
            iterator, first, lifecycle = await _resolve_stream(result.stream)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            await self._send_setup_error(scope, send, exc)
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
        except Exception:  # pylint: disable=broad-exception-caught
            # A client disconnect is handled above; reaching here means the stream itself
            # faulted (the source raised, or a chunk wouldn't encode). The 200 is already
            # sent, so the status can't change — log and stop.
            logger.exception("error streaming response for %s %s", scope["method"], scope["path"])
            return
        finally:
            try:
                await _finish_lifecycle(lifecycle)
            except Exception:  # pylint: disable=broad-exception-caught
                # Teardown failure: still swallowed so it can't crash the worker, but
                # logged now rather than lost.
                logger.exception(
                    "error tearing down stream for %s %s", scope["method"], scope["path"]
                )
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def _result_sender(
    kind: ReturnKind,
    status: int,
    reverser: _Reverser,
    exceptions: _ExceptionHandlers,
    tail: _RouteTail,
) -> _Sender:
    if kind == "bytes":
        return _BytesSender(status, tail)
    if kind == "bytes-response":
        return _BytesResponseSender(status, reverser, tail)
    if kind in ("json-response", "created", "accepted"):
        return _JSONResponseSender(status, reverser, tail)
    if kind == "no-content":
        return _NoContentSender(status, reverser, tail)
    if kind == "stream-bytes":
        return _StreamSender(status, b"application/octet-stream", reverser, exceptions, tail)
    if kind == "stream-ndjson":
        return _NDJSONStreamSender(status, b"application/x-ndjson", reverser, exceptions, tail)
    if kind == "stream-sse":
        return _SSEStreamSender(status, b"text/event-stream", reverser, exceptions, tail)
    return _JSONSender(status, tail)


@dataclass(slots=True)
class _UnionResponseSender:
    """Dispatches a union return by the runtime type of the result, against member senders
    pre-resolved at wiring and ordered most-derived-first (see :func:`_union_sender` for why
    that ordering is needed — note jero's own wrappers are *siblings*, so it is never them
    that need it).

    A result matching no member means the handler returned something its own annotation
    forbids. Nothing has been sent at that point, so — unlike a mid-stream failure — it can
    still become a proper response: it is logged here and then answered through the app's
    exception handlers, rather than escaping into the server. Logging is done *before*
    delegating because an app may register a handler for ``TypeError`` and turn this into
    some ordinary 4xx; the framework fault must reach the operator either way.
    """

    _senders: tuple[tuple[type, _Sender], ...]
    _exceptions: _ExceptionHandlers
    _tail: _RouteTail

    async def __call__(self, scope: Scope, receive: Receive, send: Send, result: object) -> None:
        for response_type, sender in self._senders:
            if isinstance(result, response_type):
                await sender(scope, receive, send, result)
                return
        message = (
            f"handler returned {type(result).__name__}, which matches none of its "
            f"declared union return types"
        )
        logger.error("%s %s: %s", scope["method"], scope["path"], message)
        await self._exceptions.send(scope, send, TypeError(message), self._tail)


def _union_sender(
    members: tuple[ResponseMember, ...],
    reverser: _Reverser,
    exceptions: _ExceptionHandlers,
    tail: _RouteTail,
) -> _UnionResponseSender:
    """Build the union sender's isinstance chain, most-derived-first.

    jero's own wrappers are siblings, so nothing here depends on the order — but an
    application may subclass one (``class WidgetResponse(JSONResponse[Widget])``) and union
    it with its own base, and then the subclass has to be tested first or every instance
    matches the base. ``__mro__`` length orders that correctly with nothing to hand-maintain:
    a deeper subclass always has the longer mro. Wiring-time only; no per-request cost."""
    ordered = sorted(members, key=lambda member: len(member.response_type.__mro__), reverse=True)
    senders = tuple(
        (
            member.response_type,
            _result_sender(member.kind, member.status, reverser, exceptions, tail),
        )
        for member in ordered
    )
    return _UnionResponseSender(senders, exceptions, tail)


async def _drain_body(receive: Receive, first: bytes) -> bytes:
    """Join a multi-chunk request body. Only the rare multi-chunk case pays this call —
    the near-universal single-chunk read stays inlined in ``_Route.__call__``."""
    chunks = [first]
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            return b"".join(chunks)


class _Route:
    """A compiled handler: bind sources, call the user fn, send the result."""

    __slots__ = (
        "_arity",
        "_bind",
        "_bind_awaits_only_body",
        "_bind_is_sync",
        "_exceptions",
        "_fn",
        "_is_async",
        "_json_status",
        "_send_result",
        "_tail",
    )

    def __init__(
        self,
        fn: Callable[..., Any],
        status: int,
        *,
        sources: Sources,
        auth: _CompiledAuth | None,
        reverser: _Reverser,
        exceptions: _ExceptionHandlers,
        tail: _RouteTail,
    ) -> None:
        self._fn = fn
        self._is_async = inspect.iscoroutinefunction(fn)
        self._bind = _Binder(sources, auth)
        self._bind_is_sync = self._bind.is_sync
        self._bind_awaits_only_body = self._bind.awaits_only_body
        # Plain-JSON results (the overwhelmingly common kind) are sent inline in
        # __call__ rather than through _send_result, to save a coroutine hop.
        self._json_status = status if sources.return_kind == "json" else None
        self._send_result = (
            _union_sender(sources.return_members, reverser, exceptions, tail)
            if sources.return_kind == "union"
            else _result_sender(sources.return_kind, status, reverser, exceptions, tail)
        )
        self._exceptions = exceptions
        self._arity = sources.arity
        self._tail = tail

    async def _send_result_guarded(
        self, scope: Scope, receive: Receive, send: Send, result: object
    ) -> None:
        """Run the result sender, funneling assembly failures into an error response.

        Headers (dynamic tail hooks included) are assembled before
        ``http.response.start``, so a failure here has sent nothing yet and can still
        become a proper error response — the same funnel as a handler failure (custom
        handlers get their shot, then the generic 500 problem). A ``WiringError``
        surfacing at send time (e.g. a Link to an unmounted operation) is a programming
        error and stays loud instead.

        Accepted edge: if ``send`` itself fails *after* the response started (a
        transport error mid-send), the funnel's own send fails the same way and
        propagates to the server — exactly where such an error ended up before this
        guard existed. Tracking started-ness would cost a wrapper on every request to
        tidy a case the server already owns."""
        try:
            await self._send_result(scope, receive, send, result)
        except WiringError:
            raise
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            await self._exceptions.send(scope, send, exc, self._tail)

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, path_values: dict[str, str]
    ) -> None:
        try:
            if self._bind_is_sync:
                # Zero-source handlers have nothing to bind at all.
                bound = None if self._arity == 0 else self._bind.bind_sync(scope, path_values)
            elif self._bind_awaits_only_body:
                # Inlined body read (mirrors _Binder.__call__) to skip the binder
                # coroutine on unauthenticated body routes; one chunk is the near-
                # universal case, so only a second chunk pays the _drain_body call.
                message = await receive()
                body = message.get("body", b"")
                if message.get("more_body"):
                    body = await _drain_body(receive, body)
                bound = self._bind.bind_with_body(scope, path_values, body)
            else:
                bound = await self._bind(scope, receive, path_values)
            # 0/1-source handlers are called positionally (no kwargs dict); see _Binder.
            if self._arity >= 2:
                kwargs = cast("dict[str, object]", bound)
                result = await self._fn(**kwargs) if self._is_async else self._fn(**kwargs)
            elif self._arity == 1:
                result = await self._fn(bound) if self._is_async else self._fn(bound)
            else:
                result = await self._fn() if self._is_async else self._fn()
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            await self._exceptions.send(scope, send, exc, self._tail)
            return
        if self._json_status is not None:
            # Inlines _JSONSender (kept for _result_sender completeness) to save a
            # coroutine hop on the hot plain-JSON path. The tail is inlined too: the
            # constant pairs ride the header literal's unpack (near-free when empty,
            # no ``active`` branch or ``extend`` call), and dynamic hooks append
            # straight into the list.
            payload = msgspec_encoder.encode(result)
            tail = self._tail
            headers = [
                (b"content-type", b"application/json"),
                (b"content-length", b"%d" % len(payload)),
                *tail.pairs,
            ]
            if tail.dynamic:
                try:
                    tail.extend_dynamic(headers, scope)
                except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                    # A dynamic header hook raised: nothing sent yet, same funnel as a
                    # handler failure.
                    await self._exceptions.send(scope, send, exc, self._tail)
                    return
            await send(
                {
                    "type": "http.response.start",
                    "status": self._json_status,
                    "headers": headers,
                }
            )
            await send({"type": "http.response.body", "body": payload})
            return
        await self._send_result_guarded(scope, receive, send, result)


def _constant_middleware_pairs(headers: Struct) -> tuple[tuple[bytes, bytes], ...]:
    """A constant ``response_headers`` Struct as wire pairs, encoded once at wiring —
    the free tier: covered routes append these with a single list concat."""
    return tuple(
        (key.encode("latin-1"), value.encode("latin-1"))
        for key, value in _typed_header_items(headers)
    )


def _claim_header_names(
    claims: dict[str, str], owner: str, pairs: Sequence[tuple[bytes, bytes]]
) -> None:
    """Claim one contributor's constant header names on a route; a second claim of the
    same name is a ``WiringError`` naming both contributors. Constants are checkable at
    wiring, so they are — dynamic pairs cannot be, and append per HTTP semantics."""
    for key, _ in pairs:
        name = key.decode("latin-1").lower()
        existing = claims.get(name)
        if existing is not None:
            raise WiringError(
                f"duplicate constant response header {name!r}: declared by both "
                f"{existing} and {owner}",
            )
        claims[name] = owner


@dataclass(slots=True)
class _HeadersTailHook:
    """Adapts a compiled ``response_headers`` method hook to the tail-hook shape: one
    scan + call, then the returned Struct encoded as wire pairs (``None`` adds nothing).
    Runs inside the senders' header assembly, before ``http.response.start``."""

    _hook: HeadersHook

    def __call__(self, scope: Scope) -> list[tuple[bytes, bytes]]:
        headers = self._hook(scope)
        if headers is None:
            return []
        return [
            (key.encode("latin-1"), value.encode("latin-1"))
            for key, value in _typed_header_items(headers)
        ]


@dataclass(slots=True)
class _StatusCapture:
    """Wraps ``send`` to record the response's status and start time, so observe hooks
    can see the outcome without ever holding the response itself. Compiled in only on
    routes an observe hook covers."""

    _send: Send
    status: int = 0
    started_at: float = 0.0

    async def __call__(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.started_at = perf_counter()
        await self._send(message)


@dataclass(slots=True)
class _InterceptRunner:
    """One compiled intercept and the sender its responses leave through (built from the
    hook's return annotation at wiring — an intercept response reuses the exact sender a
    handler returning that type would)."""

    _hook: InterceptHook
    _send_result: _Sender

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> bool:
        result = await self._hook(scope)
        if result is None:
            return False
        await self._send_result(scope, receive, send, result)
        return True


async def _run_intercepts(
    runners: tuple[_WebSocketInterceptRunner, ...],
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    exceptions: _ExceptionHandlers,
    tail: _RouteTail,
) -> bool:
    """Run one verb's intercepts in registration order; the first response wins.

    True when a response left — including an error response: a failing intercept enters
    the same funnel as a handler failure, and the request is answered either way."""
    try:
        for runner in runners:
            if await runner(scope, receive, send):
                return True
    except WiringError:
        raise
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        await exceptions.send(scope, send, exc, tail)
        return True
    return False


def _intercept_sender(
    owner: str,
    hook: InterceptHook,
    reverser: _Reverser,
    exceptions: _ExceptionHandlers,
    tail: _RouteTail,
) -> _Sender:
    """The sender for an intercept's responses, resolved from its return annotation.

    The annotation follows handler rules — any buffered return kind, unioned freely,
    with ``| None`` meaning fall-through (streaming kinds are rejected, like union
    members). A plain member's status is 200: an intercept has no verb defaults to
    inherit; the fixed-status wrappers (``NoContent``/``Created``/``Accepted``) and
    ``status_code=`` overrides carry their own."""
    hint = unwrap_alias(hook.return_annotation)
    members = _union_args(hint)
    flattened = _flatten_union_members(members) if members is not None else (hint,)
    concrete = tuple(member for member in flattened if not _is_none_type(member))
    if not concrete:
        raise WiringError(
            f"{owner}.intercept must declare at least one response return type "
            f"(None alone answers nothing)",
        )
    resolved = _union_return_members(f"{owner}.intercept", concrete, 200)
    if len(resolved) == 1:
        member = resolved[0]
        return _result_sender(member.kind, member.status, reverser, exceptions, tail)
    return _union_sender(resolved, reverser, exceptions, tail)


@dataclass(slots=True)
class _RouteMiddleware:
    """Everything middleware adds to one route's dispatch, resolved at ``__finalize``:
    the verb-keyed scoped intercepts and the observe hooks (global + scoped). Routes
    nothing covers never see one of these — they stay unwrapped ``_Route``\\ s."""

    intercepts: dict[str, tuple[_InterceptRunner, ...]] | None
    observes: tuple[ObserveHook, ...]


@dataclass(slots=True)
class _CoveredRoute:
    """A route at least one middleware hook covers, wrapping the compiled ``_Route``.

    Swapped into the routing tables at ``__finalize`` so uncovered routes pay nothing —
    not even a branch. Per covered request: stamp ``received_at`` (hooks read it off the
    scope), run the scoped intercepts for the wire method (first response wins, before
    auth and binding), dispatch, then let the observe hooks see the captured outcome."""

    _route: _Handler
    _mw: _RouteMiddleware
    _exceptions: _ExceptionHandlers
    _tail: _RouteTail

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, path_values: dict[str, str]
    ) -> None:
        received_at = perf_counter()
        scope["jero.received_at"] = received_at
        mw = self._mw
        capture: _StatusCapture | None = None
        if mw.observes:
            send = capture = _StatusCapture(send)
        answered = False
        if mw.intercepts is not None:
            runners = mw.intercepts.get(scope["method"])
            if runners is not None:
                answered = await _run_intercepts(
                    runners, scope, receive, send, exceptions=self._exceptions, tail=self._tail
                )
        if not answered:
            await self._route(scope, receive, send, path_values)
        if capture is not None:
            duration = capture.started_at - received_at if capture.started_at else 0.0
            for observe in mw.observes:
                await observe(scope, capture.status, duration)


@dataclass(slots=True)
class _GlobalMiddleware:
    """The app-level middleware machinery ``__call__`` consults: the verb-keyed global
    intercept table (pre-routing, so an intercept can answer for a path that would 404)
    and the global observe hooks (which also see short-circuits and fallthrough
    responses). ``None`` on apps without global intercept/observe hooks, so the
    disabled cost is one attribute load."""

    intercepts: dict[str, tuple[_InterceptRunner, ...]]
    observes: tuple[ObserveHook, ...]


def _camel(name: str) -> str:
    """A snake_case handler name as camelCase, for the default operationId."""
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _json_doc_handler(config: "_OpenAPIConfig", tail: _RouteTail) -> _Handler:
    """A handler serving the cached OpenAPI document. The payload is filled in at
    ``__finalize`` (after wiring), so the route can be registered before the document
    exists — the closure reads ``config.payload`` at request time. ``tail`` carries the
    app-default CORS pairs and middleware headers onto the response (contained: a hook
    failure on a framework route is logged and skipped, never a 500 docs page)."""

    async def handler(
        scope: Scope, receive: Receive, send: Send, path_values: dict[str, str]
    ) -> None:
        _ = (receive, path_values)
        await _send_json(send, 200, config.payload, tail.contained_extra(scope))

    return handler


def _static_bytes_handler(body: bytes, content_type: bytes, tail: _RouteTail) -> _Handler:
    """A handler serving a precomputed byte payload (the docs UI page, the favicon).
    ``tail`` as on :func:`_json_doc_handler`."""

    async def handler(
        scope: Scope, receive: Receive, send: Send, path_values: dict[str, str]
    ) -> None:
        _ = (receive, path_values)
        headers = [
            (b"content-type", content_type),
            (b"content-length", str(len(body)).encode()),
        ]
        extra = tail.contained_extra(scope)
        if extra is not None:
            headers += extra
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return handler


# Favicon media types by file suffix; anything else is a loud wiring failure.
_FAVICON_CONTENT_TYPES: dict[str, bytes] = {
    ".ico": b"image/x-icon",
    ".png": b"image/png",
    ".svg": b"image/svg+xml",
}


def _favicon_payload(favicon: Path) -> tuple[bytes, bytes]:
    """Read the favicon once at wiring: its bytes and content type. Fails loud on an
    unsupported suffix or an unreadable file — never at request time."""
    content_type = _FAVICON_CONTENT_TYPES.get(favicon.suffix.lower())
    if content_type is None:
        supported = ", ".join(sorted(_FAVICON_CONTENT_TYPES))
        raise WiringError(
            f"_include_openapi favicon {favicon} has an unsupported suffix; use {supported}",
        )
    try:
        body = favicon.read_bytes()
    except OSError as exc:
        raise WiringError(f"_include_openapi favicon {favicon} is not readable: {exc}") from exc
    return body, content_type


def _scalar_html(
    title: str,
    openapi_path: str,
    favicon_href: str | None,
    config: ScalarConfig | None,
) -> str:
    """The default docs page: Scalar's API reference, loaded from a CDN, pointed at the spec.

    ``config`` is rendered as Scalar's ``data-configuration`` (its set fields as JSON,
    HTML-escaped into the attribute); an all-default config adds nothing.
    """
    favicon_link = f'<link rel="icon" href="{favicon_href}">\n' if favicon_href is not None else ""
    config_attr = ""
    if config is not None:
        encoded = msgspec_encoder.encode(config).decode()
        if encoded != "{}":
            config_attr = f' data-configuration="{html.escape(encoded, quote=True)}"'
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{favicon_link}"
        f"<title>{title}</title>\n"
        "</head>\n"
        "<body>\n"
        f'<script id="api-reference" data-url="{openapi_path}"{config_attr}></script>\n'
        '<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>\n'
        "</body>\n"
        "</html>\n"
    )


@dataclass(slots=True)
class _OpenAPIConfig:
    """The settings stashed by ``_include_openapi`` and read at ``__finalize`` / startup."""

    title: str
    version: str
    description: str | None
    servers: tuple[str, ...]
    tags: tuple[Tag, ...]
    openapi_path: str
    docs_path: str | None
    payload: bytes = b"{}"  # the built OpenAPI document, filled in at __finalize


def _assemble_openapi_tags(
    central: tuple[Tag, ...], operations: tuple[OperationInput, ...]
) -> tuple[Tag, ...]:
    """The document-level ``tags`` array: every distinct tag from the central declaration
    and the operations' ``meta``, in first-seen order (central first). A tag is described
    once — a bare ``Tag(name)`` references it, a ``Tag(name, description)`` defines it, in
    any order. The same name given two *different* descriptions anywhere is a loud wiring
    error, so a tag's description can't silently fork."""
    resolved: dict[str, str | None] = {}
    sources: list[Tag] = [*central]
    for op in operations:
        sources += op.tags
    for tag in sources:
        existing = resolved.get(tag.name)
        if tag.name not in resolved or existing is None:
            resolved[tag.name] = tag.description
        elif tag.description is not None and tag.description != existing:
            raise WiringError(
                f"OpenAPI tag {tag.name!r} is given conflicting descriptions "
                f"({existing!r} and {tag.description!r}); describe each tag once",
            )
    return tuple(Tag(name=name, description=desc) for name, desc in resolved.items())


class BaseFactory:
    """Base for an app's factory. Subclass and add ``create_*`` methods that
    build services with ``self._enter`` / ``self._aenter``.

    Under an app, the app injects its exit stacks (``es`` / ``aes``); anything
    opened via the helpers is closed when the app shuts down. Standalone —
    scripts, cron jobs, notebooks — enter :meth:`open` instead and the factory
    owns the same lifecycle itself.
    """

    def __init__(self, es: ExitStack, aes: AsyncExitStack) -> None:
        self.__stack = es
        self.__astack = aes

    def _enter[T](self, cm: AbstractContextManager[T]) -> T:
        """Open a sync context manager on the factory's injected stack — under an app
        that is the app's stack, so the resource is closed at the app's shutdown."""
        return self.__stack.enter_context(cm)

    async def _aenter[T](self, cm: AbstractAsyncContextManager[T]) -> T:
        """Open an async context manager on the factory's injected stack — under an app
        that is the app's stack, so the resource is closed at the app's shutdown."""
        return await self.__astack.enter_async_context(cm)

    @classmethod
    @asynccontextmanager
    async def open(cls) -> AsyncGenerator[Self]:
        """Use the factory's service graph standalone, with real lifecycle::

            async with Factory.open() as factory:
                service = await factory.create_widget_service()

        Creates a fresh exit-stack pair, builds the factory on them exactly as an
        app does at startup, and unwinds on exit — async resources first, each
        stack in reverse order, even if the block raises. (``FactoryHarness`` is
        this, bridged onto a background loop for synchronous tests.)
        """
        with ExitStack() as stack:
            async with AsyncExitStack() as astack:
                yield _instantiate_factory(cls, stack, astack)


def _instantiate_factory[F](factory_cls: type[F], stack: ExitStack, astack: AsyncExitStack) -> F:
    """Build a factory, injecting whichever of ``es`` / ``aes`` its __init__ names.

    Shared by ``BaseApp`` (live wiring) and ``BaseFactory.open`` (standalone use,
    which ``FactoryHarness`` bridges for sync tests) so a factory is constructed
    identically everywhere — given the stacks it opens resources on.
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


class BaseApp[FactoryT = None](ABC):
    """Subclass and override ``wire`` to open resources and include resources/endpoints.

    The app owns the two exit stacks. Parameterize with a factory class —
    ``class MyApp(BaseApp[MyFactory])`` — and the app builds it at construction,
    injecting the stacks the factory's ``__init__`` names (``es`` for the
    ExitStack, ``aes`` for the AsyncExitStack). The built factory is then
    ``self._factory`` (typed as ``MyFactory``) inside ``wire``, and any resource
    it registers on those stacks is closed at shutdown.

    Pass ``factory=`` to supply a prebuilt factory instead of building one — the
    seam for tests, which inject a ``create_autospec`` stand-in
    (``MyApp(factory=mock_factory)``) so the real services are never constructed.

    Reverse-routed ``Location`` / ``Link`` URLs are relative unless the environment sets
    ``JERO_BASE_URL`` (a static public origin) or ``JERO_TRUST_FORWARDED`` (rebuild the
    origin per request from ``X-Forwarded-*``); see :func:`_forwarded_config_from_env`.
    """

    def __init__(self, *, factory: FactoryT | None = None) -> None:
        self.__static: _StaticRoutes = {}
        self.__dynamic: _DynamicRoutes = {}
        self.__websocket_static: dict[str, _WebSocketHandler] = {}
        self.__websocket_dynamic: dict[int, list[_WebSocketPattern]] = {}
        self.__allowed: _AllowedMethods = {}
        self.__allow_cache: dict[str, bytes] = {}
        self.__decoders: dict[type[Struct], Decoder[Struct]] = {}
        self.__operations: list[OperationSpec] = []  # captured for the OpenAPI document
        self.__openapi: _OpenAPIConfig | None = None  # set by _include_openapi, built at __finalize
        base_url, trust_forwarded = _forwarded_config_from_env()
        self.__reverser = _Reverser(base_url=base_url, trust_forwarded=trust_forwarded)
        self.__exceptions = _ExceptionHandlers(self.__reverser)
        # The tail for responses no route owns (unrouted 404, 405): the app-wide CORS
        # default and global middleware headers, filled at __finalize.
        self.__app_tail = _RouteTail()
        self.__includes: list[_IncludeRecord] = []
        self.__cors_default: CompiledCORS | None = None  # set by _include_cors
        # route handler -> its resolved CORS policy, for answering preflights; built
        # at __finalize once inheritance is resolved.
        self.__route_cors: dict[_Handler, CompiledCORS] = {}
        self.__middleware: list[CompiledMiddleware] = []  # global, in registration order
        # The pre-routing middleware machinery (global intercept table + observes);
        # stays None unless global middleware defines those hooks, so the disabled
        # per-request cost is one attribute load.
        self.__pre: _GlobalMiddleware | None = None
        self.__stack = ExitStack()
        self.__astack = AsyncExitStack()
        self.__factory: FactoryT = factory if factory is not None else self.__make_factory()

    @property
    def _factory(self) -> FactoryT:
        """The built factory, read inside ``wire``. Set once at construction (read-only)."""
        return self.__factory

    def _enter[T](self, cm: AbstractContextManager[T]) -> T:
        """Open a sync context manager, closed at shutdown in reverse order."""
        return self.__stack.enter_context(cm)

    async def _aenter[T](self, cm: AbstractAsyncContextManager[T]) -> T:
        """Open an async context manager, closed at shutdown in reverse order."""
        return await self.__astack.enter_async_context(cm)

    def __decoder(self, struct_type: type[Struct]) -> Decoder[Struct]:
        """The reusable typed JSON decoder for ``struct_type``, built once per app.

        Decoders are keyed by type, so models shared across handlers (a ``WidgetIn``
        used by both ``create`` and ``update_full``) share one decoder. Populated only at
        wiring time; the binder holds the resolved decoder, so the request path does
        no lookup.
        """
        if struct_type not in self.__decoders:
            self.__decoders[struct_type] = Decoder(struct_type)
        return self.__decoders[struct_type]

    def __resolve_factory_type(self) -> type | None:
        """The factory class from ``BaseApp[...]``, or None if unparameterized."""
        for base in get_original_bases(type(self)):
            if get_origin(base) is BaseApp:
                args = get_args(base)
                if args and isinstance(args[0], type) and args[0] is not type(None):
                    return args[0]
        return None

    def __make_factory(self) -> FactoryT:
        factory_type = self.__resolve_factory_type()
        if factory_type is None:
            return cast("FactoryT", None)
        return cast("FactoryT", _instantiate_factory(factory_type, self.__stack, self.__astack))

    @abstractmethod
    async def wire(self) -> None:
        """Override to open resources (via ``_enter`` / ``_aenter``) and include them.

        Runs once at startup. Anything entered via the helpers is torn
        down (in reverse order) at shutdown.

        Abstract: every ``BaseApp`` subclass must implement it. A subclass that
        omits it is flagged at its instantiation site by the type checker.
        """

    def __register(self, method: HTTPMethod, segments: list[_Segment], handler: _Handler) -> None:
        params = tuple((i, value) for i, (is_param, value) in enumerate(segments) if is_param)
        if not params:
            route_path = "/".join(value for _, value in segments)
            if (method, route_path) in self.__static:
                raise WiringError(f"{method} {route_path} is already registered")
            self.__static[(method, route_path)] = handler
            self.__allowed.setdefault(route_path, []).append(method)
            return

        statics = tuple((i, value) for i, (is_param, value) in enumerate(segments) if not is_param)
        bucket = self.__dynamic.setdefault((method, len(segments)), [])
        if any(pattern.statics == statics for pattern in bucket):
            raise WiringError(f"{method} {_template_str(segments)} is already registered")
        bucket.append(_Pattern(statics, params, handler))

    def __register_websocket(self, segments: list[_Segment], handler: _WebSocketHandler) -> None:
        params = tuple((i, value) for i, (is_param, value) in enumerate(segments) if is_param)
        if not params:
            route_path = "/".join(value for _, value in segments)
            if route_path in self.__websocket_static:
                raise WiringError(f"WebSocket {route_path} is already registered")
            self.__websocket_static[route_path] = handler
            return
        statics = tuple((i, value) for i, (is_param, value) in enumerate(segments) if not is_param)
        bucket = self.__websocket_dynamic.setdefault(len(segments), [])
        if any(pattern.statics == statics for pattern in bucket):
            raise WiringError(f"WebSocket {_template_str(segments)} is already registered")
        bucket.append(_WebSocketPattern(statics, params, handler))

    @staticmethod
    def __check_user_source(
        resource_cls: type,
        name: str,
        sources: Sources,
        auth: _CompiledAuth | None,
    ) -> None:
        """Validate a handler's ``user`` annotation against the route's authenticator — that
        there is one, that the Struct matches, and that its optionality agrees.

        A handler on an anonymous-accepting route must declare ``user``: with nothing to
        check, a handler that ignores the auth result would serve anonymous callers with no
        sign of it at the mount or in the signature. Behind a gating authenticator, omitting
        ``user`` stays fine — the gate has already run."""
        user_type = sources.user
        if user_type is None:
            if auth is not None and auth.reports_absence:
                raise WiringError(
                    f"{resource_cls.__name__}.{name} declares no 'user', but "
                    f"{auth.owner}.authenticate returns {auth.returns.__name__} | None, so "
                    f"this route serves anonymous callers — declare "
                    f"'user: {auth.returns.__name__} | None' and handle None, or mount it "
                    f"behind an authenticator that returns {auth.returns.__name__} to gate it",
                )
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
        if auth.reports_absence and not sources.user_optional:
            raise WiringError(
                f"{resource_cls.__name__}.{name}: 'user' must be annotated "
                f"'{user_type.__name__} | None' — {auth.owner}.authenticate returns "
                f"'{user_type.__name__} | None', so a caller may arrive anonymous",
            )
        if sources.user_optional and not auth.reports_absence:
            raise WiringError(
                f"{resource_cls.__name__}.{name}: 'user' must be annotated "
                f"'{user_type.__name__}' — {auth.owner}.authenticate returns "
                f"'{user_type.__name__}', so an unauthenticated caller never reaches the "
                f"handler; return '{user_type.__name__} | None' from it to accept anonymous "
                f"callers",
            )

    def __include(
        self,
        obj: Resource | Endpoint,
        methods: dict[str, _Verb],
        *,
        auth: Auth[Any, Any] | None,
        cors: CORS | None,
        middleware: Sequence[object],
    ) -> None:
        cls = type(obj)
        if cors is not None and not isinstance(cast("object", cors), CORS):
            raise WiringError(
                f"{cls.__name__}: cors= must be a CORS policy (or CORS.OFF), "
                f"got {type(cors).__name__}",
            )
        # Explicit policies are validated and compiled here, at the include call —
        # only what depends on other registrations (inheriting an omitted cors=, the
        # global middleware every route picks up) waits for __finalize.
        cors_off = cors is CORS.OFF
        compiled_cors = CompiledCORS(cors) if cors is not None and not cors_off else None
        compiled_middleware = tuple(CompiledMiddleware(m) for m in middleware)
        # One shared tail per include: every route (and sender) the include registers
        # holds this instance, and __finalize fills it in place once coverage is known.
        tail = _RouteTail()
        record = _IncludeRecord(
            tail=tail,
            routes=[],
            cors=compiled_cors,
            cors_off=cors_off,
            middleware=compiled_middleware,
        )
        path = getattr(cls, "path", None)
        if path is None:
            raise WiringError(
                f"{cls.__name__}: no path — declare it on the class, "
                f"e.g. `class {cls.__name__}(..., path='/...')`.",
            )
        template = _parse_template(path)
        # The authenticator's declared return type is the policy: `-> TUser | None` accepts
        # anonymous callers, `-> TUser` gates. Never inferred from anything else.
        compiled_auth = _CompiledAuth(auth) if auth is not None else None
        auth_mode: AuthMode = None
        if compiled_auth is not None:
            auth_mode = "optional" if compiled_auth.reports_absence else "required"
        # An authed route with no declared scheme defaults to HTTP bearer (the common case).
        security_scheme: SecurityScheme | None = None
        if auth is not None:
            declared: object = getattr(type(auth), "openapi_security", None)
            if declared is None:
                security_scheme = SecurityScheme.http_bearer()
            elif isinstance(declared, SecurityScheme):
                security_scheme = declared
            else:
                raise WiringError(
                    f"{type(auth).__name__}: openapi_security must be SecurityScheme, "
                    f"got {type(declared).__name__}",
                )

        registered = False
        for name, verb in methods.items():
            fn = getattr(obj, name, None)
            if fn is None:
                continue
            sources = _bind_sources(cls, name, fn, verb, self.__decoder)
            self.__check_user_source(cls, name, sources, compiled_auth)
            segments = _route_segments(
                cls, name, template, sources.path, extends_path=verb.extends_path
            )
            # A NoContent/Created/Accepted return fixes its own status regardless of the
            # verb's default (204/201/202); a union's members carry their own already.
            status = _effective_status(sources.return_kind, verb.success_status)
            handler = _Route(
                fn,
                status,
                sources=sources,
                auth=compiled_auth,
                reverser=self.__reverser,
                exceptions=self.__exceptions,
                tail=tail,
            )
            self.__register(verb.method, segments, handler)
            record.routes.append((verb.method, handler))
            self.__reverser.register(
                fn.__func__, cls.ref, name, _RouteRef(tuple(segments), sources.path)
            )
            self.__operations.append(
                OperationSpec(
                    path=_template_str(segments),
                    method=verb.method.lower(),
                    success_status=status,
                    sources=sources,
                    auth_mode=auth_mode,
                    security_scheme=security_scheme,
                    class_meta=cls.meta,
                    op_meta=getattr(cls, f"meta_{name}", None),
                    operation_id_default=f"{cls.__name__}_{_camel(name)}",
                )
            )
            registered = True

        if not registered:
            raise WiringError(f"{cls.__name__} defines none of: {', '.join(methods)}")
        self.__includes.append(record)

    def _include_exception_handler[
        E: Exception,
    ](self, handler: ExceptionHandler[E]) -> None:
        """Register a structurally typed custom exception handler.

        The exception, JSON body, and typed-header types are inferred from the concrete
        ``handle_exception`` signature and validated once during wiring.
        """
        self.__exceptions.register(handler)

    def _include_error_adapter(self, adapter: ErrorBodyAdapter[Any]) -> None:
        """Replace the Problem family's wire body app-wide with ``adapter``'s composition.

        Call inside ``wire``, at most once. Every Problem-family error — the framework's
        built-ins (404/405/422/500, …) and your own ``HTTPError`` subclasses, including
        those returned by exception handlers — is rendered through ``adapter.compose``
        instead of RFC 9457 Problem Details, and the derived OpenAPI error responses
        document the adapter's body. ``StructHTTPError``\\ s render themselves.
        """
        # The isinstance guards untyped callers; cast first so it isn't statically vacuous.
        if not isinstance(cast("object", adapter), ErrorBodyAdapter):
            raise WiringError(
                "_include_error_adapter requires an ErrorBodyAdapter instance, "
                f"got {type(adapter).__name__}",
            )
        if getattr(type(adapter), "body_type", None) is None:
            raise WiringError(
                f"{type(adapter).__name__} never bound a concrete body Struct; "
                "parameterize the class: ErrorBodyAdapter[YourBody]",
            )
        if self.__exceptions.adapter is not None:
            existing = type(self.__exceptions.adapter).__name__
            raise WiringError(
                f"an error body adapter ({existing}) is already registered; an app has at most one",
            )
        self.__exceptions.adapter = adapter

    def _include_cors(self, cors: CORS) -> None:
        """Serve cross-origin browser callers app-wide with one default :class:`CORS` policy.

        Call inside ``wire``, at most once (order among the ``include_*`` calls doesn't
        matter). Every include inherits the policy unless it passes its own ``cors=`` —
        a different policy overrides it, ``CORS.OFF`` removes it. Skipping
        ``_include_cors`` entirely means no CORS anywhere except includes that opt in
        with their own ``cors=``.

        The policy is compiled at wiring: a wildcard origin becomes constant header
        pairs in covered routes' responses (free per request), an origin allow-list one
        set lookup + origin echo. Preflights (``OPTIONS`` with
        ``Access-Control-Request-Method``) are answered per (path, requested method) on
        the existing OPTIONS branch, so two verbs on one path may carry two policies.
        Error responses carry the failing route's pairs — a browser page must be able
        to *read* the 401/422 problem body; unrouted 404s carry this app default.
        """
        if not isinstance(cast("object", cors), CORS):
            raise WiringError(f"_include_cors requires a CORS policy, got {type(cors).__name__}")
        if cors is CORS.OFF:
            raise WiringError(
                "CORS.OFF is the per-include opt-out; an app that wants no CORS default "
                "simply does not call _include_cors",
            )
        if self.__cors_default is not None:
            raise WiringError("a CORS default is already registered; an app has at most one")
        self.__cors_default = CompiledCORS(cors)

    def _include_middleware(self, middleware: object) -> None:
        """Register one middleware app-wide: every route (current and later includes) is
        covered, and its intercepts run *pre-routing* — they can answer requests no
        route serves, which is how an OPTIONS-scoped intercept answers preflights for
        paths that would 404.

        The middleware is a structurally typed object — no base class; its hooks
        (``response_headers`` attribute or method, ``intercept`` + ``intercept_methods``,
        ``observe``) are introspected and validated here, fail-loud, and compiled into
        the covered routes at wiring (see :class:`~jero.CORS` for the same idea as a
        built-in). Register order is run order, and globals run before include-scoped
        middleware. Only what a middleware defines costs anything: a constant
        ``response_headers`` is baked into route header blocks for free, an off-scope
        verb never reaches an ``intercept``.
        """
        self.__middleware.append(CompiledMiddleware(middleware))

    def _include_resource[THeaders: Struct, TUser: Struct](
        self,
        resource: Resource,
        *,
        auth: Auth[THeaders, TUser] | None = None,
        cors: CORS | None = None,
        middleware: Sequence[object] = (),
    ) -> None:
        """Register a ``Resource``'s CRUD methods as routes, optionally behind ``auth``.

        An authenticator returning ``TUser`` gates every method: no valid credentials, no
        handler. One returning ``TUser | None`` accepts anonymous callers instead — see
        :meth:`_include_endpoint`.

        ``cors=`` sets this include's :class:`CORS` policy: omitted inherits the
        ``_include_cors`` default, a policy overrides it, ``CORS.OFF`` opts out.
        ``middleware=`` adds include-scoped middleware on top of any registered with
        :meth:`_include_middleware` — scope is deployment policy, so it lives here at
        the mount, not on the class. Scoped intercepts run post-resolve, pre-auth.
        """
        self.__include(resource, Resource.METHODS, auth=auth, cors=cors, middleware=middleware)

    def _include_endpoint[THeaders: Struct, TUser: Struct](
        self,
        endpoint: Endpoint,
        *,
        auth: Auth[THeaders, TUser] | None = None,
        cors: CORS | None = None,
        middleware: Sequence[object] = (),
    ) -> None:
        """Register an ``Endpoint``'s verb methods as routes, optionally behind ``auth``.

        An authenticator returning ``TUser`` gates every verb: no valid credentials, no
        handler. One returning ``TUser | None`` makes credentials an *input* — a caller
        presenting none is served with ``user=None``, *invalid* credentials are still a 401,
        and every handler on the route must declare ``user: TUser | None`` (all checked at
        startup).

        ``cors=`` sets this include's :class:`CORS` policy: omitted inherits the
        ``_include_cors`` default, a policy overrides it, ``CORS.OFF`` opts out.
        ``middleware=`` adds include-scoped middleware on top of any registered with
        :meth:`_include_middleware` — scope is deployment policy, so it lives here at
        the mount, not on the class. Scoped intercepts run post-resolve, pre-auth.
        """
        self.__include(endpoint, Endpoint.METHODS, auth=auth, cors=cors, middleware=middleware)

    def _include_websocket[THeaders: Struct, TUser: Struct](
        self,
        endpoint: WebSocketEndpoint,
        *,
        auth: Auth[THeaders, TUser] | None = None,
        max_frame_size: int = 1024 * 1024,
        middleware: Sequence[object] = (),
    ) -> None:
        """Register one typed WebSocket protocol and compile its handshake contract."""
        if not isinstance(max_frame_size, int) or isinstance(max_frame_size, bool):
            raise WiringError("max_frame_size must be a positive integer")
        if max_frame_size < 1:
            raise WiringError("max_frame_size must be a positive integer")
        cls = type(endpoint)
        path = getattr(cls, "path", None)
        if path is None:
            raise WiringError(
                f"{cls.__name__}: no path — declare it on the class, "
                f"e.g. `class {cls.__name__}(WebSocketEndpoint, path='/...')`.",
            )
        fn = getattr(endpoint, "handle", None)
        if fn is None:
            raise WiringError(f"{cls.__name__} must define handle")
        sources, inbound, outbound = _bind_websocket_sources(cls, fn)
        compiled_auth = _CompiledAuth(auth) if auth is not None else None
        self.__check_user_source(cls, "handle", sources, compiled_auth)
        tail = _RouteTail()
        intercepts: list[_WebSocketInterceptRunner] = []
        for item in middleware:
            compiled = CompiledMiddleware(item)
            if compiled.intercept is None:
                continue
            if "GET" not in compiled.intercept_methods:
                raise WiringError(
                    f"{compiled.owner}.intercept can never run on a WebSocket handshake: "
                    f"intercept_methods is {compiled.intercept_methods!r}, expected GET",
                )
            intercepts.append(
                _InterceptRunner(
                    compiled.intercept,
                    _intercept_sender(
                        compiled.owner,
                        compiled.intercept,
                        self.__reverser,
                        self.__exceptions,
                        tail,
                    ),
                )
            )
        segments = _route_segments(
            cls, "handle", _parse_template(path), sources.path, extends_path=False
        )
        route = _WebSocketRoute(
            fn,
            sources=sources,
            inbound=inbound,
            outbound=outbound,
            auth=compiled_auth,
            exceptions=self.__exceptions,
            intercepts=tuple(intercepts),
            tail=tail,
            max_frame_size=max_frame_size,
        )
        self.__register_websocket(segments, route)

    def _include_openapi(
        self,
        *,
        title: str,
        version: str,
        description: str | None = None,
        openapi_path: str = "/openapi.json",
        docs_path: str | None = "/docs",
        servers: Sequence[str] = (),
        tags: Sequence[Tag] = (),
        docs_html: str | None = None,
        favicon: Path | str | None = None,
        scalar_config: ScalarConfig | None = None,
    ) -> None:
        """Serve an auto-generated OpenAPI 3.1 document and a docs UI.

        Call inside ``wire`` (order among the ``include_*`` calls doesn't matter — the
        document is built once after wiring completes). ``openapi_path`` serves the JSON
        spec; ``docs_path`` serves a Scalar UI pointed at it (pass ``None`` to omit the
        UI, or ``docs_html`` to replace the page — e.g. for offline / strict-CSP hosting).

        ``scalar_config`` is a typed :class:`~jero.ScalarConfig` tuning the Scalar UI — e.g.
        ``scalar_config=ScalarConfig(hide_models=True)`` drops the global Models list, or set a
        ``theme`` / ``layout``. Only its set fields are sent, so Scalar's own defaults apply
        otherwise. For options ``ScalarConfig`` doesn't model, supply a full ``docs_html``
        page instead (``scalar_config`` is ignored when ``docs_html`` is given).

        ``favicon`` gives the docs page an icon. A ``Path`` (the primary case) is read
        once at wiring — a missing/unreadable file or an unsupported suffix
        (``.ico``/``.png``/``.svg``) is a ``WiringError`` — and served as a precomputed
        response at ``/favicon.ico``; no runtime file I/O. A ``str`` is a URL (a
        ``data:`` URI works too), emitted verbatim in the page's ``<link rel="icon">``
        with nothing served. Like the spec routes, ``/favicon.ico`` never appears in the
        generated document. A custom ``docs_html`` page is never modified — reference
        the favicon yourself there.

        ``tags`` declares document-level ``Tag``\\ s to describe operation groups and pin the
        order they appear in the docs UI. Operations may also define/use tags via their
        ``meta`` (a bare name, or a ``Tag`` with a description); all are merged here. The one
        rule: describing the same tag name two different ways is a startup ``WiringError``.

        The spec is derived from your wired resources/endpoints: their typed sources
        (path/query/header params, request bodies), return types (responses), auth
        (security), ``msgspec.Meta`` field constraints, and the metadata you declare —
        ``OperationMeta`` (summary/description/tags/responses) and a model's ``ModelMeta``.
        Docstrings are never published; public prose is always explicit.
        """
        self.__openapi = _OpenAPIConfig(
            title=title,
            version=version,
            description=description,
            servers=tuple(servers),
            tags=tuple(tags),
            openapi_path=openapi_path,
            docs_path=docs_path,
        )
        # The framework routes register through an include record like any other
        # include, so they are covered by the app's CORS default and middleware (a
        # cross-origin tool must be able to fetch the spec; a global security-headers
        # middleware must decorate the docs page). They just never appear in the
        # generated document.
        tail = _RouteTail()
        record = _IncludeRecord(tail=tail, routes=[], cors=None, cors_off=False, middleware=())
        favicon_href: str | None = None
        if isinstance(favicon, Path):
            body, content_type = _favicon_payload(favicon)
            favicon_handler = _static_bytes_handler(body, content_type, tail)
            self.__register("GET", _parse_template("/favicon.ico"), favicon_handler)
            record.routes.append(("GET", favicon_handler))
            favicon_href = "/favicon.ico"
        elif favicon is not None:
            favicon_href = favicon
        doc_handler = _json_doc_handler(self.__openapi, tail)
        self.__register("GET", _parse_template(openapi_path), doc_handler)
        record.routes.append(("GET", doc_handler))
        if docs_path is not None:
            page = (
                docs_html
                if docs_html is not None
                else _scalar_html(title, openapi_path, favicon_href, scalar_config)
            )
            page_handler = _static_bytes_handler(page.encode(), b"text/html; charset=utf-8", tail)
            self.__register("GET", _parse_template(docs_path), page_handler)
            record.routes.append(("GET", page_handler))
        self.__includes.append(record)

    async def _create_background_tasks(
        self,
        *,
        maxsize: int = 1024,
        drain_timeout: float | None = 30.0,
        allow_one_to_many: bool = False,
    ) -> BackgroundTasks:
        """Build a :class:`BackgroundTasks` queue bound to the app's lifecycle.

        Sugar for ``await self._aenter(BackgroundTasks(...))``: the worker starts at
        startup and drains/stops at shutdown. Call inside ``wire`` *after* the services
        its handlers use, so reverse-order shutdown drains the queue before those
        services are torn down.
        """
        return await self._aenter(
            BackgroundTasks(
                maxsize=maxsize,
                drain_timeout=drain_timeout,
                allow_one_to_many=allow_one_to_many,
            )
        )

    def __build_openapi_document(self, config: _OpenAPIConfig) -> bytes:
        """Translate the captured operations into the OpenAPI document, encoded as JSON."""
        operations = tuple(
            operation_input(spec, self.__exceptions.adapter) for spec in self.__operations
        )
        schemes: dict[str, SecurityScheme] = {}
        for spec in self.__operations:
            scheme = spec.security_scheme
            if scheme is None:
                continue
            existing = schemes.get(scheme.scheme_name)
            if existing is not None and existing != scheme:
                raise WiringError(
                    f"two different OpenAPI security schemes share the name "
                    f"{scheme.scheme_name!r}; give each a distinct scheme_name",
                )
            schemes[scheme.scheme_name] = scheme
        info = Info(
            title=config.title,
            version=config.version,
            description=config.description,
            servers=config.servers,
            tags=_assemble_openapi_tags(config.tags, operations),
        )
        try:
            document = build_openapi(info, operations, schemes)
        except OpenAPINameConflictError as exc:
            # A ModelMeta(name=...) override collided with another component's name.
            raise WiringError(str(exc)) from exc
        except KeyError as exc:
            # msgspec.json.schema_components keys components by type name and raises KeyError
            # when two distinct Structs share a name in the same module.
            raise WiringError(
                f"could not generate the OpenAPI schema for {exc}; two different msgspec "
                f"Structs likely share a name in the same module — rename one",
            ) from exc
        return msgspec_encoder.encode(document)

    def __resolve_dynamic(self, method: str, path: str) -> tuple[_Handler, dict[str, str]] | None:
        # Static routes never reach here: __call__ resolves them with an inlined dict
        # lookup. The cast is paid only on this, the dynamic path.
        segments = path.split("/")
        verb = cast("HTTPMethod", method)
        for pattern in self.__dynamic.get((verb, len(segments)), ()):
            # Inlines pattern.matches (kept for the cold Allow path): a genexpr per
            # candidate is measurable here. unquote only when a segment is escaped.
            for i, value in pattern.statics:
                if segments[i] != value:
                    break
            else:
                values: dict[str, str] = {}
                for i, name in pattern.params:
                    segment = segments[i]
                    values[name] = unquote(segment) if "%" in segment else segment
                return pattern.handler, values
        return None

    def __resolve_websocket_dynamic(
        self, path: str
    ) -> tuple[_WebSocketHandler, dict[str, str]] | None:
        segments = path.split("/")
        for pattern in self.__websocket_dynamic.get(len(segments), ()):
            for index, value in pattern.statics:
                if segments[index] != value:
                    break
            else:
                values: dict[str, str] = {}
                for index, name in pattern.params:
                    segment = segments[index]
                    values[name] = unquote(segment) if "%" in segment else segment
                return pattern.handler, values
        return None

    def __allowed_methods(self, path: str) -> tuple[HTTPMethod, ...]:
        allowed = list(self.__allowed.get(path, ()))
        segments = path.split("/")
        for (method, count), bucket in self.__dynamic.items():
            if (
                count == len(segments)
                and method not in allowed
                and any(pattern.matches(segments) for pattern in bucket)
            ):
                allowed.append(method)
        return tuple(allowed)

    def __allow_for(self, path: str) -> bytes | None:
        """The Allow header for a path, or None if no route shape matches it."""
        cached = self.__allow_cache.get(path)
        if cached is not None:
            return cached
        allowed = self.__allowed_methods(path)
        return _allow_header(allowed) if allowed else None

    def __preflight_pairs(self, scope: Scope, path: str) -> list[tuple[bytes, bytes]] | None:
        """The CORS pairs for a preflight OPTIONS, or None when the request isn't one
        (no ``Access-Control-Request-Method``) or nothing answers it.

        The requested method selects *which route's* policy replies — the answer is per
        (path, requested method), so ``GET`` public / ``POST`` restricted on one path
        works. Preflights carry no credentials, so this runs before any auth would."""
        requested = requested_method(scope)
        if requested is None:
            return None
        # HEAD is served from GET routes, but the *policy* check still sees "HEAD".
        verb = "GET" if requested == "HEAD" else requested
        handler = self.__static.get((verb, path))
        if handler is None:
            resolved = self.__resolve_dynamic(verb, path)
            if resolved is None:
                return None
            handler = resolved[0]
        cors = self.__route_cors.get(handler)
        if cors is None:
            return None
        return cors.preflight_pairs(scope, requested)

    def __log_openapi_docs(self, config: _OpenAPIConfig) -> None:
        """Announce where the docs/spec are served, once, at startup.

        jero is the ASGI app, not the server, so it can't know the bound host/port — the
        URL is absolute only when ``JERO_BASE_URL`` names the public origin, otherwise the
        path is relative (the server prints its own ``Listening at`` line with the host).
        """
        base = (os.environ.get("JERO_BASE_URL") or "").rstrip("/")
        if config.docs_path is not None:
            logger.info("Serving API docs at %s%s", base, config.docs_path)
        else:
            logger.info("Serving OpenAPI spec at %s%s", base, config.openapi_path)

    def __fill_tail(
        self,
        tail: _RouteTail,
        cors: CompiledCORS | None,
        middlewares: Sequence[CompiledMiddleware],
    ) -> None:
        """Fill one response-header tail: the CORS policy first, then each middleware's
        constant pairs and ``response_headers`` method hooks (globals before scoped, in
        registration order). Duplicate *constant* names across contributors fail loud."""
        claims: dict[str, str] = {}
        if cors is not None:
            _claim_header_names(claims, "CORS", cors.constant_pairs)
            tail.pairs += cors.constant_pairs
            if cors.dynamic is not None:
                tail.dynamic += (cors.dynamic,)
        for mw in middlewares:
            if mw.constant_headers is not None:
                pairs = _constant_middleware_pairs(mw.constant_headers)
                _claim_header_names(claims, mw.owner, pairs)
                tail.pairs += pairs
            if mw.headers_hook is not None:
                tail.dynamic += (_HeadersTailHook(mw.headers_hook),)
        tail.active = bool(tail.pairs or tail.dynamic)

    def __build_global_middleware(self) -> None:
        """Build the pre-routing machinery: the verb-keyed global intercept table and
        the global observes. ``__pre`` stays None when neither exists — a middleware
        with only header tiers has no pre-routing presence at all."""
        table: dict[str, list[_InterceptRunner]] = {}
        for mw in self.__middleware:
            if mw.intercept is None:
                continue
            runner = _InterceptRunner(
                mw.intercept,
                _intercept_sender(
                    mw.owner, mw.intercept, self.__reverser, self.__exceptions, self.__app_tail
                ),
            )
            for method in mw.intercept_methods:
                table.setdefault(method, []).append(runner)
        observes = tuple(mw.observe for mw in self.__middleware if mw.observe is not None)
        if table or observes:
            self.__pre = _GlobalMiddleware(
                {method: tuple(runners) for method, runners in table.items()}, observes
            )

    @staticmethod
    def __check_scoped_intercepts(record: _IncludeRecord) -> None:
        """A scoped intercept none of the include's wire methods can ever trigger is a
        dead registration — fail loud rather than silently never running.

        A *partial* overlap stays legal: one middleware class declaring
        ``("GET", "POST")`` is reusable across a GET-only and a POST-only include, each
        picking up the verbs it serves. The exception is ``OPTIONS``, which is answered
        before routing on every path — an OPTIONS entry is dead on *any* include, so it
        is rejected outright rather than left to overlap luck."""
        served = {method for method, _ in record.routes}
        if "GET" in served:
            served.add("HEAD")
        for mw in record.middleware:
            if mw.intercept is None:
                continue
            if "OPTIONS" in mw.intercept_methods:
                raise WiringError(
                    f"{mw.owner}.intercept declares OPTIONS in intercept_methods, but "
                    f"OPTIONS never reaches include-scoped middleware — intercept it "
                    f"globally via _include_middleware",
                )
            if not served.intersection(mw.intercept_methods):
                raise WiringError(
                    f"{mw.owner}.intercept can never run on this include: it declares "
                    f"intercept_methods {mw.intercept_methods!r} but the include serves "
                    f"{', '.join(sorted(served))}",
                )

    def __swap_handler(self, old: _Handler, new: _Handler) -> None:
        """Replace one compiled route in the routing tables (wiring-time only) — how a
        covered route's ``_CoveredRoute`` wrapper takes its place, so uncovered routes
        never pay so much as a branch for middleware."""
        for key, handler in self.__static.items():
            if handler is old:
                self.__static[key] = new
        for bucket in self.__dynamic.values():
            for index, pattern in enumerate(bucket):
                if pattern.handler is old:
                    bucket[index] = _Pattern(pattern.statics, pattern.params, new)

    def __cover_route(
        self,
        record: _IncludeRecord,
        verb: HTTPMethod,
        route: _Handler,
        scoped_runners: Sequence[tuple[CompiledMiddleware, _InterceptRunner]],
        observes: tuple[ObserveHook, ...],
        *,
        headers_hooks: bool,
    ) -> _Handler:
        """Wrap one route in a ``_CoveredRoute`` when any middleware hook covers it
        (an intercept scoped to one of its wire methods, an observe, or a dynamic
        ``response_headers`` needing the ``received_at`` stamp); uncovered routes are
        returned untouched."""
        wire_methods = ("GET", "HEAD") if verb == "GET" else (verb,)
        table: dict[str, list[_InterceptRunner]] = {}
        for mw, runner in scoped_runners:
            for method in mw.intercept_methods:
                if method in wire_methods:
                    table.setdefault(method, []).append(runner)
        if not table and not observes and not headers_hooks:
            return route
        compiled = _RouteMiddleware(
            {method: tuple(runners) for method, runners in table.items()} if table else None,
            observes,
        )
        covered = _CoveredRoute(route, compiled, self.__exceptions, record.tail)
        self.__swap_handler(route, covered)
        return covered

    def __finalize_tails(self) -> None:
        """Resolve every include's response-header tail (the CORS policy it declares or
        inherits, plus middleware header tiers), the middleware coverage of each route,
        and the app-level tail + pre-routing table for unrouted responses. One pass
        after wiring, so ``_include_cors`` / ``_include_middleware`` and the include
        calls compose in any order."""
        default = self.__cors_default
        # Unrouted responses (404, 405) carry the app default and global middleware only.
        self.__fill_tail(self.__app_tail, default, self.__middleware)
        self.__build_global_middleware()
        global_observes = self.__pre.observes if self.__pre is not None else ()
        for record in self.__includes:
            self.__check_scoped_intercepts(record)
            cors = None if record.cors_off else (record.cors or default)
            middlewares = (*self.__middleware, *record.middleware)
            self.__fill_tail(record.tail, cors, middlewares)
            headers_hooks = any(mw.headers_hook is not None for mw in middlewares)
            scoped_runners = [
                (
                    mw,
                    _InterceptRunner(
                        mw.intercept,
                        _intercept_sender(
                            mw.owner, mw.intercept, self.__reverser, self.__exceptions, record.tail
                        ),
                    ),
                )
                for mw in record.middleware
                if mw.intercept is not None
            ]
            observes = global_observes + tuple(
                mw.observe for mw in record.middleware if mw.observe is not None
            )
            for verb, route in record.routes:
                handler = self.__cover_route(
                    record, verb, route, scoped_runners, observes, headers_hooks=headers_hooks
                )
                if cors is not None:
                    self.__route_cors[handler] = cors

    def __finalize(self) -> None:
        """Precompute Allow headers, resolve response-header tails, and build the OpenAPI
        document; runs once after wiring."""
        self.__finalize_tails()
        self.__allow_cache = {
            path: _allow_header(self.__allowed_methods(path)) for path in self.__allowed
        }
        if self.__openapi is not None:
            self.__openapi.payload = self.__build_openapi_document(self.__openapi)
            self.__log_openapi_docs(self.__openapi)

    async def __close_resources(self) -> None:
        await self.__astack.aclose()
        self.__stack.close()

    async def __handle_lifespan(self, receive: Receive, send: Send) -> None:
        await receive()  # lifespan.startup
        try:
            await self.wire()
            self.__finalize()  # builds the OpenAPI doc; can raise WiringError (e.g. tag conflict)
        except BaseException as exc:
            await self.__close_resources()  # release anything entered before the failure
            await send(
                {
                    "type": "lifespan.startup.failed",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
            raise
        await send({"type": "lifespan.startup.complete"})

        await receive()  # lifespan.shutdown
        try:
            await self.__close_resources()
        except BaseException as exc:
            await send(
                {
                    "type": "lifespan.shutdown.failed",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
            raise
        await send({"type": "lifespan.shutdown.complete"})

    async def __intercept_global(
        self,
        pre: _GlobalMiddleware,
        runners: tuple[_InterceptRunner, ...],
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> bool:
        """Run one verb's global intercepts, pre-routing (they can answer requests no
        route serves — preflights for paths that would 404). True when one answered:
        the response (or its error) has left and the global observes have seen it;
        False falls through to routing untouched."""
        received_at = perf_counter()
        scope["jero.received_at"] = received_at
        if scope["method"] == "HEAD":
            send = _SuppressBody(send)
        capture: _StatusCapture | None = None
        if pre.observes:
            send = capture = _StatusCapture(send)
        answered = await _run_intercepts(
            runners, scope, receive, send, exceptions=self.__exceptions, tail=self.__app_tail
        )
        if answered and capture is not None:
            duration = capture.started_at - received_at if capture.started_at else 0.0
            for observe in pre.observes:
                await observe(scope, capture.status, duration)
        return answered

    async def __fallthrough(self, scope: Scope, send: Send, method: str, path: str) -> None:
        """Answer a request no route serves. No route owns these responses, so the 404
        and 405 problems carry the app-level tail (the CORS default and global
        middleware headers); the OPTIONS answer adds the CORS preflight block when the
        requested method's route has a policy."""
        allow = self.__allow_for(path)
        error: HTTPError
        if allow is None:
            error = NotFoundError()
            await _send_json(
                send,
                error.status,
                self.__exceptions.encode_error(error),
                self.__app_tail.contained_extra(scope),
            )
        elif method == "OPTIONS":
            headers = [(b"allow", allow)]
            if self.__route_cors:
                preflight = self.__preflight_pairs(scope, path)
                if preflight is not None:
                    headers += preflight
            await send({"type": "http.response.start", "status": 204, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
        else:
            error = MethodNotAllowedError()
            extra = self.__app_tail.contained_extra(scope)
            await _send_json(
                send,
                error.status,
                self.__exceptions.encode_error(error),
                [(b"allow", allow)] + (extra if extra is not None else []),
            )

    async def __handle_websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        connect = await receive()
        if connect["type"] != "websocket.connect":
            await send({"type": "websocket.close", "code": 1008, "reason": "bad handshake"})
            return
        # WebSocket handshakes are GET requests for middleware verb scoping. Global
        # intercepts run before routing/auth exactly as on the HTTP path; observe is
        # deliberately absent for sockets in v1.
        scope["method"] = "GET"
        pre = self.__pre
        if pre is not None:
            runners = pre.intercepts.get("GET")
            if runners is not None and await _run_intercepts(
                runners,
                scope,
                receive,
                _WebSocketDenialSend(send, _supports_websocket_denial(scope)),
                exceptions=self.__exceptions,
                tail=self.__app_tail,
            ):
                return
        path: str = scope["path"]
        handler = self.__websocket_static.get(path)
        path_values: dict[str, str] = {}
        if handler is None:
            resolved = self.__resolve_websocket_dynamic(path)
            if resolved is not None:
                handler, path_values = resolved
        if handler is not None:
            await handler(scope, receive, send, path_values)
            return
        error = NotFoundError()
        payload = self.__exceptions.encode_error(error)
        await _send_websocket_rejection(scope, send, error.status, payload)

    async def __handle_non_http(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.__handle_lifespan(receive, send)
            return
        if scope["type"] == "websocket":
            await self.__handle_websocket(scope, receive, send)
            return
        raise RuntimeError(f"unsupported scope type {scope['type']!r}")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.__handle_non_http(scope, receive, send)
            return

        # HTTP is the hot path; inlined here (was _handle_http) to save a coroutine hop.
        method: str = scope["method"]
        path: str = scope["path"]
        # Global intercepts run pre-routing; the table is verb-keyed, so an off-scope
        # method costs one dict hit and apps without them one attribute load.
        pre = self.__pre
        if pre is not None:
            runners = pre.intercepts.get(method)
            if runners is not None and await self.__intercept_global(
                pre, runners, scope, receive, send
            ):
                return
        verb = "GET" if method == "HEAD" else method
        # A static hit is the hottest path of all: one dict lookup, inlined here to skip
        # the resolver call (a non-route verb simply misses).
        handler = self.__static.get((verb, path))
        path_values: dict[str, str] = {}
        if handler is None:
            resolved = self.__resolve_dynamic(verb, path)
            if resolved is not None:
                handler, path_values = resolved
        if handler is not None:
            await handler(
                scope, receive, _SuppressBody(send) if method == "HEAD" else send, path_values
            )
            return
        if pre is None or not pre.observes:
            await self.__fallthrough(scope, send, method, path)
            return
        # Global observes see fallthrough answers too — capture the outcome around it.
        received_at = perf_counter()
        scope["jero.received_at"] = received_at
        capture = _StatusCapture(send)
        await self.__fallthrough(scope, capture, method, path)
        duration = capture.started_at - received_at if capture.started_at else 0.0
        for observe in pre.observes:
            await observe(scope, capture.status, duration)
