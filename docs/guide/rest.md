# REST & error semantics

jero follows REST/HTTP semantics out of the box — the status codes, `HEAD`, and
`OPTIONS` are handled for you, consistently, so you don't hand-roll them per route.

## Status codes

| Situation                                            | Status |
| ---------------------------------------------------- | ------ |
| Unmatched URL                                        | 404    |
| Path value that fails conversion to its field type   | 404    |
| Malformed query string or headers                    | 400    |
| Malformed JSON body                                  | 400    |
| Well-formed body that fails the schema               | 422    |
| Auth failure                                        | 401    |
| Wrong method for a known path                        | 405 (with `Allow`) |
| Unsupported media type where a form is expected      | 415    |
| `create` success                                     | 201    |
| Other success                                        | 200    |

The split between **400** (malformed — can't even parse) and **422** (well-formed but
fails validation) is deliberate and follows the binding source: a body that isn't valid
JSON is 400; valid JSON that doesn't match the `Struct` is 422. A bad *path* value is
404, because a segment that doesn't convert doesn't identify a resource.

## Problem Details errors

Every framework and application `HTTPError` uses a typed Problem Details body. jero's
intentional deviation from RFC 9457 is that `type` is a stable machine-readable code,
not a URI. Clients use `type`, never `title` or `detail`, for programmatic decisions.

Define a static error by subclassing `HTTPError`:

```python
from jero import HTTPError


class AuthenticationRequiredError(
    HTTPError,
    type="authentication-required",
    title="Authentication required",
    status=401,
): ...


raise AuthenticationRequiredError()
```

```json
{"type": "authentication-required", "title": "Authentication required", "status": 401}
```

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

## Custom exception handlers

An exception handler is any hand-wired object with one typed `handle_exception` method.
No base class or decorator is required. Return an `ExceptionResponse` to replace the
exception, or `None` to continue default handling (`HTTPError` becomes its problem;
another exception becomes the generic 500 problem):

```python
from msgspec import Struct

from jero import ExceptionResponse


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


class App(BaseApp):
    async def _wire(self) -> None:
        self._add_exception_handler(UpstreamHandler())
        self._include_endpoint(StatusEndpoint())
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

## HEAD and OPTIONS

These are synthesized; you never write them:

- **`HEAD`** is served from the matching `GET` route with the body suppressed (and a
  streaming `GET` is *not* iterated for a `HEAD`).
- **`OPTIONS`** answers `204` with an `Allow` header listing the methods for that path.
- A **`405`** likewise carries an `Allow` header. `Allow` always includes `OPTIONS`, and
  `HEAD` wherever `GET` is available.

## Custom status

Override the default success status per response with `status_code` on a response
wrapper — see [Responses & headers](responses.md#status-codes).

## Why this is fixed, not configurable

These semantics are part of jero being opinionated: there's one correct mapping, the
framework encodes it, and "what status should this return?" never reaches code review.
Everything here is resolved at startup or by fixed rules — nothing adds work to the
request path.
