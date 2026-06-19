"""First-class typed URL reversal: ``Location`` (RFC 9110) and ``Link`` (RFC 8288).

A response points at another mounted operation without hand-formatting URLs. Build a
target three ways:

- ``from_operation(Class.operation, path=...)`` — the blessed, typed form. The method
  reference carries the class (its path) and the operation; the wrong ``path`` Struct
  is caught **at construction**.
- ``from_path(path)`` / ``from_url(url)`` — a literal root-relative path (composed with the
  app's URL base, like a reversed operation) or a verbatim fully-qualified URL.
- ``from_ref("name.operation", path=...)`` — a string escape hatch for genuine
  circular imports between feature modules; opt in per class with ``ref=``.

Resolution to a concrete URL happens at response send, against the app's wiring-time
reverse registry (it lives in ``core``); the URL is relative. The ``*Target`` types are
un-underscored package-internal boundary-crossers (``core`` resolves them), not public
API — only ``Location`` / ``Link`` are exported from :mod:`jero`.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, get_type_hints

from msgspec import Struct

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Self


@dataclass(frozen=True, slots=True)
class OperationTarget:
    """A reversal target: a mounted operation (its function) plus the ``path`` Struct."""

    operation: Callable[..., object]
    path: Struct | None


@dataclass(frozen=True, slots=True)
class RefTarget:
    """A reversal target addressed by a class's string ``ref`` and the operation name."""

    name: str
    operation: str
    path: Struct | None


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


def validate_path_params(expected: type[Struct] | None, path: Struct | None, label: str) -> None:
    """Validate the ``path`` Struct against an operation's declared path type (the *exact*
    type), raising ``TypeError`` on any mismatch. Shared by ``from_operation`` (at
    construction) and ``from_ref`` resolution (deferred, called from ``core``) — an
    un-underscored boundary-crosser, not public API."""
    if expected is None:
        if path is not None:
            raise TypeError(
                f"{label} takes no path params, but path of type {type(path).__name__} was given",
            )
        return
    if path is None:
        raise TypeError(f"{label} requires path of type {expected.__name__}")
    # Exact type, not isinstance: path must be *the* path struct the operation declares.
    # isinstance would silently accept a subclass; we want an exact-shape contract that
    # fails loud, so the disable is deliberate (pylint's advice is wrong for this case).
    if type(path) is not expected:  # pylint: disable=unidiomatic-typecheck
        raise TypeError(
            f"{label} expects path of type {expected.__name__}, got {type(path).__name__}",
        )


def _validate_operation_path(operation: Callable[..., object], path: Struct | None) -> None:
    """Loud & fast: the wrong ``path`` Struct fails the instant the link is built,
    introspected from the operation's own ``path`` annotation (no app/registry needed)."""
    label = getattr(operation, "__qualname__", repr(operation))
    validate_path_params(_operation_path_type(operation), path, label)


def _parse_ref(ref: str, path: Struct | None) -> RefTarget:
    name, sep, operation = ref.partition(".")
    if not sep or not name or not operation or "." in operation:
        raise TypeError(f"ref must be 'name.operation', got {ref!r}")
    return RefTarget(name, operation, path)


@dataclass(frozen=True, slots=True)
class Location:
    """An RFC 9110 ``Location`` on a response — 201 Created, a redirect target, or the
    status URL on a 202. Build with a constructor; resolution happens at response send."""

    target: Target

    @classmethod
    def from_operation(
        cls, operation: Callable[..., object], *, path: Struct | None = None
    ) -> Self:
        """Point at a mounted operation; ``path`` (type-checked here) fills its slots."""
        _validate_operation_path(operation, path)
        return cls(OperationTarget(operation, path))

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
    def from_ref(cls, ref: str, *, path: Struct | None = None) -> Self:
        """Point at an operation by its class ``ref`` (``"name.operation"``) — the
        import-cycle hatch; prefer ``from_operation`` otherwise."""
        return cls(_parse_ref(ref, path))


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
        path: Struct | None = None,
        title: str | None = None,
        media_type: str | None = None,
    ) -> Self:
        """Link to a mounted operation with relation ``rel``; ``path`` fills its slots."""
        _validate_operation_path(operation, path)
        return cls(OperationTarget(operation, path), rel, title, media_type)

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
        path: Struct | None = None,
        title: str | None = None,
        media_type: str | None = None,
    ) -> Self:
        """Link to an operation by its class ``ref`` (``"name.operation"``) — the
        import-cycle hatch; prefer ``from_operation`` otherwise."""
        return cls(_parse_ref(ref, path), rel, title, media_type)
