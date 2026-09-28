"""Static-file reading for ``_include_assets`` and the ``_include_openapi`` favicon.

Everything here runs once, at wiring (or is a pure per-request header check): reading a
file with its content type resolved by suffix, the precomputed gzip variant and strong
``ETag``, the directory walk, and the ``Accept-Encoding`` / ``If-None-Match`` checks the
asset handler consults. The handler itself lives in :mod:`jero.core` beside the route
tail it depends on; this is the sender-free half, split out to keep ``core`` under its
size budget (mirroring the :mod:`jero._exception_handlers` split).
"""

import os
from collections.abc import Iterable, Sequence
from fnmatch import fnmatch
from gzip import compress as gzip_compress
from pathlib import Path

from jero._wiring_types import WiringError


def _read_typed_file(
    file: Path,
    display: str,
    content_types: dict[str, bytes],
    *,
    label: str,
    unsupported_hint: str = "",
) -> tuple[bytes, bytes]:
    """Read one file's bytes once, at wiring, with its content type resolved by
    suffix. Fails loud on an unsupported suffix or an unreadable file, never at
    request time. Shared by :func:`favicon_payload` and :func:`_asset_payload` —
    only the accepted suffix table and the message's label/hint differ between them."""
    content_type = content_types.get(file.suffix.lower())
    if content_type is None:
        supported = ", ".join(sorted(content_types))
        raise WiringError(
            f"{label} {display} has an unsupported suffix; use one of {supported}"
            f"{unsupported_hint}",
        )
    try:
        body = file.read_bytes()
    except OSError as exc:
        raise WiringError(f"{label} {display} is not readable: {exc}") from exc
    return body, content_type


# Favicon media types by file suffix; anything else is a loud wiring failure.
_FAVICON_CONTENT_TYPES: dict[str, bytes] = {
    ".ico": b"image/x-icon",
    ".png": b"image/png",
    ".svg": b"image/svg+xml",
}


def favicon_payload(favicon: Path) -> tuple[bytes, bytes]:
    """Read the favicon once at wiring: its bytes and content type. Fails loud on an
    unsupported suffix or an unreadable file — never at request time."""
    return _read_typed_file(
        favicon, str(favicon), _FAVICON_CONTENT_TYPES, label="_include_openapi favicon"
    )


# Asset media types by file suffix; anything else is a loud wiring failure (exclude the
# file, or serve it from the proxy/CDN where real static serving belongs). A strict
# superset of _FAVICON_CONTENT_TYPES: every favicon suffix is also a valid asset.
_ASSET_CONTENT_TYPES: dict[str, bytes] = {
    ".avif": b"image/avif",
    ".css": b"text/css; charset=utf-8",
    ".gif": b"image/gif",
    ".html": b"text/html; charset=utf-8",
    ".ico": b"image/x-icon",
    ".jpeg": b"image/jpeg",
    ".jpg": b"image/jpeg",
    ".js": b"text/javascript; charset=utf-8",
    ".json": b"application/json",
    ".map": b"application/json",
    ".mjs": b"text/javascript; charset=utf-8",
    ".png": b"image/png",
    ".svg": b"image/svg+xml",
    ".txt": b"text/plain; charset=utf-8",
    ".wasm": b"application/wasm",
    ".webmanifest": b"application/manifest+json",
    ".webp": b"image/webp",
    ".woff": b"font/woff",
    ".woff2": b"font/woff2",
}


# Suffixes worth gzipping at wiring; the image/font formats are already compressed and
# a gzip pass would only add bytes and a Vary header for nothing.
_COMPRESSIBLE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".css",
        ".html",
        ".ico",
        ".js",
        ".json",
        ".map",
        ".mjs",
        ".svg",
        ".txt",
        ".wasm",
        ".webmanifest",
    }
)


def asset_etag(digest: str, *, gzip: bool = False) -> bytes:
    """A quoted strong ``ETag`` from an already-computed digest — call
    :func:`hashlib.sha256` once per body and format both the plain and gzip forms
    from it, so they can never drift out of shape with each other and the hash
    itself is never paid for twice."""
    suffix = "-gzip" if gzip else ""
    return f'"{digest}{suffix}"'.encode()


def _asset_payload(file: Path, relative: str, *, gzip: bool) -> tuple[bytes, bytes | None, bytes]:
    """One file's ``(bytes, gzip variant or None, content type)``, read once at wiring.
    The gzip variant is deterministic (``mtime=0``) and kept only when meaningfully
    smaller than the original."""
    body, content_type = _read_typed_file(
        file,
        relative,
        _ASSET_CONTENT_TYPES,
        label="_include_assets:",
        unsupported_hint=", or exclude it",
    )
    gz_body = None
    if gzip and file.suffix.lower() in _COMPRESSIBLE_SUFFIXES:
        compressed = gzip_compress(body, 9, mtime=0)
        if len(compressed) < len(body) * 0.9:
            gz_body = compressed
    return body, gz_body, content_type


def _asset_files(directory: Path) -> list[Path]:
    """Every file under ``directory``, sorted for deterministic wiring. Dotfiles and
    dot-directories are never served — pruned *before* descending into them, so a
    ``.git`` checkout or a bundler's ``.cache`` sitting under the tree is never
    walked, not just filtered out afterward. Symlinks are never served either,
    file or directory: ``os.walk``'s default ``followlinks=False`` already keeps a
    symlinked directory from ever being descended into, and a symlinked *file* is
    excluded here — otherwise it would be read straight through, serving whatever it
    points at (anywhere on disk the process can read) as if it were under
    ``directory``."""
    files: list[Path] = []
    for root, dirnames, filenames in os.walk(directory):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for filename in filenames:
            if filename.startswith("."):
                continue
            file = Path(root) / filename
            if not file.is_symlink():
                files.append(file)
    return sorted(files)


