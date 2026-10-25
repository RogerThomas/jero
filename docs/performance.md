# Performance / Benchmarks

Four workloads, ten frameworks, one worker on one dedicated core each. Every app, the
harness, and the full methodology are public at
[api-benchmarks](https://github.com/RogerThomas/api-benchmarks).

**TL;DR:** jero is the fastest Python framework on all four workloads, by 1.03× to
1.34× over Django Bolt and 1.4× to 5.0× over Blacksheep, Litestar, and FastAPI. Per
core, it beats Go's Gin on three of the four and is the fastest of all ten on the
database path.

<p align="center">
  <img src="assets/bench-grid.svg" alt="Benchmark results: jero is the fastest Python framework across all four workloads" width="820">
</p>

Each panel is scaled to its own fastest framework; the labels show absolute throughput.

## Setup

| Setting         | Value                                                             |
| :-------------- | :---------------------------------------------------------------- |
| Machine         | Amazon EC2 `c9g.2xlarge` spot, eu-central-1, one framework at a time |
| Machine specs   | AWS Graviton5 (arm64), 8 vCPUs (physical cores, no SMT), 16 GiB   |
| CPU             | 1 dedicated core per framework (cpuset affinity)                  |
| Workers         | 1 (Gin at `GOMAXPROCS=1`, Go's own default on one core; Django Bolt at `--processes 1`) |
| Spring Boot     | Tomcat with virtual threads, JVM defaults (no heap flags)         |
| Load generator  | wrk 4.2.0, 2 threads on 2 dedicated cores                         |
| Concurrency     | 128 connections, closed loop                                      |
| Duration        | 60s per attempt, after an untimed 3s warm-up                      |
| Best-of-N       | 3 attempts, highest throughput kept                               |
| Python          | 3.14.9 (uv's standalone CPython, every Python framework)          |
| Python server   | [peregrine](https://pypi.org/project/peregrine-server/) 1.2.0 + uvloop (Flask via peregrine WSGI; Robyn and Django Bolt ship their own Rust servers) |
| Outbound client | lothc 0.2.1 (over pyreqwest) for every Python framework           |
| Database driver | fastpg for the async Python fleet; psycopg for sync Flask         |
| Pools           | 64 connections per framework, database and outbound HTTP alike    |
| Versions        | jero 0.1.7, Django Bolt 0.13.0, FastAPI 0.162.0                   |
| Run             | `20261025T073117`, all ten frameworks, api-benchmarks commit `469c5ac` |

- **Correctness first.** Every framework passed a 100-request body check before load
  and returned 100% expected statuses under it.
- **Shared libraries.** Every Python framework uses the same HTTP client (lothc), JWT
  decoder (ryjwt), and Postgres driver (fastpg; Django Bolt included, bypassing Django's
  ORM). jero's author wrote all three, and this benchmark.
- **Native code.** The Python stacks do their JSON, JWT, database, and HTTP-client work
  in C and Rust extensions; Gin uses `encoding/json`, golang-jwt, and pgx.
- **Otherwise idiomatic.** The differences between apps are listed under "Known,
  intended differences" in the [api-benchmarks README](https://github.com/RogerThomas/api-benchmarks#readme).
- **Latency is under full load** (128 connections, closed loop), where tails run
  optimistic.

## Results

`req/s` higher is better; `mean` and `p99` lower is better.

### 1. `GET /info`: the pure framework path

Route, build a typed JSON response with a custom header, encode. No I/O.

| Framework            | req/s      | mean       | p99        | vs jero   |
| :------------------- | :--------- | :--------- | :--------- | :-------- |
| elysia *(Bun)*       | 156.8k     | 0.82ms     | 1.46ms     | 1.55×     |
| **jero**             | **101.2k** | **1.26ms** | **1.30ms** | **1.00×** |
| django-bolt          | 90.5k      | 1.41ms     | 1.45ms     | 0.89×     |
| gin *(Go)*           | 89.3k      | 1.44ms     | 3.25ms     | 0.88×     |
| blacksheep           | 73.4k      | 1.73ms     | 1.78ms     | 0.73×     |
| litestar             | 49.6k      | 2.56ms     | 2.62ms     | 0.49×     |
| robyn                | 43.8k      | 2.92ms     | 3.02ms     | 0.43×     |
| spring-boot *(Java)* | 42.4k      | 3.01ms     | 4.81ms     | 0.42×     |
| fastapi              | 27.3k      | 4.66ms     | 4.73ms     | 0.27×     |
| flask                | 27.3k      | 4.68ms     | 4.79ms     | 0.27×     |

### 2. `POST /movies`: the authed write path (JWT)

JWT auth, decode and validate a five-field body, encode, `201`.

| Framework            | req/s      | mean       | p99        | vs jero   |
| :------------------- | :--------- | :--------- | :--------- | :-------- |
| **jero**             | **82.9k**  | **1.53ms** | **1.58ms** | **1.00×** |
| django-bolt          | 80.6k      | 1.59ms     | 1.62ms     | 0.97×     |
| gin *(Go)*           | 54.6k      | 2.35ms     | 5.01ms     | 0.66×     |
| elysia *(Bun)*       | 53.7k      | 2.38ms     | 4.26ms     | 0.65×     |
| blacksheep           | 32.9k      | 3.88ms     | 3.95ms     | 0.40×     |
| litestar             | 30.0k      | 4.25ms     | 4.32ms     | 0.36×     |
| spring-boot *(Java)* | 27.2k      | 4.70ms     | 7.24ms     | 0.33×     |
| robyn                | 26.3k      | 4.86ms     | 4.93ms     | 0.32×     |
| flask                | 20.1k      | 6.35ms     | 6.48ms     | 0.24×     |
| fastapi              | 16.7k      | 7.65ms     | 7.75ms     | 0.20×     |

### 3. `GET /catalog`: the outbound hop

An authenticated call to a Rust upstream, decoded into a typed model and returned.

| Framework            | req/s      | mean       | p99        | vs jero   |
| :------------------- | :--------- | :--------- | :--------- | :-------- |
| elysia *(Bun)*       | 55.4k      | 2.31ms     | 3.79ms     | 1.82×     |
| gin *(Go)*           | 33.1k      | 3.87ms     | 7.05ms     | 1.09×     |
| **jero**             | **30.4k**  | **4.21ms** | **7.13ms** | **1.00×** |
| django-bolt          | 22.7k      | 5.66ms     | 11.98ms    | 0.75×     |
| blacksheep           | 20.6k      | 6.20ms     | 11.55ms    | 0.68×     |
| spring-boot *(Java)* | 20.3k      | 6.29ms     | 9.33ms     | 0.67×     |
| litestar             | 18.6k      | 6.89ms     | 14.05ms    | 0.61×     |
| robyn                | 18.0k      | 7.12ms     | 10.82ms    | 0.59×     |
| flask                | 11.4k      | 11.24ms    | 11.88ms    | 0.37×     |
| fastapi              | 10.7k      | 11.95ms    | 22.85ms    | 0.35×     |

Elysia relays the upstream JSON without decoding it into a typed model.

### 4. `GET /users/me`: the database read

JWT auth, then one Postgres row by the token's subject. Gin, Elysia, and Spring Boot use
their native drivers.

| Framework            | req/s      | mean       | p99        | vs jero   |
| :------------------- | :--------- | :--------- | :--------- | :-------- |
| **jero**             | **46.7k**  | **2.73ms** | **3.28ms** | **1.00×** |
| gin *(Go)*           | 38.9k      | 3.29ms     | 6.87ms     | 0.83×     |
| elysia *(Bun)*       | 36.3k      | 3.52ms     | 6.78ms     | 0.78×     |
| django-bolt          | 35.9k      | 3.55ms     | 4.36ms     | 0.77×     |
| blacksheep           | 28.2k      | 4.54ms     | 5.80ms     | 0.60×     |
| robyn                | 22.5k      | 5.70ms     | 8.53ms     | 0.48×     |
| litestar             | 22.0k      | 5.81ms     | 7.63ms     | 0.47×     |
| spring-boot *(Java)* | 21.0k      | 6.08ms     | 9.49ms     | 0.45×     |
| fastapi              | 11.8k      | 10.83ms    | 21.43ms    | 0.25×     |
| flask                | 8.9k       | 14.37ms    | 15.87ms    | 0.19×     |

## Caveats

- **Only one core was measured.** Go and the JVM scale across cores inside one
  process; Python and Bun add processes, each with its own memory (~39–93M per Python
  worker here, against Gin's ~10–28M).
- **Spring Boot ran on JVM defaults** at ~612–676M resident; a tuned footprint was not
  measured.
- **A benchmark is not your app.** Fixed payloads on an idle host. Measure the workload
  you care about.

## What middleware and CORS cost

[Middleware](guide/middleware.md) and [CORS](guide/cors.md) compile at wiring, so routes
they don't cover pay nothing. Relative cost on the in-process hot-path harness
(`bench.py`'s POST-echo shape, ~1.3µs/request baseline; in-process numbers exaggerate
deltas compared with a real server):

| configuration | relative cost |
| :-- | :-- |
| no middleware, no CORS | 1.00× |
| wildcard CORS / a constant `response_headers` block | ~1.02× |
| origin allow-list CORS | ~1.15× |
| on-scope `intercept` (falling through) | ~1.4× |
| `observe` | ~1.6× |
| `response_headers` method | ~1.9× |

For contrast, one onion-style ASGI wrapper appending a single constant CORS pair costs
~1.3× on every request.

## Measure it yourself

One script runs jero, Litestar, BlackSheep, and FastAPI **in-process as bare ASGI
callables** (no server, no sockets) against a hand-rolled raw ASGI app doing the same
work, the ceiling for that work. With no server in the loop the gaps run far wider than in
the tables above, so read each framework's distance from `raw`, not its throughput.

```bash
curl -LsSf https://raw.githubusercontent.com/RogerThomas/jero/main/benchmarks/micro_bench.py | uv run -
```

The source is below; save it as `micro_bench.py` and `uv run micro_bench.py` to run it
locally.

```python
--8<-- "benchmarks/micro_bench.py"
```
