"""Problem Details errors and structurally registered custom exception handlers."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from types import new_class
from typing import Annotated, Literal, cast

import pytest
from msgspec import Meta, Struct
from msgspec.json import encode

from jero import (
    BaseApp,
    BaseHTTPError,
    ConflictError,
    DataclassHTTPError,
    Endpoint,
    ErrorBodyAdapter,
    ExceptionResponse,
    ForbiddenError,
    GoneError,
    HTTPError,
    ParameterizedHTTPError,
    StructHTTPError,
    TooManyRequestsError,
    ValidationFailedError,
)
from jero.core import ExceptionHandler
from jero.testing import TestClient


class ErrorParams(Struct):
    """Select the error behavior exercised by the endpoint."""

    mode: str


class Result(Struct):
    """Successful endpoint response."""

    ok: bool


class MissingThingParams(Struct, rename="camel"):
    """Structured context used to render the missing-thing detail."""

    thing_id: str


@dataclass
class MissingThingError(
    DataclassHTTPError[MissingThingParams],
    type="thing-not-found",
    title="Thing not found",
    status=404,
    docs="https://api.example.com/problems/thing-not-found",
    detail_template="Thing {thing_id} not found",
):
    """Parameterized application problem used through the HTTP boundary."""

    thing_id: str

    def __post_init__(self) -> None:
        self._set_params(MissingThingParams(thing_id=self.thing_id))


class ServiceError(Exception):
    """Ordinary application exception translated by a custom handler."""

    def __init__(self, *, retryable: bool, expose: bool = True) -> None:
        super().__init__("service failed")
        self.retryable = retryable
        self.expose = expose


class SpecificServiceError(ServiceError):
    """A more specific failure used to verify nearest-MRO dispatch."""


class BrokenHandlerError(Exception):
    """An exception whose custom handler itself fails."""


class ServiceErrorBody(Struct, rename="camel"):
    """Custom non-Problem JSON response body."""

    code: str
    retryable: bool


class ServiceErrorHeaders(Struct):
    """Typed headers for the custom exception response."""

    retry_after: int | None = None


class ServiceErrorHandler:
    """Translate visible service failures and continue hidden ones."""

    def handle_exception(
        self,
        exception: ServiceError,
    ) -> ExceptionResponse[ServiceErrorBody, ServiceErrorHeaders] | None:
        """Return an occurrence-specific response, or continue default handling."""
        if not exception.expose:
            return None
        return ExceptionResponse(
            status_code=503 if exception.retryable else 502,
            json=ServiceErrorBody(code="service-failed", retryable=exception.retryable),
            headers=ServiceErrorHeaders(retry_after=30 if exception.retryable else None),
        )


class SpecificServiceErrorHandler:
    """Override the base handler for the nearest exception type in the MRO."""

    async def handle_exception(
        self,
        exception: SpecificServiceError,
    ) -> ExceptionResponse[ServiceErrorBody]:
        """Return the response specific to ``SpecificServiceError``."""
        _ = exception
        return ExceptionResponse(
            status_code=409,
            json=ServiceErrorBody(code="specific-service-failed", retryable=False),
        )


class BrokenHandler:
    """A malformed runtime implementation despite its valid static signature."""

    def handle_exception(
        self,
        exception: BrokenHandlerError,
    ) -> ExceptionResponse[ServiceErrorBody]:
        """Raise while trying to translate the original exception."""
        _ = exception
        raise RuntimeError("handler failed")


class BadReturnError(Exception):
    """An exception whose handler returns a value outside its declared contract."""


class BadReturnHandler:
    """A handler that breaches its (statically valid) return contract at runtime."""

    def handle_exception(self, exception: BadReturnError) -> ExceptionResponse[ServiceErrorBody]:
        """Return a value outside the declared type — a runtime-only contract breach."""
        _ = exception
        return cast("ExceptionResponse[ServiceErrorBody]", "not-a-response")


class TeapotError(HTTPError, type="teapot", title="I'm a teapot", status=418):
    """A deliberately raised typed error whose custom handler then crashes."""


class TeapotHandler:
    """A handler for a deliberately-raised HTTPError that itself fails."""

    def handle_exception(self, exception: TeapotError) -> ExceptionResponse[ServiceErrorBody]:
        """Raise while translating an HTTPError (which must not itself be logged)."""
        _ = exception
        raise RuntimeError("teapot handler failed")


class NamedResponse(ExceptionResponse[ServiceErrorBody, ServiceErrorHeaders]):
    """A named ``ExceptionResponse`` subclass used as a handler's return annotation."""


class NamedResponseError(Exception):
    """Triggers the handler that returns a named ``ExceptionResponse`` subclass."""


class NamedResponseHandler:
    """A handler whose return annotation is a named subclass rather than the generic alias."""

    def handle_exception(self, exception: NamedResponseError) -> NamedResponse:
        """Return the named subclass instance."""
        _ = exception
        return NamedResponse(
            status_code=502,
            json=ServiceErrorBody(code="named", retryable=False),
            headers=ServiceErrorHeaders(retry_after=5),
        )


class ErrorsEndpoint(Endpoint, path="/errors"):
    """Raise each error shape selected by the query parameter."""

    async def get(self, params: ErrorParams) -> Result:
        """Raise the selected error, or return success for an unknown mode."""
        if params.mode == "problem":
            raise MissingThingError(thing_id="thing-id")
        if params.mode == "retryable":
            raise ServiceError(retryable=True)
        if params.mode == "fatal":
            raise ServiceError(retryable=False)
        if params.mode == "hidden":
            raise ServiceError(retryable=False, expose=False)
        if params.mode == "specific":
            raise SpecificServiceError(retryable=False)
        if params.mode == "handler-fails":
            raise BrokenHandlerError()
        if params.mode == "bad-return":
            raise BadReturnError()
        if params.mode == "teapot-crash":
            raise TeapotError()
        if params.mode == "named":
            raise NamedResponseError()
        return Result(ok=True)


class ErrorsApp(BaseApp):
    """Wire the endpoint with base and subclass exception handlers."""

    async def wire(self) -> None:
        """Register handlers before exposing the endpoint."""
        self._include_exception_handler(ServiceErrorHandler())
        self._include_exception_handler(SpecificServiceErrorHandler())
        self._include_exception_handler(BrokenHandler())
        self._include_exception_handler(BadReturnHandler())
        self._include_exception_handler(TeapotHandler())
        self._include_exception_handler(NamedResponseHandler())
        self._include_endpoint(ErrorsEndpoint())


