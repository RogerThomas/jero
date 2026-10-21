#!yeet
"""Run one Compose overlay per framework, verify CPU limits, and combine reports."""

import asyncio
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

import msgspec
from msgspec import Struct

from benchmarks.ci.reporting import Framework, Report, write


class Options(Struct):
    """Validated controls shared with each benchmark container."""

    duration: int
    runs: int
    vus: int
    warmup: int
    output: Path
    load_cpus: int


class Allocation(Struct):
    """Keep the app and generator separate from supporting-service CPUs."""

    app: int
    load: list[int]
    database: list[int]
    upstream: list[int]


class Limits(Struct, rename={"cpuset": "CpusetCpus", "nano_cpus": "NanoCpus"}):
    """Docker's actual CPU configuration, inspected before startup."""

    cpuset: str
    nano_cpus: int


class Container(Struct, rename={"limits": "HostConfig", "image": "Image"}):
    """Only the relevant fields from docker inspect."""

    limits: Limits
    image: str


async def _command(*command: str, environment: dict[str, str] | None = None) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(output.decode())
    return output.decode().strip()


async def _allocation(count: int, backend_count: int) -> Allocation:
    if sys.platform == "linux":
        cpus = sorted(os.sched_getaffinity(0))
    else:
        cpus = list(range(int(await _command("docker", "info", "--format", "{{.NCPU}}"))))
    if len(cpus) < 4 or not 1 <= count <= len(cpus) - 2:
        raise ValueError("Need at least four CPUs, reserving one for the app and one for services")
    remaining = cpus[1 + count :]
    if backend_count < 1:
        raise ValueError("backend_cpus must be positive")
    if len(cpus) == 4 and len(remaining) == 1 and backend_count == 1:
        return Allocation(cpus[0], cpus[1 : 1 + count], remaining, remaining)
    if len(remaining) < 2 * backend_count:
        raise ValueError("Not enough CPUs for separate database and upstream allocations")
    return Allocation(
        cpus[0], cpus[1 : 1 + count], remaining[:backend_count], remaining[-backend_count:]
    )


async def _verify(
    compose: list[str],
    environment: dict[str, str],
    allocation: Allocation,
) -> dict[str, str]:
    images: dict[str, str] = {}
    for service, cpus in (
        ("app", [allocation.app]),
        ("benchmark", allocation.load),
        ("postgres", allocation.database),
        ("upstream", allocation.upstream),
    ):
        identifier = await _command(
            *compose, "ps", "--all", "--quiet", service, environment=environment
        )
        container = msgspec.json.decode(
            await _command("docker", "inspect", "--format", "{{json .}}", identifier),
            type=Container,
        )
        if (
            container.limits.cpuset != ",".join(map(str, cpus))
            or container.limits.nano_cpus != len(cpus) * 1_000_000_000
        ):
            raise RuntimeError(f"{service} does not have its required CPU affinity and quota")
        images[service] = container.image
    return images


