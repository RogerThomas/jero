"""Startup benchmarks: wiring-time introspection and the OpenAPI document build.

jero does all of its introspection once, at startup, so this is where that cost lives.
"""

import asyncio
from collections.abc import Generator

import pytest
from pytest_codspeed import BenchmarkFixture

from benchmarks.codspeed.app import BenchApp, CIApp
from benchmarks.codspeed.harness import start_and_stop


@pytest.fixture(name="loop")
def _loop() -> Generator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def test_startup_bench_app(benchmark: BenchmarkFixture, loop: asyncio.AbstractEventLoop) -> None:
    """Wire the benchmark app (routes, auth, CORS) and build its OpenAPI document."""
    benchmark(lambda: loop.run_until_complete(start_and_stop(BenchApp())))


def test_startup_ci_app(benchmark: BenchmarkFixture, loop: asyncio.AbstractEventLoop) -> None:
    """Wire the info and JWT movie workloads, without OpenAPI."""
    benchmark(lambda: loop.run_until_complete(start_and_stop(CIApp())))
