"""Typed multipart form request parts."""

from msgspec import Struct

from jero.headers import RawHeaders

_RawHeaders = RawHeaders


class NoHeaders(Struct):
    """Empty typed form part headers."""


class FormPart[T, H: Struct | None = None](Struct):
    """One multipart form part with envelope metadata."""

    data: T
    content_type: str | None
    headers: H
    raw_headers: _RawHeaders


class FilePart[H: Struct | None = None](FormPart[bytes, H]):
    """A file upload part with a required filename."""

    filename: str
