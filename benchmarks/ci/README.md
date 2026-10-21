# HTTP benchmarks on GitHub Actions

Run **HTTP benchmarks** from the repository's Actions tab. The workflow checks out
the selected ref, installs that checkout of jero, and runs jero and FastAPI
sequentially on the same `ubuntu-24.04` runner. Both use granian, asyncio, one worker,
and the same server CPU. Every measured app and supporting service gets exactly
one logical CPU: app, k6, PostgreSQL, and upstream each have a distinct CPU.
k6 also uses `GOMAXPROCS=1`; PostgreSQL has a one-CPU Docker quota, and the runner
reads back and verifies its CPU limits before seeding or measuring.

The workloads come from the larger `jero-benchmarks` harness:

- `GET /info`: build a medium typed JSON response and a fresh UUID response header.
- `POST /movies`: verify an HS256 bearer JWT with **ryjwt from PyPI**, validate a
  five-field JSON body, and return a new UUID plus the authenticated user.
- `GET /catalog`: fetch a nested product from a real, API-key-protected local HTTP
  upstream using pyreqwest, decode/validate every field, then serialize the result.
- `GET /users/me`: verify the JWT, acquire a PostgreSQL connection, execute a
  parameterized query for its subject, and return the six-column typed profile.

Both apps decode the same JWT claims into a msgspec Struct with ryjwt. jero uses
its native binding and serialization; FastAPI uses its normal Pydantic models,
async auth dependency, and response validation. The signing key is fixed public
benchmark data and is not a deployment credential.

Both apps use pyreqwest with identical settings and a psycopg pool of eight
autocommit connections. Clients and pools open at startup and close at shutdown. The database
is seeded before either app starts; every measured profile request queries it.
The upstream is a separate one-worker granian process, shared by both frameworks.

Defaults: 32 concurrent users, 2 seconds of warmup per workload, and three measured
10-second attempts per framework/workload. The report selects the attempt with
median throughput and displays that attempt's mean and p99 latency. All attempts
are saved. Before measuring, the runner verifies the complete successful response
contracts, missing/invalid/expired JWT rejection, invalid-body rejection, upstream
API-key rejection, and a 404 for a valid JWT whose subject has no database row. k6
also checks responses under load; any failed request or check fails the run.

The report records **k6 CPU**, the load generator's CPU usage (100% is its one
allocated core, not the framework's utilization). This helps
investigate whether the runner is limited by the generator. k6 is the initial tool
because its per-response assertions verify both status and payload under load;
alternatives such as oha can be evaluated if generator capacity limits the results.

The Markdown report appears directly in the workflow summary. Its artifact, kept
for 30 days, contains `report.md`, `results.json` with configuration/versions/commit,
every raw k6 summary (including warmups), and app/upstream/database/load-generator logs. Failed
runs retain logs and a report marked incomplete.

GitHub-hosted runners can differ between runs. Use the within-run comparison;
these numbers do not replace the dedicated EC2 suite or its published figures.

## Run locally

Use Linux with at least four available logical CPUs, Docker, and
[k6](https://grafana.com/docs/k6/latest/set-up/install-k6/). The runner deliberately
rejects platforms where it cannot enforce one-CPU affinity. From the repository root:

```sh
uv sync --group benchmark
docker run -d --name jero-benchmark-db --cpus 1 -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=benchmark -e POSTGRES_PASSWORD=benchmark \
  -e POSTGRES_DB=benchmark postgres:17
uv run --no-sync yeet ./benchmarks/ci/run.py --database-container jero-benchmark-db

# Short end-to-end smoke run, including auth/validation checks:
uv run --no-sync yeet ./benchmarks/ci/run.py --duration 1 --runs 1 --vus 2 \
  --warmup 1 --output /tmp/jero-benchmark-smoke --database-container jero-benchmark-db
```

Use a fresh output directory for each run. Ports 8000 and 8001 must be free;
PostgreSQL uses 5432. The runner assigns the first four CPUs from the host's allowed
affinity set to app, k6, PostgreSQL, and upstream respectively. It updates the supplied
PostgreSQL container's affinity and quota, so use a dedicated benchmark container.
Set `BENCHMARK_DATABASE_DSN` if that container uses a different local database port.
The workflow starts its own PostgreSQL service container and saves its logs; for
local runs, collect them with `docker logs jero-benchmark-db`.

## Run a PR branch

Select the branch in the workflow's **Run workflow** menu, or use:

```sh
gh workflow run benchmarks.yml --ref YOUR_BRANCH -f duration=1 -f runs=1 -f vus=2
```

The workflow remains manual. More frameworks can be added to these four workloads
without changing the artifact/report flow or introducing an external results service.
