"""The ``content_stream`` binding source: the raw request body, read incrementally.

The demo app's ``/uploads`` covers the blessed shape; small local apps cover re-framing,
the read-once rule, disconnects (driven through the raw ASGI interface, since a
``TestClient`` client never leaves mid-body), streaming responses that read the body as
they go, and the wiring contract.
"""

import asyncio
import hashlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import msgspec
import pytest
from msgspec import Struct

from demo_app.auth import TokenAuth
from demo_app.models import User
from jero import (
    BaseApp,
    ClientDisconnectedError,
    ContentStream,
    Endpoint,
    ExceptionResponse,
    Resource,
    StreamingResponse,
)
from jero.testing import TestClient

# ---------------------------------------------------------------------------
# The demo app's streamed upload
# ---------------------------------------------------------------------------


def test_upload_digests_a_single_chunk_body(client: TestClient) -> None:
    """A one-message body is hashed and sized."""
    resp = client.post("/uploads", content=b"content")

    assert resp.status_code == 200
    assert resp.json() == {"size": 7, "sha256": hashlib.sha256(b"content").hexdigest()}


def test_upload_digests_a_multi_chunk_body(client: TestClient) -> None:
    """A body split across messages hashes the same as the whole."""
    resp = client.post("/uploads", content=[b"con", b"", b"tent"])

    assert resp.json() == {"size": 7, "sha256": hashlib.sha256(b"content").hexdigest()}


def test_upload_accepts_an_empty_body(client: TestClient) -> None:
    """An empty body streams as no chunks at all."""
    resp = client.post("/uploads", content=b"")

    assert resp.json() == {"size": 0, "sha256": hashlib.sha256(b"").hexdigest()}


def test_upload_documents_a_binary_request_body(client: TestClient) -> None:
    """content_stream is documented as a binary body, exactly like content."""
    operation = client.get("/openapi.json").json()["paths"]["/uploads"]["post"]

    assert operation["requestBody"]["content"] == {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
    }


# ---------------------------------------------------------------------------
# Chunking: as delivered, and re-framed
# ---------------------------------------------------------------------------


class Chunks(Struct):
    """The chunks a handler saw, in order."""

    chunks: list[bytes]


class ChunkSize(Struct):
    """Query params choosing the re-framing size."""

    chunk_size: int


class ChunksEndpoint(Endpoint, path="/chunks"):
    """Echoes the chunks as delivered (POST) or re-framed to a size (PUT)."""

    async def post(self, content_stream: ContentStream) -> Chunks:
        """Collect the chunks exactly as the server delivered them."""
        return Chunks(chunks=[chunk async for chunk in content_stream])

    async def put(self, params: ChunkSize, content_stream: ContentStream) -> Chunks:
        """Collect the chunks re-framed to ``chunk_size``."""
        stream = content_stream.iter_chunks(chunk_size=params.chunk_size)
        return Chunks(chunks=[chunk async for chunk in stream])


class ReadTwiceEndpoint(Endpoint, path="/read-twice"):
    """Reads its body twice, which the stream forbids."""

    async def post(self, content_stream: ContentStream) -> Chunks:
        """Read the body, then try to read it again."""
        first = [chunk async for chunk in content_stream]
        second = [chunk async for chunk in content_stream]
        return Chunks(chunks=first + second)


class ChunksApp(BaseApp):
    """App wiring the chunking endpoints."""

    async def wire(self) -> None:
        self._include_endpoint(ChunksEndpoint())
        self._include_endpoint(ReadTwiceEndpoint())


def _decode_chunks(content: bytes) -> Chunks:
    return msgspec.json.decode(content, type=Chunks)


@pytest.fixture(name="chunks_client")
def _chunks_client() -> Iterator[TestClient]:
    with TestClient(ChunksApp()) as client:
        yield client


def test_iterating_yields_chunks_as_delivered_skipping_empty_ones(
    chunks_client: TestClient,
) -> None:
    """Plain iteration yields each server chunk unchanged, skipping empty ones."""
    resp = chunks_client.post("/chunks", content=[b"ab", b"", b"cdefg", b"h"])

    assert resp.status_code == 200
    assert _decode_chunks(resp.content) == Chunks(chunks=[b"ab", b"cdefg", b"h"])


@pytest.mark.parametrize(
    ("chunk_size", "expected"),
    [
        ("3", [b"abc", b"def", b"gh"]),
        ("1", [b"a", b"b", b"c", b"d", b"e", b"f", b"g", b"h"]),
        ("8", [b"abcdefgh"]),
        ("100", [b"abcdefgh"]),
    ],
)
def test_iter_chunks_reframes_to_a_fixed_size(
    chunks_client: TestClient, chunk_size: str, expected: list[bytes]
) -> None:
    """iter_chunks re-frames to chunk_size, with a shorter last chunk."""
    resp = chunks_client.put(
        "/chunks", params={"chunk_size": chunk_size}, content=[b"ab", b"cdefg", b"h"]
    )

    assert _decode_chunks(resp.content) == Chunks(chunks=expected)


