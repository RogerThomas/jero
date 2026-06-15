"""The Endpoint primitive: bare HTTP verbs, no CRUD semantics."""

from msgspec import Struct

from jero import BaseApp, Endpoint, TestClient


def test_endpoint_get(client: TestClient) -> None:
    """GET on an endpoint returns its handler's JSON body."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


class Ack(Struct):
    """Acknowledgement payload returned by the Pinger endpoint."""

    ok: bool


class Pinger(Endpoint):
    """Sample endpoint that answers POST with an acknowledgement."""

    async def post(self) -> Ack:
        """Return an acknowledgement for a POST request."""
        return Ack(ok=True)


class PingApp(BaseApp):
    """App wiring the Pinger endpoint at /ping."""

    async def _wire(self) -> None:
        self._include_endpoint(Pinger(), path="/ping")


def test_endpoint_post_returns_200_not_201() -> None:
    """An endpoint POST returns 200 since endpoints have no CRUD semantics."""
    # Endpoints carry no CRUD semantics, so POST is 200, not 201.
    with TestClient(PingApp()) as client:
        resp = client.post("/ping")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


def test_endpoint_unknown_verb_is_405() -> None:
    """An unsupported verb on an endpoint returns 405 with an Allow header."""
    with TestClient(PingApp()) as client:
        resp = client.get("/ping")
        assert resp.status_code == 405
        assert resp.headers["allow"] == "POST, OPTIONS"
