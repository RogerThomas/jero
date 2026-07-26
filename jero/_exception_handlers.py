"""Custom exception-handler contracts, compilation, and validation.

A user registers an object with a ``handle_exception(self, exception: E) -> ...`` method
via ``BaseApp.add_exception_handler``; this module turns that object into a validated,
ready-to-invoke :class:`CompiledExceptionHandler` (the exception type and the response
shape are read from the concrete signature, once, at wiring time) and defines the public
:class:`ExceptionResponse` / :class:`ExceptionHandler` surface. The *dispatch* half — the
registry that resolves the nearest handler and actually sends its response — lives in
:mod:`jero.core` alongside the response senders it depends on; this module is the
sender-free half, split out to keep ``core`` under its size budget (mirroring the
:mod:`jero._openapi_wiring` split).
"""

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import NoneType, UnionType, get_original_bases
from typing import Protocol, Union, cast, get_args, get_origin, get_type_hints

from msgspec import Struct

from jero._wiring_types import WiringError
from jero.errors import BaseHTTPError
from jero.headers import RawHeaders
from jero.links import Link, Location


@dataclass(kw_only=True, slots=True)
class ExceptionResponse[T: Struct, H: Struct | None = None]:
    """A typed JSON response returned by a custom exception handler.

    Unlike a normal response wrapper, ``status_code`` is required: exception handling
    has no operation-derived success status to fall back to.
    """

    status_code: int
    json: T
    headers: H | None = None
    raw_headers: RawHeaders | Mapping[str, str] | None = None
    location: Location | None = None
    links: Sequence[Link] = ()

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not 400 <= self.status_code <= 599:
            raise ValueError("ExceptionResponse status_code must be from 400 through 599")


class ExceptionHandler[
    E: Exception,
](Protocol):
    """Structural contract for an object registered with ``add_exception_handler``."""

    def handle_exception(self, exception: E) -> object:
        """Return a typed replacement response, or ``None`` to continue default handling."""
        raise NotImplementedError()


def _exception_response_args(annotation: object) -> tuple[object, ...]:
    """Resolve generic args from a direct response annotation or named subclass."""
    if get_origin(annotation) is ExceptionResponse:
        return get_args(annotation)
    if isinstance(annotation, type) and issubclass(annotation, ExceptionResponse):
        for klass in annotation.__mro__:
            for base in get_original_bases(klass):
                if get_origin(base) is ExceptionResponse:
                    return get_args(base)
    return ()


def _exception_response_type(
    annotation: object,
) -> tuple[type[Struct], type[Struct] | None] | None:
    """Resolve the body and headers from one concrete exception response type."""
    args = _exception_response_args(annotation)
    if not 1 <= len(args) <= 2:
        return None
    body_type = args[0]
    if not isinstance(body_type, type) or not issubclass(body_type, Struct):
        return None
    if len(args) == 1 or args[1] in (None, NoneType):
        return body_type, None
    headers_type = args[1]
    if not isinstance(headers_type, type) or not issubclass(headers_type, Struct):
        return None
    return body_type, headers_type


def _valid_exception_handler_return(annotation: object) -> bool:
    """Whether every return member is an HTTP error, response, or ``None``."""
    variants = (
        cast("tuple[object, ...]", get_args(annotation))
        if get_origin(annotation) in (Union, UnionType)
        else (annotation,)
    )
    has_response = False
    for variant in variants:
        if variant is NoneType:
            continue
        if isinstance(variant, type) and issubclass(variant, BaseHTTPError):
            has_response = True
            continue
        if _exception_response_type(cast("object", variant)) is None:
            return False
        has_response = True
    return has_response


class CompiledExceptionHandler:
    """A custom exception handler whose signature was validated at wiring time."""

    __slots__ = ("_fn", "_is_async", "exception_type", "owner")

    def __init__(self, handler: object) -> None:
        self.owner = type(handler).__name__
        fn = getattr(handler, "handle_exception", None)
        if not callable(fn):
            raise WiringError(f"{self.owner} must define a 'handle_exception' method")
        params = list(inspect.signature(fn).parameters.values())
        if len(params) != 1 or params[0].name != "exception":
            raise WiringError(
                f"{self.owner}.handle_exception must take exactly one argument named 'exception'",
            )
        hints = get_type_hints(fn)
        exception_type = hints.get("exception")
        if not (isinstance(exception_type, type) and issubclass(exception_type, Exception)):
            raise WiringError(
                f"{self.owner}.handle_exception: 'exception' must be annotated with "
                "a specific Exception subclass",
            )
        if not _valid_exception_handler_return(hints.get("return")):
            raise WiringError(
                f"{self.owner}.handle_exception must return "
                "one or more jero error or ExceptionResponse types, optionally with None",
            )
        self.exception_type: type[Exception] = exception_type
        self._fn = cast(Callable[[Exception], object], fn)
        self._is_async = inspect.iscoroutinefunction(fn)

    async def __call__(self, exception: Exception) -> object:
        result = self._fn(exception)
        if self._is_async:
            return await cast(Awaitable[object], result)
        return result


@dataclass(frozen=True, slots=True)
class _ExceptionHandlerResult:
    """Distinguish an Exception returned as data from one raised by the handler."""

    value: object


async def invoke_exception_handler(
    handler: CompiledExceptionHandler,
    exception: Exception,
) -> _ExceptionHandlerResult:
    """Run a compiled handler, wrapping its return in a result so a returned ``Exception``
    (data) is distinguishable from one the handler raised (caught by the caller's gather)."""
    return _ExceptionHandlerResult(await handler(exception))