class DuplicateHandlerApp(BaseApp):
    """Invalid app registering the same exact exception type twice."""

    async def wire(self) -> None:
        """Trigger duplicate-registration validation during startup."""
        self._include_exception_handler(ServiceErrorHandler())
        self._include_exception_handler(ServiceErrorHandler())


class StockErrorsEndpoint(Endpoint, path="/stock-errors"):
    """Raise a ready-made stock error selected by the query parameter."""

    async def get(self, params: ErrorParams) -> Result:
        """Raise the selected stock error, or return success for an unknown mode."""
        if params.mode == "forbidden":
            raise ForbiddenError()
        if params.mode == "conflict":
            raise ConflictError()
        if params.mode == "gone":
            raise GoneError()
        if params.mode == "too-many-requests":
            raise TooManyRequestsError()
        return Result(ok=True)


class StockErrorsApp(BaseApp):
    """Wire only the stock-errors endpoint; no custom handlers involved."""

    async def wire(self) -> None:
        """Expose the endpoint raising ready-made errors."""
        self._include_endpoint(StockErrorsEndpoint())


@pytest.mark.parametrize(
    ("mode", "status_code", "title"),
    [
        (
            "forbidden",
            403,
            "The caller is authenticated but not allowed to perform this operation",
        ),
        ("conflict", 409, "The request conflicts with the current state of the resource"),
        ("gone", 410, "The resource existed but has been permanently removed"),
        ("too-many-requests", 429, "The caller has exceeded a rate limit"),
    ],
)
def test_stock_errors_are_ready_to_raise(mode: str, status_code: int, title: str) -> None:
    """The exported stock errors produce their typed problem bodies when raised."""
    with TestClient(StockErrorsApp()) as client:
        resp = client.get("/stock-errors", params={"mode": mode})

    assert resp.status_code == status_code
    assert resp.json() == {"type": mode, "title": title, "status": status_code}


def test_parameterized_problem_details() -> None:
    """Runtime detail values are also exposed through typed params."""
    with TestClient(ErrorsApp()) as client:
        resp = client.get("/errors", params={"mode": "problem"})

    assert resp.status_code == 404
    assert resp.json() == {
        "type": "thing-not-found",
        "title": "Thing not found",
        "status": 404,
        "docs": "https://api.example.com/problems/thing-not-found",
        "detail": "Thing thing-id not found",
        "params": {"thingId": "thing-id"},
    }


@pytest.mark.parametrize(
    ("mode", "status_code", "retry_after"),
    [("retryable", 503, "30"), ("fatal", 502, None)],
)
def test_exception_handler_can_choose_status_and_typed_headers(
    mode: str,
    status_code: int,
    retry_after: str | None,
) -> None:
    """A handler chooses status and typed headers from the exception instance."""
    with TestClient(ErrorsApp()) as client:
        resp = client.get("/errors", params={"mode": mode})

    assert resp.status_code == status_code
    assert resp.json() == {
        "code": "service-failed",
        "retryable": mode == "retryable",
    }
    assert resp.headers.get("retry-after") == retry_after


def test_none_continues_to_internal_server_problem() -> None:
    """Returning None preserves default handling for an ordinary exception."""
    with TestClient(ErrorsApp()) as client:
        resp = client.get("/errors", params={"mode": "hidden"})

    assert resp.status_code == 500
    assert resp.json() == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }


def test_repeated_dispatch_reuses_resolved_handler() -> None:
    """Dispatching the same exception type twice reuses the memoized handler resolution."""
    with TestClient(ErrorsApp()) as client:
        first = client.get("/errors", params={"mode": "retryable"})
        second = client.get("/errors", params={"mode": "retryable"})

    assert first.status_code == second.status_code == 503
    assert first.json() == second.json() == {"code": "service-failed", "retryable": True}


def test_most_specific_exception_handler_wins() -> None:
    """The nearest registered exception type wins regardless of registration order."""
    with TestClient(ErrorsApp()) as client:
        resp = client.get("/errors", params={"mode": "specific"})

    assert resp.status_code == 409
    assert resp.json() == {"code": "specific-service-failed", "retryable": False}


def test_exception_handler_failure_is_internal_server_problem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure inside a custom handler does not recurse or escape the app, and logs both
    the handler's own crash and the original exception it was meant to translate."""
    with (
        caplog.at_level(logging.ERROR, logger="jero"),
        TestClient(ErrorsApp()) as client,
    ):
        resp = client.get("/errors", params={"mode": "handler-fails"})

    assert resp.status_code == 500
    assert resp.json() == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }
    # The handler's own crash (naming the handler) and the original exception are both logged.
    assert "exception handler BrokenHandler raised handling GET /errors" in caplog.text
    assert "handler failed" in caplog.text
    assert "unhandled error handling GET /errors" in caplog.text
    assert any(record.exc_info for record in caplog.records)


def test_handler_returning_invalid_value_is_internal_server_problem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A handler that returns outside its declared contract 500s, naming the handler and
    the bad type, and still surfaces the original exception it was meant to translate."""
    with (
        caplog.at_level(logging.ERROR, logger="jero"),
        TestClient(ErrorsApp()) as client,
    ):
        resp = client.get("/errors", params={"mode": "bad-return"})

    assert resp.status_code == 500
    assert resp.json() == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }
    assert "exception handler BadReturnHandler returned an invalid str handling GET /errors" in (
        caplog.text
    )
    assert "unhandled error handling GET /errors" in caplog.text


def test_httperror_original_is_not_logged_when_its_handler_crashes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When a deliberately-raised HTTPError's handler crashes, the handler failure is logged
    but the HTTPError (expected control flow) is not."""
    with (
        caplog.at_level(logging.ERROR, logger="jero"),
        TestClient(ErrorsApp()) as client,
    ):
        resp = client.get("/errors", params={"mode": "teapot-crash"})

    assert resp.status_code == 500
    assert "exception handler TeapotHandler raised handling GET /errors" in caplog.text
    assert "teapot handler failed" in caplog.text
    # The original is an HTTPError — expected control flow — so it is never logged.
    assert "unhandled error handling GET /errors" not in caplog.text


