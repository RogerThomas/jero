"""A ``json`` body declared as a union of tagged Structs: decoding by tag, the 422s for a
missing or unknown tag, the OpenAPI ``anyOf`` + ``discriminator``, and the wiring errors for
unions msgspec can't tell apart.

The happy paths run against ``demo_app``'s ``/price-quotes`` (camelCase fields via ``rename``,
a custom ``tag_field``, the union named through a ``type`` alias); the wiring failures build
small local apps.
"""

import pytest
from msgspec import Struct

from jero import BaseApp, Endpoint
from jero.testing import TestClient


@pytest.mark.parametrize(
    ("body", "price_cents"),
    [
        pytest.param({"kind": "fixed", "priceCents": 1000, "discountCents": 250}, 750, id="fixed"),
        pytest.param({"kind": "percent", "priceCents": 1000, "percentOff": 10}, 900, id="percent"),
    ],
)
def test_each_member_decodes_by_its_tag(
    client: TestClient, body: dict[str, object], price_cents: int
) -> None:
    """The tag picks the member; the handler narrows to it and reads its own fields."""
    resp = client.post("/price-quotes", json=body)
    assert resp.status_code == 200
    assert resp.json() == {"priceCents": price_cents}


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        pytest.param(
            {"priceCents": 1000, "discountCents": 250},
            "Object missing required field `kind`",
            id="missing-tag",
        ),
        pytest.param(
            {"kind": "unknown", "priceCents": 1000},
            "Invalid value 'unknown' - at `$.kind`",
            id="unknown-tag",
        ),
        pytest.param(
            {"kind": "percent", "priceCents": 1000, "percentOff": 101},
            "Expected `int` <= 100 - at `$.percentOff`",
            id="member-constraint",
        ),
    ],
)
def test_invalid_union_body_is_422(
    client: TestClient, body: dict[str, object], detail: str
) -> None:
    """A missing or unknown tag fails like any other body validation: the 422 Problem."""
    resp = client.post("/price-quotes", json=body)
    assert resp.status_code == 422
    assert resp.json() == {
        "type": "validation-failed",
        "title": "Validation failed",
        "status": 422,
        "detail": detail,
        "params": {"reason": detail},
    }


def test_openapi_union_body_is_discriminated_any_of(client: TestClient) -> None:
    """The request body documents as msgspec's anyOf + discriminator, the same shape a
    tagged-union response gets, with each member a component carrying its tag const."""
    spec = client.get("/openapi.json").json()
    request_body = spec["paths"]["/price-quotes"]["post"]["requestBody"]
    assert request_body == {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/FixedDiscount"},
                        {"$ref": "#/components/schemas/PercentDiscount"},
                    ],
                    "discriminator": {
                        "propertyName": "kind",
                        "mapping": {
                            "fixed": "#/components/schemas/FixedDiscount",
                            "percent": "#/components/schemas/PercentDiscount",
                        },
                    },
                }
            }
        },
    }
    fixed = spec["components"]["schemas"]["FixedDiscount"]
    assert fixed["properties"]["kind"] == {"const": "fixed"}
    assert fixed["required"] == ["kind", "priceCents", "discountCents"]


class Plain(Struct):
    """An untagged member."""

    a: int


class OtherPlain(Struct):
    """A second untagged member."""

    b: int


class Tagged(Struct, tag="tagged"):
    """A string-tagged member on the default ``type`` tag field."""

    a: int


class SameTag(Struct, tag="tagged"):
    """A member claiming ``Tagged``'s tag value."""

    b: int


class KindTagged(Struct, tag="kind-tagged", tag_field="kind"):
    """A member on a different tag field than ``Tagged``."""

    b: int


class IntTagged(Struct, tag=1):
    """A member tagged with an int rather than a string."""

    b: int


class UntaggedUnionEndpoint(Endpoint, path="/x"):
    """A union msgspec can't decode: no member is tagged."""

    async def post(self, json: Plain | OtherPlain) -> Plain | OtherPlain:
        """Never reached; wiring fails first."""
        return json


class OptionalBodyEndpoint(Endpoint, path="/x"):
    """A body that may be ``None`` is not a union of Structs."""

    async def post(self, json: Tagged | None) -> Plain:
        """Never reached; wiring fails first."""
        return Plain(a=0 if json is None else json.a)


class MixedTagFieldEndpoint(Endpoint, path="/x"):
    """Members disagreeing on the tag field."""

    async def post(self, json: Tagged | KindTagged) -> Tagged | KindTagged:
        """Never reached; wiring fails first."""
        return json


