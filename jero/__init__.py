"""jero — an opinionated, msgspec-first ASGI micro-framework."""

from jero.background import BackgroundTasks
from jero.codecs import msgspec_decoder, msgspec_encoder
from jero.core import (
    Auth,
    BaseApp,
    BaseFactory,
    BytesResponse,
    Endpoint,
    EndpointMeta,
    ExceptionResponse,
    JSONResponse,
    OperationMeta,
    Resource,
    ResourceMeta,
    WiringError,
)
from jero.errors import (
    DataclassHTTPError,
    HTTPError,
    ParameterizedHTTPError,
    Problem,
)
from jero.forms import FilePart, FormPart
from jero.headers import RawHeaders
from jero.links import Link, Location
from jero.streaming import NDJSONStreamingResponse, ServerSentEvent, SSEResponse, StreamingResponse
from jero.testing import FactoryHarness, TestClient, TestResponse, TestSSEEvent

__all__ = [
    "Auth",
    "BackgroundTasks",
    "BaseApp",
    "BaseFactory",
    "BytesResponse",
    "DataclassHTTPError",
    "Endpoint",
    "EndpointMeta",
    "ExceptionResponse",
    "FactoryHarness",
    "FilePart",
    "FormPart",
    "HTTPError",
    "JSONResponse",
    "Link",
    "Location",
    "NDJSONStreamingResponse",
    "OperationMeta",
    "ParameterizedHTTPError",
    "Problem",
    "RawHeaders",
    "Resource",
    "ResourceMeta",
    "SSEResponse",
    "ServerSentEvent",
    "StreamingResponse",
    "TestClient",
    "TestResponse",
    "TestSSEEvent",
    "WiringError",
    "msgspec_decoder",
    "msgspec_encoder",
]
