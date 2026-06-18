# Performance

jero is built for speed, but the only honest way to talk about speed is with numbers
and a clear account of how they were produced. This page is that account.

**The short version:** across four workloads benchmarked side by side against seven
other frameworks — Python (Litestar, FastAPI, Blacksheep, Robyn, Flask), Go (Gin), and
Bun (Elysia) — jero led the Python frameworks tested in every scenario. On the pure
framework hot path (a typed JSON `GET`) it topped this benchmark table by overall
score, ahead of both the Go and the Bun service. On the I/O-bound scenarios (an
upstream proxy, a database read) Go pulled well clear — there the bottleneck is the
HTTP-client and database-driver ecosystem, not the framework, and that's a fight Python
doesn't win today.

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
| Concurrency   | 100 VUs                         |
| Duration      | 5s per run                      |
| Best-of-N     | 3 runs                          |
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
| **jero**       | **44.5k** | **2.22ms** | **3.73ms** | **1.00×** |
| blacksheep     | 40.3k     | 2.45ms     | 3.36ms     | 0.97×     |
| elysia *(Bun)* | 38.7k     | 2.55ms     | 3.52ms     | 0.93×     |
| gin *(Go)*     | 38.4k     | 2.57ms     | 3.79ms     | 0.90×     |
| litestar       | 35.6k     | 2.78ms     | 3.99ms     | 0.84×     |
| fastapi        | 24.5k     | 4.06ms     | 4.81ms     | 0.62×     |
| robyn          | 20.6k     | 4.83ms     | 10.46ms    | 0.42×     |
| flask          | 17.9k     | 5.56ms     | 19.29ms    | 0.31×     |

### 2 — `POST /movies` — the authed write path (JWT)

Bearer/JWT auth → msgspec decode of the request body → handler → encode → `201`. The
realistic write path for a typed JSON API.

| Framework      | req/s     | mean       | p99        | vs all    |
| :------------- | :-------- | :--------- | :--------- | :-------- |
| gin *(Go)*     | 28.6k     | 3.46ms     | 6.39ms     | 1.06×     |
| **jero**       | **27.4k** | **3.62ms** | **6.93ms** | **1.00×** |
| elysia *(Bun)* | 24.0k     | 4.12ms     | 8.20ms     | 0.87×     |
| blacksheep     | 16.4k     | 6.05ms     | 14.58ms    | 0.55×     |
| robyn          | 15.7k     | 6.21ms     | 18.04ms    | 0.50×     |
| litestar       | 12.0k     | 8.25ms     | 22.52ms    | 0.39×     |
| flask          | 10.5k     | 9.46ms     | 48.39ms    | 0.28×     |
| fastapi        | 5.2k      | 18.97ms    | 55.64ms    | 0.17×     |

jero lands within ~5% of a hand-written Go service here, and led the Python frameworks
tested by a wide margin.

### 3 — `GET` proxy — bound by the HTTP client

The service makes an outbound HTTP call to the Rust upstream and relays the response.
The bottleneck is the HTTP client library, not the framework — which is why the whole
Python field clusters together and Go runs away.

| Framework      | req/s    | mean        | p99         | vs all    |
| :------------- | :------- | :---------- | :---------- | :-------- |
| gin *(Go)*     | 15.1k    | 6.58ms      | 15.34ms     | 5.35×     |
| elysia *(Bun)* | 11.2k    | 8.77ms      | 21.11ms     | 3.96×     |
| **jero**       | **3.2k** | **31.56ms** | **102.24ms**| **1.00×** |
| litestar       | 2.8k     | 35.17ms     | 127.50ms    | 0.86×     |
| blacksheep     | 2.9k     | 33.85ms     | 158.69ms    | 0.82×     |
| fastapi        | 2.4k     | 42.21ms     | 102.92ms    | 0.82×     |
| robyn          | 2.5k     | 40.37ms     | 167.62ms    | 0.72×     |
| flask          | 2.4k     | 41.94ms     | 166.82ms    | 0.70×     |

jero led the Python frameworks tested, but Go's mature native HTTP stack is in a
different class. This gap is the ecosystem, not jero.

### 4 — `GET /users/me` — bound by the database driver

Reads a row from Postgres. The bottleneck is the database driver, so again the field
compresses and Go's native driver leads.

| Framework      | req/s    | mean        | p99         | vs all    |
| :------------- | :------- | :---------- | :---------- | :-------- |
| gin *(Go)*     | 16.2k    | 6.13ms      | 8.72ms      | 2.44×     |
| elysia *(Bun)* | 6.0k     | 16.45ms     | 16.77ms     | 1.02×     |
| **jero**       | **8.4k** | **11.84ms** | **33.98ms** | **1.00×** |
| blacksheep     | 7.8k     | 12.84ms     | 89.58ms     | 0.69×     |
| litestar       | 6.3k     | 15.77ms     | 141.70ms    | 0.51×     |
| robyn          | 4.6k     | 21.87ms     | 172.95ms    | 0.39×     |
| fastapi        | 3.4k     | 29.26ms     | 104.84ms    | 0.38×     |
| flask          | 1.3k     | 78.03ms     | 210.18ms    | 0.15×     |

jero led the Python frameworks tested again. Go was well ahead; Bun's lower p99 edged
jero on aggregate score despite lower throughput and higher mean latency.

## How to read this

- **jero leads the Python frameworks tested in all four scenarios.** That is the
  durable claim.
- **On the pure framework path it beats even Go and Bun.** That result is real but
  narrow: an in-memory JSON path plays directly to Python + msgspec's strengths and to
  the Rust HTTP layer underneath. It is *not* evidence that Python is faster than Go in
  general — and we are not making that claim.
- **On I/O-bound paths, Go is well ahead.** When the work is an outbound HTTP call or a
  database query, the framework is barely in the picture; the HTTP client and database
  driver decide it, and Go's native libraries dominate. jero stays ahead of the Python
  frameworks tested, which is the most it can do there.
- **A benchmark is not your app.** Single worker, single core, localhost, fixed
  payloads, best-of-N. Real workloads have more moving parts. Treat these as directional
  evidence that jero's per-request overhead is low — not as a promise about your
  production numbers.

Where jero's design earns these numbers: all type introspection happens **once, at
startup**. The request path is dict lookup → msgspec decode → handler call → encode, and
nothing is ever added to it. See [the design philosophy](index.md) for why that's a
deliberate, non-negotiable bet.
