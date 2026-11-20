# Errors

Failures are typed classes you raise. A raised jero error short-circuits the handler
and becomes a JSON body with the right status:

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

- **One shape.** The body looks the same whether the framework raised the error (a 404
  route miss, a 422 validation failure) or you did.
- **Anything else is a 500.** Any other uncaught exception becomes the static
  `internal-server-error` problem. Server internals never leak to the client.
- **Errors document themselves.** Because errors are classes, they carry a schema: the
  [OpenAPI generator](openapi.md#documenting-errors) documents exactly which errors an
  operation raises, body shape included.

## Problem Details

Every framework and application `HTTPError` uses a typed
[RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457) body:

| Field              | Value |
| ------------------ | ----- |
| `type`             | a stable machine-readable code. Clients use `type`, never `title` or `detail`, for programmatic decisions |
| `title`            | the class's human-readable title |
| `status`           | the class's HTTP status |
| `docs`             | optional: a documentation link, from the `docs="https://..."` class option |
| `detail`, `params` | only on [parameterized errors](#parameterized-errors) |

The RFC makes `type` a URI. jero's built-in errors use a short kebab-case code instead
(`not-found`). That is a convention, not a rule: any non-blank string is accepted, so a
URI works too.

For common statuses, jero ships errors ready to raise, with the same body the framework
itself sends:

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from jero import ConflictError, ForbiddenError, GoneError, NotFoundError, TooManyRequestsError


raise NotFoundError()        # 404 {"type": "not-found", ...}
raise ForbiddenError()       # 403 {"type": "forbidden", ...}
raise ConflictError()        # 409 {"type": "conflict", ...}
raise GoneError()            # 410 {"type": "gone", ...}
raise TooManyRequestsError() # 429 {"type": "too-many-requests", ...}
```

- `AuthenticationRequiredError`, `ValidationFailedError`, and the other errors the
  framework raises itself are exported too.
- These carry the status semantics and nothing more. Once an error means something
  domain-specific, define your own by subclassing `HTTPError`, as in the example above.
- There is no `raise HTTPError(409, detail="widget already exists")` form. An ad-hoc
  detail string becomes a contract clients regex against, and a response without a
  stable `type` leaves them dispatching on status alone.

## Parameterized errors

When the human-readable detail contains runtime values, pair it with typed params:

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
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

`ParameterizedHTTPError` is the same error without the dataclass layer: the class
declares no fields, and the raise site passes the params Struct itself.

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from msgspec import Struct

from jero import ParameterizedHTTPError


class WidgetNotFoundParams(Struct, rename="camel"):
    widget_id: str


class WidgetNotFoundError(
    ParameterizedHTTPError[WidgetNotFoundParams],
    type="widget-not-found",
    title="Widget not found",
    status=404,
    detail_template="Widget {widget_id} not found",
): ...


raise WidgetNotFoundError(WidgetNotFoundParams(widget_id="widget-id"))
```

The response and the OpenAPI schema are identical. The difference is where you pay:

| Form                     | Class                               | Raise site |
| ------------------------ | ----------------------------------- | ---------- |
| `DataclassHTTPError`     | repeats the fields, plus a `__post_init__` | plain keyword arguments |
| `ParameterizedHTTPError` | declares nothing                    | builds the params Struct |

## Bring your own error body

Problem Details is the default, not a requirement. Starting an API from scratch, use
the [`HTTPError` family](#problem-details) above. `StructHTTPError` is for bringing jero
to an **existing** error standard: when your wire format is already decided, subclass it
with your own body Struct.

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

The class options declare how **every** field of the body gets its value:

| Source               | Feeds a body field with |
| -------------------- | ----------------------- |
| `consts=`            | a pinned value. It also appears in the OpenAPI schema as an enum const |
| `templates=`         | a string rendered from the raise-time params |
| `status_field=`      | the class's status |
| `params_field=`      | the raise-time params, nested as a Struct ([below](#nesting-the-params)) |
| a same-named param   | whatever the raise site passes |

- **Typed raise site.** Decorate with `@dataclass` and declare the params as fields: the
  generated `__init__` is statically typed.
- **Total coverage, checked at class definition.** Every body field must be fed by
  exactly one source. An unknown field name, a field fed twice, a template on a
  non-`str` field, or a status field that isn't an `int` all fail before the app can
  start.
- **Nothing you pass is mutated.** The wire body is composed fresh at encode time.
- **Templates** follow ordinary `str.format` rules. Escape literal braces as
  `{{thing}}`.

### Nesting the params

`params_field=` names a Struct-typed body field the raise-time params nest into, so the
response carries the rendered text and the raw values:

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

**Sharing one shape across errors.** Make the base generic over its varying part
(`class CompanyBody[E: Struct](Struct): ... extensions: E`) and pin `E` in a concrete
body subclass per error (`class ThingBody(CompanyBody[ThingExt]): ...`). The body handed
to `StructHTTPError[...]` must be that concrete subclass: a subscripted generic like
`StructHTTPError[CompanyBody[ThingExt]]` is a `TypeError` at class definition.

**Catch scope.** `except HTTPError` catches only the Problem family. `except
BaseHTTPError` means "any jero error", both families.

## House-wide error format

`StructHTTPError` covers errors *you* raise. The framework's own errors (404 route
misses, 422 validation failures, the unexpected-exception 500) are Problem-family. To
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

Now `GET /nope` returns:

```json
{"errorCode": "not-found", "errorMessage": "Not found", "statusCode": 404}
```

- **Every Problem-family error** renders this way, including ones your
  [exception handlers](#custom-exception-handlers) translate into.
- **`str(error)`** is the uniform human message: the `title`, or the rendered `detail`
  for a parameterized error.
- **`error.type`** is the stable machine code to map into your own vocabulary.
- **`status_field`** names a field jero *adds* to the body, so it must not already exist
  on `HouseBody`. That is the opposite of `StructHTTPError`'s `status_field`, which
  feeds an existing field.

The rules:

- `StructHTTPError`s render themselves, so each error has exactly one renderer.
- Keep `compose` pure. It receives only the error; request-correlated data belongs in
  exception handlers.
- An adapter failure is contained: it is logged, and the Problem body is sent instead.
- At most one adapter per app.
- The derived OpenAPI error responses [follow it](openapi.md#documenting-errors).

## Custom exception handlers

An exception handler is any hand-wired object with one typed `handle_exception` method.
No base class or decorator is required. The annotation on `exception` is what it
handles, and what it returns decides the response:

| Return                  | Response |
| ----------------------- | -------- |
| `None`                  | default handling continues: an `HTTPError` becomes its problem, any other exception the generic 500 problem |
| an `HTTPError` instance | that error's problem |
| an `ExceptionResponse`  | your own `status_code`, JSON Struct, and headers |

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

The rules:

- **Types come from the signature.** jero infers every type from the method signature
  at wiring.
- **One handler per exception type.** Registering two for the same type is a
  `WiringError`. Handlers for a base and a subclass may coexist, and the nearest type in
  the exception's MRO wins.
- **`ExceptionResponse` needs an error status.** `status_code` is required and must be
  from 400 through 599.
- **Union returns.** A handler may return a union of concrete response types when the
  exception occurrence determines its body, headers, or status. Every union member is
  validated at wiring.
- **`HTTPError` returns.** The union may name declared `HTTPError` subclasses. Their
  `type`, `title`, `status`, and optional `detail_template` stay static class-level
  contracts: the handler only selects and constructs the error instance.
- **A failing handler** gets the generic 500 problem. jero does not dispatch the new
  failure to handlers again.
- **Streaming.** An exception raised after a streaming response has started cannot
  replace that response.

## Related pages

- [REST semantics](rest.md): the status every framework-raised error uses, and why.
- [OpenAPI & docs](openapi.md#documenting-errors): declaring which errors an operation
  raises, so the spec documents them.
