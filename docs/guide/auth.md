# Authentication

Auth is an object you pass to `_include_resource` / `_include_endpoint`. It implements
one method:

```python
def authenticate(self, headers: THeaders) -> TUser: ...
```

- `headers` is bound from the request into your declared `Struct` (the same
  header-name mapping as the [`headers` binding](binding.md#headers-headers-typed-and-raw_headers-opaque)).
- The returned `Struct` is what handlers receive as `user`.
- Raise an `HTTPError` subclass to reject. `authenticate` may be sync or async.
- Declaring `-> TUser | None` instead makes the routes it is mounted on accept anonymous
  callers — see [optional authentication](#optional-authentication).

```python
from dataclasses import dataclass

from msgspec import Struct

from jero import BaseApp, Endpoint, HTTPError


class InvalidTokenError(
    HTTPError,
    type="invalid-token",
    title="Invalid token",
    status=401,
): ...


class Credentials(Struct):
    authorization: str            # reads the Authorization header


class User(Struct):
    id: str
    name: str


@dataclass
class TokenAuth:
    _users: dict[str, User]

    async def authenticate(self, headers: Credentials) -> User:
        token = headers.authorization.removeprefix("Bearer ").strip()
        user = self._users.get(token)
        if user is None:
            raise InvalidTokenError()
        return user
```

## Wiring it up

Pass `auth=` when including a resource or endpoint. It then runs for **every** method
on that resource, before the body is decoded:

```python
from dataclasses import dataclass

from msgspec import Struct

from jero import BaseApp, Endpoint, HTTPError


class InvalidTokenError(
    HTTPError,
    type="invalid-token",
    title="Invalid token",
    status=401,
): ...


class Credentials(Struct):
    authorization: str


class User(Struct):
    id: str
    name: str


@dataclass
class TokenAuth:
    _users: dict[str, User]

    async def authenticate(self, headers: Credentials) -> User:
        token = headers.authorization.removeprefix("Bearer ").strip()
        user = self._users.get(token)
        if user is None:
            raise InvalidTokenError()
        return user


class Health(Struct):
    status: str


class HealthEndpoint(Endpoint, path="/healthz"):
    async def get(self) -> Health:              # GET /healthz, open
        return Health(status="ok")


class WhoAmIEndpoint(Endpoint, path="/me"):
    async def get(self, user: User) -> User:    # receives the authenticate() result
        return user


class App(BaseApp):
    async def wire(self) -> None:
        auth = TokenAuth({"token": User(id="user-id", name="user-name")})
        self._include_endpoint(WhoAmIEndpoint(), auth=auth)
        self._include_endpoint(HealthEndpoint())   # no auth


app = App()
```

## Optional authentication

Sometimes credentials are an *input* rather than a gate: an anonymous caller is served,
an authenticated one is served differently, and the handler decides.

**The authenticator's return type is the policy.** `-> User` gates its routes; `-> User |
None` accepts anonymous callers, where returning `None` means "this caller presented no
credentials":

- **Absent** credentials bind `None` to the handler's `user`.
- **Present but invalid** credentials are still rejected with a 401 — raising is always
  rejection.
- Every handler on the route declares `user: User | None`.

Absent-vs-invalid is the *authenticator's* call, never a guess by the framework.

An app that wants both policies defines **two authenticators over one shared lookup**, so a
route's policy is visible in what its mount passes:

```python
from dataclasses import dataclass

from msgspec import Struct

from jero import AuthenticationRequiredError, BaseApp, BearerAuth, Endpoint, HTTPError


class InvalidTokenError(
    HTTPError,
    type="invalid-token",
    title="Invalid token",
    status=401,
): ...


class Credentials(Struct):
    authorization: str | None = None   # optional, so an absent header reaches authenticate


class User(Struct):
    id: str
    name: str


@dataclass
class TokenLookup:
    """Shared resolution; not an authenticator itself (it declares no authenticate)."""

    _users: dict[str, User]

    def _resolve(self, headers: Credentials) -> User | None:
        if headers.authorization is None:
            return None                      # nothing presented
        token = headers.authorization.removeprefix("Bearer ").strip()
        user = self._users.get(token)
        if user is None:
            raise InvalidTokenError()        # presented but bad -> 401 under either policy
        return user


class TokenAuth(TokenLookup, BearerAuth[Credentials, User]):
    async def authenticate(self, headers: Credentials) -> User:            # gates
        user = self._resolve(headers)
        if user is None:
            raise AuthenticationRequiredError()
        return user


class OptionalTokenAuth(TokenLookup, BearerAuth[Credentials, User]):
    async def authenticate(self, headers: Credentials) -> User | None:     # serves anonymous
        return self._resolve(headers)


class Spotlight(Struct):
    widget_id: str
    personalized_for: str | None


class WhoAmIEndpoint(Endpoint, path="/me"):
    async def get(self, user: User) -> User:                   # never None here
        return user


class SpotlightEndpoint(Endpoint, path="/spotlight"):
    async def get(self, user: User | None) -> Spotlight:       # None when anonymous
        return Spotlight(
            widget_id="spotlight",
            personalized_for=user.name if user is not None else None,
        )


class App(BaseApp):
    async def wire(self) -> None:
        users = {"token": User(id="user-id", name="user-name")}
        self._include_endpoint(WhoAmIEndpoint(), auth=TokenAuth(users))
        self._include_endpoint(SpotlightEndpoint(), auth=OptionalTokenAuth(users))


app = App()
```

`demo_app/auth.py` is exactly this shape. Note the two type-level declarations that make
absence expressible, both on the authenticator:

1. **`Credentials.authorization` is optional.** `authenticate` only ever sees credentials
   your `THeaders` Struct can bind. With a *required* field, a request without the header
   fails to bind and jero answers 401 before your code runs — right for a gated route, but
   it would make the anonymous case unreachable.
1. **`authenticate` returns `User | None`.** That is the whole declaration; there is no
   mount-site flag, so no route can be open without an authenticator that says so.

## The `user` argument is type-checked at startup

A handler receives the auth result by declaring a `user` argument. Its annotation is
checked against the authenticator's return type **at wiring time** — if a handler
declares `user: Admin` but the auth returns `User`, that's a `WiringError` before the
app ever serves a request. Declaring `user` without any auth configured is likewise a
startup error.

Its *optionality* is checked the same way — every direction fails loud:

| `authenticate` returns | `user: User`  | `user: User \| None` | no `user`     |
| ---------------------- | ------------- | -------------------- | ------------- |
| `User`                 | ✅            | `WiringError`        | ✅            |
| `User \| None`         | `WiringError` | ✅                   | `WiringError` |

Behind a gating authenticator the user is never `None` (an unauthenticated caller never
reaches the handler), so `User | None` would be a lie; behind an anonymous-accepting one it
can be, so bare `User` would be one.

The last cell is the important one. A handler that declares no `user` behind a gating
authenticator is fine — it just doesn't want the result, and the gate has already run:

```python
self._include_resource(WidgetResource(...), auth=TokenAuth(users))

async def read_many(self, params: Page) -> list[Widget]:   # no 'user' — still gated
    ...
```

Behind an **anonymous-accepting** authenticator the same handler is a startup error. There
would be no annotation anywhere saying the route serves anonymous callers, so a route could
silently go public. jero makes you write it down:

```python
async def read_many(self, params: Page) -> list[Widget]:
    ...
# WiringError: WidgetResource.read_many declares no 'user', but
# OptionalTokenAuth.authenticate returns User | None, so this route serves anonymous
# callers — declare 'user: User | None' and handle None, or mount it behind an
# authenticator that returns User to gate it
```

## Auth in the OpenAPI spec

An operation mounted behind `auth` gets a `security` requirement in the
[generated spec](openapi.md); one whose authenticator accepts anonymous callers gets the scheme *and*
an empty requirement object beside it — the spec's way of saying the credentials are
accepted but not required:

```json
"security": [{"bearerAuth": []}, {}]
```

The derived `401` response stays documented either way — invalid credentials remain
rejectable. To advertise the *scheme*, subclass an auth base instead of writing the
attribute by hand:

```python
from jero import BearerAuth


class TokenAuth(BearerAuth[Credentials, User]):   # adds {"type": "http", "scheme": "bearer"}
    async def authenticate(self, headers: Credentials) -> User:
        ...
```

`BearerAuth` and `BasicAuth` are sugar over an optional
`openapi_security: ClassVar[SecurityScheme]` attribute any authenticator can set; an
authed route that declares nothing defaults to HTTP bearer. For a token in a header,
query param, or cookie, set the attribute with `SecurityScheme.api_key(...)`. The value
must be a `SecurityScheme` (or absent / `None`) — anything else is a `WiringError` at
startup. See [OpenAPI & docs](openapi.md#security-schemes).