async def _up(compose: list[str], environment: dict[str, str], log: Path, timeout: int) -> None:
    with log.open("wb") as stream:
        process = await asyncio.create_subprocess_exec(
            *compose,
            "up",
            "--no-build",
            "--exit-code-from",
            "benchmark",
            env=environment,
            stdout=stream,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = await asyncio.wait_for(process.wait(), timeout=timeout)
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
    if code != 0:
        raise RuntimeError(f"Compose benchmark failed; see {log}")


async def _framework(
    framework: Framework,
    options: Options,
    environment: dict[str, str],
    allocation: Allocation,
) -> dict[str, str]:
    directory = Path(__file__).resolve().parent
    compose = [
        "docker",
        "compose",
        "--project-name",
        f"jero-bench-{os.getpid()}-{framework}",
        "-f",
        str(directory / "compose.yml"),
        "-f",
        str(directory / f"compose.{framework}.yml"),
    ]
    try:
        await _command(*compose, "create", environment=environment)
        images = await _verify(compose, environment, allocation)
        await _up(
            compose,
            environment,
            options.output / f"{framework}-compose.log",
            timeout=4 * (options.warmup + options.runs * options.duration) + 180,
        )
        return images
    finally:
        try:
            logs = await _command(*compose, "logs", "--no-color", environment=environment)
            (options.output / f"{framework}-services.log").write_text(logs, encoding="utf-8")
        finally:
            await _command(
                *compose, "down", "--volumes", "--remove-orphans", environment=environment
            )


def _combine(output: Path, images: dict[str, str], *, complete: bool) -> None:
    combined: Report | None = None
    reports = 0
    for framework in ("jero", "fastapi"):
        result = output / framework / "results.json"
        if result.is_file():
            report = msgspec.json.decode(result.read_bytes(), type=Report)
            reports += 1
            if combined is None:
                combined = report
            else:
                combined.attempts.extend(report.attempts)
                combined.complete = combined.complete and report.complete
    if combined is None:
        (output / "report.md").write_text(
            "# jero HTTP benchmarks\n\nStatus: **incomplete; see Compose logs**\n",
            encoding="utf-8",
        )
    else:
        combined.complete = combined.complete and reports == 2 and complete
        combined.images = images
        combined.database_image = images.get("postgres", "")
        write(combined, output)


async def main(
    *,
    duration: int = 10,
    runs: int = 3,
    vus: int = 32,
    warmup: int = 2,
    output: Path = Path("benchmark-results"),
    load_cpus: int = 2,
    backend_cpus: int = 1,
) -> None:
    """Run the Bake-built Compose stacks sequentially and retain logs on failure.

    Args:
        duration: Seconds per measured attempt (1-60).
        runs: Attempts per framework and workload (1-5).
        vus: Concurrent virtual users (1-128).
        warmup: Unmeasured seconds before each workload (1-10).
        output: A new directory for all artifacts.
        load_cpus: Generator CPUs; the app always has one CPU.
        backend_cpus: CPUs per supporting service; larger allocations must not overlap.
    """
    for name, value, limit in (
        ("duration", duration, 60),
        ("runs", runs, 5),
        ("vus", vus, 128),
        ("warmup", warmup, 10),
    ):
        if not 1 <= value <= limit:
            raise ValueError(f"{name} must be from 1 through {limit}")
    allocation = await _allocation(load_cpus, backend_cpus)
    options = Options(duration, runs, vus, warmup, output.resolve(), load_cpus)
    options.output.mkdir(parents=True, exist_ok=False)
    environment = {
        **os.environ,
        "BENCHMARK_DURATION": str(duration),
        "BENCHMARK_RUNS": str(runs),
        "BENCHMARK_VUS": str(vus),
        "BENCHMARK_WARMUP": str(warmup),
        "BENCHMARK_OUTPUT": str(options.output),
        "BENCHMARK_APP_CPU": str(allocation.app),
        "BENCHMARK_LOAD_CPUS": ",".join(map(str, allocation.load)),
        "BENCHMARK_LOAD_CPU_COUNT": str(load_cpus),
        "BENCHMARK_DATABASE_CPUS": ",".join(map(str, allocation.database)),
        "BENCHMARK_UPSTREAM_CPUS": ",".join(map(str, allocation.upstream)),
        "BENCHMARK_BACKEND_CPU_COUNT": str(backend_cpus),
        "BENCHMARK_COMMIT": await _command("git", "rev-parse", "HEAD"),
    }
    if "GITHUB_RUN_ID" in os.environ:
        environment["BENCHMARK_RUN_URL"] = (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    images: dict[str, str] = {}
    complete = False
    try:
        for framework in ("jero", "fastapi"):
            current = await _framework(framework, options, environment, allocation)
            if images and current != images:
                raise RuntimeError("The frameworks ran different images")
            images = current
        complete = True
    finally:
        _combine(options.output, images, complete=complete)
