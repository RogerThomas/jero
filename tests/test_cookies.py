"""Cookies: request binding (typed source, verbatim names, lenient parsing), wiring
failures, and the ``TestClient`` ``cookies=`` round trip.

Response emission (``SetCookie``), auth, and OpenAPI derivation live in their own
sections below, added as each stage of the cookies plan lands."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any, Self, cast

import pytest
from msgspec import Struct, field

from jero import (
    AuthenticationRequiredError,
    BaseApp,
    CookieAuth,
    Created,
    Endpoint,
    ExceptionResponse,
    JSONResponse,
    NoContent,
    StreamingResponse,
    WebSocket,
    WebSocketEndpoint,
)
from jero.cookies import SetCookie
from jero.testing import TestClient, WebSocketClosedError, WebSocketUpgradeError

type _Message = MutableMapping[str, Any]
type _Receive = Callable[[], Awaitable[_Message]]
type _Send = Callable[[_Message], Awaitable[None]]

# --- Binding: a typed cookies Struct on an HTTP handler ---


class SessionCookies(Struct):
    """A required session cookie plus a defaulted one."""

    session_id: str
    theme: str = "dark"


class RenamedCookies(Struct):
    """A cookie whose name is not a Python identifier, bound via msgspec's rename."""

    host_token: str | None = field(default=None, name="__Host-token")


class IntCookies(Struct):
    """A single cookie converted to a scalar type."""

    n: int


class CookieEndpoint(Endpoint, path="/cookies"):
    """Echoes the bound cookies Struct back as JSON."""

    async def get(self, cookies: SessionCookies) -> SessionCookies:
        """Echo the session cookies."""
        return cookies


class RenamedCookieEndpoint(Endpoint, path="/renamed-cookie"):
    """Echoes a Struct with a non-identifier, renamed cookie field."""

    async def get(self, cookies: RenamedCookies) -> RenamedCookies:
        """Echo the renamed cookie."""
        return cookies


class IntCookieEndpoint(Endpoint, path="/int-cookie"):
    """Echoes a Struct binding a cookie to a scalar type."""

    async def get(self, cookies: IntCookies) -> IntCookies:
        """Echo the converted cookie."""
        return cookies


class CookiesApp(BaseApp):
    """App wiring the cookie-binding endpoints above."""

    async def wire(self) -> None:
        self._include_endpoint(CookieEndpoint())
        self._include_endpoint(RenamedCookieEndpoint())
        self._include_endpoint(IntCookieEndpoint())


def test_required_and_defaulted_cookies_bind() -> None:
    """A required cookie binds, and an absent optional one falls back to its default."""
    with TestClient(CookiesApp()) as client:
        resp = client.get("/cookies", cookies={"session_id": "session-value"})
        assert resp.status_code == 200
        assert resp.json() == {"session_id": "session-value", "theme": "dark"}


def test_both_cookies_bind() -> None:
    """Both fields bind when both cookies are sent."""
    with TestClient(CookiesApp()) as client:
        resp = client.get("/cookies", cookies={"session_id": "session-value", "theme": "light"})
        assert resp.status_code == 200
        assert resp.json() == {"session_id": "session-value", "theme": "light"}


def test_missing_required_cookie_is_400() -> None:
    """A missing required cookie fails binding with 400, exactly like headers."""
    with TestClient(CookiesApp()) as client:
        resp = client.get("/cookies")
        assert resp.status_code == 400
        assert resp.json()["type"] == "malformed-request"


def test_scalar_cookie_converts() -> None:
    """A cookie value converts to its declared scalar type."""
    with TestClient(CookiesApp()) as client:
        resp = client.get("/int-cookie", cookies={"n": "5"})
        assert resp.status_code == 200
        assert resp.json() == {"n": 5}


def test_bad_scalar_cookie_is_400() -> None:
    """A cookie value that fails scalar conversion is a 400, exactly like headers/params."""
    with TestClient(CookiesApp()) as client:
        resp = client.get("/int-cookie", cookies={"n": "not-an-int"})
        assert resp.status_code == 400


def test_cookie_names_bind_verbatim_and_case_sensitively() -> None:
    """Cookie names are never snake_case-mangled like headers — 'session_id' must arrive
    verbatim, and a differently-cased name does not bind."""
    with TestClient(CookiesApp()) as client:
        resp = client.get("/cookies", headers={"cookie": "Session_Id=wrong-case"})
        assert resp.status_code == 400  # 'session_id' (correct case) is still missing


def test_renamed_cookie_binds_by_wire_name() -> None:
    """A non-identifier cookie name binds through msgspec's per-field rename."""
    with TestClient(CookiesApp()) as client:
        resp = client.get("/renamed-cookie", cookies={"__Host-token": "token-value"})
        assert resp.status_code == 200
        assert resp.json() == {"__Host-token": "token-value"}


