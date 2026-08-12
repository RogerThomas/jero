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
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Self, cast
from urllib.parse import urlencode

from msgspec.json import Decoder
from typing_extensions import TypeForm

from jero.codecs import msgspec_decoder, msgspec_encoder
from jero.core import BaseApp, BaseFactory
from jero.websockets import _payload_kind

type _DataValue = str | bytes
type _DataValues = _DataValue | list[_DataValue]
type _FileValue = tuple[str | None, bytes] | tuple[str | None, bytes, str]
type _FileValues = _FileValue | list[_FileValue]


@dataclass(frozen=True, slots=True)
class TestCookie:
    """One parsed ``Set-Cookie`` response header. Attribute names parse
    case-insensitively; ``expires`` is the raw wire value, unparsed."""

    __test__ = False

    value: str
    max_age: int | None = None
    expires: str | None = None
    path: str | None = None
    domain: str | None = None
    secure: bool = False
    http_only: bool = False
    same_site: str | None = None
    partitioned: bool = False


def _parse_set_cookie(header_value: str) -> tuple[str, TestCookie]:
    """One ``Set-Cookie`` header value as its cookie name and parsed :class:`TestCookie`."""
    name_pair, *attr_parts = header_value.split(";")
    name, _, value = name_pair.strip().partition("=")
    max_age: int | None = None
    expires: str | None = None
    path: str | None = None
    domain: str | None = None
    secure = False
    http_only = False
    same_site: str | None = None
    partitioned = False
    for part in attr_parts:
        attr_name, _, attr_value = part.strip().partition("=")
        lower = attr_name.lower()
        if lower == "max-age":
            max_age = int(attr_value)
        elif lower == "expires":
            expires = attr_value
        elif lower == "path":
            path = attr_value
        elif lower == "domain":
            domain = attr_value
        elif lower == "secure":
            secure = True
        elif lower == "httponly":
            http_only = True
        elif lower == "samesite":
            same_site = attr_value
        elif lower == "partitioned":
            partitioned = True
    return name.strip(), TestCookie(
        value=value,
        max_age=max_age,
        expires=expires,
        path=path,
        domain=domain,
        secure=secure,
        http_only=http_only,
        same_site=same_site,
        partitioned=partitioned,
    )


def _cookie_is_expired(cookie: TestCookie) -> bool:
    """Whether ``cookie`` clears itself: ``Max-Age=0`` or a past ``Expires``."""
    if cookie.max_age is not None and cookie.max_age <= 0:
        return True
    if cookie.expires is None:
        return False
    try:
        expires = parsedate_to_datetime(cookie.expires)
    except (TypeError, ValueError):
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= datetime.now(UTC)


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

    @property
    def cookies(self) -> dict[str, TestCookie]:
        """The response's ``Set-Cookie`` headers, parsed and keyed by cookie name."""
        return dict(
            _parse_set_cookie(value)
            for key, value in self.multi_headers
            if key.lower() == "set-cookie"
        )


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


class WebSocketUpgradeError(Exception):
    """A WebSocket handshake rejected with an ordinary HTTP response."""

    def __init__(self, response: TestResponse) -> None:
        super().__init__(f"WebSocket upgrade rejected with HTTP {response.status_code}")
        self.response = response


class WebSocketClosedError(Exception):
    """A test WebSocket closed with a code and optional reason."""

    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"WebSocket closed with code {code}: {reason}")
        self.code = code
        self.reason = reason


class _WebSocketCycle:
    """The two queues driving one in-process ASGI WebSocket connection."""

    __slots__ = ("from_app", "to_app")

    def __init__(self) -> None:
        self.to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.from_app: queue.Queue[dict[str, Any]] = queue.Queue()

    async def receive(self) -> dict[str, Any]:
        """Return the next event sent by the test connection."""
        return await self.to_app.get()

    async def send(self, message: dict[str, Any]) -> None:
        """Expose one app event to the synchronous test connection."""
        self.from_app.put(message)


