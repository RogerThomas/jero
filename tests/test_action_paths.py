"""Custom-method paths: an Endpoint whose last segment is ``{slot}:verb`` (the AIP-136 shape).

The happy path runs against ``demo_app``'s ``POST /widgets/{widget_id}:duplicate``, which sits
beside the widget resource's own ``/widgets/{widget_id}`` routes. Precedence, decoding,
reversal, 405s and the wiring failures build small local apps.
"""

from unittest.mock import MagicMock

import pytest
from msgspec import Struct

from demo_app.models import Widget, WidgetIn
from jero import BaseApp, Endpoint, JSONResponse, Location, Request, Resource
from jero.testing import TestClient


def test_action_route_runs_beside_the_item_routes(
    client: TestClient, widget_service_mock: MagicMock
) -> None:
    """``POST /widgets/w:duplicate`` reaches the action; ``GET /widgets/w`` still reaches
    ``read_one``, so the suffix is what tells the two apart."""
    widget_service_mock.get_widget.return_value = Widget(id="widget-id", name="name", price_cents=1)
    widget_service_mock.create_widget.return_value = Widget(
        id="copy-id", name="name", price_cents=1
    )
    auth = {"authorization": "Bearer token"}

    duplicated = client.post("/widgets/widget-id:duplicate", headers=auth)
    read = client.get("/widgets/widget-id", headers=auth)

    assert duplicated.status_code == 201
    assert duplicated.json() == {"id": "copy-id", "name": "name", "priceCents": 1}
    assert duplicated.headers["location"] == "/widgets/copy-id"
    widget_service_mock.create_widget.assert_awaited_once_with(WidgetIn(name="name", price_cents=1))
    assert read.status_code == 200
    assert widget_service_mock.get_widget.await_count == 2


def test_action_route_is_documented_under_its_suffixed_path(client: TestClient) -> None:
    """The spec keys the operation by the path as written, slot and suffix both."""
    paths = client.get("/openapi.json").json()["paths"]
    operation = paths["/widgets/{widgetId}:duplicate"]["post"]
    assert operation["summary"] == "Duplicate a widget."
    assert [param["name"] for param in operation["parameters"]] == ["widgetId"]


class ThingPath(Struct):
    """Two slots, so the action's slot is not the only one."""

    group_id: str
    thing_id: str


class Seen(Struct):
    """Which route answered, and what it bound."""

    route: str
    group_id: str
    thing_id: str


class ConvertEndpoint(Endpoint, path="/groups/{group_id}/things/{thing_id}:convert"):
    """An action route."""

    async def post(self, path: ThingPath) -> Seen:
        """Echo the bound slots."""
        return Seen(route="convert", group_id=path.group_id, thing_id=path.thing_id)


class ArchiveEndpoint(Endpoint, path="/groups/{group_id}/things/{thing_id}:archive"):
    """A second action on the same template."""

    async def post(self, path: ThingPath) -> Seen:
        """Echo the bound slots."""
        return Seen(route="archive", group_id=path.group_id, thing_id=path.thing_id)


class ThingEndpoint(Endpoint, path="/groups/{group_id}/things/{thing_id}"):
    """The plain route the actions sit beside."""

    async def post(self, path: ThingPath) -> Seen:
        """Echo the bound slots."""
        return Seen(route="plain", group_id=path.group_id, thing_id=path.thing_id)

    async def put(self, path: ThingPath) -> JSONResponse[Seen]:
        """Point at the ``:convert`` action for these slots."""
        return JSONResponse(
            json=Seen(route="plain", group_id=path.group_id, thing_id=path.thing_id),
            location=Location.from_operation(ConvertEndpoint.post, path=path),
        )


class ActionApp(BaseApp):
    """The two actions plus the plain route."""

    async def wire(self) -> None:
        self._include_endpoint(ThingEndpoint())
        self._include_endpoint(ConvertEndpoint())
        self._include_endpoint(ArchiveEndpoint())


@pytest.mark.parametrize(
    ("url", "route", "group_id", "thing_id"),
    [
        pytest.param("/groups/g/things/t:convert", "convert", "g", "t", id="convert"),
        pytest.param("/groups/g/things/t:archive", "archive", "g", "t", id="archive"),
        pytest.param(
            "/groups/g/things/t:other", "plain", "g", "t:other", id="unknown-action-is-the-id"
        ),
        pytest.param(
            "/groups/g/things/:convert", "plain", "g", ":convert", id="empty-id-is-no-action"
        ),
        pytest.param(
            "/groups/g%20h/things/a%20b:convert", "convert", "g h", "a b", id="escapes-decode"
        ),
        pytest.param("/groups/g/things/a:b:convert", "convert", "g", "a:b", id="colon-in-id"),
    ],
)
def test_action_suffix_picks_the_route(url: str, route: str, group_id: str, thing_id: str) -> None:
    """An action route wins over the plain slot route beside it; anything that is not one of
    its suffixes, or would leave the slot empty, falls through to the plain route."""
    with TestClient(ActionApp()) as client:
        resp = client.post(url)
    assert resp.status_code == 200
    assert resp.json() == {"route": route, "group_id": group_id, "thing_id": thing_id}


