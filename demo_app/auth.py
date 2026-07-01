"""Bearer-token authentication for the demo app.

A pure in-memory token-to-user map — no lifecycle resource, so it is built directly in
the app's ``wire`` rather than the factory. Swapping the factory in tests therefore
replaces only the I/O services and leaves auth wiring intact.
"""

from dataclasses import dataclass

from demo_app.models import Credentials, User
from jero import BearerAuth, HTTPError


@dataclass
class TokenAuth(BearerAuth[Credentials, User]):
    """Bearer-token authenticator over a static token-to-user map.

    Subclassing :class:`~jero.BearerAuth` makes its routes advertise HTTP bearer in the
    generated OpenAPI spec; the ``authenticate`` contract is otherwise unchanged.
    """

    _users: dict[str, User]

    # Auth.authenticate is declared sync-or-async (-> TUser | Awaitable[TUser]); pylint
    # only sees the sync arm of the union and flags the async override. It's a false positive.
    async def authenticate(self, headers: Credentials) -> User:  # pylint: disable=invalid-overridden-method
        """Resolve the bearer token to a user, or raise 401."""
        token = headers.authorization.removeprefix("Bearer ").strip()
        user = self._users.get(token)
        if user is None:
            raise HTTPError(401, "invalid token")
        return user
