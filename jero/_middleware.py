"""Middleware contracts, compilation, and the built-in CORS policy.

Middleware in jero is never an app wrapper: an onion of async callables costs a coroutine
plus allocations per layer per request, whether or not the layer applies. Instead a
middleware is a typed, hand-wired object whose hooks are introspected and compiled at
wiring (the exception-handler pattern): each hook kind has a fixed, known cost, and only
what a middleware actually defines is compiled into the routes it covers.

This module is the sender-free half — the public :class:`Request` view and :class:`CORS`
policy, plus the wiring-time compilation (:class:`CompiledMiddleware`,
:class:`CompiledCORS`, the header scanners). The dispatch half — the hook runners that
send an intercept's response, the header-tail seam in the senders, the observe capture —
lives in :mod:`jero.core` alongside the response senders it depends on (mirroring the
:mod:`jero._exception_handlers` split).
"""

import inspect
import logging
from collections.abc import Callable, Sequence
from types import NoneType
from typing import Any, ClassVar, Literal, cast, get_args, get_origin, get_type_hints
from urllib.parse import urlsplit

from msgspec import Struct, ValidationError, convert
from msgspec.structs import fields as struct_fields

from jero._wiring_types import WiringError
from jero.errors import ErrorReason, MalformedRequestError

logger = logging.getLogger("jero")

# HTTP methods the framework speaks. GET/POST/PUT/PATCH/DELETE are handler-declarable
# (the METHODS tables); HEAD/OPTIONS exist on the wire (HEAD is served from GET routes,
# OPTIONS answers preflights) and are addressable by middleware scope declarations.
type HTTPMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# The runtime value set of HTTPMethod, for validating user-supplied method tuples.
_HTTP_METHODS: frozenset[str] = frozenset(get_args(HTTPMethod.__value__))

# Response headers the senders own — middleware may never emit them: the value is
# derived from the body/kind at send time, and a middleware pair would corrupt framing.
_SENDER_OWNED_HEADERS: frozenset[str] = frozenset({"content-length", "content-type"})


class NoHeaders(Struct, frozen=True):
    """The empty headers Struct — the default ``H`` of :class:`Request`, for hooks
    that bind no request headers (``request: Request`` is ``Request[NoHeaders]``)."""


class Request[H: Struct = NoHeaders](Struct, frozen=True):
    """The read-only, typed request view a middleware hook receives.

    ``headers`` is the *hook's own* Struct: annotate ``request: Request[MyHeaders]``
    and wiring compiles a scanner that binds exactly the header keys the Struct's
    fields name (``origin`` -> ``origin``, ``x_trace_id`` -> ``x-trace-id``), exactly
    like auth's ``headers`` parameter. Give a field a ``| None = None`` default when
    the header may be absent; a missing required header is a 400.

    ``method`` is the wire method — a HEAD request reads ``"HEAD"`` even though
    routing serves it from a GET handler. ``received_at`` is ``time.perf_counter()``
    at dispatch, stamped only on routes a dynamic hook covers (``0.0`` otherwise).
    """

    method: HTTPMethod
    path: str
    headers: H
    received_at: float


class HeaderScanner:
    """Binds one hook's headers Struct from the raw ASGI pairs.

    Compiled once at wiring: the scan looks only for the wire keys the Struct's
    fields name, so a hook binding one header pays one pass over the pairs and one
    ``convert`` — never the full-header mangle the request binder does.
    """

    __slots__ = ("_empty", "_keys", "_struct_type")

    def __init__(self, struct_type: type[Struct]) -> None:
        self._struct_type = struct_type
        # ASGI header names arrive lowercased; the convert key is the field's
        # encode_name so msgspec renames keep working.
        self._keys = {
            field.encode_name.replace("_", "-").lower().encode("latin-1"): field.encode_name
            for field in struct_fields(struct_type)
        }
        # A field-less Struct (a hook annotated with bare ``Request``) binds nothing:
        # share one instance and skip the scan and convert entirely.
        self._empty = struct_type() if not self._keys else None

    def __call__(self, scope: dict[str, Any]) -> Struct:
        if self._empty is not None:
            return self._empty
        keys = self._keys
        values: dict[str, str] = {}
        for key, value in scope["headers"]:
            name = keys.get(key)
            if name is not None:
                values[name] = value.decode("latin-1")
        try:
            return convert(values, self._struct_type, strict=False)
        except ValidationError as e:
            raise MalformedRequestError(ErrorReason(reason=str(e))) from e


