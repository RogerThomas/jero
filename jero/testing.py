"""In-process test client for jero apps.

Drives the ASGI app directly — no socket, no server. Runs the app's lifespan (so ``wire`` registers
resources/endpoints and the dependency context stays open) on a dedicated background event loop, and
exposes a synchronous, requests-style API:

    from jero import TestClient

    with TestClient(app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

The lifespan starts on construction, so routes are live immediately; use the context manager (or
``close()``) for deterministic shutdown.
"""

import asyncio
import contextlib
import queue
import threading
from collections.abc import Callable, Coroutine, Sequence
from contextlib import AsyncExitStack, ExitStack
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import urlencode

from jero.codecs import msgspec_decoder, msgspec_encoder
from jero.core import BaseApp, BaseFactory, instantiate_factory

type _DataValue = str | bytes
type _DataValues = _DataValue | list[_DataValue]
type _FileValue = tuple[str | None, bytes] | tuple[str | None, bytes, str]
type _FileValues = _FileValue | list[_FileValue]


@dataclass(slots=True)
class TestResponse:
    """A captured HTTP response: status code, headers, and body bytes."""

    __test__ = False  # stop pytest from collecting this as a test case

    status_code: int
    headers: dict[str, str]
    content: bytes
    # Every header pair as sent, repeats included; ``headers`` collapses duplicates.
    multi_headers: list[tuple[str, str]]

    @property
    def text(self) -> str:
        """The response body decoded as UTF-8 text."""
        return self.content.decode()

    def json(self) -> Any:
        """The response body decoded as JSON."""
        return msgspec_decoder.decode(self.content)


@dataclass(frozen=True, slots=True)
class TestSSEEvent:
    """One decoded Server-Sent Event captured from a streaming response."""

    __test__ = False

    data: Any
    event: str | None = None
    id: str | None = None
    retry: int | None = None


class _RequestCycle:
    """Drives one ASGI request: feeds the body once, collects the response."""

    __slots__ = ("_body", "_closed", "_sent", "chunks", "headers", "multi_headers", "status")

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._closed = asyncio.Event()
        self._sent = False
        self.status = 0
        self.headers: dict[str, str] = {}
        self.multi_headers: list[tuple[str, str]] = []
        self.chunks: list[bytes] = []

    async def receive(self) -> dict[str, Any]:
        """Feed the request body once; report disconnect on later calls."""
        if self._sent:
            await self._closed.wait()
            return {"type": "http.disconnect"}
        self._sent = True
        return {"type": "http.request", "body": self._body, "more_body": False}

    async def send(self, message: dict[str, Any]) -> None:
        """Record a response start (status/headers) or body message."""
        if message["type"] == "http.response.start":
            self.status = message["status"]
            pairs = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in message["headers"]]
            self.multi_headers += pairs
            self.headers |= pairs
        elif message["type"] == "http.response.body":
            self.chunks.append(message.get("body", b""))


class _StreamCycle:
    """One ASGI streaming request with a sync queue for response chunks."""

    __slots__ = ("_body", "_receive", "_sent", "chunks")

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._receive: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._sent = False
        self.chunks: queue.Queue[dict[str, Any]] = queue.Queue()

    async def disconnect(self) -> None:
        """Queue an ``http.disconnect`` for the app to receive."""
        await self._receive.put({"type": "http.disconnect"})

    async def receive(self) -> dict[str, Any]:
        """Feed the request body once, then await queued client messages."""
        if not self._sent:
            self._sent = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        return await self._receive.get()

    async def send(self, message: dict[str, Any]) -> None:
        """Hand a response message to the sync chunk queue."""
        self.chunks.put(message)


