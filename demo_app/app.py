"""The demo app: a factory-injected widgets API used by the test suite and the docs.

It wires authed widgets (CRUD + background analytics + links), an authed ``/me``, open
health checks, a raw-form echo, and a ``from_ref`` link demo. Auth is a pure in-memory
token map built in ``_wire`` (no lifecycle), so swapping the factory in tests replaces
only the I/O services and leaves auth intact.
"""

from demo_app.auth import TokenAuth
from demo_app.factory import Factory
from demo_app.models import User
from demo_app.operations.streaming_operations import NotificationsEndpoint, QuestionsEndpoint
from demo_app.operations.system_operations import (
    FeaturedWidgetEndpoint,
    HealthEndpoint,
    RawFormEndpoint,
    RawHealthEndpoint,
    WhoAmIEndpoint,
)
from demo_app.operations.widgets_operations import WidgetResource
from jero import BackgroundTasks, BaseApp


class DemoApp(BaseApp[Factory]):
    """Factory-injected demo app: authed widgets and ``/me``; open health, raw-form, links."""

    async def _wire(self) -> None:
        """Build services from the factory, open the background queue, and wire the routes."""
        widgets_service = await self._factory.create_widget_service()
        analytics_service = await self._factory.create_analytics_service()
        questions_service = await self._factory.create_questions_service()
        # The queue is opened after the analytics service it dispatches to, so it drains
        # before that service would be torn down.
        background_tasks = await self._aenter(BackgroundTasks(drain_timeout=1.0))
        background_tasks.register(analytics_service.process)
        auth = TokenAuth({"token": User(id="user-id", name="user-name")})
        self._include_resource(WidgetResource(widgets_service, background_tasks), auth=auth)
        self._include_endpoint(WhoAmIEndpoint(), auth=auth)
        self._include_endpoint(HealthEndpoint())
        self._include_endpoint(RawHealthEndpoint())
        self._include_endpoint(RawFormEndpoint())
        self._include_endpoint(FeaturedWidgetEndpoint())
        self._include_endpoint(QuestionsEndpoint(questions_service))
        self._include_endpoint(NotificationsEndpoint())


app = DemoApp()
