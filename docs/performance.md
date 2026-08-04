# Performance

jero is built for speed, but the only honest way to talk about speed is with numbers
and a clear account of how they were produced. This page is that account.

**The short version:** across four workloads benchmarked side by side against seven
other frameworks — Python (Blacksheep, Robyn, Litestar, FastAPI, Flask), Go (Gin), and
Bun (Elysia) — jero led the Python field in every scenario. jero was the fastest Python
framework on all four tests; the tail order was identical everywhere (litestar → fastapi
→ flask), with Robyn and Blacksheep trading the 2nd/3rd Python spots — Robyn ahead on the
authed-write and database paths, Blacksheep ahead on JSON and proxy. Go and Bun topped
the raw table. On the upstream-proxy scenario — with every Python framework issuing its
outbound call through the same Rust HTTP client — jero relayed responses within ~10% of
the Go service, at an equal p99. On the database scenario Go pulled well clear: there the
bottleneck is the database driver, not the framework, and that's a fight Python doesn't
win today.

Read the caveats. These are favourable, constrained conditions, and a microbenchmark is
not your application.

## At a glance

The whole field on all four workloads. Each panel is scaled to its own fastest
framework, so the bars show the ranking within that test; the labels keep the absolute
throughput. **jero is the fastest Python framework in every one** — gin (Go) and elysia
(Bun) are shown for context.

<p align="center">
  <img src="assets/bench-grid.svg" alt="Benchmark results: jero is the fastest Python framework across all four workloads" width="820">
</p>

And yes — benchmarks are genuinely hard to do right, and to do fairly. Every
framework has a configuration that flatters it, every harness makes choices that nudge
the numbers, and reasonable people disagree about what "fair" even means. This is *one*
benchmark, run one way, on one machine. The methodology and configuration are laid out
below so you can judge for yourself — but treat it as a single data point, not the last
word. If you have a workload that matters to you, the only number worth trusting is the
one you measure yourself.

## How the numbers were produced

The benchmark runs each framework **in isolation, one at a time**. Only one framework
server is up at any moment, alongside its own freshly-started dependencies — a Rust
upstream service (for the proxy scenario) and a fresh Postgres (for the database
scenario). Nothing else competes for the machine. This removes cross-framework
contention and shared-state effects, so each number reflects that framework alone.

