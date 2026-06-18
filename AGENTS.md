# AGENTS.md

## jero

An opinionated, msgspec-first ASGI web framework (Python 3.14). The goal is a
framework that is **both** very fast **and** a joy to build on — achieved by
being aggressively opinionated rather than flexible.

> **Naming:** always write the name lowercase — `jero`, never `Jero` — even at the
> start of a sentence (à la `pytest`/`uv`/`ruff`). Capitalization is reserved for the
> logo wordmark only.

## Design philosophy (three pillars, all non-negotiable)

1. **Speed.** All introspection happens once, at wiring time; the per-request
   path is just dict lookup → msgspec decode → call → encode, nothing else.
   Benchmarks co-lead blacksheep and well outpace FastAPI. Never add per-request
   work — resolve it at startup.

2. **Opinionated, scaffolded DX.** There is *one blessed way* to do each thing,
   and the framework encodes the expertise so the developer can't get it wrong
   (lifecycle, REST semantics, validation, dependency wiring). Contracts are
   checked at startup and fail loud with a precise `WiringError` — never
   silently at runtime. The aim: there's a framework answer to "how do I do X?"
   so that question never reaches code review.

3. **Strict, expressive typing — not optional.** Everything is fully, statically
   typed under pyright-strict. If you don't like typing, this is not your
   framework. Types are not decoration — they *are* the contract (the binding,
   the `WiringError`s) and, soon, the source of the OpenAPI spec (see roadmap).
   Every design decision must produce precise static types; prefer rich,
   self-documenting generics (`-> NDJSONStreamingResponse[Movie]`,
   `BaseApp[Factory]`) over loose annotations. A feature that can't be expressed
   in exact static types isn't done. **Never** reach for `Any` or untyped
   `dict`/`bytes` I/O to dodge a type — it punches a hole in both the contract
   and the generated docs. Hold this standard in every future design session.

These pull against each other constantly; keep all three in mind on every change.

## Working on jero

- **Read `style-guide.md` before writing code** — project conventions beyond what
  ruff/pyright enforce (dataclasses, no globals, member ordering, no nested
  funcs/classes, etc.).
- pyright **strict** and ruff must stay clean; tests must pass. `task pyright`,
  `task ruff`, `task test` — or run everything at once with `task check`.
- **Pytest profiles:** the test tasks accept `PYTEST_PROFILE=agent|dev` to select a
  collection of pytest flags. **Always use `PYTEST_PROFILE=agent` by default** — it
  produces concise output suited for agent consumption (`task test PYTEST_PROFILE=agent`).
  If a test fails, re-run that single test in isolation with `PYTEST_PROFILE=dev` for
  verbose output to help debug, e.g.
  `task test-one PYTEST_PROFILE=dev -- tests/test_streaming.py::test_x`.
- **Per-test timeout:** every test has a default `1s` timeout (via `pytest-timeout`);
  a test that exceeds it fails instead of hanging the run. Override a single slow test
  with `@pytest.mark.timeout(N)` rather than raising the global default.
- **Never suppress a lint/type error to make it pass — always fix the code.**
  Adding *any* ignore/disable — `# pylint: disable=…`, `# noqa`, `# type: ignore`,
  `# pyright: ignore`, a `disable`/`ignore`/`per-file-ignores` entry in config, a
  `deptry` ignore, etc. — is **forbidden without explicit human approval first**.
  Ask, with the specific rule and why a code fix won't do; apply it only once the
  human says yes. This applies to *every* tool, every time — no exceptions, no
  "obviously fine" cases.
- For framework-level / design changes, **discuss the design first** — don't just
  implement. Give options + a recommendation, then build once decided.
- **Testing stance:** tests run only through the public boundary — `TestClient`
  against demo apps in `tests/`. **Do not unit-test `jero/` internals directly**;
  they're covered transitively. This is deliberate (style-guide rule 7, and it
  lets the internals be refactored freely — which they are, often). Don't "fix"
  the absence of internal tests. Revisit only once the internals stabilize
  (approaching a stable release), and even then only a thin layer for the
  intricate *pure* helpers (`_parse_template`/`_route_segments`, the
  `__orig_bases__` factory-type extraction, SSE/NDJSON formatting).

