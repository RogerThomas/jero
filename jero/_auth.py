"""The authenticator bases: ``Authenticator`` and the HTTP-scheme sugar on top of it.

Authentication itself is structural (the ``Auth`` / ``CookieAuth`` / ``HybridAuth``
protocols in ``_core``); these bases only carry wiring-time declarations a protocol
can't: the ``exceptions=`` class keyword, and, for ``BearerAuth`` / ``BasicAuth``, the
``openapi_security`` scheme. A leaf module (``_errors`` and ``_openapi`` only), split
out of ``_core`` for pylint's module-size cap.
"""

from collections.abc import Sequence
from typing import ClassVar

from jero._errors import BaseHTTPError
from jero._openapi import SecurityScheme


class Authenticator:
    """The base for an authenticator that declares the errors it raises.

    Subclass it with the ``exceptions=`` class keyword, and every operation the
    authenticator guards documents those errors, in the OpenAPI spec and for
    ``TestClient``'s undeclared-error check, so they're declared once instead of on every
    route::

        class SessionAuth(Authenticator, exceptions=(InvalidSessionError,)):
            async def authenticate(self, cookies: SessionCookies) -> User: ...

    A subclass that passes no ``exceptions=`` inherits its parent's. Implement
    ``authenticate`` as for any of the auth protocols; this base adds no stub, so a sync
    or ``async def`` one lints clean. :class:`BearerAuth` and :class:`BasicAuth` are
    subclasses, so they take the keyword too. Entries are validated as concrete jero error
    classes when the authenticator is mounted.
    """

    exceptions: ClassVar[tuple[type[BaseHTTPError], ...]] = ()

    def __init_subclass__(
        cls, *, exceptions: Sequence[type[BaseHTTPError]] | None = None, **kwargs: object
    ) -> None:
        super().__init_subclass__(**kwargs)
        if exceptions is not None:
            cls.exceptions = tuple(exceptions)


class BearerAuth(Authenticator):
    """A base whose operations advertise HTTP bearer in the OpenAPI spec.

    Sugar over the ``openapi_security`` attribute the spec generator reads — subclass
    this instead of writing the attribute by hand, and implement ``authenticate`` (sync
    or ``async def``) as for any :class:`Auth`. Deliberately not a subclass of the
    protocol: there is no inherited ``authenticate`` stub to override, so either flavor
    lints clean. ``authenticate`` is checked by the type checker at the ``auth=`` mount and
    by jero at wiring, which also requires its ``headers`` to bind ``authorization`` (the
    header the scheme names). Takes ``exceptions=`` as :class:`Authenticator` does.
    """

    openapi_security: ClassVar[SecurityScheme] = SecurityScheme.http_bearer()


class BasicAuth(Authenticator):
    """A base whose operations advertise HTTP basic in the OpenAPI spec; otherwise as
    :class:`BearerAuth`."""

    openapi_security: ClassVar[SecurityScheme] = SecurityScheme.http_basic()
