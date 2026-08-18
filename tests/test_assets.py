"""Wire-time in-memory static assets: ``_include_assets``."""

from collections.abc import Generator, Sequence
from gzip import decompress
from pathlib import Path

import pytest

from jero import BaseApp, Endpoint
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
