"""Middleware: the compiled hook tiers — constant/dynamic response headers, verb-scoped
intercepts (global and include-scoped), and observe.

Focused local apps throughout; ``_MiddlewareApp`` wires ``/ping`` with whatever global
and scoped middleware a test composes. Wiring failures surface as ``RuntimeError`` — the
TestClient drives the lifespan on construction.
"""

from typing import ClassVar

import pytest

from jero import (
    CORS,
    BaseApp,
    Endpoint,
    HTTPMethod,
    JSONResponse,
    NoContent,
    NotFoundError,
    Request,
    Struct,
)
from jero.testing import TestClient


class Pong(Struct):
    """Minimal response payload."""

    ok: bool


class Refusal(Struct):
    """The body an intercept answers with."""

    reason: str


class PingEndpoint(Endpoint, path="/ping"):
    """GET + POST target routes for the middleware under test."""

    def get(self) -> Pong:
        """Answer the ping."""
        return Pong(ok=True)

    def post(self) -> Pong:
        """Answer the ping."""
        return Pong(ok=True)


class MissingEndpoint(Endpoint, path="/missing"):
    """Always raises, so error responses can be inspected for middleware headers."""

    def get(self) -> Pong:
        """Raise the framework 404."""
        raise NotFoundError()


class SecurityHeaders(Struct):
    """A constant response-header block."""

    x_frame_options: str = "DENY"
    x_content_type_options: str = "nosniff"


class SecurityMiddleware:
    """The constant tier: a Struct instance baked into covered routes at wiring."""

    response_headers: ClassVar[SecurityHeaders] = SecurityHeaders()


class CallerHeaders(Struct):
    """The one request header the dynamic hook binds."""

    x_caller: str | None = None


class EchoHeaders(Struct):
    """The dynamic hook's response headers."""

    x_echo: str


class EchoMiddleware:
    """The dynamic tier: a response_headers *method*, one scan + call per response."""

    def response_headers(self, request: Request[CallerHeaders]) -> EchoHeaders | None:
        """Echo the caller header back, or add nothing."""
        if request.headers.x_caller is None:
            return None
        return EchoHeaders(x_echo=request.headers.x_caller)


class GateHeaders(Struct):
    """The header the gate intercept reads."""

    x_gate: str | None = None


class GateMiddleware:
    """A GET-scoped intercept: answers 403 when told to, else falls through."""

    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("GET",)

    def intercept(self, request: Request[GateHeaders]) -> JSONResponse[Refusal] | None:
        """Refuse gated callers."""
        if request.headers.x_gate == "block":
            return JSONResponse(json=Refusal(reason="blocked"), status_code=403)
        return None


class AsyncNoContentMiddleware:
    """An async intercept returning a fixed-status wrapper."""

    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("POST",)

    async def intercept(self, request: Request) -> NoContent | None:
        """Answer every POST with a 204."""
        _ = request
        return NoContent()


class HeadOnlyMiddleware:
    """Interception scopes on the wire method: HEAD is its own entry."""

    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("HEAD",)

    def intercept(self, request: Request) -> Refusal | None:
        """Answer HEAD requests only."""
        _ = request
        return Refusal(reason="head")


class FailingInterceptMiddleware:
    """An intercept that raises — it must enter the handler-exception funnel."""

    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("GET",)

    def intercept(self, request: Request) -> Refusal | None:
        """Always blow up."""
        _ = request
        raise ValueError("intercept boom")


class RecordingMiddleware:
    """An intercept that records it ran and falls through — for ordering tests."""

    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("GET",)

    def __init__(self, label: str, calls: list[str]) -> None:
        self._label = label
        self._calls = calls
        self._answer: Refusal | None = None

    def intercept(self, request: Request) -> Refusal | None:
        """Record, then fall through (this middleware never answers)."""
        _ = request
        self._calls.append(self._label)
        return self._answer


