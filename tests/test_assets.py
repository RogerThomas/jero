"""Wire-time in-memory static assets: ``_include_assets``."""

import asyncio
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import asynccontextmanager
from gzip import decompress
from pathlib import Path
from typing import Any

import pytest
from msgspec import Struct

from jero import BaseApp, Endpoint, Request, Resource
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
    ) -> None:
        super().__init__()
        self._directory = directory
        self._path = path
        self._include = include
        self._exclude = exclude
        self._gzip = gzip
        self._cache_control = cache_control
        self._max_total_bytes = max_total_bytes

    async def wire(self) -> None:
        self._include_assets(
            self._directory,
            path=self._path,
            include=self._include,
            exclude=self._exclude,
            gzip=self._gzip,
            cache_control=self._cache_control,
            max_total_bytes=self._max_total_bytes,
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


def test_matching_if_none_match_is_304(client: TestClient) -> None:
    """The wiring-time ETag round-trips: a matching If-None-Match gets 304, no body."""
    etag = client.get("/assets/logo.svg").headers["etag"]
    resp = client.get("/assets/logo.svg", headers={"if-none-match": etag})
    assert resp.status_code == 304
    assert resp.content == b""
    assert resp.headers["etag"] == etag


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


class WidgetsResource(Resource, path="/widgets"):
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
        self._include_resource(WidgetsResource())
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


# ---------------------------------------------------------------------------
# The size cap must fail as soon as it is exceeded, not after reading everything.
# ---------------------------------------------------------------------------


def test_size_cap_fails_on_the_offending_file_without_reading_the_rest(tmp_path: Path) -> None:
    """The running total is checked per file: the cap fires at whichever file first
    exceeds it, and the reported total does not include files that were never read."""
    (tmp_path / "a.txt").write_bytes(b"x" * 10_000)  # sorts first, alone exceeds the cap
    (tmp_path / "z.txt").write_bytes(b"y" * 10_000)  # would double the total if also read
    with pytest.raises(RuntimeError, match=r"10000 bytes.*hit while reading a\.txt"):
        TestClient(AssetsApp(tmp_path, gzip=False, max_total_bytes=100))
