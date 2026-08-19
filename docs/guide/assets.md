# Static assets

jero serves small static assets the way it does everything else: all the work happens
at wiring, and nothing is added to the request path. `_include_assets` reads a
directory **once, at startup**. Every file becomes an exact route with its bytes,
content type, and a strong `ETag` baked in; requests never touch the filesystem.

```python
from pathlib import Path
from tempfile import mkdtemp

from jero import BaseApp

# Demo stand-in for your project's static/ directory, so this example is runnable
# as-is. In a real app, point _include_assets at a directory in your repo.
assets = Path(mkdtemp())
(assets / "logo.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
(assets / "app.css").write_bytes(b"body { margin: 0 }")


class App(BaseApp):
    async def wire(self) -> None:
        self._include_assets(assets, path="/assets", cache_control="public, max-age=3600")


app = App()
```

`GET /assets/logo.svg` and `GET /assets/app.css` now serve from memory.
Subdirectories keep their relative paths (`static/img/logo.svg` mounts at
`/assets/img/logo.svg`), and an unknown path under the mount is an ordinary 404: there
is no catch-all.

## What you get

- **Conditional requests.** Each file's `ETag` is computed once at wiring; a matching
  `If-None-Match` answers `304` with no body, and no hashing or filesystem work
  happens per request.
- **Compression, also at wiring.** With `gzip=True` (the default), compressible files
  (CSS, JS, HTML, SVG, JSON, and friends) are gzipped once at startup and kept only
  when meaningfully smaller. A request with `Accept-Encoding: gzip` gets the prebaked
  variant with its own `ETag` and a `Vary: Accept-Encoding` header. Already-compressed
  formats (PNG, WebP, WOFF2) are never re-compressed.
- **Cache headers.** `cache_control="public, max-age=3600"` is emitted verbatim on
  both the `200` and the `304`.
- **The usual semantics for free.** `HEAD` is served from the `GET` route with the
  body suppressed; asset routes are covered by the app-default [CORS](cors.md) policy
  and app-wide [middleware](middleware.md); none of it appears in the
  [OpenAPI document](openapi.md).

## Selecting files

`include` and `exclude` are glob patterns ([`fnmatch`](https://docs.python.org/3/library/fnmatch.html))
matched against each file's path relative to the directory. `*` matches across `/`
too, so `"*.map"` excludes a `.map` file at any depth — there's no syntax for "this
one directory level only"; list exact relative paths for that:

```python
from pathlib import Path
from tempfile import mkdtemp

from jero import BaseApp

assets = Path(mkdtemp())  # demo stand-in, as above
(assets / "app.js").write_bytes(b"console.log('app-js')")
(assets / "app.js.map").write_bytes(b"{}")


class App(BaseApp):
    async def wire(self) -> None:
        self._include_assets(assets, exclude=("*.map",))


app = App()
```

Dotfiles are always skipped. A file whose suffix isn't in the supported set is a
`WiringError` naming the file; exclude it or serve it elsewhere.

## Loud at wiring, silent at runtime

Every failure mode is a `WiringError` at startup, never a request-time surprise: a
missing directory, an unsupported suffix, an unreadable file, globs that match
nothing, too many files, a collision with an existing route, and a total size over
the cap. A file already bigger than the remaining budget is rejected by its on-disk
size before it's ever read or compressed — a single huge file can't be fully read
just to discover it doesn't fit.

The two caps (`max_files`, 10,000; `max_total_bytes`, 10 MiB) exist because assets
are held in memory, per worker. That is the honest scope of this feature: an SPA
shell, stylesheets, some images. Raise a cap deliberately if you mean to; for large
files, `Range` requests, or catch-all SPA fallbacks, use your reverse proxy or CDN,
which do that job better than a Python worker ever will (see
[Deployment](deployment.md)). Files added or changed on disk appear on restart, like
everything else wired at startup.

Symlinks are never served, file or directory. A symlinked file would otherwise be
read straight through, serving whatever it points at — anywhere on disk the process
can read — as if it lived under the served directory; a symlinked directory is never
descended into. Point `_include_assets` at a directory you trust the *real* contents
of.
