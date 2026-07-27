"""The demo app: a factory-injected widgets API used by the test suite and the docs.

It wires authed widgets (CRUD + background analytics + links), an authed ``/me``, an
optionally-authed ``/spotlight``, open health checks, a raw-form echo, and a ``from_ref``
link demo. Auth is a pure in-memory token map built in ``wire`` (no lifecycle), so swapping
the factory in tests replaces only the I/O services and leaves auth intact.
"""

from demo_app.auth import OptionalTokenAuth, TokenAuth
from demo_app.factory import Factory
from demo_app.models import User
from demo_app.operations.streaming_operations import NotificationsEndpoint, QuestionsEndpoint
from demo_app.operations.system_operations import (
    FeaturedWidgetEndpoint,
    HealthEndpoint,
    RawFormEndpoint,
    RawHealthEndpoint,
    SpotlightEndpoint,
    WhoAmIEndpoint,
)
from demo_app.operations.widget_operations import WidgetResource
from jero import BaseApp


class DemoApp(BaseApp[Factory]):
    """Factory-injected demo app: authed widgets and ``/me``, optionally-authed
    ``/spotlight``; open health, raw-form, links."""

    async def wire(self) -> None:
        """Build services from the factory, open the background queue, and wire the routes."""
        widget_service = await self.factory.create_widget_service()
        analytics_service = await self.factory.create_analytics_service()
        question_service = await self.factory.create_question_service()
        upstream_response_error_handler = self.factory.create_upstream_response_error_handler()
        # The queue is opened after the analytics service it dispatches to, so it drains
        # before that service would be torn down.
        background_tasks = await self.create_background_tasks(drain_timeout=1.0)
        background_tasks.register(analytics_service.process)
        users = {
            "token": User(id="user-id", name="user-name"),
            "no-spotlight-token": User(id="other-id", name="other-name", may_see_spotlight=False),
        }
        token_auth = TokenAuth(users)
        # Same token lookup, the other policy: credentials are an input rather than a gate, so
        # an anonymous caller binds user=None. A bad token is still a 401.
        optional_token_auth = OptionalTokenAuth(users)
        self.add_exception_handler(upstream_response_error_handler)
        self.include_resource(WidgetResource(widget_service, background_tasks), auth=token_auth)
        self.include_endpoint(WhoAmIEndpoint(), auth=token_auth)
        self.include_endpoint(SpotlightEndpoint(), auth=optional_token_auth)
        self.include_endpoint(HealthEndpoint())
        self.include_endpoint(RawHealthEndpoint())
        self.include_endpoint(RawFormEndpoint())
        self.include_endpoint(FeaturedWidgetEndpoint())
        self.include_endpoint(QuestionsEndpoint(question_service))
        self.include_endpoint(NotificationsEndpoint())
        # Serve the auto-generated OpenAPI 3.1 spec at /openapi.json and a Scalar UI at /docs.
        # Tag descriptions are defined on the resources/endpoints themselves (see their meta);
        # pass tags=[Tag(...)] here only for app-level tags or to pin the section order.
        self.include_openapi(title="Demo API", version="0.1.0")


app = DemoApp()
