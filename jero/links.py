"""First-class typed URL reversal: ``Location`` (RFC 9110) and ``Link`` (RFC 8288).

A response points at another mounted operation without hand-formatting URLs. Build a
target three ways:

- ``from_operation(Class.operation, params=...)`` — the blessed, typed form. The method
  reference carries the class (its path) and the operation; the wrong ``params`` Struct
  is caught **at construction**.
- ``from_path(path)`` / ``from_url(url)`` — a literal root-relative path (composed with the
  app's URL base, like a reversed operation) or a verbatim fully-qualified URL.
- ``from_ref("name.operation", params=...)`` — a string escape hatch for genuine
  circular imports between feature modules; opt in per class with ``ref=``.

Resolution to a concrete URL happens at response send, against the app's wiring-time
reverse registry (it lives in ``core``); the URL is relative. The ``*Target`` types are
un-underscored package-internal boundary-crossers (``core`` resolves them), not public
API — only ``Location`` / ``Link`` are exported from :mod:`jero`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, get_type_hints

from msgspec import Struct

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Self


@dataclass(frozen=True, slots=True)
class OperationTarget:
    """A reversal target: a mounted operation (its function) plus the path ``params``."""

    operation: Callable[..., object]
    params: Struct | None


@dataclass(frozen=True, slots=True)
class RefTarget:
    """A reversal target addressed by a class's string ``ref`` and the operation name."""

    name: str
    operation: str
    params: Struct | None


@dataclass(frozen=True, slots=True)
class URLTarget:
    """A fully-qualified URL, used verbatim — never rewritten."""

    url: str


@dataclass(frozen=True, slots=True)
class PathTarget:
    """A root-relative path that picks up the app's URL base (static origin or the
    proxy's, plus prefix) the same way a reversed operation does."""

    path: str


type Target = OperationTarget | RefTarget | URLTarget | PathTarget


# Path Struct type per operation function, introspected once: get_type_hints is
# µs-expensive and the value is fixed per function. Mirrors core's _HEADER_FIELDS cache.
_OPERATION_PATH_TYPES: dict[Callable[..., object], type[Struct] | None] = {}


def _is_struct_type(ann: object) -> bool:
    return isinstance(ann, type) and issubclass(ann, Struct)


def _operation_path_type(operation: Callable[..., object]) -> type[Struct] | None:
    if operation not in _OPERATION_PATH_TYPES:
        path = get_type_hints(operation).get("path")
        _OPERATION_PATH_TYPES[operation] = path if _is_struct_type(path) else None
    return _OPERATION_PATH_TYPES[operation]


def _validate_operation_params(operation: Callable[..., object], params: Struct | None) -> None:
    """Loud & fast: the wrong ``params`` Struct fails the instant the link is built,
    introspected from the operation's own ``path`` annotation (no app/registry needed)."""
    expected = _operation_path_type(operation)
    label = getattr(operation, "__qualname__", repr(operation))
    if expected is None:
        if params is not None:
            raise TypeError(
                f"{label} takes no path params, but params of type "
                f"{type(params).__name__} was given",
            )
        return
    if params is None:
        raise TypeError(f"{label} requires params of type {expected.__name__}")
    # Exact type, not isinstance: params must be *the* path struct the operation declares.
    # isinstance would silently accept a subclass; we want an exact-shape contract that
    # fails loud, so the disable is deliberate (pylint's advice is wrong for this case).
    if type(params) is not expected:  # pylint: disable=unidiomatic-typecheck
        raise TypeError(
            f"{label} expects params of type {expected.__name__}, got {type(params).__name__}",
        )


def _parse_ref(ref: str, params: Struct | None) -> RefTarget:
    name, sep, operation = ref.partition(".")
    if not sep or not name or not operation or "." in operation:
        raise TypeError(f"ref must be 'name.operation', got {ref!r}")
    return RefTarget(name, operation, params)


@dataclass(frozen=True, slots=True)
class Location:
    """An RFC 9110 ``Location`` on a response — 201 Created, a redirect target, or the
    status URL on a 202. Build with a constructor; resolution happens at response send."""

    target: Target

    @classmethod
    def from_operation(
        cls, operation: Callable[..., object], *, params: Struct | None = None
    ) -> Self:
        """Point at a mounted operation; ``params`` (type-checked here) fills its slots."""
        _validate_operation_params(operation, params)
        return cls(OperationTarget(operation, params))

    @classmethod
    def from_url(cls, url: str) -> Self:
        """Point at a fully-qualified URL, used verbatim — never rewritten."""
        return cls(URLTarget(url))

    @classmethod
    def from_path(cls, path: str) -> Self:
        """Point at a root-relative path; it picks up the app's URL base (absolute origin /
        prefix) the same way a reversed operation does."""
        return cls(PathTarget(path))

    @classmethod
    def from_ref(cls, ref: str, *, params: Struct | None = None) -> Self:
        """Point at an operation by its class ``ref`` (``"name.operation"``) — the
        import-cycle hatch; prefer ``from_operation`` otherwise."""
        return cls(_parse_ref(ref, params))


@dataclass(frozen=True, slots=True)
class Link:
    """An RFC 8288 web link. A list of links joins into one ``Link`` header. ``rel`` is
    required; ``title`` and ``media_type`` (emitted as ``type=``) are optional."""

    target: Target
    rel: str
    title: str | None = None
    media_type: str | None = None

    @classmethod
    def from_operation(
        cls,
        operation: Callable[..., object],
        *,
        rel: str,
        params: Struct | None = None,
        title: str | None = None,
        media_type: str | None = None,
    ) -> Self:
        """Link to a mounted operation with relation ``rel``; ``params`` fills its slots."""
        _validate_operation_params(operation, params)
        return cls(OperationTarget(operation, params), rel, title, media_type)

    @classmethod
    def from_url(
        cls, url: str, *, rel: str, title: str | None = None, media_type: str | None = None
    ) -> Self:
        """Link to a fully-qualified URL, used verbatim — never rewritten."""
        return cls(URLTarget(url), rel, title, media_type)

    @classmethod
    def from_path(
        cls, path: str, *, rel: str, title: str | None = None, media_type: str | None = None
    ) -> Self:
        """Link to a root-relative path; it picks up the app's URL base the same way a
        reversed operation does."""
        return cls(PathTarget(path), rel, title, media_type)

    @classmethod
    def from_ref(
        cls,
        ref: str,
        *,
        rel: str,
        params: Struct | None = None,
        title: str | None = None,
        media_type: str | None = None,
    ) -> Self:
        """Link to an operation by its class ``ref`` (``"name.operation"``) — the
        import-cycle hatch; prefer ``from_operation`` otherwise."""
        return cls(_parse_ref(ref, params), rel, title, media_type)
