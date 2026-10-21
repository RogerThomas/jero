#!yeet
"""Run the HTTP benchmarks and write a portable Markdown report plus raw results."""

import asyncio
import os
import platform
import resource
import shutil
import signal
import socket
import sys
from contextlib import suppress
from datetime import UTC, datetime
from http.client import HTTPConnection
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import UUID

import msgspec
from msgspec import Struct

from benchmarks.ci.backends import Settings, seed_database
from benchmarks.ci.common import Claims, Info, Movie, Nested, Product, Profile, User, Workload

type Framework = Literal["jero", "fastapi"]
type WorkloadName = Literal["info", "movies", "catalog", "profile"]


class Options(Struct):
    """The validated run configuration passed to the benchmark harness."""

    duration: int
    runs: int
    vus: int
    warmup: int
    output: Path
    database_container: str


class Metrics(Struct):
    """The normalized fields emitted alongside the complete raw k6 summary."""

    requests_per_second: float
    requests: int
    mean_ms: float
    p99_ms: float
    failed_rate: float
    checks_rate: float
    generator_cpu_percent: float = 0.0


class Attempt(Struct):
    """One measured framework/workload attempt, excluding warmups."""

    framework: Framework
    workload: WorkloadName
    attempt: int
    metrics: Metrics


class Report(Struct):
    """The reproducibility metadata and every completed measured attempt."""

    created_at: str
    commit: str
    run_url: str | None
    python: str
    platform: str
    cpu: str
    server_cpus: list[int]
    load_cpus: list[int]
    upstream_cpus: list[int]
    database_cpus: list[int]
    versions: dict[str, str]
    k6: str
    duration_seconds: int
    vus: int
    runs: int
    warmup_seconds: int
    attempts: list[Attempt] = msgspec.field(default_factory=list[Attempt])
    complete: bool = False
    database_image: str = ""


class ContainerLimits(Struct, rename={"cpuset_cpus": "CpusetCpus", "nano_cpus": "NanoCpus"}):
    """The Docker CPU constraints read back after configuring PostgreSQL."""

    cpuset_cpus: str
    nano_cpus: int


class Response(Struct):
    """One local HTTP response used by the correctness checks."""

    status: int
    headers: dict[str, str]
    body: bytes


def _command(name: str) -> str:
    command = shutil.which(name)
    if command is None:
        raise RuntimeError(f"Install {name} before running the benchmarks")
    return command


def _affinity(cpus: list[int]) -> list[str]:
    if len(cpus) != 1:
        raise RuntimeError("Every benchmark process must be pinned to exactly one CPU")
    return [_command("taskset"), "--cpu-list", ",".join(map(str, cpus))]


