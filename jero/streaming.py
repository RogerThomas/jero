"""Typed streaming response wrappers."""

from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass

from msgspec import Struct

type Source[T] = AsyncIterable[T] | AsyncGenerator[AsyncIterable[T]]


@dataclass(kw_only=True, slots=True)
class ServerSentEvent[T: Struct | str]:
    """One Server-Sent Event. Yield from an :class:`SSEResponse` stream to control
    the ``event`` / ``id`` / ``retry`` fields; ``data`` is a Struct (encoded as
    JSON) or a raw ``str``."""

    data: T
    event: str | None = None
    id: str | None = None
    retry: int | None = None


@dataclass(kw_only=True, slots=True)
class _StreamingResponse[T]:
    """Shared base for the streaming response kinds. ``stream`` is an item source
    (a plain async iterable, or a one-yield lifecycle generator for setup/teardown);
    ``status`` defaults to the verb's status when None."""

    stream: Source[T]
    headers: dict[str, str] | None = None
    status: int | None = None


@dataclass(kw_only=True, slots=True)
class StreamingResponse(_StreamingResponse[bytes]):
    """A response streamed as raw ``bytes`` chunks (``application/octet-stream`` by
    default; override via ``headers``)."""


@dataclass(kw_only=True, slots=True)
class NDJSONStreamingResponse[T: Struct](_StreamingResponse[T]):
    """A response streamed as newline-delimited JSON — one ``T`` Struct per line
    (``application/x-ndjson``)."""


@dataclass(kw_only=True, slots=True)
class SSEResponse[T: Struct | str = str](_StreamingResponse[T | ServerSentEvent[T]]):
    """A Server-Sent Events response (``text/event-stream``, GET-only). Yield a
    Struct/str (sent as ``data``) or a :class:`ServerSentEvent`. ``keepalive``, if
    set, emits a comment ping every N idle seconds."""

    keepalive: float | None = None