def test_iter_chunks_of_an_empty_body_yields_nothing(chunks_client: TestClient) -> None:
    """Re-framing an empty body yields no chunks."""
    resp = chunks_client.put("/chunks", params={"chunk_size": "3"}, content=b"")

    assert _decode_chunks(resp.content) == Chunks(chunks=[])


@pytest.mark.parametrize("chunk_size", ["0", "-1"])
def test_iter_chunks_rejects_a_non_positive_size(
    chunks_client: TestClient, chunk_size: str
) -> None:
    """A non-positive chunk_size is a programming error: a 500."""
    resp = chunks_client.put("/chunks", params={"chunk_size": chunk_size}, content=b"content")

    assert resp.status_code == 500


def test_reading_the_stream_twice_is_an_error(chunks_client: TestClient) -> None:
    """The body can be read once; a second read is a programming error: a 500."""
    resp = chunks_client.post("/read-twice", content=b"content")

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Alongside other sources, and behind auth
# ---------------------------------------------------------------------------


class Receipt(Struct):
    """The id a body was stored under and its size."""

    id: str
    size: int
    user_id: str


class BlobPath(Struct):
    """The blob's id."""

    id: str


class BlobResource(Resource, path="/blobs"):
    """Stores a streamed body under a path id, behind auth."""

    async def update_full(
        self, path: BlobPath, user: User, content_stream: ContentStream
    ) -> Receipt:
        """Count the streamed body stored under ``path.id``."""
        size = 0
        async for chunk in content_stream:
            size += len(chunk)
        return Receipt(id=path.id, size=size, user_id=user.id)


class BlobApp(BaseApp):
    """App wiring the authed blob resource."""

    async def wire(self) -> None:
        auth = TokenAuth({"token": User(id="user-id", name="user-name")})
        self._include_resource(BlobResource(), auth=auth)


@pytest.fixture(name="blob_client")
def _blob_client() -> Iterator[TestClient]:
    with TestClient(BlobApp()) as client:
        yield client


def test_content_stream_binds_alongside_path_and_user(blob_client: TestClient) -> None:
    """content_stream combines with other sources and auth (the kwargs path)."""
    resp = blob_client.put(
        "/blobs/blob-id", content=[b"con", b"tent"], headers={"authorization": "Bearer token"}
    )

    assert resp.json() == {"id": "blob-id", "size": 7, "user_id": "user-id"}


def test_auth_runs_before_the_body_is_read(blob_client: TestClient) -> None:
    """A gating authenticator rejects before the handler ever reads the body."""
    resp = blob_client.put("/blobs/blob-id", content=b"content")

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Streaming responses that read the body as they go
# ---------------------------------------------------------------------------


class EchoEndpoint(Endpoint, path="/echo"):
    """Streams the request body straight back, upper-cased, as it arrives."""

    async def _upper(self, content_stream: ContentStream) -> AsyncIterator[bytes]:
        async for chunk in content_stream:
            yield chunk.upper()

    async def post(self, content_stream: ContentStream) -> StreamingResponse:
        """Echo the body back, one response chunk per request chunk."""
        return StreamingResponse(stream=self._upper(content_stream))


class EchoApp(BaseApp):
    """App wiring the echo endpoint."""

    async def wire(self) -> None:
        self._include_endpoint(EchoEndpoint())


def test_streaming_response_reading_the_body_sees_every_chunk() -> None:
    """The response's disconnect watcher must not swallow body chunks it isn't owed."""
    with TestClient(EchoApp()) as client:
        resp = client.post("/echo", content=[b"a", b"b", b"c", b"d"])

    assert resp.status_code == 200
    assert resp.content == b"ABCD"


# ---------------------------------------------------------------------------
# Client disconnects mid-body (raw ASGI: the client leaves partway through)
# ---------------------------------------------------------------------------


class Size(Struct):
    """A body's size."""

    size: int


class Cleanup:
    """Records whether a handler saw the disconnect and cleaned up."""

    def __init__(self) -> None:
        self.cleaned_up = False


class DisconnectEndpoint(Endpoint, path="/disconnect"):
    """Counts its body, cleaning up if the client leaves partway through."""

    def __init__(self, cleanup: Cleanup) -> None:
        self._cleanup = cleanup

    async def post(self, content_stream: ContentStream) -> Size:
        """Count the body; on a disconnect, record the cleanup and let it propagate."""
        size = 0
        try:
            async for chunk in content_stream:
                size += len(chunk)
        except ClientDisconnectedError:
            self._cleanup.cleaned_up = True
            raise
        return Size(size=size)


class DisconnectApp(BaseApp):
    """App wiring the disconnect and echo endpoints."""

    def __init__(self, cleanup: Cleanup) -> None:
        super().__init__()
        self._cleanup = cleanup

    async def wire(self) -> None:
        self._include_endpoint(DisconnectEndpoint(self._cleanup))
        self._include_endpoint(EchoEndpoint())


