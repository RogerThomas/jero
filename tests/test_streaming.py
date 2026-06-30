"""Streaming response contract (pyright venv check)."""

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from dataclasses import dataclass

import pytest
from msgspec import Struct

from jero import (
    BaseApp,
    Endpoint,
    HTTPError,
    NDJSONStreamingResponse,
    ServerSentEvent,
    SSEResponse,
    StreamingResponse,
    TestClient,
)


class Item(Struct):
    """A streamed payload item."""

    name: str


@dataclass
class StreamState:
    """Mutable flags recording stream lifecycle events for assertions."""

    closed: bool = False
    iterated: bool = False


class NDJSONEndpoint(Endpoint, path="/stream"):
    """Endpoint streaming a finite sequence of items as NDJSON."""

    async def _items(self) -> AsyncIterator[Item]:
        yield Item(name="one")
        yield Item(name="two")

    async def get(self) -> NDJSONStreamingResponse[Item]:
        """Return an NDJSON stream of items."""
        return NDJSONStreamingResponse(stream=self._items())


class BytesEndpoint(Endpoint, path="/stream"):
    """Endpoint streaming raw byte chunks with a custom content type."""

    async def _chunks(self) -> AsyncIterator[bytes]:
        yield b"a,"
        yield b"b\n"

    async def get(self) -> StreamingResponse:
        """Return a byte stream with a CSV content type."""
        return StreamingResponse(stream=self._chunks(), raw_headers={"content-type": "text/csv"})


class SSEEndpoint(Endpoint, path="/stream"):
    """Endpoint streaming server-sent events, including a typed event."""

    async def _events(self) -> AsyncIterator[Item | ServerSentEvent[Item]]:
        yield Item(name="one")
        yield ServerSentEvent(data=Item(name="two"), event="created", id="2", retry=1000)

    async def get(self) -> SSEResponse[Item]:
        """Return an SSE stream of items and a typed event."""
        return SSEResponse(stream=self._events())


@dataclass
class LifecycleEndpoint(Endpoint, path="/stream"):
    """Endpoint whose stream records teardown on disconnect."""

    _state: StreamState

    async def _events(self) -> AsyncIterator[str]:
        yield "ready"
        while True:
            await asyncio.sleep(60)

    async def _lifecycle(self) -> AsyncGenerator[AsyncIterable[str]]:
        yield self._events()
        self._state.closed = True

    async def get(self) -> SSEResponse[str]:
        """Return an SSE stream guarded by lifecycle teardown."""
        return SSEResponse(stream=self._lifecycle())


@dataclass
class NDJSONLifecycleEndpoint(Endpoint, path="/stream"):
    """NDJSON endpoint whose stream records teardown on disconnect."""

    _state: StreamState

    async def _items(self) -> AsyncIterator[Item]:
        yield Item(name="ready")
        while True:
            await asyncio.sleep(60)

    async def _lifecycle(self) -> AsyncGenerator[AsyncIterable[Item]]:
        yield self._items()
        self._state.closed = True

    async def get(self) -> NDJSONStreamingResponse[Item]:
        """Return an NDJSON stream guarded by lifecycle teardown."""
        return NDJSONStreamingResponse(stream=self._lifecycle())


@dataclass
class ErrorStreamEndpoint(Endpoint, path="/stream"):
    """Endpoint whose stream raises mid-iteration; teardown must still run."""

    _state: StreamState

    async def _items(self) -> AsyncIterator[Item]:
        yield Item(name="ready")
        raise RuntimeError("stream boom")

    async def _lifecycle(self) -> AsyncGenerator[AsyncIterable[Item]]:
        yield self._items()
        # Plain teardown after the yield, no try/finally: the framework guarantees
        # it runs even though _items() raised mid-stream.
        self._state.closed = True

    async def get(self) -> NDJSONStreamingResponse[Item]:
        """Return an NDJSON stream that raises after its first item."""
        return NDJSONStreamingResponse(stream=self._lifecycle())


class SetupErrorEndpoint(Endpoint, path="/stream"):
    """Endpoint that raises during stream setup before any item is yielded."""

    async def _items(self) -> AsyncIterator[Item]:
        yield Item(name="never")

    async def _lifecycle(self) -> AsyncGenerator[AsyncIterable[Item]]:
        raise HTTPError(418, "setup failed")
        # Unreachable, but required to make this an async generator: setup raises
        # before any item is produced.
        yield self._items()  # pylint: disable=unreachable

    async def get(self) -> NDJSONStreamingResponse[Item]:
        """Return an NDJSON stream whose setup raises an HTTP error."""
        return NDJSONStreamingResponse(stream=self._lifecycle())