def test_action_route_reverse_routes_with_its_suffix() -> None:
    """``Location.from_operation`` on an action builds the slot value plus the suffix."""
    with TestClient(ActionApp()) as client:
        resp = client.put("/groups/g/things/t")
    assert resp.headers["location"] == "/groups/g/things/t:convert"


class ConvertOnlyApp(BaseApp):
    """An action with no plain route beside it."""

    async def wire(self) -> None:
        self._include_endpoint(ConvertEndpoint())


def test_action_route_wrong_method_is_405_with_allow() -> None:
    """A path only an action matches answers other verbs with 405 and that action's Allow."""
    with TestClient(ConvertOnlyApp()) as client:
        resp = client.get("/groups/g/things/t:convert")
        missing = client.get("/groups/g/things/t:archive")
    assert resp.status_code == 405
    assert resp.headers["allow"] == "POST, OPTIONS"
    assert missing.status_code == 404


class StampHeaders(Struct):
    """The header the scoped middleware adds."""

    x_stamp: str


class StampMiddleware:
    """A dynamic ``response_headers`` hook, so its routes are wrapped at wiring."""

    def response_headers(self, request: Request) -> StampHeaders:
        """Stamp every covered response with the path it answered."""
        return StampHeaders(x_stamp=request.path)


class ScopedActionApp(BaseApp):
    """An action route covered by include-scoped middleware."""

    async def wire(self) -> None:
        self._include_endpoint(ConvertEndpoint(), middleware=[StampMiddleware()])


def test_scoped_middleware_covers_an_action_route() -> None:
    """Middleware scoped to an action's include wraps that route like any other."""
    with TestClient(ScopedActionApp()) as client:
        resp = client.post("/groups/g/things/t:convert")
    assert resp.status_code == 200
    assert resp.headers["x-stamp"] == "/groups/g/things/t:convert"


class GroupPath(Struct):
    """One slot."""

    group_id: str


class DuplicateConvertEndpoint(Endpoint, path="/groups/{group_id}/things/{thing_id}:convert"):
    """The same action registered a second time."""

    async def post(self, path: ThingPath) -> Seen:
        """Never reached; wiring fails first."""
        return Seen(route="duplicate", group_id=path.group_id, thing_id=path.thing_id)


class EmptyActionEndpoint(Endpoint, path="/groups/{group_id}:"):
    """A suffix with no verb."""

    async def post(self, path: GroupPath) -> GroupPath:
        """Never reached; wiring fails first."""
        return path


class SlashActionEndpoint(Endpoint, path="/groups/{group_id}:a b"):
    """A verb with a character outside the unreserved set."""

    async def post(self, path: GroupPath) -> GroupPath:
        """Never reached; wiring fails first."""
        return path


class MidPathActionEndpoint(Endpoint, path="/groups/{group_id}:convert/things"):
    """A suffix on a segment that is not the last."""

    async def post(self, path: GroupPath) -> GroupPath:
        """Never reached; wiring fails first."""
        return path


class ActionResource(Resource, path="/groups/{group_id}:convert"):
    """A suffix on a Resource, whose item routes append a segment after it."""

    async def read_many(self, path: GroupPath) -> list[GroupPath]:
        """Never reached; wiring fails first."""
        return [path]


class _RoutableApp(BaseApp):
    def __init__(self, *routables: Endpoint | Resource) -> None:
        self._routables = routables
        super().__init__()

    async def wire(self) -> None:
        for routable in self._routables:
            if isinstance(routable, Resource):
                self._include_resource(routable)
            else:
                self._include_endpoint(routable)


@pytest.mark.parametrize(
    ("routables", "message"),
    [
        pytest.param(
            (ConvertEndpoint(), DuplicateConvertEndpoint()),
            r"POST /groups/\{group_id\}/things/\{thing_id\}:convert is already registered",
            id="duplicate",
        ),
        pytest.param((EmptyActionEndpoint(),), r"action '' must be", id="empty-verb"),
        pytest.param((SlashActionEndpoint(),), r"action 'a b' must be", id="bad-verb"),
        pytest.param(
            (MidPathActionEndpoint(),),
            r"segment '\{group_id\}:convert' carries an action suffix",
            id="not-last-segment",
        ),
        pytest.param(
            (ActionResource(),),
            r"only the last segment of an Endpoint path may",
            id="resource",
        ),
    ],
)
def test_misplaced_or_clashing_action_fails_wiring(
    routables: tuple[Endpoint | Resource, ...], message: str
) -> None:
    """An action is one unreserved verb on an Endpoint's last segment, registered once."""
    with pytest.raises(RuntimeError, match=message):
        TestClient(_RoutableApp(*routables))