class DuplicateTagEndpoint(Endpoint, path="/x"):
    """Two members claiming one tag value."""

    async def post(self, json: Tagged | SameTag) -> Tagged | SameTag:
        """Never reached; wiring fails first."""
        return json


class IntTagEndpoint(Endpoint, path="/x"):
    """A member tagged with an int."""

    async def post(self, json: Tagged | IntTagged) -> Tagged | IntTagged:
        """Never reached; wiring fails first."""
        return json


class _EndpointApp(BaseApp):
    def __init__(self, endpoint: Endpoint) -> None:
        self._endpoint = endpoint
        super().__init__()

    async def wire(self) -> None:
        self._include_endpoint(self._endpoint)


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        pytest.param(
            UntaggedUnionEndpoint(),
            r"'json': Plain must declare an explicit string tag \(tag=True",
            id="untagged",
        ),
        pytest.param(
            OptionalBodyEndpoint(),
            "'json' must be annotated with a msgspec.Struct subclass or a union of tagged Structs",
            id="optional",
        ),
        pytest.param(
            MixedTagFieldEndpoint(),
            "is not decodable: .*must have the same `tag_field`",
            id="mixed-tag-field",
        ),
        pytest.param(
            DuplicateTagEndpoint(),
            "is not decodable: .*must have unique `tag` values",
            id="duplicate-tag",
        ),
        pytest.param(
            IntTagEndpoint(), "IntTagged must declare an explicit string tag", id="int-tag"
        ),
    ],
)
def test_undecodable_union_body_fails_wiring(endpoint: Endpoint, message: str) -> None:
    """A union whose members msgspec can't tell apart by a string tag fails at startup."""
    with pytest.raises(RuntimeError, match=message):
        TestClient(_EndpointApp(endpoint))


class Second(Struct, tag="second"):
    """A second string-tagged member on the default ``type`` tag field."""

    b: int


class Third(Struct, tag="third"):
    """A third string-tagged member on the default ``type`` tag field."""

    c: int


class ThreeMemberEndpoint(Endpoint, path="/x"):
    """A union of more than two tagged members, echoed back."""

    async def post(self, json: Tagged | Second | Third) -> Tagged | Second | Third:
        """Echo the decoded member, re-encoded with its tag."""
        return json


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"type": "tagged", "a": 1}, id="tagged"),
        pytest.param({"type": "second", "b": 1}, id="second"),
        pytest.param({"type": "third", "c": 1}, id="third"),
    ],
)
def test_union_of_three_members_decodes_each(body: dict[str, object]) -> None:
    """Every member of a wider union is reachable by its tag, not only the first two."""
    with TestClient(_EndpointApp(ThreeMemberEndpoint())) as client:
        resp = client.post("/x", json=body)
    assert resp.status_code == 200
    assert resp.json() == body


type TaggedOrSecond = Tagged | Second
type PlainAlias = Plain


class NestedAliasEndpoint(Endpoint, path="/x"):
    """A union whose first member is an alias for a union itself, echoed back."""

    async def post(self, json: TaggedOrSecond | Third) -> Tagged | Second | Third:
        """Echo the decoded member, re-encoded with its tag."""
        return json


class RepeatedMemberEndpoint(Endpoint, path="/x"):
    """A "union" that is one untagged Struct named twice, once through an alias."""

    async def post(self, json: PlainAlias | Plain) -> Plain:
        """Echo the body."""
        return json


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"type": "tagged", "a": 1}, id="through-the-alias"),
        pytest.param({"type": "third", "c": 1}, id="beside-the-alias"),
    ],
)
def test_alias_nested_in_a_union_body_flattens(body: dict[str, object]) -> None:
    """``AB | C`` with ``type AB = A | B`` is the union ``A | B | C``: the alias is spliced in
    rather than read as one non-Struct member."""
    with TestClient(_EndpointApp(NestedAliasEndpoint())) as client:
        resp = client.post("/x", json=body)
    assert resp.status_code == 200
    assert resp.json() == body


def test_union_collapsing_to_one_struct_needs_no_tag() -> None:
    """A union that names one Struct twice is that Struct, so it binds untagged."""
    with TestClient(_EndpointApp(RepeatedMemberEndpoint())) as client:
        resp = client.post("/x", json={"a": 1})
    assert resp.status_code == 200
    assert resp.json() == {"a": 1}
