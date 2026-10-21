#!yeet
"""Run correctness checks and k6 inside the Compose benchmark container."""

import asyncio
import os
import platform
import resource
import signal
import sys
from contextlib import suppress
from datetime import UTC, datetime
from http.client import HTTPConnection
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from uuid import UUID

import msgspec

from benchmarks.ci.common import (
    Claims,
    CPUCounter,
    Info,
    Movie,
    Nested,
    Product,
    Profile,
    User,
    Workload,
)
from benchmarks.ci.reporting import Attempt, Framework, Metrics, Report, WorkloadName, write


async def _metadata(duration: int, runs: int, vus: int, warmup: int) -> Report:
    if sys.platform == "linux":
        load_cpus = sorted(os.sched_getaffinity(0))
    else:
        raise RuntimeError("Run the benchmark worker in its Linux Compose container")
    expected = [int(cpu) for cpu in os.environ["BENCHMARK_LOAD_CPUS"].split(",")]
    if load_cpus != expected:
        raise RuntimeError(f"k6 CPU affinity is {load_cpus}, expected {expected}")
    process = await asyncio.create_subprocess_exec("k6", "version", stdout=asyncio.subprocess.PIPE)
    output, _ = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError("Cannot run k6")
    cpu = platform.processor() or platform.machine()
    for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("model name"):
            cpu = line.partition(":")[2].strip()
            break
    return Report(
        created_at=datetime.now(UTC).isoformat(),
        commit=os.environ["BENCHMARK_COMMIT"],
        run_url=os.environ.get("BENCHMARK_RUN_URL") or None,
        python=platform.python_version(),
        platform=platform.platform(),
        cpu=cpu,
        server_cpus=[int(os.environ["BENCHMARK_APP_CPU"])],
        load_cpus=load_cpus,
        database_cpus=[int(os.environ["BENCHMARK_DATABASE_CPU"])],
        upstream_cpus=[int(os.environ["BENCHMARK_UPSTREAM_CPU"])],
        versions={
            name: version(name)
            for name in (
                "jero",
                "fastapi",
                "granian",
                "uvloop",
                "msgspec",
                "pydantic",
                "ryjwt",
                "pyreqwest",
                "psycopg",
                "psycopg-pool",
            )
        },
        k6=output.decode().strip(),
        duration_seconds=duration,
        vus=vus,
        runs=runs,
        warmup_seconds=warmup,
    )


def _request(
    method: str,
    path: str,
    body: bytes | None = None,
    token: str | None = None,
    *,
    host: str = "app",
    port: int = 8000,
) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection(host, port, timeout=2)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.headers.items()), response.read()
    finally:
        connection.close()


async def _ready(*, host: str = "app", port: int = 8000, path: str = "/info") -> None:
    for _ in range(300):
        try:
            if _request("GET", path, host=host, port=port)[0] == 200:
                return
        except OSError:
            pass
        await asyncio.sleep(0.1)
    raise TimeoutError(f"{host}:{port} did not become ready")


def _smoke(token: str) -> None:
    status, headers, content = _request("GET", "/info")
    if status != 200:
        raise RuntimeError(f"GET /info returned {status}")
    UUID(headers["x-response-id"])
    expected = Info(
        id="info-1",
        name="Benchmark Info",
        count=42,
        ratio=3.14159,
        active=True,
        tags=["alpha", "beta", "gamma"],
        created_at="2026-01-01T00:00:00Z",
        nested=Nested(level=2, label="nested"),
    )
    if msgspec.json.decode(content, type=Info) != expected:
        raise RuntimeError("GET /info returned an unexpected payload")
    body = msgspec.json.encode(Workload.movie())
    status, _, content = _request("POST", "/movies", body, token)
    if status != 201:
        raise RuntimeError(f"POST /movies returned {status}")
    movie = msgspec.json.decode(content, type=Movie)
    if movie != Movie(
        id=movie.id,
        user=User(id="user-1", name="name"),
        title="title",
        year=2026,
        director="director",
        genre="genre",
        rating=8.0,
    ):
        raise RuntimeError("POST /movies returned an unexpected payload")
    expired = Workload.signing_key().encode(Claims(sub="user-1", name="name", exp=1))
    for invalid in (None, "invalid", expired, token + "invalid"):
        if _request("POST", "/movies", body, invalid)[0] != 401:
            raise RuntimeError("POST /movies accepted invalid credentials")
    invalid_body = body.replace(b"2026", b'"invalid"')
    if _request("POST", "/movies", invalid_body, token)[0] != 422:
        raise RuntimeError("POST /movies accepted an invalid body")
    _smoke_backends(token, expired)