class Observer:
    """Records every outcome observe sees."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, int, float, float]] = []

    def observe(self, request: Request, status: int, duration: float) -> None:
        """Record (path, status, duration, received_at)."""
        self.seen.append((request.path, status, duration, request.received_at))


class AsyncObserver:
    """The async flavor of :class:`Observer`."""

    def __init__(self) -> None:
        self.seen: list[int] = []

    async def observe(self, request: Request, status: int, duration: float) -> None:
        """Record the status."""
        _ = (request, duration)
        self.seen.append(status)


class FailingObserver:
    """An observe that raises — it must be logged and swallowed."""

    def observe(self, request: Request, status: int, duration: float) -> None:
        """Always blow up."""
        _ = (request, status, duration)
        raise ValueError("observe boom")


class _MiddlewareApp(BaseApp):
    """Wires /ping (and /missing) with the given global and scoped middleware."""

    def __init__(
        self,
        *,
        global_middleware: tuple[object, ...] = (),
        scoped: tuple[object, ...] = (),
        cors: CORS | None = None,
    ) -> None:
        self._global_middleware = global_middleware
        self._scoped = scoped
        self._cors = cors
        super().__init__()

    async def wire(self) -> None:
        """Register the composed middleware and the target endpoints."""
        if self._cors is not None:
            self._include_cors(self._cors)
        for middleware in self._global_middleware:
            self._include_middleware(middleware)
        self._include_endpoint(PingEndpoint(), middleware=self._scoped)
        self._include_endpoint(MissingEndpoint())


# --- response_headers: the constant tier ---


def test_constant_headers_on_covered_responses() -> None:
    """A constant response_headers Struct is baked into every covered response."""
    with TestClient(_MiddlewareApp(global_middleware=(SecurityMiddleware(),))) as client:
        response = client.get("/ping")

    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_constant_headers_on_error_responses() -> None:
    """Headers merge onto WHATEVER response leaves — problem bodies included."""
    with TestClient(_MiddlewareApp(global_middleware=(SecurityMiddleware(),))) as client:
        response = client.get("/missing")

    assert response.status_code == 404
    assert response.headers["x-frame-options"] == "DENY"


def test_global_constant_headers_on_unrouted_404() -> None:
    """Global middleware covers the app-level fallthrough responses too."""
    with TestClient(_MiddlewareApp(global_middleware=(SecurityMiddleware(),))) as client:
        response = client.get("/nowhere")

    assert response.status_code == 404
    assert response.headers["x-frame-options"] == "DENY"


def test_scoped_constant_headers_cover_only_their_include() -> None:
    """Include-scoped middleware never leaks onto other includes."""
    with TestClient(_MiddlewareApp(scoped=(SecurityMiddleware(),))) as client:
        covered = client.get("/ping")
        other = client.get("/missing")

    assert covered.headers["x-frame-options"] == "DENY"
    assert "x-frame-options" not in other.headers


# --- response_headers: the method tier ---


def test_headers_method_binds_request_headers() -> None:
    """The hook's Request[H] binds exactly the headers its Struct names."""
    with TestClient(_MiddlewareApp(global_middleware=(EchoMiddleware(),))) as client:
        echoed = client.get("/ping", headers={"x-caller": "caller"})
        silent = client.get("/ping")

    assert echoed.headers["x-echo"] == "caller"
    assert "x-echo" not in silent.headers


def test_headers_method_covers_errors() -> None:
    """The method tier decorates problem bodies too."""
    with TestClient(_MiddlewareApp(global_middleware=(EchoMiddleware(),))) as client:
        response = client.get("/missing", headers={"x-caller": "caller"})

    assert response.status_code == 404
    assert response.headers["x-echo"] == "caller"


# --- intercept ---


def test_intercept_answers_and_falls_through() -> None:
    """A non-None return answers the request; None falls through to the handler."""
    with TestClient(_MiddlewareApp(scoped=(GateMiddleware(),))) as client:
        blocked = client.get("/ping", headers={"x-gate": "block"})
        passed = client.get("/ping")

    assert blocked.status_code == 403
    assert blocked.json() == {"reason": "blocked"}
    assert passed.status_code == 200
    assert passed.json() == {"ok": True}


def test_intercept_scopes_to_declared_verbs() -> None:
    """A GET-scoped intercept never sees a POST."""
    with TestClient(_MiddlewareApp(scoped=(GateMiddleware(),))) as client:
        response = client.post("/ping", json={}, headers={"x-gate": "block"})

    assert response.status_code == 200


def test_async_intercept_with_fixed_status_wrapper() -> None:
    """Async intercepts compile like async handlers; NoContent fixes its 204."""
    with TestClient(_MiddlewareApp(scoped=(AsyncNoContentMiddleware(),))) as client:
        response = client.post("/ping", json={})

    assert response.status_code == 204
    assert response.content == b""


