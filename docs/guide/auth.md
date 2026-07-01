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
    async def _wire(self) -> None:
        auth = TokenAuth({"token": User(id="user-id", name="user-name")})
        self._include_endpoint(WhoAmIEndpoint(), auth=auth)
        self._include_endpoint(HealthEndpoint())   # no auth


app = App()
```

## The `user` argument is type-checked at startup

A handler receives the auth result by declaring a `user` argument. Its annotation is
checked against the authenticator's return type **at wiring time** — if a handler
declares `user: Admin` but the auth returns `User`, that's a `WiringError` before the
app ever serves a request. Declaring `user` without any auth configured is likewise a
startup error.

!!! note

    Handlers that don't declare `user` still run behind the auth gate; they just don't
    receive the result.