- **Load generator:** [k6](https://k6.io/), a fixed virtual-user (VU) count hammering
  the service for a fixed duration.
- **Best-of-N:** every `(framework, scenario)` pair is run *N* times and the best run
  is kept. Repeating and taking the best beats down the ~3–4% run-to-run noise floor so
  the comparison reflects each framework's ceiling, not a noisy sample.
- **Single worker, one dedicated core:** every framework runs with one worker process,
  pinned to its own CPU core (cpuset affinity); Go is pinned to `GOMAXPROCS=1`. This is
  a like-for-like, single-core comparison — not a test of how well each scales across
  cores.
- **Identical scenarios** — the same request scripts, the same selection logic, and the
  same scoring table for every framework.
- **Identical outbound HTTP client** — every Python framework makes its upstream call
  (the proxy scenario) through the same Rust HTTP client,
  [pyreqwest](https://pypi.org/project/pyreqwest/), so that scenario compares
  frameworks rather than client libraries. (Flask, being sync, uses its `SyncClient`.)

### Run configuration

| Setting         | Value                                            |
| :-------------- | :----------------------------------------------- |
| Machine         | Apple M3 Max, 36 GB (Docker Desktop Linux VM)    |
| Concurrency     | 128 VUs                                          |
| Duration        | 30s per run                                      |
| Best-of-N       | 3 runs                                           |
| CPU             | 1 dedicated core per framework (cpuset affinity) |
| Workers         | 1 (Go pinned to `GOMAXPROCS=1`)                  |
| Python          | 3.13.14 (pinned image, every Python framework)   |
| Python server   | Granian + uvloop, single worker                  |
| Outbound client | pyreqwest (Rust) for every Python framework      |

## Results

`req/s` is throughput (higher is better); `mean` and `p99` are request latency (lower is
better). `vs jero` is throughput relative to jero. Every framework returned 100%
successful responses in every run, so that column is omitted. Frameworks are ordered
fastest → slowest within each scenario.

### 1 — `GET /info` — the pure framework path

Route → build a typed JSON response with a typed response header → encode. No I/O. This
isolates routing and serialization, and is the closest thing to a measure of the
framework's own per-request overhead.

| Framework      | req/s     | mean       | p99        | vs jero   |
| :------------- | :-------- | :--------- | :--------- | :-------- |
| gin *(Go)*     | 96.5k     | 1.24ms     | 6.44ms     | 1.41×     |
| elysia *(Bun)* | 88.1k     | 1.30ms     | 6.73ms     | 1.28×     |
| **jero**       | **68.5k** | **1.83ms** | **6.90ms** | **1.00×** |
| blacksheep     | 54.5k     | 2.31ms     | 7.51ms     | 0.80×     |
| robyn          | 45.3k     | 2.79ms     | 7.45ms     | 0.66×     |
| litestar       | 39.0k     | 3.26ms     | 8.38ms     | 0.57×     |
| fastapi        | 29.3k     | 4.35ms     | 9.90ms     | 0.43×     |
| flask          | 17.6k     | 7.25ms     | 13.61ms    | 0.26×     |

jero is the fastest Python framework on the pure framework path — 1.3× blacksheep,
1.5× Robyn, 1.8× litestar, 2.3× FastAPI — running at ~100% CPU, its genuine single-core
ceiling. Go and Bun lead outright, and both finished with CPU headroom to spare (~87%
and ~72% average), so the table understates them: read that gap as *at least* what it
shows.

### 2 — `POST /movies` — the authed write path (JWT)

Bearer/JWT auth → msgspec decode of the request body → handler → encode → `201`. The
realistic write path for a typed JSON API.

| Framework      | req/s     | mean       | p99         | vs jero   |
| :------------- | :-------- | :--------- | :---------- | :-------- |
| gin *(Go)*     | 60.2k     | 1.97ms     | 8.48ms      | 1.89×     |
| elysia *(Bun)* | 46.7k     | 2.65ms     | 9.74ms      | 1.47×     |
| **jero**       | **31.8k** | **3.99ms** | **10.69ms** | **1.00×** |
| robyn          | 21.9k     | 5.80ms     | 11.99ms     | 0.69×     |
| blacksheep     | 20.5k     | 6.23ms     | 13.79ms     | 0.64×     |
| litestar       | 14.5k     | 8.77ms     | 18.24ms     | 0.46×     |
| fastapi        | 10.1k     | 12.61ms    | 23.56ms     | 0.32×     |
| flask          | 9.5k      | 13.49ms    | 21.84ms     | 0.30×     |

This is jero's widest Python margin of the four scenarios: ~1.45× the next Python
framework (Robyn, which edges Blacksheep here), and over 3× FastAPI's.

### 3 — `GET` proxy — the outbound hop

The service makes an outbound HTTP call to the Rust upstream and relays the response.
Every Python framework issues that call through the same Rust HTTP client (pyreqwest),
so this scenario measures the framework around the client, not the client itself.

| Framework      | req/s     | mean       | p99         | vs jero   |
| :------------- | :-------- | :--------- | :---------- | :-------- |
| elysia *(Bun)* | 47.6k     | 2.62ms     | 9.13ms      | 1.52×     |
| gin *(Go)*     | 33.8k     | 3.74ms     | 10.03ms     | 1.08×     |
| **jero**       | **31.4k** | **4.05ms** | **10.23ms** | **1.00×** |
| blacksheep     | 25.8k     | 4.94ms     | 11.56ms     | 0.82×     |
| robyn          | 24.3k     | 5.24ms     | 10.67ms     | 0.77×     |
| litestar       | 20.1k     | 6.35ms     | 14.59ms     | 0.64×     |
| fastapi        | 16.0k     | 7.96ms     | 17.73ms     | 0.51×     |
| flask          | 6.1k      | 20.83ms    | 25.75ms     | 0.19×     |

jero proxies within ~10% of the Go service, at an equal ~10ms p99 — with the client
held constant, what remains of that gap is the framework, and there's ~10% of it.
elysia is the fastest proxy overall.

### 4 — `GET /users/me` — bound by the database driver

Reads a row from Postgres. Every request pays the database driver's cost, which
compresses the Python field and plays to Go's cheap native driver.

| Framework      | req/s     | mean       | p99         | vs jero   |
| :------------- | :-------- | :--------- | :---------- | :-------- |
| gin *(Go)*     | 36.3k     | 3.46ms     | 9.92ms      | 2.61×     |
| elysia *(Bun)* | 28.4k     | 4.46ms     | 11.19ms     | 2.05×     |
| **jero**       | **13.9k** | **9.18ms** | **15.71ms** | **1.00×** |
| robyn          | 12.4k     | 10.33ms    | 18.03ms     | 0.89×     |
| blacksheep     | 12.0k     | 10.63ms    | 17.92ms     | 0.86×     |
| litestar       | 9.4k      | 13.60ms    | 23.69ms     | 0.68×     |
| fastapi        | 7.8k      | 16.36ms    | 29.00ms     | 0.56×     |
| flask          | 3.3k      | 39.33ms    | 65.69ms     | 0.24×     |

jero stays the fastest Python framework, but the margin narrows to ~1.12× the next
Python framework (Robyn) — when the driver dominates the request, the framework matters
less. That is the honest ceiling on what a framework can do for a database-bound
service.

## How to read this

- **jero leads the Python frameworks tested in all four scenarios.** That is the
  durable claim — jero is #1 Python on every test, and the tail order is identical
  everywhere (litestar → fastapi → flask). Robyn and Blacksheep trade the 2nd/3rd Python
  spots between them (Robyn ahead on the authed-write and DB paths, Blacksheep on JSON
  and proxy).
- **Robyn debuts solidly mid-pack.** Its Rust-based server shows up most in footprint:
  the lowest Python memory of the field by a wide margin (~50–70M vs ~80–110M for the
  others). On throughput it lands behind jero on all four tests.
- **Go and Bun top the raw table.** On the pure framework path they even finished with
  CPU headroom to spare (the load generator, not the CPU, was their limit), while every
  Python framework ran at its genuine single-core ceiling. Python is not faster than Go
  — and this doesn't claim it is.
- **The proxy result is the framework, not the client.** With the same Rust HTTP client
  under every Python framework, jero relays upstream responses within ~10% of Go at an
  equal p99. Pick a pure-Python client instead and the client's ceiling swallows the
  whole Python field — the client library matters more than the framework on that path.
- **On the database path, the driver decides it.** Go's native driver is well ahead;
  jero stays ahead of the Python frameworks tested, which is the most it can do there.
- **A benchmark is not your app.** Single worker, one core, a Docker Desktop Linux VM
  (CPU pinning near-exact, not exact), fixed payloads, best-of-N. Real workloads have
  more moving parts. Treat these as directional evidence that jero's per-request
  overhead is low — not as a promise about your production numbers.

Where jero's design earns these numbers: all type introspection happens **once, at
startup**. The request path is dict lookup → msgspec decode → handler call → encode, and
nothing is ever added to it. See [the design philosophy](index.md) for why that's a
deliberate, non-negotiable bet.

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
| origin allow-list CORS | ~1.3× |
| on-scope `intercept` (falling through) | ~1.4× |
| `observe` | ~1.6× |
| `response_headers` method | ~1.9× |

For contrast, a single *onion-style* ASGI wrapper doing nothing but appending one
constant CORS pair measured ~1.3× on the same harness — for every request, whether or
not it applied, and before it does anything a real middleware does. That number is why
jero's middleware is a compiled protocol instead: one bare wrapper already costs what
the compiled model's mid tiers do, and the compiled model's floor is free.

## Measure it yourself

The numbers above need a load generator, a server, and patience. This one doesn't — and
it answers a *different question*. A single self-contained script drives each framework
**in-process as a bare ASGI callable** — no server, no sockets — so it isolates pure
framework overhead (routing, binding/validation, serialization) on your own machine in
under a minute.

It benchmarks jero, Litestar, BlackSheep, and FastAPI against a hand-rolled raw ASGI
app serving the same three-endpoint API. The raw app uses no framework but is kept
honest: it routes by hand, extracts the path and query values, does a typed validating
msgspec decode of the POST body, and a typed msgspec encode of every response. It
skips everything a framework gives you (404/405 semantics, HEAD/OPTIONS, content-type
checks, error envelopes) — that's the point: it is the **theoretical ceiling** the
frameworks are measured against.

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
[uv](https://docs.astral.sh/uv/) resolves them on the fly — nothing is installed into
your project, and it runs in a throwaway environment. There are two ways to run it.

**Fast path** — pipe it straight into uv, which reads the script from stdin, resolves
the inline dependencies, and runs it:

```bash
curl -LsSf https://raw.githubusercontent.com/RogerThomas/jero/main/benchmarks/micro_bench.py | uv run -
```

That executes a script fetched over the network — the full source is right below, so
read it first if you'd rather trust your own eyes. To run it locally instead, save it as
`micro_bench.py` and run `uv run micro_bench.py`. Either way, pin versions by editing the
`dependencies` block, and tweak the apps or scenarios as you see fit.

```python
--8<-- "benchmarks/micro_bench.py"
```
