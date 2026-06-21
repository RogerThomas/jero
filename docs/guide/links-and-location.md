# Links & Location

A response often needs to point at *another* route — a `Location` on `201 Created`, a
redirect target, or the status URL on a `202 Accepted`. Because the path lives **on the
class**, jero reverse-routes to a mounted operation from the class itself, so you never
hand-format these URLs.

Two response fields carry them, alongside `headers` / `status_code`:

- `location: Location | None` → a single `Location` header (RFC 9110).
- `links: Sequence[Link]` → one `Link` header, all links joined (RFC 8288).

## Location on a created resource

`create` returns `201` and points `Location` at `read_one`. The method reference
`WidgetResource.read_one` carries the class (its path) and the operation; `params` fills
the URL slots:

```python
from msgspec import Struct

from jero import BaseApp, JSONResponse, Location, Resource


class Widget(Struct):
    id: str


class WidgetPath(Struct):
    widget_id: str


class WidgetResource(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> Widget:        # GET /widgets/{widget_id}
        return Widget(id=path.widget_id)

    async def create(self, json: Widget) -> JSONResponse[Widget]:  # POST /widgets
        return JSONResponse(
            json=json,
            status_code=201,
            location=Location.from_operation(
                WidgetResource.read_one, path=WidgetPath(widget_id=json.id)
            ),
        )


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

`POST /widgets` with `{"id": "w1"}` returns `201` and `Location: /widgets/w1`. URLs are
**relative** by default — always RFC-valid and free of proxy-host ambiguity. See
[Behind a proxy](#behind-a-proxy-x-forwarded-) below to emit absolute public URLs instead.

## Links

`Link` is the same target plus a required `rel` (and optional `title` / `media_type`,
emitted as `type=`). A list joins into one `Link` header:

```python
from msgspec import Struct

from jero import BaseApp, JSONResponse, Link, Resource


class Widget(Struct):
    id: str


class WidgetPath(Struct):
    widget_id: str


class WidgetResource(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> Widget:
        return Widget(id=path.widget_id)

    async def create(self, json: Widget) -> JSONResponse[Widget]:
        return JSONResponse(
            json=json,
            status_code=201,
            links=[
                Link.from_operation(
                    WidgetResource.read_one, rel="self", path=WidgetPath(widget_id=json.id)
                ),
                Link.from_path(
                    "/docs/widgets", rel="help", title="Docs", media_type="text/html"
                ),
                Link.from_url("https://status.example.com", rel="status"),
            ],
        )


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

This emits:

```
Link: </widgets/w1>; rel="self", </docs/widgets>; rel="help"; title="Docs"; type="text/html", <https://status.example.com>; rel="status"
```

Besides `from_operation`, there are two literal constructors: **`from_path(path)`** — a
root-relative path that picks up the app's URL base just like a reversed operation (so it
goes absolute under a configured proxy) — and **`from_url(url)`** — a fully-qualified URL
used verbatim, never rewritten.

## Loud and fast

`from_operation(Class.operation, path=...)` validates the `path` Struct **at construction**,
introspected from the operation's own `path` annotation — so the wrong Struct fails the
instant you build the link (in a handler, or a unit test that just constructs it), with
no app required:

```python
# read_one declares `path: WidgetPath` — this raises TypeError immediately:
Location.from_operation(WidgetResource.read_one, path=WrongPath(...))
```

(A bare method reference can't carry the `path` type to pyrefly statically, so this is a
hard *runtime* check at construction — immediate, not deferred to a served request.)

## Behind a proxy (`X-Forwarded-*`)

Relative URLs are correct for a directly-served app, but behind a reverse proxy or load
balancer two things change: the client sees a different scheme/host than your app does,
and the proxy may strip a path prefix. Reversed URLs become **absolute** when either of
two environment variables is set (read once when the app is constructed — no code change):

| Variable | Effect |
| --- | --- |
| `JERO_BASE_URL` | A static public origin (e.g. `https://api.example.com`, may include a prefix). Absolute against it, with no header trust — safest when your origin is fixed. |
| `JERO_TRUST_FORWARDED` | Truthy (`1`/`true`/`yes`/`on`). Rebuild the origin **per request** from `X-Forwarded-Proto` / `-Host` / `-Port`, and restore the stripped path with `X-Forwarded-Prefix`. |

They're **mutually exclusive** — setting both is a startup `WiringError` (one source for
the base). With `JERO_TRUST_FORWARDED=1`, the same `create` above — for a request carrying
`X-Forwarded-Proto: https`, `X-Forwarded-Host: api.example.com`, `X-Forwarded-Prefix: /api`
— emits:

```
Location: https://api.example.com/api/widgets/w1
```

(`X-Forwarded-For` is the *client IP* — it never shapes a URL, so it plays no part here.)
Operation, ref, and `from_path(...)` links are rewritten against the base; `from_url(...)`
links are left exactly as you wrote them.

Both default **off** (relative), and that matters for `JERO_TRUST_FORWARDED`: honoring
`X-Forwarded-*` when you are *not* behind a trusted proxy lets any client spoof
`X-Forwarded-Host` and poison your `Location` URLs. Setting it is your explicit statement
that everything reaching the app comes through a proxy you control. `JERO_BASE_URL` has no
such risk — it's a constant you set, never client input.

## Circular imports: the `ref` escape hatch

`from_operation` needs to import the target class. When two feature modules link to each
other, that import would form a cycle. For that case — and only that case — declare a
string `ref` on the class and address it with `from_ref("ref.operation", ...)`:

```python
from msgspec import Struct

from jero import BaseApp, Endpoint, JSONResponse, Link, Resource


class Job(Struct):
    id: str


class JobPath(Struct):
    job_id: str


class JobsResource(Resource, path="/jobs", ref="jobs"):
    async def read_one(self, path: JobPath) -> Job:
        return Job(id=path.job_id)


class JobLinkEndpoint(Endpoint, path="/job-link"):
    # Imagine this lives in a module that can't import JobsResource without a cycle.
    async def get(self) -> JSONResponse[Job]:
        return JSONResponse(
            json=Job(id="job-id"),
            links=[Link.from_ref("jobs.read_one", rel="related", path=JobPath(job_id="job-id"))],
        )


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_resource(JobsResource())
        self._include_endpoint(JobLinkEndpoint())


app = App()
```

The `ref` form trades away some safety, by design: the string can't carry the `params`
type to pyrefly, so its type check is deferred to resolution rather than construction, and
a typo'd or unmounted ref surfaces when the response is sent rather than at startup. So
**prefer `from_operation`** — reach for `ref` only to break a genuine import cycle (often,
extracting the shared route into a module both import is cleaner still). Duplicate `ref`
values are a startup `WiringError`.

See [Resources & Endpoints](resources.md) for how the path — the thing all of this reverses
against — is declared.
