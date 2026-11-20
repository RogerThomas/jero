# Cookies

Cookies are a typed binding source, like headers:

- **Read** them by binding `cookies` in a handler, right beside `headers`.
- **Set** them with `SetCookie` on any response wrapper.
- **Authenticate** on them: an authenticator can gate a route on a session cookie exactly
  as it gates on a bearer header, including on a WebSocket handshake.

## Reading cookies — `cookies`

Declare a `Struct` and bind it as `cookies`, the same way you'd bind `headers` or
`params`:

```python
from msgspec import Struct

from jero import BaseApp, Endpoint


class SessionCookies(Struct):
    session_id: str
    theme: str = "dark"           # optional cookie, defaulted when absent


class ThemeEndpoint(Endpoint, path="/theme"):
    async def get(self, cookies: SessionCookies) -> SessionCookies:   # GET /theme
        return cookies


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(ThemeEndpoint())


app = App()
```

**A field name is the cookie name, verbatim and case-sensitive.** `headers` lower-cases
and turns `-` into `_` (`X-Trace-Id` → `x_trace_id`); `cookies` does none of that,
because RFC 6265 cookie names are case-sensitive and often aren't Python identifiers
(`__Host-session`). For a name that isn't a valid identifier, use msgspec's per-field
rename:

```python
from msgspec import field
from msgspec import Struct


class HostCookies(Struct):
    host_token: str | None = field(default=None, name="__Host-token")
```

## Lenient parsing, strict binding

A browser sends *every* cookie scoped to the domain and path in one `Cookie` header,
including cookies set by other applications on that domain. So the parser is lenient
about everything that isn't yours:

- A malformed fragment (`;;`, a nameless `=x`, a bare token with no `=`) is skipped,
  never a 400.
- A cookie your `Struct` doesn't mention is ignored.
- If a name repeats, the **first** occurrence wins.
- A value wrapped in one pair of `"` quotes has them stripped. Nothing else is
  unescaped, since RFC 6265 defines no escaping convention.
- A `Cookie` header split across several field lines (HTTP/2 permits this) is joined
  with `"; "` before parsing.

Your own declared fields are strict, exactly like `headers`: a required cookie that is
absent, or a value that fails conversion (a non-`int` where you declared `int`), is a
**400**.

## Setting cookies — `SetCookie`

Every response wrapper (`JSONResponse`, `BytesResponse`, `NoContent`, `Created`,
`Accepted`, the streaming responses, `ExceptionResponse`) takes a `cookies:
Sequence[SetCookie]`, right beside `headers`:

```python
from msgspec import Struct

from jero import BaseApp, Endpoint, NoContent, SetCookie


class LoginRequest(Struct):
    token: str


class SessionEndpoint(Endpoint, path="/session"):
    async def post(self, json: LoginRequest) -> NoContent:    # POST /session — log in
        return NoContent(cookies=[SetCookie("session_id", json.token)])

    async def delete(self) -> NoContent:                      # DELETE /session — log out
        return NoContent(cookies=[SetCookie.expire("session_id")])


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(SessionEndpoint())


app = App()
```

**`SetCookie` is secure by default.** A bare `SetCookie("session_id", token)` is already:

```
Set-Cookie: session_id=<token>; Path=/; Secure; HttpOnly; SameSite=Lax
```

Loosening any of that is explicit, and so visible in review: `http_only=False` for a
cookie your JavaScript needs to read, `secure=False`, a different `same_site`.

Keep `secure=True` in local development too. Browsers treat `http://localhost` as a
trustworthy origin, so `Secure` cookies work there without HTTPS.

`SetCookie(...)` validates at construction and raises `ValueError` there, not at send
time:

- The name must be a legal RFC 6265 token.
- The value must contain no control characters, quotes, commas, semicolons, or
  backslashes.
- `same_site="none"` requires `secure=True`.
- A `__Host-` or `__Secure-` name must carry the attributes the prefix promises.
  `__Host-` additionally requires `path="/"` and no `domain`.

Two `SetCookie` entries with the same name in one response is a bug: the client gets a
500.

## Deleting a cookie — `SetCookie.expire`

Use the named constructor, as `SessionEndpoint.delete` does above. It sends both
`Max-Age=0` **and** an `Expires` at the Unix epoch, for clients that only look at one of
them.

`path` and `domain` must match the cookie you're clearing, because a browser only
removes a cookie whose scope matches exactly:
`SetCookie.expire("session_id", path="/app")` if that's where you scoped it.

## Cookie auth

An authenticator may bind `cookies` instead of, or alongside, `headers`. Everything in
[Authentication](auth.md) is unchanged. Only what `authenticate` binds from differs:

