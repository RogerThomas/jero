"""Response kinds: bytes in, BytesResponse / JSONResponse out, camelCase."""

import logging
from collections.abc import Generator
from dataclasses import dataclass
from enum import Enum
from typing import cast
from uuid import UUID

import pytest
from msgspec import Struct

from jero import (
    Accepted,
    BaseApp,
    BytesResponse,
    Created,
    Endpoint,
    ExceptionResponse,
    JSONResponse,
    Location,
    NoContent,
    RawHeaders,
    Resource,
)
from jero.testing import TestClient


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
        self._include_resource(BlobResource())


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
        self._include_resource(RawRespResource())


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
        self._include_resource(TypedHeaderResource())


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
        self._include_resource(UUIDHeaderResource())


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
        self._include_resource(StatusResource())


def test_status_code_overrides_verb_default() -> None:
    """A response's status_code overrides the verb's default (201 -> 202)."""
    with TestClient(StatusApp()) as client:
        resp = client.post("/status", content=b"ok")
        assert resp.status_code == 202
        assert resp.json() == {"body": "ok"}


# --- NoContent / Created / Accepted: dynamic success status (single member) ---


class NoContentEndpoint(Endpoint, path="/no-content"):
    """Endpoint returning a bare NoContent — a 204 despite the GET verb's 200 default."""

    async def get(self) -> NoContent:
        """Return 204, no body, but a Location header."""
        return NoContent(location=Location.from_path("/elsewhere"))


class CreatedEndpoint(Endpoint, path="/created"):
    """Endpoint returning Created — a 201 despite the GET verb's 200 default."""

    async def get(self) -> Created[Echo]:
        """Return 201 with a JSON body."""
        return Created(json=Echo(body="ok"))


class AcceptedEndpoint(Endpoint, path="/accepted"):
    """Endpoint returning Accepted — a 202 despite the POST verb's 200 default."""

    async def post(self, content: bytes) -> Accepted[Echo]:
        """Return 202 with a JSON body."""
        return Accepted(json=Echo(body=content.decode()))


class NoContentOverrideEndpoint(Endpoint, path="/no-content-override"):
    """Endpoint whose NoContent overrides its own fixed status via status_code=."""

    async def get(self) -> NoContent:
        """Return 200 instead of NoContent's own 204 (the escape hatch)."""
        return NoContent(status_code=200)


class FixedStatusApp(BaseApp):
    """App wiring the fixed-status single-member endpoints."""

    async def wire(self) -> None:
        self._include_endpoint(NoContentEndpoint())
        self._include_endpoint(CreatedEndpoint())
        self._include_endpoint(AcceptedEndpoint())
        self._include_endpoint(NoContentOverrideEndpoint())


@pytest.fixture(name="fixed_status_client")
def _fixed_status_client() -> Generator[TestClient]:
    with TestClient(FixedStatusApp()) as client:
        yield client


def test_no_content_is_204_with_empty_body_and_no_content_headers(
    fixed_status_client: TestClient,
) -> None:
    """NoContent sends 204, an empty body, no content-type/content-length, but Location.

    A 204 is the case where omitting both content headers is what the spec requires, so
    unlike the override above there is deliberately no ``content-length: 0`` here."""
    resp = fixed_status_client.get("/no-content")
    assert resp.status_code == 204
    assert resp.content == b""
    assert "content-type" not in resp.headers
    assert "content-length" not in resp.headers
    assert resp.headers["location"] == "/elsewhere"


def test_created_documents_and_sends_201_on_a_get(fixed_status_client: TestClient) -> None:
    """Created fixes 201 even though the verb (GET) would otherwise default to 200."""
    resp = fixed_status_client.get("/created")
    assert resp.status_code == 201
    assert resp.json() == {"body": "ok"}


def test_accepted_documents_and_sends_202_on_a_post(fixed_status_client: TestClient) -> None:
    """Accepted fixes 202 even though the verb (POST on an Endpoint) defaults to 200."""
    resp = fixed_status_client.post("/accepted", content=b"ok")
    assert resp.status_code == 202
    assert resp.json() == {"body": "ok"}


def test_status_code_overrides_no_contents_own_fixed_status(
    fixed_status_client: TestClient,
) -> None:
    """status_code= still wins over NoContent's own fixed 204, as the escape hatch — and the
    empty body is then framed with ``content-length: 0``. Only 204/304/1xx may omit content
    framing entirely; at any other status leaving it out would hand the framing to the
    server to guess at."""
    resp = fixed_status_client.get("/no-content-override")
    assert resp.status_code == 200
    assert resp.headers["content-length"] == "0"
    assert resp.content == b""


# --- Union returns: a handler answers with different success statuses ---


class VisibilityParams(Struct):
    """Query flag selecting which union branch a handler takes."""

    visible: str


