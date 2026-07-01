"""The widgets resource: CRUD delegating to the service.

``create`` shows two cross-cutting features at once: it records an analytics event off
the request path (background tasks) and returns ``201`` with a ``Location`` and ``Link``
reverse-routed to the new resource (links). ``ref="widgets"`` lets other modules address
this resource by string via ``Link.from_ref('widgets.read_one')``.
"""

from dataclasses import dataclass

from demo_app.models import Deleted, Page, Widget, WidgetEvent, WidgetIn, WidgetPatch, WidgetPath
from demo_app.services.widgets_service import WidgetService
from jero import (
    BackgroundTasks,
    JSONResponse,
    Link,
    Location,
    OperationMeta,
    Resource,
    ResourceMeta,
    ResponseSpec,
    Tag,
)


@dataclass
class WidgetResource(
    Resource,
    path="/widgets",
    ref="widgets",
    # every widget operation is tagged "widgets"; defining it here describes the group
    meta=ResourceMeta(tags=[Tag("widgets", "Create, read, and manage widgets.")]),
    # summary is explicit (docstrings aren't published); create can also fail with a
    # conflict the framework can't infer — declare it here.
    meta_create=OperationMeta(
        summary="Create a widget.",
        responses=[ResponseSpec(409, "A widget with that name already exists")],
    ),
):
    """CRUD over widgets, delegating to the injected service."""

    _service: WidgetService
    _background_tasks: BackgroundTasks

    async def create(self, json: WidgetIn) -> JSONResponse[Widget]:
        """Create a widget, record the event, and point the response at the new resource."""
        widget = await self._service.create_widget(json)
        await self._background_tasks.add(WidgetEvent(action="created", widget_id=widget.id))
        return JSONResponse(
            json=widget,
            status_code=201,
            location=Location.from_operation(
                WidgetResource.read_one, path=WidgetPath(widget_id=widget.id)
            ),
            links=[
                Link.from_operation(
                    WidgetResource.read_one, rel="self", path=WidgetPath(widget_id=widget.id)
                ),
                Link.from_operation(WidgetResource.read_many, rel="collection"),
            ],
        )

    async def read_one(self, path: WidgetPath) -> Widget:
        """Return a single widget by path id."""
        return await self._service.get_widget(path.widget_id)

    async def read_many(self, params: Page) -> list[Widget]:
        """List widgets honouring the pagination params."""
        return await self._service.list_widgets(params.limit, params.offset)

    async def update(self, path: WidgetPath, json: WidgetIn) -> Widget:
        """Replace a widget by path id."""
        return await self._service.replace_widget(path.widget_id, json)

    async def partial_update(self, path: WidgetPath, json: WidgetPatch) -> Widget:
        """Partially update a widget by path id."""
        return await self._service.patch_widget(path.widget_id, json)

    async def delete(self, path: WidgetPath) -> Deleted:
        """Delete a widget by path id."""
        await self._service.delete_widget(path.widget_id)
        return Deleted(id=path.widget_id, deleted=True)
