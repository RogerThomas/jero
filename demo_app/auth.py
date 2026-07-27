"""Bearer-token authentication for the demo app.

Two authenticators over one shared token lookup, because an authenticator's declared return
type *is* its routes' auth policy: :class:`TokenAuth` returns ``User`` and gates, while
:class:`OptionalTokenAuth` returns ``User | None`` and lets an anonymous caller through. A
route's policy is therefore visible in which one its mount passes.

A pure in-memory token-to-user map — no lifecycle resource, so they are built directly in
the app's ``wire`` rather than the factory. Swapping the factory in tests therefore
replaces only the I/O services and leaves auth wiring intact.
"""

from dataclasses import dataclass

from demo_app.errors import InvalidTokenError
from demo_app.models import Credentials, User
from jero import AuthenticationRequiredError, BearerAuth


@dataclass
class TokenLookup:
    """The shared token-to-user resolution both authenticators are built on.

    Not an authenticator itself (it declares no ``authenticate``) — it exists so the two
    policies differ *only* in what they do about absent credentials.
    """

    _users: dict[str, User]

    def _resolve(self, headers: Credentials) -> User | None:
        """The user for the presented token: ``None`` if none was presented, raising if the
        token is present but unknown (invalid is a rejection, never an absence)."""
        if headers.authorization is None:
            return None
        token = headers.authorization.removeprefix("Bearer ").strip()
        user = self._users.get(token)
        if user is None:
            raise InvalidTokenError()
        return user


class TokenAuth(TokenLookup, BearerAuth[Credentials, User]):
    """Gating authenticator: every route mounted with it requires valid credentials.

    Subclassing :class:`~jero.BearerAuth` makes its routes advertise HTTP bearer in the
    generated OpenAPI spec.
    """

    # Auth.authenticate is declared sync-or-async (-> TUser | Awaitable[TUser]); pylint
    # only sees the sync arm of the union and flags the async override. It's a false positive.
    async def authenticate(self, headers: Credentials) -> User:  # pylint: disable=invalid-overridden-method
        """Resolve the bearer token to a user, or raise 401."""
        user = self._resolve(headers)
        if user is None:
            raise AuthenticationRequiredError()
        return user


class OptionalTokenAuth(TokenLookup, BearerAuth[Credentials, User]):
    """Anonymous-accepting authenticator: returning ``None`` reports that the caller
    presented no credentials, and the route's handlers are invoked with ``user=None``.

    Invalid credentials are still rejected — only *absence* is passed through. Handlers on
    these routes must declare ``user: User | None``; jero checks that at startup.
    """

    async def authenticate(self, headers: Credentials) -> User | None:  # pylint: disable=invalid-overridden-method
        """Resolve the bearer token to a user, ``None`` if none was presented, or raise 401."""
        return self._resolve(headers)
