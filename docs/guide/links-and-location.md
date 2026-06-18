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
                WidgetResource.read_one, params=WidgetPath(widget_id=json.id)
            ),
        )


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

`POST /widgets` with `{"id": "w1"}` returns `201` and `Location: /widgets/w1`. URLs are
**relative** — always RFC-valid and free of proxy-host ambiguity.

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
                    WidgetResource.read_one, rel="self", params=WidgetPath(widget_id=json.id)
                ),
                Link.from_url("/docs/widgets", rel="help", title="Docs", media_type="text/html"),
            ],
        )


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

This emits:

```
Link: </widgets/w1>; rel="self", </docs/widgets>; rel="help"; title="Docs"; type="text/html"
```

`from_url` is the third constructor — a literal URL (relative or absolute), no reversal.

## Loud and fast

`from_operation(Class.operation, params=...)` validates `params` **at construction**,
introspected from the operation's own `path` annotation — so the wrong Struct fails the
instant you build the link (in a handler, or a unit test that just constructs it), with
no app required:

```python
# read_one declares `path: WidgetPath` — this raises TypeError immediately:
Location.from_operation(WidgetResource.read_one, params=WrongPath(...))
```

(A bare method reference can't carry the `params` type to pyright statically, so this is a
hard *runtime* check at construction — immediate, not deferred to a served request.)

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
            links=[Link.from_ref("jobs.read_one", rel="related", params=JobPath(job_id="job-id"))],
        )


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_resource(JobsResource())
        self._include_endpoint(JobLinkEndpoint())


app = App()
```

The `ref` form trades away some safety, by design: the string can't carry the `params`
type to pyright, so its type check is deferred to resolution rather than construction, and
a typo'd or unmounted ref surfaces when the response is sent rather than at startup. So
**prefer `from_operation`** — reach for `ref` only to break a genuine import cycle (often,
extracting the shared route into a module both import is cleaner still). Duplicate `ref`
values are a startup `WiringError`.

See [Resources & Endpoints](resources.md) for how the path — the thing all of this reverses
against — is declared.