def test_renamed_cookie_absent_uses_default() -> None:
    """The renamed cookie is optional and defaults to None when absent."""
    with TestClient(CookiesApp()) as client:
        resp = client.get("/renamed-cookie")
        assert resp.status_code == 200
        assert resp.json() == {"__Host-token": None}


def test_unknown_cookies_are_ignored() -> None:
    """Cookies the declared Struct doesn't mention are silently ignored — a browser sends
    every cookie scoped to the domain, including other applications'."""
    with TestClient(CookiesApp()) as client:
        resp = client.get(
            "/cookies",
            headers={"cookie": "other_app=garbage; session_id=session-value"},
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "session-value"


def test_duplicate_cookie_name_first_occurrence_wins() -> None:
    """When a name repeats in the Cookie header, the first occurrence binds."""
    with TestClient(CookiesApp()) as client:
        resp = client.get("/cookies", headers={"cookie": "session_id=first; session_id=second"})
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "first"


def test_quoted_cookie_value_is_unwrapped() -> None:
    """A DQUOTE-wrapped cookie value has its surrounding quotes stripped."""
    with TestClient(CookiesApp()) as client:
        resp = client.get("/cookies", headers={"cookie": 'session_id="quoted-value"'})
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "quoted-value"


def test_malformed_fragment_is_skipped_while_good_ones_bind() -> None:
    """A malformed fragment (bare token, nameless '=x') is skipped rather than failing the
    whole header — an unrelated app's broken cookie must never break this route."""
    with TestClient(CookiesApp()) as client:
        resp = client.get(
            "/cookies",
            headers={"cookie": ";; =nameless; bare-token; session_id=session-value"},
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "session-value"


class _Lifespan:
    """Drives the lifespan protocol over the ASGI interface, the way a server does —
    mirrors ``tests/test_asgi_typing.py``'s helper, needed here because a split ``Cookie``
    header (two entries, same name) can't be expressed through ``TestClient``'s
    ``headers: dict[str, str]``, which collapses same-name keys."""

    def __init__(self, app: BaseApp[Any]) -> None:
        self._app = app
        self._events: asyncio.Queue[_Message] = asyncio.Queue()
        self._sent: asyncio.Queue[_Message] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def _receive(self) -> _Message:
        return await self._events.get()

    async def _send(self, message: _Message) -> None:
        await self._sent.put(message)

    async def _handshake(self, event: str) -> str:
        await self._events.put({"type": event})
        return str((await self._sent.get())["type"])

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._app({"type": "lifespan"}, self._receive, self._send))
        assert await self._handshake("lifespan.startup") == "lifespan.startup.complete"
        return self

    async def __aexit__(self, *_: object) -> None:
        assert await self._handshake("lifespan.shutdown") == "lifespan.shutdown.complete"
        assert self._task is not None
        await self._task


class _Cycle:
    """The ``receive``/``send`` pair for one request, collecting the response."""

    def __init__(self) -> None:
        self.status = 0
        self.body = b""

    async def receive(self) -> _Message:
        """The whole (empty) request body in one chunk."""
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(self, message: _Message) -> None:
        """Record the status off the start message, accumulate the body chunks."""
        if message["type"] == "http.response.start":
            self.status = message["status"]
        else:
            self.body += message.get("body", b"")


@pytest.mark.asyncio
async def test_split_cookie_headers_both_bind() -> None:
    """Two separate Cookie header entries (as HTTP/2 clients may send) are joined before
    parsing, so fields from either one bind."""
    app = CookiesApp()
    cycle = _Cycle()
    async with _Lifespan(app):
        await app(
            {
                "type": "http",
                "method": "GET",
                "path": "/cookies",
                "query_string": b"",
                "headers": [
                    (b"cookie", b"session_id=session-value"),
                    (b"cookie", b"theme=light"),
                ],
            },
            cycle.receive,
            cycle.send,
        )
    assert cycle.status == 200
    assert cycle.body == b'{"session_id":"session-value","theme":"light"}'


# --- Wiring failures ---


class NonStructCookiesEndpoint(Endpoint, path="/bad-cookies"):
    """Endpoint whose 'cookies' argument is not a msgspec Struct."""

    async def get(self, cookies: int) -> SessionCookies:  # must be a Struct
        """Handler annotating cookies as a non-Struct type."""
        return SessionCookies(session_id=str(cookies))


class BadCookiesApp(BaseApp):
    """App wiring the invalid cookies endpoint."""

    async def wire(self) -> None:
        self._include_endpoint(NonStructCookiesEndpoint())


def test_non_struct_cookies_annotation_is_a_wiring_error() -> None:
    """Annotating 'cookies' as anything but a Struct fails at startup."""
    with pytest.raises(RuntimeError, match="'cookies' must be annotated"):
        TestClient(BadCookiesApp())


# --- WebSocket handshake cookies ---


class Inbound(Struct, tag="inbound"):
    """Client-to-server WebSocket message (unused; the test never sends one)."""

    text: str


class Outbound(Struct, tag="outbound"):
    """Server-to-client WebSocket message: the handshake cookie, echoed back."""

    session_id: str


class CookieWebSocket(WebSocketEndpoint, path="/ws-cookies"):
    """WebSocket endpoint binding a handshake cookie."""

    async def handle(
        self, websocket: WebSocket[Inbound, Outbound], cookies: SessionCookies
    ) -> None:
        """Send the handshake cookie back once, then close."""
        await websocket.send(Outbound(session_id=cookies.session_id))


class CookieWebSocketApp(BaseApp):
    """App wiring the handshake-cookie WebSocket endpoint."""

    async def wire(self) -> None:
        self._include_websocket(CookieWebSocket())


def test_websocket_handshake_binds_cookies() -> None:
    """A cookie declared on a WebSocket handler binds from the handshake headers."""
    with (
        TestClient(CookieWebSocketApp()) as client,
        client.websocket(
            "/ws-cookies", inbound=Inbound, outbound=Outbound, cookies={"session_id": "ws-value"}
        ) as ws,
    ):
        assert ws.receive() == Outbound(session_id="ws-value")


# --- TestClient cookies= surface ---


def test_testclient_cookies_and_explicit_cookie_header_conflict() -> None:
    """Passing both cookies= and an explicit 'Cookie' header is ambiguous."""
    with TestClient(CookiesApp()) as client, pytest.raises(ValueError, match="cookies="):
        client.get(
            "/cookies",
            cookies={"session_id": "a"},
            headers={"Cookie": "session_id=b"},
        )


# --- Response side: SetCookie on every wrapper kind ---


class Result(Struct):
    """A trivial JSON body used only to exercise SetCookie on responses."""

    ok: bool


class LogoutError(Exception):
    """An ordinary exception whose custom handler also expires a cookie."""


class LogoutHandler:
    """Translates LogoutError into a 401 that also clears the session cookie."""

    def handle_exception(self, exception: LogoutError) -> ExceptionResponse[Result]:
        """Return a 401 whose response expires the session cookie."""
        _ = exception
        return ExceptionResponse(
            status_code=401, json=Result(ok=False), cookies=[SetCookie.expire("session")]
        )


class SetCookieJSONEndpoint(Endpoint, path="/set-cookie-json"):
    """Returns a JSON response carrying a SetCookie."""

    async def get(self) -> JSONResponse[Result]:
        """Set a session cookie alongside a JSON body."""
        return JSONResponse(json=Result(ok=True), cookies=[SetCookie("session", "abc")])


class SetCookieNoContentEndpoint(Endpoint, path="/set-cookie-no-content"):
    """Returns a 204 carrying a SetCookie."""

    async def get(self) -> NoContent:
        """Set a session cookie on an otherwise empty response."""
        return NoContent(cookies=[SetCookie("session", "abc")])


class SetCookieCreatedEndpoint(Endpoint, path="/set-cookie-created"):
    """Returns a 201 carrying a SetCookie."""

    async def post(self) -> Created[Result]:
        """Set a session cookie alongside a Created response."""
        return Created(json=Result(ok=True), cookies=[SetCookie("session", "abc")])


class SetCookieStreamEndpoint(Endpoint, path="/set-cookie-stream"):
    """Returns a streaming response carrying a SetCookie."""

    async def _chunks(self) -> AsyncIterator[bytes]:
        """Yield one byte chunk."""
        yield b"chunk"

    async def get(self) -> StreamingResponse:
        """Set a session cookie on a streamed byte response."""
        return StreamingResponse(stream=self._chunks(), cookies=[SetCookie("session", "abc")])


class ExpireCookieEndpoint(Endpoint, path="/expire-cookie"):
    """Returns a response expiring the session cookie."""

    async def post(self) -> NoContent:
        """Expire the session cookie."""
        return NoContent(cookies=[SetCookie.expire("session")])


class DuplicateCookieEndpoint(Endpoint, path="/duplicate-cookie"):
    """Returns two SetCookie entries sharing a name — a programming error."""

    async def get(self) -> NoContent:
        """Emit a duplicate cookie name."""
        return NoContent(cookies=[SetCookie("session", "a"), SetCookie("session", "b")])


class RawAfterCookieEndpoint(Endpoint, path="/raw-after-cookie"):
    """Returns a typed SetCookie plus a hand-rolled raw_headers Set-Cookie."""

    async def get(self) -> NoContent:
        """Emit both a typed cookie and a raw_headers Set-Cookie, in that order."""
        return NoContent(
            cookies=[SetCookie("session", "a")], raw_headers={"set-cookie": "legacy=raw-value"}
        )


class RaisesLogoutEndpoint(Endpoint, path="/raises-logout"):
    """Raises LogoutError, letting the custom handler emit the response."""

    async def get(self) -> Result:
        """Always raise, so the custom exception handler responds instead."""
        raise LogoutError()


class ResponseCookiesApp(BaseApp):
    """App wiring every response-side cookie scenario above."""

    async def wire(self) -> None:
        self._include_endpoint(SetCookieJSONEndpoint())
        self._include_endpoint(SetCookieNoContentEndpoint())
        self._include_endpoint(SetCookieCreatedEndpoint())
        self._include_endpoint(SetCookieStreamEndpoint())
        self._include_endpoint(ExpireCookieEndpoint())
        self._include_endpoint(DuplicateCookieEndpoint())
        self._include_endpoint(RawAfterCookieEndpoint())
        self._include_endpoint(RaisesLogoutEndpoint())
        self._include_exception_handler(LogoutHandler())


def test_json_response_emits_secure_default_cookie() -> None:
    """A JSONResponse's SetCookie arrives with secure defaults on the wire."""
    with TestClient(ResponseCookiesApp()) as client:
        resp = client.get("/set-cookie-json")
        assert resp.status_code == 200
        cookie = resp.cookies["session"]
        assert cookie.value == "abc"
        assert cookie.path == "/"
        assert cookie.secure is True
        assert cookie.http_only is True
        assert cookie.same_site == "Lax"


def test_no_content_emits_cookie() -> None:
    """A 204 NoContent still carries its SetCookie."""
    with TestClient(ResponseCookiesApp()) as client:
        resp = client.get("/set-cookie-no-content")
        assert resp.status_code == 204
        assert resp.cookies["session"].value == "abc"


def test_created_emits_cookie() -> None:
    """A 201 Created still carries its SetCookie."""
    with TestClient(ResponseCookiesApp()) as client:
        resp = client.post("/set-cookie-created")
        assert resp.status_code == 201
        assert resp.cookies["session"].value == "abc"


def test_streaming_emits_cookie() -> None:
    """A streaming response's SetCookie is sent with the response start."""
    with (
        TestClient(ResponseCookiesApp()) as client,
        client.stream_get("/set-cookie-stream") as stream,
    ):
        assert stream.status_code == 200
        assert stream.headers["set-cookie"].startswith("session=abc")
        list(stream)  # drain so the app task finishes cleanly


def test_exception_response_emits_cookie() -> None:
    """A custom exception handler's response can expire a stale session cookie."""
    with TestClient(ResponseCookiesApp()) as client:
        resp = client.get("/raises-logout")
        assert resp.status_code == 401
        cookie = resp.cookies["session"]
        assert cookie.value == ""
        assert cookie.max_age == 0


def test_expire_sets_max_age_zero_and_epoch_expires() -> None:
    """SetCookie.expire() emits Max-Age=0 and an Expires at the Unix epoch."""
    with TestClient(ResponseCookiesApp()) as client:
        resp = client.post("/expire-cookie")
        cookie = resp.cookies["session"]
        assert cookie.value == ""
        assert cookie.max_age == 0
        assert cookie.expires == "Thu, 01 Jan 1970 00:00:00 GMT"


def test_duplicate_cookie_name_in_one_response_is_500() -> None:
    """Two SetCookie entries sharing a name in one response is a programming error."""
    with TestClient(ResponseCookiesApp()) as client:
        resp = client.get("/duplicate-cookie")
        assert resp.status_code == 500


def test_raw_headers_set_cookie_survives_after_typed_cookie() -> None:
    """The raw_headers escape hatch still works and is appended after typed cookies."""
    with TestClient(ResponseCookiesApp()) as client:
        resp = client.get("/raw-after-cookie")
        values = [v for k, v in resp.multi_headers if k.lower() == "set-cookie"]
        assert len(values) == 2
        assert values[0].startswith("session=a;")
        assert values[1] == "legacy=raw-value"


# --- Testing surface: the opt-in cookie jar ---


class SessionEndpoint(Endpoint, path="/session"):
    """A minimal login/logout pair for exercising the TestClient cookie jar."""

    async def post(self) -> NoContent:
        """Log in: set the session cookie."""
        return NoContent(cookies=[SetCookie("session_id", "session-value")])

    async def delete(self) -> NoContent:
        """Log out: expire the session cookie."""
        return NoContent(cookies=[SetCookie.expire("session_id")])


class WhoAmIEndpoint(Endpoint, path="/whoami"):
    """Echoes the incoming session cookie, or None if absent."""

    async def get(self, cookies: SessionCookies) -> SessionCookies:
        """Echo the session cookie the request carried."""
        return cookies


class JarApp(BaseApp):
    """App wiring the login/logout/whoami endpoints for jar tests."""

    async def wire(self) -> None:
        self._include_endpoint(SessionEndpoint())
        self._include_endpoint(WhoAmIEndpoint())


def test_jar_disabled_by_default_no_persistence() -> None:
    """Without cookie_jar=True, a Set-Cookie from one request never reaches the next."""
    with TestClient(JarApp()) as client:
        client.post("/session")
        resp = client.get("/whoami")
        assert resp.status_code == 400  # 'session_id' was never sent


def test_jar_persists_cookies_across_requests() -> None:
    """With cookie_jar=True, a Set-Cookie from one response is sent on the next request."""
    with TestClient(JarApp(), cookie_jar=True) as client:
        client.post("/session")
        resp = client.get("/whoami")
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "session-value"


def test_jar_expiring_set_cookie_removes_the_entry() -> None:
    """A logout response's expiring Set-Cookie clears the entry from the jar."""
    with TestClient(JarApp(), cookie_jar=True) as client:
        client.post("/session")
        assert "session_id" in client.cookie_jar
        client.delete("/session")
        assert "session_id" not in client.cookie_jar


def test_jar_explicit_cookies_override_the_jar() -> None:
    """Per-request cookies= wins over a jar entry with the same name."""
    with TestClient(JarApp(), cookie_jar=True) as client:
        client.post("/session")
        resp = client.get("/whoami", cookies={"session_id": "explicit-value"})
        assert resp.json()["session_id"] == "explicit-value"


def test_jar_is_directly_mutable() -> None:
    """client.cookie_jar is a plain, directly writable dict."""
    with TestClient(JarApp(), cookie_jar=True) as client:
        client.cookie_jar["session_id"] = "manually-set"
        resp = client.get("/whoami")
        assert resp.json()["session_id"] == "manually-set"


# --- Cookie auth: CookieAuth, HybridAuth, and OpenAPI security derivation ---

_TOKENS = {"good-token": "user-name"}


class AuthCookies(Struct):
    """A single required session cookie — derives an apiKey-in-cookie OpenAPI scheme."""

    session_id: str


class OptionalAuthCookies(Struct):
    """A single optional session cookie, letting absence reach ``authenticate``."""

    session_id: str | None = None


class AuthUser(Struct):
    """The authenticated caller, for the cookie-auth test section."""

    name: str


@dataclass
class CookieOnlyAuth:
    """Gating cookie authenticator: every mounted route requires a valid session cookie."""

    async def authenticate(self, cookies: AuthCookies) -> AuthUser:
        """Resolve the session cookie to a user, or raise 401."""
        name = _TOKENS.get(cookies.session_id)
        if name is None:
            raise AuthenticationRequiredError()
        return AuthUser(name=name)


@dataclass
class OptionalCookieAuth:
    """Anonymous-accepting cookie authenticator: absence serves ``user=None``."""

    async def authenticate(self, cookies: OptionalAuthCookies) -> AuthUser | None:
        """Resolve the session cookie, ``None`` if absent, or raise 401 if invalid."""
        if cookies.session_id is None:
            return None
        name = _TOKENS.get(cookies.session_id)
        if name is None:
            raise AuthenticationRequiredError()
        return AuthUser(name=name)


class HybridCredentials(Struct):
    """Typed headers for the hybrid authenticator — optional, like the cookie."""

    authorization: str | None = None


@dataclass
class HybridTokenAuth:
    """Accepts either a bearer header or a session cookie, whichever is present."""

    async def authenticate(
        self, headers: HybridCredentials, cookies: OptionalAuthCookies
    ) -> AuthUser:
        """Resolve a user from either source, or raise 401 if neither validates."""
        token = cookies.session_id
        if token is None and headers.authorization is not None:
            token = headers.authorization.removeprefix("Bearer ").strip()
        name = _TOKENS.get(token) if token is not None else None
        if name is None:
            raise AuthenticationRequiredError()
        return AuthUser(name=name)


class GatedEndpoint(Endpoint, path="/gated"):
    """Endpoint mounted behind whichever authenticator the app wires."""

    async def get(self, user: AuthUser) -> AuthUser:
        """Return the authenticated caller."""
        return user


class OptionalGatedEndpoint(Endpoint, path="/optional-gated"):
    """Endpoint mounted behind an anonymous-accepting authenticator."""

    async def get(self, user: AuthUser | None) -> AuthUser:
        """Return the authenticated caller, or a placeholder when anonymous."""
        return user if user is not None else AuthUser(name="anonymous")


class CookieAuthApp(BaseApp):
    """App wiring GatedEndpoint behind a cookie-only authenticator."""

    async def wire(self) -> None:
        self._include_endpoint(GatedEndpoint(), auth=CookieOnlyAuth())


class OptionalCookieAuthApp(BaseApp):
    """App wiring OptionalGatedEndpoint behind an anonymous-accepting cookie authenticator."""

    async def wire(self) -> None:
        self._include_endpoint(OptionalGatedEndpoint(), auth=OptionalCookieAuth())


class HybridAuthApp(BaseApp):
    """App wiring GatedEndpoint behind a hybrid header-or-cookie authenticator."""

    async def wire(self) -> None:
        self._include_endpoint(GatedEndpoint(), auth=HybridTokenAuth())


def test_cookie_auth_valid_cookie_authenticates() -> None:
    """A valid session cookie authenticates and injects the user."""
    with TestClient(CookieAuthApp()) as client:
        resp = client.get("/gated", cookies={"session_id": "good-token"})
        assert resp.status_code == 200
        assert resp.json() == {"name": "user-name"}


def test_cookie_auth_missing_cookie_is_401() -> None:
    """A missing session cookie is rejected with 401 before the handler runs."""
    with TestClient(CookieAuthApp()) as client:
        resp = client.get("/gated")
        assert resp.status_code == 401


def test_cookie_auth_invalid_cookie_is_401() -> None:
    """An unknown session cookie value is rejected with 401."""
    with TestClient(CookieAuthApp()) as client:
        resp = client.get("/gated", cookies={"session_id": "bad-token"})
        assert resp.status_code == 401


def test_optional_cookie_auth_absent_serves_anonymous() -> None:
    """With no session cookie at all, the anonymous-accepting route serves user=None."""
    with TestClient(OptionalCookieAuthApp()) as client:
        resp = client.get("/optional-gated")
        assert resp.status_code == 200
        assert resp.json() == {"name": "anonymous"}


def test_optional_cookie_auth_valid_cookie_authenticates() -> None:
    """A valid session cookie on an anonymous-accepting route injects the user."""
    with TestClient(OptionalCookieAuthApp()) as client:
        resp = client.get("/optional-gated", cookies={"session_id": "good-token"})
        assert resp.status_code == 200
        assert resp.json() == {"name": "user-name"}


def test_optional_cookie_auth_invalid_cookie_is_still_401() -> None:
    """A present but invalid session cookie is still a 401, even on an anonymous route."""
    with TestClient(OptionalCookieAuthApp()) as client:
        resp = client.get("/optional-gated", cookies={"session_id": "bad-token"})
        assert resp.status_code == 401


def test_hybrid_auth_binds_via_cookie() -> None:
    """A hybrid authenticator accepts a caller presenting only a cookie."""
    with TestClient(HybridAuthApp()) as client:
        resp = client.get("/gated", cookies={"session_id": "good-token"})
        assert resp.status_code == 200
        assert resp.json() == {"name": "user-name"}


def test_hybrid_auth_binds_via_header() -> None:
    """The same hybrid authenticator accepts a caller presenting only a bearer header."""
    with TestClient(HybridAuthApp()) as client:
        resp = client.get("/gated", headers={"authorization": "Bearer good-token"})
        assert resp.status_code == 200
        assert resp.json() == {"name": "user-name"}


def test_hybrid_auth_neither_source_is_401() -> None:
    """A caller presenting neither a cookie nor a header is rejected with 401."""
    with TestClient(HybridAuthApp()) as client:
        resp = client.get("/gated")
        assert resp.status_code == 401


# --- Auth wiring failures ---


class WrongParamNameAuth:
    """An authenticator whose argument name is neither 'headers' nor 'cookies'."""

    async def authenticate(self, cookie: AuthCookies) -> AuthUser:
        """Never runs; the signature itself fails wiring."""
        raise NotImplementedError


class ZeroParamAuth:
    """An authenticator whose authenticate() takes no arguments at all."""

    async def authenticate(self) -> AuthUser:
        """Never runs; the signature itself fails wiring."""
        raise NotImplementedError


class WrongParamAuthApp(BaseApp):
    """App wiring an authenticator with an invalid argument name."""

    async def wire(self) -> None:
        # Neither shape genuinely satisfies CookieAuth (that's the point of the test —
        # jero's runtime introspection catches it); the cast only silences the static
        # checker's correct rejection of a deliberately invalid authenticate() signature.
        auth = cast(CookieAuth[AuthCookies, AuthUser], WrongParamNameAuth())
        self._include_endpoint(GatedEndpoint(), auth=auth)


class ZeroParamAuthApp(BaseApp):
    """App wiring an authenticator whose authenticate() takes no arguments."""

    async def wire(self) -> None:
        auth = cast(CookieAuth[AuthCookies, AuthUser], ZeroParamAuth())
        self._include_endpoint(GatedEndpoint(), auth=auth)


def test_authenticate_with_wrong_argument_name_is_a_wiring_error() -> None:
    """An authenticate() argument that is neither 'headers' nor 'cookies' fails at startup."""
    with pytest.raises(RuntimeError, match="'headers', 'cookies', or both"):
        TestClient(WrongParamAuthApp())


def test_authenticate_with_zero_arguments_is_a_wiring_error() -> None:
    """An authenticate() with no arguments fails at startup."""
    with pytest.raises(RuntimeError, match="'headers', 'cookies', or both"):
        TestClient(ZeroParamAuthApp())


# --- OpenAPI security-scheme derivation ---


class MultiFieldAuthCookies(Struct):
    """Several cookie fields — no single field to name an apiKey scheme after."""

    session_id: str
    csrf_token: str


@dataclass
class MultiFieldCookieAuth:
    """A cookies-only authenticator whose Struct has more than one field."""

    async def authenticate(self, cookies: MultiFieldAuthCookies) -> AuthUser:
        """Never reached in the derivation test — wiring fails before any request."""
        return AuthUser(name=cookies.session_id)


class UndeclaredSchemeApp(BaseApp):
    """App wiring a multi-field cookies-only authenticator with no explicit
    openapi_security, then building the OpenAPI document."""

    async def wire(self) -> None:
        self._include_endpoint(GatedEndpoint(), auth=MultiFieldCookieAuth())
        self._include_openapi(title="t", version="1")


class UndeclaredSchemeNoDocsApp(BaseApp):
    """The same wiring, but never calling _include_openapi — must not pay the check."""

    async def wire(self) -> None:
        self._include_endpoint(GatedEndpoint(), auth=MultiFieldCookieAuth())


def test_multi_field_cookie_auth_derives_no_scheme_but_wires_fine_without_openapi() -> None:
    """An app that never calls _include_openapi never hits the derivation check."""
    with TestClient(UndeclaredSchemeNoDocsApp()) as client:
        resp = client.get(
            "/gated", cookies={"session_id": "good-token", "csrf_token": "csrf-value"}
        )
        assert resp.status_code == 200


def test_multi_field_cookie_auth_with_no_declared_scheme_fails_at_include_openapi() -> None:
    """A multi-field cookies-only authenticator with nothing declared fails once the
    document is actually built — naming the authenticator and the fix."""
    with pytest.raises(RuntimeError, match="declare `openapi_security` on MultiFieldCookieAuth"):
        TestClient(UndeclaredSchemeApp())


class HybridNoSchemeApp(BaseApp):
    """App wiring a hybrid authenticator with no explicit openapi_security, docs on."""

    async def wire(self) -> None:
        self._include_endpoint(GatedEndpoint(), auth=HybridTokenAuth())
        self._include_openapi(title="t", version="1")


def test_hybrid_auth_with_no_declared_scheme_fails_at_include_openapi() -> None:
    """A hybrid authenticator can't derive a scheme either, and fails the same way."""
    with pytest.raises(RuntimeError, match="declare `openapi_security` on HybridTokenAuth"):
        TestClient(HybridNoSchemeApp())


class SingleFieldCookieAuthApp(BaseApp):
    """App wiring the single-field cookie authenticator with docs on, to inspect the
    derived security scheme."""

    async def wire(self) -> None:
        self._include_endpoint(GatedEndpoint(), auth=CookieOnlyAuth())
        self._include_openapi(title="t", version="1")


def test_single_field_cookie_auth_derives_api_key_cookie_scheme() -> None:
    """A single-field cookies-only authenticator with nothing declared derives an
    apiKey-in-cookie scheme named for that field."""
    with TestClient(SingleFieldCookieAuthApp()) as client:
        doc = client.get("/openapi.json").json()
        schemes = doc["components"]["securitySchemes"]
        assert len(schemes) == 1
        scheme = next(iter(schemes.values()))
        assert scheme == {"type": "apiKey", "in": "cookie", "name": "session_id"}
        assert doc["paths"]["/gated"]["get"]["security"] == [{next(iter(schemes)): []}]


# --- WebSocket cookie auth: the motivating browser case ---


class AuthOutbound(Struct, tag="auth-outbound"):
    """Server-to-client message: the authenticated caller's name."""

    name: str


class AuthInbound(Struct, tag="auth-inbound"):
    """Client-to-server message (unused; the test never sends one)."""

    text: str


class GatedWebSocket(WebSocketEndpoint, path="/ws-gated"):
    """WebSocket endpoint mounted behind a cookie authenticator."""

    async def handle(self, websocket: WebSocket[AuthInbound, AuthOutbound], user: AuthUser) -> None:
        """Send the authenticated caller's name back once, then close."""
        await websocket.send(AuthOutbound(name=user.name))


class CookieAuthWebSocketApp(BaseApp):
    """App wiring GatedWebSocket behind the cookie-only authenticator."""

    async def wire(self) -> None:
        self._include_websocket(GatedWebSocket(), auth=CookieOnlyAuth())


def test_websocket_cookie_auth_accepts_with_valid_cookie() -> None:
    """A valid session cookie authenticates the WebSocket handshake."""
    with (
        TestClient(CookieAuthWebSocketApp()) as client,
        client.websocket(
            "/ws-gated",
            inbound=AuthInbound,
            outbound=AuthOutbound,
            cookies={"session_id": "good-token"},
        ) as ws,
    ):
        assert ws.receive() == AuthOutbound(name="user-name")


def test_websocket_cookie_auth_rejects_before_upgrade_without_a_cookie() -> None:
    """A missing session cookie rejects the handshake before the upgrade, as an HTTP 401 —
    the browser WebSocket API cannot set an Authorization header, so cookies are how a
    browser client authenticates a WebSocket at all."""
    with TestClient(CookieAuthWebSocketApp()) as client:
        with pytest.raises(WebSocketUpgradeError) as caught:
            client.websocket("/ws-gated", inbound=AuthInbound, outbound=AuthOutbound)
        assert caught.value.response.status_code == 401


def test_websocket_cookie_auth_rejects_without_denial_extension() -> None:
    """Without the optional denial-response extension, rejection is a pre-accept close."""
    with TestClient(CookieAuthWebSocketApp()) as client:
        with pytest.raises(WebSocketClosedError) as caught:
            client.websocket(
                "/ws-gated",
                inbound=AuthInbound,
                outbound=AuthOutbound,
                denial_response_extension=False,
            )
        assert caught.value.code == 1008


# --- OpenAPI: request-side cookie params, and the Set-Cookie response boundary ---


class OpenAPICookies(Struct):
    """A required and a defaulted cookie, for verbatim in: cookie param derivation."""

    session_id: str
    theme: str = "dark"


class OpenAPICookieEndpoint(Endpoint, path="/openapi-cookies"):
    """Endpoint declaring cookies, exercised only for its derived OpenAPI parameters."""

    async def get(self, cookies: OpenAPICookies) -> OpenAPICookies:
        """Echo the bound cookies."""
        return cookies


class CookieEmittingEndpoint(Endpoint, path="/openapi-response-cookie"):
    """Endpoint whose response sets a cookie — never modeled in the spec.

    Named, and its path spelled, to avoid the substring "set-cookie" entirely: the
    document-absence test below scans the raw JSON text for exactly that string, and an
    operationId or path containing it as a substring would be a false positive.
    """

    async def get(self) -> NoContent:
        """Set a cookie the OpenAPI document must not describe."""
        return NoContent(cookies=[SetCookie("session_id", "value")])


class OpenAPICookiesApp(BaseApp):
    """App wiring the two endpoints above with the OpenAPI document enabled."""

    async def wire(self) -> None:
        self._include_endpoint(OpenAPICookieEndpoint())
        self._include_endpoint(CookieEmittingEndpoint())
        self._include_openapi(title="t", version="1")


def test_openapi_cookie_params_use_verbatim_names_and_required_flags() -> None:
    """Declared cookies appear as in: cookie params, verbatim-named, required tracking
    whether the field has a default."""
    with TestClient(OpenAPICookiesApp()) as client:
        doc = client.get("/openapi.json").json()
        params = doc["paths"]["/openapi-cookies"]["get"]["parameters"]
        by_name = {param["name"]: param for param in params}
        assert by_name["session_id"]["in"] == "cookie"
        assert by_name["session_id"]["required"] is True
        assert by_name["theme"]["in"] == "cookie"
        assert by_name["theme"]["required"] is False


def test_openapi_never_models_set_cookie_responses() -> None:
    """A response's SetCookie entries never appear anywhere in the generated document —
    OpenAPI can't describe repeated Set-Cookie headers, and they're per-instance runtime
    values, not part of the static return type."""
    with TestClient(OpenAPICookiesApp()) as client:
        doc = client.get("/openapi.json").json()
        raw = client.get("/openapi.json").text
        assert "set-cookie" not in raw.lower()
        operation = doc["paths"]["/openapi-response-cookie"]["get"]
        for response in operation["responses"].values():
            assert "headers" not in response
