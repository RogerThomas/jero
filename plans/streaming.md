# Plan: Streaming responses (NDJSON, raw bytes, SSE)

Status: **designed, not built.** This captures the locked design from the
Q1–E design discussion. Build in the staged order at the bottom.

## Goal

Add streaming response types so a handler can return a long/▶ open response made
of many body chunks instead of one. Three kinds, all typed end-to-end (the
generic `T` carries the per-item schema, which the future OpenAPI work will read):

- `StreamingResponse` — raw `bytes` chunks.
- `NDJSONStreamingResponse[T: Struct]` — one JSON Struct per line (`\n`-delimited).
- `SSEResponse[T: Struct | str = str]` — Server-Sent Events (`text/event-stream`).

## Exported public API

```python
@dataclass(kw_only=True, slots=True)
class ServerSentEvent[T: Struct | str]:
    data: T
    event: str | None = None
    id: str | None = None
    retry: int | None = None        # reconnection delay (ms)

# internal shared base (NOT exported)
@dataclass(kw_only=True, slots=True)
class _StreamingResponse[T]:
    stream: Source[T]               # see "unified source" below
    headers: dict[str, str] | None = None
    status: int | None = None       # None → the verb's default status

class StreamingResponse(_StreamingResponse[bytes]): ...          # non-generic; bytes
class NDJSONStreamingResponse[T: Struct](_StreamingResponse[T]): ...
class SSEResponse[T: Struct | str = str](_StreamingResponse[T | ServerSentEvent[T]]):
    keepalive: float | None = None  # seconds; emit `: ping` when idle
```

Exported from `jero`: `StreamingResponse`, `NDJSONStreamingResponse`,
`SSEResponse`, `ServerSentEvent`. Lives in `jero/streaming.py`, re-exported.

## How an app author uses it

**NDJSON, lifecycle form (setup + teardown, no `try/finally`):**
```python
class MovieResource(Resource):
    async def read_many(self) -> NDJSONStreamingResponse[Movie]:
        return NDJSONStreamingResponse(stream=self._service.stream_movies())

@dataclass
class MovieService:
    _db: Database
    async def stream_movies(self) -> AsyncGenerator[AsyncIterable[Movie]]:
        async with self._db.cursor("select ...") as cur:   # teardown via async with
            yield self._rows(cur)
    async def _rows(self, cur: Cursor) -> AsyncIterator[Movie]:
        async for row in cur:
            yield Movie(title=row.title, year=row.year)
```

**NDJSON, plain form (no setup/teardown):**
```python
async def read_many(self) -> NDJSONStreamingResponse[Movie]:
    return NDJSONStreamingResponse(stream=self._service.rows())   # AsyncIterator[Movie]
```

**Raw bytes (file export):**
```python
async def read_one(self, path: ExportPath) -> StreamingResponse:
    return StreamingResponse(
        stream=self._service.stream_csv(path.id),
        headers={"content-type": "text/csv",
                 "content-disposition": 'attachment; filename="export.csv"'},
    )
```

**SSE (GET-only), typed events:**
```python
class NotificationsEndpoint(Endpoint):
    async def get(self, user: User) -> SSEResponse[Notification]:
        return SSEResponse(stream=self._hub.subscribe(user.id), keepalive=15.0)
        # subscribe(): AsyncGenerator[AsyncIterable[Notification | ServerSentEvent[Notification]]]
        # yields bare Notification → `data: {json}`, or ServerSentEvent for event/id/retry
```

**SSE raw text:** `-> SSEResponse` (≡ `SSEResponse[str]`); yield `str` → `data: <text>`.

The user never writes `try/finally`, never polls for disconnect, never breaks a
loop manually. Resource cleanup lives after the single `yield` in the lifecycle
generator; the framework guarantees it runs.

## Locked design decisions

**Wrapper, not handler-as-generator (Q1).** Handler stays a normal coroutine
returning a wrapper; the format lives in the return *type* (`-> NDJSONStreamingResponse[Movie]`).
An async-gen handler can't encode the format in its type and would need a
decorator (which jero refuses). Source generators are *methods* (on the service),
not nested functions — satisfies the no-nested-funcs rule.

**Unified `stream=` source.** One param accepts either a plain item stream or a
one-yield lifecycle generator:
```python
type Source[T] = AsyncIterable[T] | AsyncGenerator[AsyncIterable[T]]
```
Discriminated at runtime by pulling the first value: `isinstance(first, AsyncIterable)`
⇒ lifecycle form (the value *is* the inner stream); else plain form (the value is
item 0). **Unambiguous** because no item type (`Struct` / `bytes` / `str` /
`ServerSentEvent`) is itself async-iterable. Stays statically precise — both arms
carry `T`, and only one arm matches a given value.