class UnexpectedSetupErrorEndpoint(Endpoint, path="/stream"):
    """Endpoint whose stream setup raises an unexpected (non-HTTP) error."""

    async def _items(self) -> AsyncIterator[Item]:
        yield Item(name="never")

    async def _lifecycle(self) -> AsyncGenerator[AsyncIterable[Item]]:
        raise RuntimeError("setup boom")
        yield self._items()  # pylint: disable=unreachable

    async def get(self) -> NDJSONStreamingResponse[Item]:
        """Return an NDJSON stream whose setup raises an unexpected error."""
        return NDJSONStreamingResponse(stream=self._lifecycle())


class TeardownErrorEndpoint(Endpoint, path="/stream"):
    """Endpoint whose stream completes normally but whose teardown then raises."""

    async def _items(self) -> AsyncIterator[Item]:
        yield Item(name="ready")

    async def _lifecycle(self) -> AsyncGenerator[AsyncIterable[Item]]:
        yield self._items()
        raise RuntimeError("teardown boom")

    async def get(self) -> NDJSONStreamingResponse[Item]:
        """Return an NDJSON stream whose post-yield teardown raises."""
        return NDJSONStreamingResponse(stream=self._lifecycle())


@dataclass
class HeadEndpoint(Endpoint, path="/stream"):
    """Endpoint recording whether its stream body was iterated."""

    _state: StreamState

    async def _items(self) -> AsyncIterator[Item]:
        self._state.iterated = True
        yield Item(name="never")

    async def get(self) -> NDJSONStreamingResponse[Item]:
        """Return an NDJSON stream that flags iteration when consumed."""
        return NDJSONStreamingResponse(stream=self._items())


class BadSSEEndpoint(Endpoint, path="/stream"):
    """Endpoint illegally returning an SSE response from POST."""

    async def _events(self) -> AsyncIterator[str]:
        yield "never"

    async def post(self) -> SSEResponse[str]:
        """POST handler that illegally returns an SSE response."""
        return SSEResponse(stream=self._events())


class BareSSEEndpoint(Endpoint, path="/stream"):
    """SSE endpoint using the bare (str-default) SSEResponse, no type parameters."""

    async def _events(self) -> AsyncIterator[str]:
        yield "tick"

    async def get(self) -> SSEResponse:
        """Return a plain-string SSE stream via the unparameterized response."""
        return SSEResponse(stream=self._events())


class KeepaliveEndpoint(Endpoint, path="/stream"):
    """Endpoint emitting SSE keepalive comments on an idle stream."""

    async def _events(self) -> AsyncIterator[str]:
        while True:
            await asyncio.sleep(60)
            yield "never"

    async def _lifecycle(self) -> AsyncGenerator[AsyncIterable[str]]:
        yield self._events()

    async def get(self) -> SSEResponse[str]:
        """Return an SSE stream configured with a short keepalive interval."""
        return SSEResponse(stream=self._lifecycle(), keepalive=0.01)


class _EndpointApp(BaseApp):
    def __init__(self, endpoint: Endpoint) -> None:
        self._endpoint = endpoint
        super().__init__()

    async def wire(self) -> None:
        self.include_endpoint(self._endpoint)


def test_finite_ndjson_stream() -> None:
    """A finite NDJSON stream yields each item as a decoded object."""
    with TestClient(_EndpointApp(NDJSONEndpoint())) as client:
        assert list(client.stream_get("/stream")) == [{"name": "one"}, {"name": "two"}]


def test_bytes_stream() -> None:
    """A byte stream preserves its custom content type and chunk boundaries."""
    with TestClient(_EndpointApp(BytesEndpoint())) as client:
        stream = client.stream_get("/stream")
        assert stream.headers["content-type"] == "text/csv"
        assert list(stream) == [b"a,", b"b\n"]


def test_sse_events() -> None:
    """An SSE stream yields data and the event, id, and retry fields of typed events."""
    with TestClient(_EndpointApp(SSEEndpoint())) as client:
        events = list(client.stream_get("/stream"))
        assert events[0].data == {"name": "one"}
        assert events[1].data == {"name": "two"}
        assert events[1].event == "created"
        assert events[1].id == "2"
        assert events[1].retry == 1000


