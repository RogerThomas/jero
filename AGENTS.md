# AGENTS.md

## jero

An opinionated, msgspec-first ASGI web framework (Python 3.13+). The goal is a
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
   the `WiringError`s) and the source of the OpenAPI spec (`jero/openapi.py`).
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
  "obviously fine" cases. Sometimes though, it may be neccessary or pragmatic to
  ignore a rule for a single line, so don't always avoid this. Use pragmatically, but sparingly.
- For framework-level / design changes, **discuss the design first** — don't just
  implement. Give options + a recommendation, then build once decided.
- **Keep this file current.** After any relatively sizable change — a new feature,
  a changed convention, a dependency or supported-version bump, a repo-layout move —
  update `AGENTS.md` in the same change so it never drifts from how the project
  actually works.
- **Testing stance:** tests run only through the public boundary — `TestClient`
  against the `demo_app/` package (the single source of truth; some tests build
  small local apps for focused cases). **Do not unit-test `jero/` internals directly**;
  they're covered transitively. This is deliberate (style-guide rule 7, and it
  lets the internals be refactored freely — which they are, often). Don't "fix"
  the absence of internal tests. Revisit only once the internals stabilize
  (approaching a stable release), and even then only a thin layer for the
  intricate *pure* helpers (`_parse_template`/`_route_segments`, the
  `__orig_bases__` factory-type extraction, SSE/NDJSON formatting).

## The contract (how apps are built)

