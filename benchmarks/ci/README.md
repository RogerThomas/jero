# HTTP benchmarks on GitHub Actions

Run **HTTP benchmarks** from the repository's Actions tab. The workflow checks out
the selected ref, installs that checkout of jero, and runs jero and FastAPI
sequentially on the same `ubuntu-24.04` runner. Both use granian, asyncio, one worker,
and the same server CPU; k6 runs on the other available CPUs.

The initial workloads come from the larger `jero-benchmarks` harness:

- `GET /info`: build a medium typed JSON response and a fresh UUID response header.
- `POST /movies`: verify an HS256 bearer JWT with **ryjwt from PyPI**, validate a
  five-field JSON body, and return a new UUID plus the authenticated user.

Both apps decode the same JWT claims into a msgspec Struct with ryjwt. jero uses
its native binding and serialization; FastAPI uses its normal Pydantic models,
async auth dependency, and response validation. The signing key is fixed public
benchmark data and is not a deployment credential.

Defaults: 32 concurrent users, 2 seconds of warmup per workload, and three measured
10-second attempts per framework/workload. The report selects the attempt with
median throughput and displays that attempt's mean and p99 latency. All attempts
are saved. Before measuring, the runner verifies the complete successful response
contracts, missing/invalid/expired JWT rejection, and invalid-body rejection. k6
also checks responses under load; any failed request or check fails the run.

The report also records load-generator CPU usage (100% means one core). This helps
investigate whether the runner is limited by the generator. k6 is the initial tool
because its per-response assertions verify both status and payload under load;
alternatives such as oha can be evaluated if generator capacity limits the results.

The Markdown report appears directly in the workflow summary. Its artifact, kept
for 30 days, contains `report.md`, `results.json` with configuration/versions/commit,
every raw k6 summary (including warmups), and server/load-generator logs. Failed
runs retain logs and a report marked incomplete.

GitHub-hosted runners can differ between runs. Use the within-run comparison;
these numbers do not replace the dedicated EC2 suite or its published figures.

## Run locally

Install [k6](https://grafana.com/docs/k6/latest/set-up/install-k6/), then from the
repository root:

```sh
uv sync --group benchmark
uv run --no-sync yeet ./benchmarks/ci/run.py

# Short end-to-end smoke run, including auth/validation checks:
uv run --no-sync yeet ./benchmarks/ci/run.py --duration 1 --runs 1 --vus 2 \
  --warmup 1 --output /tmp/jero-benchmark-smoke
```

Use a fresh output directory for each run. Port 8000 must be free. Linux runs pin
the server and load generator to separate CPUs; local macOS runs do not pin CPUs.

## Next workloads

The next increments are `GET /catalog` against a local HTTP upstream and JWT-authenticated
`GET /users/me` against Postgres. These can use GitHub Actions service containers
and extend the same per-attempt JSON/artifact/report flow. No external results
service is needed. More frameworks can be added once these first two are running
reliably in CI.
