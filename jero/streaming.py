"""Typed streaming response wrappers."""

from collections.abc import AsyncGenerator, AsyncIterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from msgspec import Struct

from jero.codecs import msgspec_encoder
from jero.headers import (
    RawHeaders,  # noqa: TC001  # runtime-evaluated annotation (no future import)
)
from jero.links import (
    Link,  # noqa: TC001  # runtime-evaluated annotation (no future import)
    Location,  # noqa: TC001  # runtime-evaluated annotation (no future import)
)

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
class _StreamingResponse[T, H: Struct | None = None]:
    """Shared base for the streaming response kinds. ``stream`` is an item source
    (a plain async iterable, or a one-yield lifecycle generator for setup/teardown);
    ``status_code`` overrides the verb's default status when set.

    Headers work as on the other response classes: ``headers`` is a typed Struct
    (the header *type* is the parameter ``H``, defaulting to None), ``raw_headers``
    the escape hatch for exotic names, casing, or repeats; both are emitted, typed
    first."""

    stream: Source[T]
    headers: H | None = None
    raw_headers: RawHeaders | Mapping[str, str] | None = None
    status_code: int | None = None
    location: Location | None = None
    links: Sequence[Link] = ()


@dataclass(kw_only=True, slots=True)
class StreamingResponse[H: Struct | None = None](_StreamingResponse[bytes, H]):
    """A response streamed as raw ``bytes`` chunks (``application/octet-stream`` by
    default; override via ``raw_headers``)."""


@dataclass(kw_only=True, slots=True)
class NDJSONStreamingResponse[T: Struct, H: Struct | None = None](_StreamingResponse[T, H]):
    """A response streamed as newline-delimited JSON — one ``T`` Struct per line
    (``application/x-ndjson``)."""


@dataclass(kw_only=True, slots=True)
class SSEResponse[T: Struct | str = str, H: Struct | None = None](
    _StreamingResponse[T | ServerSentEvent[T], H]
):
    """A Server-Sent Events response (``text/event-stream``, GET-only). Yield a
    Struct/str (sent as ``data``) or a :class:`ServerSentEvent`. ``keepalive``, if
    set, emits a comment ping every N idle seconds."""

    keepalive: float | None = None


def _sse_data_lines(data: Struct | str) -> list[str]:
    if isinstance(data, str):
        return data.splitlines() or [""]
    return msgspec_encoder.encode(data).decode().splitlines()


# Un-underscored: the SSE wire-format encoder lives with ServerSentEvent but is
# called by core's stream sender — it deliberately crosses the module boundary.
def encode_sse(item: Struct | str | ServerSentEvent[Any]) -> bytes:
    """Encode one item as an SSE ``text/event-stream`` frame (event/id/retry/data)."""
    event = item if isinstance(item, ServerSentEvent) else ServerSentEvent(data=item)
    lines: list[str] = []
    if event.event is not None:
        lines.append(f"event: {event.event}")
    if event.id is not None:
        lines.append(f"id: {event.id}")
    if event.retry is not None:
        lines.append(f"retry: {event.retry}")
    lines += [f"data: {line}" for line in _sse_data_lines(event.data)]
    return ("\n".join(lines) + "\n\n").encode()
