"""Per-request hot-path benchmarks, driven in-process through the ASGI callable.

Each benchmark sends a batch of identical requests so the fixed cost of entering the
event loop is amortized and the measurement is dominated by jero's request handling.
"""

from collections.abc import Generator

import msgspec
import pytest
from pytest_codspeed import BenchmarkFixture

from benchmarks.codspeed.app import BENCH_BEARER, BenchApp, CIApp
from benchmarks.codspeed.harness import Harness, Request
from benchmarks.codspeed.workloads import Workload

BATCH = 50
JSON_HEADERS = ((b"content-type", b"application/json"),)


@pytest.fixture(name="bench", scope="module")
def _bench() -> Generator[Harness]:
    harness = Harness.start(BenchApp())
    yield harness
    harness.close()


@pytest.fixture(name="ci_app", scope="module")
def _ci_app() -> Generator[Harness]:
    harness = Harness.start(CIApp())
    yield harness
    harness.close()


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        pytest.param(Request("GET", "/ping"), 200, id="static-get"),
        pytest.param(Request("HEAD", "/ping"), 200, id="static-head"),
        pytest.param(Request("GET", "/items/abc", query=b"limit=10"), 200, id="path-and-query"),
        pytest.param(Request("GET", "/items", query=b"limit=50"), 200, id="list-50"),
        pytest.param(
            Request(
                "POST",
                "/items",
                headers=JSON_HEADERS,
                body=b'{"name":"gizmo","priceCents":499,"tags":["a","b"]}',
            ),
            201,
            id="json-body",
        ),
        pytest.param(
            Request("POST", "/items", headers=JSON_HEADERS, body=b'{"name":"gizmo"}'),
            422,
            id="json-body-invalid",
        ),
        pytest.param(
            Request("GET", "/echo-headers", headers=((b"x-trace-id", b"trace"),)),
            200,
            id="typed-headers",
        ),
        pytest.param(
            Request("GET", "/me", headers=((b"authorization", f"Bearer {BENCH_BEARER}".encode()),)),
            200,
            id="auth",
        ),
        pytest.param(
            Request("GET", "/items/abc", headers=((b"origin", b"https://example.com"),)),
            200,
            id="cors-origin",
        ),
        pytest.param(Request("GET", "/items/missing"), 404, id="handler-404"),
        pytest.param(Request("GET", "/nowhere"), 404, id="unrouted-404"),
        pytest.param(Request("DELETE", "/ping"), 405, id="method-not-allowed"),
        pytest.param(Request("GET", "/openapi.json"), 200, id="openapi-json"),
    ],
)
def test_request(
    benchmark: BenchmarkFixture, bench: Harness, request_: Request, expected: int
) -> None:
    """One request shape through the full jero hot path."""
    benchmark(bench.send, request_, repeat=BATCH, expected=expected)


def test_ci_info(benchmark: BenchmarkFixture, ci_app: Harness) -> None:
    """The ``GET /info`` workload: medium JSON plus a typed header."""
    benchmark(ci_app.send, Request("GET", "/info"), repeat=BATCH)


def test_ci_create_movie(benchmark: BenchmarkFixture, ci_app: Harness) -> None:
    """The JWT-authenticated ``POST /movies`` workload."""
    request = Request(
        "POST",
        "/movies",
        headers=(
            *JSON_HEADERS,
            (b"authorization", f"Bearer {Workload.token()}".encode()),
        ),
        body=msgspec.json.encode(Workload.movie()),
    )
    benchmark(ci_app.send, request, repeat=BATCH, expected=201)