class _DisconnectingReceive:
    """ASGI receive: one body chunk with more to come, then the client leaves."""

    def __init__(self) -> None:
        messages: list[dict[str, Any]] = [
            {"type": "http.request", "body": b"content", "more_body": True},
            {"type": "http.disconnect"},
        ]
        self._messages = iter(messages)

    async def __call__(self) -> dict[str, Any]:
        return next(self._messages)


class _CollectSend:
    """ASGI send that records every message."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


@asynccontextmanager
async def _lifespan(app: BaseApp) -> AsyncGenerator[None]:
    """Drive ``app`` through ASGI lifespan startup/shutdown around a raw request."""
    to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(app({"type": "lifespan"}, to_app.get, from_app.put))
    await to_app.put({"type": "lifespan.startup"})
    assert (await from_app.get())["type"] == "lifespan.startup.complete"
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        await from_app.get()
        await task


async def _post_then_disconnect(app: BaseApp, path: str) -> list[dict[str, Any]]:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [],
    }
    send = _CollectSend()
    async with _lifespan(app):
        await app(scope, _DisconnectingReceive(), send)
    return send.messages


@pytest.mark.asyncio
async def test_disconnect_mid_body_sends_nothing_and_lets_the_handler_clean_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A client leaving mid-body gets no response and no log; the handler can clean up."""
    cleanup = Cleanup()

    with caplog.at_level(logging.ERROR, logger="jero"):
        messages = await _post_then_disconnect(DisconnectApp(cleanup), "/disconnect")

    assert messages == []
    assert cleanup.cleaned_up
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_disconnect_mid_body_ends_a_streaming_response_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A streaming response reading the body ends quietly when the client leaves."""
    with caplog.at_level(logging.ERROR, logger="jero"):
        messages = await _post_then_disconnect(DisconnectApp(Cleanup()), "/echo")

    # The first chunk was echoed; the disconnect then ends the stream without the final
    # empty body message a completed response sends, and without logging a fault.
    assert [message["type"] for message in messages] == [
        "http.response.start",
        "http.response.body",
    ]
    assert messages[1]["body"] == b"CONTENT"
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# The wiring contract
# ---------------------------------------------------------------------------


class Empty(Struct):
    """An empty body."""


class WrongAnnotationEndpoint(Endpoint, path="/wrong"):
    """Declares content_stream with the wrong type."""

    async def post(self, content_stream: bytes) -> Empty:
        """Never wires."""
        _ = content_stream
        return Empty()


class SyncEndpoint(Endpoint, path="/sync"):
    """Declares content_stream on a sync handler."""

    def post(self, content_stream: ContentStream) -> Empty:
        """Never wires."""
        _ = content_stream
        return Empty()


class GetEndpoint(Endpoint, path="/get"):
    """Declares content_stream on a bodyless verb."""

    async def get(self, content_stream: ContentStream) -> Empty:
        """Never wires."""
        _ = content_stream
        return Empty()


class TwoBodiesEndpoint(Endpoint, path="/two-bodies"):
    """Declares content_stream alongside a JSON body."""

    async def post(self, json: Empty, content_stream: ContentStream) -> Empty:
        """Never wires."""
        _ = content_stream
        return json


class WiringApp(BaseApp):
    """App wiring one endpoint that should fail wiring."""

    def __init__(self, endpoint: Endpoint) -> None:
        super().__init__()
        self._endpoint = endpoint

    async def wire(self) -> None:
        self._include_endpoint(self._endpoint)


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        (WrongAnnotationEndpoint(), "'content_stream' must be annotated as ContentStream"),
        (SyncEndpoint(), "a handler taking 'content_stream' must be async"),
        (GetEndpoint(), "GET handlers cannot take 'content_stream'"),
        (
            TwoBodiesEndpoint(),
            "only one of 'json', 'content', 'content_stream', or 'form' is allowed",
        ),
    ],
)
def test_wiring_rejects_a_misdeclared_content_stream(endpoint: Endpoint, message: str) -> None:
    """A misdeclared content_stream fails loud at startup."""
    with pytest.raises(RuntimeError, match=message), TestClient(WiringApp(endpoint)):
        pass


class DisconnectHandler:
    """Tries to handle a disconnect, which has no one to answer."""

    def handle_exception(self, exception: ClientDisconnectedError) -> ExceptionResponse[Empty]:
        """Never registers."""
        _ = exception
        return ExceptionResponse(status_code=500, json=Empty())


class DisconnectHandlerApp(BaseApp):
    """App registering a handler for ClientDisconnectedError."""

    async def wire(self) -> None:
        self._include_exception_handler(DisconnectHandler())


def test_wiring_rejects_a_handler_for_client_disconnects() -> None:
    """A handler for ClientDisconnectedError could never run, so registering one fails."""
    with (
        pytest.raises(RuntimeError, match="ClientDisconnectedError cannot be handled"),
        TestClient(DisconnectHandlerApp()),
    ):
        pass
