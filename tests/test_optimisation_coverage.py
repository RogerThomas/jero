"""Coverage for the performance optimisation paths introduced in the lets-optimise branch.

Tests exercise: _parse_query edge cases, the bind_sync / bind_with_body / _finish
dispatch paths, multi-chunk ASGI body reassembly, dynamic-route static-segment
mismatch resolution, and the shared static-route path_values sentinel.
"""

import asyncio
from typing import Any

import pytest
from msgspec import Struct

from demo_app.auth import TokenAuth
from demo_app.models import User
from jero import BaseApp, Endpoint
from jero.core import _EMPTY_PATH_VALUES
from jero.testing import TestClient

# ---------------------------------------------------------------------------
# Shared auth helper
# ---------------------------------------------------------------------------

_AUTH = TokenAuth({"token": User(id="user-id", name="user-name")})
_AUTH_HEADER = {"authorization": "Bearer token"}


# ---------------------------------------------------------------------------
# Query-string parsing: empty values and percent / plus encoding
# ---------------------------------------------------------------------------


class SearchParams(Struct, rename="camel"):
    """Query params with a free-text search term and optional page."""

    q: str
    page: int = 1


class SearchResult(Struct, rename="camel"):
    """Echo of the bound search params."""

    q: str
    page: int


class SearchEndpoint(Endpoint, path="/search"):
    """Endpoint echoing parsed query params."""

    async def get(self, params: SearchParams) -> SearchResult:
        """Return the bound search params."""
        return SearchResult(q=params.q, page=params.page)


class SearchApp(BaseApp):
    """App wiring the search endpoint."""

    async def wire(self) -> None:
        self._include_endpoint(SearchEndpoint())


def test_query_param_with_plus_encoding() -> None:
    """A + in a query value is decoded as a space (unquote_plus path)."""
    with TestClient(SearchApp()) as client:
        resp = client.request("GET", "/search", params={"q": "hello world"})
        assert resp.status_code == 200
        assert resp.json()["q"] == "hello world"


def test_query_param_with_percent_encoding() -> None:
    """Percent-encoded query values are decoded correctly."""
    with TestClient(SearchApp()) as client:
        resp = client.request("GET", "/search", params={"q": "a&b=c"})
        assert resp.status_code == 200
        assert resp.json()["q"] == "a&b=c"


def test_query_param_empty_value_is_skipped() -> None:
    """A query pair with a blank value (key=) is silently skipped."""
    with TestClient(SearchApp()) as client:
        # The TestClient builds query_string from params via urlencode, but urlencode
        # preserves empty strings as key=, which triggers the skip in _parse_query.
        resp = client.request("GET", "/search", params={"q": "term", "empty": ""})
        assert resp.status_code == 200
        assert resp.json()["q"] == "term"


# ---------------------------------------------------------------------------
# _one: headers-only single-source handler (arity 1, headers path)
# ---------------------------------------------------------------------------


class TokenHeader(Struct):
    """Request headers carrying a single token."""

    x_token: str


class TokenReply(Struct):
    """Echo of the bound token header."""

    token: str


class HeadersOnlyEndpoint(Endpoint, path="/headers-only"):
    """Endpoint binding only typed request headers."""

    async def get(self, headers: TokenHeader) -> TokenReply:
        """Return the bound token header."""
        return TokenReply(token=headers.x_token)


class HeadersOnlyApp(BaseApp):
    """App wiring the headers-only endpoint."""

    async def wire(self) -> None:
        self._include_endpoint(HeadersOnlyEndpoint())


def test_headers_only_single_source() -> None:
    """A handler whose sole source is typed headers binds through the _one fast path."""
    with TestClient(HeadersOnlyApp()) as client:
        resp = client.get("/headers-only", headers={"x-token": "secret"})
        assert resp.status_code == 200
        assert resp.json() == {"token": "secret"}


# ---------------------------------------------------------------------------
# _finish: arity-0 handler behind auth (async binder path, returns None)
# ---------------------------------------------------------------------------


class Pong(Struct):
    """Acknowledgement body for the authed ping."""

    ok: bool


class AuthedPingEndpoint(Endpoint, path="/authed-ping"):
    """Arity-0 endpoint behind auth."""

    async def get(self) -> Pong:
        """Return a static acknowledgement."""
        return Pong(ok=True)