def _smoke_backends(token: str, expired: str) -> None:
    status, _, content = _request("GET", "/catalog")
    if status != 200 or msgspec.json.decode(content, type=Product) != Workload.product():
        raise RuntimeError("GET /catalog returned an unexpected upstream product")
    status, _, content = _request("GET", "/users/me", token=token)
    if status != 200 or msgspec.json.decode(content, type=Profile) != Workload.profile():
        raise RuntimeError("GET /users/me returned an unexpected database profile")
    for invalid in (None, "invalid", expired, token + "invalid"):
        if _request("GET", "/users/me", token=invalid)[0] != 401:
            raise RuntimeError("GET /users/me accepted invalid credentials")
    missing = Workload.signing_key().encode(Claims(sub="missing-user", name="name", exp=4102444800))
    if _request("GET", "/users/me", token=missing)[0] != 404:
        raise RuntimeError("GET /users/me did not look up the authenticated user")
    for invalid in (None, "invalid"):
        if _request("GET", "/data", token=invalid, host="upstream", port=8001)[0] != 401:
            raise RuntimeError("The upstream accepted an invalid API key")


def _cpu() -> CPUCounter:
    status, _, content = _request("GET", "/_benchmark/cpu")
    if status != 200:
        raise RuntimeError("Cannot sample the app worker CPU counter")
    return msgspec.json.decode(content, type=CPUCounter)


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def _load(
    report: Report,
    output: Path,
    workload: WorkloadName,
    name: str,
    token: str,
    duration: int,
) -> Metrics:
    result_file = output.resolve() / f"{name}.json"
    environment = {
        **os.environ,
        "BASE_URL": "http://app:8000",
        "WORKLOAD": workload,
        "DURATION": str(duration),
        "VUS": str(report.vus),
        "TOKEN": token,
        "MOVIE": msgspec.json.encode(Workload.movie()).decode(),
        "RESULT_FILE": str(result_file),
        "GOMAXPROCS": str(len(report.load_cpus)),
    }
    server_before = _cpu()
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = perf_counter()
    with (output / f"{name}.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            "k6",
            "run",
            "--quiet",
            "--no-usage-report",
            str(Path(__file__).with_name("load.js")),
            env=environment,
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = await asyncio.wait_for(process.wait(), timeout=duration + 30)
        finally:
            await _stop(process)
    elapsed = perf_counter() - started
    cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    server_after = _cpu()
    if code != 0:
        raise RuntimeError(f"k6 failed for {name}; see {name}.log and {name}.json")
    if server_before.pid != server_after.pid:
        raise RuntimeError("The sole app worker restarted during the attempt")
    metrics = msgspec.json.decode(result_file.read_bytes(), type=Metrics)
    if metrics.requests < 1 or metrics.failed_rate != 0 or metrics.checks_rate != 1:
        raise RuntimeError(f"{name} returned failed requests or response checks")
    cpu_seconds = (
        cpu_after.ru_utime + cpu_after.ru_stime - cpu_before.ru_utime - cpu_before.ru_stime
    )
    metrics.generator_cpu_percent = 100 * cpu_seconds / elapsed
    metrics.server_cpu_percent = (
        100
        * (server_after.cpu_seconds - server_before.cpu_seconds)
        / (server_after.wall_seconds - server_before.wall_seconds)
    )
    return metrics


async def main(
    *,
    duration: int = 10,
    runs: int = 3,
    vus: int = 32,
    warmup: int = 2,
    output: Path = Path("/results"),
) -> None:
    """Run one framework's four workloads in its CPU-limited Compose stack."""
    selected = os.environ["BENCHMARK_FRAMEWORK"]
    framework: Framework
    if selected == "jero":
        framework = "jero"
    elif selected == "fastapi":
        framework = "fastapi"
    else:
        raise ValueError(f"Unknown framework: {selected}")
    output = output / framework
    output.mkdir(parents=True, exist_ok=False)
    report = await _metadata(duration, runs, vus, warmup)
    try:
        await _ready(host="upstream", port=8001, path="/health")
        await _ready()
        token = Workload.token()
        _smoke(token)
        workloads: tuple[WorkloadName, ...] = ("info", "movies", "catalog", "profile")
        for workload in workloads:
            await _load(report, output, workload, f"{framework}-{workload}-warmup", token, warmup)
            for attempt in range(1, runs + 1):
                name = f"{framework}-{workload}-{attempt}"
                metrics = await _load(report, output, workload, name, token, duration)
                report.attempts.append(Attempt(framework, workload, attempt, metrics))
                print(f"{name}: {metrics.requests_per_second:,.0f} req/s", flush=True)
        report.complete = True
    finally:
        write(report, output)
