"""A complete, idiomatic jero app, reused across the test suite.

The app is factory-injected (``BaseApp[Factory]``): ``Factory`` builds the
services that own lifecycle resources, opening them on the app's exit stacks.
``WidgetService`` is the unit boundary — it owns the upstream HTTP client and is
what tests mock by swapping in a stand-in factory (see the factory-swap tests for
the ``mocker`` pattern).

Auth is a pure in-memory token map built directly in ``_wire`` (no lifecycle, so
it doesn't belong in the factory). That split is deliberate: swapping the factory
replaces only the I/O service, leaving auth wiring intact — so most tests just
send a valid bearer token and only mock service behaviour.
"""

from dataclasses import dataclass

import httpx2
from msgspec import Struct
from msgspec.json import decode as json_decode
from msgspec.json import encode as json_encode

from jero import BaseApp, BaseFactory, Endpoint, HTTPError, Resource


class Camel(Struct, rename="camel"):
    """camelCase on the wire, snake_case in code."""


class WidgetIn(Camel):
    """Inbound widget payload (no id yet)."""

    name: str
    price_cents: int


class Widget(WidgetIn):
    """A stored widget, including its assigned id."""

    id: str


class WidgetPatch(Camel):
    """Partial widget update; omitted fields are left unchanged."""

    name: str | None = None
    price_cents: int | None = None


class WidgetPath(Camel):
    """Path params carrying a widget id."""

    widget_id: str


class Page(Camel):
    """Pagination query params for listing widgets."""

    limit: int = 20
    offset: int = 0


class Deleted(Camel):
    """Response confirming a widget was removed."""

    id: str
    deleted: bool


class Credentials(Camel):
    """The bearer token lifted from the request's Authorization header."""

    authorization: str


class User(Camel):
    """The authenticated caller."""

    id: str
    name: str


class Health(Camel):
    """Health-check response body."""

    status: str


@dataclass
class WidgetService:
    """The unit boundary: owns the upstream client; mocked in HTTP tests."""

    _client: httpx2.AsyncClient
    _base_url: str

    async def list_widgets(self, limit: int, offset: int) -> list[Widget]:
        """Fetch a page of widgets from the upstream catalog."""
        resp = await self._client.get(f"{self._base_url}/widgets?limit={limit}&offset={offset}")
        return json_decode(resp.content, type=list[Widget])

    async def get_widget(self, widget_id: str) -> Widget:
        """Fetch one widget, raising 404 when the upstream has none."""
        resp = await self._client.get(f"{self._base_url}/widgets/{widget_id}")
        if resp.status_code == 404:
            raise HTTPError(404, "widget not found")
        return json_decode(resp.content, type=Widget)

    async def create_widget(self, data: WidgetIn) -> Widget:
        """Create a widget upstream and return the stored record."""
        resp = await self._client.post(f"{self._base_url}/widgets", content=json_encode(data))
        return json_decode(resp.content, type=Widget)

    async def replace_widget(self, widget_id: str, data: WidgetIn) -> Widget:
        """Replace a widget upstream and return the stored record."""
        resp = await self._client.put(
            f"{self._base_url}/widgets/{widget_id}", content=json_encode(data)
        )
        return json_decode(resp.content, type=Widget)

    async def patch_widget(self, widget_id: str, data: WidgetPatch) -> Widget:
        """Partially update a widget upstream and return the stored record."""
        resp = await self._client.patch(
            f"{self._base_url}/widgets/{widget_id}", content=json_encode(data)
        )
        return json_decode(resp.content, type=Widget)

    async def delete_widget(self, widget_id: str) -> None:
        """Delete a widget upstream."""
        await self._client.delete(f"{self._base_url}/widgets/{widget_id}")


@dataclass
class TokenAuth:
    """Bearer-token authenticator over a static token-to-user map."""

    _users: dict[str, User]

    async def authenticate(self, headers: Credentials) -> User:
        """Resolve the bearer token to a user, or raise 401."""
        token = headers.authorization.removeprefix("Bearer ").strip()
        user = self._users.get(token)
        if user is None:
            raise HTTPError(401, "invalid token")
        return user


class Factory(BaseFactory):
    """Composition root for services that own lifecycle resources."""

    async def create_widget_service(self) -> WidgetService:
        """Build a WidgetService with a client opened on the app's stack."""
        client = await self._aenter(httpx2.AsyncClient())
        return WidgetService(client, "http://base-url")


@dataclass
class WidgetResource(Resource):
    """CRUD resource over widgets, delegating to the injected service."""

    _service: WidgetService

    async def create(self, json: WidgetIn) -> Widget:
        """Create a widget from the request body."""
        return await self._service.create_widget(json)

    async def read_one(self, path: WidgetPath) -> Widget:
        """Return a single widget by path id."""
        return await self._service.get_widget(path.widget_id)

    async def read_many(self, params: Page) -> list[Widget]:
        """List widgets honouring the pagination params."""
        return await self._service.list_widgets(params.limit, params.offset)

    async def update(self, path: WidgetPath, json: WidgetIn) -> Widget:
        """Replace a widget by path id."""
        return await self._service.replace_widget(path.widget_id, json)

    async def partial_update(self, path: WidgetPath, json: WidgetPatch) -> Widget:
        """Partially update a widget by path id."""
        return await self._service.patch_widget(path.widget_id, json)

    async def delete(self, path: WidgetPath) -> Deleted:
        """Delete a widget by path id."""
        await self._service.delete_widget(path.widget_id)
        return Deleted(id=path.widget_id, deleted=True)


class WhoAmIEndpoint(Endpoint):
    """Authenticated endpoint returning the current user."""

    async def get(self, user: User) -> User:
        """Return the authenticated caller."""
        return user


class HealthEndpoint(Endpoint):
    """Unauthenticated health-check endpoint."""

    async def get(self) -> Health:
        """Return an ok health status."""
        return Health(status="ok")


class RawHealthEndpoint(Endpoint):
    """Unauthenticated health-check endpoint returning raw JSON."""

    async def get(self) -> bytes:
        """Return an ok health status as raw JSON."""
        return b'{"status":"ok"}'


class DemoApp(BaseApp[Factory]):
    """Factory-injected demo app: authed widgets, authed /me, open /healthz."""

    async def _wire(self) -> None:
        """Build services from the factory and wire the routes."""
        widgets = await self._factory.create_widget_service()
        auth = TokenAuth({"token": User(id="user-id", name="user-name")})
        self._include_resource(WidgetResource(widgets), path="/widgets", auth=auth)
        self._include_endpoint(WhoAmIEndpoint(), path="/me", auth=auth)
        self._include_endpoint(HealthEndpoint(), path="/healthz")
        self._include_endpoint(RawHealthEndpoint(), path="/raw-healthz")


app = DemoApp()
