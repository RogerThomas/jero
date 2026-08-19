"""Wire-time in-memory static assets: ``_include_assets``."""

import asyncio
import os
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import asynccontextmanager
from gzip import decompress
from pathlib import Path
from typing import Any

import pytest
from msgspec import Struct

from jero import BaseApp, Endpoint, Request, Resource, WiringError
from jero.testing import TestClient

SVG_BODY = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
CSS_BODY = b"body { margin: 0 }\n" * 50
PNG_BODY = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
TXT_BODY = b"note-body"


class AssetsApp(BaseApp):
    """App wiring one assets directory; the include's knobs pass straight through."""

    def __init__(
        self,
        directory: Path,
        *,
        path: str = "/assets",
        include: Sequence[str] = ("*",),
        exclude: Sequence[str] = (),
        gzip: bool = True,
        cache_control: str | None = None,
        max_total_bytes: int = 10 * 1024 * 1024,
        max_files: int = 10_000,
    ) -> None:
        super().__init__()
        self._directory = directory
        self._path = path
        self._include = include
        self._exclude = exclude
        self._gzip = gzip
        self._cache_control = cache_control
        self._max_total_bytes = max_total_bytes
        self._max_files = max_files

    async def wire(self) -> None:
        self._include_assets(
            self._directory,
            path=self._path,
            include=self._include,
            exclude=self._exclude,
            gzip=self._gzip,
            cache_control=self._cache_control,
            max_total_bytes=self._max_total_bytes,
            max_files=self._max_files,
        )


@pytest.fixture(name="asset_dir")
def _asset_dir(tmp_path: Path) -> Path:
    (tmp_path / "sub").mkdir()
    (tmp_path / "logo.svg").write_bytes(SVG_BODY)
    (tmp_path / "app.css").write_bytes(CSS_BODY)
    (tmp_path / "pixel.png").write_bytes(PNG_BODY)
    (tmp_path / "sub" / "notes.txt").write_bytes(TXT_BODY)
    (tmp_path / ".hidden.css").write_bytes(b"hidden-body")
    return tmp_path


@pytest.fixture(name="client")
def _client(asset_dir: Path) -> Generator[TestClient]:
    with TestClient(AssetsApp(asset_dir)) as client:
        yield client


def test_asset_is_served_with_baked_headers(client: TestClient) -> None:
    """A file becomes an exact route with its content type, length, and a strong ETag."""
    resp = client.get("/assets/logo.svg")
    assert resp.status_code == 200
    assert resp.content == SVG_BODY
    assert resp.headers["content-type"] == "image/svg+xml"
    assert resp.headers["etag"].startswith('"')


def test_unrelated_request_header_is_ignored(client: TestClient) -> None:
    """A header that is neither Accept-Encoding nor If-None-Match is simply not one
    the handler cares about — the request still serves the asset normally."""
    resp = client.get("/assets/logo.svg", headers={"x-custom": "value"})
    assert resp.status_code == 200
    assert resp.content == SVG_BODY


def test_nested_asset_keeps_its_relative_path(client: TestClient) -> None:
    """Subdirectories map to path segments under the mount."""
    resp = client.get("/assets/sub/notes.txt")
    assert resp.status_code == 200
    assert resp.content == TXT_BODY
    assert resp.headers["content-type"] == "text/plain; charset=utf-8"


def test_unknown_asset_is_404(client: TestClient) -> None:
    """No catch-all: a path that matched no file at wiring is an ordinary 404."""
    assert client.get("/assets/missing.css").status_code == 404


def test_dotfile_is_never_served(client: TestClient) -> None:
    """Dotfiles are skipped at wiring, so their route does not exist."""
    assert client.get("/assets/.hidden.css").status_code == 404


