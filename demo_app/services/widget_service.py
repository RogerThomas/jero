"""The widget service: the I/O boundary that owns the upstream HTTP client.

This is the unit boundary the test suite mocks — every HTTP test swaps a stand-in
``WidgetService`` in through the factory, so no real network calls happen.
"""

from dataclasses import dataclass

from msgspec.json import decode as json_decode
from msgspec.json import encode as json_encode
from pyreqwest.bytes import Bytes
from pyreqwest.client import Client
from pyreqwest.response import Response

from demo_app.errors import UpstreamResponseError, WidgetNotFoundError
from demo_app.models import Widget, WidgetIn, WidgetPatch
from jero import ConflictError


async def _body(resp: Response) -> Bytes:
    """Read the buffered response as a Rust-backed buffer for msgspec to decode."""
    if resp.status == 503:
        raise UpstreamResponseError(retryable=True)
    content = await resp.bytes()
    if not content:
        raise UpstreamResponseError(retryable=False)
    return content


@dataclass
class WidgetService:
    """The unit boundary: owns the upstream client; mocked in HTTP tests."""

    _client: Client
    _base_url: str
    _api_key: str

    async def _request(self, method: str, path: str, body: bytes | None = None) -> Response:
        """Issue one authenticated request and fully buffer its response before returning."""
        request = self._client.request(method, f"{self._base_url}{path}").bearer_auth(self._api_key)
        if body is not None:
            request = request.header("Content-Type", "application/json").body_bytes(body)
        return await request.build().send()

    async def list_widgets(self, limit: int, offset: int) -> list[Widget]:
        """Fetch a page of widgets from the upstream catalog."""
        resp = await self._request("GET", f"/widgets?limit={limit}&offset={offset}")
        return json_decode(await _body(resp), type=list[Widget])

    async def get_widget(self, widget_id: str) -> Widget:
        """Fetch one widget, raising 404 when the upstream has none."""
        resp = await self._request("GET", f"/widgets/{widget_id}")
        if resp.status == 404:
            raise WidgetNotFoundError(widget_id=widget_id)
        return json_decode(await _body(resp), type=Widget)

    async def create_widget(self, data: WidgetIn) -> Widget:
        """Create a widget upstream, raising 409 when the name is already taken."""
        resp = await self._request("POST", "/widgets", json_encode(data))
        if resp.status == 409:
            raise ConflictError()
        return json_decode(await _body(resp), type=Widget)

    async def replace_widget(self, widget_id: str, data: WidgetIn) -> Widget:
        """Replace a widget upstream and return the stored record."""
        resp = await self._request("PUT", f"/widgets/{widget_id}", json_encode(data))
        return json_decode(await _body(resp), type=Widget)

    async def patch_widget(self, widget_id: str, data: WidgetPatch) -> Widget:
        """Partially update a widget upstream and return the stored record."""
        resp = await self._request("PATCH", f"/widgets/{widget_id}", json_encode(data))
        return json_decode(await _body(resp), type=Widget)

    async def delete_widget(self, widget_id: str) -> None:
        """Delete a widget upstream."""
        await self._request("DELETE", f"/widgets/{widget_id}")
