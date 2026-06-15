"""Source binding and REST error semantics: 400 / 404 / 422 / 500."""

from msgspec import Struct
from msgspec.json import decode as json_decode

from jero import BaseApp, Endpoint, Resource, TestClient


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


class IntResource(Resource):
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
        self._include_resource(IntResource(), path="/things")


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


class UpstreamValidationEndpoint(Endpoint):
    """Endpoint that triggers a validation error while decoding upstream data."""

    async def get(self) -> Body:
        """Decode upstream JSON whose field type is invalid."""
        return json_decode(b'{"n": "not-an-int"}', type=Body)


class UpstreamDecodeEndpoint(Endpoint):
    """Endpoint that triggers a decode error on malformed upstream data."""

    async def get(self) -> Body:
        """Decode malformed upstream JSON."""
        return json_decode(b'{"n":', type=Body)


class UpstreamDecodeApp(BaseApp):
    """App wiring the upstream-error endpoints."""

    async def _wire(self) -> None:
        self._include_endpoint(UpstreamValidationEndpoint(), path="/upstream-validation")
        self._include_endpoint(UpstreamDecodeEndpoint(), path="/upstream-decode")


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
