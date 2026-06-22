"""Bearer-token authentication for the demo app.

A pure in-memory token-to-user map — no lifecycle resource, so it is built directly in
the app's ``_wire`` rather than the factory. Swapping the factory in tests therefore
replaces only the I/O services and leaves auth wiring intact.
"""

from dataclasses import dataclass

from demo_app.models import Credentials, User
from jero import HTTPError


@dataclass
class TokenAuth:
    """Bearer-token authenticator over a static token-to-user map."""

    _users: dict[str, User]

    async def authenticate(self, headers: Credentials) -> User:
        """Resolve the bearer token to a user, or raise 401."""
        token = headers.authorization.removeprefix("Bearer ").strip()
        user = self._users.get(token)
        if user is None:
            raise HTTPError(401, "invalid token")
        return user