def test_special_file_is_silently_skipped(tmp_path: Path) -> None:
    """A non-regular dirent (a FIFO, here) that isn't a symlink is walked like any
    other file but fails Path.is_file() — silently excluded, same as a dotfile,
    rather than raising or being read through."""
    os.mkfifo(tmp_path / "pipe.txt")
    (tmp_path / "real.txt").write_bytes(TXT_BODY)
    with TestClient(AssetsApp(tmp_path)) as client:
        assert client.get("/assets/pipe.txt").status_code == 404
        assert client.get("/assets/real.txt").content == TXT_BODY


def test_matching_if_none_match_is_304(client: TestClient) -> None:
    """The wiring-time ETag round-trips: a matching If-None-Match gets 304, no body."""
    etag = client.get("/assets/logo.svg").headers["etag"]
    resp = client.get("/assets/logo.svg", headers={"if-none-match": etag})
    assert resp.status_code == 304
    assert resp.content == b""
    assert resp.headers["etag"] == etag


def test_wildcard_if_none_match_is_304(client: TestClient) -> None:
    """A bare '*' in If-None-Match always revalidates, regardless of the ETag."""
    resp = client.get("/assets/logo.svg", headers={"if-none-match": "*"})
    assert resp.status_code == 304
    assert resp.content == b""


def test_weak_if_none_match_matches_the_strong_etag(client: TestClient) -> None:
    """A weak validator (W/"...") revalidates against jero's strong ETag — RFC 7232
    weak comparison, valid here since this handler only ever serves GET/HEAD."""
    etag = client.get("/assets/logo.svg").headers["etag"]
    resp = client.get("/assets/logo.svg", headers={"if-none-match": f"W/{etag}"})
    assert resp.status_code == 304


def test_gzip_variant_served_on_accept_encoding(client: TestClient) -> None:
    """A compressible asset gets a wiring-time gzip variant with its own ETag."""
    plain = client.get("/assets/app.css")
    gz = client.get("/assets/app.css", headers={"accept-encoding": "gzip"})
    assert gz.headers["content-encoding"] == "gzip"
    assert gz.headers["vary"] == "accept-encoding"
    assert plain.headers["vary"] == "accept-encoding"
    assert gz.headers["etag"] != plain.headers["etag"]
    assert decompress(gz.content) == CSS_BODY


def test_gzip_304_uses_the_variant_etag(client: TestClient) -> None:
    """Conditional requests revalidate against the encoding actually served."""
    gz_etag = client.get("/assets/app.css", headers={"accept-encoding": "gzip"}).headers["etag"]
    resp = client.get(
        "/assets/app.css", headers={"accept-encoding": "gzip", "if-none-match": gz_etag}
    )
    assert resp.status_code == 304
    assert resp.content == b""


def test_already_compressed_asset_gets_no_gzip_variant(client: TestClient) -> None:
    """Image formats are served as-is even when the client accepts gzip."""
    resp = client.get("/assets/pixel.png", headers={"accept-encoding": "gzip"})
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert "vary" not in resp.headers
    assert resp.content == PNG_BODY


def test_head_suppresses_body_and_keeps_headers(client: TestClient) -> None:
    """HEAD is served from the GET route with the body dropped."""
    resp = client.head("/assets/logo.svg")
    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["content-length"] == str(len(SVG_BODY))


def test_cache_control_is_emitted_on_200_and_304(asset_dir: Path) -> None:
    """A cache_control value rides on both the full and the 304 response."""
    with TestClient(AssetsApp(asset_dir, cache_control="public, max-age=60")) as client:
        resp = client.get("/assets/logo.svg")
        assert resp.headers["cache-control"] == "public, max-age=60"
        etag = resp.headers["etag"]
        resp = client.get("/assets/logo.svg", headers={"if-none-match": etag})
        assert resp.status_code == 304
        assert resp.headers["cache-control"] == "public, max-age=60"


def test_custom_mount_path(asset_dir: Path) -> None:
    """The mount prefix is configurable."""
    with TestClient(AssetsApp(asset_dir, path="/static")) as client:
        assert client.get("/static/logo.svg").status_code == 200
        assert client.get("/assets/logo.svg").status_code == 404


