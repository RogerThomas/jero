"""Exercise the real benchmark upstream's public HTTP contract."""

import msgspec
import pytest

from benchmarks.ci.common import Workload
from benchmarks.ci.upstream_app import App
from jero.testing import TestClient


@pytest.mark.parametrize("authorization", [None, "invalid", "Bearer wrong-key"])
def test_benchmark_upstream_requires_its_api_key(authorization: str | None) -> None:
    """Catalog requests cannot bypass the upstream's per-request authentication."""
    with TestClient(App()) as client:
        headers = {} if authorization is None else {"authorization": authorization}
        response = client.get("/data", headers=headers)
    assert response.status_code == 401


def test_benchmark_upstream_returns_the_complete_nested_product() -> None:
    """Both measured apps must preserve all upstream fields, including aliases."""
    with TestClient(App()) as client:
        response = client.get(
            "/data", headers={"authorization": f"Bearer {Workload.upstream_token()}"}
        )
    assert response.status_code == 200
    assert response.content == msgspec.json.encode(Workload.product())
