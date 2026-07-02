"""Problem Details errors and structurally registered custom exception handlers."""

import logging
from dataclasses import dataclass
from typing import cast

import pytest
from msgspec import Struct

from jero import BaseApp, DataclassHTTPError, Endpoint, ExceptionResponse, HTTPError, TestClient


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
        return Result(ok=True)


class ErrorsApp(BaseApp):
    """Wire the endpoint with base and subclass exception handlers."""

    async def wire(self) -> None:
        """Register handlers before exposing the endpoint."""
        self.add_exception_handler(ServiceErrorHandler())
        self.add_exception_handler(SpecificServiceErrorHandler())
        self.add_exception_handler(BrokenHandler())
        self.add_exception_handler(BadReturnHandler())
        self.add_exception_handler(TeapotHandler())
        self.include_endpoint(ErrorsEndpoint())


class DuplicateHandlerApp(BaseApp):
    """Invalid app registering the same exact exception type twice."""

    async def wire(self) -> None:
        """Trigger duplicate-registration validation during startup."""
        self.add_exception_handler(ServiceErrorHandler())
        self.add_exception_handler(ServiceErrorHandler())


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
