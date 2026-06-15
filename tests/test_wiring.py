"""Contract violations surface as startup failures (WiringError).

The TestClient drives the lifespan on construction, so a bad ``_wire``
raises ``RuntimeError`` wrapping the framework's ``WiringError`` message.
"""

import pytest
from msgspec import Struct

from jero import BaseApp, Endpoint, Resource, TestClient


class P(Struct):
    """Minimal struct used as a body/return payload in wiring tests."""

    name: str


class ItemPath(Struct):
    """Path params carrying an item id."""

    id: str


class _ResourceApp(BaseApp):
    def __init__(self, resource: Resource, path: str = "/x") -> None:
        self._resource = resource
        self._path = path
        super().__init__()

    async def _wire(self) -> None:
        self._include_resource(self._resource, path=self._path)


class _EndpointApp(BaseApp):
    def __init__(self, endpoint: Endpoint, path: str = "/x") -> None:
        self._endpoint = endpoint
        self._path = path
        super().__init__()

    async def _wire(self) -> None:
        self._include_endpoint(self._endpoint, path=self._path)


class BadArgResource(Resource):
    """Resource whose handler uses an unsupported argument name."""

    async def create(self, body: P) -> P:  # 'body' is not a supported source name
        """Handler with an invalid 'body' source argument."""
        return body


class BodyOnGetResource(Resource):
    """Resource whose GET handler illegally declares a body source."""

    async def read_one(self, json: P) -> P:  # GET handlers cannot take a body
        """GET handler that illegally requests a JSON body."""
        return json


class NonStructReturnResource(Resource):
    """Resource whose handler declares an unsupported return type."""

    async def read_many(self) -> int:  # must return a Struct / list[Struct] / bytes / Response
        """Handler returning an unsupported plain int type."""
        return 0


class ExactPathEndpoint(Endpoint):
    """Endpoint that illegally declares a path slot for an exact path."""

    async def get(self, path: ItemPath) -> P:  # /x has no {id} slot; endpoint paths are exact
        """GET handler requesting a path slot that the exact path lacks."""
        return P(name=path.id)


class EmptyResource(Resource):
    """Resource defining none of the CRUD methods."""


def test_unsupported_argument_name() -> None:
    """Wiring a handler with an unsupported argument name fails at startup."""
    with pytest.raises(RuntimeError, match="unsupported argument 'body'"):
        TestClient(_ResourceApp(BadArgResource()))


def test_body_on_get_handler() -> None:
    """Wiring a GET handler that takes a body fails at startup."""
    with pytest.raises(RuntimeError, match="GET handlers cannot take 'json'"):
        TestClient(_ResourceApp(BodyOnGetResource()))


def test_non_struct_return_type() -> None:
    """Wiring a handler with an unsupported return type fails at startup."""
    with pytest.raises(RuntimeError, match="must declare a return type"):
        TestClient(_ResourceApp(NonStructReturnResource()))


def test_endpoint_path_must_be_exact() -> None:
    """Wiring an endpoint with a templated path slot fails at startup."""
    with pytest.raises(RuntimeError, match="path is exact"):
        TestClient(_EndpointApp(ExactPathEndpoint()))


def test_resource_with_no_crud_methods() -> None:
    """Wiring a resource that defines no CRUD methods fails at startup."""
    with pytest.raises(RuntimeError, match="defines none of"):
        TestClient(_ResourceApp(EmptyResource()))