def test_exclude_glob_removes_files(asset_dir: Path) -> None:
    """Excluded files never become routes."""
    with TestClient(AssetsApp(asset_dir, exclude=("*.css",))) as client:
        assert client.get("/assets/app.css").status_code == 404
        assert client.get("/assets/logo.svg").status_code == 200


def test_include_glob_limits_files(asset_dir: Path) -> None:
    """Only included files become routes."""
    with TestClient(AssetsApp(asset_dir, include=("*.svg",))) as client:
        assert client.get("/assets/logo.svg").status_code == 200
        assert client.get("/assets/sub/notes.txt").status_code == 404


def test_gzip_off_serves_plain_only(asset_dir: Path) -> None:
    """gzip=False bakes no compressed variant."""
    with TestClient(AssetsApp(asset_dir, gzip=False)) as client:
        resp = client.get("/assets/app.css", headers={"accept-encoding": "gzip"})
        assert "content-encoding" not in resp.headers
        assert resp.content == CSS_BODY


def test_missing_directory_fails_at_wiring(tmp_path: Path) -> None:
    """A directory that does not exist is a WiringError at startup."""
    with pytest.raises(RuntimeError, match="is not a directory"):
        TestClient(AssetsApp(tmp_path / "missing"))


def test_unsupported_suffix_fails_at_wiring(tmp_path: Path) -> None:
    """A file with an unknown suffix is a WiringError naming the file."""
    (tmp_path / "blob.xyz").write_bytes(b"blob-body")
    with pytest.raises(RuntimeError, match="unsupported suffix"):
        TestClient(AssetsApp(tmp_path))


def test_no_matching_files_fails_at_wiring(asset_dir: Path) -> None:
    """Globs that match nothing are a WiringError, not a silently empty mount."""
    with pytest.raises(RuntimeError, match="no files matched"):
        TestClient(AssetsApp(asset_dir, include=("*.nope",)))


def test_size_cap_fails_at_wiring(asset_dir: Path) -> None:
    """A total over max_total_bytes is a WiringError pointing at the cap."""
    with pytest.raises(RuntimeError, match="max_total_bytes"):
        TestClient(AssetsApp(asset_dir, max_total_bytes=10))


def test_oversized_file_is_rejected_before_being_read(tmp_path: Path) -> None:
    """A single file already bigger than the cap is rejected by its on-disk size —
    the failure names the file it was about to read, not a running total, proving
    it never got as far as read_bytes()/gzip on that file."""
    (tmp_path / "huge.txt").write_bytes(b"x" * 1000)
    with pytest.raises(RuntimeError, match=r"reading huge\.txt would exceed"):
        TestClient(AssetsApp(tmp_path, gzip=False, max_total_bytes=100))


def test_gzip_variant_pushes_total_over_the_cap_after_reading(tmp_path: Path) -> None:
    """A file whose raw size alone fits the remaining budget can still tip the
    combined (body + gzip variant) total over it once compressed — caught by the
    post-read check, since the pre-read check only knows the raw size."""
    (tmp_path / "a.txt").write_bytes(b"a" * 100)  # raw 100B; gzips to ~24B, kept
    with pytest.raises(RuntimeError, match=r"124 bytes.*hit while reading a\.txt"):
        TestClient(AssetsApp(tmp_path, max_total_bytes=110))


def test_file_count_cap_fails_at_wiring(asset_dir: Path) -> None:
    """More files than max_files is a WiringError, independent of their total size."""
    with pytest.raises(RuntimeError, match="max_files"):
        TestClient(AssetsApp(asset_dir, max_files=1))


# ---------------------------------------------------------------------------
# Symlinks are never served — a symlinked file must not be read through, however
# large or wherever it points.
# ---------------------------------------------------------------------------


