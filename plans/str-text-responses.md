# Plan: `str` handler returns → `text/plain`

Status: **designed, not built.** Small framework addition. The one thing to nail
is the `str`-means-text (not raw-JSON) distinction.

## The decision (read this first)

A handler may `return "hello"`, and it is sent as a **`text/plain; charset=utf-8`**
body of `hello`. `str` is the **text sibling of `bytes`**: `bytes` →
`application/octet-stream` (raw binary), `str` → `text/plain` (raw text).

`str` does **not** mean "raw JSON." Returning `'{"status":"ok"}'` does not produce
`application/json` — it produces `text/plain` with that literal body. The
"JSON is always a Struct, never a raw blob" rule is unchanged: for JSON you return
a Struct (so there's a real OpenAPI schema); for plain text you return `str`. This
is what makes `str` safe to add — it's a distinct, well-typed response kind, not a
hole in the Struct-only JSON contract.

This also fixes a DX papercut: a handler annotated `-> str` currently raises a
`WiringError` ("must declare a return type of Struct, …"), which sent at least one
contributor reaching for a raw-JSON-string hack. `str` gets an obvious meaning
instead.

## Why it's consistent with the pillars

- **Speed:** `result.encode()` + a fixed content-type, resolved at wiring like
  every other return kind. No per-request work.
- **One blessed way:** adds the blessed way to return *text*; it does not add a
  second way to return JSON.
- **Typing / OpenAPI:** `str` is precise and `text/plain` is schema-able
  (`type: string`, no `Any`, no untyped blob). A `-> str` endpoint appears in the
  spec as a `text/plain` string response.

## Public surface

No new exported type for v1 — a bare `str` return is the whole feature, mirroring
how bare `bytes` works today:

```python
class RobotsEndpoint(Endpoint):
    async def get(self) -> str:
        return "User-agent: *\nDisallow:"        # text/plain; charset=utf-8
```

Allowed on any verb in both `Resource` and `Endpoint`, exactly like `bytes`
returns (a `create` returning `str` → 201 text/plain).

## Implementation

- **`_return_kind`** (core.py): add `if ann is str: return "text"` (alongside the
  `ann is bytes -> "bytes"` arm).
- **`_StrSender`** (core.py): a `@dataclass(slots=True)` mirroring `_BytesSender`,
  but encoding utf-8 and defaulting content-type to `text/plain; charset=utf-8`:
  ```python
  @dataclass(slots=True)
  class _StrSender:
      _status: int

      async def __call__(self, scope: Scope, receive: Receive, send: Send, result: str) -> None:
          _ = (scope, receive)
          payload = result.encode()
          headers = _response_headers(None, b"text/plain; charset=utf-8", len(payload))
          await _send_payload(send, self._status, payload, headers)
  ```
- **`_result_sender`**: `if kind == "text": return _StrSender(status)`.
- **WiringError message** (`_bind_sources`): add `str` to the list of valid return
  types so the failure text stays accurate.
- HEAD is already handled generically (body suppressed, content-length retained) —
  no special-casing needed.

## Tests (extend `tests/test_responses.py`, esoteric local app)

- `Endpoint.get -> str` → 200, body bytes match, `content-type` is
  `text/plain; charset=utf-8`.
- `Resource.create -> str` → 201 + text/plain (status still from the verb).
- HEAD on a `-> str` route → empty body, content-length retained.
- (Doc-only, not a test) a `'{"...}'` string goes out as text/plain, *not* json —
  reinforces the distinction.

## Docs

- `AGENTS.md` "The contract": add `str` (→ `text/plain`) to the allowed returns,
  with the one-line "str is text, not raw JSON; JSON is still always a Struct."
- `jero/core.py` module docstring: same addition to the returns sentence.

## Deferred / notes

- **`TextResponse(BaseResponse)`** with `content: str` (custom headers/status),
  parallel to `BytesResponse`/`JSONResponse` — add when a handler needs to set
  text headers/status; bare `str` covers the common case first.
- **`list[str]`** is *not* supported (no obvious wire meaning — NDJSON of strings
  would be a streaming response). Only scalar `str`.
- **No `str`-as-JSON** path, ever — that stays a Struct.
