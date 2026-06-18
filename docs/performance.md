# Performance

jero is built for speed, but the only honest way to talk about speed is with numbers
and a clear account of how they were produced. This page is that account.

**The short version:** across four workloads benchmarked side by side against seven
other frameworks — Python (Litestar, FastAPI, Blacksheep, Robyn, Flask), Go (Gin), and
Bun (Elysia) — jero is the fastest Python framework in every scenario. On the pure
framework hot path (a typed JSON `GET`) it tops the table outright, ahead of both the
Go and the Bun service. On the I/O-bound scenarios (an upstream proxy, a database read)
Go pulls well clear — there the bottleneck is the HTTP-client and database-driver
ecosystem, not the framework, and that's a fight Python doesn't win today.

Read the caveats. These are favourable, constrained conditions, and a microbenchmark is
not your application.

And yes — we know benchmarks are genuinely hard to do right and to do fairly. Every
framework has a configuration that flatters it, every harness makes choices that nudge
the numbers, and reasonable people disagree about what "fair" even means. This is *one*
benchmark, run one way, on one machine. We've tried to be even-handed and we show
exactly how it was produced below so you can judge for yourself — but please treat it as
a single data point, not the last word. If you have a workload that matters to you, the
only number worth trusting is the one you measure yourself.

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
- **Single worker, single core:** every framework runs with one worker process; Go is
  pinned to `GOMAXPROCS=1`. This is a like-for-like, single-core comparison — not a
  test of how well each scales across cores.
- **Identical scenarios** — the same request scripts, the same selection logic, and the
  same scoring table for every framework.

### Run configuration

| Setting       | Value                           |
| :------------ | :------------------------------ |
| Machine       | Apple M3 Max, 36 GB             |
| Concurrency   | 125 VUs                         |
| Duration      | 30s per run                     |
| Workers       | 1 (Go pinned to `GOMAXPROCS=1`) |
| Python server | Granian, single worker          |

## Results

`req/s` is throughput (higher is better); `mean` and `p99` are request latency (lower is
better). `vs all` is an aggregate score across all three — a single "overall standing"
number, normalised so jero = `1.00×` in every scenario. Every framework returned 100%
successful responses in every run, so that column is omitted. Frameworks are ordered by
`vs all` within each scenario.

### 1 — `GET /info` — the pure framework path

Route → build a typed JSON response with a typed response header → encode. No I/O. This
isolates routing and serialization, and is the closest thing to a measure of the
framework's own per-request overhead.

| Framework      | req/s     | mean       | p99        | vs all    |
| :------------- | :-------- | :--------- | :--------- | :-------- |
| **jero**       | **43.4k** | **2.84ms** | **4.09ms** | **1.00×** |
| blacksheep     | 39.7k     | 3.11ms     | 4.15ms     | 0.94×     |
| elysia *(Bun)* | 38.6k     | 3.19ms     | 4.15ms     | 0.92×     |
| gin *(Go)*     | 39.2k     | 3.15ms     | 4.52ms     | 0.90×     |
| litestar       | 33.8k     | 3.65ms     | 4.90ms     | 0.80×     |
| fastapi        | 25.7k     | 4.81ms     | 5.31ms     | 0.65×     |
| robyn          | 21.4k     | 5.79ms     | 29.67ms    | 0.32×     |
| flask          | 16.1k     | 7.67ms     | 130.99ms   | 0.16×     |

### 2 — `POST /movies` — the authed write path (JWT)

Bearer/JWT auth → msgspec decode of the request body → handler → encode → `201`. The
realistic write path for a typed JSON API.

| Framework      | req/s     | mean       | p99        | vs all    |
| :------------- | :-------- | :--------- | :--------- | :-------- |
| gin *(Go)*     | 29.1k     | 4.24ms     | 7.07ms     | 1.00×     |
| **jero**       | **28.6k** | **4.30ms** | **6.86ms** | **1.00×** |
| elysia *(Bun)* | 25.3k     | 4.87ms     | 10.63ms    | 0.80×     |
| blacksheep     | 17.3k     | 7.14ms     | 27.69ms    | 0.45×     |
| robyn          | 16.5k     | 7.50ms     | 42.14ms    | 0.38×     |
| fastapi        | 9.3k      | 13.29ms    | 25.35ms    | 0.31×     |
| litestar       | 12.5k     | 9.86ms     | 67.69ms    | 0.27×     |
| flask          | 8.1k      | 15.25ms    | 137.68ms   | 0.16×     |

