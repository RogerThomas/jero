"""The authenticator bases: ``Authenticator`` and the HTTP-scheme sugar on top of it.

Authentication itself is structural (the ``Auth`` / ``CookieAuth`` / ``HybridAuth``
protocols in ``_core``); these bases only carry wiring-time declarations a protocol
can't: the ``exceptions=`` class keyword, and, for ``BearerAuth`` / ``BasicAuth``, the
``openapi_security`` scheme. A leaf module (``_errors``, ``_openapi``, ``_wiring_types``),
split out of ``_core`` for pylint's module-size cap.
"""

from collections.abc import Sequence
from typing import ClassVar, cast

from jero._errors import BaseHTTPError, HTTPError, StructHTTPError
from jero._openapi import SecurityScheme
from jero._wiring_types import WiringError


def validated_error_classes(
    entries: Sequence[object], owner: str
) -> tuple[type[BaseHTTPError], ...]:
    """``entries`` validated as concrete jero error classes (``HTTPError`` or
    ``StructHTTPError`` subclasses), deduped in order; anything else is a ``WiringError``
    naming ``owner``. Un-underscored: ``_openapi_wiring`` validates a meta's
    ``exceptions=`` with it too, so both declaration sites share one rule."""
    validated: dict[type[BaseHTTPError], None] = {}
    for entry in entries:
        concrete = (
            isinstance(entry, type)
            and hasattr(entry, "status")
            and issubclass(entry, (HTTPError, StructHTTPError))
        )
        if not concrete:
            raise WiringError(
                f"{owner}: 'exceptions' entries must be concrete jero error "
                f"classes (HTTPError or StructHTTPError subclasses); got {entry!r}",
            )
        validated.setdefault(cast("type[BaseHTTPError]", entry), None)
    return tuple(validated)


def _stray_exceptions_error(owner: str) -> WiringError:
    """The error for an ``exceptions`` attribute anywhere on an authenticator: the class
    keyword is the only way to declare its errors, and jero never reads the attribute."""
    return WiringError(
        f"{owner}: declare its errors with the class keyword, "
        f"`class {owner}(Authenticator, exceptions=[...])`, not an `exceptions` attribute",
    )


class Authenticator:
    """The base for an authenticator that declares the errors it raises.

    Subclass it with the ``exceptions=`` class keyword, and every operation the
    authenticator guards documents those errors, in the OpenAPI spec and for
    ``TestClient``'s undeclared-error check, so they're declared once instead of on every
    route::

        class SessionAuth(Authenticator, exceptions=[InvalidSessionError]):
            async def authenticate(self, cookies: SessionCookies) -> User: ...

    The container type picks the rule, as for ``OperationMeta`` tags: a ``list`` extends
    the parents' errors (every ``Authenticator`` base's, combined), a (non-empty)
    ``tuple`` replaces them, and passing none inherits them. Implement
    ``authenticate`` as for any of the auth protocols; this base adds no stub, so a sync
    or ``async def`` one lints clean. :class:`BearerAuth` and :class:`BasicAuth` are
    subclasses, so they take the keyword too. Entries are validated as concrete jero error
    classes once, when the class is defined: anything else is a ``WiringError`` there.
    The validated errors are stored as ``__auth_exceptions__`` (as ``jero.Struct`` stores
    ``__model_meta__``), never under the keyword's own name, so an ``exceptions``
    attribute on the class, from its body or a mixin, is always a mistake and fails loud.
    """

    __auth_exceptions__: ClassVar[tuple[type[BaseHTTPError], ...]] = ()

    def __init_subclass__(
        cls, *, exceptions: Sequence[type[BaseHTTPError]] | None = None, **kwargs: object
    ) -> None:
        super().__init_subclass__(**kwargs)
        if hasattr(cls, "exceptions"):
            # From the class body or a mixin: it would bypass the validation below.
            raise _stray_exceptions_error(cls.__name__)
        if "__auth_exceptions__" in cls.__dict__:
            # A rebuilt class (@dataclass(slots=True)) carries its validated errors in
            # its copied namespace; recomputing from the bases would drop its own.
            return
        # Every Authenticator parent's errors, not just the first in the MRO's.
        inherited = tuple(
            dict.fromkeys(
                error
                for base in cls.__bases__
                if issubclass(base, Authenticator)
                for error in base.__auth_exceptions__
            )
        )
        # The container type chooses, as for OperationMeta tags: nothing declared
        # inherits, a tuple replaces the parents' errors, a list extends them.
        # Validated here, once per class.
        if not exceptions:
            cls.__auth_exceptions__ = inherited
            return
        base_errors = () if isinstance(exceptions, tuple) else inherited
        cls.__auth_exceptions__ = validated_error_classes([*base_errors, *exceptions], cls.__name__)


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


def authenticator_exceptions(auth: object) -> tuple[type[BaseHTTPError], ...]:
    """The errors an authenticator declares it raises, with the ``exceptions=`` class
    keyword (already validated once, when its class was defined), for every operation it
    guards to document, in the spec and for ``TestClient``'s undeclared-error check. An
    ``exceptions`` attribute on any authenticator, an instance field included, is a
    ``WiringError`` rather than silently ignored. Un-underscored: ``core`` calls it at
    every mount, WebSocket ones included."""
    if hasattr(auth, "exceptions"):
        raise _stray_exceptions_error(type(auth).__name__)
    if isinstance(auth, Authenticator):
        return type(auth).__auth_exceptions__
    return ()
