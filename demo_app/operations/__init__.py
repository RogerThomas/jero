"""Routing layer: the widgets resource and the standalone endpoints."""

from demo_app.operations.system_operations import (
    FeaturedWidgetEndpoint,
    HealthEndpoint,
    RawFormEndpoint,
    RawHealthEndpoint,
    WhoAmIEndpoint,
)
from demo_app.operations.widgets_operations import WidgetResource

__all__ = [
    "FeaturedWidgetEndpoint",
    "HealthEndpoint",
    "RawFormEndpoint",
    "RawHealthEndpoint",
    "WhoAmIEndpoint",
    "WidgetResource",
]