- **The operation set is closed and fixed** — this is load-bearing. A `Resource`'s
  operations are *exactly* `create` / `read_one` / `read_many` / `update_full` /
  `update_partial` / `delete`; an `Endpoint`'s are *exactly* `get` / `post` / `put` /
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
  path="/widgets")`, read once at wiring; `include_resource(Widgets())` takes no
  `path=`. The class is the single source of truth for its path (what URL reversal /
  `Link` / `Location` and the OpenAPI spec read). Optional OpenAPI metadata rides the
  same class kwargs: `meta` (all operations) and `meta_<op>` per operation, typed
  `EndpointMeta` / `ResourceMeta` / `OperationMeta` (the wrong meta type on a shape is a
  loud failure; `operation_id` lives only on `OperationMeta`; `summary`/`description`/
  `responses` refine the generated spec). `path` is **required** on
  the concrete shapes — omitting it is a pyright error, not just a startup `WiringError`.
  An optional `ref="..."` gives the class a string handle for URL reversal across import
  cycles (see Links/Location below).
- **Links & Location** — responses carry `location: Location | None` (RFC 9110, one
  `Location` header) and `links: Sequence[Link]` (RFC 8288, joined into one `Link`
  header), reverse-routed to a mounted operation. Build a target with
  `from_operation(Class.op, path=...)` (the blessed, typed form — the wrong `path`
  Struct is caught **at construction** by introspecting the operation's own `path`
  annotation), `from_path(path)` / `from_url(url)` (a literal root-relative path composed
  with the URL base, or a verbatim full URL), or `from_ref("ref.op", path=...)` (string
  escape hatch for import cycles; weaker guarantees — checked at resolution). URLs are
  relative unless one of two env vars is set (read once at app construction — env is
  available before the factory, so it sidesteps the settings-only-in-the-factory ordering
  problem): `JERO_BASE_URL` (a static public origin, no header trust) or
  `JERO_TRUST_FORWARDED` (truthy → rebuild the origin per request from `X-Forwarded-*`
  proto/host/port + the stripped `X-Forwarded-Prefix`; off by default since honouring
  forwarded headers untrusted is a host-injection footgun). The two are mutually exclusive
  (both set → `WiringError`); operation / ref / `from_path` links pick up the base while
  `from_url` links pass through verbatim. jero reads `os.environ` only here. The
  `Location`/`Link` types live in `jero/links.py`; reversal resolves against a wiring-time
  registry (`_Reverser`) in `core`. The `*Target` types are un-underscored
  package-internal boundary-crossers (like `encode_sse`), not public API.
- Handler args bind **by name**, each a msgspec Struct: `json`, `content` (raw
  bytes), `form` (multipart) — the three body sources are mutually exclusive —
  `params` (query), `path` (URL template slots), `headers` (typed), `raw_headers`
  (opaque `RawHeaders` bag), `user` (auth result). Return a Struct, `list[Struct]`,
  `bytes`, or a response wrapper to control headers/status: `JSONResponse[T, H]` /
  `BytesResponse[H]` / a streaming response (`NDJSONStreamingResponse[T, H]`, …).
  A wrapper's `T` may also be a **union of tagged Structs** (mixed streams — chunks
  plus a footer, say): runtime encodes each member with its tag, and the OpenAPI
  schema is msgspec's `anyOf` + `discriminator`. Tags are strictly required only when
  the spec is built (untagged unions stream fine at runtime) but are recommended
  regardless — the tag is the client's discriminator. Untagged multi-Struct unions
  (once `include_openapi` is wired) and any other `T` shape fail loud at startup —
  never a silent `{}` schema.
- **Response headers & status**: the wrappers carry a typed `headers` Struct (the
  header *type* is a parameter `H`; field names inverse-mangle `x_trace_id` →
  `x-trace-id`, scalars stringify, Structs JSON-encode, None fields omit), a
  `raw_headers` escape hatch (exotic names, casing, repeats — e.g. `Set-Cookie`),
  and a `status_code` override (else the verb's default). The buffered wrappers are
  `@dataclass` (like the streaming ones), generic over body `T` and headers `H` so
  both schemas survive to the OpenAPI spec — a bare `JSONResponse` (no `[T]`) is a
  pyright-strict error on purpose.
- **Errors**: `BaseHTTPError` is the abstract root — the HTTP contract (class-level
  `status`, validated 400–599 at class creation) without a wire body; `except
  BaseHTTPError` means "any jero error". Two families sit on it. The **Problem family**
  (`HTTPError` and friends) renders typed RFC 9457 Problem Details and is the blessed
  default. The built-ins use a short kebab-case machine code for `type` rather than the
  RFC 9457 URI — a convention, not a rule: any non-blank string is accepted (a URI
  works too). The **Struct family** (`StructHTTPError[B]`) is a generic engine over a
  user body Struct: class options declare how *every* field of `B` is fed — `consts=`
  (pinned; wire value + schema enum const), `templates=` (rendered at raise time from
  the params; `{{brace}}` escaping ships literal braces), `status_field=` (an existing
  int field fed the class status), `params_field=` (a Struct field the params nest
  into) — and
  leftovers are same-named raise-time params. Total coverage validated loud at class
  creation; the wire model (consts narrowed to `Literal`s) is composed once there.
  Raise sites: kwargs (runtime-checked) or the blessed `@dataclass` tier whose declared
  fields ARE the params (generated, statically-typed `__init__`; validated on first
  raise). Nothing user-passed is ever mutated. An
  **`ErrorBodyAdapter[B]`** (`include_error_adapter`, at most one) replaces the Problem
  family's rendering app-wide (house error formats) — framework built-ins and
  handler-translated errors included; `StructHTTPError`s render themselves; an adapter
  crash is contained (logged, Problem body sent). Define a
  static error as an `HTTPError` subclass with class-level `type` / `title` / `status`
  and optional `docs`; use `DataclassHTTPError[Params]` plus `detail_template` when an
  occurrence has runtime values. Parameterized errors always emit both human-only
  `detail` and machine-readable typed `params`; static errors emit neither. Uncaught
  exceptions become the generic `internal-server-error` problem. Always raise an
  exception instance (`raise WidgetNotFoundError()`), never the exception class.
- **Custom exception handlers**: hand-wire a structurally typed object with
  `handle_exception(exception: E) -> ErrorResponse1 | ErrorResponse2 | None` via
  `add_exception_handler`; no base class or decorator. Returning `None` continues
  default handling (`HTTPError` serializes itself, anything else becomes the generic
  500); returning a declared `HTTPError` sends its Problem Details, while returning
  `ExceptionResponse` sends its required per-occurrence `status_code`, typed JSON
  Struct, and optional typed/raw headers and links. Signatures are compiled from every
  concrete `HTTPError` / `ExceptionResponse[T, H]` return-union member
  at wiring, nearest-MRO registration wins, and registering the same exception type
  twice is a `WiringError`. Its status must be 400–599; a handler failure becomes the
  generic 500 without recursively invoking handlers.
- **A JSON body is always a Struct — never a raw `dict`.** The
  `@api.get(...) → return {"a": 1}` idiom is gone: a `dict`/blob return is a
  `WiringError` at startup. JSON in and out is a typed Struct, every time — that's
  what gives it validation *and* a schema for the OpenAPI spec. No exceptions.
- **Auth**: an object with `authenticate(headers: Struct) -> UserStruct`; the
  user type is checked against handlers at startup. **The authenticator's declared return
  type is the route's auth policy**: `-> UserStruct` gates, while `-> UserStruct | None`
  makes credentials an *input* — returning `None` reports **absent** credentials and the
  handler runs with `user=None`, while **present but invalid** ones still raise (401).
  Absence is the authenticator's call, never a heuristic; note `authenticate` only sees what
  its headers Struct can bind, so the credential field needs a `| None` default for absence
  to reach it. Handlers must match (`user: UserStruct | None` against the second), checked
  both directions at startup — **and a handler that omits `user` is a `WiringError` on an
  anonymous-accepting route** (nothing would record that the route is open; behind a gating
  authenticator omitting it stays fine). An app needing both policies defines two
  authenticators over one shared lookup (`TokenAuth` / `OptionalTokenAuth` in `demo_app`),
  so a route's policy is visible in what its mount passes. `AuthMode` (`"required" |
  "optional" | None`) carries this on the `OperationSpec`; the spec emits
  `[{scheme: []}, {}]` for an anonymous-accepting operation and keeps its derived 401.
- **Wiring / DI**: there is **no DI container** — and that's deliberate, not a
  gap. You hand-wire classes in the overridden `wire` (`BaseApp` is an `ABC` and
  `wire` is abstract; subclass `BaseApp[Factory]`, linear async, no yield); a
  dependency is just a constructor argument. The one thing
  the language doesn't give you free — lifecycle — is what the framework adds:
  open resources with `self.aenter` / `self.enter` (the app owns two exit
  stacks, closed in reverse at shutdown, even on partial failure), and a
  `BaseFactory` (stacks injected) groups construction. Standalone (scripts, cron,
  notebooks): `async with Factory.open() as factory:` — a classmethod async CM that
  owns the stack pair and unwinds on exit; `FactoryHarness` is the sync-test bridge
  reimplemented on top of it (one lifecycle code path). Past that there's nothing
  to "resolve." Per-request resources are an `async with` inside the handler.
  Do **not** add an injection/resolver system.
  - **Naming**: the extension surface is intentionally **public** (`wire`,
    `include_resource`, `include_endpoint`, `include_openapi`, `add_exception_handler`,
    `enter`, `aenter`, `factory`). Technically
    these are private (only called from inside a subclass), but a leading `_` reads as
    "keep out" for the API users are meant to use, so they're public. Underscore is
    reserved for genuine internals (`_include`, `_register`, `_make_factory`, the
    exit-stack fields). Do **not** re-underscore the extension surface.
- **Background tasks**: `BackgroundTasks` is an in-process, fire-and-forget queue
  (not durable). Build it in `wire` and open it with `aenter` (it's an async CM —
  worker starts at startup, drains at shutdown); `register(handler)` infers the item
  type from the handler's one Struct param; endpoints `await tasks.add(item)`. One
  handler per type (`allow_one_to_many=True` to fan out); `drain_timeout: float | None`
  controls shutdown (float = drain best-effort then drop, None = drop now). Enter it
  *after* the resources its handlers use, so it drains before they're torn down.
- REST error semantics throughout (404/400/422/401/405, auto HEAD + OPTIONS);
  camelCase on the wire via msgspec `rename`.
- **Naming convention**: foundations you extend once are `Base*` (`BaseApp`,
  `BaseFactory`); the request vocabulary you implement many specific subclasses of
  stays plain (`Resource`, `Endpoint`). **Acronyms are upper-cased in class/type names**,
  not title-cased — `JSONResponse`, `URLTarget`, `SSEResponse`, `NDJSONStreamingResponse`
  (never `JsonResponse` / `UrlTarget`). Method and field names stay lowercase
  (`from_url`, `raw_headers`).

## Layout

- `jero/core.py` — the framework (routing, binding, response senders, lifecycle).
  `jero/_wiring_types.py` — the resolved wiring contracts (`Sources`, `FormSpec`,
  `OperationSpec`, the `*Meta` types, `is_struct_type`/`strip_list`), all msgspec Structs; a leaf depending
  only on msgspec + `jero.openapi`. `jero/openapi.py` — the dependency-free OpenAPI 3.1
  builder (`SecurityScheme`, `ResponseSpec`, `ModelMeta`, `build_openapi`).
  `jero/structs.py` — `Struct`, jero's drop-in `msgspec.Struct` base whose `meta=` class
  keyword (handled by a `msgspec.StructMeta` subclass) attaches a `ModelMeta` as
  `__model_meta__`; the builder reads it to set a model's schema description. `jero/_openapi_wiring.py` —
  the translation layer (`operation_input`) turning an `OperationSpec` into the builder's
  inputs. The graph is a strict DAG: `core` and `_openapi_wiring` both import the contracts
  from `_wiring_types`; `core` imports `_openapi_wiring`; nothing imports `core` back (the
  shared contracts sit *below* both, which is what keeps it acyclic — no lazy imports).
  `jero/testing.py` — sync in-process `TestClient` + `FactoryHarness`.
  `jero/forms.py` / `jero/streaming.py` — multipart parts and streaming response
  types. `jero/background.py` — the in-process `BackgroundTasks` queue.
  `jero/links.py` — `Location` / `Link` and their reverse-routing targets (a leaf module
  `core` and `streaming` both import). `jero/headers.py` — the `RawHeaders` opaque bag.
  `jero/errors.py` — typed Problem Details Structs, `HTTPError` foundations, and the
  framework's fixed error types.
  `jero/codecs.py` — the
  shared reusable `msgspec_encoder` / `msgspec_decoder` (imported by `core`,
  `streaming`, `testing`; SSE wire-formatting lives in `streaming.py` as the
  un-underscored boundary-crosser `encode_sse`).
- Runtime deps are intentionally sparse: `msgspec` for typed validation/JSON and
  `python-multipart` for buffered `multipart/form-data` parsing.
- `demo_app/` — a complete, project-structured example app (`config`, `models`,
  `auth`, `services/`, `operations/`, `factory`, `app`); its `TokenAuth` gates widgets +
  `/me` and its `OptionalTokenAuth` serves anonymous callers at `/spotlight`. It is the **single source of
  truth**: the worked example in the docs, the app the test suite runs against, and a
  typed consumer of the public API that every type checker validates. Keep it working
  and bounded (it demonstrates the shape; resist turning it into a feature dumping ground).
- `tests/` — pytest suite driven through `TestClient` against `demo_app/` (plus small
  local apps for focused cases).
- `plans/` — design plans for not-yet-built work, fully designed with decisions locked,
  staged for review before implementation. Currently `middleware.md`, `websockets.md`, and
  `private-extension-surface.md` (the `_`/`__` naming rule for the extension surface).
- `bugs/` — one markdown note per **not-yet-fixed** bug, tracked in `bugs/README.md`
  (the manifest). **Only write a note for a bug you're leaving unfixed for later** —
  if you fix a bug in the same change, *don't* add a note; the regression test is the
  record. A fix isn't done until it has a regression test. **Never delete a bug note
  that already exists** — when its bug is fixed, flip its row to `Done` in the manifest
  and update the Open/Done counts rather than removing it.
- The competitor/benchmark harness lives in a **separate repo**, not here. (The
  framework's own example app, `demo_app/`, *is* in this repo — see above.)

## Status & sharp edges

- **Built**: routing + path-param templates, Resource/Endpoint, all binding sources
  (incl. typed `headers` and the opaque `raw_headers`), auth (required *and* optional),
  REST semantics,
  response kinds — generic `JSONResponse[T, H]` / `BytesResponse[H]` / streaming
  `[T, H]` with typed response headers, `raw_headers`, and `status_code` overrides;
  `NoContent[H]` / `Created[T, H]` / `Accepted[T, H]` (204/201/202 regardless of the
  verb's own default); a **union return** (`Widget | NoContent`) lets a handler answer
  with different, statically-typed success statuses, each documented as its own OpenAPI
  response entry. Any non-streaming return kind may be a union member, plain
  `Struct`/`list[Struct]`/`bytes` included — a member's status is the one it would have
  alone, so no wrapper is needed just to join a union. Members **may** share a status:
  OpenAPI keys one response there, so they merge into it (bodies as one `anyOf`, header
  maps unioned — response headers carry no `required`, so merging asserts nothing new),
  which makes `Widget | Other` and `JSONResponse[Widget | Other]` produce the identical
  document. Members that encode differently share a status too — `content` is keyed by
  media type, so they sit side by side (the OpenAPI shape for `Accept` negotiation, which
  is what such a handler is doing). A shared status is rejected only when the merge can't
  be *said*: header Structs disagreeing on a wire name, or a member with no single Struct
  body (bare wrapper / `list`). Those are document questions, so they fire under
  `include_openapi`, like the streaming item-type checks — `BaseApp`/`BaseFactory`
  lifecycle, in-process `BackgroundTasks`, reverse-routed
  `Location` / `Link` responses, typed Problem Details errors, structurally registered
  custom exception handlers, `TestClient`, the test suite. **OpenAPI 3.1**:
  `include_openapi` serves `/openapi.json` + a Scalar `/docs` UI, derived from the
  wired types (sources, returns incl. generics, `msgspec.Meta`); `favicon=` (Path read
  once at wiring → precomputed `/favicon.ico` route + `<link>` in the default page;
  str → URL linked verbatim; bad file/suffix is a `WiringError`; the route is never
  documented). **Docstrings are never
  published** — public prose is explicit: `OperationMeta` (summary/description), `ModelMeta`
  via `jero.Struct`'s `meta=` class keyword (model description, attached through a
  `msgspec.StructMeta` subclass), field `Meta`. `SecurityScheme` / `BearerAuth` /
  `BasicAuth` for security; derived per-source error responses reference the shared
  `Problem` schema (or, with an `ErrorBodyAdapter` registered, the adapter's per-status
  body). **Declared exceptions**: `exceptions=[ErrorClass, ...]` on
  `OperationMeta`/`EndpointMeta`/`ResourceMeta` derives error responses entirely from
  the error class — status, description (`title` / `description`), and body schema
  (per-class Problem model with `type`/`status` consts + params; the Struct family's
  wire model). Class-level entries extend the operation's; same-status entries merge as
  a `oneOf`; precedence per status is derived < declared exceptions < explicit
  `ResponseSpec`; non-error entries are a wiring failure.
- **Performance (validated, 2026-07-16 run)**: fastest Python framework on all four
  scenarios (VUS=128, 1 dedicated core, granian+uvloop); Python order is jero →
  blacksheep → litestar → fastapi → flask everywhere. Authed write path
  (`POST /movies`): jero 31.5k req/s ≈ 1.5× blacksheep, ~2.4× litestar, ~3.4× FastAPI.
  Proxy path (all Python frameworks on pyreqwest): within ~10% of Go/gin at equal p99.
  (The benchmark harness lives in a separate repo.)
- **Unbuilt**: cookies (first-class `Set-Cookie` / `Cookie` — fully designed, all
  decisions locked in `plans/cookies.md`); absolute (vs relative) reverse-routed URLs are
  a deliberate follow-up. Minor polish:
  the factory's `es`/`aes` stack injection matches by name with no startup check — a
  `WiringError` on an unrecognized param would close that.
- **The OpenAPI spec is *derived from the types*** (Struct sources, typed returns
  including generics like `NDJSONStreamingResponse[Movie]`), with no runtime guessing.
  This is the reason every endpoint must be statically typed end to end — any feature
  that escapes static typing won't appear in the spec, so don't add one. The builder
  (`jero/openapi.py`) takes resolved operations and never imports `core`; the translation
  lives in `jero/_openapi_wiring.py` (`operation_input`). Keep that boundary. Forms can't
  be schema'd whole — schema-ing a form model yields the jero-side envelope
  (`FormPart.data`/`content_type`/`headers`/`raw_headers`, `bytes` as base64, `RawHeaders`
  internals) instead of the multipart wire shape. So `_form_fields` captures each field's
  *resolved payload type* (with its `Meta`), the builder routes the non-binary ones through
  the shared `schema_components` pass (so constraints/description/examples and struct
  `$ref`s come out right) and emits `{type: string, format: binary}` for files/raw bytes.
  Note this is why `_payload_kind` strips `Annotated` to classify while keeping the
  annotated type for both `convert` (constraint enforcement) and the schema.
- **Untested**: no non-trivial real app has been built on it yet — that's where
  the opinions (pagination, streaming, cross-cutting concerns) get stress-tested.
