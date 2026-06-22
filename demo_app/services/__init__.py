"""Service layer: the I/O-owning widget service and the in-memory analytics recorder."""

from demo_app.services.analytics_service import AnalyticsService
from demo_app.services.widgets_service import WidgetService

__all__ = ["AnalyticsService", "WidgetService"]