class SpotlightEndpoint(Endpoint, path="/spotlight"):
    """The motivating case: a 204 for a caller who may not see the resource, else 200."""

    async def get(self, params: VisibilityParams) -> JSONResponse[Echo] | NoContent:
        """Return the resource, or 204 when the caller may not see it."""
        if params.visible == "no":
            return NoContent()
        return JSONResponse(json=Echo(body="widget"))


class WrapperUnionEndpoint(Endpoint, path="/wrapper-union"):
    """A union of two wrappers whose fixed statuses differ (Created's 201 vs the verb's 200)."""

    async def get(self, params: VisibilityParams) -> JSONResponse[Echo] | Created[Echo]:
        """Return the Created branch or the plain JSONResponse branch, on request."""
        if params.visible == "no":
            return Created(json=Echo(body="made"))
        return JSONResponse(json=Echo(body="found"))


class PlainStructEndpoint(Endpoint, path="/plain-struct"):
    """A union of a *plain* Struct return and NoContent — no wrapper needed."""

    async def get(self, params: VisibilityParams) -> Echo | NoContent:
        """Return the bare Struct at the verb's default status, or 204."""
        if params.visible == "no":
            return NoContent()
        return Echo(body="plain")


class PlainListEndpoint(Endpoint, path="/plain-list"):
    """A union whose body branch is a bare ``list[Struct]``."""

    async def get(self, params: VisibilityParams) -> list[Echo] | NoContent:
        """Return a JSON array at the verb's default status, or 204."""
        if params.visible == "no":
            return NoContent()
        return [Echo(body="plain")]


class PlainBytesEndpoint(Endpoint, path="/plain-bytes"):
    """A union whose body branch is bare ``bytes``."""

    async def get(self, params: VisibilityParams) -> bytes | NoContent:
        """Return raw bytes at the verb's default status, or 204."""
        if params.visible == "no":
            return NoContent()
        return b"plain"


class UnionApp(BaseApp):
    """App wiring the union-return endpoints."""

    async def wire(self) -> None:
        self._include_endpoint(SpotlightEndpoint())
        self._include_endpoint(WrapperUnionEndpoint())
        self._include_endpoint(PlainStructEndpoint())
        self._include_endpoint(PlainListEndpoint())
        self._include_endpoint(PlainBytesEndpoint())


@pytest.fixture(name="union_client")
def _union_client() -> Generator[TestClient]:
    with TestClient(UnionApp()) as client:
        yield client


def test_union_returns_the_json_branch(union_client: TestClient) -> None:
    """The JSONResponse branch of a union return sends 200 with its body."""
    resp = union_client.get("/spotlight", params={"visible": "yes"})
    assert resp.status_code == 200
    assert resp.json() == {"body": "widget"}


def test_union_returns_the_no_content_branch(union_client: TestClient) -> None:
    """The NoContent branch of a union return sends 204 with no body."""
    resp = union_client.get("/spotlight", params={"visible": "no"})
    assert resp.status_code == 204
    assert resp.content == b""


def test_created_and_accepted_are_siblings_of_json_response() -> None:
    """Never subclasses. As a subclass, ``-> JSONResponse[T]`` would statically accept a
    returned ``Created`` and then send the *verb's* status — an object whose type says 201
    answering 200, with no type checker able to see it. Sibling makes that a type error."""
    assert not issubclass(Created, JSONResponse)
    assert not issubclass(Accepted, JSONResponse)


def test_union_dispatches_each_wrapper_to_its_own_status(union_client: TestClient) -> None:
    """Each wrapper member sends the status its own type fixes: Created 201, plain
    JSONResponse the verb's 200."""
    made = union_client.get("/wrapper-union", params={"visible": "no"})
    assert made.status_code == 201
    assert made.json() == {"body": "made"}
    found = union_client.get("/wrapper-union", params={"visible": "yes"})
    assert found.status_code == 200
    assert found.json() == {"body": "found"}


@pytest.mark.parametrize(
    ("path", "content_type", "body"),
    [
        ("/plain-struct", "application/json", b'{"body":"plain"}'),
        ("/plain-list", "application/json", b'[{"body":"plain"}]'),
        ("/plain-bytes", "application/octet-stream", b"plain"),
    ],
)
def test_plain_return_joins_a_union_without_a_wrapper(
    union_client: TestClient, path: str, content_type: str, body: bytes
) -> None:
    """A bare Struct / list[Struct] / bytes member needs no wrapper: it takes the verb's
    default status, exactly as it does when it is the handler's sole return."""
    resp = union_client.get(path, params={"visible": "yes"})
    assert resp.status_code == 200
    assert resp.content == body
    assert resp.headers["content-type"] == content_type


