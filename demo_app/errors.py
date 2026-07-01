"""Application error contracts and the custom upstream exception handler."""

from dataclasses import dataclass

from msgspec import Struct

from jero import DataclassHTTPError, HTTPError


class InvalidTokenError(HTTPError, type="invalid-token", title="Invalid token", status=401):
    """The supplied bearer token does not identify a user."""


class WidgetNotFoundParams(Struct, rename="camel"):
    """Occurrence-specific context for the stable widget-not-found problem."""

    widget_id: str


@dataclass
class WidgetNotFoundError(
    DataclassHTTPError[WidgetNotFoundParams],
    type="widget-not-found",
    title="Widget not found",
    status=404,
    detail_template="Widget {widget_id} not found",
):
    """A requested widget id does not exist upstream."""

    widget_id: str

    def __post_init__(self) -> None:
        self._set_params(WidgetNotFoundParams(widget_id=self.widget_id))


@dataclass
class UpstreamResponseError(Exception):
    """The upstream service returned an unusable response."""

    retryable: bool


class UpstreamFailureParams(Struct, rename="camel"):
    """Structured context for an unavailable upstream service."""

    retry_after_seconds: int


class EmptyUpstreamResponseError(
    HTTPError,
    type="empty-upstream-response",
    title="Empty upstream response",
    status=502,
):
    """The upstream returned no usable response body."""


@dataclass
class UpstreamUnavailableError(
    DataclassHTTPError[UpstreamFailureParams],
    type="upstream-unavailable",
    title="Upstream unavailable",
    status=503,
    detail_template=(
        "An upstream service is overloaded; please try again after {retry_after_seconds} seconds"
    ),
):
    """The upstream is unavailable for a known retry interval."""

    retry_after_seconds: int

    def __post_init__(self) -> None:
        self._set_params(UpstreamFailureParams(retry_after_seconds=self.retry_after_seconds))


@dataclass
class UpstreamResponseErrorHandler:
    """Translate one upstream exception into either its 502 or 503 response."""

    _retry_after_seconds: int

    async def handle_exception(
        self, exception: UpstreamResponseError
    ) -> EmptyUpstreamResponseError | UpstreamUnavailableError:
        """Select the concrete error response from this exception occurrence."""
        if not exception.retryable:
            return EmptyUpstreamResponseError()

        return UpstreamUnavailableError(retry_after_seconds=self._retry_after_seconds)
