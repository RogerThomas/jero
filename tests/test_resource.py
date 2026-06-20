"""CRUD verbs on a BaseResource: route -> bind -> delegate to the service."""

from unittest.mock import MagicMock

from jero import TestClient
from tests.demo_app import Widget, WidgetIn, WidgetPatch


def test_create_returns_201_and_delegates(client: TestClient, widgets_mock: MagicMock) -> None:
    """POST binds the body, calls create_widget, and returns its result as 201."""
    widgets_mock.create_widget.return_value = Widget(id="widget-id", name="name", price_cents=1)
    resp = client.post(
        "/widgets",
        json={"name": "name", "priceCents": 1},
        headers={"authorization": "Bearer token"},
    )
    assert resp.status_code == 201
    assert resp.json() == {"id": "widget-id", "name": "name", "priceCents": 1}
    widgets_mock.create_widget.assert_awaited_once_with(WidgetIn(name="name", price_cents=1))


def test_read_one_binds_path_and_delegates(client: TestClient, widgets_mock: MagicMock) -> None:
    """GET on an item route binds the id and delegates to get_widget."""
    widgets_mock.get_widget.return_value = Widget(id="widget-id", name="name", price_cents=1)
    resp = client.get("/widgets/widget-id", headers={"authorization": "Bearer token"})
    assert resp.status_code == 200
    assert resp.json() == {"id": "widget-id", "name": "name", "priceCents": 1}
    widgets_mock.get_widget.assert_awaited_once_with("widget-id")


def test_read_many_returns_a_list(client: TestClient, widgets_mock: MagicMock) -> None:
    """GET on the collection returns the service's list as a JSON array."""
    widgets_mock.list_widgets.return_value = [Widget(id="widget-id", name="name", price_cents=1)]
    resp = client.get("/widgets", headers={"authorization": "Bearer token"})
    assert resp.status_code == 200
    assert resp.json() == [{"id": "widget-id", "name": "name", "priceCents": 1}]


def test_read_many_binds_pagination_query_params(
    client: TestClient, widgets_mock: MagicMock
) -> None:
    """The collection listing binds limit/offset and passes them to the service."""
    widgets_mock.list_widgets.return_value = []
    client.get(
        "/widgets", params={"limit": "5", "offset": "2"}, headers={"authorization": "Bearer token"}
    )
    widgets_mock.list_widgets.assert_awaited_once_with(5, 2)


def test_update_replaces_via_put(client: TestClient, widgets_mock: MagicMock) -> None:
    """PUT binds the id and body and delegates to replace_widget."""
    widgets_mock.replace_widget.return_value = Widget(id="widget-id", name="name", price_cents=2)
    resp = client.put(
        "/widgets/widget-id",
        json={"name": "name", "priceCents": 2},
        headers={"authorization": "Bearer token"},
    )
    assert resp.status_code == 200
    widgets_mock.replace_widget.assert_awaited_once_with(
        "widget-id", WidgetIn(name="name", price_cents=2)
    )


def test_partial_update_via_patch(client: TestClient, widgets_mock: MagicMock) -> None:
    """PATCH binds a partial body and delegates to patch_widget."""
    widgets_mock.patch_widget.return_value = Widget(id="widget-id", name="name", price_cents=2)
    resp = client.patch(
        "/widgets/widget-id", json={"priceCents": 2}, headers={"authorization": "Bearer token"}
    )
    assert resp.status_code == 200
    widgets_mock.patch_widget.assert_awaited_once_with("widget-id", WidgetPatch(price_cents=2))


def test_delete_returns_confirmation(client: TestClient, widgets_mock: MagicMock) -> None:
    """DELETE delegates to delete_widget and returns the deletion confirmation."""
    resp = client.delete("/widgets/widget-id", headers={"authorization": "Bearer token"})
    assert resp.status_code == 200
    assert resp.json() == {"id": "widget-id", "deleted": True}
    widgets_mock.delete_widget.assert_awaited_once_with("widget-id")
