# Authentication

Auth is an object you pass to `_include_resource` / `_include_endpoint`. It implements
one method:

```python
def authenticate(self, headers: THeaders) -> TUser: ...
```

- `headers` is bound from the request into your declared `Struct` (the same
  header-name mapping as the [`headers` binding](binding.md#headers-headers-typed-and-raw_headers-opaque)).
- The returned `Struct` is what handlers receive as `user`.
- Raise `HTTPError(401, ...)` to reject. `authenticate` may be sync or async.

```python
from dataclasses import dataclass

from msgspec import Struct

from jero import BaseApp, Endpoint, HTTPError


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
            raise HTTPError(401, "invalid token")
        return user
```

## Wiring it up

Pass `auth=` when including a resource or endpoint. It then runs for **every** method
on that resource, before the body is decoded:

```python
class WhoAmIEndpoint(Endpoint):
    async def get(self, user: User) -> User:    # receives the authenticate() result
        return user


class App(BaseApp):
    async def _wire(self) -> None:
        auth = TokenAuth({"token": User(id="user-id", name="user-name")})
        self._include_resource(WidgetResource(...), path="/widgets", auth=auth)
        self._include_endpoint(WhoAmIEndpoint(), path="/me", auth=auth)
        self._include_endpoint(HealthEndpoint(), path="/healthz")   # no auth
```

## The `user` argument is type-checked at startup

A handler receives the auth result by declaring a `user` argument. Its annotation is
checked against the authenticator's return type **at wiring time** — if a handler
declares `user: Admin` but the auth returns `User`, that's a `WiringError` before the
app ever serves a request. Declaring `user` without any auth configured is likewise a
startup error.

Handlers that don't declare `user` still run behind the auth gate; they just don't
receive the result.