class TestWebSocket[Inbound, Outbound]:
    """A typed synchronous connection returned by :meth:`TestClient.websocket`."""

    __test__ = False

    def __init__(
        self,
        submit: Callable[[Coroutine[Any, Any, Any]], Any],
        cycle: _WebSocketCycle,
        task: asyncio.Task[None],
        inbound: TypeForm[Inbound],
        outbound: TypeForm[Outbound],
    ) -> None:
        self._submit = submit
        self._cycle = cycle
        self._task = task
        self._inbound_kind = _payload_kind("test WebSocket inbound type", inbound)
        self._outbound_kind = _payload_kind("test WebSocket outbound type", outbound)
        self._decoder: Decoder[object] | None = (
            Decoder(outbound) if self._outbound_kind == "json" else None
        )
        self._closed = False

    async def _put(self, message: dict[str, Any]) -> None:
        await self._cycle.to_app.put(message)

    async def _wait_task(self) -> None:
        await self._task

    def close(self, *, code: int = 1000) -> None:
        """Disconnect the client and wait for handler teardown."""
        if self._closed:
            return
        self._closed = True
        self._submit(self._put({"type": "websocket.disconnect", "code": code}))
        with contextlib.suppress(Exception):
            self._submit(self._wait_task())

    def send(self, message: Inbound) -> None:
        """Encode one typed client-to-server message."""
        if self._closed:
            raise RuntimeError("cannot send on a closed test WebSocket")
        if self._inbound_kind == "bytes":
            event = {"type": "websocket.receive", "bytes": cast("bytes", message)}
        elif self._inbound_kind == "text":
            event = {"type": "websocket.receive", "text": cast("str", message)}
        else:
            event = {"type": "websocket.receive", "text": msgspec_encoder.encode(message).decode()}
        self._submit(self._put(event))

    def send_text(self, text: str) -> None:
        """Send a raw text frame, including malformed input for protocol tests."""
        if self._closed:
            raise RuntimeError("cannot send on a closed test WebSocket")
        self._submit(self._put({"type": "websocket.receive", "text": text}))

    def send_bytes(self, data: bytes) -> None:
        """Send a raw binary frame, including a deliberately wrong frame kind."""
        if self._closed:
            raise RuntimeError("cannot send on a closed test WebSocket")
        self._submit(self._put({"type": "websocket.receive", "bytes": data}))

    def receive(self) -> Outbound:
        """Receive and decode one typed server-to-client message."""
        message = self._cycle.from_app.get()
        if message["type"] == "websocket.close":
            self._closed = True
            self._submit(self._put({"type": "websocket.disconnect", "code": message["code"]}))
            with contextlib.suppress(Exception):
                self._submit(self._wait_task())
            raise WebSocketClosedError(message["code"], message.get("reason", ""))
        if self._outbound_kind == "bytes":
            return cast("Outbound", message["bytes"])
        text = cast("str", message["text"])
        if self._outbound_kind == "text":
            return cast("Outbound", text)
        decoder = cast("Decoder[object]", self._decoder)
        return cast("Outbound", decoder.decode(text.encode()))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class TestClient:
    """Synchronous in-process client. Prefer ``with TestClient(app) as c``.

    ``cookie_jar=True`` opts into automatic cookie persistence across requests (off by
    default — every request then sends only what you explicitly pass). When on, each
    response's ``Set-Cookie`` values are stored in ``client.cookie_jar`` (a plain,
    directly inspectable and mutable ``dict[str, str]``) and attached to subsequent
    requests and WebSocket handshakes; an expiring ``Set-Cookie`` (``Max-Age=0`` or a
    past ``Expires``) removes its entry. Per-request ``cookies=`` merges *over* the jar
    (explicit wins on name collisions). The jar is name -> value only, with no
    path/domain scoping — the harness is single-origin and in-process, so RFC 6265
    scoping rules would be dead code here."""

    __test__ = False  # stop pytest from collecting this as a test case

    def __init__(self, app: BaseApp[Any], *, cookie_jar: bool = False) -> None:
        self._app = app
        self._jar_enabled = cookie_jar
        self.cookie_jar: dict[str, str] = {}
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
    def _merge_cookies(
        headers: Mapping[str, str] | None, cookies: Mapping[str, str] | None
    ) -> dict[str, str]:
        """``headers`` with ``cookies`` folded in as one ``Cookie`` header. Passing both
        ``cookies=`` and an explicit ``Cookie`` entry in ``headers=`` is ambiguous."""
        merged = dict(headers or {})
        if not cookies:
            return merged
        if any(key.lower() == "cookie" for key in merged):
            raise ValueError("TestClient: pass cookies= or a 'Cookie' header, not both")
        merged["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
        return merged

    def _outgoing_cookies(self, cookies: Mapping[str, str] | None) -> Mapping[str, str] | None:
        """``cookies`` merged over the jar (explicit wins on a name collision), when the
        jar is enabled; ``cookies`` unchanged otherwise."""
        if not self._jar_enabled:
            return cookies
        return {**self.cookie_jar, **(cookies or {})}

    def _apply_response_cookies(self, multi_headers: list[tuple[str, str]]) -> None:
        """Fold a response's ``Set-Cookie`` headers into the jar, when enabled: store a
        live cookie, drop one that expires itself (``Max-Age=0`` or a past ``Expires``)."""
        if not self._jar_enabled:
            return
        for key, value in multi_headers:
            if key.lower() != "set-cookie":
                continue
            name, cookie = _parse_set_cookie(value)
            if _cookie_is_expired(cookie):
                self.cookie_jar.pop(name, None)
            else:
                self.cookie_jar[name] = cookie.value

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
        cookies: Mapping[str, str] | None,
    ) -> TestResponse:
        body = b""
        outgoing = self._merge_cookies(headers, self._outgoing_cookies(cookies))
        wire_headers = {k.lower(): v for k, v in outgoing.items()}
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
        self._apply_response_cookies(cycle.multi_headers)
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
        cookies: Mapping[str, str] | None,
    ) -> _StreamSession:
        body = b""
        outgoing = self._merge_cookies(headers, self._outgoing_cookies(cookies))
        wire_headers = {k.lower(): v for k, v in outgoing.items()}
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

    async def _open_websocket(
        self,
        path: str,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        cookies: Mapping[str, str] | None,
        denial_response_extension: bool,
    ) -> tuple[_WebSocketCycle, asyncio.Task[None]]:
        outgoing = self._merge_cookies(headers, self._outgoing_cookies(cookies))
        wire_headers = {key.lower(): value for key, value in outgoing.items()}
        scope: dict[str, Any] = {
            "type": "websocket",
            "path": path,
            "query_string": urlencode(params or {}).encode("latin-1"),
            "headers": [
                (key.encode("latin-1"), value.encode("latin-1"))
                for key, value in wire_headers.items()
            ],
            "subprotocols": [],
            "extensions": ({"websocket.http.response": {}} if denial_response_extension else {}),
        }
        cycle = _WebSocketCycle()
        task = asyncio.create_task(self._app(scope, cycle.receive, cycle.send))
        await cycle.to_app.put({"type": "websocket.connect"})
        first = await asyncio.to_thread(cycle.from_app.get)
        if first["type"] == "websocket.accept":
            return cycle, task
        if first["type"] == "websocket.close":
            await task
            raise WebSocketClosedError(first["code"], first.get("reason", ""))
        if first["type"] != "websocket.http.response.start":
            raise RuntimeError(f"unexpected WebSocket handshake event {first['type']!r}")
        body_message = await asyncio.to_thread(cycle.from_app.get)
        await task
        pairs = [
            (key.decode("latin-1"), value.decode("latin-1")) for key, value in first["headers"]
        ]
        response = TestResponse(
            status_code=first["status"],
            headers=dict(pairs),
            content=body_message.get("body", b""),
            multi_headers=pairs,
        )
        raise WebSocketUpgradeError(response)

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
        cookies: Mapping[str, str] | None = None,
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
                cookies=cookies,
            )
        )

    def websocket[Inbound, Outbound](
        self,
        path: str,
        *,
        inbound: TypeForm[Inbound],
        outbound: TypeForm[Outbound],
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        denial_response_extension: bool = True,
    ) -> TestWebSocket[Inbound, Outbound]:
        """Open a typed in-process WebSocket connection."""
        cycle, task = self._submit(
            self._open_websocket(
                path,
                params=params,
                headers=headers,
                cookies=cookies,
                denial_response_extension=denial_response_extension,
            )
        )
        return TestWebSocket(self._submit, cycle, task, inbound, outbound)

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
        cookies: Mapping[str, str] | None = None,
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
                cookies=cookies,
            )
        )

    def get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> TestResponse:
        """Issue a GET request."""
        return self.request("GET", path, params=params, headers=headers, cookies=cookies)

    def stream_get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> _StreamSession:
        """Open a streaming GET request."""
        return self.stream_request("GET", path, params=params, headers=headers, cookies=cookies)

    def head(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> TestResponse:
        """Issue a HEAD request."""
        return self.request("HEAD", path, params=params, headers=headers, cookies=cookies)

    def options(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> TestResponse:
        """Issue an OPTIONS request."""
        return self.request("OPTIONS", path, params=params, headers=headers, cookies=cookies)

    def delete(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> TestResponse:
        """Issue a DELETE request."""
        return self.request("DELETE", path, params=params, headers=headers, cookies=cookies)

    def stream_delete(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> _StreamSession:
        """Open a streaming DELETE request."""
        return self.stream_request("DELETE", path, params=params, headers=headers, cookies=cookies)

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
        cookies: Mapping[str, str] | None = None,
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
            cookies=cookies,
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
        cookies: Mapping[str, str] | None = None,
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
            cookies=cookies,
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
        cookies: Mapping[str, str] | None = None,
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
            cookies=cookies,
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
        cookies: Mapping[str, str] | None = None,
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
            cookies=cookies,
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
        cookies: Mapping[str, str] | None = None,
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
            cookies=cookies,
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
        cookies: Mapping[str, str] | None = None,
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
            cookies=cookies,
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
    """Build a factory in isolation and exercise its ``create_*`` methods from sync tests.

    The factory-level sibling of :class:`TestClient`, and a thin sync bridge over
    :meth:`BaseFactory.open` — lifecycle has that one code path. The harness enters
    ``Factory.open()`` on a background loop, so services are built — and their
    resources opened and torn down — exactly as under a live app, but drivable from
    synchronous test code (matching the sync ``TestClient``). Use it to test the
    real factory wiring that an app's ``factory=`` seam mocks away; in async code
    (scripts, cron jobs, notebooks) use ``async with Factory.open()`` directly.

        with FactoryHarness(Factory) as harness:
            service = harness.run(harness.factory.create_widget_service())
            assert isinstance(service, WidgetService)
        # everything opened on the stacks is closed here

    Synchronous ``create_*`` methods can be called directly on ``harness.factory``;
    ``run`` awaits the async ones on the harness's loop.
    """

    def __init__(self, factory_cls: type[FactoryT]) -> None:
        self._loop_thread = _LoopThread()
        self._scope = AsyncExitStack()
        self.factory: FactoryT = self._loop_thread.submit(
            self._scope.enter_async_context(factory_cls.open())
        )

    def run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Await an async ``create_*`` coroutine on the harness's loop."""
        return self._loop_thread.submit(coro)

    def close(self) -> None:
        """Exit ``Factory.open()`` — closing everything the factory opened — then stop
        the loop."""
        self._loop_thread.submit(self._scope.aclose())
        self._loop_thread.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
