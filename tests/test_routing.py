"""Routing, method handling, HEAD/OPTIONS, and the Allow header."""

from unittest.mock import MagicMock

from demo_app.models import Widget
from jero import TestClient


def test_unknown_path_is_404(client: TestClient) -> None:
    """GET to an unregistered path returns 404."""
    assert client.get("/nope").status_code == 404


def test_wrong_method_is_405_with_allow(client: TestClient) -> None:
    """An unsupported method returns 405 with an Allow header listing valid verbs."""
    resp = client.delete("/healthz")
    assert resp.status_code == 405
    assert resp.headers["allow"] == "GET, HEAD, OPTIONS"


def test_options_returns_204_and_allow(client: TestClient) -> None:
    """OPTIONS returns 204 with an Allow header listing supported verbs."""
    resp = client.options("/widgets")
    assert resp.status_code == 204
    assert "POST" in resp.headers["allow"]
    assert "OPTIONS" in resp.headers["allow"]


def test_head_mirrors_get_without_a_body(client: TestClient, widgets_mock: MagicMock) -> None:
    """HEAD returns the GET status and content-length but an empty body."""
    widgets_mock.get_widget.return_value = Widget(id="widget-id", name="name", price_cents=1)
    resp = client.head("/widgets/widget-id", headers={"authorization": "Bearer token"})
    assert resp.status_code == 200
    assert resp.content == b""
    assert int(resp.headers["content-length"]) > 0


def test_static_and_templated_routes_coexist(client: TestClient, widgets_mock: MagicMock) -> None:
    """A static collection route and a templated item route both resolve."""
    # /widgets (collection) and /widgets/{id} (item) are distinct routes.
    widgets_mock.list_widgets.return_value = []
    widgets_mock.get_widget.return_value = Widget(id="widget-id", name="name", price_cents=1)
    assert client.get("/widgets", headers={"authorization": "Bearer token"}).status_code == 200
    assert (
        client.get("/widgets/widget-id", headers={"authorization": "Bearer token"}).status_code
        == 200
    )