## The contract (how apps are built)

- **The operation set is closed and fixed** — this is load-bearing. A `Resource`'s
  operations are *exactly* `create` / `read_one` / `read_many` / `update` /
  `partial_update` / `delete`; an `Endpoint`'s are *exactly* `get` / `post` / `put` /
  `patch` / `delete`. The **method name *is* the operation** (a deterministic, total
  mapping defined by `Resource.METHODS` / `Endpoint.METHODS`) — you cannot rename them,
  add others, or define arbitrary route methods. A method whose name isn't in the set is
  just a regular method, not a route. Because the set is fixed and finite, anything
  keyed per-operation (e.g. the `meta_<op>` kwargs) is a closed enumeration — there is no
  "drift" between method names and per-op declarations to worry about.
- **`Resource`** — a class defining any of the six CRUD operations above →
  POST / GET(item) / GET(collection) / PUT / PATCH / DELETE, with item/collection path
  semantics.
- **`Endpoint`** — a class defining any of the five bare verbs above, for non-resource
  routes (health, webhooks, actions). One exact path per Endpoint; a different path is a
  different `Endpoint`.
- **The path is declared on the class, not at wiring** — `class Widgets(Resource,
  path="/widgets")`, read once at wiring; `_include_resource(Widgets())` takes no
  `path=`. The class is the single source of truth for its path (what URL reversal /
  `Link` / `Location` and the OpenAPI spec read). Optional OpenAPI metadata rides the
  same class kwargs: `meta` (all operations) and `meta_<op>` per operation, typed
  `EndpointMeta` / `ResourceMeta` / `OperationMeta` (the wrong meta type on a shape is a
  loud failure; `operation_id` lives only on `OperationMeta`).
- Handler args bind **by name**, each a msgspec Struct: `json`, `content` (raw
  bytes), `form` (multipart) — the three body sources are mutually exclusive —
  `params` (query), `path` (URL template slots), `headers` (typed), `raw_headers`
  (opaque `RawHeaders` bag), `user` (auth result). Return a Struct, `list[Struct]`,
  `bytes`, or a response wrapper to control headers/status: `JSONResponse[T, H]` /
  `BytesResponse[H]` / a streaming response (`NDJSONStreamingResponse[T, H]`, …).