def test_disconnect_runs_lifecycle_teardown() -> None:
    """Disconnecting mid-stream runs the stream's lifecycle teardown."""
    state = StreamState()
    with TestClient(_EndpointApp(LifecycleEndpoint(state))) as client:
        with client.stream_get("/stream") as events:
            assert next(events).data == "ready"
        assert state.closed


def test_ndjson_disconnect_runs_lifecycle_teardown() -> None:
    """Disconnecting mid-stream runs the NDJSON stream's lifecycle teardown."""
    state = StreamState()
    with TestClient(_EndpointApp(NDJSONLifecycleEndpoint(state))) as client:
        with client.stream_get("/stream") as events:
            assert next(events) == {"name": "ready"}
        assert state.closed


def test_error_in_stream_still_runs_lifecycle_teardown() -> None:
    """An error raised inside the stream still runs the plain post-yield teardown."""
    state = StreamState()
    with TestClient(_EndpointApp(ErrorStreamEndpoint(state))) as client:
        with client.stream_get("/stream") as events:
            assert next(events) == {"name": "ready"}
        # _items() raised after "ready"; the framework still resumed _lifecycle past
        # its yield, so the teardown line ran despite the error.
        assert state.closed


def test_setup_error_is_normal_error_response() -> None:
    """An error raised during stream setup becomes a normal error response."""
    with TestClient(_EndpointApp(SetupErrorEndpoint())) as client:
        resp = client.get("/stream")
        assert resp.status_code == 418
        assert resp.json() == {"error": "setup failed"}


def test_mid_stream_error_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A stream that faults mid-iteration logs the traceback (it's swallowed otherwise)."""
    state = StreamState()
    # Read the first item; the server then pulls the next one, which raises (the error path
    # returns without a stream terminator, so don't consume to completion).
    with (
        caplog.at_level(logging.ERROR, logger="jero"),
        TestClient(_EndpointApp(ErrorStreamEndpoint(state))) as client,
        client.stream_get("/stream") as events,
    ):
        assert next(events) == {"name": "ready"}
    assert "error streaming response for GET /stream" in caplog.text
    assert "stream boom" in caplog.text
    assert any(record.exc_info for record in caplog.records)


def test_unexpected_setup_error_is_500_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A non-HTTP error during stream setup becomes a 500 and is logged (HTTPError isn't)."""
    with (
        caplog.at_level(logging.ERROR, logger="jero"),
        TestClient(_EndpointApp(UnexpectedSetupErrorEndpoint())) as client,
    ):
        resp = client.get("/stream")
    assert resp.status_code == 500
    assert resp.json() == {"error": "internal server error"}
    assert "error opening stream for GET /stream" in caplog.text
    assert "setup boom" in caplog.text


def test_stream_teardown_error_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A teardown failure after the stream completes is logged (still swallowed)."""
    with (
        caplog.at_level(logging.ERROR, logger="jero"),
        TestClient(_EndpointApp(TeardownErrorEndpoint())) as client,
    ):
        # The stream itself completes cleanly, so it's safe to consume to the end.
        assert list(client.stream_get("/stream")) == [{"name": "ready"}]
    assert "error tearing down stream for GET /stream" in caplog.text
    assert "teardown boom" in caplog.text


def test_head_skips_stream_iteration() -> None:
    """A HEAD request returns stream headers without iterating the stream body."""
    state = StreamState()
    with TestClient(_EndpointApp(HeadEndpoint(state))) as client:
        resp = client.head("/stream")
        assert resp.status_code == 200
        assert resp.content == b""
        assert resp.headers["content-type"] == "application/x-ndjson"
        assert not state.iterated


def test_sse_on_post_is_wiring_error() -> None:
    """Returning an SSE response from POST fails at startup."""
    with pytest.raises(RuntimeError, match="SSEResponse is only allowed on GET"):
        TestClient(_EndpointApp(BadSSEEndpoint()))


def test_bare_sse_response_streams_strings() -> None:
    """A bare SSEResponse (str default) streams plain-string events."""
    with (
        TestClient(_EndpointApp(BareSSEEndpoint())) as client,
        client.stream_get("/stream") as events,
    ):
        assert next(events).data == "tick"


def test_sse_keepalive() -> None:
    """An idle SSE stream emits an empty keepalive event."""
    with (
        TestClient(_EndpointApp(KeepaliveEndpoint())) as client,
        client.stream_get("/stream") as events,
    ):
        assert next(events).data == ""