def test_symlinked_file_is_never_served(tmp_path: Path) -> None:
    """A symlink inside the served directory pointing at a file outside it is
    excluded entirely, not read and served as if it were a real asset."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_bytes(b"outside-the-tree")
    served = tmp_path / "served"
    served.mkdir()
    (served / "logo.txt").symlink_to(secret)

    with pytest.raises(RuntimeError, match="no files matched"):
        TestClient(AssetsApp(served))


def test_symlinked_file_alongside_real_files_is_just_skipped(tmp_path: Path) -> None:
    """A symlink is excluded silently, like a dotfile — real files in the same
    directory still wire and serve normally."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_bytes(b"outside-the-tree")
    served = tmp_path / "served"
    served.mkdir()
    (served / "logo.txt").symlink_to(secret)
    (served / "real.txt").write_bytes(TXT_BODY)

    with TestClient(AssetsApp(served)) as client:
        assert client.get("/assets/logo.txt").status_code == 404
        real = client.get("/assets/real.txt")
        assert real.status_code == 200
        assert real.content == TXT_BODY


def test_symlinked_directory_is_never_descended_into(tmp_path: Path) -> None:
    """A symlink to a directory outside the served tree is not followed — nothing
    beneath it is ever read."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside-the-tree")
    served = tmp_path / "served"
    served.mkdir()
    (served / "linked-dir").symlink_to(outside)

    with pytest.raises(RuntimeError, match="no files matched"):
        TestClient(AssetsApp(served))


class LogoEndpoint(Endpoint, path="/assets/logo.svg"):
    """Endpoint colliding with an asset route."""

    async def get(self) -> bytes:
        """Serve bytes at the colliding path."""
        return b"endpoint-body"


class CollidingApp(BaseApp):
    """App registering an endpoint on a path an asset also claims."""

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self._directory = directory

    async def wire(self) -> None:
        self._include_endpoint(LogoEndpoint())
        self._include_assets(self._directory)


def test_route_collision_fails_at_wiring(asset_dir: Path) -> None:
    """An asset landing on an already-registered route is a WiringError."""
    with pytest.raises(RuntimeError, match="already registered"):
        TestClient(CollidingApp(asset_dir))


# ---------------------------------------------------------------------------
# A literal asset at the same shape as an existing dynamic route's slot is not an
# error: jero's router has no ordering rules by design (docs/guide/resources.md,
# "Registering them") — a literal always wins for its exact path, and the dynamic
# route still serves every other value. This is the same "id route + literal
# sibling" pattern any REST app relies on (/widgets/{id} + /widgets/search), so an
# assets mount that happens to land on one of those slot values must resolve to
# the file, not fail wiring or silently misroute.
# ---------------------------------------------------------------------------


class WidgetId(Struct):
    """Path param source for the dynamic route an asset shares a shape with."""

    widget_id: str


class WidgetResource(Resource, path="/widgets"):
    """Resource with one dynamic GET, the same shape as an asset named '5.txt'."""

    async def read_one(self, path: WidgetId) -> bytes:
        """Return the id as bytes."""
        return path.widget_id.encode()


class AssetBesideDynamicApp(BaseApp):
    """Assets wired alongside a dynamic resource sharing the same route shape."""

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self._directory = directory

    async def wire(self) -> None:
        self._include_resource(WidgetResource())
        self._include_assets(self._directory, path="/widgets")


@pytest.fixture(name="shadow_dir")
def _shadow_dir(tmp_path: Path) -> Path:
    (tmp_path / "5.txt").write_bytes(TXT_BODY)
    return tmp_path


def test_asset_wins_over_a_same_shaped_dynamic_route_for_its_own_path(
    shadow_dir: Path,
) -> None:
    """The literal asset serves its own path; the dynamic route still serves every
    other value at that depth — both wire together without error, either order."""
    with TestClient(AssetBesideDynamicApp(shadow_dir)) as client:
        asset_resp = client.get("/widgets/5.txt")
        assert asset_resp.status_code == 200
        assert asset_resp.content == TXT_BODY
        dynamic_resp = client.get("/widgets/abc")
        assert dynamic_resp.status_code == 200
        assert dynamic_resp.content == b"abc"


# ---------------------------------------------------------------------------
# A brace-shaped file/directory name must never become a route parameter.
# ---------------------------------------------------------------------------


@pytest.fixture(name="braced_dir")
def _braced_dir(tmp_path: Path) -> Path:
    (tmp_path / "{locale}").mkdir()
    (tmp_path / "{locale}" / "a.css").write_bytes(CSS_BODY)
    return tmp_path


def test_brace_named_directory_is_a_literal_segment_not_a_slot(braced_dir: Path) -> None:
    """A directory literally named '{locale}' must not become a dynamic route: only
    the exact on-disk path is servable, not an arbitrary substituted segment."""
    with TestClient(AssetsApp(braced_dir)) as client:
        assert client.get("/assets/{locale}/a.css").status_code == 200
        assert client.get("/assets/anything-else/a.css").status_code == 404
        assert client.get("/assets/fr/a.css").status_code == 404


def test_two_brace_named_files_do_not_collide(tmp_path: Path) -> None:
    """Two files that would look like the same '{slot}' template must be two distinct
    literal routes, not a route collision or a single merged slot."""
    (tmp_path / "{a}.css").write_bytes(CSS_BODY)
    (tmp_path / "{b}.css").write_bytes(CSS_BODY)
    with TestClient(AssetsApp(tmp_path)) as client:
        assert client.get("/assets/{a}.css").status_code == 200
        assert client.get("/assets/{b}.css").status_code == 200


# ---------------------------------------------------------------------------
# A working dynamic header hook applies normally to an asset route, the same as
# it would to any other route.
# ---------------------------------------------------------------------------


class EchoedHeaders(Struct):
    """Return annotation for a hook that always succeeds."""

    x_echo: str


class WorkingHeadersMiddleware:
    """App-wide middleware whose dynamic hook always succeeds."""

    def response_headers(self, request: Request) -> EchoedHeaders | None:
        """Add a constant header to every covered response."""
        _ = request
        return EchoedHeaders(x_echo="hit")


class AssetsWithWorkingMiddlewareApp(BaseApp):
    """App wiring an app-wide succeeding header hook alongside an assets mount."""

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self._directory = directory

    async def wire(self) -> None:
        self._include_middleware(WorkingHeadersMiddleware())
        self._include_assets(self._directory)


def test_succeeding_header_hook_applies_to_an_asset_route(asset_dir: Path) -> None:
    """A response_headers hook that succeeds decorates an asset response normally —
    the strict tail application isn't just an error path, it's the whole path."""
    with TestClient(AssetsWithWorkingMiddlewareApp(asset_dir)) as client:
        resp = client.get("/assets/logo.svg")
        assert resp.status_code == 200
        assert resp.headers["x-echo"] == "hit"


# ---------------------------------------------------------------------------
# A failing dynamic header hook must become an error response, not a silent 200.
# ---------------------------------------------------------------------------


class BrokenHeaders(Struct):
    """Return annotation for a hook that never actually returns."""

    x_never: str


class BrokenHeadersMiddleware:
    """App-wide middleware whose dynamic hook always raises."""

    def response_headers(self, request: Request) -> BrokenHeaders | None:
        """Blow up unconditionally."""
        _ = request
        raise RuntimeError("header hook exploded")


class AssetsWithBrokenMiddlewareApp(BaseApp):
    """App wiring an app-wide failing header hook alongside an assets mount."""

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self._directory = directory

    async def wire(self) -> None:
        self._include_middleware(BrokenHeadersMiddleware())
        self._include_assets(self._directory)


def test_failing_header_hook_becomes_error_response(asset_dir: Path) -> None:
    """An asset route under a failing app-wide response_headers hook answers the
    framework's error response, exactly like any other route — it must not silently
    ship the asset without the headers the hook was supposed to add."""
    with TestClient(AssetsWithBrokenMiddlewareApp(asset_dir)) as client:
        resp = client.get("/assets/logo.svg")
        assert resp.status_code == 500
        assert resp.json()["type"] == "internal-server-error"


class ProgrammingErrorHeaders(Struct):
    """Return annotation for a hook that raises a framework programming error."""

    x_never: str


class ProgrammingErrorMiddleware:
    """App-wide middleware whose dynamic hook raises WiringError itself."""

    def response_headers(self, request: Request) -> ProgrammingErrorHeaders | None:
        """Raise the framework's own programming-error type, not a handler failure."""
        _ = request
        raise WiringError("misconfigured on purpose")


