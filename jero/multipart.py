"""Typed shim over the untyped ``multipart`` library.

The ``multipart`` package ships no type stubs, so every access reads as ``Unknown``
to pyright. This module is the single place that casts its surface to precise types;
the rest of jero imports these typed names and never touches ``multipart`` directly.
"""

from collections.abc import Callable, Iterable
from io import BytesIO  # noqa: TC003  # runtime-evaluated annotation (no future import)
from typing import Protocol, cast

import multipart as _multipart  # pyright: ignore[reportMissingTypeStubs]


class MultipartPart(Protocol):
    """One parsed form part: its name, filename, headers, and raw bytes."""

    name: str | None
    filename: str | None
    headerlist: list[tuple[str, str]]
    raw: bytes


class _MultipartParserFactory(Protocol):
    """The ``MultipartParser`` constructor: a body stream and boundary in, parts out."""

    def __call__(
        self, stream: BytesIO, boundary: str, *, strict: bool
    ) -> Iterable[MultipartPart]: ...


type OptionsParser = Callable[[str], tuple[str, dict[str, str]]]

parse_options_header = cast(
    "OptionsParser",
    _multipart.parse_options_header,  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
)
MultipartParser = cast("_MultipartParserFactory", _multipart.MultipartParser)
MultipartError = cast(
    "type[Exception]",
    _multipart.MultipartError,  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
)