- **Response headers & status**: the wrappers carry a typed `headers` Struct (the
  header *type* is a parameter `H`; field names inverse-mangle `x_trace_id` →
  `x-trace-id`, scalars stringify, Structs JSON-encode, None fields omit), a
  `raw_headers` escape hatch (exotic names, casing, repeats — e.g. `Set-Cookie`),
  and a `status_code` override (else the verb's default). The buffered wrappers are
  `@dataclass` (like the streaming ones), generic over body `T` and headers `H` so
  both schemas survive to the OpenAPI spec — a bare `JSONResponse` (no `[T]`) is a
  pyright-strict error on purpose.
- **A JSON body is always a Struct — never a raw `dict`.** The
  `@api.get(...) → return {"a": 1}` idiom is gone: a `dict`/blob return is a
  `WiringError` at startup. JSON in and out is a typed Struct, every time — that's
  what gives it validation *and* a schema for the OpenAPI spec. No exceptions.
- **Auth**: an object with `authenticate(headers: Struct) -> UserStruct`; the
  user type is checked against handlers at startup.
- **Wiring / DI**: there is **no DI container** — and that's deliberate, not a
  gap. You hand-wire classes in the overridden `_wire` (`BaseApp` is an `ABC` and
  `_wire` is abstract; subclass `BaseApp[Factory]`, linear async, no yield); a
  dependency is just a constructor argument. The one thing
  the language doesn't give you free — lifecycle — is what the framework adds:
  open resources with `self._aenter` / `self._enter` (the app owns two exit
  stacks, closed in reverse at shutdown, even on partial failure), and a
  `BaseFactory` (stacks injected) groups construction. Past that there's nothing
  to "resolve." Per-request resources are an `async with` inside the handler.
  Do **not** add an injection/resolver system.
- **Background tasks**: `BackgroundTasks` is an in-process, fire-and-forget queue
  (not durable). Build it in `_wire` and open it with `_aenter` (it's an async CM —
  worker starts at startup, drains at shutdown); `register(handler)` infers the item
  type from the handler's one Struct param; endpoints `await tasks.add(item)`. One
  handler per type (`allow_one_to_many=True` to fan out); `drain_timeout: float | None`
  controls shutdown (float = drain best-effort then drop, None = drop now). Enter it
  *after* the resources its handlers use, so it drains before they're torn down.
- REST error semantics throughout (404/400/422/401/405, auto HEAD + OPTIONS);
  camelCase on the wire via msgspec `rename`.
- **Naming convention**: foundations you extend once are `Base*` (`BaseApp`,
  `BaseFactory`); the request vocabulary you implement many specific subclasses of
  stays plain (`Resource`, `Endpoint`).

## Layout

- `jero/core.py` — the framework (routing, binding, response senders, lifecycle).
  `jero/testing.py` — sync in-process `TestClient` + `FactoryHarness`.
  `jero/forms.py` / `jero/streaming.py` — multipart parts and streaming response
  types. `jero/background.py` — the in-process `BackgroundTasks` queue.
  `jero/headers.py` — the `RawHeaders` opaque bag. `jero/codecs.py` — the
  shared reusable `msgspec_encoder` / `msgspec_decoder` (imported by `core`,
  `streaming`, `testing`; SSE wire-formatting lives in `streaming.py` as the
  un-underscored boundary-crosser `encode_sse`).
- Runtime deps are intentionally sparse: `msgspec` for typed validation/JSON and
  `python-multipart` for buffered `multipart/form-data` parsing.
- `tests/` — pytest suite driven through `TestClient` against demo apps in
  `tests/demo_app.py`.
- `plans/` — design plans for not-yet-built features (e.g. `streaming.md`,
  `forms.md`, and `cookies.md` — fully designed, all decisions locked), staged for
  review before implementation.
- `bugs/` — one markdown note per **not-yet-fixed** bug, tracked in `bugs/README.md`
  (the manifest). **Only write a note for a bug you're leaving unfixed for later** —
  if you fix a bug in the same change, *don't* add a note; the regression test is the
  record. A fix isn't done until it has a regression test. **Never delete a bug note
  that already exists** — when its bug is fixed, flip its row to `Done` in the manifest
  and update the Open/Done counts rather than removing it.
- Demo apps and the competitor/benchmark harness live in a **separate repo**, not here.

## Status & sharp edges

- **Built**: routing + path-param templates, Resource/Endpoint, all binding sources
  (incl. typed `headers` and the opaque `raw_headers`), auth, REST semantics,
  response kinds — generic `JSONResponse[T, H]` / `BytesResponse[H]` / streaming
  `[T, H]` with typed response headers, `raw_headers`, and `status_code` overrides
  — `BaseApp`/`BaseFactory` lifecycle, in-process `BackgroundTasks`, `TestClient`,
  the test suite.
- **Performance (validated natively)**: on the authed write path
  (`POST /movies` — bearer auth + JSON decode + encode + 201, C=200), jero ≈
  blacksheep (~43k req/s, a tie), ~2× litestar, ~3× robyn, ~6× idiomatic FastAPI.
  Tight unimodal latency — trustworthy. (The benchmark harness lives in a
  separate repo; run natively rather than under emulation for real figures.)
- **Unbuilt**: cookies (first-class `Set-Cookie` / `Cookie` — fully designed, all
  decisions locked in `plans/cookies.md`), `Location` header on 201. Minor polish:
  the factory's `es`/`aes` stack injection matches by name with no startup check — a
  `WiringError` on an unrecognized param would close that.
- **Roadmap**: auto-generated **OpenAPI spec + live, hosted docs**. This is the
  reason every endpoint must be statically typed end to end — the schema is
  *derived from the types* (Struct sources, typed returns including generics like
  `NDJSONStreamingResponse[Movie]`), with no runtime guessing. Any feature that
  escapes static typing won't appear in the spec, so don't add one.
- **Untested**: no non-trivial real app has been built on it yet — that's where
  the opinions (pagination, streaming, cross-cutting concerns) get stress-tested.