def test_duplicate_exception_handler_is_wiring_error() -> None:
    """Two handlers for the same exact exception type fail during startup."""
    with pytest.raises(
        RuntimeError,
        match="exception handler for ServiceError is already registered",
    ):
        TestClient(DuplicateHandlerApp())


def test_handler_returning_named_exception_response_subclass() -> None:
    """A handler may return a named ``ExceptionResponse`` subclass, not just the alias."""
    with TestClient(ErrorsApp()) as client:
        resp = client.get("/errors", params={"mode": "named"})

    assert resp.status_code == 502
    assert resp.json() == {"code": "named", "retryable": False}
    assert resp.headers.get("retry-after") == "5"


# --- The public HTTPError subclass contract (validated at class definition) ---


def _define_httperror(**options: object) -> str:
    """Define an HTTPError subclass dynamically; its option validation may raise. Returns the
    created class name (a plain value, so the definition is never a bare exception statement)."""
    return type("Bad", (HTTPError,), {}, **options).__name__


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"title": "t", "status": 400}, "missing required class option 'type'"),
        ({"type": "x", "status": 400}, "missing required class option 'title'"),
        ({"type": "x", "title": "t"}, "missing required class option 'status'"),
        (
            {"type": "x", "title": "t", "status": 400, "extra": 1},
            "unexpected HTTPError class option",
        ),
        ({"type": 1, "title": "t", "status": 400}, "type must be a non-blank string"),
        ({"type": "", "title": "t", "status": 400}, "type must be a non-blank string"),
        ({"type": " \n ", "title": "t", "status": 400}, "type must be a non-blank string"),
        ({"type": "x", "title": "", "status": 400}, "title must be a non-empty"),
        ({"type": "x", "title": "t", "status": 200}, "from 400 through 599"),
        ({"type": "x", "title": "t", "status": True}, "from 400 through 599"),
        ({"type": "x", "title": "t", "status": 400, "docs": 1}, "docs must be a string"),
    ],
)
def test_httperror_subclass_validation(options: dict[str, object], match: str) -> None:
    """Each malformed set of class options fails at subclass definition."""
    with pytest.raises(TypeError, match=match):
        _define_httperror(**options)


@pytest.mark.parametrize("error_type", ["PascalCase", "https://example.com/errors/thing"])
def test_httperror_type_format_is_unconstrained(error_type: str) -> None:
    """``type`` accepts any non-empty string — jero's kebab-case is a convention, not a rule."""
    assert _define_httperror(type=error_type, title="t", status=400) == "Bad"


def _instantiate(factory: Callable[..., object], *args: object) -> str:
    """Instantiate via ``factory`` and return its repr; construction may raise. Returns a plain
    value so the call is never a bare exception statement inside a ``pytest.raises`` block."""
    return repr(factory(*args))


def test_bare_httperror_cannot_be_instantiated() -> None:
    """The un-subclassed HTTPError base is not a usable error."""
    with pytest.raises(TypeError, match="must be subclassed"):
        _instantiate(HTTPError)


class Coords(Struct):
    """Params Struct for parameterized-error tests."""

    x: int


def _define_parameterized(orig_base: object, **options: object) -> str:
    """Define a ``ParameterizedHTTPError`` subclass dynamically; its validation may raise.

    Built via ``type()`` rather than a ``class`` statement so a definition that fails
    validation leaves no unused local class for the linters to flag. ``__orig_bases__``
    carries the generic argument that ``_resolve_params_type`` reads, exactly as a real
    ``class`` statement would set it. Returns the created class name (a plain value).
    """
    ns = {"__orig_bases__": (orig_base,)}
    return type("Generated", (ParameterizedHTTPError,), ns, **options).__name__


def test_parameterized_requires_detail_template() -> None:
    """A parameterized error without a ``detail_template`` fails at definition."""
    with pytest.raises(TypeError, match="requires detail_template"):
        _define_parameterized(ParameterizedHTTPError[Coords], type="x", title="t", status=400)


def test_parameterized_requires_concrete_params() -> None:
    """A parameterized error without a concrete params Struct fails at definition."""
    with pytest.raises(TypeError, match="requires a concrete params Struct"):
        _define_parameterized(
            ParameterizedHTTPError, type="x", title="t", status=400, detail_template="{x}"
        )


def test_detail_template_must_reference_a_field() -> None:
    """A ``detail_template`` with no field placeholders fails at definition."""
    with pytest.raises(TypeError, match="must reference at least one params field"):
        _define_parameterized(
            ParameterizedHTTPError[Coords], type="x", title="t", status=400, detail_template="none"
        )


def test_detail_template_rejects_unknown_field() -> None:
    """A ``detail_template`` referencing a non-field fails at definition."""
    with pytest.raises(TypeError, match="unknown params field"):
        _define_parameterized(
            ParameterizedHTTPError[Coords],
            type="x",
            title="t",
            status=400,
            detail_template="{missing}",
        )


class RenderError(
    ParameterizedHTTPError[Coords],
    type="render",
    title="Render",
    status=400,
    detail_template="x is {x}",
):
    """A plain (non-dataclass) parameterized error, instantiated directly in tests."""


def test_parameterized_error_renders_detail_and_problem() -> None:
    """Instantiating a parameterized error renders its detail and typed problem body."""
    error = RenderError(Coords(x=1))
    assert error.detail == "x is 1"
    problem = error.problem
    assert problem.detail == "x is 1"
    assert problem.params.x == 1
    assert problem.status == 400


def test_parameterized_params_type_mismatch() -> None:
    """Passing the wrong params Struct type fails at construction."""
    with pytest.raises(TypeError, match="params must be Coords"):
        _instantiate(RenderError, cast("Coords", Result(ok=True)))


@pytest.mark.parametrize("status_code", [200, 600, True])
def test_exception_response_status_must_be_client_or_server_error(status_code: int) -> None:
    """``ExceptionResponse`` rejects a status outside 400-599 (booleans included)."""
    with pytest.raises(ValueError, match="from 400 through 599"):
        ExceptionResponse(status_code=status_code, json=Result(ok=True))


class _BareApp(BaseApp):
    """Minimal app registering one exception handler, for validation tests."""

    def __init__(self, handler: object) -> None:
        self._handler = handler
        super().__init__()

    async def wire(self) -> None:
        """No routes; the validation under test runs at ``_include_exception_handler``."""
        self._include_exception_handler(cast(ExceptionHandler[Exception], self._handler))