class AssetsWithProgrammingErrorApp(BaseApp):
    """App wiring a header hook that raises WiringError alongside an assets mount."""

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self._directory = directory

    async def wire(self) -> None:
        self._include_middleware(ProgrammingErrorMiddleware())
        self._include_assets(self._directory)


def test_wiring_error_from_header_hook_stays_loud_on_an_asset_route(asset_dir: Path) -> None:
    """A WiringError from a dynamic header hook is a programming error, not a client-
    facing failure — it must propagate loud on an asset route exactly as it would on
    any other route, never get funneled into a generic 500 Problem body."""
    with (
        TestClient(AssetsWithProgrammingErrorApp(asset_dir)) as client,
        pytest.raises(WiringError, match="misconfigured on purpose"),
    ):
        client.get("/assets/logo.svg")


# ---------------------------------------------------------------------------
# Accept-Encoding grammar: an explicit q=0 is a refusal, not an acceptance.
# ---------------------------------------------------------------------------


def test_accept_encoding_gzip_q0_is_a_refusal(client: TestClient) -> None:
    """'gzip;q=0' explicitly refuses gzip (RFC 9110) even though the substring 'gzip'
    is present in the header value."""
    resp = client.get("/assets/app.css", headers={"accept-encoding": "gzip;q=0"})
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert resp.content == CSS_BODY


