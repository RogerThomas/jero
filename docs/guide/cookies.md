# Cookies

Cookies are a first-class, typed, compiled binding source — reading them is `cookies`,
right beside `headers`; setting them is `SetCookie` on any response wrapper; and an
authenticator can gate a route on a session cookie exactly as it gates on a bearer
header, including on a WebSocket handshake.

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

**Cookie names bind verbatim and case-sensitively — there is no header-style mangle.**
`headers` lower-cases and turns `-` into `_` (`X-Trace-Id` → `x_trace_id`); `cookies`
does none of that, because RFC 6265 cookie names are case-sensitive and routinely use
shapes that aren't Python identifiers at all (`__Host-session`, `csrftoken`). A field
name simply *is* the cookie name it binds. For a name that isn't a valid identifier, use
msgspec's per-field rename:

```python
from msgspec import field
from msgspec import Struct


class HostCookies(Struct):
    host_token: str | None = field(default=None, name="__Host-token")
```

A missing required cookie, or a value that fails conversion (a non-`int` where you
declared `int`), is a **400** — exactly like `headers`.

## Lenient parsing, strict binding

A browser sends *every* cookie scoped to the domain and path in one `Cookie` header,
including cookies set by other applications sharing that domain. jero's cookie parser
reflects that reality:

- A malformed *fragment* — `;;`, a nameless `=x`, a bare token with no `=` — is skipped,
  never a 400. Rejecting a request because an unrelated app's cookie was malformed would
  be unacceptable.
- A cookie your `Struct` doesn't mention is silently ignored (ordinary msgspec
  `convert` behavior).
- If a name repeats, the **first** occurrence wins.
- A value wrapped in one pair of `"` quotes has them stripped; nothing else is
  unescaped — RFC 6265 defines no escaping convention, and jero does not guess at one.
- A client that splits the `Cookie` header across several field lines (HTTP/2 permits
  this) has them joined with `"; "` before parsing, so nothing from either line is lost.

What *is* strict is your own declared fields: a required cookie that's absent, or a
value that fails to convert, is a 400 — the same contract as every other binding
source. Lenient only applies to the noise around your cookies, never to them.

## Setting cookies — `SetCookie`

Every response wrapper — `JSONResponse`, `BytesResponse`, `NoContent`, `Created`,
`Accepted`, the streaming responses, `ExceptionResponse` — takes a `cookies:
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

Loosening any of that — `http_only=False` for a cookie your JavaScript needs to read,
`secure=False`, a different `same_site` — is explicit and therefore visible in review.
Nobody accidentally ships a cookie a script can steal or that rides along on a
cross-site request.

This holds even in local development: modern browsers treat `http://localhost`
as a trustworthy origin specifically so that `Secure` cookies still work without HTTPS.
If a cookie you set locally doesn't seem to be arriving, the cause is essentially never
`secure=True` — don't "fix" it by turning `secure` off.

Construction validates immediately (`ValueError` at the `SetCookie(...)` call, not at
send time): the name must be a legal RFC 6265 token, the value must contain no
control characters, quotes, commas, semicolons, or backslashes, `same_site="none"`
requires `secure=True`, and a `__Host-`/`__Secure-`-prefixed name enforces the
attributes those prefixes promise (`__Host-` additionally requires `path="/"` and no
`domain`).

Setting two `SetCookie` entries with the same name in one response is a bug, not a
choice between them — it raises at send time.

## Deleting a cookie — `SetCookie.expire`

Don't hand-build the deletion recipe (`Max-Age=0`, hoping the client honors it). Use the
named constructor — `SessionEndpoint.delete` above already does — which sends both
`Max-Age=0` **and** an `Expires` at the Unix epoch, for clients that only look at one of
them.

`path`/`domain` must match the cookie you're clearing — a browser only removes a cookie
whose scope matches exactly, so `SetCookie.expire("session_id", path="/app")` if that's
where you originally scoped it.

## Cookie auth

An authenticator may declare `cookies` instead of, or alongside, `headers`. The
return-type-is-the-policy contract from [Authentication](auth.md) is unchanged — only
what `authenticate` binds from is different:

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

`SessionAuth` needs no base class — jero's wiring introspects the concrete
`authenticate` signature, so any object satisfying the shape structurally works,
exactly as with header auth. Two more shapes follow the same rule:

