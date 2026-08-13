# Testing

jero ships a synchronous, in-process `TestClient` — it drives the ASGI app directly,
no socket and no running server. It runs the app's full lifespan (so `wire` registers
routes and the dependency context stays open) on a background event loop, and exposes a
`requests`-style API.

```python
from jero.testing import TestClient


def test_read_one():
    with TestClient(App()) as client:
        resp = client.get("/widgets/abc")
        assert resp.status_code == 200
        assert resp.json() == {"id": "abc", "name": "widget-name"}
```

Use it as a context manager (or call `close()`) for deterministic shutdown — that's
what runs the app's resource teardown.

## The request API

`get` / `post` / `put` / `patch` / `delete` / `head` / `options`, plus `request`. Bodies
go in as `json=`, `content=` (raw bytes), or `data=` / `files=` (multipart):

```python
client.post("/widgets", json={"name": "n", "priceCents": 100}, headers={"authorization": "Bearer token"})
client.post("/upload", files={"document": ("report.pdf", b"...", "application/pdf")})
client.get("/widgets", params={"limit": "5"})
client.get("/theme", cookies={"session_id": "session-value"})
```

`TestClient(App(), cookie_jar=True)` opts into automatic cookie persistence across
requests (and into WebSocket handshakes) — off by default. See
[Cookies](cookies.md#testing) for the full request/response/jar treatment.

Each call returns a `TestResponse` with `status_code`, `headers`, `content`, `.text`,
`.json()`, `.cookies` (parsed `Set-Cookie` headers, by name — see [Cookies](cookies.md#testing)),
and `multi_headers` — the faithful wire pair list, repeats included, for anything
`.cookies`/`.headers` doesn't cover.

## Streaming

For streaming endpoints, use the `stream_*` methods. They return a session you iterate
for decoded chunks (and that you can use as a context manager to disconnect early):

```python
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

## Mocking dependencies

Inject a stand-in factory through the `factory=` seam so the real I/O services are
never built:

```python
from unittest.mock import create_autospec


def test_create_widget(mocker):
    factory_mock = create_autospec(Factory, spec_set=True, instance=True)
    widget_service_mock = create_autospec(WidgetService, spec_set=True, instance=True)
    factory_mock.create_widget_service.return_value = widget_service_mock
    widget_service_mock.create_widget.return_value = Widget(id="1", name="n")

    with TestClient(App(factory=factory_mock)) as client:
        resp = client.post("/widgets", json={...}, headers={...})
        assert resp.status_code == 201
    widget_service_mock.create_widget.assert_awaited_once()
```

## `FactoryHarness`

To test the *real* factory wiring that `factory=` mocks away — that each `create_*`
builds the right service and opens/closes its resources — use `FactoryHarness`. It is
the **sync-test bridge** over `Factory.open()` (the
[standalone entry point](wiring.md#standalone-use)): the same lifecycle, entered on a
background loop so synchronous test code can drive async `create_*` methods, matching
the sync `TestClient`. Outside tests, use `async with Factory.open()` directly.

```python
from jero.testing import FactoryHarness


def test_factory_builds_service():
    with FactoryHarness(Factory) as harness:
        service = harness.run(harness.factory.create_widget_service())
        assert isinstance(service, WidgetService)
    # everything opened on the stacks is closed here
```
