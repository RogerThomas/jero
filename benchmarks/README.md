# Benchmarks

The **Benchmarks** GitHub Actions workflow (`codspeed.yml`) runs two measurement
modes that share the HTTP workload handlers, models, and ryjwt authentication.

| Mode | Runs | Measures | Results |
| :--- | :--- | :--- | :--- |
| CPU (CodSpeed) | Every PR and push to `main`; manual | CPU cost of jero's request handling and startup | CodSpeed comparisons between commits and PR reports |
| HTTP (k6) | Manual | Real HTTP throughput, latency, and CPU under concurrent load; jero vs FastAPI, including PostgreSQL and upstream calls | GitHub job summary and artifacts |

CodSpeed's CPU simulation detects changes in routing, validation, serialization,
authentication, and wiring. It excludes system-call time, so its measurements
are not network req/s or database latency. The HTTP job keeps the real services
and concurrent load needed for those measurements. CPU results go to CodSpeed;
HTTP results remain in GitHub.

## Run in GitHub Actions

Open **Actions → Benchmarks → Run workflow**. Select `cpu` (the default), `http`,
or `all`. The runner, CPU budget, duration, attempts, and VU inputs affect only
the HTTP job. Automatic PR and `main` runs select only CPU checks.

```sh
# CodSpeed CPU regression checks:
gh workflow run codspeed.yml --ref YOUR_BRANCH -f suite=cpu

# Full HTTP comparison:
gh workflow run codspeed.yml --ref YOUR_BRANCH -f suite=http \
  -f duration=3 -f runs=3 -f vus=100

# Both modes in one workflow run:
gh workflow run codspeed.yml --ref YOUR_BRANCH -f suite=all \
  -f duration=3 -f runs=3 -f vus=100
```

The jobs run independently. A new CPU run cancels an older CPU run on the same
ref; an HTTP measurement already in progress runs to completion.

## Run locally

```sh
uv sync --all-groups

# Check all 17 CPU benchmark cases without instrumentation:
task test-one PYTEST_PROFILE=agent -- benchmarks/codspeed --timeout=0

# Measure CPU regressions with CodSpeed's CLI installed:
codspeed run --mode simulation -- uv run pytest benchmarks/codspeed --codspeed --timeout=0

# Run the real HTTP comparison (Docker needs at least four CPUs):
docker buildx bake -f benchmarks/ci/docker-bake.hcl --load
uv run --no-sync yeet ./benchmarks/ci/run.py
```

`benchmarks/codspeed/app.py` reuses the `/info` and `/movies` handlers from
`benchmarks/ci/jero_app.py`, wiring them without database or HTTP pools. Its
other cases cover individual request shapes and startup. The HTTP suite uses
the full app with real pools and the same DTOs and authentication.

See [HTTP suite details](ci/README.md) for workloads, CPU allocation, fairness,
artifacts, and runner options. CodSpeed Macro Runners require an
organization-owned repository; the current personal-repository integration uses
simulation on standard GitHub runners.