class _StreamSession:
    __test__ = False

    def __init__(
        self,
        submit: Callable[[Coroutine[Any, Any, Any]], Any],
        cycle: _StreamCycle,
        task: asyncio.Task[None],
        status_code: int,
        headers: dict[str, str],
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self._submit = submit
        self._cycle = cycle
        self._task = task
        self._buffer = b""
        self._closed = False

    async def _wait_task(self) -> None:
        await self._task

    def _decode_sse(self, raw: bytes) -> TestSSEEvent:
        event: str | None = None
        event_id: str | None = None
        retry: int | None = None
        data_lines: list[str] = []
        for line in raw.decode().splitlines():
            if line.startswith(":"):
                continue
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("id: "):
                event_id = line[4:]
            elif line.startswith("retry: "):
                retry = int(line[7:])
            elif line.startswith("data: "):
                data_lines.append(line[6:])
        data_text = "\n".join(data_lines)
        try:
            data: Any = msgspec_decoder.decode(data_text.encode())
        except ValueError:
            data = data_text
        return TestSSEEvent(data=data, event=event, id=event_id, retry=retry)

    def _decode(self, raw: bytes) -> Any:
        content_type = self.headers["content-type"].split(";")[0]
        if content_type == "application/x-ndjson":
            return msgspec_decoder.decode(raw.rstrip(b"\n"))
        if content_type == "text/event-stream":
            return self._decode_sse(raw)
        return raw

    def close(self) -> None:
        """Disconnect the stream and wait for the app task to finish."""
        if self._closed:
            return
        self._closed = True
        self._submit(self._cycle.disconnect())
        with contextlib.suppress(Exception):
            self._submit(self._wait_task())

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> Any:
        while True:
            message = self._cycle.chunks.get()
            if message["type"] != "http.response.body":
                continue
            body = message.get("body", b"")
            if not message.get("more_body"):
                self._closed = True
                self._submit(self._wait_task())
                raise StopIteration
            if body:
                return self._decode(body)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _LoopThread:
    """A dedicated asyncio loop on its own daemon thread.

    Lets synchronous helpers drive async code — and async teardown — on one
    stable loop, so resources opened on it are closed on the same loop. Shared
    by ``TestClient`` and ``FactoryHarness``.
    """

    __slots__ = ("_loop", "_thread")

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def submit[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine on the loop from the calling thread and return its result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def close(self) -> None:
        """Stop the loop and join its thread."""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()


class TestClient:
    """Synchronous in-process client. Prefer ``with TestClient(app) as c``."""

    __test__ = False  # stop pytest from collecting this as a test case

    def __init__(self, app: BaseApp[Any]) -> None:
        self._app = app
        self._loop_thread = _LoopThread()
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._lifespan_task: asyncio.Task[None]
        try:
            self._submit(self._start_lifespan())
        except BaseException:
            self._loop_thread.close()
            raise

    def _submit[T](self, coro: Coroutine[Any, Any, T]) -> T:
        return self._loop_thread.submit(coro)

    @staticmethod
    def _part_content(value: str | bytes) -> bytes:
        return value if isinstance(value, bytes) else value.encode()

    @staticmethod
    def _disposition(name: str, filename: str | None = None) -> bytes:
        escaped_name = name.replace("\\", "\\\\").replace('"', '\\"')
        value = f'Content-Disposition: form-data; name="{escaped_name}"'
        if filename is not None:
            escaped_filename = filename.replace("\\", "\\\\").replace('"', '\\"')
            value += f'; filename="{escaped_filename}"'
        return value.encode()

    @staticmethod
    def _iter_data_values(value: _DataValues) -> Sequence[_DataValue]:
        return value if isinstance(value, list) else [value]

    @staticmethod
    def _iter_file_values(value: _FileValues) -> Sequence[_FileValue]:
        return value if isinstance(value, list) else [value]

    def _encode_multipart(
        self,
        data: dict[str, _DataValues] | None,
        files: dict[str, _FileValues] | None,
    ) -> tuple[bytes, str]:
        boundary = "jero-test-boundary"
        chunks: list[bytes] = []
        for name, value in (data or {}).items():
            for item in self._iter_data_values(value):
                chunks += [
                    f"--{boundary}\r\n".encode(),
                    self._disposition(name),
                    b"\r\n\r\n",
                    self._part_content(item),
                    b"\r\n",
                ]
        for name, value in (files or {}).items():
            for item in self._iter_file_values(value):
                filename, content = item[:2]
                content_type = item[2] if len(item) == 3 else None
                chunks += [
                    f"--{boundary}\r\n".encode(),
                    self._disposition(name, filename),
                    b"\r\n",
                ]
                if content_type is not None:
                    chunks += [f"Content-Type: {content_type}\r\n".encode()]
                chunks += [b"\r\n", content, b"\r\n"]
        chunks.append(f"--{boundary}--\r\n".encode())
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    async def _start_lifespan(self) -> None:
        self._lifespan_task = asyncio.create_task(
            self._app({"type": "lifespan"}, self._to_app.get, self._from_app.put)
        )
        await self._to_app.put({"type": "lifespan.startup"})
        message = await self._from_app.get()
        if message["type"] == "lifespan.startup.failed":
            # The app re-raises after reporting; retrieve it so asyncio
            # doesn't warn about an unretrieved task exception.
            with contextlib.suppress(Exception):
                await self._lifespan_task
            raise RuntimeError(f"lifespan startup failed: {message.get('message')}")

    async def _stop_lifespan(self) -> None:
        await self._to_app.put({"type": "lifespan.shutdown"})
        await self._from_app.get()
        await self._lifespan_task

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None,
        json: Any,
        content: bytes | None,
        data: dict[str, _DataValues] | None,
        files: dict[str, _FileValues] | None,
        headers: dict[str, str] | None,
    ) -> TestResponse:
        body = b""
        wire_headers = {k.lower(): v for k, v in (headers or {}).items()}
        if json is not None:
            body = msgspec_encoder.encode(json)
            wire_headers.setdefault("content-type", "application/json")
        elif content is not None:
            body = content
            wire_headers.setdefault("content-type", "application/octet-stream")
        elif data is not None or files is not None:
            body, content_type = self._encode_multipart(data, files)
            wire_headers.setdefault("content-type", content_type)

        scope: dict[str, Any] = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": urlencode(params or {}).encode("latin-1"),
            "headers": [
                (k.encode("latin-1"), v.encode("latin-1")) for k, v in wire_headers.items()
            ],
        }

        cycle = _RequestCycle(body)
        await self._app(scope, cycle.receive, cycle.send)
        return TestResponse(
            status_code=cycle.status,
            headers=cycle.headers,
            content=b"".join(cycle.chunks),
            multi_headers=cycle.multi_headers,
        )

    async def _stream_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None,
        json: Any,
        content: bytes | None,
        data: dict[str, _DataValues] | None,
        files: dict[str, _FileValues] | None,
        headers: dict[str, str] | None,
    ) -> _StreamSession:
        body = b""
        wire_headers = {k.lower(): v for k, v in (headers or {}).items()}
        if json is not None:
            body = msgspec_encoder.encode(json)
            wire_headers.setdefault("content-type", "application/json")
        elif content is not None:
            body = content
            wire_headers.setdefault("content-type", "application/octet-stream")
        elif data is not None or files is not None:
            body, content_type = self._encode_multipart(data, files)
            wire_headers.setdefault("content-type", content_type)

        scope: dict[str, Any] = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": urlencode(params or {}).encode("latin-1"),
            "headers": [
                (k.encode("latin-1"), v.encode("latin-1")) for k, v in wire_headers.items()
            ],
        }
        cycle = _StreamCycle(body)
        task = asyncio.create_task(self._app(scope, cycle.receive, cycle.send))
        while True:
            message = await asyncio.to_thread(cycle.chunks.get)
            if message["type"] == "http.response.start":
                headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in message["headers"]}
                return _StreamSession(self._submit, cycle, task, message["status"], headers)
            cycle.chunks.put(message)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        data: dict[str, _DataValues] | None = None,
        files: dict[str, _FileValues] | None = None,
        headers: dict[str, str] | None = None,
    ) -> TestResponse:
        """Issue a request and return the buffered response."""
        return self._submit(
            self._request(
                method.upper(),
                path,
                params=params,
                json=json,
                content=content,
                data=data,
                files=files,
                headers=headers,
            )
        )

    def stream_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        data: dict[str, _DataValues] | None = None,
        files: dict[str, _FileValues] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _StreamSession:
        """Issue a request and return a streaming session for its chunks."""
        return self._submit(
            self._stream_request(
                method.upper(),
                path,
                params=params,
                json=json,
                content=content,
                data=data,
                files=files,
                headers=headers,
            )
        )

    def get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> TestResponse:
        """Issue a GET request."""
        return self.request("GET", path, params=params, headers=headers)

    def stream_get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _StreamSession:
        """Open a streaming GET request."""
        return self.stream_request("GET", path, params=params, headers=headers)

    def head(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> TestResponse:
        """Issue a HEAD request."""
        return self.request("HEAD", path, params=params, headers=headers)

    def options(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> TestResponse:
        """Issue an OPTIONS request."""
        return self.request("OPTIONS", path, params=params, headers=headers)

    def delete(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> TestResponse:
        """Issue a DELETE request."""
        return self.request("DELETE", path, params=params, headers=headers)

    def stream_delete(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _StreamSession:
        """Open a streaming DELETE request."""
        return self.stream_request("DELETE", path, params=params, headers=headers)

    def post(
        self,
        path: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        data: dict[str, _DataValues] | None = None,
        files: dict[str, _FileValues] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> TestResponse:
        """Issue a POST request (JSON, raw bytes, or multipart form)."""
        return self.request(
            "POST",
            path,
            json=json,
            content=content,
            data=data,
            files=files,
            params=params,
            headers=headers,
        )

    def stream_post(
        self,
        path: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        data: dict[str, _DataValues] | None = None,
        files: dict[str, _FileValues] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _StreamSession:
        """Open a streaming POST request."""
        return self.stream_request(
            "POST",
            path,
            json=json,
            content=content,
            data=data,
            files=files,
            params=params,
            headers=headers,
        )

    def put(
        self,
        path: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        data: dict[str, _DataValues] | None = None,
        files: dict[str, _FileValues] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> TestResponse:
        """Issue a PUT request (JSON, raw bytes, or multipart form)."""
        return self.request(
            "PUT",
            path,
            json=json,
            content=content,
            data=data,
            files=files,
            params=params,
            headers=headers,
        )

    def stream_put(
        self,
        path: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        data: dict[str, _DataValues] | None = None,
        files: dict[str, _FileValues] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _StreamSession:
        """Open a streaming PUT request."""
        return self.stream_request(
            "PUT",
            path,
            json=json,
            content=content,
            data=data,
            files=files,
            params=params,
            headers=headers,
        )

    def patch(
        self,
        path: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        data: dict[str, _DataValues] | None = None,
        files: dict[str, _FileValues] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> TestResponse:
        """Issue a PATCH request (JSON, raw bytes, or multipart form)."""
        return self.request(
            "PATCH",
            path,
            json=json,
            content=content,
            data=data,
            files=files,
            params=params,
            headers=headers,
        )

    def stream_patch(
        self,
        path: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        data: dict[str, _DataValues] | None = None,
        files: dict[str, _FileValues] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _StreamSession:
        """Open a streaming PATCH request."""
        return self.stream_request(
            "PATCH",
            path,
            json=json,
            content=content,
            data=data,
            files=files,
            params=params,
            headers=headers,
        )

    def close(self) -> None:
        """Run the app's lifespan shutdown and stop the background loop."""
        self._submit(self._stop_lifespan())
        self._loop_thread.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FactoryHarness[FactoryT: BaseFactory]:
    """Build a factory in isolation and exercise its ``create_*`` methods.

    The factory-level sibling of :class:`TestClient`. It owns the exit stacks the
    factory writes to via ``enter`` / ``aenter`` and runs async ``create_*``
    methods on a background loop, so services are built — and their resources
    opened and torn down — exactly as under a live app, but with no app, routes,
    or server. Use it to test the real factory wiring that an app's ``factory=``
    seam mocks away.

        with FactoryHarness(Factory) as harness:
            service = harness.run(harness.factory.create_widget_service())
            assert isinstance(service, WidgetService)
        # everything opened on the stacks is closed here

    Synchronous ``create_*`` methods can be called directly on ``harness.factory``;
    ``run`` awaits the async ones on the harness's loop.
    """

    def __init__(self, factory_cls: type[FactoryT]) -> None:
        self._loop_thread = _LoopThread()
        self._stack = ExitStack()
        self._astack = AsyncExitStack()
        self.factory: FactoryT = instantiate_factory(factory_cls, self._stack, self._astack)

    def run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Await an async ``create_*`` coroutine on the harness's loop."""
        return self._loop_thread.submit(coro)

    async def _close_stacks(self) -> None:
        await self._astack.aclose()
        self._stack.close()

    def close(self) -> None:
        """Close everything the factory opened on its exit stacks, then stop the loop."""
        self._loop_thread.submit(self._close_stacks())
        self._loop_thread.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
