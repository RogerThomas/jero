"""Problem Details errors and structurally registered custom exception handlers."""

from dataclasses import dataclass

import pytest
from msgspec import Struct

from jero import BaseApp, DataclassHTTPError, Endpoint, ExceptionResponse, TestClient


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
        return Result(ok=True)


class ErrorsApp(BaseApp):
    """Wire the endpoint with base and subclass exception handlers."""

    async def _wire(self) -> None:
        """Register handlers before exposing the endpoint."""
        self._add_exception_handler(ServiceErrorHandler())
        self._add_exception_handler(SpecificServiceErrorHandler())
        self._add_exception_handler(BrokenHandler())
        self._include_endpoint(ErrorsEndpoint())


class DuplicateHandlerApp(BaseApp):
    """Invalid app registering the same exact exception type twice."""

    async def _wire(self) -> None:
        """Trigger duplicate-registration validation during startup."""
        self._add_exception_handler(ServiceErrorHandler())
        self._add_exception_handler(ServiceErrorHandler())


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


def test_exception_handler_failure_is_internal_server_problem() -> None:
    """A failure inside a custom handler does not recurse or escape the app."""
    with TestClient(ErrorsApp()) as client:
        resp = client.get("/errors", params={"mode": "handler-fails"})

    assert resp.status_code == 500
    assert resp.json() == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }


def test_duplicate_exception_handler_is_wiring_error() -> None:
    """Two handlers for the same exact exception type fail during startup."""
    with pytest.raises(
        RuntimeError,
        match="exception handler for ServiceError is already registered",
    ):
        TestClient(DuplicateHandlerApp())
