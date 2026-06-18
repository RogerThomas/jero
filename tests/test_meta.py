"""Route metadata declared at class definition: ``meta`` (all operations) and
``meta_<op>`` (one), stored on the class for the coming OpenAPI spec.
"""

import types

import pytest

from jero import Endpoint, EndpointMeta, OperationMeta, Resource, ResourceMeta, WiringError


class ThingsEndpoint(
    Endpoint,
    path="/things",
    meta=EndpointMeta(tags=["things"]),
    meta_get=OperationMeta(tags=["unsafe"], operation_id="getThing"),
):
    """Test endpoint with metadata."""

    async def get(self) -> str:
        """GET operation."""
        return ""

    async def post(self) -> str:
        """POST operation."""
        return ""


class WidgetsResource(
    Resource,
    path="/widgets",
    meta=ResourceMeta(tags=["widgets"]),
    meta_create=OperationMeta(operation_id="createWidget"),
):
    """Test resource with metadata."""

    async def create(self) -> str:
        """CREATE operation."""
        return ""


def test_class_level_meta_is_stored() -> None:
    """Class-level metadata is accessible on the endpoint/resource."""
    assert ThingsEndpoint.meta == EndpointMeta(tags=["things"])
    assert WidgetsResource.meta == ResourceMeta(tags=["widgets"])


def test_per_operation_meta_is_stored() -> None:
    """Per-operation metadata is accessible on the endpoint/resource."""
    assert ThingsEndpoint.meta_get == OperationMeta(tags=["unsafe"], operation_id="getThing")
    assert WidgetsResource.meta_create == OperationMeta(operation_id="createWidget")


def test_unset_meta_defaults_to_none() -> None:
    """Unset operation metadata defaults to None."""
    # meta_post wasn't declared; the class-level default applies.
    assert ThingsEndpoint.meta_post is None
    assert WidgetsResource.meta_read_one is None


def test_path_still_resolves_alongside_meta() -> None:
    """Path resolution works when metadata is also declared."""
    assert ThingsEndpoint.path == "/things"
    assert WidgetsResource.path == "/widgets"


def test_endpoint_rejects_resource_meta() -> None:
    """Endpoint rejects ResourceMeta type."""
    # new_class with a kwds dict exercises the *runtime* guard (pyright's static kwarg
    # check already forbids the wrong type at the call site).
    with pytest.raises(WiringError, match="meta must be EndpointMeta"):
        types.new_class("_Bad", (Endpoint,), {"path": "/x", "meta": ResourceMeta()})


def test_resource_rejects_endpoint_meta() -> None:
    """Resource rejects EndpointMeta type."""
    with pytest.raises(WiringError, match="meta must be ResourceMeta"):
        types.new_class("_Bad", (Resource,), {"path": "/x", "meta": EndpointMeta()})


def test_operation_id_is_operation_only() -> None:
    """operation_id is only available on OperationMeta, not class-level metadata."""
    # operation_id lives only on OperationMeta — never cascades from the class level.
    assert not hasattr(EndpointMeta(), "operation_id")
    assert not hasattr(ResourceMeta(), "operation_id")
    assert OperationMeta().operation_id is None
