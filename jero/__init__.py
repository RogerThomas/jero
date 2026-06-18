"""jero — an opinionated, msgspec-first ASGI micro-framework."""

from jero.background import BackgroundTasks
from jero.codecs import msgspec_decoder, msgspec_encoder
from jero.core import (
    Auth,
    BaseApp,
    BaseFactory,
    BytesResponse,
    Endpoint,
    HTTPError,
    JSONResponse,
    Resource,
    WiringError,
)
from jero.forms import FilePart, FormPart, NoHeaders
from jero.headers import RawHeaders
from jero.streaming import NDJSONStreamingResponse, ServerSentEvent, SSEResponse, StreamingResponse
from jero.testing import FactoryHarness, TestClient, TestResponse, TestSSEEvent

__all__ = [
    "Auth",
    "BackgroundTasks",
    "BaseApp",
    "BaseFactory",
    "BytesResponse",
    "Endpoint",
    "FactoryHarness",
    "FilePart",
    "FormPart",
    "HTTPError",
    "JSONResponse",
    "NDJSONStreamingResponse",
    "NoHeaders",
    "RawHeaders",
    "Resource",
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