class NoMethodHandler:
    """Not a valid handler — it has no ``handle_exception`` method."""


class WrongParamNameHandler:
    """Its ``handle_exception`` argument is not named ``exception``."""

    def handle_exception(self, exc: ServiceError) -> ExceptionResponse[ServiceErrorBody]:
        """Return a valid response (the wrong parameter name is the defect)."""
        _ = exc
        return ExceptionResponse(status_code=500, json=ServiceErrorBody(code="x", retryable=False))


class NonExceptionAnnotationHandler:
    """Its ``exception`` argument is annotated with a non-Exception type."""

    def handle_exception(self, exception: str) -> ExceptionResponse[ServiceErrorBody]:
        """Return a valid response (the annotation is the defect)."""
        _ = exception
        return ExceptionResponse(status_code=500, json=ServiceErrorBody(code="x", retryable=False))


class BadReturnAnnotationHandler:
    """Its return annotation is neither an ``HTTPError`` nor an ``ExceptionResponse``."""

    def handle_exception(self, exception: ServiceError) -> int:
        """Return an int, outside the allowed handler return contract."""
        _ = exception
        return 0


@pytest.mark.parametrize(
    ("handler", "match"),
    [
        (NoMethodHandler(), "must define a 'handle_exception' method"),
        (WrongParamNameHandler(), "exactly one argument named 'exception'"),
        (NonExceptionAnnotationHandler(), "must be annotated with"),
        (BadReturnAnnotationHandler(), "must return"),
    ],
)
def test_exception_handler_wiring_validation(handler: object, match: str) -> None:
    """A structurally invalid handler is rejected when it is registered."""
    with pytest.raises(RuntimeError, match=match):
        TestClient(_BareApp(handler))


# --- StructHTTPError (the bring-your-own-body engine) ---


class HouseBody(Struct, rename="camel"):
    """A house-style error body without a status field."""

    error_code: str
    error_message: str


class StatusHouseBody(Struct, rename="camel"):
    """A house-style error body carrying the status."""

    error_code: str
    error_message: str
    status_code: int


@dataclass
class TooBigError(
    StructHTTPError[StatusHouseBody],
    status=413,
    description="Too big",
    consts={"error_code": "too-big"},
    templates={"error_message": "Document is {size} bytes"},
    status_field="status_code",
):
    """The typed tier: the declared dataclass fields are the params."""

    size: int


class PlainHouseError(StructHTTPError[HouseBody], status=422, description="Plain"):
    """The kwargs tier: no declarations, every body field is a raise-time param."""


class NestedExtensions(Struct, rename="camel"):
    """Params nested into the body's extensions field."""

    thing: str


class CompanyBody(Struct, rename="camel"):
    """A company shape: const code, rendered description, nested extensions."""

    error_code: str
    error_description: str
    extensions: NestedExtensions


@dataclass
class ThingFailedError(
    StructHTTPError[CompanyBody],
    status=422,
    description="Thing failed",
    consts={"error_code": "thing-failed"},
    templates={"error_description": "This {thing} has failed"},
    params_field="extensions",
):
    """Rendered description; the raw params also nest into extensions."""

    thing: str


@dataclass
class DriftedError(
    StructHTTPError[HouseBody],
    status=422,
    description="Drifted",
    consts={"error_code": "drifted"},
):
    """Its dataclass fields drifted from its params (error_message is required)."""

    wrong_name: str


class StructRaisingEndpoint(Endpoint, path="/struct-error"):
    """Raises one of the Struct-family errors, selected by query param."""

    async def get(self, params: ErrorParams) -> Result:
        """Raise the selected Struct-family error."""
        if params.mode == "with-status":
            raise TooBigError(size=51)
        if params.mode == "company":
            raise ThingFailedError(thing="my-thing")
        raise PlainHouseError(error_code="error-code", error_message="error-message")


class StructErrorsApp(BaseApp):
    """App raising Struct-family errors."""

    async def wire(self) -> None:
        """Expose the raising endpoint."""
        self._include_endpoint(StructRaisingEndpoint())


def _define_struct_httperror(**options: object) -> str:
    """Define a StructHTTPError subclass dynamically (over StatusHouseBody); its option
    validation may raise. ``types.new_class`` rather than ``type()``: the parameterized
    base is a generic alias, which only the former resolves through ``__mro_entries__``."""
    return new_class("Bad", (StructHTTPError[StatusHouseBody],), dict(options)).__name__


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"description": "d"}, "missing required class option 'status'"),
        ({"status": 413}, "missing required class option 'description'"),
        ({"status": 413, "description": ""}, "description must be a non-blank string"),
        ({"status": 200, "description": "d"}, "from 400 through 599"),
        ({"status": True, "description": "d"}, "from 400 through 599"),
        ({"status": 413, "description": "d", "extra": 1}, "unexpected StructHTTPError"),
        (
            {"status": 413, "description": "d", "consts": {"nope": "x"}},
            "not a field of StatusHouseBody",
        ),
        (
            {
                "status": 413,
                "description": "d",
                "consts": {"error_code": "x"},
                "templates": {"error_code": "{y}"},
            },
            "fed by both consts and templates",
        ),
        (
            {"status": 413, "description": "d", "templates": {"status_code": "{y}"}},
            "carry text",
        ),
        (
            {"status": 413, "description": "d", "templates": {"error_message": "fixed"}},
            "references no placeholders",
        ),
        (
            {"status": 413, "description": "d", "status_field": "error_code"},
            "must be an int field",
        ),
        (
            {"status": 413, "description": "d", "params_field": "error_code"},
            "must be a Struct-typed field",
        ),
        (
            {"status": 413, "description": "d", "templates": {"error_message": "x {0}"}},
            "must be named params",
        ),
        (
            {"status": 413, "description": "d", "templates": {"error_message": "x {}"}},
            "must be named params",
        ),
        (
            {"status": 413, "description": "d", "consts": {"error_code": 123}},
            "does not match the field's declared type",
        ),
        (
            {"status": 413, "description": "d", "consts": {"error_code": True}},
            "must be a str or int",
        ),
        ({"status": 413, "description": "d", "consts": "nope"}, "consts must be a dict"),
        (
            {"status": 413, "description": "d", "consts": {1: "x"}},
            "consts keys must be field-name strings",
        ),
        ({"status": 413, "description": "d", "templates": "nope"}, "templates must be a dict"),
        (
            {"status": 413, "description": "d", "templates": {"error_message": 1}},
            "map field-name strings to format strings",
        ),
        (
            {"status": 413, "description": "d", "status_field": 1},
            "status_field must be a field-name string",
        ),
        (
            {"status": 413, "description": "d", "params_field": 1},
            "params_field must be a field-name string",
        ),
    ],
)
def test_struct_httperror_subclass_validation(options: dict[str, object], match: str) -> None:
    """Each malformed set of class options fails at class definition."""
    with pytest.raises(TypeError, match=match):
        _define_struct_httperror(**options)