**Lifecycle driving (from the user's `streaming.py` prototype):**
- `anext(lifecycle)` → runs setup, returns the inner stream.
- iterate the inner stream, sending chunks.
- `finally`: resume the lifecycle (`anext` again) → runs post-yield teardown,
  expects `StopAsyncIteration`. If it yields a 2nd time → `aclose()` +
  `RuntimeError("streaming lifecycle must yield exactly one stream")`.
- "yields exactly one stream" is the one **runtime** contract (yield-count isn't
  expressible in types — same as `@asynccontextmanager`).

**Teardown via `gen.aclose()`, never task-cancel.** On stop/disconnect the sender
`aclose()`s the active generator → `GeneratorExit` cascades into the (possibly
blocked) inner stream and unwinds through the lifecycle `finally` to teardown.
Awaiting in a `finally` under `GeneratorExit` is safe (no re-yield), so **no
`asyncio.shield` needed**.

**Disconnect-watch (Q2).** The sender concurrently awaits *(next item)* vs
*(`receive()` → `http.disconnect`)*. On disconnect → `aclose()` (→ teardown).
Needed because an idle SSE stream blocks on the next item and would otherwise
never notice the client left. Watch task is cancelled on normal completion.

**Error / commit model (A).** The response "commits" when `http.response.start`
(status + headers) is sent — after which status is locked.
- **Lifecycle form:** setup (advance to first yield) runs *before* commit. A
  setup error → a **normal error response** (`HTTPError` → its status, else 500),
  exactly like a non-streaming handler. This is the reason to prefer the
  lifecycle form. It also lets SSE headers/`onopen` fire promptly (setup, then
  commit, then events).
- **Plain form:** no setup phase → commit immediately, then iterate.
- **After commit:** any exception → **silent close** (truncate). No SSE error
  event.

**HEAD (B).** On a streaming route, HEAD sends `response.start` (status +
content-type, **no** content-length) + empty body and does **not** touch the
stream/lifecycle (no side-effecting setup).

**Verb scope (C).** SSE is **GET-only**, enforced at startup with a `WiringError`
if returned from a non-GET handler. NDJSON and bytes streams are allowed on any
verb.

**SSE shape (Q3 + hybrid).** Yield one of three: a bare `Struct` (→ `data: <json>`),
a bare `str` (→ `data: <text>`, multi-line splits into multiple `data:` lines), or
a `ServerSentEvent[T]` for `event:`/`id:`/`retry:` control. Send-time dispatch by
mutually-exclusive type: `isinstance(item, ServerSentEvent)` → full; `isinstance(item, str)`
→ raw; else `Struct` → json. `data` inside a `ServerSentEvent` follows the same
str-vs-Struct rule.

**keepalive (D).** `SSEResponse(stream=..., keepalive=N)` → the watch also races an
N-second timer; on fire (idle), emit `: ping\n\n` and continue. Three-way race:
next-event vs disconnect vs keepalive.

**Content-type & length.** No `content-length` on any stream (chunked). Content-type:
`application/x-ndjson` (fixed), `text/event-stream` (fixed), `application/octet-stream`
(default for bytes, **overridable** via `headers`, e.g. `text/csv`). `status` =
`response.status` or the verb default.

## TestClient streaming (E)

Verb-named methods (consistent with jero never taking a string verb):
`stream_get` / `stream_post` / `stream_put` / `stream_patch` / `stream_delete`.

Returns a `_StreamSession` that is **both iterable and a context manager**:
```python
for movie in client.stream_get("/movies"):        # finite → exhausts
    assert movie["title"] == "Dune"

with client.stream_get("/notifications") as events:   # infinite → deterministic disconnect
    for event in events:
        assert event.data["title"] == "Dune"
        break
    # leaving the block sends http.disconnect → assert teardown ran
```
Items decoded by response **content-type**: `x-ndjson` → parsed JSON per line
(`Any`); `event-stream` → event object (`.data`, `.event`, `.id`); `octet-stream`
→ `bytes` chunk. Bridges the app's async stream (background loop) to the sync
iterator via `run_coroutine_threadsafe` + a queue; exiting/closing routes
`http.disconnect` into the app.

## Build stages (review each)

1. Response types + `_StreamingResponse` base + `ServerSentEvent`; export; `Source[T]`
   alias; `_return_kind` detection of the three kinds; SSE-GET-only `WiringError`.
2. Streaming senders: unified-source discriminator, plain + lifecycle driving,
   `aclose` teardown, commit/error model, HEAD skip, no content-length.
3. Disconnect-watch; SSE keepalive timer.
4. SSE formatting (event/id/retry, str vs Struct data, multi-line `data:`).
5. `TestClient.stream_*` + decode-by-content-type + disconnect-on-exit.
6. Tests: finite NDJSON, bytes, SSE events, disconnect→teardown, setup-error→
   error response, HEAD skips iteration, SSE-on-POST→`WiringError`, keepalive.

## Deferred / notes

- One `http.response.body` send per item; chunk batching deferred (correctness first).
- SSE error-event-on-failure intentionally **not** done (silent close chosen).
- Per-request scoping (e.g. a transaction) is just the lifecycle generator — no
  framework "scope" machinery.
- Verify `[T: Struct | str = str]` (union bound + PEP 696 default) on 3.14/pyright;
  fallback is requiring explicit `SSEResponse[str]` for the raw case.
