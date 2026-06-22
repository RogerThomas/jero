"""A complete, idiomatic jero app, shared by the test suite and the documentation.

The building blocks live in submodules — ``demo_app.config``, ``demo_app.models``,
``demo_app.auth``, ``demo_app.services``, ``demo_app.operations``, ``demo_app.factory`` —
and the wired application is ``demo_app.app`` (re-exported here as ``app`` / ``DemoApp``).
"""

from demo_app.app import DemoApp, app
from demo_app.factory import Factory
from demo_app.services.analytics_service import AnalyticsService
from demo_app.services.widgets_service import WidgetService

__all__ = ["AnalyticsService", "DemoApp", "Factory", "WidgetService", "app"]
