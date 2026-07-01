"""The composition root: builds the demo app's services.

``WidgetService`` owns a lifecycle resource (the upstream client) opened on the app's
exit stack; ``AnalyticsService`` is a plain in-memory recorder. Both are built here so
the app's ``_wire`` stays a short list of includes. Tests swap a stand-in factory in
through ``BaseApp``'s ``factory=`` seam to mock the I/O service.
"""

import niquests
from openai import AsyncOpenAI

from demo_app.config import get_settings
from demo_app.errors import UpstreamResponseErrorHandler
from demo_app.services.analytics_service import AnalyticsService
from demo_app.services.questions_service import QuestionsService
from demo_app.services.widgets_service import WidgetService
from jero import BaseFactory


class Factory(BaseFactory):
    """Builds the demo app's services from settings."""

    async def create_widget_service(self) -> WidgetService:
        """Build a WidgetService with a client opened on the app's stack."""
        settings = get_settings()
        client = await self._aenter(niquests.AsyncSession())
        return WidgetService(client, settings.widget_base_url, settings.widget_api_key)

    def create_upstream_response_error_handler(self) -> UpstreamResponseErrorHandler:
        """Build the upstream handler from its environment-selected retry setting."""
        settings = get_settings()
        return UpstreamResponseErrorHandler(settings.widget_retry_after_seconds)

    async def create_analytics_service(self) -> AnalyticsService:
        """Build the in-memory analytics recorder."""
        return AnalyticsService(processed=[])

    async def create_questions_service(self) -> QuestionsService:
        """Build a QuestionsService with an OpenAI client opened on the app's stack."""
        settings = get_settings()
        client = await self._aenter(AsyncOpenAI(api_key=settings.openai_api_key))
        return QuestionsService(client, settings.openai_model)