def test_intercept_sees_the_wire_method() -> None:
    """HEAD is its own interception entry even though routing serves it from GET —
    and HEAD semantics still suppress the intercepted body."""
    with TestClient(_MiddlewareApp(scoped=(HeadOnlyMiddleware(),))) as client:
        head = client.head("/ping")
        get = client.get("/ping")

    assert head.status_code == 200
    assert head.content == b""  # intercepted, body suppressed per HEAD
    assert get.json() == {"ok": True}  # GET is out of scope, handler runs


def test_global_intercept_runs_pre_routing() -> None:
    """A global intercept can answer a path no route serves."""
    with TestClient(_MiddlewareApp(global_middleware=(GateMiddleware(),))) as client:
        response = client.get("/nowhere", headers={"x-gate": "block"})

    assert response.status_code == 403


def test_intercept_order_globals_first_then_registration() -> None:
    """Globals run before scoped middleware, each in registration order."""
    calls: list[str] = []
    app = _MiddlewareApp(
        global_middleware=(
            RecordingMiddleware("global-one", calls),
            RecordingMiddleware("global-two", calls),
        ),
        scoped=(RecordingMiddleware("scoped", calls),),
    )
    with TestClient(app) as client:
        client.get("/ping")

    assert calls == ["global-one", "global-two", "scoped"]


def test_failing_intercept_enters_the_exception_funnel() -> None:
    """An intercept crash becomes the generic 500 problem, carrying the route tail."""
    app = _MiddlewareApp(
        scoped=(FailingInterceptMiddleware(), SecurityMiddleware()),
    )
    with TestClient(app) as client:
        response = client.get("/ping")

    assert response.status_code == 500
    assert response.json()["type"] == "internal-server-error"
    assert response.headers["x-frame-options"] == "DENY"


# --- observe ---


def test_observe_sees_success_and_error_outcomes() -> None:
    """Observe sees the status of whatever left, success or problem body."""
    observer = Observer()
    with TestClient(_MiddlewareApp(global_middleware=(observer,))) as client:
        client.get("/ping")
        client.get("/missing")

    assert [(path, status) for path, status, _, _ in observer.seen] == [
        ("/ping", 200),
        ("/missing", 404),
    ]


def test_observe_duration_and_received_at_are_stamped() -> None:
    """A covered route stamps received_at at dispatch; duration measures to
    response-start."""
    observer = Observer()
    with TestClient(_MiddlewareApp(global_middleware=(observer,))) as client:
        client.get("/ping")

    (_, _, duration, received_at) = observer.seen[0]
    assert duration > 0.0
    assert received_at > 0.0


def test_global_observe_sees_unrouted_fallthrough() -> None:
    """Global observes cover 404/405 answers no route owns."""
    observer = Observer()
    with TestClient(_MiddlewareApp(global_middleware=(observer,))) as client:
        client.get("/nowhere")

    assert [(path, status) for path, status, _, _ in observer.seen] == [("/nowhere", 404)]


def test_global_observe_sees_intercept_short_circuit() -> None:
    """An answer from a global intercept is still an outcome observes see."""
    observer = Observer()
    app = _MiddlewareApp(global_middleware=(GateMiddleware(), observer))
    with TestClient(app) as client:
        client.get("/ping", headers={"x-gate": "block"})

    assert [(path, status) for path, status, _, _ in observer.seen] == [("/ping", 403)]


def test_async_observe() -> None:
    """Observe compiles sync or async, like auth."""
    observer = AsyncObserver()
    with TestClient(_MiddlewareApp(scoped=(observer,))) as client:
        client.get("/ping")

    assert observer.seen == [200]


def test_failing_observe_is_swallowed() -> None:
    """Observability must never break the response that already left."""
    with TestClient(_MiddlewareApp(scoped=(FailingObserver(),))) as client:
        response = client.get("/ping")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


# --- composition with CORS ---


def test_middleware_and_cors_compose_on_one_route() -> None:
    """CORS pairs and middleware headers land on the same response."""
    app = _MiddlewareApp(global_middleware=(SecurityMiddleware(),), cors=CORS())
    with TestClient(app) as client:
        response = client.get("/ping")

    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["x-frame-options"] == "DENY"


