"""Service layer: the widget I/O service, the analytics recorder, and the OpenAI-backed
questions service."""

from demo_app.services.analytics_service import AnalyticsService
from demo_app.services.question_service import QuestionService
from demo_app.services.widget_service import WidgetService

__all__ = ["AnalyticsService", "QuestionService", "WidgetService"]
