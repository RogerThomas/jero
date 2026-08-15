# Performance / Benchmarks

jero is built for speed, but the only honest way to talk about speed is with numbers
and a clear account of how they were produced. This page is that account, and the
entire benchmark — every app's source, the harness, and the full methodology — is
public at [api-benchmarks](https://github.com/RogerThomas/api-benchmarks).

**The short version:** across four workloads benchmarked side by side against ten
other frameworks — Python (Django Bolt, Blacksheep, Robyn, Litestar, FastAPI, Flask,
Django Ninja), Go (Gin), Bun (Elysia), and Java (Spring Boot) — **jero is the fastest
Python ASGI framework in every scenario**, and the fastest Python framework outright
on the proxy and database paths. The two tests it doesn't lead the Python field on go
to Django Bolt, whose built-in Rust server handles routing and serialization outside
Python entirely — and which drops to last place among the established frameworks the
moment a request touches the database (more on that below). Go and Bun top the raw
table; Spring Boot lands mid-field at five to ten times everyone's memory.

Read the caveats. These are favourable, constrained conditions, and a microbenchmark is
not your application.

## At a glance

The whole field on all four workloads. Each panel is scaled to its own fastest
framework, so the bars show the ranking within that test; the labels keep the absolute
throughput. **jero is the fastest Python ASGI framework in every one** — Django Bolt
and Robyn ship their own Rust servers, and gin (Go), elysia (Bun), and spring-boot
(Java) are shown for cross-language context.

<p align="center">
  <img src="assets/bench-grid.svg" alt="Benchmark results: jero is the fastest Python ASGI framework across all four workloads" width="820">
</p>

## How the numbers were produced

The full account — every framework's app code, the compose files, the k6 scenarios,
and the methodology — lives in the
[api-benchmarks](https://github.com/RogerThomas/api-benchmarks) repo, and every run
writes a complete report (including per-container CPU and memory accounting) to its
`reports/` directory. The suite is written by jero's author; the code is public
precisely so you don't have to take fairness on trust — if a framework looks
shortchanged, open an issue there. The load-bearing points:

- **One framework alive at a time.** Each framework runs in its own compose stack
  alongside freshly started dependencies (Postgres, the Rust upstream, the k6 runner),
  torn down before the next framework starts. Nothing else competes for the machine, so
  each number reflects that framework alone.
- **Best-of-3 by composite score.** [k6](https://k6.io/) drives a fixed virtual-user
  count for a fixed duration; every `(framework, test)` pair runs three times and the
  attempt with the best composite score is kept — throughput, mean latency, and p99
  weighted equally, each normalized against that test's best attempt. Repeating and
  keeping the best beats down run-to-run noise so the comparison reflects each
  framework's ceiling, not a noisy sample.
- **One worker, one core.** Every service runs one worker (Gin at `GOMAXPROCS=1`,
  Django Bolt at `--processes 1`), pinned to a single dedicated CPU core via `cpuset`
  affinity — affinity rather than a CFS quota, because a quota throttles a saturated
  container every scheduler period and pollutes the tail. The pin confines *all* of a
  framework's threads, including GIL-releasing extensions like msgspec and the DB
  drivers. This is a like-for-like, single-core comparison — not a test of how well
  each scales across cores.
- **Equal budgets, equal work.** The DB pool and outbound-HTTP pool are capped at 64
  connections for every framework in every language. Every framework runs the same
  scenarios against the same scoring; every JWT endpoint hand-decodes the same bearer
  token with PyJWT (the two Django frameworks included, so nobody's auth stack does
  different work); and every Python framework makes its upstream call through the same
  Rust HTTP client, [pyreqwest](https://pypi.org/project/pyreqwest/). Beyond that,
  each framework uses its own idiomatic, recommended tools — and the real differences
  that creates (Django's ORM, FastAPI's response re-validation, typed vs passthrough
  upstream parsing) are documented in the repo as *known, intended differences* rather
  than normalised away.

### Run configuration

| Setting         | Value                                                             |
| :-------------- | :---------------------------------------------------------------- |
| Machine         | Amazon EC2 `c9g.2xlarge`, eu-central-1                            |
| Machine specs   | AWS Graviton5 (arm64), 8 vCPUs — physical cores, no SMT — 16 GiB   |
| Concurrency     | 128 VUs                                                           |
| Duration        | 60s per attempt                                                   |
| Best-of-N       | 3 attempts, best composite score kept                             |
| CPU             | 1 dedicated core per framework (cpuset affinity)                  |
| Workers         | 1 (Gin at `GOMAXPROCS=1`, Django Bolt at `--processes 1`)         |
| Python          | 3.13 (pinned image, every Python framework)                       |
| Python server   | granian + uvloop (Flask via granian WSGI; Robyn and Django Bolt ship their own Rust servers) |
| Outbound client | pyreqwest (Rust) for every Python framework                       |
| Pools           | 64 connections per framework — database and outbound HTTP alike   |

## Results

`req/s` is throughput (higher is better); `mean` and `p99` are request latency (lower is
better). `vs jero` is throughput relative to jero. Every framework returned 100%
successful responses in every run, so that column is omitted. Frameworks are ordered
fastest → slowest within each scenario.

### 1 — `GET /info` — the pure framework path

Route → build a typed JSON response with a custom response header → encode. No I/O.
This isolates routing and serialization, and is the closest thing to a measure of the
framework's own per-request overhead.

| Framework           | req/s     | mean       | p99         | vs jero   |
| :------------------ | :-------- | :--------- | :---------- | :-------- |
| gin *(Go)*          | 55.7k     | 2.20ms     | 9.75ms      | 1.18×     |
| django-bolt         | 55.1k     | 2.24ms     | 10.57ms     | 1.17×     |
| elysia *(Bun)*      | 55.0k     | 2.23ms     | 9.91ms      | 1.17×     |
| **jero**            | **47.1k** | **2.66ms** | **11.61ms** | **1.00×** |
| blacksheep          | 42.6k     | 2.96ms     | 11.32ms     | 0.90×     |
| robyn               | 37.8k     | 3.32ms     | 12.25ms     | 0.80×     |
| spring-boot *(Java)*| 33.1k     | 3.80ms     | 12.13ms     | 0.70×     |
| litestar            | 33.0k     | 3.84ms     | 12.25ms     | 0.70×     |
| fastapi             | 25.2k     | 5.06ms     | 13.35ms     | 0.53×     |
| flask               | 19.5k     | 6.53ms     | 15.90ms     | 0.41×     |
| django-ninja        | 2.8k      | 45.21ms    | 73.35ms     | 0.06×     |

jero is the fastest Python ASGI framework on the pure framework path — 1.1×
Blacksheep, 1.4× Litestar, 1.9× FastAPI — running at ~90% CPU, close to its genuine
single-core ceiling. The ~55k club above it is instructive: Gin (Go), Django Bolt
(Django on its own Rust server, so routing and serialization never enter Python), and
Elysia (Bun) — and Elysia got there at just 44% CPU, so read its number as *at least*
what the table shows.

### 2 — `POST /movies` — the authed write path (JWT)

Bearer/JWT auth → decode and validate a five-field request body → handler → encode →
`201`. The realistic write path for a typed JSON API.

| Framework           | req/s     | mean       | p99         | vs jero   |
| :------------------ | :-------- | :--------- | :---------- | :-------- |
| gin *(Go)*          | 44.0k     | 2.72ms     | 12.02ms     | 1.89×     |
| elysia *(Bun)*      | 41.2k     | 3.00ms     | 12.80ms     | 1.77×     |
| django-bolt         | 38.5k     | 3.21ms     | 13.03ms     | 1.65×     |
| **jero**            | **23.3k** | **5.42ms** | **17.02ms** | **1.00×** |
| spring-boot *(Java)*| 22.4k     | 5.65ms     | 15.18ms     | 0.96×     |
| robyn               | 20.3k     | 6.25ms     | 16.40ms     | 0.87×     |
| blacksheep          | 17.0k     | 7.49ms     | 17.29ms     | 0.73×     |
| litestar            | 13.6k     | 9.37ms     | 19.78ms     | 0.58×     |
| flask               | 11.0k     | 11.59ms    | 20.72ms     | 0.47×     |
| fastapi             | 9.7k      | 13.20ms    | 25.40ms     | 0.41×     |
| django-ninja        | 2.5k      | 50.97ms    | 79.71ms     | 0.11×     |

jero leads the ASGI field by its widest margin of the four — 1.4× Blacksheep, 1.7×
Litestar, 2.4× FastAPI, which drops below even sync Flask here under the weight of
re-validating its own response against the `response_model`. Django Bolt's Rust core
takes the Python crown outright, and Spring Boot lands within 4% of jero — while
holding ~610M of memory to jero's ~90M.

### 3 — `GET /catalog` — the outbound hop

The service makes an outbound HTTP call to the Rust upstream (authenticated with a
bearer API key) and returns the payload. Every Python framework issues that call
through the same Rust HTTP client (pyreqwest), so this scenario measures the framework
around the client, not the client itself.

| Framework           | req/s     | mean       | p99         | vs jero   |
| :------------------ | :-------- | :--------- | :---------- | :-------- |
| elysia *(Bun)*      | 43.5k     | 2.85ms     | 11.82ms     | 1.67×     |
| gin *(Go)*          | 31.3k     | 4.00ms     | 12.78ms     | 1.20×     |
| **jero**            | **26.1k** | **4.86ms** | **13.81ms** | **1.00×** |
| django-bolt         | 21.6k     | 5.88ms     | 14.64ms     | 0.83×     |
| blacksheep          | 21.5k     | 5.92ms     | 14.90ms     | 0.82×     |
| robyn               | 18.8k     | 6.79ms     | 15.30ms     | 0.72×     |
| litestar            | 17.6k     | 7.22ms     | 17.59ms     | 0.68×     |
| spring-boot *(Java)*| 17.1k     | 7.45ms     | 14.19ms     | 0.66×     |
| fastapi             | 13.6k     | 9.41ms     | 20.67ms     | 0.52×     |
| flask               | 9.8k      | 13.06ms    | 20.95ms     | 0.37×     |
| django-ninja        | 3.1k      | 40.85ms    | 69.07ms     | 0.12×     |

jero is the fastest Python framework outright on the outbound hop, within ~20% of the
Go service — and it gets there while decoding the upstream payload into a typed model,
where Django Bolt, Blacksheep, and Elysia relay the raw bytes straight through. With
the client held constant across the Python field, what separates these rows is the
framework. Elysia is the fastest proxy overall.

### 4 — `GET /users/me` — bound by the database driver

Bearer/JWT auth, then a row read from Postgres. Every request pays the database
driver's cost, which compresses the field and plays to Go's cheap native driver.

| Framework           | req/s     | mean        | p99         | vs jero   |
| :------------------ | :-------- | :---------- | :---------- | :-------- |
| gin *(Go)*          | 34.9k     | 3.55ms      | 13.05ms     | 2.94×     |
| elysia *(Bun)*      | 27.6k     | 4.56ms      | 13.96ms     | 2.33×     |
| spring-boot *(Java)*| 17.9k     | 7.11ms      | 15.61ms     | 1.51×     |
| **jero**            | **11.9k** | **10.75ms** | **19.38ms** | **1.00×** |
| blacksheep          | 10.5k     | 12.18ms     | 21.62ms     | 0.88×     |
| robyn               | 10.0k     | 12.81ms     | 22.38ms     | 0.84×     |
| litestar            | 8.5k      | 15.00ms     | 26.86ms     | 0.72×     |
| fastapi             | 6.9k      | 18.58ms     | 33.29ms     | 0.58×     |
| flask               | 6.0k      | 21.19ms     | 39.63ms     | 0.51×     |
| django-bolt         | 3.6k      | 35.36ms     | 59.82ms     | 0.30×     |
| django-ninja        | 1.5k      | 85.16ms     | 126.36ms    | 0.13×     |

jero is the fastest Python framework, at a narrowed margin — 1.1× Blacksheep — because
when the driver dominates the request, the framework matters less. The compiled
languages pull clear on their native drivers; that is the honest ceiling on what any
Python framework can do for a database-bound service.

The eyebrow-raiser is Django Bolt: from winning the JSON and write tests outright to
0.30× jero here — a **fifteen-fold swing** between its best and worst scenarios, by far
the widest in the field. The cause is the benchmark's own idiomatic-code rule. Every
framework uses its recommended data-access path, and for anything Django-based that
means the Django ORM. Bolt's handler is fully async (`await ...aget(...)`), but
Django's async ORM is an async *API* over the synchronous psycopg driver — each query
hops to a worker thread — where the rest of the Python field rides psqlpy, a natively
async Rust driver. On the three tests the ORM never touches, Bolt's Rust core wins or
places; the moment a request reaches the database, every request pays that bridge, and
on one pinned core it costs Bolt its entire advantage. Read Bolt's row here as a
measurement of Django's data layer, not of Bolt's HTTP machinery — and it stays in the
table because swapping in a raw driver would stop being idiomatic Django.

## How to read this

- **jero is the fastest Python ASGI framework in all four scenarios, and the fastest
  Python framework outright on the proxy and database paths.** That is the durable
  claim. Against the frameworks that share its architecture — Blacksheep, Litestar,
  FastAPI on the same granian server — jero leads every test, by 1.1× to 2.4×.
- **Django Bolt is the newcomer worth understanding, not dismissing.** Its built-in
  Rust server takes the JSON and authed-write tests ahead of every Python framework
  including jero — and then the database test shows the flip side (see scenario 4): a
  framework is its whole stack, and Bolt's Django half doesn't move at Rust speed.
- **Django Ninja's numbers are about Django's request machinery, not Ninja's binding.**
  An order of magnitude below the field on every test — even with an empty middleware
  stack and the same hand-rolled auth as everyone else — because every request runs the
  full Django request cycle on a single core. Bolt, which bypasses that machinery
  entirely, is the controlled experiment proving where the cost lives.
- **Go, Bun, and Java are context, not competition.** Gin tops three of four tests and
  Elysia owns the proxy; Spring Boot runs mid-field — beating jero on the database test,
  trailing it elsewhere — while holding ~595–650M of memory against the Python field's
  ~50–145M and Gin's ~15–30M. Python is not faster than Go, and this page doesn't claim
  it is.
- **The proxy result is the framework, not the client.** With the same Rust HTTP client
  under every Python framework, jero relays a *typed, validated* upstream payload
  within ~20% of the Go service. Pick a pure-Python client instead and the client's
  ceiling swallows the whole Python field — the client library matters more than the
  framework on that path.
- **On the database path, the driver decides it.** Go's native driver is well ahead;
  jero stays ahead of every Python framework, which is the most it can do there.
- **A benchmark is not your app.** Single worker, one core, fixed payloads, best-of-N
  on an idle EC2 host. Real workloads have more moving parts. Treat these as
  directional evidence that jero's per-request overhead is low — not as a promise about
  your production numbers. For a workload that matters to you, the only number worth
  trusting is the one you measure yourself.

Where jero's design earns these numbers: all type introspection happens **once, at
startup**. The request path is dict lookup → msgspec decode → handler call → encode, and
nothing is ever added to it. See [Philosophy](philosophy.md) for the reasoning.

## What middleware and CORS cost

[Middleware](guide/middleware.md) and [CORS](guide/cors.md) are compiled at wiring
rather than layered as app wrappers, so their cost is per *tier*, opt-in, and zero on
routes nothing covers. Measured with the in-process hot-path harness (`bench.py`'s
POST-echo shape, ~1.3µs/request baseline; in-process numbers amplify framework deltas
relative to a real server, where socket I/O dominates):

| configuration | relative cost |
| :-- | :-- |
| no middleware, no CORS | 1.00× |
| wildcard CORS / a constant `response_headers` block | ~1.02× |
| origin allow-list CORS | ~1.15× |
| on-scope `intercept` (falling through) | ~1.4× |
| `observe` | ~1.6× |
| `response_headers` method | ~1.9× |

For contrast, a single *onion-style* ASGI wrapper doing nothing but appending one
constant CORS pair measured ~1.3× on the same harness — for every request, whether or
not it applied, and before it does anything a real middleware does. That number is why
jero's middleware is a compiled protocol instead: one bare wrapper already costs what
the compiled model's mid tiers do, and the compiled model's floor is free.

## Measure it yourself

A single self-contained script drives jero, Litestar, BlackSheep, and FastAPI
**in-process as bare ASGI callables** — no server, no sockets — isolating pure
framework overhead (routing, binding/validation, serialization) on your own machine in
under a minute. Each is measured against a hand-rolled raw ASGI app serving the same
three-endpoint API: no framework, but kept honest (hand routing, typed validating
msgspec decode, typed encode), while skipping everything a framework gives you
(404/405 semantics, HEAD/OPTIONS, error envelopes). That raw app is the **theoretical
ceiling** the frameworks are measured against.

!!! note "This is a different test from the four scenarios above"

    The tables above measure **throughput you'd actually get** — a real server, real
    sockets, 128 concurrent clients. This script measures **how close each framework gets
    to the theoretical maximum** for the work itself. The hand-rolled `raw` row *is* that
    maximum — identical routing, decode, and encode with no framework at all — so read each
    framework's distance from `raw` as the price of what it adds for you.

    Because there's no server in the loop, the fixed per-request cost (socket, ASGI server,
    event loop) that compresses the networked numbers disappears — so the gaps here run
    **far wider** than any `vs jero` column above (jero lands close to `raw`; the others
    trail it by more). Read this as a relative ordering-and-headroom check, **not** a re-run
    of the throughput tables.

The script declares its dependencies inline (PEP 723), so
[uv](https://docs.astral.sh/uv/) runs it in a throwaway environment — nothing is
installed into your project:

```bash
curl -LsSf https://raw.githubusercontent.com/RogerThomas/jero/main/benchmarks/micro_bench.py | uv run -
```

That executes a script fetched over the network — the full source is right below, so
read it first if you'd rather. To run it locally, save it as `micro_bench.py` and
`uv run micro_bench.py`; pin versions or tweak the scenarios by editing it.

```python
--8<-- "benchmarks/micro_bench.py"
```
