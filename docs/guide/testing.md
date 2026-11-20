# Testing

jero ships a synchronous, in-process `TestClient` with a `requests`-style API. It drives
the ASGI app directly: no socket, no running server.

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from jero.testing import TestClient


def test_read_one():
    with TestClient(App()) as client:
        resp = client.get("/widgets/widget-id")
        assert resp.status_code == 200
        assert resp.json() == {"id": "widget-id", "name": "widget-name"}
```

- The client runs the app's full lifespan on a background event loop, so `wire`
  registers the routes and the resources it opened stay open for the whole block.
- Use it as a context manager (or call `close()`) for deterministic shutdown. That is
  what runs the app's resource teardown.

## The request API

The methods are `get` / `post` / `put` / `patch` / `delete` / `head` / `options`, plus
`request`.

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
client.post("/widgets", json={"name": "n", "priceCents": 100}, headers={"authorization": "Bearer token"})
client.post("/upload", files={"document": ("report.pdf", b"...", "application/pdf")})
client.post("/uploads", content=[b"first-chunk", b"second-chunk"])
client.get("/widgets", params={"limit": "5"})
client.get("/theme", cookies={"session_id": "session-value"})
```

| Keyword | Sends |
| --- | --- |
| `json=` | a JSON body |
| `content=` | raw bytes, or an iterable of `bytes` sent as one ASGI message per item (to exercise a multi-chunk body, such as a [`content_stream`](binding.md#streamed-raw-body-content_stream) handler's re-framing) |
| `data=` / `files=` | a multipart body |
| `params=` | the query string |
| `headers=` / `cookies=` | request headers and cookies |

Each call returns a `TestResponse`:

- `status_code`, `headers`, `content`, `.text`, `.json()`.
- `.cookies`: the parsed `Set-Cookie` headers, by name.
- `multi_headers`: the faithful wire pair list, repeats included, for anything
  `.cookies` / `.headers` doesn't cover.

`TestClient(App(), cookie_jar=True)` opts into automatic cookie persistence across
requests (and into WebSocket handshakes). It is off by default. See
[Cookies](cookies.md#testing) for the full request, response and jar treatment.

## Streaming

For streaming endpoints, use the `stream_*` methods. They return a session you iterate
for decoded chunks, and that you can use as a context manager to disconnect early:

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
with TestClient(App()) as client:
    # NDJSON -> decoded objects
    assert list(client.stream_get("/movies")) == [{"title": "a"}, {"title": "b"}]

    # SSE -> decoded events
    with client.stream_get("/events") as events:
        first = next(events)
        assert first.data == {"title": "a"}
        assert first.event == "added"
        # leaving the block disconnects; lifecycle teardown runs
```

## WebSockets

`client.websocket(...)` opens a typed, synchronous socket session. See
[WebSockets: Testing](websockets.md#testing).

## Undeclared errors

When an operation declares its errors with `exceptions=[...]`, every `TestClient`
request checks the answer against that list.

- An error the OpenAPI spec doesn't document fails the request with an `AssertionError`
  naming the operation and the error class.
- There is nothing to turn on.

See [Keeping the list complete](openapi.md#keeping-the-list-complete) for exactly what
counts as documented.

## Mocking dependencies

Inject a stand-in factory through the `factory=` seam so the real I/O services are
never built. This test drives the app from [Wiring](wiring.md):

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from unittest.mock import create_autospec

from jero.testing import TestClient


def test_read_one_delegates_to_service():
    widget_service_mock = create_autospec(WidgetService, spec_set=True, instance=True)
    widget_service_mock.get_widget.return_value = Widget(id="widget-id", name="widget-name")
    factory_mock = create_autospec(Factory, spec_set=True, instance=True)
    factory_mock.create_widget_service.return_value = widget_service_mock

    with TestClient(App(factory=factory_mock)) as client:
        resp = client.get("/widgets/widget-id")

    assert resp.status_code == 200
    assert resp.json() == {"id": "widget-id", "name": "widget-name"}
    widget_service_mock.get_widget.assert_awaited_once_with("widget-id")
```

## `FactoryHarness`

`FactoryHarness` tests the *real* factory wiring that `factory=` mocks away: that each
`create_*` builds the right service and opens and closes its resources.

```python
# doc-example: fragment (illustrative snippet, not a runnable app)
from jero.testing import FactoryHarness


def test_factory_builds_service():
    with FactoryHarness(Factory) as harness:
        service = harness.run(harness.factory.create_widget_service())
        assert isinstance(service, WidgetService)
    # everything opened on the stacks is closed here
```

- It is the **sync-test bridge** over `Factory.open()`, the
  [standalone entry point](wiring.md#standalone-use): the same lifecycle, entered on a
  background loop so synchronous test code can drive async `create_*` methods, matching
  the sync `TestClient`.
- `harness.run(...)` awaits an async `create_*`; a sync one can be called directly on
  `harness.factory`.
- Outside tests, use `async with Factory.open()` directly.
