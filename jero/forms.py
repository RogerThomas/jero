"""Typed multipart form request parts."""

from msgspec import Struct


class NoHeaders(Struct):
    """Default empty form part headers."""


class FormPart[T, H: Struct = NoHeaders](Struct):
    """One multipart form part with envelope metadata."""

    data: T
    content_type: str | None
    headers: H


class FilePart[H: Struct = NoHeaders](FormPart[bytes, H]):
    """A file upload part with a required filename."""

    filename: str