class AuthedPingApp(BaseApp):
    """App wiring the authed ping endpoint."""

    async def wire(self) -> None:
        self._include_endpoint(AuthedPingEndpoint(), auth=_AUTH)


def test_arity_zero_with_auth() -> None:
    """An arity-0 handler behind auth dispatches through the async binder."""
    with TestClient(AuthedPingApp()) as client:
        resp = client.get("/authed-ping", headers=_AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# _finish: multi-source with user + params (covers user and params kwargs)
# ---------------------------------------------------------------------------


class Filters(Struct):
    """Query params carrying a tag filter."""

    tag: str = "all"


class UserFiltersReply(Struct, rename="camel"):
    """Echo of the authenticated user name and bound filter."""

    user_name: str
    tag: str


class UserFiltersEndpoint(Endpoint, path="/user-filters"):
    """Endpoint binding both user and query params."""

    async def get(self, user: User, params: Filters) -> UserFiltersReply:
        """Return the user name and the tag filter."""
        return UserFiltersReply(user_name=user.name, tag=params.tag)


class UserFiltersApp(BaseApp):
    """App wiring the user-filters endpoint behind auth."""

    async def wire(self) -> None:
        self._include_endpoint(UserFiltersEndpoint(), auth=_AUTH)


def test_multi_source_user_and_params() -> None:
    """A handler binding both user and params populates both kwargs in _finish."""
    with TestClient(UserFiltersApp()) as client:
        resp = client.get("/user-filters", params={"tag": "featured"}, headers=_AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json() == {"userName": "user-name", "tag": "featured"}


# ---------------------------------------------------------------------------
# _finish: multi-source with content + path (covers content kwargs)
# ---------------------------------------------------------------------------


class ItemPath(Struct):
    """Path params carrying an item id."""

    item_id: str


class UploadResult(Struct, rename="camel"):
    """Echo of the item id and uploaded payload size."""

    item_id: str
    size: int


class UploadEndpoint(Endpoint, path="/items/{item_id}/upload"):
    """Endpoint binding both path and raw content bytes."""

    async def post(self, path: ItemPath, content: bytes) -> UploadResult:
        """Return the item id and the byte length of the uploaded content."""
        return UploadResult(item_id=path.item_id, size=len(content))


class UploadApp(BaseApp):
    """App wiring the upload endpoint."""

    async def wire(self) -> None:
        self._include_endpoint(UploadEndpoint())


def test_multi_source_content_and_path() -> None:
    """A handler binding both path and content populates both kwargs in _finish."""
    with TestClient(UploadApp()) as client:
        resp = client.post("/items/item-id/upload", content=b"payload")
        assert resp.status_code == 200
        assert resp.json() == {"itemId": "item-id", "size": 7}


# ---------------------------------------------------------------------------
# Dynamic-route resolution: static-segment mismatch triggers break
# ---------------------------------------------------------------------------


class APath(Struct):
    """Path params for the /a route."""

    id: str


class BPath(Struct):
    """Path params for the /b route."""

    id: str


class AReply(Struct):
    """Echo identifying which route matched."""

    source: str
    id: str


class AEndpoint(Endpoint, path="/a/{id}"):
    """Dynamic endpoint at /a/{id}."""

    async def get(self, path: APath) -> AReply:
        """Return the source tag and bound id."""
        return AReply(source="a", id=path.id)


class BEndpoint(Endpoint, path="/b/{id}"):
    """Dynamic endpoint at /b/{id}."""

    async def get(self, path: BPath) -> AReply:
        """Return the source tag and bound id."""
        return AReply(source="b", id=path.id)


class ABApp(BaseApp):
    """App wiring two dynamic endpoints with the same depth."""

    async def wire(self) -> None:
        self._include_endpoint(AEndpoint())
        self._include_endpoint(BEndpoint())


def test_dynamic_route_static_segment_mismatch() -> None:
    """When two dynamic routes share verb and depth, a static mismatch skips to the next."""
    with TestClient(ABApp()) as client:
        resp = client.get("/b/item-id")
        assert resp.status_code == 200
        assert resp.json() == {"source": "b", "id": "item-id"}

        resp = client.get("/a/item-id")
        assert resp.status_code == 200
        assert resp.json() == {"source": "a", "id": "item-id"}


# ---------------------------------------------------------------------------
# Multi-chunk ASGI body: the more_body path in _Route.__call__
# ---------------------------------------------------------------------------


class Echo(Struct):
    """Request/response body for the echo endpoint."""

    value: str


class EchoEndpoint(Endpoint, path="/echo"):
    """Endpoint echoing a JSON body."""

    async def post(self, json: Echo) -> Echo:
        """Return the body unchanged."""
        return json


class EchoApp(BaseApp):
    """App wiring the echo endpoint."""

    async def wire(self) -> None:
        self._include_endpoint(EchoEndpoint())


class _ChunkedReceive:
    """ASGI receive that yields a body split across two messages."""

    def __init__(self, chunk1: bytes, chunk2: bytes) -> None:
        self._chunks = iter(
            [
                {"type": "http.request", "body": chunk1, "more_body": True},
                {"type": "http.request", "body": chunk2, "more_body": False},
            ]
        )

    async def __call__(self) -> dict[str, Any]:
        return next(self._chunks)


class _CollectSend:
    """ASGI send that records every message."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


@pytest.mark.asyncio
async def test_multi_chunk_body() -> None:
    """A body arriving in multiple ASGI chunks is reassembled by the inlined reader."""
    app = EchoApp()
    to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    lifespan_task = asyncio.create_task(app({"type": "lifespan"}, to_app.get, from_app.put))
    await to_app.put({"type": "lifespan.startup"})
    msg = await from_app.get()
    assert msg["type"] == "lifespan.startup.complete"

    full_body = b'{"value":"chunked"}'
    mid = len(full_body) // 2
    receive = _ChunkedReceive(full_body[:mid], full_body[mid:])
    send = _CollectSend()

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/echo",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }

    await app(scope, receive, send)

    assert send.messages[0]["type"] == "http.response.start"
    assert send.messages[0]["status"] == 200
    assert send.messages[1]["type"] == "http.response.body"
    assert b'"chunked"' in send.messages[1]["body"]

    await to_app.put({"type": "lifespan.shutdown"})
    await from_app.get()
    await lifespan_task


# ---------------------------------------------------------------------------
# _EMPTY_PATH_VALUES: the shared static-route path_values sentinel
# ---------------------------------------------------------------------------


class Ping(Struct):
    """Acknowledgement body for the static ping route."""

    ok: bool


class PingEndpoint(Endpoint, path="/ping"):
    """Static endpoint (no path params) exercising the shared path_values sentinel."""

    async def get(self) -> Ping:
        """Return a static acknowledgement."""
        return Ping(ok=True)


class PingApp(BaseApp):
    """App wiring the static ping endpoint."""

    async def wire(self) -> None:
        self._include_endpoint(PingEndpoint())


async def _no_body_receive() -> dict[str, Any]:
    """ASGI receive for a bodyless GET — returns a disconnect if ever awaited."""
    return {"type": "http.disconnect"}


def test_empty_path_values_sentinel_is_read_only() -> None:
    """_EMPTY_PATH_VALUES rejects mutation instead of silently corrupting the shared
    sentinel every static route reuses (see BaseApp.__call__'s static-hit branch)."""
    assert _EMPTY_PATH_VALUES == {}
    with pytest.raises((TypeError, AttributeError)):
        _EMPTY_PATH_VALUES["x"] = "y"  # type: ignore[index]


@pytest.mark.asyncio
async def test_static_route_shares_path_values_dict_under_concurrency() -> None:
    """Many concurrent requests to a static route all reuse one shared, never-mutated
    path_values dict without corrupting or leaking state across requests."""
    app = PingApp()
    to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    lifespan_task = asyncio.create_task(app({"type": "lifespan"}, to_app.get, from_app.put))
    await to_app.put({"type": "lifespan.startup"})
    msg = await from_app.get()
    assert msg["type"] == "lifespan.startup.complete"

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/ping",
        "query_string": b"",
        "headers": [],
    }

    async def one_request() -> bool:
        send = _CollectSend()
        await app(dict(scope), _no_body_receive, send)
        return send.messages[0]["status"] == 200

    results = await asyncio.gather(*(one_request() for _ in range(2000)))
    assert all(results)
    assert _EMPTY_PATH_VALUES == {}

    await to_app.put({"type": "lifespan.shutdown"})
    await from_app.get()
    await lifespan_task
