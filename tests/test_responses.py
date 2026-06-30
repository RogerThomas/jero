"""Response kinds: bytes in, BytesResponse / JSONResponse out, camelCase."""

from collections.abc import Generator
from enum import Enum
from uuid import UUID

import pytest
from msgspec import Struct

from jero import BaseApp, BytesResponse, JSONResponse, RawHeaders, Resource, TestClient


class Echo(Struct):
    """Response echoing a decoded request body."""

    body: str


class BlobPath(Struct):
    """Path params carrying a blob id."""

    id: str


class BlobResource(Resource, path="/blobs"):
    """Resource exercising raw bytes in and custom response types out."""

    async def create(self, content: bytes) -> JSONResponse[Echo]:
        """Echo a raw bytes body back as a JSON response with a custom header."""
        return JSONResponse(json=Echo(body=content.decode()), raw_headers={"x-kind": "echo"})

    async def read_one(self, path: BlobPath) -> BytesResponse:
        """Return the path id as raw bytes with a custom header."""
        return BytesResponse(content=path.id.encode(), raw_headers={"x-id": path.id})


class BlobApp(BaseApp):
    """App exercising the non-JSON response kinds."""

    async def wire(self) -> None:
        self.include_resource(BlobResource())


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


class RawRespResource(Resource, path="/raw-resp"):
    """Resource returning responses whose headers come from a RawHeaders bag."""

    async def read_many(self) -> JSONResponse[Echo]:
        """Set response headers from a RawHeaders, preserving its as-sent casing."""
        return JSONResponse(
            json=Echo(body="ok"),
            raw_headers=RawHeaders([("X-Kind", "raw"), ("X-Trace-Id", "trace")]),
        )

    async def create(self, content: bytes) -> JSONResponse[Echo]:
        """Forward a bag carrying a repeated header (the Set-Cookie case)."""
        return JSONResponse(
            json=Echo(body=content.decode()),
            raw_headers=RawHeaders([("Set-Cookie", "first"), ("Set-Cookie", "second")]),
        )


class RawRespApp(BaseApp):
    """App wiring the RawHeaders-response resource."""

    async def wire(self) -> None:
        self.include_resource(RawRespResource())


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


# --- Typed response headers: a Struct, mirroring how headers are received ---


class Meta(Struct):
    """A nested value to exercise Struct-valued (JSON-encoded) headers."""

    region: str


class Tier(Enum):
    """An enum value to exercise Enum-valued headers."""

    GOLD = "gold"


class RespHeaders(Struct):
    """Typed response headers: field names inverse-mangle to wire names."""

    x_trace_id: str
    x_rate_limit: int
    x_cached: bool
    x_meta: Meta
    x_tier: Tier
    x_absent: str | None = None


class TypedHeaderResource(Resource, path="/typed"):
    """Resource returning typed headers, plus raw_headers for a repeated cookie."""

    async def read_many(self) -> JSONResponse[Echo, RespHeaders]:
        """Set typed headers (Struct) and a raw Set-Cookie repeat together."""
        return JSONResponse(
            json=Echo(body="ok"),
            headers=RespHeaders(
                x_trace_id="trace",
                x_rate_limit=100,
                x_cached=True,
                x_meta=Meta(region="eu"),
                x_tier=Tier.GOLD,
            ),
            raw_headers=RawHeaders([("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]),
        )


class TypedHeaderApp(BaseApp):
    """App wiring the typed-header resource."""

    async def wire(self) -> None:
        self.include_resource(TypedHeaderResource())


@pytest.fixture(name="typed_client")
def _typed_client() -> Generator[TestClient]:
    with TestClient(TypedHeaderApp()) as client:
        yield client


def test_typed_headers_mangle_names_and_encode_values(typed_client: TestClient) -> None:
    """Field names inverse-mangle (x_trace_id -> x-trace-id) and values stringify;
    a Struct field is JSON-encoded; a None field is omitted."""
    resp = typed_client.get("/typed")
    assert resp.status_code == 200
    assert resp.headers["x-trace-id"] == "trace"
    assert resp.headers["x-rate-limit"] == "100"
    assert resp.headers["x-cached"] == "true"
    assert resp.headers["x-meta"] == '{"region":"eu"}'
    assert resp.headers["x-tier"] == "gold"
    assert "x-absent" not in resp.headers


def test_typed_and_raw_headers_both_emitted(typed_client: TestClient) -> None:
    """Typed headers and raw_headers are both sent; raw repeats survive."""
    resp = typed_client.get("/typed")
    assert resp.headers["x-trace-id"] == "trace"
    assert ("Set-Cookie", "a=1") in resp.multi_headers
    assert ("Set-Cookie", "b=2") in resp.multi_headers


# --- A UUID-valued typed header serializes to its bare text (regression) ---


class UUIDHeaders(Struct):
    """A typed header carrying a UUID — not a str/int/bool/Enum scalar."""

    x_response_id: UUID


class UUIDHeaderResource(Resource, path="/uuid"):
    """Resource returning a single UUID-valued typed header."""

    async def read_many(self) -> JSONResponse[Echo, UUIDHeaders]:
        """Set a typed header whose value is a UUID."""
        return JSONResponse(
            json=Echo(body="ok"),
            headers=UUIDHeaders(x_response_id=UUID("019ed22b-3467-7194-809b-215e581bf0d4")),
        )


class UUIDHeaderApp(BaseApp):
    """App wiring the UUID-header resource."""

    async def wire(self) -> None:
        self.include_resource(UUIDHeaderResource())


def test_uuid_typed_header_is_bare_string() -> None:
    """A UUID header value is emitted as its bare text, not a quoted JSON scalar.

    Regression for the bug where a UUID (and other stringy extended scalars) fell
    through to JSON-encoding and arrived wrapped in literal double quotes."""
    with TestClient(UUIDHeaderApp()) as client:
        resp = client.get("/uuid")
        assert resp.headers["x-response-id"] == "019ed22b-3467-7194-809b-215e581bf0d4"


# --- status_code overrides the verb's default status ---


class StatusResource(Resource, path="/status"):
    """Resource overriding the default status code on its response."""

    async def create(self, content: bytes) -> JSONResponse[Echo]:
        """Return 202 instead of the create verb's default 201."""
        return JSONResponse(json=Echo(body=content.decode()), status_code=202)


class StatusApp(BaseApp):
    """App wiring the status-override resource."""

    async def wire(self) -> None:
        self.include_resource(StatusResource())


def test_status_code_overrides_verb_default() -> None:
    """A response's status_code overrides the verb's default (201 -> 202)."""
    with TestClient(StatusApp()) as client:
        resp = client.post("/status", content=b"ok")
        assert resp.status_code == 202
        assert resp.json() == {"body": "ok"}
