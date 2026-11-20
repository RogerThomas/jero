# Links & Location

A response often needs to point at *another* route: a `Location` on `201 Created`, a
redirect target, or the status URL on a `202 Accepted`. jero builds these URLs from the
target class and its path, so you never hand-format them.

Two fields on every response wrapper carry them, alongside `headers` / `status_code`:

- `location: Location | None` → a single `Location` header (RFC 9110).
- `links: Sequence[Link]` → one `Link` header, all links joined (RFC 8288).

## Location on a created resource

`create` returns `201` and points `Location` at `read_one`:

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
            location=Location.from_operation(
                WidgetResource.read_one, path=WidgetPath(widget_id=json.id)
            ),
        )


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

`POST /widgets` with `{"id": "widget-id"}` returns `201` and
`Location: /widgets/widget-id`.

- **`WidgetResource.read_one`**, the method reference, names the class (its path) and the
  operation.
- **`path`** fills the URL slots.
- **URLs are relative** by default. See [Behind a proxy](#behind-a-proxy) to emit
  absolute public URLs.

## The four constructors

`Location` and `Link` share the same constructors (`Link` adds `rel`):

| Constructor                              | Points at                                         |
| ---------------------------------------- | ------------------------------------------------- |
| `from_operation(Class.operation, path=...)` | a mounted operation, reverse-routed            |
| `from_path(path)`                        | a literal root-relative path. It picks up the app's URL base like a reversed operation, so it goes absolute [behind a proxy](#behind-a-proxy) |
| `from_url(url)`                          | a fully-qualified URL, used verbatim and never rewritten |
| `from_ref("ref.operation", path=...)`    | an operation by string, for [import cycles](#circular-imports-the-ref-escape-hatch) only |

## Links

A `Link` is a target plus a required `rel`, and optional `title` / `media_type` (emitted
as `type=`). A list joins into one `Link` header:

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
    async def wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

This emits:

```
Link: </widgets/widget-id>; rel="self", </docs/widgets>; rel="help"; title="Docs"; type="text/html", <https://status.example.com>; rel="status"
```

## Validated at construction

`from_operation` checks `path` against the operation's own `path` annotation. The wrong
`path` Struct raises `TypeError` when the link is built:

```python
# read_one declares `path: WidgetPath`, so this raises TypeError immediately:
Location.from_operation(WidgetResource.read_one, path=WrongPath(...))
```

This is a runtime check, not one the type checker makes. It needs no running app, so a
unit test that only constructs the link catches it.

## Behind a proxy

Behind a reverse proxy you usually want **absolute** public URLs. Set one environment
variable and every reversed URL goes absolute, with no code change:

- `JERO_BASE_URL`: a static public origin.
- `JERO_TRUST_FORWARDED`: rebuild the origin per request from `X-Forwarded-*`.

Details, and the trust warning that comes with forwarded headers, are on
[Deployment](deployment.md#behind-a-reverse-proxy).

## Circular imports: the `ref` escape hatch

`from_operation` needs to import the target class. When two feature modules link to each
other, that import would form a cycle. For that case only, declare a string `ref` on the
class and address it with `from_ref("ref.operation", ...)`:

```python
from msgspec import Struct

from jero import BaseApp, Endpoint, JSONResponse, Link, Resource


class Job(Struct):
    id: str


class JobPath(Struct):
    job_id: str


class JobResource(Resource, path="/jobs", ref="jobs"):
    async def read_one(self, path: JobPath) -> Job:
        return Job(id=path.job_id)


class JobLinkEndpoint(Endpoint, path="/job-link"):
    # Imagine this lives in a module that can't import JobResource without a cycle.
    async def get(self) -> JSONResponse[Job]:
        return JSONResponse(
            json=Job(id="job-id"),
            links=[Link.from_ref("jobs.read_one", rel="related", path=JobPath(job_id="job-id"))],
        )


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(JobResource())
        self._include_endpoint(JobLinkEndpoint())


app = App()
```

- **A `ref` is checked only when the response is sent**, so prefer `from_operation`.
  That covers the `path` type, a typo'd ref, and a ref that was never mounted.
- **Duplicate `ref` values** are a startup `WiringError`.
- Extracting the shared route into a module both sides import is often cleaner still.

See [Resources & Endpoints](resources.md) for how the path — the thing all of this reverses
against — is declared.