jero lands within ~2% of a hand-written Go service here, and is the fastest Python
framework by a wide margin.

### 3 — `GET` proxy — bound by the HTTP client

The service makes an outbound HTTP call to the Rust upstream and relays the response.
The bottleneck is the HTTP client library, not the framework — which is why the whole
Python field clusters together and Go runs away.

| Framework      | req/s    | mean        | p99         | vs all    |
| :------------- | :------- | :---------- | :---------- | :-------- |
| gin *(Go)*     | 15.6k    | 7.94ms      | 35.03ms     | 3.69×     |
| elysia *(Bun)* | 9.4k     | 13.11ms     | 19.70ms     | 3.20×     |
| **jero**       | **3.6k** | **34.69ms** | **91.88ms** | **1.00×** |
| blacksheep     | 3.2k     | 39.00ms     | 137.17ms    | 0.81×     |
| fastapi        | 3.0k     | 41.21ms     | 149.69ms    | 0.76×     |
| litestar       | 3.0k     | 41.44ms     | 149.90ms    | 0.75×     |
| robyn          | 2.6k     | 47.94ms     | 137.16ms    | 0.71×     |
| flask          | 2.1k     | 58.70ms     | 800.63ms    | 0.34×     |

jero is the fastest Python framework, but Go's mature native HTTP stack is in a
different class. This gap is the ecosystem, not jero.

### 4 — `GET /users/me` — bound by the database driver

Reads a row from Postgres. The bottleneck is the database driver, so again the field
compresses and Go's native driver leads.

| Framework      | req/s    | mean        | p99         | vs all    |
| :------------- | :------- | :---------- | :---------- | :-------- |
| gin *(Go)*     | 16.8k    | 7.38ms      | 11.41ms     | 2.92×     |
| elysia *(Bun)* | 12.2k    | 10.17ms     | 17.67ms     | 2.04×     |
| **jero**       | **9.5k** | **13.03ms** | **90.81ms** | **1.00×** |
| fastapi        | 6.4k     | 19.33ms     | 44.04ms     | 0.98×     |
| blacksheep     | 8.5k     | 14.64ms     | 118.91ms    | 0.85×     |
| litestar       | 7.0k     | 17.67ms     | 140.70ms    | 0.71×     |
| robyn          | 4.8k     | 25.81ms     | 112.22ms    | 0.59×     |
| flask          | 2.2k     | 56.92ms     | 167.05ms    | 0.31×     |

Fastest Python again, behind the Go and Bun services on driver-bound work.

## How to read this

- **jero leads the Python field in all four scenarios.** That is the durable claim.
- **On the pure framework path it beats even Go and Bun.** That result is real but
  narrow: an in-memory JSON path plays directly to Python + msgspec's strengths and to
  the Rust HTTP layer underneath. It is *not* evidence that Python is faster than Go in
  general — and we are not making that claim.
- **On I/O-bound paths, Go is well ahead.** When the work is an outbound HTTP call or a
  database query, the framework is barely in the picture; the HTTP client and database
  driver decide it, and Go's native libraries dominate. jero stays the fastest Python
  option, which is the most it can do there.
- **A benchmark is not your app.** Single worker, single core, localhost, fixed
  payloads, best-of-N. Real workloads have more moving parts. Treat these as directional
  evidence that jero's per-request overhead is low — not as a promise about your
  production numbers.

Where jero's design earns these numbers: all type introspection happens **once, at
startup**. The request path is dict lookup → msgspec decode → handler call → encode, and
nothing is ever added to it. See [the design philosophy](index.md) for why that's a
deliberate, non-negotiable bet.
