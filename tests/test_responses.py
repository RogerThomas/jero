"""Response kinds: bytes in, BytesResponse / JSONResponse out, camelCase."""

from collections.abc import Generator

import pytest
from msgspec import Struct

from jero import BaseApp, BytesResponse, JSONResponse, RawHeaders, Resource, TestClient


class Echo(Struct):
    """Response echoing a decoded request body."""

    body: str


class BlobPath(Struct):
    """Path params carrying a blob id."""

    id: str


class BlobResource(Resource):
    """Resource exercising raw bytes in and custom response types out."""

    async def create(self, content: bytes) -> JSONResponse:
        """Echo a raw bytes body back as a JSON response with a custom header."""
        return JSONResponse(json=Echo(body=content.decode()), headers={"x-kind": "echo"})

    async def read_one(self, path: BlobPath) -> BytesResponse:
        """Return the path id as raw bytes with a custom header."""
        return BytesResponse(content=path.id.encode(), headers={"x-id": path.id})


class BlobApp(BaseApp):
    """App exercising the non-JSON response kinds."""

    async def _wire(self) -> None:
        self._include_resource(BlobResource(), path="/blobs")


@pytest.fixture(name="blob_client")
def _blob_client() -> Generator[TestClient]:
    with TestClient(BlobApp()) as client:
        yield client


def test_content_bytes_in_json_response_out(blob_client: TestClient) -> None:
    """A raw bytes body is accepted and echoed back as a JSON response with headers."""
    resp = blob_client.post("/blobs", content=b"hello")
    assert resp.status_code == 201
    assert resp.json() == {"body": "hello"}
    assert resp.headers["x-kind"] == "echo"
    assert resp.headers["content-type"] == "application/json"


def test_bytes_response_with_custom_header(blob_client: TestClient) -> None:
    """A BytesResponse returns raw bytes, a custom header, and an octet-stream type."""
    resp = blob_client.get("/blobs/abc")
    assert resp.status_code == 200
    assert resp.content == b"abc"
    assert resp.headers["x-id"] == "abc"
    assert resp.headers["content-type"] == "application/octet-stream"


def test_snakecase_key_is_rejected_for_camel_field(client: TestClient) -> None:
    """A snake_case key is not accepted for a camelCase field (rejected with 422)."""
    # priceCents is the wire name; a snake_case price_cents leaves it unset -> 422.
    # (camelCase *output* is already asserted by test_resource's create test.)
    bad = client.post(
        "/widgets",
        json={"name": "name", "price_cents": 1},
        headers={"authorization": "Bearer token"},
    )
    assert bad.status_code == 422


# --- Response headers accept a RawHeaders bag (forwarding), not just a dict ---


class RawRespResource(Resource):
    """Resource returning responses whose headers come from a RawHeaders bag."""

    async def read_many(self) -> JSONResponse:
        """Set response headers from a RawHeaders, preserving its as-sent casing."""
        return JSONResponse(
            json=Echo(body="ok"),
            headers=RawHeaders([("X-Kind", "raw"), ("X-Trace-Id", "trace")]),
        )

    async def create(self, content: bytes) -> JSONResponse:
        """Forward a bag carrying a repeated header (the Set-Cookie case)."""
        return JSONResponse(
            json=Echo(body=content.decode()),
            headers=RawHeaders([("Set-Cookie", "first"), ("Set-Cookie", "second")]),
        )


class RawRespApp(BaseApp):
    """App wiring the RawHeaders-response resource."""

    async def _wire(self) -> None:
        self._include_resource(RawRespResource(), path="/raw-resp")


def test_response_accepts_raw_headers_bag() -> None:
    """A response built with headers=RawHeaders(...) emits those headers, casing intact."""
    with TestClient(RawRespApp()) as client:
        resp = client.get("/raw-resp")
        assert resp.status_code == 200
        assert resp.json() == {"body": "ok"}
        # Names go out with their as-sent casing (the framework does not lowercase them).
        assert resp.headers["X-Kind"] == "raw"
        assert resp.headers["X-Trace-Id"] == "trace"


def test_response_forwards_repeated_headers_from_raw_bag() -> None:
    """A RawHeaders response forwards repeated headers (Set-Cookie) a dict can't hold.

    The captured ``headers`` dict collapses repeats, so this asserts on
    ``multi_headers`` — the faithful wire pair list.
    """
    with TestClient(RawRespApp()) as client:
        resp = client.post("/raw-resp", content=b"ok")
        assert resp.status_code == 201
        assert ("Set-Cookie", "first") in resp.multi_headers
        assert ("Set-Cookie", "second") in resp.multi_headers
