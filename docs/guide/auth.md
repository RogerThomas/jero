# Authentication

An authenticator is an object with one method, `authenticate`. You pass it as `auth=`
when you mount a resource or endpoint, and whatever it returns is what handlers receive
as `user`.

```python
from dataclasses import dataclass

from msgspec import Struct

from jero import BaseApp, BearerAuth, Endpoint, HTTPError


class InvalidTokenError(HTTPError, type="invalid-token", title="Invalid token", status=401): ...


class Credentials(Struct):
    authorization: str                           # the Authorization header


class User(Struct):
    id: str
    name: str


@dataclass
class TokenAuth(BearerAuth, exceptions=[InvalidTokenError]):
    _users: dict[str, User]

    async def authenticate(self, headers: Credentials) -> User:
        user = self._users.get(headers.authorization.removeprefix("Bearer "))
        if user is None:
            raise InvalidTokenError()            # reject
        return user                              # becomes the handler's `user`


class Health(Struct):
    status: str


class HealthEndpoint(Endpoint, path="/healthz"):
    async def get(self) -> Health:               # open: mounted without auth
        return Health(status="ok")


class WhoAmIEndpoint(Endpoint, path="/me"):
    async def get(self, user: User) -> User:     # gated: receives the User
        return user


class App(BaseApp):
    async def wire(self) -> None:
        users = {"token": User(id="user-id", name="user-name")}
        self._include_endpoint(WhoAmIEndpoint(), auth=TokenAuth(users))
        self._include_endpoint(HealthEndpoint())


app = App()
```

What each piece does:

