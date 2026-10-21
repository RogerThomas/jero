# HTTP benchmarks on GitHub Actions

Run **HTTP benchmarks** manually from Actions. Docker Bake builds the shared
Python app image, the k6 runner, and the Rust upstream. Compose runs one framework
at a time using `compose.yml` plus `compose.jero.yml` or `compose.fastapi.yml`.
Both apps use Python 3.14.8, granian, uvloop, and one worker on the same single CPU.
Reports appear in the job summary; all attempts and logs are retained for 30 days.
No external results service is involved.

## CPU allocation and interpreting results

Every app has a one-CPU affinity and quota. The generator needs spare capacity to
measure that app: a saturated k6 process can hide the faster framework's capacity.
The default four-CPU runner layout is:

| Component | CPUs | Quota |
| :--- | :--- | :--- |
| App | 0 | 1 CPU |
| k6 | 1,2 | 2 CPUs |
| PostgreSQL | 3 | 1 CPU |
| Rust upstream | 3 | 1 CPU |

The two supporting services share a CPU; each measured workload uses only one of
them. App and generator CPUs do not overlap with supporting services. The runner
uses actual available CPU IDs, verifies every container's affinity and quota
before startup, and checks the benchmark container's actual affinity at runtime.

The AWS suite's current layout is larger: app `0`, PostgreSQL `1,2`, upstream
`3,4`, k6 `5,6`. It also runs on dedicated Graviton hardware rather than shared
x86 virtual CPUs. This four-CPU suite is not an exact AWS reproduction. Its
PostgreSQL driver is psycopg; the AWS suite currently uses a vendored fastpg wheel.

The report shows **App CPU** and **k6 CPU** separately. App CPU samples the sole
worker's process CPU clock, including all its threads, before and after each
attempt. k6 CPU comes from its child-process resource usage. These averages
include process startup/shutdown; 100% means one CPU. k6's header states its total
budget (normally 200%). Attempts using at least 90% of that budget are marked
**generator saturated** and cannot establish full framework capacity on their own.

`--load-cpus 1` deliberately tests with one generator CPU instead; the default is
two. For more than two, the Docker host needs additional CPUs so app and generator
remain separate from supporting services. All allocations appear in the report.

## Workloads and fairness

- `GET /info`: construct medium typed JSON and a fresh UUID response header.
- JWT `POST /movies`: verify HS256 with **ryjwt from PyPI**, validate five input
  fields, and return a new UUID plus the authenticated user.
- `GET /catalog`: fetch from a real API-key-protected Rust/axum upstream, decode
  and validate its nested static JSON, then serialize the result.
- JWT `GET /users/me`: acquire a PostgreSQL connection, execute a parameterized
  query for the verified subject, and return a six-column typed profile.

Both apps use equivalent pyreqwest settings and 64-connection psycopg pools.
Resources open during startup and close at shutdown. A fresh database is seeded
at app startup before measurement; every profile request performs a real query.
jero uses msgspec models and native binding/serialization; FastAPI uses Pydantic,
an async auth dependency, and normal response validation.

Before load, the worker checks complete response contracts, upstream API-key
rejection, missing/malformed/expired JWT rejection, invalid JSON input, and a valid
JWT whose subject has no row. During load, k6 uses the AWS suite's lightweight
checks: status/header for `/info`, and status/selected identity fields for the
other workloads. Full JSON validation stays in preflight so extra generator work
does not distort the comparison. Any failed request or assertion fails the run.

Defaults are 32 VUs, two seconds of warmup per workload, and three 10-second
attempts. The report selects the median-throughput attempt and that attempt's
latency and CPU measurements. This differs from the AWS suite's best-of-three
selection. All raw attempts remain available.

## Run locally

Docker must have at least four available CPUs. From the repository root:

```sh
uv sync --group benchmark
docker buildx bake -f benchmarks/ci/docker-bake.hcl --load
uv run --no-sync yeet ./benchmarks/ci/run.py

# Short end-to-end validation of both complete stacks:
uv run --no-sync yeet ./benchmarks/ci/run.py --duration 1 --runs 1 --vus 2 \
  --warmup 1 --output /tmp/jero-benchmark-smoke
```

The orchestrator creates a unique Compose project per framework, verifies CPU
limits, runs `up --no-build --exit-code-from benchmark`, collects logs, then runs
`down --volumes`. Apps, database, and upstream expose no host ports. Every run
needs a fresh output directory; Compose stacks and their anonymous database
volumes are removed even when the benchmark fails.

To run a single overlay directly after Bake (default CPU IDs 0–3):

```sh
docker compose -f benchmarks/ci/compose.yml -f benchmarks/ci/compose.jero.yml \
  up --no-build --exit-code-from benchmark
docker compose -f benchmarks/ci/compose.yml -f benchmarks/ci/compose.jero.yml \
  down --volumes
```

Replace `compose.jero.yml` with `compose.fastapi.yml` for FastAPI. Direct Compose
runs write the framework's report under `benchmark-results/`; the yeetr
orchestrator also creates the combined comparison and saves resolved image IDs.

## Validate a PR branch

Select the branch in **Run workflow**, or use:

```sh
gh workflow run benchmarks.yml --ref YOUR_BRANCH -f duration=3 -f runs=3 -f vus=100
```

Artifacts contain `report.md`, typed `results.json`, each framework's report,
every k6 summary and warmup, and Compose logs for all services. Failed runs retain
partial results and an incomplete report. The workflow remains manual.
