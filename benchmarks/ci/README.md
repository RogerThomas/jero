# HTTP benchmarks on GitHub Actions

Select `http` or `all` in the **Benchmarks** workflow's manual run. See the
[suite overview](../README.md) for CodSpeed CPU regression checks and shared
workloads. Docker Bake builds the shared
Python app image, the k6 runner, and the Rust upstream. Compose runs one framework
at a time using `compose.yml` plus `compose.jero.yml` or `compose.fastapi.yml`.
Both apps use Python 3.14.8, granian, uvloop, and one worker on the same single CPU.
Reports appear in the job summary; all attempts and logs are retained for 30 days.
No external results service is involved.

The manual workflow offers `ubuntu-24.04` (x86) or `ubuntu-24.04-arm` (ARM), both
standard four-CPU GitHub-hosted runners that are free for public repositories.
The images build natively on the chosen architecture, with separate build caches.
Changing architecture changes the benchmark environment; compare frameworks in
the same run rather than treating results from different machines as equivalent.

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
`--backend-cpus N` assigns N CPUs to each supporting service (default one).
Except for the single shared support CPU on a four-CPU host, backend allocations
must be separate; insufficient capacity fails before any containers start.

On a larger host, for example a 16-core runner:

```sh
uv run --no-sync yeet ./benchmarks/ci/run.py --load-cpus 4 --backend-cpus 2
```

The app keeps one CPU, k6 gets four, and each backend gets two. Other CPU IDs are
left out of these container allocations. The workflow exposes both CPU counts as
manual inputs; standard four-CPU runners need their default two/one settings.

### CodSpeed eligibility

[CodSpeed Macro Runners](https://codspeed.io/docs/features/macro-runners) offer
dedicated 16-core Graviton hosts with 600 free minutes per month. They run GitHub
Actions jobs, so logs and artifacts can stay in GitHub. However, their
[GitHub Actions prerequisites](https://codspeed.io/docs/integrations/ci/github-actions/macro-runners)
require an **organization-owned repository**, CodSpeed enabled on that repository,
and their walltime action. Personal repositories cannot use these runners.
`RogerThomas/jero` is currently a personal repository; selecting a CodSpeed label
without changing that would leave the job without a usable runner. The workflow
therefore offers the working GitHub-hosted options until eligibility is resolved.

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
gh workflow run codspeed.yml --ref YOUR_BRANCH -f suite=http \
  -f duration=3 -f runs=3 -f vus=100

# Native ARM on GitHub's free runner:
gh workflow run codspeed.yml --ref YOUR_BRANCH -f suite=http -f runner=ubuntu-24.04-arm \
  -f load_cpus=2 -f backend_cpus=1 -f duration=3 -f runs=3 -f vus=100
```

Artifacts contain `report.md`, typed `results.json`, each framework's report,
every k6 summary and warmup, and Compose logs for all services. Failed runs retain
partial results and an incomplete report. The HTTP job remains manual.