@pytest.mark.parametrize("path", ["/plain-struct", "/plain-list", "/plain-bytes"])
def test_plain_return_unions_still_take_the_no_content_branch(
    union_client: TestClient, path: str
) -> None:
    """The other branch of a plain-return union is still a bodyless 204."""
    resp = union_client.get(path, params={"visible": "no"})
    assert resp.status_code == 204
    assert resp.content == b""
    assert "content-type" not in resp.headers


# --- Typed headers on a 204, and the unsubscripted wrappers ---


class TraceHeaders(Struct):
    """Typed headers carried by a bodyless 204."""

    x_trace_id: str


class NoContentHeadersEndpoint(Endpoint, path="/no-content-headers"):
    """A 204 that still carries typed headers — RFC 9110 allows it."""

    async def get(self) -> NoContent[TraceHeaders]:
        """Return 204 with a typed header and no body."""
        return NoContent(headers=TraceHeaders(x_trace_id="trace"))


class FramingHeadersEndpoint(Endpoint, path="/framing-headers"):
    """A 204 whose raw headers try to supply the framing headers the status forbids."""

    async def get(self) -> NoContent:
        """Return 204, asking for content headers RFC 9110 does not allow at that status."""
        return NoContent(
            raw_headers={"content-type": "application/json", "content-length": "17"},
            headers=None,
        )


class LyingUnionEndpoint(Endpoint, path="/lying-union"):
    """A handler that returns something its own union annotation does not allow."""

    async def get(self) -> Echo | NoContent:
        """Violate the declared return type, as a mistyped handler would."""
        return cast("Echo", object())


class HeaderedNoContentApp(BaseApp):
    """App wiring the typed-header 204 and the contract-violating union."""

    async def wire(self) -> None:
        self._include_endpoint(NoContentHeadersEndpoint())
        self._include_endpoint(FramingHeadersEndpoint())
        self._include_endpoint(LyingUnionEndpoint())


@pytest.fixture(name="unsubscripted_client")
def _unsubscripted_client() -> Generator[TestClient]:
    with TestClient(HeaderedNoContentApp()) as client:
        yield client


def test_no_content_carries_typed_headers(unsubscripted_client: TestClient) -> None:
    """A 204 emits its typed headers while still sending no body and no content headers."""
    resp = unsubscripted_client.get("/no-content-headers")
    assert resp.status_code == 204
    assert resp.headers["x-trace-id"] == "trace"
    assert resp.content == b""
    assert "content-type" not in resp.headers
    assert "content-length" not in resp.headers


def test_no_content_strips_framing_headers_it_is_handed(
    unsubscripted_client: TestClient,
) -> None:
    """204 forbids ``content-type`` and ``content-length`` (RFC 9110 §15.3.5), so supplying them
    through ``raw_headers`` drops them rather than emitting a framing claim the status disallows.
    Stripping is the reason the bodyless header path exists, so it needs a case that hands it
    something to strip — every other 204 test supplies nothing and would pass either way."""
    resp = unsubscripted_client.get("/framing-headers")
    assert resp.status_code == 204
    assert resp.content == b""
    assert "content-type" not in resp.headers
    assert "content-length" not in resp.headers


def test_union_result_matching_no_member_is_an_error(
    unsubscripted_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A handler returning something outside its own union has no sender to dispatch to.
    Nothing has been sent yet, so it becomes a proper 500 through the app's exception
    handlers — logged for the operator, a clean Problem body for the client."""
    with caplog.at_level(logging.ERROR, logger="jero"):
        resp = unsubscripted_client.get("/lying-union")
    assert resp.status_code == 500
    assert resp.json()["type"] == "internal-server-error"
    assert "matches none of its declared union return types" in caplog.text


@dataclass
class _TypeErrorHandler:
    """An app-registered handler for TypeError — the framework's union breach is reported as
    one, so a handler like this intercepts it."""

    def handle_exception(self, exception: TypeError) -> ExceptionResponse[Echo, None]:
        """Answer 400 rather than letting the default 500 through."""
        return ExceptionResponse(status_code=400, json=Echo(body=str(exception)))


class HandledBreachApp(BaseApp):
    """App that handles TypeError, so the union breach never reaches the default 500."""

    async def wire(self) -> None:
        self._include_endpoint(LyingUnionEndpoint())
        self._include_exception_handler(_TypeErrorHandler())


def test_union_breach_is_logged_even_when_the_app_handles_type_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The breach is logged by the sender *before* it is delegated, so registering a handler
    for TypeError can change the response but can never hide the framework fault from the
    operator."""
    with TestClient(HandledBreachApp()) as client, caplog.at_level(logging.ERROR, logger="jero"):
        resp = client.get("/lying-union")
    assert resp.status_code == 400  # the app's handler won, as it should
    assert "matches none of its declared union return types" in caplog.text