def asset_payloads(
    directory: Path,
    include: Sequence[str],
    exclude: Sequence[str],
    *,
    gzip: bool,
    max_total_bytes: int,
    max_files: int,
) -> list[tuple[str, bytes, bytes | None, bytes]]:
    """Read every servable file under ``directory`` once, at wiring: ``(relative posix
    path, bytes, gzipped bytes or None, content type)`` per file. The gzip variant is
    compressed here (deterministically, ``mtime=0``) and kept only when meaningfully
    smaller. Fails loud on a missing directory, an unsupported suffix, an unreadable
    file, zero matches, too many files, or a total (both variants counted) over the
    cap — never at request time. A single file already over the remaining budget is
    rejected by its on-disk size *before* it is read or compressed, so the cap bounds
    the cost of checking it, not just the cost of holding it."""
    if not directory.is_dir():
        raise WiringError(f"_include_assets directory {directory} is not a directory")
    payloads: list[tuple[str, bytes, bytes | None, bytes]] = []
    total = 0
    for file in _asset_files(directory):
        if not file.is_file():
            continue
        relative = file.relative_to(directory).as_posix()
        if not any(fnmatch(relative, pattern) for pattern in include):
            continue
        if any(fnmatch(relative, pattern) for pattern in exclude):
            continue
        if len(payloads) >= max_files:
            raise WiringError(
                f"_include_assets: more than max_files={max_files} files matched under "
                f"{directory} (hit at {relative}). Serve a directory this large from a "
                f"proxy/CDN, or raise the cap deliberately",
            )
        if total + file.stat().st_size > max_total_bytes:
            raise WiringError(
                f"_include_assets: reading {relative} would exceed "
                f"max_total_bytes={max_total_bytes} under {directory}. Assets are held "
                f"in memory per worker — serve large or many files from a proxy/CDN, "
                f"or raise the cap deliberately",
            )
        body, gz_body, content_type = _asset_payload(file, relative, gzip=gzip)
        total += len(body) + (0 if gz_body is None else len(gz_body))
        if total > max_total_bytes:
            raise WiringError(
                f"_include_assets: {total} bytes under {directory} exceeds "
                f"max_total_bytes={max_total_bytes} (hit while reading {relative}). Assets "
                f"are held in memory per worker — serve large or many files from a "
                f"proxy/CDN, or raise the cap deliberately",
            )
        payloads.append((relative, body, gz_body, content_type))
    if not payloads:
        raise WiringError(
            f"_include_assets: no files matched under {directory} "
            f"(include={list(include)}, exclude={list(exclude)})",
        )
    return payloads


def _accept_encoding_weight(directive: bytes) -> tuple[bytes, float]:
    """One ``Accept-Encoding`` directive (e.g. ``b"gzip;q=0.5"``) as ``(name,
    qvalue)`` (RFC 9110 §12.5.3). An unparseable ``q`` is treated as though none were
    given — full acceptance — the same lenient reading as a directive with no ``q``
    at all. A directive repeating ``q`` (undefined by the spec) takes the last one
    present, consistent with how a repeated header *line* is handled by the caller."""
    parts = directive.split(b";")
    name = parts[0].strip().lower()
    weight = 1.0
    for param in parts[1:]:
        key, _, raw_q = param.strip().partition(b"=")
        if key.strip().lower() != b"q":
            continue
        try:
            weight = float(raw_q)
        except ValueError:
            weight = 1.0
    return name, weight


def accepts_gzip(values: Iterable[bytes]) -> bool:
    """Whether the (possibly repeated) ``Accept-Encoding`` header values accept gzip.
    Scans for just the two directives that matter (``gzip``, the wildcard) rather
    than building a table of every encoding named — this runs on every asset
    request. An explicit ``gzip`` directive wins over the wildcard; ``q=0``
    — including ``gzip;q=0`` — is an explicit refusal (RFC 9110). No header at all is
    treated as "don't bother", matching the identity-only behaviour a plain GET has
    always had."""
    gzip_weight: float | None = None
    wildcard_weight: float | None = None
    for value in values:
        for directive in value.split(b","):
            directive = directive.strip()
            if not directive:
                continue
            name, weight = _accept_encoding_weight(directive)
            if name == b"gzip":
                gzip_weight = weight
            elif name == b"*":
                wildcard_weight = weight
    if gzip_weight is not None:
        return gzip_weight > 0
    return (wildcard_weight or 0.0) > 0


def if_none_match_hit(values: Iterable[bytes], etag: bytes) -> bool:
    """Whether any ``If-None-Match`` header value contains a token matching ``etag``
    exactly, or the wildcard. Tokenizes on commas rather than a raw substring check,
    so a value that merely *contains* the tag's bytes without being it — malformed
    input, or one tag embedded inside another — can't produce a false revalidation.
    An optional leading ``W/`` is stripped (RFC 7232's weak comparison, valid for the
    safe GET/HEAD this handler only ever serves)."""
    for value in values:
        for token in value.split(b","):
            token = token.strip()
            if token == b"*":
                return True
            if token.startswith(b"W/"):
                token = token[2:]
            if token == etag:
                return True
    return False