async def _capture(*command: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    output, _ = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(output.decode())
    return output.decode().strip()


async def _metadata(options: Options) -> Report:
    if sys.platform == "linux":
        cpus = sorted(os.sched_getaffinity(0))
    else:
        raise RuntimeError("Run benchmarks on Linux or GitHub Actions to enforce one-CPU affinity")
    if len(cpus) < 4:
        raise RuntimeError(
            "The benchmark needs four CPUs: one each for app, k6, upstream, database"
        )
    cpu = platform.processor() or platform.machine()
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        cpu = next(
            (
                line.partition(":")[2].strip()
                for line in cpuinfo.read_text(encoding="utf-8").splitlines()
                if line.startswith("model name")
            ),
            cpu,
        )
    run_url: str | None = None
    if "GITHUB_RUN_ID" in os.environ:
        run_url = (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    return Report(
        created_at=datetime.now(UTC).isoformat(),
        commit=await _capture(_command("git"), "rev-parse", "HEAD"),
        run_url=run_url,
        python=platform.python_version(),
        platform=platform.platform(),
        cpu=cpu,
        server_cpus=cpus[:1],
        load_cpus=cpus[1:2],
        database_cpus=cpus[2:3],
        upstream_cpus=cpus[3:4],
        versions={
            name: version(name)
            for name in (
                "jero",
                "fastapi",
                "granian",
                "msgspec",
                "pydantic",
                "ryjwt",
                "pyreqwest",
                "psycopg",
                "psycopg-pool",
            )
        },
        k6=await _capture(_command("k6"), "version"),
        duration_seconds=options.duration,
        vus=options.vus,
        runs=options.runs,
        warmup_seconds=options.warmup,
    )


def _request(
    method: str,
    path: str,
    body: bytes | None = None,
    token: str | None = None,
    *,
    port: int = 8000,
) -> Response:
    connection = HTTPConnection("127.0.0.1", port, timeout=1)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return Response(response.status, dict(response.headers.items()), response.read())
    finally:
        connection.close()


def _smoke(token: str) -> None:
    response = _request("GET", "/info")
    if response.status != 200:
        raise RuntimeError(f"GET /info returned {response.status}")
    UUID(response.headers["x-response-id"])
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
    if msgspec.json.decode(response.body, type=Info) != expected:
        raise RuntimeError("GET /info returned an unexpected payload")
    body = msgspec.json.encode(Workload.movie())
    response = _request("POST", "/movies", body, token)
    if response.status != 201:
        raise RuntimeError(f"POST /movies returned {response.status}")
    movie = msgspec.json.decode(response.body, type=Movie)
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
        if _request("POST", "/movies", body, invalid).status != 401:
            raise RuntimeError("POST /movies accepted invalid credentials")
    invalid_body = body.replace(b"2026", b'"invalid"')
    if _request("POST", "/movies", invalid_body, token).status != 422:
        raise RuntimeError("POST /movies accepted an invalid body")
    _smoke_backends(token, expired)


def _smoke_backends(token: str, expired: str) -> None:
    response = _request("GET", "/catalog")
    if (
        response.status != 200
        or msgspec.json.decode(response.body, type=Product) != Workload.product()
    ):
        raise RuntimeError("GET /catalog returned an unexpected upstream product")
    response = _request("GET", "/users/me", token=token)
    if (
        response.status != 200
        or msgspec.json.decode(response.body, type=Profile) != Workload.profile()
    ):
        raise RuntimeError("GET /users/me returned an unexpected database profile")
    for invalid in (None, "invalid", expired, token + "invalid"):
        if _request("GET", "/users/me", token=invalid).status != 401:
            raise RuntimeError("GET /users/me accepted invalid credentials")
    missing = Workload.signing_key().encode(Claims(sub="missing-user", name="name", exp=4102444800))
    if _request("GET", "/users/me", token=missing).status != 404:
        raise RuntimeError("GET /users/me did not look up the authenticated user")


async def _ready(
    process: asyncio.subprocess.Process, port: int = 8000, path: str = "/info"
) -> None:
    for _ in range(300):
        if process.returncode is not None:
            raise RuntimeError("Server exited before becoming ready; see the server log")
        try:
            if _request("GET", path, port=port).status == 200:
                return
        except OSError:
            pass
        await asyncio.sleep(0.1)
    raise TimeoutError("Server did not become ready in 30 seconds")


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


async def _verify_affinity(process: asyncio.subprocess.Process, cpus: list[int]) -> None:
    if sys.platform == "linux":
        for _ in range(100):
            if process.returncode is not None:
                raise RuntimeError("Benchmark process exited before its CPU affinity was verified")
            if os.sched_getaffinity(process.pid) == set(cpus):
                return
            await asyncio.sleep(0.01)
    raise RuntimeError("Could not verify the benchmark process's one-CPU affinity")


async def _load(
    options: Options, report: Report, workload: WorkloadName, name: str, token: str, duration: int
) -> Metrics:
    result_file = options.output.resolve() / f"{name}.json"
    environment = {
        **os.environ,
        "WORKLOAD": workload,
        "DURATION": str(duration),
        "VUS": str(options.vus),
        "TOKEN": token,
        "MOVIE": msgspec.json.encode(Workload.movie()).decode(),
        "PRODUCT": msgspec.json.encode(Workload.product()).decode(),
        "PROFILE": msgspec.json.encode(Workload.profile()).decode(),
        "RESULT_FILE": str(result_file),
        "GOMAXPROCS": "1",
    }
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = perf_counter()
    with (options.output / f"{name}.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            *_affinity(report.load_cpus),
            _command("k6"),
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
            await _verify_affinity(process, report.load_cpus)
            code = await asyncio.wait_for(process.wait(), timeout=duration + 30)
        finally:
            await _stop(process)
    if code != 0:
        raise RuntimeError(f"k6 failed for {name}; see {name}.log and {name}.json")
    metrics = msgspec.json.decode(result_file.read_bytes(), type=Metrics)
    if metrics.requests < 1 or metrics.failed_rate != 0 or metrics.checks_rate != 1:
        raise RuntimeError(f"{name} returned failed requests or response checks")
    cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_seconds = (cpu_after.ru_utime + cpu_after.ru_stime) - (
        cpu_before.ru_utime + cpu_before.ru_stime
    )
    metrics.generator_cpu_percent = 100 * cpu_seconds / (perf_counter() - started)
    return metrics


async def _framework(options: Options, report: Report, framework: Framework) -> None:
    # Fail before starting a process rather than accidentally benchmarking an existing app.
    with socket.socket() as port:
        port.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port.bind(("127.0.0.1", 8000))
    with (options.output / f"{framework}-server.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            *_affinity(report.server_cpus),
            sys.executable,
            "-m",
            "granian",
            "--interface",
            "asgi",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--workers",
            "1",
            "--loop",
            "asyncio",
            "--log-level",
            "warning",
            f"benchmarks.ci.{framework}_app:app",
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            await _ready(process)
            await _verify_affinity(process, report.server_cpus)
            token = Workload.token()
            _smoke(token)
            workloads: tuple[WorkloadName, ...] = ("info", "movies", "catalog", "profile")
            for workload in workloads:
                await _load(
                    options,
                    report,
                    workload,
                    f"{framework}-{workload}-warmup",
                    token,
                    options.warmup,
                )
                for attempt in range(1, options.runs + 1):
                    name = f"{framework}-{workload}-{attempt}"
                    metrics = await _load(options, report, workload, name, token, options.duration)
                    report.attempts.append(Attempt(framework, workload, attempt, metrics))
                    print(f"{name}: {metrics.requests_per_second:,.0f} req/s", flush=True)
        finally:
            await _stop(process)


async def _database(options: Options, report: Report) -> None:
    if not options.database_container:
        raise ValueError("Pass --database-container with the local PostgreSQL container name or ID")
    cpu = str(report.database_cpus[0])
    await _capture(
        _command("docker"),
        "update",
        "--cpuset-cpus",
        cpu,
        "--cpus",
        "1",
        options.database_container,
    )
    limits = msgspec.json.decode(
        await _capture(
            _command("docker"),
            "inspect",
            "--format",
            "{{json .HostConfig}}",
            options.database_container,
        ),
        type=ContainerLimits,
    )
    if limits.cpuset_cpus != cpu or limits.nano_cpus != 1_000_000_000:
        raise RuntimeError("PostgreSQL must be pinned to exactly one CPU with a one-CPU quota")
    report.database_image = await _capture(
        _command("docker"),
        "inspect",
        "--format",
        "{{.Image}}",
        options.database_container,
    )
    await seed_database(Settings.from_environment())


async def _workloads(options: Options, report: Report) -> None:
    # Reserve the upstream port before starting it, as with the measured apps.
    with socket.socket() as port:
        port.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port.bind(("127.0.0.1", 8001))
    with (options.output / "upstream-server.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            *_affinity(report.upstream_cpus),
            sys.executable,
            "-m",
            "granian",
            "--interface",
            "asgi",
            "--host",
            "127.0.0.1",
            "--port",
            "8001",
            "--workers",
            "1",
            "--loop",
            "asyncio",
            "--log-level",
            "warning",
            "benchmarks.ci.upstream_app:app",
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            await _ready(process, port=8001, path="/health")
            await _verify_affinity(process, report.upstream_cpus)
            for token in (None, "invalid"):
                if _request("GET", "/data", token=token, port=8001).status != 401:
                    raise RuntimeError("The upstream accepted an invalid API key")
            for framework in ("jero", "fastapi"):
                await _framework(options, report, framework)
        finally:
            await _stop(process)


def _markdown(report: Report) -> str:
    lines = [
        "# jero HTTP benchmarks",
        "",
        "Status: **complete**" if report.complete else "Status: **incomplete; see the run logs**",
        "",
        f"Commit: `{report.commit}` · Python {report.python} · {report.created_at}",
        "",
        (
            f"{report.vus} VUs · {report.duration_seconds}s per attempt · {report.runs} attempts · "
            f"{report.warmup_seconds}s warmup per workload · granian / asyncio / one worker."
        ),
        "",
        "Each app, k6, the upstream, and PostgreSQL is pinned to one distinct logical CPU.",
        "",
        (
            "Rows use the median-throughput attempt; latency comes from that same attempt. "
            "Every measured response must pass its status and payload checks."
        ),
        "",
        (
            "GitHub-hosted runners vary between runs. Compare frameworks within this run; "
            "these figures are not directly comparable to the dedicated EC2 benchmarks."
        ),
        "",
    ]
    for workload, label in (
        ("info", "GET /info"),
        ("movies", "JWT POST /movies"),
        ("catalog", "GET /catalog (HTTP upstream)"),
        ("profile", "JWT GET /users/me (PostgreSQL)"),
    ):
        lines += [
            f"## {label}",
            "",
            "| Framework | req/s | Mean (ms) | p99 (ms) | k6 CPU (1 core) | Attempt |",
            "| :--- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for framework in ("jero", "fastapi"):
            attempts = sorted(
                (
                    item
                    for item in report.attempts
                    if item.framework == framework and item.workload == workload
                ),
                key=lambda item: item.metrics.requests_per_second,
            )
            if attempts:
                selected = attempts[len(attempts) // 2]
                metric = selected.metrics
                lines.append(
                    f"| {framework} | {metric.requests_per_second:,.0f} | "
                    f"{metric.mean_ms:.3f} | {metric.p99_ms:.3f} | "
                    f"{metric.generator_cpu_percent:.0f}% | {selected.attempt} |"
                )
        lines.append("")
    lines += [
        "## Environment",
        "",
        f"- CPU: {report.cpu}",
        f"- OS: {report.platform}",
        f"- Server CPUs: {report.server_cpus}",
        f"- Load CPUs: {report.load_cpus}",
        f"- Upstream CPUs: {report.upstream_cpus}",
        f"- PostgreSQL CPUs: {report.database_cpus} (also capped at one CPU)",
        f"- PostgreSQL image: {report.database_image}",
        f"- k6: {report.k6}",
    ]
    lines += (f"- {name}: {value}" for name, value in report.versions.items())
    if report.run_url is not None:
        lines.append(f"- [Workflow run]({report.run_url})")
    lines += [
        "",
        (
            "k6 CPU is the generator's average CPU usage (100% = its one allocated core). "
            "Values near 100% can indicate generator-limited throughput."
        ),
        "",
        (
            "The artifact includes `results.json`, every k6 summary, warmup results, "
            "and server/load-generator logs."
        ),
        "",
    ]
    return "\n".join(lines)


async def main(
    *,
    duration: int = 10,
    runs: int = 3,
    vus: int = 32,
    warmup: int = 2,
    output: Path = Path("benchmark-results"),
    database_container: str = "",
) -> None:
    """Run both frameworks sequentially, preserving reports and logs even on failure.

    Args:
        duration: Seconds per measured attempt (1-60).
        runs: Attempts per framework and workload (1-5).
        vus: Concurrent virtual users (1-128).
        warmup: Unmeasured seconds before each workload (1-10).
        output: A new directory for the report, raw results, and logs.
        database_container: Running PostgreSQL Docker container to pin and cap to one CPU.
    """
    for name, value, limit in (
        ("duration", duration, 60),
        ("runs", runs, 5),
        ("vus", vus, 128),
        ("warmup", warmup, 10),
    ):
        if not 1 <= value <= limit:
            raise ValueError(f"{name} must be from 1 through {limit}")
    options = Options(
        duration=duration,
        runs=runs,
        vus=vus,
        warmup=warmup,
        output=output,
        database_container=database_container,
    )
    options.output.mkdir(parents=True, exist_ok=False)
    report = await _metadata(options)
    try:
        if sys.platform == "linux":
            os.sched_setaffinity(0, report.server_cpus)
        await _database(options, report)
        await _workloads(options, report)
        report.complete = True
    finally:
        (options.output / "results.json").write_bytes(
            msgspec.json.format(msgspec.json.encode(report))
        )
        (options.output / "report.md").write_text(_markdown(report), encoding="utf-8")