class LiteralCodeBody(Struct, rename="camel"):
    """A body whose code field is a Literal — const values must be among its members."""

    error_code: Literal["a", "b"]
    error_message: str


def test_struct_httperror_const_must_match_literal_field() -> None:
    """A const value outside a Literal field's members fails at class definition."""
    with pytest.raises(TypeError, match="not among the field's literal values"):
        new_class(
            "Bad",
            (StructHTTPError[LiteralCodeBody],),
            {"status": 413, "description": "d", "consts": {"error_code": "c"}},
        )


class CollidingExtensions(Struct):
    """Nested params whose field name collides with a body field."""

    error_message: str


class CollidingBody(Struct, rename="camel"):
    """A body whose params_field nests a Struct sharing a field name with the body."""

    error_message: str
    extensions: CollidingExtensions


def test_struct_httperror_params_field_collision_fails() -> None:
    """A nested params-Struct field colliding with a same-named body field is loud."""
    with pytest.raises(TypeError, match="collide with same-named body fields"):
        new_class(
            "Bad",
            (StructHTTPError[CollidingBody],),
            {"status": 413, "description": "d", "params_field": "extensions"},
        )


def test_struct_httperror_template_placeholder_collision_fails() -> None:
    """A template placeholder naming another declared source is loud."""
    with pytest.raises(TypeError, match="collide with declared body-field sources"):
        new_class(
            "Bad",
            (StructHTTPError[StatusHouseBody],),
            {
                "status": 413,
                "description": "d",
                "consts": {"error_code": "x"},
                "templates": {"error_message": "{error_code}"},
            },
        )


def test_struct_httperror_abstract_intermediate_is_allowed() -> None:
    """An `_abstract=True` intermediate defers body resolution to concrete subclasses."""
    intermediate = new_class("Intermediate", (StructHTTPError,), {"_abstract": True})
    assert not hasattr(intermediate, "body_type")


def test_struct_httperror_unsubclassed_init_fails() -> None:
    """Instantiating a StructHTTPError that was never given a body fails clearly."""
    with pytest.raises(TypeError, match="must be subclassed with a body"):
        _instantiate(cast("Callable[[], object]", StructHTTPError))


def test_struct_httperror_requires_a_body_struct() -> None:
    """Subclassing without a concrete body Struct fails at class definition."""
    with pytest.raises(TypeError, match="requires a concrete body Struct"):
        new_class("Bad", (StructHTTPError,), {"status": 413, "description": "d"})


def test_struct_httperror_composes_consts_templates_and_status() -> None:
    """The typed tier renders templates and pins consts and the status in the body."""
    with TestClient(StructErrorsApp()) as client:
        resp = client.get("/struct-error", params={"mode": "with-status"})
    assert resp.status_code == 413
    assert resp.json() == {
        "errorCode": "too-big",
        "errorMessage": "Document is 51 bytes",
        "statusCode": 413,
    }


def test_struct_httperror_kwargs_tier_wire_body() -> None:
    """With no declarations, every body field is a same-named raise-time param."""
    with TestClient(StructErrorsApp()) as client:
        resp = client.get("/struct-error", params={"mode": "plain"})
    assert resp.status_code == 422
    assert resp.json() == {"errorCode": "error-code", "errorMessage": "error-message"}


def test_struct_httperror_renders_description_and_nests_params() -> None:
    """The description renders from the params, which also nest into extensions."""
    with TestClient(StructErrorsApp()) as client:
        resp = client.get("/struct-error", params={"mode": "company"})
    assert resp.status_code == 422
    assert resp.json() == {
        "errorCode": "thing-failed",
        "errorDescription": "This my-thing has failed",
        "extensions": {"thing": "my-thing"},
    }


def test_struct_httperror_rejects_bad_raise_time_params() -> None:
    """The kwargs tier validates the flat param namespace at raise time."""
    with pytest.raises(TypeError, match="missing: error_message"):
        _instantiate(lambda: PlainHouseError(error_code="error-code"))
    with pytest.raises(TypeError, match="unexpected: nope"):
        _instantiate(lambda: PlainHouseError(error_code="c", error_message="m", nope="x"))


def test_struct_httperror_dataclass_fields_must_match_params() -> None:
    """A typed-tier subclass whose fields drifted from its params fails on first raise."""
    with pytest.raises(TypeError, match="dataclass fields must match"):
        _instantiate(DriftedError, "x")


def test_struct_httperror_body_is_the_typed_struct() -> None:
    """``.body`` composes the occurrence as the declared body type, fully populated."""
    error = TooBigError(size=51)
    assert error.body == StatusHouseBody(
        error_code="too-big", error_message="Document is 51 bytes", status_code=413
    )


def test_base_httperror_catches_both_families() -> None:
    """BaseHTTPError means "any jero error"; HTTPError only the Problem family."""
    struct_error = PlainHouseError(error_code="c", error_message="m")
    assert isinstance(struct_error, BaseHTTPError)
    assert not isinstance(struct_error, HTTPError)
    assert isinstance(GoneError(), BaseHTTPError)


# --- ErrorBodyAdapter (app-wide house rendering of the Problem family) ---


class HouseAdapter(ErrorBodyAdapter[HouseBody]):
    """Renders any Problem-family error in the house shape, status in body."""

    status_field = "status_code"

    def compose(self, error: HTTPError) -> HouseBody:
        return HouseBody(error_code=error.type, error_message=str(error))