def test_accept_encoding_wildcard_is_honored(client: TestClient) -> None:
    """A bare wildcard with no explicit gzip entry accepts gzip."""
    resp = client.get("/assets/app.css", headers={"accept-encoding": "*"})
    assert resp.headers["content-encoding"] == "gzip"


def test_accept_encoding_explicit_gzip_overrides_zero_wildcard(client: TestClient) -> None:
    """An explicit 'gzip' entry wins over a wildcard refusal ('*;q=0')."""
    resp = client.get("/assets/app.css", headers={"accept-encoding": "*;q=0, gzip"})
    assert resp.headers["content-encoding"] == "gzip"


def test_accept_encoding_repeated_q_on_one_directive_takes_the_last(
    client: TestClient,
) -> None:
    """A directive repeating 'q' (undefined by the spec) takes the last one present,
    matching how a repeated header line is resolved elsewhere in this parser."""
    refused = client.get("/assets/app.css", headers={"accept-encoding": "gzip;q=1;q=0"})
    assert "content-encoding" not in refused.headers

    accepted = client.get("/assets/app.css", headers={"accept-encoding": "gzip;q=0;q=1"})
    assert accepted.headers["content-encoding"] == "gzip"


def test_accept_encoding_non_q_param_is_ignored(client: TestClient) -> None:
    """A directive parameter other than 'q' (here, a made-up one) is skipped, not
    mistaken for the quality value."""
    resp = client.get("/assets/app.css", headers={"accept-encoding": "gzip;foo=bar"})
    assert resp.headers["content-encoding"] == "gzip"


def test_accept_encoding_malformed_q_defaults_to_full_acceptance(client: TestClient) -> None:
    """An unparseable q value (empty, here) is treated as though none were given."""
    resp = client.get("/assets/app.css", headers={"accept-encoding": "gzip;q="})
    assert resp.headers["content-encoding"] == "gzip"


def test_accept_encoding_empty_directive_is_skipped(client: TestClient) -> None:
    """A bare comma (an empty directive between separators) is ignored rather than
    raising or being mistaken for a real encoding name."""
    resp = client.get("/assets/app.css", headers={"accept-encoding": ",,gzip,,"})
    assert resp.headers["content-encoding"] == "gzip"