def request_origin(scope: dict[str, Any]) -> str | None:
    """The request's ``Origin`` header, or ``None`` when the caller sent none
    (a same-origin or non-browser request — not a CORS request at all)."""
    for key, value in scope["headers"]:
        if key == b"origin":
            return value.decode("latin-1")
    return None


def requested_method(scope: dict[str, Any]) -> str | None:
    """The ``Access-Control-Request-Method`` header of a CORS preflight, or ``None``
    when the OPTIONS request is not a preflight."""
    for key, value in scope["headers"]:
        if key == b"access-control-request-method":
            return value.decode("latin-1")
    return None


class CORS(Struct, frozen=True):
    """One cross-origin resource sharing policy.

    Register an app-wide default with ``_include_cors(CORS(...))``; override it per
    include with the ``cors=`` keyword on ``_include_resource`` / ``_include_endpoint``,
    or opt a route out with ``cors=CORS.OFF``. An include that passes nothing inherits
    the app default; an app that never calls ``_include_cors`` serves no CORS headers
    at all (pure opt-in).

    ``allow_origins="*"`` compiles to constant header pairs in every covered route's
    response — free at request time. An explicit origin tuple compiles to an origin
    echo whose verdict is memoized per origin (and a ``Vary: Origin`` pair) instead.
    Combining an
    origin allow-list with ``allow_credentials=True`` echoes the origin with
    ``Access-Control-Allow-Credentials: true``; combining credentials with ``"*"`` is
    spec-forbidden and fails wiring loud.
    """

    allow_origins: tuple[str, ...] | Literal["*"] = "*"
    allow_methods: tuple[HTTPMethod, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE")
    allow_headers: tuple[str, ...] = ("content-type", "authorization")
    allow_credentials: bool = False
    max_age: int = 600

    # The per-include opt-out sentinel: ``cors=CORS.OFF`` removes an app-wide default
    # from that include's routes. Compared by identity, so it never collides with a
    # user's own all-default ``CORS()``.
    OFF: ClassVar["CORS"]


CORS.OFF = CORS()


def _validate_origin(origin: str) -> None:
    parts = urlsplit(origin)
    well_formed = bool(
        parts.scheme and parts.netloc and not parts.path and not parts.query and not parts.fragment
    )
    if not well_formed or origin != f"{parts.scheme}://{parts.netloc}":
        raise WiringError(
            f"CORS allow_origins entry {origin!r} is not an origin; expected "
            f"scheme://host[:port] with no path or trailing slash",
        )


class _OriginEcho:
    """The allow-list simple-response hook: echo the request origin when it is allowed
    (plus the credentials pair when the policy grants them), else contribute nothing.

    The verdict pairs are memoized per raw wire origin (origins repeat heavily across a
    client's requests), so the steady state is one header scan plus one dict hit — no
    decode, no ``.lower()``, no tuple build. The cache is capped so a flood of hostile
    distinct origins cannot grow it unbounded; overflow traffic just takes the uncached
    path, which is the pre-memo cost."""

    _CACHE_CAP = 128

    __slots__ = ("_cache", "_extra", "_origins")

    def __init__(self, origins: frozenset[str], *, credentials: bool) -> None:
        self._origins = origins
        self._extra: tuple[tuple[bytes, bytes], ...] = (
            ((b"access-control-allow-credentials", b"true"),) if credentials else ()
        )
        self._cache: dict[bytes, tuple[tuple[bytes, bytes], ...]] = {}

    def __call__(self, scope: dict[str, Any]) -> Sequence[tuple[bytes, bytes]]:
        for key, value in scope["headers"]:
            if key == b"origin":
                pairs = self._cache.get(value)
                if pairs is None:
                    pairs = (
                        ((b"access-control-allow-origin", value), *self._extra)
                        if value.decode("latin-1").lower() in self._origins
                        else ()
                    )
                    if len(self._cache) < self._CACHE_CAP:
                        self._cache[value] = pairs
                return pairs
        return ()


def _validate_cors(config: CORS, *, wildcard: bool) -> None:
    """Fail loud at wiring on a policy the CORS spec forbids or a malformed field."""
    if wildcard and config.allow_credentials:
        raise WiringError(
            "CORS allow_credentials=True cannot be combined with allow_origins='*' "
            "(spec-forbidden); list the origins that may send credentials",
        )
    if not config.allow_methods:
        raise WiringError("CORS allow_methods must not be empty")
    for method in config.allow_methods:
        if method not in _HTTP_METHODS:
            raise WiringError(
                f"CORS allow_methods entry {method!r} is not an HTTP method; "
                f"expected one of {', '.join(sorted(_HTTP_METHODS))}",
            )
    if not wildcard:
        for origin in cast("tuple[str, ...]", config.allow_origins):
            _validate_origin(origin)
    if isinstance(config.max_age, bool) or config.max_age < 0:
        raise WiringError("CORS max_age must be a non-negative number of seconds")


class CompiledCORS:
    """A :class:`CORS` policy validated once at wiring and precomputed into header pairs.

    ``constant_pairs`` joins a covered route's constant header tail (the free tier:
    ``access-control-allow-origin: *`` for a wildcard policy, ``Vary: Origin`` for an
    allow-list). ``dynamic`` is the allow-list origin-echo hook (verdicts memoized per
    origin), or ``None`` for a wildcard policy. :meth:`preflight_pairs` answers one
    preflight from the precomputed block.
    """

    __slots__ = ("_allowed_methods", "_origins", "_preflight_block", "constant_pairs", "dynamic")

    _allowed_methods: frozenset[str]
    _origins: frozenset[str]
    _preflight_block: tuple[tuple[bytes, bytes], ...]
    constant_pairs: tuple[tuple[bytes, bytes], ...]
    dynamic: _OriginEcho | None

    def __init__(self, config: CORS) -> None:
        wildcard = config.allow_origins == "*"
        _validate_cors(config, wildcard=wildcard)
        self._allowed_methods = frozenset(config.allow_methods)
        self._origins = (
            frozenset()
            if wildcard
            else frozenset(o.lower() for o in cast("tuple[str, ...]", config.allow_origins))
        )
        methods_value = ", ".join(config.allow_methods).encode("latin-1")
        block: list[tuple[bytes, bytes]] = [
            (b"access-control-allow-methods", methods_value),
        ]
        if config.allow_headers:
            headers_value = ", ".join(h.lower() for h in config.allow_headers).encode("latin-1")
            block.append((b"access-control-allow-headers", headers_value))
        block.append((b"access-control-max-age", str(config.max_age).encode("latin-1")))
        if wildcard:
            self.constant_pairs = ((b"access-control-allow-origin", b"*"),)
            self.dynamic = None
            self._preflight_block = ((b"access-control-allow-origin", b"*"), *block)
        else:
            self.constant_pairs = ((b"vary", b"Origin"),)
            self.dynamic = _OriginEcho(self._origins, credentials=config.allow_credentials)
            if config.allow_credentials:
                block.append((b"access-control-allow-credentials", b"true"))
            block.append((b"vary", b"Origin"))
            self._preflight_block = tuple(block)

    def preflight_pairs(
        self, scope: dict[str, Any], requested: str
    ) -> list[tuple[bytes, bytes]] | None:
        """The CORS half of a preflight answer for one covered route, or ``None`` when
        this policy does not allow the requested method or origin (the plain 204 with
        ``Allow`` still goes out; the browser blocks the cross-origin call).

        A requested ``HEAD`` rides ``GET`` here, exactly as routing auto-serves HEAD
        from GET handlers — a policy allowing GET would otherwise deny the preflight
        for a HEAD the route happily answers. An explicit ``HEAD`` entry also works."""
        if requested not in self._allowed_methods and not (
            requested == "HEAD" and "GET" in self._allowed_methods
        ):
            return None
        if self.dynamic is None:
            return list(self._preflight_block)
        origin = request_origin(scope)
        if origin is None or origin.lower() not in self._origins:
            return None
        return [
            (b"access-control-allow-origin", origin.encode("latin-1")),
            *self._preflight_block,
        ]


def build_request(scope: dict[str, Any], scanner: HeaderScanner) -> Request[Any]:
    """The :class:`Request` a hook receives. ``received_at`` reads the dispatch stamp a
    covered route wrote into the scope (``0.0`` when the route wasn't stamped)."""
    return Request(
        method=scope["method"],
        path=scope["path"],
        headers=scanner(scope),
        received_at=scope.get("jero.received_at", 0.0),
    )


def _request_headers_type(owner: str, hook: str, ann: object) -> type[Struct]:
    """The ``H`` of a hook's ``request: Request[H]`` annotation (``NoHeaders`` for a bare
    ``Request``); anything else fails wiring loud."""
    if ann is Request:
        return NoHeaders
    if get_origin(ann) is Request:
        args = get_args(ann)
        if len(args) == 1 and isinstance(args[0], type) and issubclass(args[0], Struct):
            return args[0]
    raise WiringError(
        f"{owner}.{hook}: 'request' must be annotated Request[YourHeaders] "
        f"(or a bare Request to bind no headers), got {ann!r}",
    )


def _hook_request_param(owner: str, hook: str, fn: Callable[..., object]) -> HeaderScanner:
    """Validate a hook's leading ``request`` parameter and compile its header scanner."""
    params = list(inspect.signature(fn).parameters.values())
    if not params or params[0].name != "request":
        raise WiringError(
            f"{owner}.{hook} must take 'request' as its first argument",
        )
    hints = get_type_hints(fn)
    return HeaderScanner(_request_headers_type(owner, hook, hints.get("request")))


def _check_header_fields(owner: str, hook: str, struct_type: type[Struct]) -> None:
    """Reject header fields the senders own: their wire values are derived from the
    body at send time, and a middleware pair would corrupt response framing."""
    for field in struct_fields(struct_type):
        wire = field.name.replace("_", "-").lower()
        if wire in _SENDER_OWNED_HEADERS:
            raise WiringError(
                f"{owner}.{hook}: header field {field.name!r} maps to {wire!r}, which the "
                f"response senders own — middleware cannot emit it",
            )


def _strip_optional_struct(ann: object) -> tuple[type[Struct] | None, bool]:
    """Resolve ``SomeStruct`` / ``SomeStruct | None`` to (the Struct, had-None);
    ``(None, False)`` for anything else."""
    if isinstance(ann, type) and issubclass(ann, Struct):
        return ann, False
    args = get_args(ann)
    if len(args) == 2 and NoneType in args:
        payload = next(arg for arg in args if arg is not NoneType)
        if isinstance(payload, type) and issubclass(payload, Struct):
            return payload, True
    return None, False


class HeadersHook:
    """A compiled ``response_headers`` *method*: one header scan, one call, and the
    returned Struct merges onto whatever response leaves (``None`` adds nothing).

    Sync by contract — it runs inline while a sender assembles the response headers,
    where an await would put a coroutine on every covered response."""

    __slots__ = ("_fn", "_scanner", "owner", "returns")

    def __init__(self, owner: str, fn: Callable[..., object]) -> None:
        self.owner = owner
        if inspect.iscoroutinefunction(fn):
            raise WiringError(
                f"{owner}.response_headers must be sync — it runs inline while response "
                f"headers are assembled; move async work into intercept or observe",
            )
        self._scanner = _hook_request_param(owner, "response_headers", fn)
        hints = get_type_hints(fn)
        returns, _ = _strip_optional_struct(hints.get("return"))
        if returns is None:
            raise WiringError(
                f"{owner}.response_headers must declare a Struct return type "
                f"(optionally '| None' to sometimes add nothing)",
            )
        _check_header_fields(owner, "response_headers", returns)
        self.returns: type[Struct] = returns
        self._fn = fn

    def __call__(self, scope: dict[str, Any]) -> Struct | None:
        result = self._fn(build_request(scope, self._scanner))
        return cast("Struct | None", result)


class InterceptHook:
    """A compiled ``intercept``: scan the declared headers, build the request view, and
    call the hook (sync or async). ``None`` falls through to routing; a response answers
    the request. The response *sender* is attached in :mod:`jero.core`."""

    __slots__ = ("_fn", "_is_async", "_scanner", "owner", "return_annotation")

    def __init__(self, owner: str, fn: Callable[..., object]) -> None:
        self.owner = owner
        self._scanner = _hook_request_param(owner, "intercept", fn)
        hints = get_type_hints(fn)
        self.return_annotation: object = hints.get("return")
        if self.return_annotation is None:
            raise WiringError(
                f"{owner}.intercept must declare its response return type "
                f"(with '| None' when it can fall through to routing)",
            )
        self._fn = fn
        self._is_async = inspect.iscoroutinefunction(fn)

    async def __call__(self, scope: dict[str, Any]) -> object | None:
        result = self._fn(build_request(scope, self._scanner))
        if self._is_async:
            return await cast("Any", result)
        return result


class ObserveHook:
    """A compiled ``observe``: sees the outcome (status, duration), cannot touch it.

    Runs after the response starts; sync or async. Its exceptions are logged and
    swallowed here — observability must never break the response that already left."""

    __slots__ = ("_fn", "_is_async", "_scanner", "owner")

    def __init__(self, owner: str, fn: Callable[..., object]) -> None:
        self.owner = owner
        params = [p.name for p in inspect.signature(fn).parameters.values()]
        if params != ["request", "status", "duration"]:
            raise WiringError(
                f"{owner}.observe must take exactly (request, status, duration), "
                f"got ({', '.join(params)})",
            )
        hints = get_type_hints(fn)
        self._scanner = HeaderScanner(_request_headers_type(owner, "observe", hints.get("request")))
        if hints.get("status") is not int or hints.get("duration") is not float:
            raise WiringError(
                f"{owner}.observe must annotate 'status: int' and 'duration: float'",
            )
        self._fn = fn
        self._is_async = inspect.iscoroutinefunction(fn)

    async def __call__(self, scope: dict[str, Any], status: int, duration: float) -> None:
        try:
            result = self._fn(build_request(scope, self._scanner), status, duration)
            if self._is_async:
                await cast("Any", result)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception(
                "observe hook %s failed for %s %s; ignored",
                self.owner,
                scope["method"],
                scope["path"],
            )


def _validate_intercept_methods(owner: str, declared: object) -> tuple[HTTPMethod, ...]:
    """Validate an ``intercept_methods`` declaration: a non-empty tuple of distinct
    wire methods. HEAD counts as its own entry (routing serves it from GET handlers,
    but interception scopes on what's on the wire)."""
    if not isinstance(declared, tuple) or not declared:
        raise WiringError(
            f"{owner}.intercept_methods must be a non-empty tuple of HTTP methods",
        )
    methods = cast("tuple[object, ...]", declared)
    seen: set[str] = set()
    for method in methods:
        if not isinstance(method, str) or method not in _HTTP_METHODS:
            raise WiringError(
                f"{owner}.intercept_methods entry {method!r} is not an HTTP method; "
                f"expected one of {', '.join(sorted(_HTTP_METHODS))}",
            )
        if method in seen:
            raise WiringError(f"{owner}.intercept_methods lists {method!r} twice")
        seen.add(method)
    return cast("tuple[HTTPMethod, ...]", declared)


def _compile_intercept(
    owner: str, middleware: object
) -> tuple[InterceptHook | None, tuple[HTTPMethod, ...]]:
    """Compile the intercept hook: the method and its scope declaration come and go
    together — one without the other is a dead registration and fails loud."""
    fn = getattr(middleware, "intercept", None)
    declared = getattr(middleware, "intercept_methods", None)
    if fn is not None and declared is None:
        raise WiringError(
            f"{owner} defines intercept but no intercept_methods — declare the wire "
            f"methods it runs on, e.g. intercept_methods = ('OPTIONS',)",
        )
    if declared is not None and fn is None:
        raise WiringError(f"{owner} declares intercept_methods but defines no intercept")
    if fn is None:
        return None, ()
    if not callable(fn):
        raise WiringError(f"{owner}.intercept must be a method")
    return InterceptHook(owner, fn), _validate_intercept_methods(owner, declared)


class CompiledMiddleware:
    """A middleware object introspected and validated once at wiring time.

    The protocol is structural — no base class, no decorator. An object registered via
    ``_include_middleware`` (global) or the ``middleware=`` include keyword (scoped) may
    define any of:

    - ``response_headers`` as a Struct *instance* (usually a ``ClassVar``): the constant
      tier — its pairs are baked into covered routes' header blocks at wiring, costing
      nothing per request;
    - ``response_headers`` as a *method* ``(request: Request[H]) -> HeadersStruct | None``:
      merged onto every covered response as it leaves (sync only);
    - ``intercept(request: Request[H]) -> SomeResponse | None`` plus a class-level
      ``intercept_methods`` tuple naming the wire methods it runs on: answer instead of
      routing (``None`` falls through); sync or async;
    - ``observe(request: Request[H], status: int, duration: float) -> None``: watch the
      outcome after the response starts; sync or async, exceptions logged and swallowed.

    Anything else on the object is ignored; an object defining none of these hooks is a
    :class:`WiringError`. Only what a middleware actually defines is compiled into the
    routes it covers — an absent hook has no request-time existence.
    """

    __slots__ = (
        "constant_headers",
        "headers_hook",
        "intercept",
        "intercept_methods",
        "observe",
        "owner",
    )

    owner: str
    constant_headers: Struct | None
    headers_hook: HeadersHook | None
    intercept: InterceptHook | None
    intercept_methods: tuple[HTTPMethod, ...]
    observe: ObserveHook | None

    def __init__(self, middleware: object) -> None:
        self.owner = type(middleware).__name__
        self.constant_headers = None
        self.headers_hook = None
        self.observe = None

        declared_headers = getattr(middleware, "response_headers", None)
        if isinstance(declared_headers, Struct):
            _check_header_fields(self.owner, "response_headers", type(declared_headers))
            self.constant_headers = declared_headers
        elif callable(declared_headers):
            self.headers_hook = HeadersHook(self.owner, declared_headers)
        elif declared_headers is not None:
            raise WiringError(
                f"{self.owner}.response_headers must be a Struct instance (constant "
                f"pairs) or a method, got {type(declared_headers).__name__}",
            )

        self.intercept, self.intercept_methods = _compile_intercept(self.owner, middleware)

        observe_fn = getattr(middleware, "observe", None)
        if observe_fn is not None:
            if not callable(observe_fn):
                raise WiringError(f"{self.owner}.observe must be a method")
            self.observe = ObserveHook(self.owner, observe_fn)

        if (
            self.constant_headers is None
            and self.headers_hook is None
            and self.intercept is None
            and self.observe is None
        ):
            raise WiringError(
                f"{self.owner} defines no middleware hooks; define response_headers "
                f"(a Struct attribute or a method), intercept (+ intercept_methods), "
                f"or observe",
            )
