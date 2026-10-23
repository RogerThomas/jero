# Performance / Benchmarks

jero is built for speed, but the only honest way to talk about speed is with numbers
and a clear account of how they were produced. This page is that account, and the
entire benchmark (every app's source, the harness, and the full methodology) is public
at [api-benchmarks](https://github.com/RogerThomas/api-benchmarks).

**TL;DR:** across four workloads benchmarked side by side against nine other frameworks
(Python: Django Bolt, Blacksheep, Litestar, Robyn, FastAPI, Flask; Go: Gin; Bun:
Elysia; Java: Spring Boot), **jero is the fastest Python framework on three of the
four, and ties Django Bolt (within 0.1%) on the fourth.** It is the fastest Python ASGI
framework on all four, by 1.4× to 4.5× over Blacksheep, Litestar, and FastAPI. jero
also out-runs Go's Gin on three of the four, and on the database path it is the
fastest framework of any language in the field. Bun's Elysia tops the pure framework
and proxy tests; Spring Boot lands mid-field at roughly ten times everyone's resident
memory (a JVM-defaults figure; see the caveats).

!!! warning "Read the caveats"

    These are favourable, constrained conditions, and a microbenchmark is not your
    application.

## At a glance

The whole field on all four workloads. Each panel is scaled to its own fastest
framework, so the bars show the ranking within that test; the labels keep the absolute
throughput. Django Bolt and Robyn ship their own Rust servers, and gin (Go), elysia
(Bun), and spring-boot (Java) are shown for cross-language context.

<p align="center">
  <img src="assets/bench-grid.svg" alt="Benchmark results: jero is the fastest Python ASGI framework across all four workloads" width="820">
</p>

## How the numbers were produced

The full account (every framework's app code, the compose files, the load-test
scenarios, and the methodology) lives in the
[api-benchmarks](https://github.com/RogerThomas/api-benchmarks) repo, and every run
writes a complete report (including per-container CPU and memory accounting) to its
`reports/` directory. The suite is written by jero's author, as are three of the
libraries every Python framework shares in it ([lothc](https://pypi.org/project/lothc/),
[fastpg](https://pypi.org/project/fastpg/), [ryjwt](https://pypi.org/project/ryjwt/));
the code is public precisely so you don't have to take fairness on trust. If a
framework looks shortchanged, open an issue there. The load-bearing points:

- **One framework per machine.** Each framework gets its own EC2 spot instance (all
  the same instance type, under one run id), where it runs in its own compose stack
  alongside freshly started dependencies: Postgres, the Rust upstream, and the load
  generator. Nothing else competes for the machine, so each number reflects that
  framework alone. Repeat runs on different instances agree within ~1–2%.
- **Correctness first, then best-of-3.** Before any load, 100 sequential requests are
  checked for status *and* key body fields; one wrong response reports the whole test
  as 0% success, so a fast-but-wrong answer can never score. Then
  [wrk](https://github.com/wg/wrk) holds 128 connections open in a closed loop: an
  untimed 3s warm-up, then three 60s attempts, keeping the one with the highest
  throughput. Repeating and keeping the best beats down run-to-run noise so the
  comparison reflects each framework's ceiling, not a noisy sample.
- **One worker, one core.** Every service runs one worker (Gin at `GOMAXPROCS=1`,
  Django Bolt at `--processes 1`), pinned to a single dedicated CPU core via `cpuset`
  affinity rather than a CFS quota, because a quota throttles a saturated container
  every scheduler period and pollutes the tail. The pin confines *all* of a framework's
  threads, including GIL-releasing extensions like msgspec and the DB driver. Postgres,
  the upstream, and wrk each get their own cores, so the framework, not the load
  generator, is what saturates. This is a like-for-like, single-core comparison, not a
  test of how well each scales across cores.
- **Equal budgets, equal work.** The DB pool and outbound-HTTP pool are capped at 64
  connections for every framework in every language. Every framework runs the same
  scenarios against the same scoring; every Python JWT endpoint hand-decodes the same
  bearer token with ryjwt into a typed claims model; every Python framework makes its
  upstream call through the same HTTP client, lothc (a typed client over the Rust-based
  [pyreqwest](https://pypi.org/project/pyreqwest/)), with one shared configuration; and
  the whole async Python fleet (Django Bolt deliberately included, bypassing Django's
  ORM) reads Postgres through the same Rust-based driver, fastpg, so the database test
  measures the framework rather than a data layer. Beyond that, each framework uses
  its own idiomatic, recommended tools, and the real differences that creates
  (FastAPI's and Django Bolt's response re-validation, typed vs passthrough upstream
  parsing, serializer choices) are documented in the repo as *known, intended
  differences* rather than normalised away.

### Run configuration

| Setting         | Value                                                             |
| :-------------- | :---------------------------------------------------------------- |
| Machine         | Amazon EC2 `c9g.2xlarge` spot, eu-central-1, one per framework    |
| Machine specs   | AWS Graviton5 (arm64), 8 vCPUs (physical cores, no SMT), 16 GiB   |
| Load generator  | wrk 4.2.0, 2 threads on 2 dedicated cores                         |
| Concurrency     | 128 connections, closed loop                                      |
| Duration        | 60s per attempt, after an untimed 3s warm-up                      |
| Best-of-N       | 3 attempts, highest throughput kept                               |
| CPU             | 1 dedicated core per framework (cpuset affinity)                  |
| Workers         | 1 (Gin at `GOMAXPROCS=1`, Django Bolt at `--processes 1`)         |
| Python          | 3.14.9 (uv's standalone CPython, every Python framework)          |
| Python server   | [peregrine](https://pypi.org/project/peregrine-server/) 1.2.0 + uvloop (Flask via peregrine WSGI; Robyn and Django Bolt ship their own Rust servers) |
| Outbound client | lothc 0.2.0 (over pyreqwest) for every Python framework           |
| Database driver | fastpg for the async Python fleet; psycopg for sync Flask         |
| Pools           | 64 connections per framework, database and outbound HTTP alike    |
| Runs            | Python frameworks: `20261023T113907`; Gin, Elysia, Spring Boot: `20261023T091453` |

The Python field was rerun on its own (in `20261023T113907`) to pick up lothc 0.2.0;
the three cross-language rows, marked †, come from the full-fleet run earlier the same
day, on the same harness and settings, with their code unchanged in between.

## Results

`req/s` is throughput (higher is better); `mean` and `p99` are request latency (lower is
better). `vs jero` is throughput relative to jero. Every framework returned 100%
successful responses in every run, so that column is omitted. Frameworks are ordered
fastest → slowest within each scenario. † marks a row from run `20261023T091453`.

Read the latency columns as latency *at saturation*: 128 connections in a closed loop
drive every framework to its single-core ceiling, which is what throughput measures,
but it is not a model of realistic load, and closed-loop tails run optimistic.

### 1. `GET /info`: the pure framework path

Route → build a typed JSON response with a custom response header → encode. No I/O.
This isolates routing and serialization, and is the closest thing to a measure of the
framework's own per-request overhead.

| Framework              | req/s      | mean       | p99        | vs jero   |
| :--------------------- | :--------- | :--------- | :--------- | :-------- |
| elysia *(Bun)* †       | 161.3k     | 0.79ms     | 1.43ms     | 1.61×     |
| **jero**               | **100.4k** | **1.27ms** | **1.31ms** | **1.00×** |
| django-bolt            | 95.7k      | 1.33ms     | 1.36ms     | 0.95×     |
| gin *(Go)* †           | 87.6k      | 1.47ms     | 3.33ms     | 0.87×     |
| blacksheep             | 73.6k      | 1.73ms     | 1.78ms     | 0.73×     |
| litestar               | 48.9k      | 2.60ms     | 2.66ms     | 0.49×     |
| robyn                  | 43.4k      | 2.95ms     | 3.08ms     | 0.43×     |
| spring-boot *(Java)* † | 41.3k      | 3.09ms     | 4.88ms     | 0.41×     |
| fastapi                | 30.6k      | 4.17ms     | 4.22ms     | 0.30×     |
| flask                  | 27.3k      | 4.69ms     | 4.80ms     | 0.27×     |

jero is the fastest Python framework here, ahead of Django Bolt's Rust server by 5%
and of Gin by 15%. Within the ASGI field it leads by 1.4× Blacksheep, 2× Litestar,
3.3× FastAPI. Only Elysia, whose Bun runtime does this in native code, is faster.
Every framework in the table ran its core at ~100% CPU, so these are genuine
single-core ceilings rather than load-generator limits.

### 2. `POST /movies`: the authed write path (JWT)

Bearer/JWT auth → decode and validate a five-field request body → handler → encode →
`201`. The realistic write path for a typed JSON API.

| Framework              | req/s      | mean       | p99        | vs jero   |
| :--------------------- | :--------- | :--------- | :--------- | :-------- |
| django-bolt            | 83.1k      | 1.54ms     | 1.57ms     | 1.00×     |
| **jero**               | **83.0k**  | **1.53ms** | **1.59ms** | **1.00×** |
| elysia *(Bun)* †       | 54.6k      | 2.34ms     | 4.19ms     | 0.66×     |
| gin *(Go)* †           | 53.6k      | 2.39ms     | 5.10ms     | 0.65×     |
| blacksheep             | 33.1k      | 3.85ms     | 3.91ms     | 0.40×     |
| litestar               | 29.9k      | 4.27ms     | 4.34ms     | 0.36×     |
| spring-boot *(Java)* † | 26.6k      | 4.82ms     | 7.35ms     | 0.32×     |
| robyn                  | 26.4k      | 4.83ms     | 4.93ms     | 0.32×     |
| flask                  | 20.2k      | 6.34ms     | 6.48ms     | 0.24×     |
| fastapi                | 18.5k      | 6.89ms     | 6.96ms     | 0.22×     |

jero and Django Bolt tie, 0.1% apart, and both run ~1.5× faster than Elysia and Gin.
jero leads the ASGI field by its widest margin of the four: 2.5× Blacksheep, 2.8×
Litestar, and 4.5× FastAPI, which drops below even sync Flask here under the weight of
re-validating its own response against the `response_model`.

### 3. `GET /catalog`: the outbound hop

The service makes an outbound HTTP call to the Rust upstream (authenticated with a
bearer API key), decodes the payload into a typed model, and returns it. Every Python
framework issues that call through the same HTTP client (lothc, over the Rust-based
pyreqwest) with the same configuration, so this scenario measures the framework around
the client, not the client itself.

| Framework              | req/s      | mean       | p99        | vs jero   |
| :--------------------- | :--------- | :--------- | :--------- | :-------- |
| elysia *(Bun)* †       | 56.7k      | 2.25ms     | 3.71ms     | 1.87×     |
| gin *(Go)* †           | 32.0k      | 3.99ms     | 7.30ms     | 1.06×     |
| **jero**               | **30.3k**  | **4.23ms** | **7.19ms** | **1.00×** |
| django-bolt            | 23.4k      | 5.52ms     | 13.09ms    | 0.77×     |
| blacksheep             | 20.7k      | 6.20ms     | 11.61ms    | 0.68×     |
| spring-boot *(Java)* † | 20.4k      | 6.25ms     | 9.64ms     | 0.68×     |
| litestar               | 18.2k      | 7.06ms     | 17.32ms    | 0.60×     |
| robyn                  | 17.9k      | 7.15ms     | 10.89ms    | 0.59×     |
| fastapi                | 12.2k      | 10.45ms    | 20.04ms    | 0.40×     |
| flask                  | 11.2k      | 11.35ms    | 12.05ms    | 0.37×     |

jero is the fastest Python framework outright on the outbound hop, 1.3× Django Bolt,
and within 6% of the Go service at a slightly lower p99. With the client held constant
across the Python field, what separates these rows is the framework. Elysia, which
returns the upstream payload parsed but unvalidated, is the fastest proxy overall.

### 4. `GET /users/me`: the database read

Bearer/JWT auth, then a row read from Postgres by the token's subject. Every request
pays the database driver's cost. The whole async Python fleet (Django Bolt included,
by the suite's documented exception to per-framework idiom) reads through the same
Rust-based driver (fastpg), so these rows compare frameworks, not data layers.

| Framework              | req/s      | mean       | p99        | vs jero   |
| :--------------------- | :--------- | :--------- | :--------- | :-------- |
| **jero**               | **46.9k**  | **2.72ms** | **3.26ms** | **1.00×** |
| elysia *(Bun)* †       | 37.2k      | 3.44ms     | 6.60ms     | 0.79×     |
| gin *(Go)* †           | 37.0k      | 3.48ms     | 7.79ms     | 0.79×     |
| django-bolt            | 36.4k      | 3.51ms     | 4.18ms     | 0.78×     |
| blacksheep             | 28.7k      | 4.47ms     | 6.71ms     | 0.61×     |
| robyn                  | 22.4k      | 5.71ms     | 8.62ms     | 0.48×     |
| litestar               | 21.9k      | 5.87ms     | 10.37ms    | 0.47×     |
| spring-boot *(Java)* † | 20.8k      | 6.15ms     | 9.72ms     | 0.44×     |
| fastapi                | 14.0k      | 9.11ms     | 18.28ms    | 0.30×     |
| flask                  | 8.8k       | 14.53ms    | 16.05ms    | 0.19×     |

jero is the fastest framework in the field here, 1.26× Elysia and Gin on their native
drivers and 1.6× Blacksheep. A fast driver doesn't make the framework irrelevant: with
fastpg under the whole async Python fleet, the spread from jero to FastAPI is still
3.3×.

## How to read this

- **jero is the fastest Python framework on three of four workloads and ties Django
  Bolt on the fourth.** That is the durable claim. Against the frameworks that share
  its architecture (Blacksheep, Litestar, and FastAPI on the same peregrine server),
  jero leads every test, by 1.4× to 4.5×.
- **jero out-runs Go here, under these conditions.** It beats Gin on three of the four
  tests and trails it by 6% on the proxy. Read that as "a compiled single-core
  baseline is no longer the ceiling for a Python framework on this workload shape",
  not as "Python is faster than Go": Gin at `GOMAXPROCS=1` is deliberately held to one
  core, and real Go services scale across cores in ways a single-worker Python process
  does not.
- **Django Bolt is the closest Python rival.** Its built-in Rust server does routing
  and serialization outside Python, which ties it with jero on the authed write and
  keeps it within 5% on JSON. On the proxy and database paths, where the request's time
  is spent beyond the framework, jero leads it by ~1.3×. A Rust core accelerates
  exactly the slice of the request it covers.
- **Bun and Java are context, not competition.** Elysia takes the pure framework and
  proxy tests outright. Spring Boot runs mid-field while holding ~615–685M of resident
  memory against the Python field's ~40–107M and Gin's ~10–29M. Read the JVM number
  with care: under default heap sizing the JVM commits memory up front and rarely
  returns it, so much of that footprint is reserved heap rather than working set, and
  a tuned `-Xmx` would show far less. It is the honest cost of idiomatic defaults, not
  Spring Boot's floor.
- **The proxy result is the framework, not the client.** With the same HTTP client
  under every Python framework, jero relays a *typed, validated* upstream payload
  within 6% of the Go service. Pick a pure-Python client instead and the client's
  ceiling swallows the whole Python field: the client library matters more than the
  framework on that path.
- **Two runs, one harness.** The Python rows and the cross-language rows (†) come from
  two runs on the same day with the same settings. Repeat runs agree within ~1–2%, so
  the comparison holds, but the † rows were not measured alongside the rest.
- **A benchmark is not your app.** Single worker, one core, fixed payloads, best-of-N
  on an idle EC2 host, latency measured at saturation. Real workloads have more moving
  parts. Treat these as directional evidence that jero's per-request overhead is low,
  not as a promise about your production numbers. For a workload that matters to you,
  the only number worth trusting is the one you measure yourself.

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
constant CORS pair measured ~1.3× on the same harness: paid on every request, whether
or not it applied, and before it does anything a real middleware does. That number is
why jero's middleware is a compiled protocol instead: one bare wrapper already costs
what the compiled model's mid tiers do, and the compiled model's floor is free.

## Measure it yourself

A single self-contained script drives jero, Litestar, BlackSheep, and FastAPI
**in-process as bare ASGI callables** (no server, no sockets), isolating pure
framework overhead (routing, binding/validation, serialization) on your own machine in
under a minute. Each is measured against a hand-rolled raw ASGI app serving the same
three-endpoint API: no framework, but kept honest (hand routing, typed validating
msgspec decode, typed encode), while skipping everything a framework gives you
(404/405 semantics, HEAD/OPTIONS, error envelopes). That raw app is the **theoretical
ceiling** the frameworks are measured against.

!!! warning "This is a different test from the four scenarios above"

    The tables above measure **throughput you'd actually get**: a real server, real
    sockets, 128 concurrent connections. This script measures **how close each framework gets
    to the theoretical maximum** for the work itself. The hand-rolled `raw` row *is* that
    maximum (identical routing, decode, and encode with no framework at all), so read each
    framework's distance from `raw` as the price of what it adds for you.

    Because there's no server in the loop, the fixed per-request cost (socket, ASGI server,
    event loop) that compresses the networked numbers disappears, so the gaps here run
    **far wider** than any `vs jero` column above (jero lands close to `raw`; the others
    trail it by more). Read this as a relative ordering-and-headroom check, **not** a re-run
    of the throughput tables.

The script declares its dependencies inline (PEP 723), so
[uv](https://docs.astral.sh/uv/) runs it in a throwaway environment; nothing is
installed into your project:

```bash
curl -LsSf https://raw.githubusercontent.com/RogerThomas/jero/main/benchmarks/micro_bench.py | uv run -
```

That executes a script fetched over the network; the full source is right below, so
read it first if you'd rather. To run it locally, save it as `micro_bench.py` and
`uv run micro_bench.py`; pin versions or tweak the scenarios by editing it.

```python
--8<-- "benchmarks/micro_bench.py"
```