class CrashingAdapter(ErrorBodyAdapter[HouseBody]):
    """An adapter whose compose always fails, to exercise the containment path."""

    def compose(self, error: HTTPError) -> HouseBody:
        raise RuntimeError("adapter boom")


class UpstreamUnavailableError(
    HTTPError,
    type="upstream-unavailable",
    title="Upstream unavailable",
    status=502,
):
    """The Problem-family error a domain handler translates into."""


class DomainBoomError(Exception):
    """A domain exception translated by a handler into an HTTPError."""


class DomainErrorHandler:
    """Translate the domain exception into the Problem family (then adapter-rendered)."""

    def handle_exception(self, exception: DomainBoomError) -> UpstreamUnavailableError:
        """Return the translated error."""
        _ = exception
        return UpstreamUnavailableError()


class AdapterProbeEndpoint(Endpoint, path="/errors"):
    """Raise a Problem-family error, a translated domain error, or an unexpected one."""

    async def get(self, params: ErrorParams) -> Result:
        """Raise the selected error, or return success for an unknown mode."""
        if params.mode == "conflict":
            raise ConflictError()
        if params.mode == "translated":
            raise DomainBoomError()
        if params.mode == "unexpected":
            raise RuntimeError("unexpected boom")
        return Result(ok=True)


class AdaptedApp(BaseApp):
    """App with an adapter, raising endpoints, and a domain-exception handler."""

    def __init__(self, adapter: ErrorBodyAdapter[HouseBody]) -> None:
        self._adapter = adapter
        super().__init__()

    async def wire(self) -> None:
        """Register the adapter, both raising endpoints, and the handler."""
        self._include_error_adapter(self._adapter)
        self._include_endpoint(AdapterProbeEndpoint())
        self._include_endpoint(StructRaisingEndpoint())
        self._include_exception_handler(DomainErrorHandler())


def test_adapter_renders_framework_errors_house_shaped() -> None:
    """Route misses and method misses render through the adapter."""
    with TestClient(AdaptedApp(HouseAdapter())) as client:
        missing = client.get("/nope")
        assert missing.status_code == 404
        assert missing.json() == {
            "errorCode": "not-found",
            "errorMessage": "Not found",
            "statusCode": 404,
        }
        wrong_method = client.post("/struct-error")
        assert wrong_method.status_code == 405
        assert wrong_method.json()["errorCode"] == "method-not-allowed"


def test_adapter_renders_raised_and_translated_errors(caplog: pytest.LogCaptureFixture) -> None:
    """Raised HTTPErrors, handler-translated errors, and the unexpected-500 fallback all
    render house-shaped; StructHTTPErrors keep rendering themselves."""
    with TestClient(AdaptedApp(HouseAdapter())) as client:
        raised = client.get("/errors", params={"mode": "conflict"})
        assert raised.status_code == 409
        assert raised.json()["errorCode"] == "conflict"

        translated = client.get("/errors", params={"mode": "translated"})
        assert translated.status_code == 502
        assert translated.json()["errorCode"] == "upstream-unavailable"

        with caplog.at_level(logging.ERROR):
            crashed = client.get("/errors", params={"mode": "unexpected"})
        assert crashed.status_code == 500
        assert crashed.json()["errorCode"] == "internal-server-error"

        own = client.get("/struct-error", params={"mode": "with-status"})
        assert own.json() == {
            "errorCode": "too-big",
            "errorMessage": "Document is 51 bytes",
            "statusCode": 413,
        }


def test_adapter_crash_falls_back_to_problem(caplog: pytest.LogCaptureFixture) -> None:
    """An adapter failure is contained: logged, with the Problem body sent instead."""
    with TestClient(AdaptedApp(CrashingAdapter())) as client, caplog.at_level(logging.ERROR):
        resp = client.get("/nope")
    assert resp.status_code == 404
    assert resp.json() == {"type": "not-found", "title": "Not found", "status": 404}
    assert any("CrashingAdapter" in record.message for record in caplog.records)


def _unbound_compose(self: "ErrorBodyAdapter[HouseBody]", error: HTTPError) -> HouseBody:
    """compose for the dynamically-built unbound adapter."""
    _ = self
    return HouseBody(error_code=error.type, error_message=str(error))


class UnboundAdapterApp(BaseApp):
    """Invalid app registering an adapter that never bound its body Struct."""

    async def wire(self) -> None:
        """Build an adapter without [B] parameterization and register it."""
        adapter_cls = new_class(
            "UnboundAdapter",
            (ErrorBodyAdapter,),
            exec_body=lambda ns: ns.update(compose=_unbound_compose),
        )
        self._include_error_adapter(cast("ErrorBodyAdapter[HouseBody]", adapter_cls()))


def test_adapter_subclass_validation() -> None:
    """A bound adapter validates at class definition; a generic intermediate (B still
    unbound) is allowed and binds through its concrete subclasses."""
    with pytest.raises(TypeError, match="collides with a field on HouseBody"):
        new_class(
            "Bad",
            (ErrorBodyAdapter[HouseBody],),
            exec_body=lambda ns: ns.update(status_field="error_code"),
        )
    intermediate = new_class("Intermediate", (ErrorBodyAdapter,))
    assert not hasattr(intermediate, "body_type")


def test_unbound_adapter_is_rejected_at_registration() -> None:
    """Registering an adapter that never bound a body Struct is a startup failure."""
    with pytest.raises(RuntimeError, match="never bound a concrete body Struct"):
        TestClient(UnboundAdapterApp())


class TwoAdaptersApp(BaseApp):
    """Invalid app registering a second adapter."""

    async def wire(self) -> None:
        """Register the adapter twice to trigger the duplicate check."""
        self._include_error_adapter(HouseAdapter())
        self._include_error_adapter(HouseAdapter())


class NotAnAdapterApp(BaseApp):
    """Invalid app registering something that isn't an adapter."""

    async def wire(self) -> None:
        """Register a non-adapter to trigger the type check."""
        self._include_error_adapter(cast("ErrorBodyAdapter[HouseBody]", object()))


def test_include_error_adapter_rejects_duplicates_and_non_adapters() -> None:
    """Registering twice, or registering a non-adapter, is a startup failure."""
    with pytest.raises(RuntimeError, match="already registered"):
        TestClient(TwoAdaptersApp())
    with pytest.raises(RuntimeError, match="requires an ErrorBodyAdapter instance"):
        TestClient(NotAnAdapterApp())