# ---------------------------------------------------------------------------
# Repeated headers are one logical list, not "whichever line arrived last".
# ---------------------------------------------------------------------------


class _CollectSend:
    """ASGI send that records every message."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


async def _empty_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


@asynccontextmanager
async def _lifespan(app: BaseApp) -> AsyncGenerator[None]:
    """Drive ``app`` through ASGI lifespan startup/shutdown around a manually-built
    scope — used here to send repeated same-name headers, which the dict-based
    ``TestClient`` API cannot express."""
    to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(app({"type": "lifespan"}, to_app.get, from_app.put))
    await to_app.put({"type": "lifespan.startup"})
    msg = await from_app.get()
    assert msg["type"] == "lifespan.startup.complete"
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        await from_app.get()
        await task


def _get_scope(path: str, headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": headers,
    }


@pytest.mark.asyncio
async def test_repeated_if_none_match_headers_all_count(asset_dir: Path) -> None:
    """A matching ETag in an earlier If-None-Match header line must not be lost just
    because a later header line with a different value also arrived."""
    app = AssetsApp(asset_dir)
    async with _lifespan(app):
        probe = _CollectSend()
        await app(_get_scope("/assets/logo.svg", []), _empty_receive, probe)
        etag = next(v for k, v in probe.messages[0]["headers"] if k == b"etag")

        revalidate = _CollectSend()
        await app(
            _get_scope(
                "/assets/logo.svg",
                [(b"if-none-match", etag), (b"if-none-match", b'"something-else"')],
            ),
            _empty_receive,
            revalidate,
        )
        assert revalidate.messages[0]["status"] == 304


@pytest.mark.asyncio
async def test_repeated_accept_encoding_headers_merge(asset_dir: Path) -> None:
    """'gzip' arriving on one header line must not be discarded because a later
    header line named a different encoding."""
    app = AssetsApp(asset_dir)
    async with _lifespan(app):
        collected = _CollectSend()
        await app(
            _get_scope(
                "/assets/app.css",
                [(b"accept-encoding", b"gzip"), (b"accept-encoding", b"br")],
            ),
            _empty_receive,
            collected,
        )
        headers = dict(collected.messages[0]["headers"])
        assert headers[b"content-encoding"] == b"gzip"


@pytest.mark.asyncio
async def test_if_none_match_does_not_match_a_substring(asset_dir: Path) -> None:
    """The ETag's bytes appearing as a substring of a malformed, non-delimited value
    must not revalidate — only an exact, comma-tokenized match counts. A raw
    substring check would wrongly treat the ETag as found here, since it's a literal
    prefix of the (malformed) header value."""
    app = AssetsApp(asset_dir)
    async with _lifespan(app):
        probe = _CollectSend()
        await app(_get_scope("/assets/logo.svg", []), _empty_receive, probe)
        etag = next(v for k, v in probe.messages[0]["headers"] if k == b"etag")

        malformed = etag + b'"junk"'  # etag is a literal prefix, but not a real token
        resp = _CollectSend()
        await app(
            _get_scope("/assets/logo.svg", [(b"if-none-match", malformed)]),
            _empty_receive,
            resp,
        )
        assert resp.messages[0]["status"] == 200


# ---------------------------------------------------------------------------
# The size cap must fail as soon as it is exceeded, not after reading everything.
# ---------------------------------------------------------------------------


def test_size_cap_fails_on_the_offending_file_without_reading_the_rest(tmp_path: Path) -> None:
    """The on-disk size is checked per file, before it's read: the cap fires at
    whichever file first exceeds it, and a later file that would double the total is
    never even opened."""
    (tmp_path / "a.txt").write_bytes(b"x" * 10_000)  # sorts first, alone exceeds the cap
    (tmp_path / "z.txt").write_bytes(b"y" * 10_000)  # would double the total if also read
    with pytest.raises(RuntimeError, match=r"reading a\.txt would exceed"):
        TestClient(AssetsApp(tmp_path, gzip=False, max_total_bytes=100))
