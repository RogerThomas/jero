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

import niquests
from msgspec import Struct
from msgspec.json import decode as json_decode
from msgspec.json import encode as json_encode

from jero import (
    BackgroundTasks,
    BaseApp,
    BaseFactory,
    Endpoint,
    HTTPError,
    JSONResponse,
    Link,
    Location,
    Resource,
)


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

    async def _request(
        self, method: str, path: str, body: bytes | None = None
    ) -> niquests.Response:
        """Issue one upstream request; non-streaming, so the result is a ``Response``."""
        return await self._client.request(method, f"{self._base_url}{path}", data=body)

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
        client = await self._aenter(niquests.AsyncSession())
        return WidgetService(client, "http://base-url")


@dataclass
class WidgetResource(Resource, path="/widgets"):
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


class WhoAmIEndpoint(Endpoint, path="/me"):
    """Authenticated endpoint returning the current user."""

    async def get(self, user: User) -> User:
        """Return the authenticated caller."""
        return user


class HealthEndpoint(Endpoint, path="/healthz"):
    """Unauthenticated health-check endpoint."""

    async def get(self) -> Health:
        """Return an ok health status."""
        return Health(status="ok")


class RawHealthEndpoint(Endpoint, path="/raw-healthz"):
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
        self._include_resource(WidgetResource(widgets), auth=auth)
        self._include_endpoint(WhoAmIEndpoint(), auth=auth)
        self._include_endpoint(HealthEndpoint())
        self._include_endpoint(RawHealthEndpoint())


app = DemoApp()


class AnalyticsEvent(Camel):
    """An event recorded off the request path by the background worker."""

    name: str


@dataclass
class AnalyticsService:
    """Processes analytics events in the background, recording each into a sink."""

    processed: list[str]

    async def process(self, event: AnalyticsEvent) -> None:
        """Handle one event — its type is inferred from this parameter at registration."""
        self.processed.append(event.name)


@dataclass
class EventsEndpoint(Endpoint, path="/events"):
    """Accepts an event and hands it to the background queue, returning immediately."""

    _tasks: BackgroundTasks

    async def post(self, json: AnalyticsEvent) -> AnalyticsEvent:
        """Enqueue the event for background processing."""
        await self._tasks.add(json)
        return json


class BackgroundDemoApp(BaseApp):
    """Demonstrates background tasks: ``POST /events`` enqueues an ``AnalyticsEvent`` that
    a single worker processes via the registered handler. The queue is entered *after*
    the service its handler uses, so it drains before that service is torn down."""

    def __init__(self, analytics: AnalyticsService) -> None:
        self._analytics = analytics
        super().__init__()

    async def _wire(self) -> None:
        """Open the queue, register the handler (its type is inferred), and wire the route."""
        tasks = await self._aenter(BackgroundTasks(drain_timeout=1.0))
        tasks.register(self._analytics.process)
        self._include_endpoint(EventsEndpoint(tasks))


class Job(Camel):
    """A created job; its id flows into the reversed ``Location`` / ``Link`` URLs."""

    id: str


class JobPath(Camel):
    """Path Struct for a single job — its ``job_id`` fills the reversed URL slot."""

    job_id: str


class JobsResource(Resource, path="/jobs", ref="jobs"):
    """Jobs collection. ``create`` returns 201 with a ``Location`` reverse-routed to
    ``read_one`` and a ``Link`` header (self + a literal help link). ``ref="jobs"`` lets
    another module address it by string via ``Link.from_ref('jobs.read_one')``."""

    async def read_one(self, path: JobPath) -> Job:
        """Return a single job by path id (the reversal target)."""
        return Job(id=path.job_id)

    async def create(self, json: Job) -> JSONResponse[Job]:
        """Create a job, pointing the response at the new resource."""
        return JSONResponse(
            json=json,
            status_code=201,
            location=Location.from_operation(JobsResource.read_one, params=JobPath(job_id=json.id)),
            links=[
                Link.from_operation(
                    JobsResource.read_one, rel="self", params=JobPath(job_id=json.id)
                ),
                # a path link picks up the app's URL base; a full url is verbatim.
                Link.from_path("/docs/jobs", rel="help", title="Job docs", media_type="text/html"),
                Link.from_url("https://status.example.com", rel="status"),
            ],
        )


class JobLinkEndpoint(Endpoint, path="/job-link"):
    """A second 'module' that can't import ``JobsResource`` (imagine an import cycle), so
    it addresses the jobs route by string ref instead of by the operation reference."""

    async def get(self) -> JSONResponse[Job]:
        """Return a job carrying a cross-module ``Link`` resolved through the ref."""
        return JSONResponse(
            json=Job(id="job-id"),
            links=[Link.from_ref("jobs.read_one", rel="related", params=JobPath(job_id="job-id"))],
        )


class LinksDemoApp(BaseApp):
    """Demonstrates reverse-routed ``Location`` / ``Link``: typed ``from_operation``, a
    literal ``from_path`` / ``from_url``, and the ``from_ref`` string hatch across 'modules'.

    Whether the emitted URLs are relative or absolute is decided by the environment
    (``JERO_BASE_URL`` / ``JERO_TRUST_FORWARDED``), read once at construction — the app
    itself needs no extra wiring."""

    async def _wire(self) -> None:
        """Wire the jobs resource and the cross-module link endpoint."""
        self._include_resource(JobsResource())
        self._include_endpoint(JobLinkEndpoint())
