"""Typed Problem Details errors.

jero intentionally uses a short machine-readable code for ``type`` rather than the
RFC 9457 URI.  Static error metadata lives on the exception class; parameterized
errors additionally carry a typed Struct whose values render the human-only detail.
"""

import re
from abc import ABC, abstractmethod
from string import Formatter
from types import get_original_bases
from typing import ClassVar, get_args, get_origin

from msgspec import Struct
from msgspec.structs import asdict, fields


class Problem(Struct, kw_only=True, omit_defaults=True):
    """The wire representation of a static API error."""

    type: str
    title: str
    status: int
    docs: str | None = None


class ParameterizedProblem[P: Struct](Problem, kw_only=True, omit_defaults=True):
    """The wire representation of an API error with occurrence-specific context."""

    detail: str
    params: P


def _class_option(options: dict[str, object], name: str) -> object:
    try:
        return options.pop(name)
    except KeyError as exc:
        raise TypeError(f"HTTPError subclass is missing required class option {name!r}") from exc


def _resolve_params_type(cls: type) -> type[Struct] | None:
    """Find the concrete params Struct through either parameterized error base."""
    for klass in cls.__mro__:
        for base in get_original_bases(klass):
            origin = get_origin(base)
            if not isinstance(origin, type) or not issubclass(origin, ParameterizedHTTPError):
                continue
            args = get_args(base)
            if args and isinstance(args[0], type) and issubclass(args[0], Struct):
                return args[0]
    return None


class HTTPError(Exception):
    """A static typed API error.

    Subclasses declare their stable contract as class options::

        class AuthenticationRequiredError(
            HTTPError,
            type="authentication-required",
            title="Authentication required",
            status=401,
        ): ...
    """

    type: ClassVar[str]
    title: ClassVar[str]
    status: ClassVar[int]
    docs: ClassVar[str | None]

    def __init_subclass__(cls, **options: object) -> None:
        abstract = options.pop("_abstract", False)
        if abstract is True:
            super().__init_subclass__()
            return

        error_type = _class_option(options, "type")
        title = _class_option(options, "title")
        status = _class_option(options, "status")
        docs = options.pop("docs", None)
        if options:
            names = ", ".join(sorted(options))
            raise TypeError(f"unexpected HTTPError class option(s): {names}")
        if not isinstance(error_type, str):
            raise TypeError("HTTPError type must be a non-empty lowercase kebab-case string")
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", error_type) is None:
            raise TypeError("HTTPError type must be a non-empty lowercase kebab-case string")
        if not isinstance(title, str) or not title:
            raise TypeError("HTTPError title must be a non-empty string")
        if not isinstance(status, int) or isinstance(status, bool) or not 400 <= status <= 599:
            raise TypeError("HTTPError status must be an integer from 400 through 599")
        if docs is not None and not isinstance(docs, str):
            raise TypeError("HTTPError docs must be a string or None")

        super().__init_subclass__()
        cls.type = error_type
        cls.title = title
        cls.status = status
        cls.docs = docs

    def __init__(self) -> None:
        if not hasattr(type(self), "title"):
            raise TypeError("HTTPError must be subclassed with type, title, and status")
        super().__init__(self.title)

    @property
    def problem(self) -> Problem:
        """Build the typed wire body for this error occurrence."""
        return Problem(type=self.type, title=self.title, status=self.status, docs=self.docs)


class ParameterizedHTTPError[P: Struct](HTTPError, _abstract=True):
    """An API error whose detail is rendered from a typed params Struct."""

    detail_template: ClassVar[str]
    params_type: ClassVar[type[Struct]]

    params: P
    detail: str

    def __init_subclass__(
        cls,
        *,
        detail_template: str | None = None,
        **options: object,
    ) -> None:
        abstract = options.get("_abstract") is True
        super().__init_subclass__(**options)
        if abstract:
            return
        if detail_template is None:
            raise TypeError("ParameterizedHTTPError subclass requires detail_template")
        params_type = _resolve_params_type(cls)
        if params_type is None:
            raise TypeError("ParameterizedHTTPError subclass requires a concrete params Struct")

        param_names = {field.name for field in fields(params_type)}
        template_names = {
            name.split(".", 1)[0].split("[", 1)[0]
            for _, name, _, _ in Formatter().parse(detail_template)
            if name is not None
        }
        if not template_names:
            raise TypeError("detail_template must reference at least one params field")
        unknown = template_names - param_names
        if unknown:
            names = ", ".join(sorted(unknown))
            raise TypeError(f"detail_template references unknown params field(s): {names}")

        cls.detail_template = detail_template
        cls.params_type = params_type

    def __init__(self, params: P) -> None:
        super().__init__()
        self._set_params(params)

    def _set_params(self, params: P) -> None:
        if not isinstance(params, self.params_type):
            raise TypeError(
                f"{type(self).__name__} params must be {self.params_type.__name__}, "
                f"got {type(params).__name__}",
            )
        self.params = params
        self.detail = self.detail_template.format(**asdict(params))
        Exception.__init__(self, self.detail)

    @property
    def problem(self) -> ParameterizedProblem[P]:
        """Build the typed wire body for this error occurrence."""
        return ParameterizedProblem(
            type=self.type,
            title=self.title,
            status=self.status,
            docs=self.docs,
            detail=self.detail,
            params=self.params,
        )


class DataclassHTTPError[P: Struct](ParameterizedHTTPError[P], ABC, _abstract=True):
    """The ergonomic parameterized-error base used by dataclass error subclasses."""

    @abstractmethod
    def __post_init__(self) -> None:
        """Build the params Struct by calling ``self._set_params(...)``."""


class NotFoundError(
    HTTPError,
    type="not-found",
    title="Not found",
    status=404,
):
    """No route or resource matches the requested path."""


class MethodNotAllowedError(
    HTTPError,
    type="method-not-allowed",
    title="Method not allowed",
    status=405,
):
    """The path exists but does not support the requested method."""


class MalformedRequestError(
    HTTPError,
    type="malformed-request",
    title="Malformed request",
    status=400,
):
    """The request cannot be parsed or bound."""


class ValidationFailedError(
    HTTPError,
    type="validation-failed",
    title="Validation failed",
    status=422,
):
    """The request is syntactically valid but does not match its typed contract."""


class AuthenticationRequiredError(
    HTTPError,
    type="authentication-required",
    title="Authentication required",
    status=401,
):
    """Authentication credentials are absent or invalid."""


class UnsupportedMediaTypeError(
    HTTPError,
    type="unsupported-media-type",
    title="Unsupported media type",
    status=415,
):
    """The request body does not use the media type required by the operation."""


class InternalServerError(
    HTTPError,
    type="internal-server-error",
    title="Internal server error",
    status=500,
):
    """An unexpected server-side failure whose internals are not exposed."""