- `authenticate(self, cookies: TCookies) -> TUser` — session-cookie auth, as above.
- `authenticate(self, headers: THeaders, cookies: TCookies) -> TUser` — **hybrid**: one
  authenticator serving both a bearer-token API client and a cookie-carrying browser
  client on the same routes.

For static typing, `CookieAuth[TCookies, TUser]` and `HybridAuth[THeaders, TCookies,
TUser]` sit beside `Auth[THeaders, TUser]` — every rule from
[Authentication](auth.md) still applies per source: give a cookie field a `| None`
default to let its absence reach `authenticate` rather than failing binding before your
code runs, and an anonymous-accepting authenticator (`-> TUser | None`) still requires
every handler on its routes to declare `user: TUser | None`.

### WebSockets: the motivating case

Cookie auth exists in large part *for* this: a browser's WebSocket API has no way to
set an `Authorization` header, but it always sends cookies for the connecting origin.
`cookies` joins the handshake's binding sources exactly like `headers` (see
[WebSockets](websockets.md#handshake-binding-and-authentication)) — mount a
`CookieAuth`/`HybridAuth` authenticator on `_include_websocket` and a browser client
authenticates with the session cookie it already has, no extra handshake step needed.

## The OpenAPI boundary

Request-side cookies document like any other param — `in: cookie`, named verbatim (the
same no-mangle rule as binding, so a renamed field's wire name is what appears). Cookie
auth derives a security scheme automatically when it can: a headers-only authenticator
defaults to HTTP bearer as always; a cookies-only authenticator whose `Struct` has
**exactly one field** derives an `apiKey` scheme with `in: cookie`, named for that field.
A hybrid authenticator, or a cookies-only one with several fields, can't be reduced to
one scheme — declare `openapi_security` on the class yourself (see
[Auth in the OpenAPI spec](auth.md#auth-in-the-openapi-spec)), or startup fails once
`_include_openapi` is wired, naming the class and the fix.

**Response `Set-Cookie` is never modeled in the spec.** Two independent reasons: OpenAPI
has no way to describe *several* `Set-Cookie` headers on one response, and a response's
cookies are per-instance runtime values (`SetCookie("session_id", token)`), not part of
the static return type the way a typed `headers` Struct is. This is the same boundary
streaming responses draw around their item type — a stated absence, not a gap. If you
need a single, documented cookie-shaped value in the spec, put it on a typed `headers`
Struct field instead; it just won't render as a real `Set-Cookie` attribute set.

## Testing

`TestClient` mirrors every piece of this end to end.

**Sending cookies** — pass `cookies=` on any request or `client.websocket(...)` call,
encoded as one `Cookie` header:

```python
resp = client.get("/theme", cookies={"session_id": "session-value"})
```

Passing `cookies=` *and* an explicit `Cookie` entry in `headers=` is ambiguous and
raises `ValueError`.

**Reading `Set-Cookie` back** — `TestResponse.cookies` is a `dict[str, TestCookie]`
parsed from the response, one entry per cookie name:

```python
resp = client.post("/session", json={"token": "session-value"})
cookie = resp.cookies["session_id"]
assert cookie.value == "session-value"
assert cookie.http_only and cookie.secure
```

**A session that should persist across requests** — pass `cookie_jar=True` to
`TestClient`. Off by default (every request sends only what you explicitly pass,
nothing hidden); on, each response's `Set-Cookie` values are stored in
`client.cookie_jar` — a plain, directly inspectable and mutable `dict[str, str]` — and
attached to subsequent requests and WebSocket handshakes automatically. An expiring
`Set-Cookie` (`Max-Age=0`, or a past `Expires`, exactly what `SetCookie.expire()` sends)
removes its entry, so a logout test can assert the jar emptied:

```python
with TestClient(App(), cookie_jar=True) as client:
    client.post("/session", json={"token": "session-value"})
    assert client.cookie_jar["session_id"] == "session-value"

    resp = client.get("/profile")            # the jar's cookie rides along
    assert resp.status_code == 200

    client.delete("/session")                # logout expires the cookie
    assert "session_id" not in client.cookie_jar
```

Per-request `cookies=` always wins over a same-named jar entry, and the jar is a plain
dict you can seed or clear by hand — there is no path/domain scoping, deliberately: the
harness is single-origin and in-process, so RFC 6265 scoping rules would be dead code.
