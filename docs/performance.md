# Performance / Benchmarks

Four workloads, ten frameworks, one worker on one dedicated core each. Every app, the
harness, and the full methodology are public at
[api-benchmarks](https://github.com/RogerThomas/api-benchmarks).

**TL;DR:** jero is the fastest Python ASGI framework on all four workloads, by 1.4× to
4.5× over Blacksheep, Litestar, and FastAPI. It is the fastest Python framework on three
of the four and ties Django Bolt (within 0.1%) on the fourth. Per core, it beats Go's
Gin on three of the four and is the fastest of all ten on the database path.

<p align="center">
  <img src="assets/bench-grid.svg" alt="Benchmark results: jero is the fastest Python ASGI framework across all four workloads" width="820">
</p>

Each panel is scaled to its own fastest framework; the labels show absolute throughput.

## Setup

| Setting         | Value                                                             |
| :-------------- | :---------------------------------------------------------------- |
| Machine         | Amazon EC2 `c9g.2xlarge` spot, eu-central-1, one per framework    |
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
| Outbound client | lothc 0.2.0 (over pyreqwest) for every Python framework           |
| Database driver | fastpg for the async Python fleet; psycopg for sync Flask         |
| Pools           | 64 connections per framework, database and outbound HTTP alike    |
| Runs            | Gin, Elysia, Spring Boot (†): `20261023T091453`; Python frameworks rerun in `20261023T113907` to pick up lothc 0.2.0, same harness and settings |

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

`req/s` higher is better; `mean` and `p99` lower is better. † marks a row from run
`20261023T091453`.

### 1. `GET /info`: the pure framework path

Route, build a typed JSON response with a custom header, encode. No I/O.

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

### 2. `POST /movies`: the authed write path (JWT)

JWT auth, decode and validate a five-field body, encode, `201`.

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

### 3. `GET /catalog`: the outbound hop

An authenticated call to a Rust upstream, decoded into a typed model and returned.

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

Elysia relays the upstream JSON without decoding it into a typed model.

### 4. `GET /users/me`: the database read

JWT auth, then one Postgres row by the token's subject. Gin, Elysia, and Spring Boot use
their native drivers.

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

## Caveats

- **Only one core was measured.** Go and the JVM scale across cores inside one
  process; Python and Bun add processes, each with its own memory (~40–107M per Python
  worker here, against Gin's ~10–29M).
- **Spring Boot ran on JVM defaults** at ~615–684M resident; a tuned footprint was not
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