# --- wiring validation ---


class Hookless:
    """Defines nothing the middleware protocol knows."""


class MethodlessIntercept:
    """intercept without intercept_methods."""

    def intercept(self, request: Request) -> Refusal | None:
        """Never wired."""
        _ = request
        raise NotImplementedError()


class InterceptlessMethods:
    """intercept_methods without intercept."""

    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("GET",)


class OptionsScopedIntercept:
    """OPTIONS can never reach include-scoped middleware."""

    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("OPTIONS",)

    def intercept(self, request: Request) -> Refusal | None:
        """Never reachable when scoped."""
        _ = request
        raise NotImplementedError()


class ForbiddenHeaders(Struct):
    """Claims a header the senders own."""

    content_type: str = "text/plain"


class ForbiddenHeaderMiddleware:
    """Constant pairs may not touch sender-owned headers."""

    response_headers: ClassVar[ForbiddenHeaders] = ForbiddenHeaders()


class AsyncHeadersMiddleware:
    """response_headers must be sync — it runs inline in header assembly."""

    async def response_headers(self, request: Request) -> EchoHeaders | None:
        """Illegal: async."""
        _ = request
        raise NotImplementedError()


class BadObserveSignature:
    """observe must take exactly (request, status, duration)."""

    def observe(self, request: Request, status: int) -> None:
        """Illegal: missing duration."""
        _ = (request, status)


class BadRequestAnnotation:
    """The request parameter must be annotated Request[H]."""

    intercept_methods: ClassVar[tuple[HTTPMethod, ...]] = ("GET",)

    def intercept(self, request: str) -> Refusal | None:
        """Illegal: request isn't a Request."""
        _ = request
        raise NotImplementedError()


def test_hookless_middleware_is_a_wiring_error() -> None:
    """An object with no recognized hooks fails loud."""
    with pytest.raises(RuntimeError, match="defines no middleware hooks"):
        TestClient(_MiddlewareApp(global_middleware=(Hookless(),)))


def test_intercept_without_methods_is_a_wiring_error() -> None:
    """intercept requires its scope declaration."""
    with pytest.raises(RuntimeError, match="no intercept_methods"):
        TestClient(_MiddlewareApp(global_middleware=(MethodlessIntercept(),)))


def test_methods_without_intercept_is_a_wiring_error() -> None:
    """A scope declaration without the hook is dead configuration."""
    with pytest.raises(RuntimeError, match="defines no intercept"):
        TestClient(_MiddlewareApp(global_middleware=(InterceptlessMethods(),)))


def test_scoped_intercept_that_can_never_run_is_a_wiring_error() -> None:
    """OPTIONS-scoped middleware on an include is unreachable — fail loud."""
    with pytest.raises(RuntimeError, match="can never run"):
        TestClient(_MiddlewareApp(scoped=(OptionsScopedIntercept(),)))


def test_sender_owned_header_is_a_wiring_error() -> None:
    """content-type/content-length belong to the senders."""
    with pytest.raises(RuntimeError, match="senders own"):
        TestClient(_MiddlewareApp(global_middleware=(ForbiddenHeaderMiddleware(),)))


def test_async_response_headers_is_a_wiring_error() -> None:
    """The method tier runs inline in header assembly — sync only."""
    with pytest.raises(RuntimeError, match="must be sync"):
        TestClient(_MiddlewareApp(global_middleware=(AsyncHeadersMiddleware(),)))


def test_bad_observe_signature_is_a_wiring_error() -> None:
    """observe's shape is fixed."""
    with pytest.raises(RuntimeError, match="request, status, duration"):
        TestClient(_MiddlewareApp(global_middleware=(BadObserveSignature(),)))


def test_bad_request_annotation_is_a_wiring_error() -> None:
    """Hooks must annotate request: Request[H]."""
    with pytest.raises(RuntimeError, match="must be annotated Request"):
        TestClient(_MiddlewareApp(global_middleware=(BadRequestAnnotation(),)))


def test_duplicate_constant_headers_are_a_wiring_error() -> None:
    """Two middlewares' constant pairs claiming one name is checkable — so it fails."""
    with pytest.raises(RuntimeError, match="duplicate constant response header"):
        TestClient(_MiddlewareApp(global_middleware=(SecurityMiddleware(), SecurityMiddleware())))
