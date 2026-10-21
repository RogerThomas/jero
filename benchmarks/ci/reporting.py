"""Typed attempt data and the shared report used inside and outside Compose."""

from pathlib import Path
from typing import Literal

import msgspec
from msgspec import Struct

type Framework = Literal["jero", "fastapi"]
type WorkloadName = Literal["info", "movies", "catalog", "profile"]


class Metrics(Struct):
    """Rates and CPU measurements from the same attempt."""

    requests_per_second: float
    requests: int
    mean_ms: float
    p99_ms: float
    failed_rate: float
    checks_rate: float
    generator_cpu_percent: float = 0.0
    server_cpu_percent: float = 0.0


class Attempt(Struct):
    """One measured attempt, excluding warmup."""

    framework: Framework
    workload: WorkloadName
    attempt: int
    metrics: Metrics


class Report(Struct):
    """Reproducibility metadata and every completed attempt."""

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
    images: dict[str, str] = msgspec.field(default_factory=dict[str, str])


def _rows(report: Report, workload: WorkloadName) -> list[str]:
    rows: list[str] = []
    for framework in ("jero", "fastapi"):
        attempts = sorted(
            (a for a in report.attempts if a.framework == framework and a.workload == workload),
            key=lambda a: a.metrics.requests_per_second,
        )
        if not attempts:
            continue
        selected = attempts[len(attempts) // 2]
        metric = selected.metrics
        saturated = metric.generator_cpu_percent >= 90 * len(report.load_cpus)
        note = "generator saturated" if saturated else ""
        rows.append(
            f"| {framework} | {metric.requests_per_second:,.0f} | "
            f"{metric.mean_ms:.3f} | {metric.p99_ms:.3f} | "
            f"{metric.server_cpu_percent:.0f}% | {metric.generator_cpu_percent:.0f}% | "
            f"{note} | {selected.attempt} |"
        )
    return rows


def markdown(report: Report) -> str:
    """Use median-throughput attempts, including their latency and CPU samples."""
    lines = [
        "# jero HTTP benchmarks",
        "",
        "Status: **complete**" if report.complete else "Status: **incomplete; see the run logs**",
        "",
        f"Commit: `{report.commit}` · Python {report.python} · {report.created_at}",
        "",
        (
            f"{report.vus} VUs · {report.duration_seconds}s per attempt · {report.runs} attempts · "
            f"{report.warmup_seconds}s warmup · granian / uvloop / one worker."
        ),
        "",
        (
            "Each app, upstream, and PostgreSQL is capped at one logical CPU. "
            f"k6 has {len(report.load_cpus)} CPU(s)."
        ),
        "",
        (
            "Rows use the median-throughput attempt; latency and CPU come from the same attempt. "
            "All measured responses must pass status and payload checks."
        ),
        "",
        (
            "**Generator saturation can hide framework capacity.** App CPU measures the sole "
            "worker, including all its threads; k6 CPU measures the generator. "
            "100% = one CPU. Generator saturation is flagged at 90% of its allocated CPUs."
        ),
        "",
        (
            "GitHub-hosted runners vary. Compare within this run; shared virtual CPUs and Docker "
            "networking do not reproduce the dedicated AWS machine's absolute throughput."
        ),
        "",
    ]
    workloads: tuple[tuple[WorkloadName, str], ...] = (
        ("info", "GET /info"),
        ("movies", "JWT POST /movies"),
        ("catalog", "GET /catalog (HTTP upstream)"),
        ("profile", "JWT GET /users/me (PostgreSQL)"),
    )
    for workload, label in workloads:
        lines += [
            f"## {label}",
            "",
            (
                "| Framework | req/s | Mean (ms) | p99 (ms) | App CPU (max 100%) | "
                f"k6 CPU (max {100 * len(report.load_cpus)}%) | Note | Attempt |"
            ),
            "| :--- | ---: | ---: | ---: | ---: | ---: | :--- | ---: |",
            *_rows(report, workload),
            "",
        ]
    lines += [
        "## Environment",
        "",
        f"- CPU: {report.cpu}",
        f"- OS: {report.platform}",
        f"- App CPUs: {report.server_cpus} (one-CPU quota)",
        f"- k6 CPUs: {report.load_cpus}",
        f"- Upstream CPUs: {report.upstream_cpus} (one-CPU quota)",
        f"- PostgreSQL CPUs: {report.database_cpus} (one-CPU quota)",
        f"- PostgreSQL image: {report.database_image}",
        f"- k6: {report.k6}",
    ]
    lines += [f"- {name}: {value}" for name, value in report.versions.items()]
    lines += [f"- Image {name}: {value}" for name, value in report.images.items()]
    if report.run_url is not None:
        lines.append(f"- [Workflow run]({report.run_url})")
    lines += ["", "Artifacts contain every attempt, warmup, raw k6 summary, and Compose logs.", ""]
    return "\n".join(lines)


def write(report: Report, output: Path) -> None:
    """Preserve complete or partial reports with the same serialization."""
    (output / "results.json").write_bytes(msgspec.json.format(msgspec.json.encode(report)))
    (output / "report.md").write_text(markdown(report), encoding="utf-8")
