"""CORS: the app-wide default, per-include overrides, preflights, and error responses.

Focused local apps (the demo app inherits the wildcard default; see the demo test at the
bottom). CORS is pure response-header policy, so everything asserts on headers. Wiring
failures surface as ``RuntimeError`` — the TestClient drives the lifespan on construction.
"""

from collections.abc import Generator
from typing import cast

import pytest

from jero import (
    CORS,
    BaseApp,
    BytesResponse,
    Endpoint,
    ExceptionResponse,
    HTTPMethod,
    NotFoundError,
    Struct,
)
from jero.testing import TestClient


class Pong(Struct):
    """Minimal response payload."""

    ok: bool


class PingEndpoint(Endpoint, path="/ping"):
    """GET + POST, so preflights can distinguish allowed and unrouted methods."""

    def get(self) -> Pong:
        """Answer the ping."""
        return Pong(ok=True)

    def post(self) -> Pong:
        """Answer the ping."""
        return Pong(ok=True)


class MissingEndpoint(Endpoint, path="/missing"):
    """Always raises, so error responses can be inspected for CORS pairs."""

    def get(self) -> Pong:
        """Raise the framework 404."""
        raise NotFoundError()


class OpenEndpoint(Endpoint, path="/open"):
    """A route left without CORS in the per-include apps."""

    def get(self) -> Pong:
        """Answer plainly."""
        return Pong(ok=True)


class WildcardApp(BaseApp):
    """App-wide wildcard default; both endpoints inherit it."""

    async def wire(self) -> None:
        self._include_cors(CORS())
        self._include_endpoint(PingEndpoint())
        self._include_endpoint(MissingEndpoint())


class AllowListApp(BaseApp):
    """Origin allow-list with credentials — the dynamic echo tier."""

    async def wire(self) -> None:
        self._include_cors(CORS(allow_origins=("https://app.example",), allow_credentials=True))
        self._include_endpoint(PingEndpoint())


class PerIncludeApp(BaseApp):
    """No app default: /ping opts in on its own, /open stays CORS-free."""

    async def wire(self) -> None:
        self._include_endpoint(PingEndpoint(), cors=CORS(allow_methods=("GET",)))
        self._include_endpoint(OpenEndpoint())


class OptOutApp(BaseApp):
    """App default, but /open opts out with CORS.OFF."""

    async def wire(self) -> None:
        self._include_cors(CORS())
        self._include_endpoint(PingEndpoint())
        self._include_endpoint(OpenEndpoint(), cors=CORS.OFF)


class CredentialsWithWildcardApp(BaseApp):
    """Spec-forbidden combination; wiring must fail loud."""

    async def wire(self) -> None:
        self._include_cors(CORS(allow_credentials=True))


class MalformedOriginApp(BaseApp):
    """A trailing slash is a URL, not an origin; wiring must fail loud."""

    async def wire(self) -> None:
        self._include_cors(CORS(allow_origins=("https://app.example/",)))


class UnknownMethodApp(BaseApp):
    """A lowercase verb sneaks past static typing via cast; wiring must fail loud."""

    async def wire(self) -> None:
        methods = cast(tuple[HTTPMethod, ...], ("get",))
        self._include_endpoint(PingEndpoint(), cors=CORS(allow_methods=methods))


class DoubleDefaultApp(BaseApp):
    """Two app-wide defaults; wiring must fail loud."""

    async def wire(self) -> None:
        self._include_cors(CORS())
        self._include_cors(CORS())


class OffDefaultApp(BaseApp):
    """CORS.OFF is a per-include opt-out, not an app default."""

    async def wire(self) -> None:
        self._include_cors(CORS.OFF)


class SplitGetEndpoint(Endpoint, path="/split"):
    """The GET half of one path carrying two policies (see the POST half)."""

    def get(self) -> Pong:
        """Answer the split GET."""
        return Pong(ok=True)


class SplitPostEndpoint(Endpoint, path="/split-elsewhere"):
    """POST wired at a different path, so /split has no POST route at all."""

    def post(self) -> Pong:
        """Answer the split POST."""
        return Pong(ok=True)


class SplitApp(BaseApp):
    """Preflight answers are per (path, requested method)."""

    async def wire(self) -> None:
        self._include_endpoint(SplitGetEndpoint(), cors=CORS(allow_methods=("GET",)))
        self._include_endpoint(SplitPostEndpoint(), cors=CORS(allow_methods=("POST",)))


@pytest.fixture(name="wildcard_client")
def _wildcard_client() -> Generator[TestClient]:
    with TestClient(WildcardApp()) as client:
        yield client


@pytest.fixture(name="allow_list_client")
def _allow_list_client() -> Generator[TestClient]:
    with TestClient(AllowListApp()) as client:
        yield client