- **`headers: Credentials`** is bound from the request into your Struct, with the same
  name mapping as the [`headers` binding](binding.md#headers-headers-typed-and-raw_headers-opaque).
  A request missing a required field is answered 401 before `authenticate` runs.
- **The return value** is handed to handlers as `user`, untouched. It never crosses the
  wire, so it can be any type: a Struct, a dataclass, an ORM model, a bare `int` id.
- **Raising** any `HTTPError` rejects the request.
- **`auth=`** covers every operation on that mount and runs before the body is decoded.
  The same keyword works on `_include_resource` and `_include_websocket`.

`authenticate` may be sync or `async def`. Import its Structs normally, not under
`if TYPE_CHECKING:`, because jero reads the annotations at startup (see the
[FAQ](../faq.md#why-does-ruff-want-my-struct-imports-under-type_checking)).

## Which base class

None of the bases authenticates anything: reading the header is always your
`authenticate`. A base only sets the scheme the
[OpenAPI spec](#auth-in-the-openapi-spec) advertises and takes `exceptions=`.

| Subclass        | Client sends                         | Spec advertises |
| --------------- | ------------------------------------ | --------------- |
| `BearerAuth`    | `Authorization: Bearer <token>`      | HTTP bearer     |
| `BasicAuth`     | `Authorization: Basic <base64 user:password>` | HTTP basic |
| `Authenticator` | anything else                        | derived from what `authenticate` binds |

`BearerAuth` and `BasicAuth` behave identically at runtime. The only difference is the
scheme in the spec, and so whether `/docs` asks for a token or a username and password.
A plain class with no base also works, but can't declare `exceptions=`.

## The `user` argument

A handler asks for the auth result by declaring `user`. jero checks the annotation
against what `authenticate` returns when the app starts, so a mismatch is a
`WiringError` and never a surprise at request time.

- The handler must accept every type `authenticate` can return: the same type, a base
  class of it, or a wider union. A Protocol counts only if the class inherits it
  explicitly.
- A handler that doesn't need the result can leave `user` out. The route is still gated.
- Declaring `user` on a mount with no `auth=` is a `WiringError`.

## Public and gated operations on one path

`auth=` is one policy for the whole mount, so a single `Resource` can't have an open
`read_one` beside a gated `create`. Split them into two classes at the same path:

```python
from msgspec import Struct

from jero import BaseApp, BearerAuth, HTTPError, Resource


class InvalidTokenError(HTTPError, type="invalid-token", title="Invalid token", status=401): ...


class Credentials(Struct):
    authorization: str


class User(Struct):
    id: str


class TokenAuth(BearerAuth, exceptions=[InvalidTokenError]):
    async def authenticate(self, headers: Credentials) -> User:
        if headers.authorization != "Bearer token":
            raise InvalidTokenError()
        return User(id="user-id")


class WidgetIn(Struct):
    name: str


class Widget(Struct):
    id: str
    name: str


class WidgetPath(Struct):
    widget_id: str


class PublicWidgetResource(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> Widget:           # GET /widgets/{id}, open
        return Widget(id=path.widget_id, name="name")


class GatedWidgetResource(Resource, path="/widgets"):
    async def create(self, json: WidgetIn, user: User) -> Widget:   # POST /widgets, gated
        return Widget(id="widget-id", name=json.name)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(PublicWidgetResource())
        self._include_resource(GatedWidgetResource(), auth=TokenAuth())


app = App()
```

Routing dispatches by method, so two mounts at one path are fine as long as their
operations don't overlap.

## Optional authentication

Sometimes credentials are an input, not a gate: anonymous callers are served, and
signed-in callers are served differently.

**The return type of `authenticate` is the policy.** `-> User` gates the routes it is
mounted on. `-> User | None` accepts anonymous callers, and returning `None` means "this
caller presented no credentials".

An app that needs both policies defines two authenticators over one shared lookup, so
each route's policy is visible in what its mount passes:

```python
from dataclasses import dataclass

from msgspec import Struct

from jero import AuthenticationRequiredError, BaseApp, BearerAuth, Endpoint, HTTPError


class InvalidTokenError(HTTPError, type="invalid-token", title="Invalid token", status=401): ...


class Credentials(Struct):
    authorization: str | None = None   # optional, so a missing header reaches authenticate


class User(Struct):
    id: str
    name: str


@dataclass
class TokenLookup:
    """The shared lookup. Not an authenticator: it has no authenticate method."""

    _users: dict[str, User]

    def _resolve(self, headers: Credentials) -> User | None:
        if headers.authorization is None:
            return None                      # nothing presented
        user = self._users.get(headers.authorization.removeprefix("Bearer "))
        if user is None:
            raise InvalidTokenError()        # presented but wrong: 401 under either policy
        return user


class TokenAuth(TokenLookup, BearerAuth, exceptions=[InvalidTokenError]):
    async def authenticate(self, headers: Credentials) -> User:            # gates
        user = self._resolve(headers)
        if user is None:
            raise AuthenticationRequiredError()
        return user


class OptionalTokenAuth(TokenLookup, BearerAuth, exceptions=[InvalidTokenError]):
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

`demo_app/auth.py` has exactly this shape. Three rules make it work:

- **The credential field is optional.** `authenticate` only sees what its Struct can
  bind. With a required `authorization`, a request without the header is answered 401
  before your code runs, so the anonymous case could never be reached.
- **Absent is not the same as invalid.** Return `None` for no credentials. Raise for
  wrong ones. jero never guesses which is which.
- **Every handler declares `user: User | None`.** A handler that leaves `user` out is
  fine behind a gate, but behind an anonymous-accepting authenticator nothing in the
  code would say the route is open, so jero refuses to start.

The handler's `user` must match the authenticator's return type in both directions:

| `authenticate` returns | `user: User`  | `user: User \| None` | no `user`     |
| ---------------------- | ------------- | -------------------- | ------------- |
| `User`                 | ✅            | `WiringError`        | ✅            |
| `User \| None`         | `WiringError` | ✅                   | `WiringError` |

The bottom-right error tells you what to do:

```text
WiringError: WidgetResource.read_many declares no 'user', but
OptionalTokenAuth.authenticate returns User | None, so this route serves anonymous
callers — declare 'user: User | None' and handle None, or mount it behind an
authenticator that returns User to gate it
```

## Cookies and hybrid authenticators

`authenticate` can take `cookies: TCookies` instead of `headers`, or both together. That
gives you session-cookie auth, or one authenticator serving API clients and browsers on
the same routes. Everything on this page applies unchanged. See
[Cookie auth](cookies.md#cookie-auth), including why browser WebSockets need it.

## Auth in the OpenAPI spec

Every operation mounted behind `auth=` gets a `security` requirement in the
[generated spec](openapi.md) and a documented `401`. The scheme it names comes from the
authenticator:

| Authenticator                              | Scheme in the spec                    |
| ------------------------------------------ | ------------------------------------- |
| `BearerAuth`                               | HTTP bearer                           |
| `BasicAuth`                                | HTTP basic                            |
| no scheme declared, binds `headers` only   | HTTP bearer                           |
| no scheme declared, binds one cookie field | `apiKey` in a cookie named for it     |
| anything else                              | set `openapi_security` yourself       |

`BearerAuth` and `BasicAuth` are shorthand for a class attribute,
`openapi_security: ClassVar[SecurityScheme]`, that any authenticator can set. For a key
in a custom header, a query parameter, or a cookie, set it with
`SecurityScheme.api_key(...)`. See [security schemes](openapi.md#security-schemes).

Two checks run at startup:

- Bearer and basic both name the `Authorization` header, so an authenticator declaring
  either must bind an `authorization` field in its `headers` Struct.
- `openapi_security` must be a `SecurityScheme` or `None`.

An operation behind an [anonymous-accepting](#optional-authentication) authenticator
lists the scheme and an empty requirement beside it, which is how OpenAPI says
credentials are accepted but not required:

```json
"security": [{"bearerAuth": []}, {}]
```

### Declaring the authenticator's errors

An authenticator often raises errors of its own: an invalid token, or a 503 when the
identity provider can't be reached. List them once with the `exceptions=` class keyword
and every operation the authenticator guards documents them, with nothing to repeat on
each route:

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
class TokenAuth(BearerAuth, exceptions=[InvalidTokenError, ProviderUnavailableError]):
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

The details:

- **Tests.** The listed errors also pass
  [`TestClient`'s undeclared-error check](openapi.md#keeping-the-list-complete). An
  error the authenticator raises but doesn't list fails that check, 401s included.
- **Subclasses.** A list extends the parent's errors, a tuple replaces them, and leaving
  the keyword out inherits them. With several `Authenticator` parents, their errors are
  combined.
- **The keyword is the only form.** An `exceptions` attribute on an authenticator is a
  `WiringError`, as is an entry that isn't a jero error class.