```python
from dataclasses import dataclass

from msgspec import Struct

from jero import AuthenticationRequiredError, BaseApp, Endpoint


class SessionCookies(Struct):
    session_id: str


class User(Struct):
    id: str
    name: str


@dataclass
class SessionAuth:
    _sessions: dict[str, User]

    async def authenticate(self, cookies: SessionCookies) -> User:
        user = self._sessions.get(cookies.session_id)
        if user is None:
            raise AuthenticationRequiredError()
        return user


class ProfileEndpoint(Endpoint, path="/profile"):
    async def get(self, user: User) -> User:    # GET /profile, cookie-gated
        return user


class App(BaseApp):
    async def wire(self) -> None:
        sessions = {"session-value": User(id="user-id", name="user-name")}
        self._include_endpoint(ProfileEndpoint(), auth=SessionAuth(sessions))


app = App()
```

`authenticate` can take either shape:

| Signature                                                   | Use | Static protocol |
| ----------------------------------------------------------- | --- | --------------- |
| `authenticate(self, cookies: TCookies) -> TUser`            | session-cookie auth, as above | `CookieAuth[TCookies, TUser]` |
| `authenticate(self, headers: THeaders, cookies: TCookies) -> TUser` | **hybrid**: bearer-token API clients and cookie-carrying browsers on the same routes | `HybridAuth[THeaders, TCookies, TUser]` |

The static protocols sit beside `Auth[THeaders, TUser]`. The rules:

- **No base class is needed.** jero introspects the concrete `authenticate` signature, so
  any object with the right shape works, exactly as with header auth.
- **Add the `Authenticator` base to declare errors.** An authenticator that raises its
  own errors (an `InvalidSessionError`, say) lists them with
  `class SessionAuth(Authenticator, exceptions=[...])`, so every route it guards documents
  them. See [Declaring the authenticator's errors](auth.md#declaring-the-authenticators-errors).
- **Optional credentials work per source.** Give a cookie field a `| None` default so its
  absence reaches `authenticate` instead of failing binding first.
- **Anonymous-accepting authenticators** (`-> TUser | None`) still require every handler
  on their routes to declare `user: TUser | None`.

### WebSockets: the motivating case

A browser's WebSocket API has no way to set an `Authorization` header, but it always
sends cookies for the connecting origin. So `cookies` is one of the
[handshake's binding sources](websockets.md#handshake-binding-and-authentication), like
`headers`: mount a cookie or hybrid authenticator on `_include_websocket` and a browser
client authenticates with the session cookie it already has, with no extra handshake
step.

## The OpenAPI boundary

- **Request cookies** document like any other parameter: `in: cookie`, named verbatim, so
  a renamed field's wire name is what appears.
- **Cookie auth** derives a security scheme when it can. When it can't, set
  `openapi_security` on the class, or startup fails once `_include_openapi` is wired.
  See [Auth in the OpenAPI spec](auth.md#auth-in-the-openapi-spec).
- **Response `Set-Cookie` is not in the spec.** OpenAPI cannot describe several
  `Set-Cookie` headers on one response. If you need one documented cookie-shaped value,
  put it on a typed `headers` Struct field instead. It won't render as a real
  `Set-Cookie` attribute set.

## Testing

**Sending cookies.** Pass `cookies=` on any request or `client.websocket(...)` call. It
is encoded as one `Cookie` header:

```python
resp = client.get("/theme", cookies={"session_id": "session-value"})
```

Passing `cookies=` *and* an explicit `Cookie` entry in `headers=` is ambiguous and
raises `ValueError`.

**Reading `Set-Cookie` back.** `TestResponse.cookies` is a `dict[str, TestCookie]`
parsed from the response, one entry per cookie name:

```python
resp = client.post("/session", json={"token": "session-value"})
cookie = resp.cookies["session_id"]
assert cookie.value == "session-value"
assert cookie.http_only and cookie.secure
```

**Persisting a session across requests.** Pass `cookie_jar=True` to `TestClient`:

- Off by default: every request sends only what you explicitly pass.
- On: each response's `Set-Cookie` values are stored in `client.cookie_jar`, a plain
  `dict[str, str]` you can inspect, seed or clear by hand, and attached to later
  requests and WebSocket handshakes.
- An expiring `Set-Cookie` (`Max-Age=0` or a past `Expires`, which is what
  `SetCookie.expire()` sends) removes its entry, so a logout test can assert the jar
  emptied.
- A per-request `cookies=` wins over a same-named jar entry.
- There is no path or domain scoping: the harness is single-origin and in-process.

```python
with TestClient(App(), cookie_jar=True) as client:
    client.post("/session", json={"token": "session-value"})
    assert client.cookie_jar["session_id"] == "session-value"

    resp = client.get("/profile")            # the jar's cookie rides along
    assert resp.status_code == 200

    client.delete("/session")                # logout expires the cookie
    assert "session_id" not in client.cookie_jar
```
