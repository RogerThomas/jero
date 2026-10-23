# Benchmarks

The **Benchmarks** GitHub Actions workflow (`codspeed.yml`) runs the CodSpeed suite
in CPU simulation on every PR and push to `main`, and on manual dispatch. It
detects changes in the CPU cost of jero's routing, validation, serialization,
authentication, and wiring; results appear as CodSpeed comparisons between
commits and in PR reports.

CPU simulation counts instructions, so shared CI runners don't add noise to it. It
also excludes system-call time, so it doesn't measure network req/s or database
latency. Published HTTP throughput numbers (see the
[performance page](../docs/performance.md)) come from a separate harness run on
dedicated EC2 hardware, not from this repository.

## Run in GitHub Actions

Open **Actions → Benchmarks → Run workflow**, or:

```sh
gh workflow run codspeed.yml --ref YOUR_BRANCH
```

A new run cancels an older one on the same ref.

## Run locally

```sh
uv sync --all-groups

# Check all 17 benchmark cases without instrumentation:
task test-one PYTEST_PROFILE=agent -- benchmarks/codspeed --timeout=0

# Measure CPU regressions with CodSpeed's CLI installed:
codspeed run --mode simulation -- uv run pytest benchmarks/codspeed --codspeed --timeout=0
```

`benchmarks/codspeed/app.py` wires one route per request shape worth tracking,
plus the info and JWT movie workloads from `workloads.py`; `harness.py` drives
them in-process on a benchmark-owned event loop. The other cases cover startup.
CodSpeed Macro Runners require an organization-owned repository; the current
personal-repository integration uses simulation on standard GitHub runners.