class ReservedBody(Struct, rename="camel"):
    """A body whose leftover field name collides with the engine's attributes."""

    error_message: str
    status: int


def test_struct_httperror_rejects_reserved_param_names() -> None:
    """A leftover body field named like an engine attribute cannot become a param —
    in the dataclass tier it would shadow the class contract (e.g. the status line)."""
    with pytest.raises(TypeError, match="reserved by the error engine"):
        new_class("Bad", (StructHTTPError[ReservedBody],), {"status": 400, "description": "d"})


def test_direct_base_httperror_subclass_is_rejected() -> None:
    """BaseHTTPError cannot be subclassed directly; pick a family."""
    with pytest.raises(TypeError, match="subclasses BaseHTTPError directly"):
        new_class("Weird", (BaseHTTPError,), {"status": 418})


class AnnotatedBody(Struct, rename="camel"):
    """A body using Annotated fields (the msgspec.Meta idiom)."""

    error_message: Annotated[str, Meta(description="human text")]
    status_code: Annotated[int, Meta(ge=400)]


@dataclass
class AnnotatedError(
    StructHTTPError[AnnotatedBody],
    status=418,
    description="Annotated",
    templates={"error_message": "hello {name}"},
    status_field="status_code",
):
    """Annotated template/status fields validate through the Meta wrapper."""

    name: str


def test_struct_httperror_supports_annotated_fields() -> None:
    """Annotated[str/int, Meta] fields validate at class creation and render."""
    error = AnnotatedError(name="name")
    assert encode(error.response_body) == (b'{"errorMessage":"hello name","statusCode":418}')


@dataclass(frozen=True)
class FrozenHouseError(
    StructHTTPError[HouseBody],
    status=422,
    description="Frozen",
    consts={"error_code": "frozen"},
    templates={"error_message": "{why}"},
):
    """A frozen dataclass error: binding must not hit FrozenInstanceError."""

    why: str


def test_frozen_dataclass_error_binds() -> None:
    """A frozen @dataclass subclass raises and renders normally."""
    error = FrozenHouseError(why="why")
    assert error.status == 422
    assert encode(error.response_body) == b'{"errorCode":"frozen","errorMessage":"why"}'


class TaggedBody(Struct, tag=True, rename="camel"):
    """A tagged body: the composed wire model must keep the tag."""

    error_message: str


class TaggedHouseError(StructHTTPError[TaggedBody], status=422, description="Tagged"):
    """Kwargs-tier error over a tagged body."""


def test_tagged_body_keeps_its_tag_on_the_wire() -> None:
    """The wire model carries the body's tag, matching B's own encoding."""
    error = TaggedHouseError(error_message="m")
    assert encode(error.response_body) == encode(error.body)
    assert b'"type":"TaggedBody"' in encode(error.response_body)


class KwargsTemplatedError(
    StructHTTPError[HouseBody],
    status=422,
    description="Kwargs templated",
    consts={"error_code": "kwargs-templated"},
    templates={"error_message": "hello {name}"},
):
    """Kwargs tier with a rendered template (templates aren't dataclass-tier-only)."""


def test_struct_httperror_kwargs_tier_renders_templates() -> None:
    """The kwargs tier renders templates from the flat param namespace."""
    error = KwargsTemplatedError(name="name")
    assert encode(error.response_body) == (
        b'{"errorCode":"kwargs-templated","errorMessage":"hello name"}'
    )


class KwOnlyBody(Struct, rename="camel", kw_only=True):
    """A kw_only body: the adapter must compose by field name, not positionally."""

    error_code: str
    error_message: str


class KwOnlyAdapter(ErrorBodyAdapter[KwOnlyBody]):
    """Adapter over a kw_only body."""

    status_field = "status_code"

    def compose(self, error: HTTPError) -> KwOnlyBody:
        return KwOnlyBody(error_code=error.type, error_message=str(error))


class KwOnlyAdaptedApp(BaseApp):
    """App with the kw_only adapter registered."""

    async def wire(self) -> None:
        """Register the adapter and a probe endpoint."""
        self._include_error_adapter(KwOnlyAdapter())
        self._include_endpoint(AdapterProbeEndpoint())


def test_adapter_supports_kw_only_bodies() -> None:
    """A kw_only body renders through the adapter — never a silent Problem fallback."""
    with TestClient(KwOnlyAdaptedApp()) as client:
        resp = client.get("/nope")
    assert resp.json() == {
        "errorCode": "not-found",
        "errorMessage": "Not found",
        "statusCode": 404,
    }


class StructReturningHandler:
    """Translate the domain exception into a Struct-family error."""

    def handle_exception(self, exception: DomainBoomError) -> PlainHouseError:
        """Return the translated Struct-family error."""
        _ = exception
        return PlainHouseError(error_code="translated", error_message="boom")


class StructHandlerApp(BaseApp):
    """App whose handler returns a StructHTTPError."""

    async def wire(self) -> None:
        """Register the handler and the probe endpoint."""
        self._include_exception_handler(StructReturningHandler())
        self._include_endpoint(AdapterProbeEndpoint())


def test_handler_may_return_a_struct_family_error() -> None:
    """A custom handler's StructHTTPError return renders itself."""
    with TestClient(StructHandlerApp()) as client:
        resp = client.get("/errors", params={"mode": "translated"})
    assert resp.status_code == 422
    assert resp.json() == {"errorCode": "translated", "errorMessage": "boom"}


class NoStatusAdapter(ErrorBodyAdapter[HouseBody]):
    """An adapter without status_field: the body is rendered and documented as-is."""

    def compose(self, error: HTTPError) -> HouseBody:
        return HouseBody(error_code=error.type, error_message=str(error))


class NoStatusPathParams(Struct):
    """Path params so the probe endpoint derives a 404 error response."""

    thing_id: str


class NoStatusProbeEndpoint(Endpoint, path="/things/{thing_id}"):
    """A path-sourced endpoint so the OpenAPI doc carries a derived 404."""

    async def get(self, path: NoStatusPathParams) -> Result:
        """Echo the id."""
        _ = path
        return Result(ok=True)


