"""The widget service: the I/O boundary that owns the upstream HTTP client.

This is the unit boundary the test suite mocks — every HTTP test swaps a stand-in
``WidgetService`` in through the factory, so no real network calls happen.
"""

from dataclasses import dataclass

import niquests
from msgspec.json import decode as json_decode
from msgspec.json import encode as json_encode

from demo_app.models import Widget, WidgetIn, WidgetPatch
from jero import HTTPError


def _body(resp: niquests.Response) -> bytes:
    """The response body, guarding niquests' optional ``content``."""
    content = resp.content
    if content is None:
        raise HTTPError(502, "empty upstream response")
    return content


@dataclass
class WidgetService:
    """The unit boundary: owns the upstream client; mocked in HTTP tests."""

    _client: niquests.AsyncSession
    _base_url: str
    _api_key: str

    async def _request(
        self, method: str, path: str, body: bytes | None = None
    ) -> niquests.Response:
        """Issue one authenticated upstream request; non-streaming, so it returns a ``Response``."""
        return await self._client.request(
            method,
            f"{self._base_url}{path}",
            data=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    async def list_widgets(self, limit: int, offset: int) -> list[Widget]:
        """Fetch a page of widgets from the upstream catalog."""
        resp = await self._request("GET", f"/widgets?limit={limit}&offset={offset}")
        return json_decode(_body(resp), type=list[Widget])

    async def get_widget(self, widget_id: str) -> Widget:
        """Fetch one widget, raising 404 when the upstream has none."""
        resp = await self._request("GET", f"/widgets/{widget_id}")
        if resp.status_code == 404:
            raise HTTPError(404, "widget not found")
        return json_decode(_body(resp), type=Widget)

    async def create_widget(self, data: WidgetIn) -> Widget:
        """Create a widget upstream and return the stored record."""
        resp = await self._request("POST", "/widgets", json_encode(data))
        return json_decode(_body(resp), type=Widget)

    async def replace_widget(self, widget_id: str, data: WidgetIn) -> Widget:
        """Replace a widget upstream and return the stored record."""
        resp = await self._request("PUT", f"/widgets/{widget_id}", json_encode(data))
        return json_decode(_body(resp), type=Widget)

    async def patch_widget(self, widget_id: str, data: WidgetPatch) -> Widget:
        """Partially update a widget upstream and return the stored record."""
        resp = await self._request("PATCH", f"/widgets/{widget_id}", json_encode(data))
        return json_decode(_body(resp), type=Widget)

    async def delete_widget(self, widget_id: str) -> None:
        """Delete a widget upstream."""
        await self._request("DELETE", f"/widgets/{widget_id}")
