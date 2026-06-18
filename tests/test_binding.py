"""Source binding and REST error semantics: 400 / 404 / 422 / 500."""

from msgspec import Struct
from msgspec.json import decode as json_decode

from jero import BaseApp, Endpoint, RawHeaders, Resource, TestClient


def test_bad_body_type_is_422(client: TestClient) -> None:
    """A body field of the wrong type fails validation with 422."""
    resp = client.post(
        "/widgets",
        json={"name": "name", "priceCents": "not-an-int"},
        headers={"authorization": "Bearer token"},
    )
    assert resp.status_code == 422


def test_malformed_body_is_400(client: TestClient) -> None:
    """A syntactically invalid JSON body fails decoding with 400."""
    resp = client.post("/widgets", content=b'{"name":', headers={"authorization": "Bearer token"})
    assert resp.status_code == 400


def test_bad_query_param_is_400(client: TestClient) -> None:
    """A query param of the wrong type fails binding with 400."""
    resp = client.get(
        "/widgets", params={"limit": "not-an-int"}, headers={"authorization": "Bearer token"}
    )
    assert resp.status_code == 400


# --- Esoteric: scalar-typed path and header sources (DemoApp uses str ids only) ---


class IntPath(Struct):
    """Path params with an integer slot."""

    n: int


class Body(Struct):
    """Request/response body with a single integer field."""

    n: int


class Headers(Struct):
    """Request headers with an integer token."""

    x_token: int


class IntResource(Resource, path="/things"):
    """Resource binding integer path, body, and header sources."""

    async def read_one(self, path: IntPath) -> Body:
        """Echo the integer bound from the path."""
        return Body(n=path.n)

    async def create(self, json: Body, headers: Headers) -> Body:
        """Echo the sum of the body field and the bound integer header."""
        return Body(n=json.n + headers.x_token)


class IntApp(BaseApp):
    """App wiring IntResource at /things."""

    async def _wire(self) -> None:
        self._include_resource(IntResource())


def test_path_value_that_fails_conversion_is_404() -> None:
    """A path value that cannot convert to int is treated as no match (404)."""
    with TestClient(IntApp()) as client:
        assert client.get("/things/5").status_code == 200
        assert client.get("/things/not-an-int").status_code == 404


def test_bad_header_is_400() -> None:
    """A header value of the wrong type fails binding with 400."""
    with TestClient(IntApp()) as client:
        resp = client.post("/things", json={"n": 1}, headers={"x-token": "not-an-int"})
        assert resp.status_code == 400


# --- Esoteric: errors raised inside a handler are server faults (500) ---


class UpstreamValidationEndpoint(Endpoint, path="/upstream-validation"):
    """Endpoint that triggers a validation error while decoding upstream data."""

    async def get(self) -> Body:
        """Decode upstream JSON whose field type is invalid."""
        return json_decode(b'{"n": "not-an-int"}', type=Body)


class UpstreamDecodeEndpoint(Endpoint, path="/upstream-decode"):
    """Endpoint that triggers a decode error on malformed upstream data."""

    async def get(self) -> Body:
        """Decode malformed upstream JSON."""
        return json_decode(b'{"n":', type=Body)


class UpstreamDecodeApp(BaseApp):
    """App wiring the upstream-error endpoints."""

    async def _wire(self) -> None:
        self._include_endpoint(UpstreamValidationEndpoint())
        self._include_endpoint(UpstreamDecodeEndpoint())


def test_handler_side_validation_error_is_500() -> None:
    """A validation error raised inside a handler surfaces as 500."""
    with TestClient(UpstreamDecodeApp()) as client:
        resp = client.get("/upstream-validation")
        assert resp.status_code == 500


def test_handler_side_decode_error_is_500() -> None:
    """A decode error raised inside a handler surfaces as 500."""
    with TestClient(UpstreamDecodeApp()) as client:
        resp = client.get("/upstream-decode")
        assert resp.status_code == 500


# --- raw_headers: the opaque header bag, for forwarding / diagnostics ---


class Reply(Struct):
    """Single-string reply for the raw_headers handlers."""

    value: str


class Trace(Struct):
    """Typed headers carrying a trace id (x-trace-id -> x_trace_id)."""

    x_trace_id: str


class RawHeadersEndpoint(Endpoint, path="/raw"):
    """GET handler reading a request header through the opaque bag."""

    async def get(self, raw_headers: RawHeaders) -> Reply:
        """Echo a header looked up with different casing than it was sent."""
        return Reply(value=raw_headers["X-Trace-Id"])


class BothHeadersEndpoint(Endpoint, path="/both"):
    """GET handler taking the typed headers Struct and the raw bag together."""

    async def get(self, headers: Trace, raw_headers: RawHeaders) -> Reply:
        """Combine the validated typed header with a raw, case-insensitive lookup."""
        return Reply(value=f"{headers.x_trace_id}:{raw_headers['x-trace-id']}")


class RawHeadersApp(BaseApp):
    """App wiring the raw_headers endpoints."""

    async def _wire(self) -> None:
        self._include_endpoint(RawHeadersEndpoint())
        self._include_endpoint(BothHeadersEndpoint())


def test_raw_headers_handler_sees_request_headers() -> None:
    """A handler declaring raw_headers reads the request headers, casing-insensitively."""
    with TestClient(RawHeadersApp()) as client:
        resp = client.get("/raw", headers={"X-Trace-Id": "trace"})
        assert resp.status_code == 200
        assert resp.json() == {"value": "trace"}


def test_raw_headers_coexists_with_typed_headers() -> None:
    """raw_headers and a typed headers Struct bind independently on the same handler."""
    with TestClient(RawHeadersApp()) as client:
        resp = client.get("/both", headers={"x-trace-id": "trace"})
        assert resp.status_code == 200
        assert resp.json() == {"value": "trace:trace"}