def test_wildcard_pairs_on_success(wildcard_client: TestClient) -> None:
    """A wildcard policy stamps its constant pair on covered responses."""
    response = wildcard_client.get("/ping", headers={"origin": "https://app.example"})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_wildcard_pairs_without_origin_header(wildcard_client: TestClient) -> None:
    """The wildcard tier is constant pairs — present for non-browser callers too."""
    response = wildcard_client.get("/ping")

    assert response.headers["access-control-allow-origin"] == "*"


def test_wildcard_pairs_on_head(wildcard_client: TestClient) -> None:
    """HEAD is served from the GET route and carries the same pairs."""
    response = wildcard_client.head("/ping")

    assert response.headers["access-control-allow-origin"] == "*"


def test_error_response_carries_route_pairs(wildcard_client: TestClient) -> None:
    """A browser page must be able to *read* the problem body, so the routed 404
    problem carries the route's CORS pairs."""
    response = wildcard_client.get("/missing", headers={"origin": "https://app.example"})

    assert response.status_code == 404
    assert response.headers["access-control-allow-origin"] == "*"


def test_unrouted_404_carries_app_default(wildcard_client: TestClient) -> None:
    """No route owns an unrouted 404; it carries the app default."""
    response = wildcard_client.get("/nowhere")

    assert response.status_code == 404
    assert response.headers["access-control-allow-origin"] == "*"


def test_405_carries_app_default(wildcard_client: TestClient) -> None:
    """A 405 is an app-level answer and carries the app default."""
    response = wildcard_client.request("PUT", "/ping")

    assert response.status_code == 405
    assert response.headers["access-control-allow-origin"] == "*"


def test_preflight_answers_wildcard(wildcard_client: TestClient) -> None:
    """A preflight rides the OPTIONS branch and gets the policy's precomputed block."""
    response = wildcard_client.options(
        "/ping",
        headers={"origin": "https://app.example", "access-control-request-method": "POST"},
    )

    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
    assert "POST" in response.headers["access-control-allow-methods"]
    assert response.headers["access-control-allow-headers"] == "content-type, authorization"
    assert response.headers["access-control-max-age"] == "600"


def test_preflight_for_unrouted_method_has_no_pairs(wildcard_client: TestClient) -> None:
    """PUT is not routed on /ping: the plain 204 + Allow goes out without CORS pairs."""
    response = wildcard_client.options(
        "/ping",
        headers={"origin": "https://app.example", "access-control-request-method": "PUT"},
    )

    assert response.status_code == 204
    assert "access-control-allow-origin" not in response.headers


def test_plain_options_is_not_a_preflight(wildcard_client: TestClient) -> None:
    """OPTIONS without Access-Control-Request-Method is the plain Allow answer."""
    response = wildcard_client.options("/ping")

    assert response.status_code == 204
    assert "allow" in response.headers
    assert "access-control-allow-origin" not in response.headers


def test_allow_list_echoes_allowed_origin(allow_list_client: TestClient) -> None:
    """An allow-listed origin is echoed back, with credentials and Vary: Origin."""
    response = allow_list_client.get("/ping", headers={"origin": "https://app.example"})

    assert response.headers["access-control-allow-origin"] == "https://app.example"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["vary"] == "Origin"


