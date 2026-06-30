"""jero — an opinionated, msgspec-first ASGI micro-framework."""

from jero.background import BackgroundTasks
from jero.codecs import msgspec_decoder, msgspec_encoder
from jero.core import (
    Auth,
    BaseApp,
    BaseFactory,
    BasicAuth,
    BearerAuth,
    BytesResponse,
    Endpoint,
    EndpointMeta,
    HTTPError,
    JSONResponse,
    OperationMeta,
    Resource,
    ResourceMeta,
    WiringError,
)
from jero.forms import FilePart, FormPart
from jero.headers import RawHeaders
from jero.links import Link, Location
from jero.openapi import ModelMeta, ResponseSpec, SecurityScheme, Tag
from jero.streaming import NDJSONStreamingResponse, ServerSentEvent, SSEResponse, StreamingResponse
from jero.structs import Struct
from jero.testing import FactoryHarness, TestClient, TestResponse, TestSSEEvent

__all__ = [
    "Auth",
    "BackgroundTasks",
    "BaseApp",
    "BaseFactory",
    "BasicAuth",
    "BearerAuth",
    "BytesResponse",
    "Endpoint",
    "EndpointMeta",
    "FactoryHarness",
    "FilePart",
    "FormPart",
    "HTTPError",
    "JSONResponse",
    "Link",
    "Location",
    "ModelMeta",
    "NDJSONStreamingResponse",
    "OperationMeta",
    "RawHeaders",
    "Resource",
    "ResourceMeta",
    "ResponseSpec",
    "SSEResponse",
    "SecurityScheme",
    "ServerSentEvent",
    "StreamingResponse",
    "Struct",
    "Tag",
    "TestClient",
    "TestResponse",
    "TestSSEEvent",
    "WiringError",
    "msgspec_decoder",
    "msgspec_encoder",
]
