"""Service layer: the widget I/O service, the analytics recorder, and the OpenAI-backed
questions service."""

from demo_app.services.analytics_service import AnalyticsService
from demo_app.services.questions_service import QuestionsService
from demo_app.services.widgets_service import WidgetService

__all__ = ["AnalyticsService", "QuestionsService", "WidgetService"]
