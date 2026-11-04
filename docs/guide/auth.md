# Authentication

Auth is an object you pass to `_include_resource` / `_include_endpoint`. It implements
one method:

```python
def authenticate(self, headers: THeaders) -> TUser: ...
```

- `headers` is bound from the request into your declared `Struct` (the same
  header-name mapping as the [`headers` binding](binding.md#headers-headers-typed-and-raw_headers-opaque)).
- The return value is what handlers receive as `user`, untouched. `TUser` is your choice:
  a `Struct`, a dataclass, an ORM model, a bare `int` id, a union of several. Handlers must
  accept every type it can return: the same type, a base class of it, or a wider union
  (a Protocol only if the class explicitly inherits it), checked at startup.
- Raise an `HTTPError` subclass to reject. `authenticate` may be sync or async.
- Declaring `-> TUser | None` instead makes the routes it is mounted on accept anonymous
  callers — see [optional authentication](#optional-authentication).

`authenticate` may instead (or also) declare `cookies: TCookies` — session-cookie auth,
or a **hybrid** authenticator serving both a bearer-token API client and a
cookie-carrying browser client on the same routes. Everything on this page — the
return-type policy, optional auth, the `user` cross-check, OpenAPI security — applies
per source exactly the same way. See [Cookies](cookies.md#cookie-auth) for the full
treatment, including the WebSocket case cookie auth exists for.

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from dataclasses import dataclass

from msgspec import Struct

from jero import Authenticator, BaseApp, Endpoint, HTTPError


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
class TokenAuth(Authenticator, exceptions=(InvalidTokenError,)):  # the error it raises
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
on that resource, before the body is decoded (the authenticator here is a trivial
stand-in for the one above):

```python
from msgspec import Struct

from jero import Authenticator, BaseApp, Endpoint, HTTPError


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


class TokenAuth(Authenticator, exceptions=(InvalidTokenError,)):
    async def authenticate(self, headers: Credentials) -> User:
        if headers.authorization != "Bearer token":
            raise InvalidTokenError()
        return User(id="user-id", name="user-name")


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
        self._include_endpoint(WhoAmIEndpoint(), auth=TokenAuth())
        self._include_endpoint(HealthEndpoint())   # no auth


app = App()
```

### Mixing policies on one REST resource

`auth=` is one policy for the whole mount, so a `Resource` can't have a public
`read_one` next to a gated `create` — every method on it shares whatever `auth` the
mount was given. Split the methods across two `Resource` subclasses at the same
`path`, each implementing only the methods that share a policy, each with its own
`auth=` (or none):

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
class PublicWidgets(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> Widget: ...


class GatedWidgets(Resource, path="/widgets"):
    async def create(self, json: WidgetIn, user: User) -> Widget: ...


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(PublicWidgets())
        self._include_resource(GatedWidgets(), auth=TokenAuth())
```

Two mounts at the same path with disjoint methods is fine — routing dispatches by
method, not by which class declared it.

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


class TokenAuth(TokenLookup, BearerAuth, exceptions=(InvalidTokenError,)):
    async def authenticate(self, headers: Credentials) -> User:            # gates
        user = self._resolve(headers)
        if user is None:
            raise AuthenticationRequiredError()
        return user


class OptionalTokenAuth(TokenLookup, BearerAuth, exceptions=(InvalidTokenError,)):
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
# doc-example: fragment (illustrative snippet, not a runnable app)
from jero import BearerAuth


class TokenAuth(BearerAuth):   # adds {"type": "http", "scheme": "bearer"}
    async def authenticate(self, headers: Credentials) -> User:
        ...
```

`BearerAuth` and `BasicAuth` are plain bases, not the `Auth` protocol itself, so there is
no inherited `authenticate` to override: write it sync or `async def` and it lints clean
either way. Import the `headers`/`cookies` Struct normally, not under `if TYPE_CHECKING:`:
jero reads the annotation at startup (see the
[FAQ](../faq.md#why-does-ruff-want-my-struct-imports-under-type_checking)). The type
checker checks `authenticate` at the `auth=` mount, and jero checks it at startup,
including that its `headers` Struct binds `authorization`: bearer and basic name the
`Authorization` header, so either scheme on an authenticator that reads a cookie or another
header is a `WiringError`.
Both are sugar over an optional
`openapi_security: ClassVar[SecurityScheme]` attribute any authenticator can set; an
authed route that declares nothing defaults to HTTP bearer. For a token in a header,
query param, or cookie, set the attribute with `SecurityScheme.api_key(...)`. The value
must be a `SecurityScheme` (or absent / `None`) — anything else is a `WiringError` at
startup. See [OpenAPI & docs](openapi.md#security-schemes).

### Declaring the authenticator's errors

An authenticator often raises errors of its own: an invalid token, or a 503 when the
identity provider can't be reached. Declare them once, with the `exceptions=` class
keyword on `Authenticator`, and every operation the authenticator guards documents them
in the spec, with no entry on each route. `BearerAuth` and `BasicAuth` are
`Authenticator`s, so they take the keyword too:

```python
from dataclasses import dataclass

from msgspec import Struct

from jero import BaseApp, BearerAuth, Endpoint, HTTPError


class Credentials(Struct):
    authorization: str


class User(Struct):
    user_id: str


class InvalidTokenError(HTTPError, type="invalid-token", title="Invalid token", status=401): ...


class ProviderUnavailableError(
    HTTPError, type="provider-unavailable", title="Provider unavailable", status=503
): ...


@dataclass
class TokenAuth(BearerAuth, exceptions=(InvalidTokenError, ProviderUnavailableError)):
    _users: dict[str, User] | None  # None: the identity provider couldn't be reached

    async def authenticate(self, headers: Credentials) -> User:
        if self._users is None:
            raise ProviderUnavailableError()
        user = self._users.get(headers.authorization.removeprefix("Bearer "))
        if user is None:
            raise InvalidTokenError()
        return user


class MeEndpoint(Endpoint, path="/me"):
    async def get(self, user: User) -> User:
        return user


class App(BaseApp):
    async def wire(self) -> None:
        auth = TokenAuth({"token": User(user_id="user-id")})
        self._include_endpoint(MeEndpoint(), auth=auth)
        self._include_openapi(title="Me API", version="1.0.0")


app = App()
```

They count as declared for
[`TestClient`'s check](openapi.md#keeping-the-list-complete) too, so a test that sends a
bad token to an operation listing its own `exceptions` passes. Nothing else does: an
authenticator error it doesn't declare fails that check like any other, 401s included.
The keyword is type-checked, a subclass that passes none inherits its parent's, and the
entries are checked once, when the class is defined: anything but a jero error class is
a `WiringError` right there, as is an `exceptions` attribute in the class body (the
keyword is the only form). One on an authenticator outside these bases, which jero
would never read, is a `WiringError` when it's mounted.