class NoStatusAdaptedApp(BaseApp):
    """App with the no-status adapter registered and OpenAPI served."""

    async def wire(self) -> None:
        """Register the adapter, a probe endpoint, and the spec."""
        self._include_error_adapter(NoStatusAdapter())
        self._include_endpoint(AdapterProbeEndpoint())
        self._include_endpoint(NoStatusProbeEndpoint())
        self._include_openapi(title="no-status", version="1")


def test_adapter_without_status_field_renders_body_as_is() -> None:
    """An adapter with no status_field emits its body Struct at runtime, and the derived
    OpenAPI error responses document that plain body (no per-status wire model)."""
    with TestClient(NoStatusAdaptedApp()) as client:
        resp = client.get("/nope")
        assert resp.json() == {"errorCode": "not-found", "errorMessage": "Not found"}
        schema = client.get("/openapi.json").json()["paths"]["/things/{thing_id}"]["get"][
            "responses"
        ]["404"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/HouseBody"}


def test_adapter_status_field_must_be_a_string() -> None:
    """A non-string status_field on an adapter fails at class definition."""
    with pytest.raises(TypeError, match="status_field must be a non-blank string"):
        new_class(
            "Bad",
            (ErrorBodyAdapter[HouseBody],),
            exec_body=lambda ns: ns.update(status_field=1, compose=NoStatusAdapter.compose),
        )


class WrongTypeAdapter(ErrorBodyAdapter[HouseBody]):
    """An adapter whose compose returns the wrong Struct type."""

    status_field = "status_code"

    def compose(self, error: HTTPError) -> HouseBody:
        _ = error
        return cast("HouseBody", Result(ok=True))


class WrongTypeAdaptedApp(BaseApp):
    """App with the wrong-type adapter registered."""

    async def wire(self) -> None:
        """Register the adapter and a probe endpoint."""
        self._include_error_adapter(WrongTypeAdapter())
        self._include_endpoint(AdapterProbeEndpoint())


def test_adapter_wrong_compose_type_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    """compose returning the wrong Struct is contained: logged, Problem body sent."""
    with TestClient(WrongTypeAdaptedApp()) as client, caplog.at_level(logging.ERROR):
        resp = client.get("/nope")
    assert resp.json() == {"type": "not-found", "title": "Not found", "status": 404}


@dataclass
class LiteralConstError(
    StructHTTPError[LiteralCodeBody],
    status=422,
    description="Literal const",
    consts={"error_code": "a"},  # a valid member of the Literal field
):
    """A const value that IS among the field's Literal members is accepted."""

    error_message: str


def test_struct_httperror_valid_literal_const() -> None:
    """A const matching one of a Literal field's members renders and validates."""
    error = LiteralConstError(error_message="m")
    assert encode(error.response_body) == b'{"errorCode":"a","errorMessage":"m"}'


def test_adapter_reuses_its_per_status_wire_model() -> None:
    """Two errors of the same status render through one cached wire model."""
    with TestClient(AdaptedApp(HouseAdapter())) as client:
        first = client.get("/nope")  # 404, builds the model
        second = client.get("/also-missing")  # 404, hits the cache
    assert first.json()["statusCode"] == 404
    assert second.json()["statusCode"] == 404


# --------------------------------------------------------------------------
# A msgspec validation failure surfaces its detail through both house channels:
# an app-wide adapter, and a per-error exception handler.
# --------------------------------------------------------------------------
class Widget(Struct, rename="camel"):
    """Body Struct that forces a msgspec validation error on a bad field type."""

    name: str
    price_cents: int


class WidgetEndpoint(Endpoint, path="/widgets"):
    """Decode a Widget body so a bad field type raises ``ValidationFailedError``."""

    async def post(self, json: Widget) -> Result:
        """Echo success; the interesting path is the 422 on a bad body."""
        _ = json
        return Result(ok=True)


class HouseErrorBody(Struct, rename="camel"):
    """A house error format whose message field is named ``info``, not ``detail``."""

    error_code: str
    info: str


class InfoHouseAdapter(ErrorBodyAdapter[HouseErrorBody]):
    """Re-skin the whole Problem family into the house shape, app-wide."""

    status_field = "status_code"

    def compose(self, error: HTTPError) -> HouseErrorBody:
        """Map any Problem-family error; ``str(error)`` is the rendered detail."""
        return HouseErrorBody(error_code=error.type, info=str(error))


class HouseAdapterApp(BaseApp):
    """Wire the endpoint behind an app-wide error body adapter."""

    async def wire(self) -> None:
        """Register the house adapter, then expose the endpoint."""
        self._include_error_adapter(InfoHouseAdapter())
        self._include_endpoint(WidgetEndpoint())


class ValidationBody(Struct, rename="camel"):
    """The custom body a per-error validation handler returns."""

    code: str
    message: str


class ValidationHandler:
    """Handle only ``ValidationFailedError``, leaving every other error untouched."""

    def handle_exception(
        self, exception: ValidationFailedError
    ) -> ExceptionResponse[ValidationBody]:
        """Build a bespoke body from the error's typed params."""
        return ExceptionResponse(
            status_code=exception.status,
            json=ValidationBody(code=exception.type, message=exception.params.reason),
        )


class ValidationHandlerApp(BaseApp):
    """Wire the endpoint behind a validation-specific exception handler."""

    async def wire(self) -> None:
        """Register the per-error handler, then expose the endpoint."""
        self._include_exception_handler(ValidationHandler())
        self._include_endpoint(WidgetEndpoint())


_BAD_WIDGET = {"name": "gizmo", "priceCents": "not-an-int"}
_MSGSPEC_DETAIL = "Expected `int`, got `str` - at `$.priceCents`"


def test_msgspec_detail_reaches_house_adapter() -> None:
    """A msgspec validation failure renders into the house body's ``info`` field."""
    with TestClient(HouseAdapterApp()) as client:
        resp = client.post("/widgets", json=_BAD_WIDGET)

    assert resp.status_code == 422
    assert resp.json() == {
        "errorCode": "validation-failed",
        "info": _MSGSPEC_DETAIL,
        "statusCode": 422,
    }


def test_msgspec_detail_reaches_per_error_handler() -> None:
    """A ``ValidationFailedError`` handler reads the msgspec message from typed params."""
    with TestClient(ValidationHandlerApp()) as client:
        resp = client.post("/widgets", json=_BAD_WIDGET)

    assert resp.status_code == 422
    assert resp.json() == {"code": "validation-failed", "message": _MSGSPEC_DETAIL}