def test_allow_list_ignores_unknown_origin(allow_list_client: TestClient) -> None:
    """An origin off the list gets no pairs — but Vary: Origin stays constant, so
    caches never serve one origin's response to another."""
    response = allow_list_client.get("/ping", headers={"origin": "https://evil.example"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    assert response.headers["vary"] == "Origin"


def test_allow_list_without_origin_sends_no_echo(allow_list_client: TestClient) -> None:
    """No Origin header means no CORS request; nothing is echoed."""
    response = allow_list_client.get("/ping")

    assert "access-control-allow-origin" not in response.headers


def test_allow_list_preflight_echo(allow_list_client: TestClient) -> None:
    """The preflight answer echoes an allow-listed origin and grants credentials."""
    response = allow_list_client.options(
        "/ping",
        headers={"origin": "https://app.example", "access-control-request-method": "GET"},
    )

    assert response.headers["access-control-allow-origin"] == "https://app.example"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_allow_list_preflight_unknown_origin(allow_list_client: TestClient) -> None:
    """A preflight from an origin off the list gets the plain 204, no pairs."""
    response = allow_list_client.options(
        "/ping",
        headers={"origin": "https://evil.example", "access-control-request-method": "GET"},
    )

    assert response.status_code == 204
    assert "access-control-allow-origin" not in response.headers


def test_preflight_policy_is_per_requested_method() -> None:
    """One path, one policy per verb: the requested method selects which replies."""
    with TestClient(SplitApp()) as client:
        allowed = client.options(
            "/split",
            headers={"origin": "https://a.example", "access-control-request-method": "GET"},
        )
        refused = client.options(
            "/split",
            headers={"origin": "https://a.example", "access-control-request-method": "POST"},
        )

    assert allowed.headers["access-control-allow-methods"] == "GET"
    assert "access-control-allow-origin" not in refused.headers


def test_per_include_opt_in_without_default() -> None:
    """cors= works with no app default; everything else stays CORS-free."""
    with TestClient(PerIncludeApp()) as client:
        covered = client.get("/ping")
        open_route = client.get("/open")
        unrouted = client.get("/nowhere")

    assert covered.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-origin" not in open_route.headers
    assert "access-control-allow-origin" not in unrouted.headers


def test_cors_off_opts_an_include_out() -> None:
    """CORS.OFF removes the app-wide default from one include."""
    with TestClient(OptOutApp()) as client:
        covered = client.get("/ping")
        opted_out = client.get("/open")

    assert covered.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-origin" not in opted_out.headers


def test_credentials_with_wildcard_is_a_wiring_error() -> None:
    """allow_credentials=True with '*' is spec-forbidden and fails at startup."""
    with pytest.raises(RuntimeError, match="allow_credentials"):
        TestClient(CredentialsWithWildcardApp())


def test_malformed_origin_is_a_wiring_error() -> None:
    """An origin with a path/trailing slash fails at startup."""
    with pytest.raises(RuntimeError, match="not an origin"):
        TestClient(MalformedOriginApp())


def test_unknown_method_is_a_wiring_error() -> None:
    """A method outside the HTTPMethod vocabulary fails at startup."""
    with pytest.raises(RuntimeError, match="not an HTTP method"):
        TestClient(UnknownMethodApp())


def test_second_cors_default_is_a_wiring_error() -> None:
    """An app has at most one CORS default."""
    with pytest.raises(RuntimeError, match="already registered"):
        TestClient(DoubleDefaultApp())


def test_off_as_app_default_is_a_wiring_error() -> None:
    """CORS.OFF makes no sense as the app default — just skip _include_cors."""
    with pytest.raises(RuntimeError, match="per-include opt-out"):
        TestClient(OffDefaultApp())


def test_head_preflight_rides_get() -> None:
    """A preflight requesting HEAD is answered by the GET route's policy — routing
    auto-serves HEAD from GET, so the default policy must not deny it."""
    with TestClient(WildcardApp()) as client:
        response = client.options(
            "/ping",
            headers={"origin": "https://app.example", "access-control-request-method": "HEAD"},
        )

    assert response.headers["access-control-allow-origin"] == "*"


def test_openapi_routes_carry_the_app_default(client: TestClient) -> None:
    """/openapi.json and /docs are covered by the app-wide policy, so a hosted tool on
    another origin can fetch the spec."""
    spec = client.get("/openapi.json")
    docs = client.get("/docs")

    assert spec.headers["access-control-allow-origin"] == "*"
    assert docs.headers["access-control-allow-origin"] == "*"


def test_demo_app_inherits_wildcard_default(client: TestClient) -> None:
    """The demo app registers a wildcard default; open routes inherit it."""
    response = client.get("/healthz")

    assert response.headers["access-control-allow-origin"] == "*"


class BinaryEndpoint(Endpoint, path="/binary"):
    """bytes and BytesResponse returns, so the byte senders' tails are exercised."""

    def get(self) -> bytes:
        """Raw bytes out."""
        return b"raw"

    def post(self) -> BytesResponse:
        """Wrapped bytes out."""
        return BytesResponse(content=b"wrapped")


class ThingPath(Struct):
    """The template slot of the dynamic route."""

    thing_id: str


class ThingEndpoint(Endpoint, path="/things/{thing_id}"):
    """A templated (dynamic) route, so preflights resolve through the dynamic table."""

    def get(self, path: ThingPath) -> Pong:
        """Answer for any thing."""
        _ = path
        return Pong(ok=True)


class BoomError(Exception):
    """The error the custom handler translates."""


class BoomBody(Struct):
    """The custom handler's response body."""

    code: str


class BoomHandler:
    """Translates BoomError into a typed ExceptionResponse."""

    def handle_exception(self, exception: BoomError) -> ExceptionResponse[BoomBody]:
        """Always answer 502."""
        _ = exception
        return ExceptionResponse(status_code=502, json=BoomBody(code="boom"))


class BoomEndpoint(Endpoint, path="/boom"):
    """Always raises the custom-handled error."""

    def get(self) -> Pong:
        """Raise for the handler to translate."""
        raise BoomError()


class KindsApp(BaseApp):
    """Wildcard default over the non-plain-JSON senders and a dynamic route."""

    async def wire(self) -> None:
        self._include_cors(CORS())
        self._include_endpoint(BinaryEndpoint())
        self._include_endpoint(ThingEndpoint())


class HandledErrorApp(BaseApp):
    """Allow-list CORS + a custom exception handler, so the handler's response
    carries both the constant Vary pair and the dynamic origin echo."""

    async def wire(self) -> None:
        self._include_cors(CORS(allow_origins=("https://app.example",)))
        self._include_exception_handler(BoomHandler())
        self._include_endpoint(BoomEndpoint())


class EmptyMethodsApp(BaseApp):
    """allow_methods=() is meaningless; wiring must fail loud."""

    async def wire(self) -> None:
        self._include_cors(CORS(allow_methods=()))


class NegativeMaxAgeApp(BaseApp):
    """A negative max_age is malformed; wiring must fail loud."""

    async def wire(self) -> None:
        self._include_cors(CORS(max_age=-1))


class NotACORSDefaultApp(BaseApp):
    """A non-CORS object sneaks past static typing via cast; wiring must fail loud."""

    async def wire(self) -> None:
        self._include_cors(cast(CORS, object()))


class NotACORSIncludeApp(BaseApp):
    """A non-CORS cors= keyword sneaks past static typing via cast; fail loud."""

    async def wire(self) -> None:
        self._include_endpoint(PingEndpoint(), cors=cast(CORS, object()))


def test_wildcard_pairs_on_bytes_kinds() -> None:
    """The byte senders (plain bytes and BytesResponse) append the tail too."""
    with TestClient(KindsApp()) as client:
        raw = client.get("/binary")
        wrapped = client.post("/binary", json=None)

    assert raw.headers["access-control-allow-origin"] == "*"
    assert wrapped.headers["access-control-allow-origin"] == "*"


def test_preflight_resolves_dynamic_routes() -> None:
    """A preflight for a templated path resolves through the dynamic table."""
    with TestClient(KindsApp()) as client:
        response = client.options(
            "/things/thing-id",
            headers={"origin": "https://app.example", "access-control-request-method": "GET"},
        )

    assert response.headers["access-control-allow-origin"] == "*"


def test_preflight_denied_when_policy_excludes_a_routed_method() -> None:
    """POST is routed on /ping but the include's policy allows only GET — the policy
    replies with nothing (the plain 204 still goes out)."""
    with TestClient(PerIncludeApp()) as client:
        response = client.options(
            "/ping",
            headers={"origin": "https://a.example", "access-control-request-method": "POST"},
        )

    assert response.status_code == 204
    assert "access-control-allow-origin" not in response.headers


def test_preflight_on_uncovered_route_has_no_pairs() -> None:
    """A route with no policy (no default, no cors=) answers preflights plainly."""
    with TestClient(PerIncludeApp()) as client:
        response = client.options(
            "/open",
            headers={"origin": "https://a.example", "access-control-request-method": "GET"},
        )

    assert response.status_code == 204
    assert "access-control-allow-origin" not in response.headers


def test_allow_list_preflight_without_origin() -> None:
    """An allow-list preflight with no Origin header gets no pairs."""
    with TestClient(AllowListApp()) as client:
        response = client.options("/ping", headers={"access-control-request-method": "GET"})

    assert response.status_code == 204
    assert "access-control-allow-origin" not in response.headers


def test_exception_handler_response_carries_pairs() -> None:
    """A custom handler's ExceptionResponse leaves with the route's CORS pairs —
    constant Vary and the dynamic origin echo both."""
    with TestClient(HandledErrorApp()) as client:
        response = client.get("/boom", headers={"origin": "https://app.example"})

    assert response.status_code == 502
    assert response.headers["access-control-allow-origin"] == "https://app.example"
    assert response.headers["vary"] == "Origin"


def test_empty_allow_methods_is_a_wiring_error() -> None:
    """allow_methods=() fails at startup."""
    with pytest.raises(RuntimeError, match="must not be empty"):
        TestClient(EmptyMethodsApp())


def test_negative_max_age_is_a_wiring_error() -> None:
    """max_age=-1 fails at startup."""
    with pytest.raises(RuntimeError, match="non-negative"):
        TestClient(NegativeMaxAgeApp())


def test_non_cors_default_is_a_wiring_error() -> None:
    """_include_cors rejects a non-CORS object at startup."""
    with pytest.raises(RuntimeError, match="requires a CORS policy"):
        TestClient(NotACORSDefaultApp())


def test_non_cors_include_keyword_is_a_wiring_error() -> None:
    """cors= rejects a non-CORS object at startup."""
    with pytest.raises(RuntimeError, match="must be a CORS policy"):
        TestClient(NotACORSIncludeApp())
