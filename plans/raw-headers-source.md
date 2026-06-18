# Plan: `raw_headers` binding source + `RawHeaders` type (opaque header bag)

Status: **designed, not built.** A deliberate, narrowly-scoped exception to the
"headers are always typed" stance — read the rationale before building or
"simplifying" it away.

## Why this exists (and why it isn't a contradiction)

jero deliberately never exposes raw headers: the `headers` source binds only into
a typed Struct, and `plans/form-typed-headers.md` *removes* the one raw-dict hole
(`FormPart.headers`). This plan adds raw header access back — on purpose, for a
different job. The line:

- **Consuming a header** (read `authorization`, branch on a flag) → model it in a
  typed `headers` Struct. This stays the one blessed way to *read* headers; it's
  the contract and the OpenAPI source (pillar 3). **`raw_headers` is not for this.**
- **Transport** — forwarding the whole bag to an upstream service, or dumping it
  for diagnostics — is not consuming. There is no correct typed shape for "all
  headers, whatever they are": a Struct would silently drop every header you
  didn't enumerate, defeating forwarding. The opaque bag *is* the right type.

`raw_headers` produces no OpenAPI schema, and that's fine — there's nothing
meaningful to document about "the request's raw headers," so it punches no hole in
the generated docs the way an untyped body/return would.

**Do not** use `raw_headers` to dodge typing a header you actually read, and **do
not** delete it as an untyped-I/O violation — it's transport, not contract.

## Goal

An opt-in handler binding source, `raw_headers: RawHeaders`, giving the full
request header bag, available to every Resource/Endpoint handler, coexisting with
the typed `headers` source.

`RawHeaders` is a small immutable, **case-insensitive** view that **preserves
as-sent casing**. It's a concrete typed type (not a bare `dict`), which both
removes the case-sensitivity footgun and signals intent ("opaque bag, transport
only").

```python
class GatewayResource(Resource):
    def __init__(self, upstream: Upstream) -> None:
        self._upstream = upstream

    async def create(self, json: Payload, raw_headers: RawHeaders) -> Reply:
        trace = raw_headers["X-Trace-Id"]            # == raw_headers["x-traceid"]
        return await self._upstream.post(
            "/work", json=json, headers=raw_headers,  # Mapping → drops into niquests
        )
```

Used with typed headers when you both read *and* forward:

```python
async def create(self, headers: Auth, raw_headers: RawHeaders) -> Reply: ...
```

## The `RawHeaders` type (`jero/headers.py`, exported)

Immutable, case-insensitive, casing-preserving, multi-value-aware. Mirrors the
well-understood Starlette `Headers` shape so it's familiar and interops with niquests.

```python
class RawHeaders:
    """Immutable, case-insensitive view of the request headers, preserving as-sent
    names and order. For forwarding/diagnostics — not for reading values you act
    on (model those in a typed `headers` Struct)."""

    __slots__ = ("_pairs",)

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs                       # decoded, original casing, in order

    def __getitem__(self, key: str) -> str: ...   # first match, case-insensitive; KeyError if absent
    def get(self, key: str, default: str | None = None) -> str | None: ...
    def getlist(self, key: str) -> list[str]: ...  # all values for a (case-insensitive) name
    def __contains__(self, key: object) -> bool: ...
    def __iter__(self) -> Iterator[str]: ...       # keys, original casing (Mapping contract: unique)
    def keys(self) -> ...                          # unique keys, first-seen casing
    def values(self) -> ...
    def items(self) -> ...                         # unique keys (Mapping contract)
    def multi_items(self) -> list[tuple[str, str]]: ...  # every pair, incl. repeats — faithful forwarding
    def __len__(self) -> int: ...                  # number of unique keys
    def __repr__(self) -> str: ...                 # shows as-sent casing
```

- Register as `collections.abc.Mapping[str, str]` so `niquests(headers=raw_headers)`
  works for the common case; `headers=raw_headers.multi_items()` when repeated
  headers must survive.
- Case-insensitive comparison: lowercase both sides on lookup. Storage keeps
  original casing for iteration/`repr`/forwarding.
- Plain class with `__slots__` (not a Struct — it's a container with behavior, not
  a data record; cf. `_CompiledAuth`).

## Locked decisions

- **`RawHeaders`, not `dict[str, str]`.** Resolves the multi-value question
  (`getlist`/`multi_items`), fixes case-sensitivity, and is fully typed. (Earlier
  draft proposed a bare dict + an open multi-value question — both now closed.)
- **As-sent casing preserved, case-insensitive access.** Distinct from the
  internal `_raw_headers` (core.py:244), which snake_cases (`x_request_id`) for
  `convert` — do **not** reuse it; build pairs with real wire names.
- **Special-cased annotation.** `raw_headers` is not a Struct source — it must be
  annotated exactly `RawHeaders`, special-cased in `_bind_sources` like
  `content: bytes`. Wrong annotation → `WiringError`.
- **Coexists with `headers`.** Independent: one typed-and-validated, one opaque.
- **Immutable.** Request input; no mutation API.
- **Scope — NOT everywhere:**
  - **Form parts:** excluded. The typed-`H` design stands; re-adding per-part raw
    access recreates the inconsistency we removed. No use case for forwarding a
    part's headers.
  - **Auth:** excluded. Auth *consumes* (validates) → typed `headers` is correct.

## Implementation

- **`jero/headers.py`:** new `RawHeaders` (above). Export `RawHeaders` from `jero`.
- **`_SOURCES`** (core.py:97) → add `"raw_headers"`.
- **`_bind_sources`** (core.py ~589) → special-case `raw_headers`: require
  annotation exactly `RawHeaders`, set a `wants_raw_headers` flag (parallel to
  `content`). `WiringError` otherwise.
- **`_Sources`** → add `raw_headers: bool = False`.
- **`_Binder`** → add `_wants_raw_headers`; include in `_needs_raw`-style gating;
  in `__call__`, build the wire pairs and `kwargs["raw_headers"] = RawHeaders(pairs)`.
  Pairs come from a new helper that preserves real names:
  ```python
  def _wire_header_pairs(scope: Scope) -> list[tuple[str, str]]:
      return [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]]
  ```
  (The existing snake_cased `_raw_headers` dict is still built separately when a
  typed `headers`/auth wants it; both may run if a handler wants both. Cheap,
  opt-in.)

## Tests

- `RawHeaders` unit behavior (the one place a small *pure* type earns a direct
  test per the testing stance): case-insensitive `__getitem__`/`get`/`in`;
  `getlist`/`multi_items` for repeats; `repr`/iteration show as-sent casing; len =
  unique keys.
- Through `TestClient` (`tests/test_binding.py`): handler declaring
  `raw_headers: RawHeaders` sees all headers; `raw["X-Trace-Id"]` works regardless
  of sent casing; coexists with a typed `headers` Struct; repeated header
  preserved via `getlist`; wrong annotation → `WiringError`.

## Out of scope

- Form parts and auth (see Scope).
- Any *response* raw-header passthrough — request-side only.
- Mutable headers.
