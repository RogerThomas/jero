"""jero — an opinionated, msgspec-first ASGI micro-framework."""

from jero.background import BackgroundTasks
from jero.codecs import msgspec_decoder, msgspec_encoder
from jero.core import (
    Auth,
    BaseApp,
    BaseEndpoint,
    BaseFactory,
    BaseResource,
    BytesResponse,
    EndpointMeta,
    HTTPError,
    JSONResponse,
    OperationMeta,
    ResourceMeta,
    WiringError,
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
    "BaseEndpoint",
    "BaseFactory",
    "BaseResource",
    "BytesResponse",
    "EndpointMeta",
    "FactoryHarness",
    "FilePart",
    "FormPart",
    "HTTPError",
    "JSONResponse",
    "Link",
    "Location",
    "NDJSONStreamingResponse",
    "OperationMeta",
    "RawHeaders",
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
