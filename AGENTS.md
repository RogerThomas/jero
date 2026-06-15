# AGENTS.md

## jero

An opinionated, msgspec-first ASGI web framework (Python 3.14). The goal is a
framework that is **both** very fast **and** a joy to build on — achieved by
being aggressively opinionated rather than flexible.

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

- **`Resource`** — a class with CRUD methods: `create` / `read_one` / `read_many`
  / `update` / `partial_update` / `delete` → POST / GET(item) / GET(collection)
  / PUT / PATCH / DELETE.
- **`Endpoint`** — bare verbs (`get`/`post`/…) for non-resource routes (health,
  webhooks, actions). One path per Endpoint.
- Handler args bind **by name**, each a msgspec Struct: `json`, `content` (raw
  bytes, exclusive with `json`), `params` (query), `path` (URL template slots),
  `headers`, `user` (auth result). Return a Struct, `list[Struct]`, `bytes`, or a
  `BytesResponse`/`JSONResponse`/streaming response.
- **A JSON body is always a Struct — never a raw `dict`.** The
  `@api.get(...) → return {"a": 1}` idiom is gone: a `dict`/blob return is a
  `WiringError` at startup. JSON in and out is a typed Struct, every time — that's
  what gives it validation *and* a schema for the OpenAPI spec. No exceptions.
- **Auth**: an object with `authenticate(headers: Struct) -> UserStruct`; the
  user type is checked against handlers at startup.
- **Wiring / DI**: there is **no DI container** — and that's deliberate, not a
  gap. You hand-wire classes in `_wire` (subclass `BaseApp[Factory]`, linear
  async, no yield); a dependency is just a constructor argument. The one thing
  the language doesn't give you free — lifecycle — is what the framework adds:
  open resources with `self._aenter` / `self._enter` (the app owns two exit
  stacks, closed in reverse at shutdown, even on partial failure), and a
  `BaseFactory` (stacks injected) groups construction. Past that there's nothing
  to "resolve." Per-request resources are an `async with` inside the handler.
  Do **not** add an injection/resolver system.
- REST error semantics throughout (404/400/422/401/405, auto HEAD + OPTIONS);
  camelCase on the wire via msgspec `rename`.
- **Naming convention**: foundations you extend once are `Base*` (`BaseApp`,
  `BaseFactory`); the request vocabulary you implement many specific subclasses of
  stays plain (`Resource`, `Endpoint`).

## Layout

- `jero/core.py` — the framework. `jero/testing.py` — sync in-process `TestClient`
  + `FactoryHarness`. `jero/forms.py` / `jero/streaming.py` — form parts and
  streaming response types.
- Runtime deps are intentionally sparse: `msgspec` for typed validation/JSON and
  `python-multipart` for buffered `multipart/form-data` parsing.
- `tests/` — pytest suite driven through `TestClient` against demo apps in
  `tests/demo_app.py`.
- `plans/` — design plans for not-yet-built features (e.g. `streaming.md`,
  `forms.md`), staged for review before implementation.
- `bugs/` — one markdown note per bug, tracked in `bugs/README.md` (the manifest).
  **Never delete a fixed bug note** — flip its row to `Done` in the manifest and
  update the Open/Done counts. A fix isn't done until it has a regression test.
- Demo apps and the competitor/benchmark harness live in a **separate repo**, not here.

## Status & sharp edges

- **Built**: routing + path-param templates, Resource/Endpoint, all binding
  sources, auth, REST semantics, response kinds, `BaseApp`/`BaseFactory`
  lifecycle, `TestClient`, the test suite.
- **Performance (validated natively)**: on the authed write path
  (`POST /movies` — bearer auth + JSON decode + encode + 201, C=200), jero ≈
  blacksheep (~43k req/s, a tie), ~2× litestar, ~3× robyn, ~6× idiomatic FastAPI.
  Tight unimodal latency — trustworthy. (The benchmark harness lives in a
  separate repo; run natively rather than under emulation for real figures.)
- **Unbuilt**: custom status codes, `Location` header on 201. Minor polish: the
  factory's `es`/`aes` stack injection matches by name with no startup check — a
  `WiringError` on an unrecognized param would close that.
- **Roadmap**: auto-generated **OpenAPI spec + live, hosted docs**. This is the
  reason every endpoint must be statically typed end to end — the schema is
  *derived from the types* (Struct sources, typed returns including generics like
  `NDJSONStreamingResponse[Movie]`), with no runtime guessing. Any feature that
  escapes static typing won't appear in the spec, so don't add one.
- **Untested**: no non-trivial real app has been built on it yet — that's where
  the opinions (pagination, streaming, cross-cutting concerns) get stress-tested.
