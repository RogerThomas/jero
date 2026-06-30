"""The composition root: builds the demo app's services.

``WidgetService`` owns a lifecycle resource (the upstream client) opened on the app's
exit stack; ``AnalyticsService`` is a plain in-memory recorder. Both are built here so
the app's ``wire`` stays a short list of includes. Tests swap a stand-in factory in
through ``BaseApp``'s ``factory=`` seam to mock the I/O service.
"""

from functools import cached_property

import niquests
from openai import AsyncOpenAI

from demo_app.config import Settings, get_settings
from demo_app.services.analytics_service import AnalyticsService
from demo_app.services.questions_service import QuestionsService
from demo_app.services.widgets_service import WidgetService
from jero import BaseFactory


class Factory(BaseFactory):
    """Builds the demo app's services from settings."""

    @cached_property
    def _settings(self) -> Settings:
        """Resolve settings once, on first use, then reuse across every create_* call."""
        return get_settings()

    async def create_widget_service(self) -> WidgetService:
        """Build a WidgetService with a client opened on the app's stack."""
        client = await self.aenter(niquests.AsyncSession())
        return WidgetService(client, self._settings.widget_base_url, self._settings.widget_api_key)

    async def create_analytics_service(self) -> AnalyticsService:
        """Build the in-memory analytics recorder."""
        return AnalyticsService(processed=[])

    async def create_questions_service(self) -> QuestionsService:
        """Build a QuestionsService with an OpenAI client opened on the app's stack."""
        client = await self.aenter(AsyncOpenAI(api_key=self._settings.openai_api_key))
        return QuestionsService(client, self._settings.openai_model)
