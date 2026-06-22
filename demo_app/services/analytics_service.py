"""The analytics service: records widget events processed off the request path.

In-memory for the demo (it just appends to a list a test can inspect); a real one would
batch and forward events to an analytics backend. It has no lifecycle resource, so the
background worker can call it freely and it needs no teardown.
"""

from dataclasses import dataclass

from demo_app.models import WidgetEvent


@dataclass
class AnalyticsService:
    """Records widget events. ``processed`` is the in-memory sink tests assert on."""

    processed: list[str]

    async def process(self, event: WidgetEvent) -> None:
        """Handle one event — its type is inferred from this parameter at registration."""
        self.processed.append(f"{event.action}:{event.widget_id}")
