# Errors

Failures are typed classes you raise. A raised jero error short-circuits the handler
and becomes a consistent JSON body with the right status — the same shape whether the
framework raised it (a 404 route miss, a 422 validation failure) or you did:

```python
from msgspec import Struct

from jero import BaseApp, HTTPError, Resource


class OutOfStockError(
    HTTPError,
    type="out-of-stock",
    title="Widget out of stock",
    status=409,
): ...


class WidgetPath(Struct):
    widget_id: str


class Widget(Struct):
    id: str


class WidgetResource(Resource, path="/widgets"):
    async def read_one(self, path: WidgetPath) -> Widget:
        if path.widget_id == "sold-out":
            raise OutOfStockError()
        return Widget(id=path.widget_id)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_resource(WidgetResource())


app = App()
```

```json
{"type": "out-of-stock", "title": "Widget out of stock", "status": 409}
```

Because errors are classes, they carry a schema — the [OpenAPI generator](openapi.md#documenting-errors)
documents exactly which errors an operation raises, body shape included.

## Problem Details

Every framework and application `HTTPError` uses a typed
[RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457) body. jero
deviates from the RFC in one way: `type` is a stable machine-readable code, not a
URI. Clients use `type`, never `title` or `detail`, for programmatic decisions.

For common statuses, jero ships errors ready to raise — one line, same typed problem
body the framework itself sends:

```python
from jero import ConflictError, ForbiddenError, GoneError, NotFoundError, TooManyRequestsError


raise NotFoundError()        # 404 {"type": "not-found", ...}
raise ForbiddenError()       # 403 {"type": "forbidden", ...}
raise ConflictError()        # 409 {"type": "conflict", ...}
raise GoneError()            # 410 {"type": "gone", ...}
raise TooManyRequestsError() # 429 {"type": "too-many-requests", ...}
```

(`AuthenticationRequiredError`, `ValidationFailedError`, and the other statuses the
framework raises itself are exported too.) These carry the status semantics and nothing
more — the moment an error means something domain-specific, define your own static
error by subclassing `HTTPError`, as in the example above.

There is no `raise HTTPError(409, detail="widget already exists")` form. An ad-hoc
detail string becomes a contract clients regex against, and a response without a
stable `type` leaves them dispatching on status alone. The class is four lines, once,
and every raise site stays consistent.

## Parameterized errors

When the human-readable detail contains runtime values, pair it with typed params:

```python
from dataclasses import dataclass

from msgspec import Struct

from jero import DataclassHTTPError


class WidgetNotFoundParams(Struct, rename="camel"):
    widget_id: str


@dataclass
class WidgetNotFoundError(
    DataclassHTTPError[WidgetNotFoundParams],
    type="widget-not-found",
    title="Widget not found",
    status=404,
    detail_template="Widget {widget_id} not found",
):
    widget_id: str

    def __post_init__(self) -> None:
        self._set_params(WidgetNotFoundParams(widget_id=self.widget_id))


raise WidgetNotFoundError(widget_id="widget-id")
```

The response includes both `"detail": "Widget widget-id not found"` and
`"params": {"widgetId": "widget-id"}`. `detail` and `params` cannot appear separately.
An optional `docs="https://..."` class option adds documentation for either error form.

Any other uncaught exception becomes the static `internal-server-error` problem; server
internals never leak to the client.

## Bring your own error body

Problem Details is the default, not a requirement: starting an API from
scratch, use the [`HTTPError` family](#problem-details) above. `StructHTTPError`
is the channel for backporting jero onto an **existing** error standard — when your
wire format is already decided, subclass it with your own body Struct. The class options
declare how **every** field of the body gets its value — pinned constants, templates
rendered from raise-time params, the class's status — and whatever is left over is a
raise-time parameter. Decorate with `@dataclass` and declare those params as fields:
the generated `__init__` gives you a statically-typed raise site.

```python
from dataclasses import dataclass

from msgspec import Struct

from jero import BaseApp, Endpoint, StructHTTPError


class HouseBody(Struct, rename="camel"):
    error_code: str
    error_message: str
    status_code: int


@dataclass
class DocumentTooLargeError(
    StructHTTPError[HouseBody],
    status=413,
    description="Document too large",              # the OpenAPI response description
    consts={"error_code": "abcd"},                 # pinned: wire value + schema const
    templates={"error_message": "Document is {size} bytes; the limit is 50MB"},
    status_field="status_code",                    # fed the class's status
):
    size: int                                      # the raise-time param, typed


class Accepted(Struct):
    size: int


class DocumentsEndpoint(Endpoint, path="/documents"):
    async def post(self, content: bytes) -> Accepted:
        if len(content) > 50_000_000:
            raise DocumentTooLargeError(size=len(content))
        return Accepted(size=len(content))


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(DocumentsEndpoint())


app = App()
```

```json
{"errorCode": "abcd", "errorMessage": "Document is 50000001 bytes; the limit is 50MB", "statusCode": 413}
```

Total coverage is enforced loud at class definition: every body field must be fed by
exactly one source (a const, a template, the status, or a same-named param) — an
unknown field name, a field fed twice, a template on a non-`str` field, or a status
field that isn't an `int` all fail before the app can start. The wire representation is
composed fresh at encode time from a model built once at class creation; nothing you
pass is ever mutated, and the pinned values appear in the OpenAPI schema as enum
consts, so the spec documents exactly which code (and status) each error carries.

One more source covers richer house formats: **`params_field=`** — a Struct-typed
body field the raise-time params nest into, so the response carries the rendered text
*and* the raw values:

```python
from dataclasses import dataclass

from msgspec import Struct

from jero import BaseApp, Endpoint, StructHTTPError


class ThingExtensions(Struct, rename="camel"):
    thing: str


class CompanyBody(Struct, rename="camel"):
    error_code: str
    error_description: str
    extensions: ThingExtensions


@dataclass
class ThingFailedError(
    StructHTTPError[CompanyBody],
    status=422,
    description="Thing failed",
    consts={"error_code": "abc"},
    templates={"error_description": "This {thing} has failed"},
    params_field="extensions",
):
    thing: str


class Ok(Struct):
    ok: bool


class ThingsEndpoint(Endpoint, path="/things"):
    async def post(self, content: bytes) -> Ok:
        _ = content
        raise ThingFailedError(thing="my-thing")


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(ThingsEndpoint())


app = App()
```

```json
{"errorCode": "abc", "errorDescription": "This my-thing has failed",
 "extensions": {"thing": "my-thing"}}
```

The description renders server-side from the params, and the same params ship raw in
`extensions` for clients that want the structured values. Templates follow ordinary
`str.format` rules (escape literal braces as `{{thing}}`). For a shared shape across
errors, make the base generic over its varying part
(`class CompanyBody[E: Struct](Struct): ... extensions: E`) and pin `E` in a concrete
body subclass per error (`class ThingBody(CompanyBody[ThingExt]): ...`). The body handed
to `StructHTTPError[...]` must be that concrete subclass — a subscripted generic like
`StructHTTPError[CompanyBody[ThingExt]]` is a `TypeError` at class definition.

Catch scope: `except HTTPError` catches only the Problem family; `except BaseHTTPError`
means "any jero error", both families.

## House-wide error format

`StructHTTPError` covers errors *you* raise. The framework's own errors — 404 route
misses, 422 validation failures, the unexpected-exception 500 — are Problem-family. To
render those in your house shape too, register an `ErrorBodyAdapter`: the app-wide
renderer for the Problem family.

```python
from msgspec import Struct

from jero import BaseApp, Endpoint, ErrorBodyAdapter, HTTPError


class HouseBody(Struct, rename="camel"):
    error_code: str
    error_message: str


class HouseErrorAdapter(ErrorBodyAdapter[HouseBody]):
    status_field = "status_code"

    def compose(self, error: HTTPError) -> HouseBody:
        return HouseBody(error_code=error.type, error_message=str(error))


class Health(Struct):
    status: str


class HealthEndpoint(Endpoint, path="/healthz"):
    async def get(self) -> Health:
        return Health(status="ok")


class App(BaseApp):
    async def wire(self) -> None:
        self._include_error_adapter(HouseErrorAdapter())
        self._include_endpoint(HealthEndpoint())


app = App()
```

Now `GET /nope` returns
`{"errorCode": "not-found", "errorMessage": "Not found", "statusCode": 404}` — and the
same for every Problem-family error, including ones your
[exception handlers](#custom-exception-handlers) translate into. `str(error)` is the
uniform human message (the `title`, or the rendered `detail` for parameterized errors);
`error.type` is the stable machine code to map into your own vocabulary.
`StructHTTPError`s render themselves, so each error has exactly one renderer. Keep
`compose` pure — it receives only the error; request-correlated data belongs in
exception handlers. An adapter failure is contained: logged, with the Problem body sent
instead. At most one adapter per app, and the derived OpenAPI error responses
[follow it](openapi.md#documenting-errors).

## Custom exception handlers

An exception handler is any hand-wired object with one typed `handle_exception` method.
No base class or decorator is required. Return an `ExceptionResponse` to replace the
exception, or `None` to continue default handling (`HTTPError` becomes its problem;
another exception becomes the generic 500 problem):

```python
from msgspec import Struct

from jero import BaseApp, Endpoint, ExceptionResponse


class UpstreamError(Exception):
    """A call to an upstream service failed."""

    def __init__(self, *, retryable: bool, safe_to_expose: bool) -> None:
        super().__init__("upstream call failed")
        self.retryable = retryable
        self.safe_to_expose = safe_to_expose


class Status(Struct):
    ok: bool


class FailureBody(Struct):
    code: str


class FailureHeaders(Struct):
    retry_after: int


class UpstreamHandler:
    def handle_exception(
        self, exception: UpstreamError
    ) -> ExceptionResponse[FailureBody, FailureHeaders] | None:
        if not exception.safe_to_expose:
            return None
        return ExceptionResponse(
            status_code=503 if exception.retryable else 502,
            json=FailureBody(code="upstream-failed"),
            headers=FailureHeaders(retry_after=30),
        )


class StatusEndpoint(Endpoint, path="/status"):
    async def get(self) -> Status:
        raise UpstreamError(retryable=True, safe_to_expose=True)


class App(BaseApp):
    async def wire(self) -> None:
        self._include_exception_handler(UpstreamHandler())
        self._include_endpoint(StatusEndpoint())


app = App()
```

jero infers every type from the method signature at wiring. Registering two handlers
for the same exception type is a `WiringError`; handlers for a base and subclass may
coexist, and the nearest type in the exception's MRO wins. Exceptions raised after a
streaming response has started cannot replace that response. `ExceptionResponse`
requires an error `status_code` from 400 through 599; if a custom handler itself fails,
jero sends the generic 500 problem without recursively dispatching the new failure.
A handler may return a union of concrete response types when the exception occurrence
determines its body, headers, or status; every union member is validated at wiring.
It may also return a union of declared `HTTPError` subclasses; their `type`, `title`,
`status`, and optional `detail_template` remain static class-level contracts, and the
handler only selects and constructs the appropriate error instance.

## Related pages

- [REST semantics](rest.md) — the status every framework-raised error uses, and why.
- [OpenAPI & docs](openapi.md#documenting-errors) — declaring which errors an operation
  raises, so the spec documents them.
