"""The auto-generated OpenAPI 3.1 document and the served docs UI.

The rich assertions run against the shared ``demo_app`` (the ``client`` fixture), which
wires every route shape; small local apps cover the esoteric edges (no auth, bytes
returns, the docs-UI knobs, an apiKey scheme).
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, cast

import pytest
from msgspec import Meta, Struct
from openapi_spec_validator import validate

from jero import (
    BaseApp,
    BaseHTTPError,
    BearerAuth,
    Endpoint,
    EndpointMeta,
    ErrorBodyAdapter,
    FormPart,
    HTTPError,
    JSONResponse,
    ModelMeta,
    NDJSONStreamingResponse,
    NotFoundError,
    OperationMeta,
    ParameterizedHTTPError,
    Resource,
    ResourceMeta,
    ResponseSpec,
    ScalarConfig,
    SecurityScheme,
    SSEResponse,
    StreamingResponse,
    StructHTTPError,
    Tag,
)
from jero import Struct as JeroStruct
from jero.testing import TestClient


def test_document_is_valid_openapi_31(client: TestClient) -> None:
    """The served document validates against the OpenAPI 3.1 schema."""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    document = resp.json()
    validate(document)  # raises if the document is not valid OpenAPI 3.1
    assert document["openapi"] == "3.1.0"
    assert document["info"] == {"title": "Demo API", "version": "0.1.0"}


def test_paths_cover_the_wired_routes(client: TestClient) -> None:
    """Every wired resource/endpoint shows up; the docs routes themselves do not."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/widgets" in paths
    assert "/widgets/{widgetId}" in paths
    assert "/healthz" in paths
    assert "/openapi.json" not in paths  # the spec routes are not self-documented
    assert "/docs" not in paths


def test_operation_id_is_auto_derived(client: TestClient) -> None:
    """An undeclared operationId derives stably from the shape and method name."""
    paths = client.get("/openapi.json").json()["paths"]
    assert paths["/widgets/{widgetId}"]["get"]["operationId"] == "WidgetResource_readOne"
    assert paths["/widgets"]["post"]["operationId"] == "WidgetResource_create"


def test_operation_summary_comes_from_operation_meta(client: TestClient) -> None:
    """The operation summary is the explicit OperationMeta.summary — never a docstring."""
    create = client.get("/openapi.json").json()["paths"]["/widgets"]["post"]
    assert create["summary"] == "Create a widget."


def test_request_body_references_the_model_schema(client: TestClient) -> None:
    """A json body is documented as application/json pointing at the model component."""
    create = client.get("/openapi.json").json()["paths"]["/widgets"]["post"]
    schema = create["requestBody"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/WidgetIn"}


def test_parameters_are_expanded_with_wire_names(client: TestClient) -> None:
    """Path/query params expand to individual parameter objects using wire names."""
    paths = client.get("/openapi.json").json()["paths"]
    read_one = paths["/widgets/{widgetId}"]["get"]["parameters"]
    assert {"name": "widgetId", "in": "path", "required": True, "schema": {"type": "string"}} in (
        read_one
    )
    read_many = {p["name"]: p for p in paths["/widgets"]["get"]["parameters"]}
    assert read_many["limit"]["in"] == "query"
    assert read_many["limit"]["required"] is False  # has a default


def test_error_responses_are_derived_from_sources(client: TestClient) -> None:
    """create (json body, authed) lists body + auth errors; the bodyless read_one does not."""
    paths = client.get("/openapi.json").json()["paths"]
    create = paths["/widgets"]["post"]["responses"]
    assert set(create) >= {"201", "400", "401", "422", "500"}
    read_one = paths["/widgets/{widgetId}"]["get"]["responses"]
    assert "422" not in read_one  # no body to validate -> no false 422
    assert "400" not in read_one
    assert set(read_one) >= {"200", "401", "404", "500"}


def test_meta_declared_response_is_merged_in(client: TestClient) -> None:
    """The 409 declared on meta_create is documented alongside the derived responses."""
    create = client.get("/openapi.json").json()["paths"]["/widgets"]["post"]["responses"]
    assert create["409"]["description"] == "A widget with that name already exists"


def test_tags_come_from_resource_meta(client: TestClient) -> None:
    """The class-level ResourceMeta tag applies to every operation."""
    create = client.get("/openapi.json").json()["paths"]["/widgets"]["post"]
    assert create["tags"] == ["widgets"]


def test_security_scheme_and_requirement(client: TestClient) -> None:
    """Authed routes reference a bearer scheme; open routes carry no security."""
    document = client.get("/openapi.json").json()
    assert document["components"]["securitySchemes"]["bearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    paths = document["paths"]
    assert paths["/widgets"]["post"]["security"] == [{"bearerAuth": []}]
    assert "security" not in paths["/healthz"]["get"]


def test_msgspec_meta_constraints_appear_in_schema(client: TestClient) -> None:
    """msgspec.Meta field constraints and descriptions flow into the JSON schema."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    name = schemas["WidgetIn"]["properties"]["name"]
    assert name["minLength"] == 1
    assert name["description"] == "Human-readable widget name"
    price = schemas["WidgetIn"]["properties"]["priceCents"]
    assert price["minimum"] == 0


def test_derived_error_schema_is_per_class(client: TestClient) -> None:
    """Derived error responses point at per-class Problem models. The 422 documents the
    parameterized validation body: ``type``/``status`` consts (clients dispatch on
    ``type``), a human ``detail``, and the typed ``params``."""
    document = client.get("/openapi.json").json()
    schemas = document["components"]["schemas"]
    create = document["paths"]["/widgets"]["post"]["responses"]
    assert create["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ValidationFailedProblem"
    }
    problem = schemas["ValidationFailedProblem"]["properties"]
    assert problem["type"] == {"const": "validation-failed"}
    assert problem["status"] == {"const": 422}
    assert problem["detail"] == {"type": "string"}
    assert problem["params"] == {"$ref": "#/components/schemas/ErrorReason"}
    assert schemas["ErrorReason"]["properties"]["reason"] == {"type": "string"}


def test_streaming_content_types(client: TestClient) -> None:
    """NDJSON and SSE endpoints document their stream content types."""
    paths = client.get("/openapi.json").json()["paths"]
    questions = paths["/questions"]["post"]["responses"]["200"]["content"]
    assert "application/x-ndjson" in questions
    notifications = paths["/notifications"]["get"]["responses"]["200"]["content"]
    assert "text/event-stream" in notifications


def test_docs_ui_is_served(client: TestClient) -> None:
    """/docs returns a Scalar HTML page pointed at the spec."""
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    assert 'data-url="/openapi.json"' in resp.text
    assert "@scalar/api-reference" in resp.text


# --- Esoteric edges: small local apps ---


class Item(Struct):
    """A trivial response model."""

    id: str


class OpenEndpoint(Endpoint, path="/open"):
    """An open (unauthenticated) endpoint."""

    async def get(self) -> Item:
        """Get an item."""
        return Item(id="id")


class BlobEndpoint(Endpoint, path="/blob"):
    """An endpoint returning raw bytes."""

    async def get(self) -> bytes:
        """Get a blob."""
        return b"blob"


class RateHeaders(Struct):
    """Typed response headers carried by JSONResponse[Item, RateHeaders]."""

    x_rate_limit: int


class HeaderedEndpoint(Endpoint, path="/headered"):
    """An endpoint with typed response headers."""

    async def get(self) -> JSONResponse[Item, RateHeaders]:
        """Get an item with rate-limit headers."""
        return JSONResponse(json=Item(id="id"), headers=RateHeaders(x_rate_limit=1))


class OpenApp(BaseApp):
    """App with one open endpoint and one bytes endpoint."""

    async def wire(self) -> None:
        self.include_endpoint(OpenEndpoint())
        self.include_endpoint(BlobEndpoint())
        self.include_endpoint(HeaderedEndpoint())
        self.include_openapi(title="open", version="1")


def test_unauthed_operation_has_no_security() -> None:
    """An operation behind no auth emits no security requirement."""
    with TestClient(OpenApp()) as client:
        get = client.get("/openapi.json").json()["paths"]["/open"]["get"]
        assert "security" not in get


def test_bytes_return_is_documented_as_binary() -> None:
    """A bytes return is application/octet-stream with a binary schema."""
    with TestClient(OpenApp()) as client:
        content = client.get("/openapi.json").json()["paths"]["/blob"]["get"]["responses"]["200"][
            "content"
        ]
        assert content["application/octet-stream"]["schema"] == {
            "type": "string",
            "format": "binary",
        }


class DocsOffApp(BaseApp):
    """App that serves the spec JSON but not the UI."""

    async def wire(self) -> None:
        self.include_endpoint(OpenEndpoint())
        self.include_openapi(title="t", version="1", docs_path=None)


def test_typed_response_headers_are_documented() -> None:
    """The H in JSONResponse[T, H] becomes the response's documented headers."""
    with TestClient(OpenApp()) as client:
        ok = client.get("/openapi.json").json()["paths"]["/headered"]["get"]["responses"]["200"]
        assert "x-rate-limit" in ok["headers"]
        assert ok["headers"]["x-rate-limit"]["schema"] == {"type": "integer"}
        # the body is still the item model, not the header struct
        assert ok["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Item"}


def test_docs_path_none_disables_the_ui() -> None:
    """docs_path=None serves the spec but registers no /docs route."""
    with TestClient(DocsOffApp()) as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 404


def test_startup_logs_relative_docs_path(caplog: pytest.LogCaptureFixture) -> None:
    """Startup logs where the docs are served; relative when no public origin is known."""
    # The log fires during lifespan startup (in TestClient construction), so capture first.
    with caplog.at_level(logging.INFO, logger="jero"), TestClient(OpenApp()):
        pass
    assert "Serving API docs at /docs" in caplog.text


def test_startup_logs_absolute_docs_url_with_base_url(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With JERO_BASE_URL set, the startup line is a full, clickable docs URL."""
    monkeypatch.setenv("JERO_BASE_URL", "http://127.0.0.1:8000")
    with caplog.at_level(logging.INFO, logger="jero"), TestClient(OpenApp()):
        pass
    assert "Serving API docs at http://127.0.0.1:8000/docs" in caplog.text


def test_startup_logs_spec_path_when_docs_disabled(caplog: pytest.LogCaptureFixture) -> None:
    """With the UI disabled, the startup line points at the spec instead."""
    with caplog.at_level(logging.INFO, logger="jero"), TestClient(DocsOffApp()):
        pass
    assert "Serving OpenAPI spec at /openapi.json" in caplog.text


class CustomDocsApp(BaseApp):
    """App that overrides the docs HTML."""

    async def wire(self) -> None:
        self.include_endpoint(OpenEndpoint())
        self.include_openapi(title="t", version="1", docs_html="<html>custom</html>")


def test_custom_docs_html_is_served_verbatim() -> None:
    """A supplied docs_html replaces the default Scalar page."""
    with TestClient(CustomDocsApp()) as client:
        assert client.get("/docs").text == "<html>custom</html>"


class Credentials(Struct):
    """Bearer credentials lifted from the Authorization header."""

    authorization: str


class Caller(Struct):
    """The authenticated caller."""

    id: str


@dataclass
class PlainAuth:
    """A structural authenticator that declares no openapi_security."""

    async def authenticate(self, headers: Credentials) -> Caller:
        """Resolve a caller from the credentials."""
        return Caller(id=headers.authorization)


@dataclass
class ApiKeyAuth:
    """An authenticator declaring an apiKey scheme directly via the attribute."""

    openapi_security = SecurityScheme.api_key(name="X-API-Key", location="header")

    async def authenticate(self, headers: Credentials) -> Caller:
        """Resolve a caller from the credentials."""
        return Caller(id=headers.authorization)


class SecuredEndpoint(Endpoint, path="/secured"):
    """An endpoint to mount behind various auth schemes."""

    async def get(self, user: Caller) -> Item:
        """Get an item for the caller."""
        return Item(id=user.id)


class PlainAuthApp(BaseApp):
    """App whose auth declares no scheme (should default to bearer)."""

    async def wire(self) -> None:
        self.include_endpoint(SecuredEndpoint(), auth=PlainAuth())
        self.include_openapi(title="t", version="1")


class ApiKeyApp(BaseApp):
    """App whose auth declares an apiKey scheme."""

    async def wire(self) -> None:
        self.include_endpoint(SecuredEndpoint(), auth=ApiKeyAuth())
        self.include_openapi(title="t", version="1")


def test_undeclared_auth_defaults_to_bearer() -> None:
    """An authed route whose Auth declares no scheme defaults to HTTP bearer."""
    with TestClient(PlainAuthApp()) as client:
        document = client.get("/openapi.json").json()
        assert document["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
        assert document["paths"]["/secured"]["get"]["security"] == [{"bearerAuth": []}]


def test_api_key_scheme_is_emitted() -> None:
    """A declared apiKey scheme renders with its location and name."""
    with TestClient(ApiKeyApp()) as client:
        document = client.get("/openapi.json").json()
        assert document["components"]["securitySchemes"]["apiKeyAuth"] == {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
        }
        assert document["paths"]["/secured"]["get"]["security"] == [{"apiKeyAuth": []}]


class DescribedEndpoint(
    Endpoint,
    path="/described",
    meta_get=OperationMeta(
        operation_id="customId",
        summary="Custom summary",
        description="Custom description",
    ),
):
    """An endpoint whose prose comes entirely from OperationMeta."""

    async def get(self) -> Item:
        """Maintainer-only docstring — never published."""
        return Item(id="id")


class DescribedApp(BaseApp):
    """App exercising OperationMeta summary/description/operation_id."""

    async def wire(self) -> None:
        self.include_endpoint(DescribedEndpoint())
        self.include_openapi(title="t", version="1")


class TaggedEndpoint(
    Endpoint,
    path="/tagged",
    meta=EndpointMeta(tags=[Tag("base")]),
    meta_get=OperationMeta(tags=[Tag("extra")]),  # list -> extends the class tags
    meta_post=OperationMeta(tags=(Tag("only"),)),  # tuple -> replaces the class tags
    meta_put=OperationMeta(tags=[Tag("base"), Tag("more")]),  # list with a dup -> extend + dedup
    meta_delete=OperationMeta(operation_id="del"),  # no tags -> inherit the class tags
):
    """Endpoint exercising the list-extends / tuple-overrides tag cascade."""

    async def get(self) -> Item:
        """Get."""
        return Item(id="id")

    async def post(self) -> Item:
        """Post."""
        return Item(id="id")

    async def put(self) -> Item:
        """Put."""
        return Item(id="id")

    async def delete(self) -> Item:
        """Delete."""
        return Item(id="id")


class TaggedApp(BaseApp):
    """App exercising the tag cascade across operations (tags defined on the meta)."""

    async def wire(self) -> None:
        self.include_endpoint(TaggedEndpoint())
        self.include_openapi(title="t", version="1")


def test_list_op_tags_extend_class_tags() -> None:
    """A list of operation tags is unioned onto the class tags."""
    with TestClient(TaggedApp()) as client:
        ops = client.get("/openapi.json").json()["paths"]["/tagged"]
        assert ops["get"]["tags"] == ["base", "extra"]


def test_tuple_op_tags_override_class_tags() -> None:
    """A non-empty tuple of operation tags replaces the class tags."""
    with TestClient(TaggedApp()) as client:
        ops = client.get("/openapi.json").json()["paths"]["/tagged"]
        assert ops["post"]["tags"] == ["only"]


def test_list_op_tags_extend_and_dedupe() -> None:
    """Extending with a tag already on the class doesn't duplicate it."""
    with TestClient(TaggedApp()) as client:
        ops = client.get("/openapi.json").json()["paths"]["/tagged"]
        assert ops["put"]["tags"] == ["base", "more"]


def test_empty_op_tags_inherit_class_tags() -> None:
    """An operation that declares no tags inherits the class tags."""
    with TestClient(TaggedApp()) as client:
        ops = client.get("/openapi.json").json()["paths"]["/tagged"]
        assert ops["delete"]["tags"] == ["base"]


def test_operation_meta_supplies_summary_description_and_id() -> None:
    """OperationMeta supplies summary/description/operation_id (the docstring isn't used)."""
    with TestClient(DescribedApp()) as client:
        get = client.get("/openapi.json").json()["paths"]["/described"]["get"]
        assert get["operationId"] == "customId"
        assert get["summary"] == "Custom summary"
        assert get["description"] == "Custom description"


# --- Multipart form bodies ---


class FormAddress(Struct, rename="camel"):
    """A struct payload nested in a multipart field."""

    city: str


class UploadForm(Struct, rename="camel"):
    """A multipart form spanning a constrained scalar, a file, and a struct payload."""

    quantity: Annotated[int, Meta(ge=1, le=9, description="How many", examples=[3])]
    avatar: FormPart[bytes]
    address: FormAddress


class UploadEndpoint(Endpoint, path="/upload"):
    """Accepts the multipart upload form."""

    async def post(self, form: UploadForm) -> Item:
        """Upload."""
        return Item(id=form.address.city)


class UploadApp(BaseApp):
    """App exercising multipart form documentation."""

    async def wire(self) -> None:
        self.include_endpoint(UploadEndpoint())
        self.include_openapi(title="t", version="1")


def test_form_field_meta_and_payloads_are_documented() -> None:
    """A multipart body documents Meta on scalars, binary for files, and shared $refs."""
    with TestClient(UploadApp()) as client:
        document = client.get("/openapi.json").json()
        schema = document["paths"]["/upload"]["post"]["requestBody"]["content"][
            "multipart/form-data"
        ]["schema"]
        props = schema["properties"]
        # full msgspec.Meta flows onto the scalar field
        assert props["quantity"] == {
            "description": "How many",
            "examples": [3],
            "type": "integer",
            "minimum": 1,
            "maximum": 9,
        }
        assert props["avatar"] == {"type": "string", "format": "binary"}  # a file
        # a struct payload references the shared component, which is present
        assert props["address"] == {"$ref": "#/components/schemas/FormAddress"}
        assert "FormAddress" in document["components"]["schemas"]
        assert schema["required"] == ["quantity", "avatar", "address"]


# --- info.description and servers ---


class InfoEndpoint(Endpoint, path="/info"):
    """Trivial endpoint for the info/servers assertions."""

    async def get(self) -> Item:
        """Get."""
        return Item(id="id")


class InfoApp(BaseApp):
    """App exercising the description and servers knobs of include_openapi."""

    async def wire(self) -> None:
        self.include_endpoint(InfoEndpoint())
        self.include_openapi(
            title="My API",
            version="2.0.0",
            description="A described API.",
            servers=["https://api.example.com", "https://staging.example.com"],
        )


def test_info_description_and_servers_are_emitted() -> None:
    """include_openapi's description and servers reach the document (and it stays valid)."""
    with TestClient(InfoApp()) as client:
        document = client.get("/openapi.json").json()
        validate(document)
        assert document["info"]["description"] == "A described API."
        assert document["servers"] == [
            {"url": "https://api.example.com"},
            {"url": "https://staging.example.com"},
        ]


# --- Composed whole-model examples ---


class ExampledModel(Struct, rename="camel"):
    """Every field carries its own examples."""

    name: Annotated[str, Meta(examples=["Gadget", "Gizmo"])]
    price_cents: Annotated[int, Meta(examples=[1999, 2999])]


class ExampledEndpoint(Endpoint, path="/exampled"):
    """Accepts a fully-exampled model."""

    async def post(self, json: ExampledModel) -> Item:
        """Post."""
        return Item(id=json.name)


class PartialModel(Struct, rename="camel"):
    """One field has examples, one doesn't."""

    name: Annotated[str, Meta(examples=["Gadget"])]
    price_cents: int


class PartialEndpoint(Endpoint, path="/partial"):
    """Accepts a partially-exampled model."""

    async def post(self, json: PartialModel) -> Item:
        """Post."""
        return Item(id=json.name)


class ExamplesApp(BaseApp):
    """App exercising whole-model example composition."""

    async def wire(self) -> None:
        self.include_endpoint(ExampledEndpoint())
        self.include_endpoint(PartialEndpoint())
        self.include_openapi(title="t", version="1")


def test_whole_model_examples_are_composed_at_the_media_type() -> None:
    """When every field has examples, the body's media type gets full object examples (the
    place docs UIs surface them), zipped by index and named selectably."""
    with TestClient(ExamplesApp()) as client:
        document = client.get("/openapi.json").json()
        media = document["paths"]["/exampled"]["post"]["requestBody"]["content"]["application/json"]
        assert media["examples"] == {
            "example 1": {"value": {"name": "Gadget", "priceCents": 1999}},
            "example 2": {"value": {"name": "Gizmo", "priceCents": 2999}},
        }
        # the per-field examples still live in the schema component
        props = document["components"]["schemas"]["ExampledModel"]["properties"]
        assert props["name"]["examples"] == ["Gadget", "Gizmo"]


def test_no_model_example_when_a_field_lacks_examples() -> None:
    """A model isn't given a composed example unless every field carries one."""
    with TestClient(ExamplesApp()) as client:
        media = client.get("/openapi.json").json()["paths"]["/partial"]["post"]["requestBody"][
            "content"
        ]["application/json"]
        assert "examples" not in media


# --- Document-level tags ---


def test_document_level_tags_have_descriptions(client: TestClient) -> None:
    """Tags defined on the resources/endpoints surface at the document root with their
    descriptions, in first-seen order."""
    tags = client.get("/openapi.json").json()["tags"]
    assert tags == [
        {"name": "widgets", "description": "Create, read, and manage widgets."},
        {"name": "system", "description": "Health checks and diagnostics."},
    ]


class OwnerEndpoint(Endpoint, path="/owner", meta=EndpointMeta(tags=[Tag("billing", "Invoices.")])):
    """Defines the 'billing' tag (with a description) inline on its meta."""

    async def get(self) -> Item:
        """Get."""
        return Item(id="id")


class RefEndpoint(Endpoint, path="/ref", meta=EndpointMeta(tags=["billing"])):
    """Uses 'billing' by bare-string name — no redefinition."""

    async def get(self) -> Item:
        """Get."""
        return Item(id="id")


class InlineTagsApp(BaseApp):
    """A tag defined inline on one endpoint and referenced by name on another."""

    async def wire(self) -> None:
        self.include_endpoint(OwnerEndpoint())
        self.include_endpoint(RefEndpoint())
        self.include_openapi(title="t", version="1")  # no central tags — meta defines them


def test_tag_defined_inline_is_referenced_by_name_elsewhere() -> None:
    """One endpoint defines a tag's description; another references it by bare name; both
    operations carry it, and the root tag keeps the single description."""
    with TestClient(InlineTagsApp()) as client:
        document = client.get("/openapi.json").json()
        assert document["paths"]["/owner"]["get"]["tags"] == ["billing"]
        assert document["paths"]["/ref"]["get"]["tags"] == ["billing"]
        assert document["tags"] == [{"name": "billing", "description": "Invoices."}]


class AdminEndpoint(Endpoint, path="/admin", meta=EndpointMeta(tags=["admin"])):
    """Uses 'admin' by name; the description and order come from include_openapi."""

    async def get(self) -> Item:
        """Admin."""
        return Item(id="id")


class CentralTagsApp(BaseApp):
    """App pinning tag order/descriptions centrally; the endpoint references by name."""

    async def wire(self) -> None:
        self.include_endpoint(AdminEndpoint())
        self.include_openapi(
            title="t",
            version="1",
            tags=[
                Tag("public"),  # name-only, app-level, pinned first
                Tag("admin", "Admin ops."),  # describes the tag AdminEndpoint references
                Tag("unused", "Declared but no operation uses it."),  # still emitted
            ],
        )


def test_central_tags_pin_order_and_describe_references() -> None:
    """include_openapi(tags=...) sets section order and descriptions; a declared-but-unused
    tag is still emitted; an endpoint's name-only reference picks up the description."""
    with TestClient(CentralTagsApp()) as client:
        document = client.get("/openapi.json").json()
        assert document["tags"] == [
            {"name": "public"},
            {"name": "admin", "description": "Admin ops."},
            {"name": "unused", "description": "Declared but no operation uses it."},
        ]
        assert document["paths"]["/admin"]["get"]["tags"] == ["admin"]


class ConflictEndpointA(Endpoint, path="/ca", meta=EndpointMeta(tags=[Tag("x", "First.")])):
    """Describes tag 'x' one way."""

    async def get(self) -> Item:
        """Get."""
        return Item(id="id")


class ConflictEndpointB(Endpoint, path="/cb", meta=EndpointMeta(tags=[Tag("x", "Second.")])):
    """Describes the same tag 'x' a different way."""

    async def get(self) -> Item:
        """Get."""
        return Item(id="id")


class ConflictTagApp(BaseApp):
    """Two endpoints give tag 'x' conflicting descriptions — a wiring error."""

    async def wire(self) -> None:
        self.include_endpoint(ConflictEndpointA())
        self.include_endpoint(ConflictEndpointB())
        self.include_openapi(title="t", version="1")


def test_conflicting_tag_descriptions_is_a_wiring_error() -> None:
    """Describing the same tag two different ways fails loud at startup."""
    with pytest.raises(RuntimeError, match="conflicting descriptions"):
        TestClient(ConflictTagApp())


class EarlyRefEndpoint(Endpoint, path="/early", meta=EndpointMeta(tags=["audit"])):
    """Uses 'audit' by bare name, wired before anything describes it."""

    async def get(self) -> Item:
        """Get."""
        return Item(id="id")


class LateDefEndpoint(Endpoint, path="/late", meta=EndpointMeta(tags=[Tag("audit", "Audit log.")])):
    """Defines 'audit' later in wiring order."""

    async def get(self) -> Item:
        """Get."""
        return Item(id="id")


class RefThenDefineApp(BaseApp):
    """A name-only reference is wired before the description — order must not matter."""

    async def wire(self) -> None:
        self.include_endpoint(EarlyRefEndpoint())
        self.include_endpoint(LateDefEndpoint())
        self.include_openapi(title="t", version="1")


def test_tag_description_set_after_a_reference_is_resolved() -> None:
    """Setting a description on a tag first seen as a bare reference fills it (not a conflict)."""
    with TestClient(RefThenDefineApp()) as client:
        assert client.get("/openapi.json").json()["tags"] == [
            {"name": "audit", "description": "Audit log."},
        ]


class SharedAEndpoint(Endpoint, path="/sa", meta=EndpointMeta(tags=["shared"])):
    """Uses a bare tag also used elsewhere and never described."""

    async def get(self) -> Item:
        """Get."""
        return Item(id="id")


class SharedBEndpoint(Endpoint, path="/sb", meta=EndpointMeta(tags=["shared"])):
    """Uses the same bare tag."""

    async def get(self) -> Item:
        """Get."""
        return Item(id="id")


class SharedTagApp(BaseApp):
    """Two endpoints share a name-only tag that no one describes or declares centrally."""

    async def wire(self) -> None:
        self.include_endpoint(SharedAEndpoint())
        self.include_endpoint(SharedBEndpoint())
        self.include_openapi(title="t", version="1")


def test_shared_name_only_tag_is_emitted_once() -> None:
    """A bare tag used by several operations is carried by each and listed once at the root
    (description-less) — there's no notion of a referenced tag that's 'missing'."""
    with TestClient(SharedTagApp()) as client:
        document = client.get("/openapi.json").json()
        assert document["paths"]["/sa"]["get"]["tags"] == ["shared"]
        assert document["paths"]["/sb"]["get"]["tags"] == ["shared"]
        assert document["tags"] == [{"name": "shared"}]


# --- SecurityScheme construction ---


def test_security_scheme_constructors() -> None:
    """The constructors render the expected OpenAPI scheme objects."""
    assert SecurityScheme.http_bearer(bearer_format="JWT").to_openapi() == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
    assert SecurityScheme.http_basic().to_openapi() == {"type": "http", "scheme": "basic"}
    assert SecurityScheme.api_key(name="session", location="cookie").to_openapi() == {
        "type": "apiKey",
        "in": "cookie",
        "name": "session",
    }
    assert SecurityScheme.http_bearer(description="A JWT bearer token.").to_openapi() == {
        "type": "http",
        "scheme": "bearer",
        "description": "A JWT bearer token.",
    }


@pytest.mark.parametrize("base", [BearerAuth, type("X", (), {})])
def test_bearer_auth_base_carries_scheme(base: type) -> None:
    """BearerAuth declares an http-bearer openapi_security; a bare class does not."""
    if base is BearerAuth:
        assert isinstance(BearerAuth.openapi_security, SecurityScheme)
        assert BearerAuth.openapi_security.scheme == "bearer"
    else:
        assert not hasattr(base, "openapi_security")


# A constrained model proving Annotated[..., Meta(...)] survives end to end.
class Constrained(Struct):
    """A model with a richly constrained field."""

    code: Annotated[str, Meta(min_length=2, max_length=4, pattern="^[A-Z]+$", description="a code")]


class ConstrainedEndpoint(Endpoint, path="/constrained"):
    """Echoes a constrained model."""

    async def post(self, json: Constrained) -> Constrained:
        """Echo the body."""
        return json


class ConstrainedApp(BaseApp):
    """App exercising msgspec.Meta passthrough on a request/response model."""

    async def wire(self) -> None:
        self.include_endpoint(ConstrainedEndpoint())
        self.include_openapi(title="t", version="1")


def test_meta_constraints_survive_for_arbitrary_models() -> None:
    """All of min/max length, pattern, and description reach the schema."""
    with TestClient(ConstrainedApp()) as client:
        code = client.get("/openapi.json").json()["components"]["schemas"]["Constrained"][
            "properties"
        ]["code"]
        assert code["minLength"] == 2
        assert code["maxLength"] == 4
        assert code["pattern"] == "^[A-Z]+$"
        assert code["description"] == "a code"


# --- Review-regression coverage ---


class SSEHeaders(Struct):
    """Typed SSE response headers."""

    x_rate: int


class StrSSEEndpoint(Endpoint, path="/sse-str"):
    """SSE stream of plain strings (T defaults to / is str)."""

    async def _events(self) -> AsyncIterator[str]:
        yield "tick"

    async def get(self) -> SSEResponse[str, SSEHeaders]:
        """Stream strings with a rate header."""
        return SSEResponse(stream=self._events(), headers=SSEHeaders(x_rate=1))


class ListItemsEndpoint(Endpoint, path="/items"):
    """Returns a list of structs."""

    async def get(self) -> list[Item]:
        """List items."""
        return [Item(id="id")]


class BytesBodyEndpoint(Endpoint, path="/raw-upload"):
    """Takes a raw bytes request body."""

    async def post(self, content: bytes) -> Item:
        """Upload raw bytes."""
        return Item(id=str(len(content)))


class TraceHeaders(Struct):
    """Typed request headers bound as a source."""

    x_trace_id: str


class HeaderParamEndpoint(Endpoint, path="/trace"):
    """Binds typed request headers (they surface as header parameters)."""

    async def get(self, headers: TraceHeaders) -> Item:
        """Echo the trace id."""
        return Item(id=headers.x_trace_id)


class CoverageApp(BaseApp):
    """Exercises SSE-of-str (+headers), list responses, bytes body, header params."""

    async def wire(self) -> None:
        self.include_endpoint(StrSSEEndpoint())
        self.include_endpoint(ListItemsEndpoint())
        self.include_endpoint(BytesBodyEndpoint())
        self.include_endpoint(HeaderParamEndpoint())
        self.include_openapi(title="t", version="1")


def test_sse_str_body_is_string_not_the_header_struct() -> None:
    """SSEResponse[str, H] documents a string body and H as headers — not H as the body."""
    with TestClient(CoverageApp()) as client:
        ok = client.get("/openapi.json").json()["paths"]["/sse-str"]["get"]["responses"]["200"]
        assert ok["content"]["text/event-stream"]["schema"] == {"type": "string"}
        assert "x-rate" in ok["headers"]


def test_list_struct_response_is_an_array_of_refs() -> None:
    """A list[Struct] return documents an array whose items $ref the model."""
    with TestClient(CoverageApp()) as client:
        ok = client.get("/openapi.json").json()["paths"]["/items"]["get"]["responses"]["200"]
        assert ok["content"]["application/json"]["schema"] == {
            "type": "array",
            "items": {"$ref": "#/components/schemas/Item"},
        }


def test_raw_bytes_request_body_is_binary() -> None:
    """A content: bytes body is documented as application/octet-stream binary."""
    with TestClient(CoverageApp()) as client:
        body = client.get("/openapi.json").json()["paths"]["/raw-upload"]["post"]["requestBody"]
        assert body["content"]["application/octet-stream"]["schema"] == {
            "type": "string",
            "format": "binary",
        }


def test_typed_request_headers_become_header_parameters() -> None:
    """A headers: Struct source expands to header parameters with inverted wire names."""
    with TestClient(CoverageApp()) as client:
        params = client.get("/openapi.json").json()["paths"]["/trace"]["get"]["parameters"]
        assert {
            "name": "x-trace-id",
            "in": "header",
            "required": True,
            "schema": {"type": "string"},
        } in params


class IntraConflictEndpoint(
    Endpoint,
    path="/intra",
    meta=EndpointMeta(
        tags=[Tag("x", "First."), Tag("x", "Second.")]
    ),  # same name, two descriptions
):
    """Declares conflicting descriptions for one tag within a single meta."""

    async def get(self) -> Item:
        """Get."""
        return Item(id="id")


class IntraConflictApp(BaseApp):
    """A single meta describes one tag two different ways."""

    async def wire(self) -> None:
        self.include_endpoint(IntraConflictEndpoint())
        self.include_openapi(title="t", version="1")


def test_conflicting_tag_descriptions_within_one_meta_is_a_wiring_error() -> None:
    """The conflict rule applies within a single meta, not only across operations."""
    with pytest.raises(RuntimeError, match="conflicting descriptions"):
        TestClient(IntraConflictApp())


class BearerOne(BearerAuth[Credentials, Caller]):
    """Bearer auth using the default scheme_name."""

    # Auth.authenticate is sync-or-async (-> TUser | Awaitable[TUser]); pylint only sees the
    # sync arm and flags the async override. False positive (see demo_app.auth.TokenAuth).
    async def authenticate(self, headers: Credentials) -> Caller:  # pylint: disable=invalid-overridden-method
        """Resolve a caller."""
        return Caller(id=headers.authorization)


@dataclass
class ClashAuth:
    """An apiKey auth that reuses the 'bearerAuth' scheme_name with a different scheme."""

    openapi_security = SecurityScheme.api_key(
        name="X-Key", location="header", scheme_name="bearerAuth"
    )

    async def authenticate(self, headers: Credentials) -> Caller:
        """Resolve a caller."""
        return Caller(id=headers.authorization)


class EndpointA(Endpoint, path="/sa-a"):
    """Behind bearer auth."""

    async def get(self, user: Caller) -> Item:
        """Get."""
        return Item(id=user.id)


class EndpointB(Endpoint, path="/sa-b"):
    """Behind the clashing apiKey auth."""

    async def get(self, user: Caller) -> Item:
        """Get."""
        return Item(id=user.id)


class SchemeClashApp(BaseApp):
    """Two auths share scheme_name 'bearerAuth' but are different schemes."""

    async def wire(self) -> None:
        self.include_endpoint(EndpointA(), auth=BearerOne())
        self.include_endpoint(EndpointB(), auth=ClashAuth())
        self.include_openapi(title="t", version="1")


def test_clashing_security_scheme_names_is_a_wiring_error() -> None:
    """Two different security schemes under one scheme_name fail loud."""
    with pytest.raises(RuntimeError, match="share the name"):
        TestClient(SchemeClashApp())


class CsvEndpoint(
    Endpoint,
    path="/csv",
    meta_get=OperationMeta(
        responses=[ResponseSpec(503, "Maintenance", content_type="text/plain")],  # no model
    ),
):
    """Declares a schemaless non-JSON response via meta."""

    async def get(self) -> Item:
        """Get."""
        return Item(id="id")


class CsvApp(BaseApp):
    """A meta response with a content_type but no model."""

    async def wire(self) -> None:
        self.include_endpoint(CsvEndpoint())
        self.include_openapi(title="t", version="1")


def test_response_spec_content_type_without_model_is_schemaless_body() -> None:
    """A ResponseSpec with content_type but no model documents a schemaless body of that type."""
    with TestClient(CsvApp()) as client:
        resp = client.get("/openapi.json").json()["paths"]["/csv"]["get"]["responses"]["503"]
        assert resp["content"] == {"text/plain": {"schema": {}}}


# --- Model metadata (ModelMeta) and the no-docstring rule ---


class DescribedModel(JeroStruct, meta=ModelMeta(description="A described model.")):
    """Maintainer-only docstring — must not be published."""

    name: str


class EnvelopeModel(JeroStruct, meta=ModelMeta(description="A response envelope.")):
    """Has a wire field literally named 'meta' alongside the meta= kwarg."""

    meta: str
    name: str


class PlainModel(Struct):
    """A plain Struct with a docstring and no ModelMeta."""

    name: str


class InheritingModel(DescribedModel):
    """Subclass of a described model, with no ModelMeta of its own."""

    extra: str


class InheritingEndpoint(Endpoint, path="/inheriting-model"):
    """Returns a subclass that declares no ModelMeta of its own."""

    async def get(self) -> InheritingModel:
        """Get."""
        return InheritingModel(name="name", extra="extra")


class ModelMetaEndpoint(Endpoint, path="/described-model"):
    """Uses the described model as a body and the plain one as the response."""

    async def post(self, json: DescribedModel) -> PlainModel:
        """Maintainer-only handler docstring — must not be published."""
        return PlainModel(name=json.name)


class EnvelopeEndpoint(Endpoint, path="/envelope"):
    """Returns the envelope model (meta= kwarg + meta field)."""

    async def get(self) -> EnvelopeModel:
        """Get."""
        return EnvelopeModel(meta="meta", name="name")


class ModelMetaApp(BaseApp):
    """App exercising ModelMeta, the meta field, and docstring suppression."""

    async def wire(self) -> None:
        self.include_endpoint(ModelMetaEndpoint())
        self.include_endpoint(EnvelopeEndpoint())
        self.include_endpoint(InheritingEndpoint())
        self.include_openapi(title="t", version="1")


def test_model_description_comes_from_model_meta() -> None:
    """A ModelMeta description appears on the component; the class docstring does not."""
    with TestClient(ModelMetaApp()) as client:
        schema = client.get("/openapi.json").json()["components"]["schemas"]["DescribedModel"]
        assert schema["description"] == "A described model."
        assert "Maintainer-only" not in str(schema)  # docstring not leaked


def test_model_docstring_is_not_published() -> None:
    """A model with only a docstring (no ModelMeta) gets no component description."""
    with TestClient(ModelMetaApp()) as client:
        schema = client.get("/openapi.json").json()["components"]["schemas"]["PlainModel"]
        assert "description" not in schema


def test_handler_docstring_is_not_published() -> None:
    """A handler with a docstring but no OperationMeta gets no summary/description."""
    with TestClient(ModelMetaApp()) as client:
        post = client.get("/openapi.json").json()["paths"]["/described-model"]["post"]
        assert "summary" not in post
        assert "description" not in post


def test_model_description_is_not_inherited_by_subclass() -> None:
    """A subclass without its own ModelMeta gets no description — the parent's doesn't leak."""
    with TestClient(ModelMetaApp()) as client:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]
        assert schemas["DescribedModel"]["description"] == "A described model."
        assert "description" not in schemas["InheritingModel"]


def test_meta_field_coexists_with_meta_kwarg() -> None:
    """A wire field named 'meta' coexists with the meta= model metadata."""
    with TestClient(ModelMetaApp()) as client:
        document = client.get("/openapi.json").json()
        envelope = document["components"]["schemas"]["EnvelopeModel"]
        assert "meta" in envelope["properties"]  # the wire field survives
        assert envelope["description"] == "A response envelope."  # the meta= description applies


# --- Model component name (ModelMeta.name) ---


class RenamedModel(JeroStruct, meta=ModelMeta(name="CustomWidget")):
    """A model whose component key is overridden via ModelMeta(name=...)."""

    id: str


class WrapperModel(Struct):
    """Embeds the renamed model, so its property is a $ref that must be rewritten too."""

    item: RenamedModel


class NameEndpoint(Endpoint, path="/renamed"):
    """Uses the renamed model as a body and a wrapper of it as the response."""

    async def post(self, json: RenamedModel) -> WrapperModel:
        """Post."""
        return WrapperModel(item=json)


class NameApp(BaseApp):
    """App exercising ModelMeta(name=...)."""

    async def wire(self) -> None:
        self.include_endpoint(NameEndpoint())
        self.include_openapi(title="title", version="version")


def test_model_name_overrides_component_key() -> None:
    """ModelMeta(name=...) renames the component and every $ref that points at it."""
    with TestClient(NameApp()) as client:
        document = client.get("/openapi.json").json()
        schemas = document["components"]["schemas"]
        assert "CustomWidget" in schemas  # the override is the component key
        assert "RenamedModel" not in schemas  # the class name is gone
        body = document["paths"]["/renamed"]["post"]["requestBody"]
        assert body["content"]["application/json"]["schema"]["$ref"] == (
            "#/components/schemas/CustomWidget"
        )
        # a nested $ref (WrapperModel.item -> RenamedModel) is rewritten to the new name
        assert schemas["WrapperModel"]["properties"]["item"]["$ref"] == (
            "#/components/schemas/CustomWidget"
        )


class DupeNameA(JeroStruct, meta=ModelMeta(name="Shared")):
    """First model claiming the component name 'Shared'."""

    a: str


class DupeNameB(JeroStruct, meta=ModelMeta(name="Shared")):
    """Second model claiming the same component name 'Shared'."""

    b: str


class DupeNameEndpoint(Endpoint, path="/dupe"):
    """Two models that both claim the component name 'Shared'."""

    async def post(self, json: DupeNameA) -> DupeNameB:
        """Post."""
        return DupeNameB(b=json.a)


class DupeNameApp(BaseApp):
    """App whose two models collide on ModelMeta(name=...)."""

    async def wire(self) -> None:
        self.include_endpoint(DupeNameEndpoint())
        self.include_openapi(title="title", version="version")


def test_conflicting_model_names_is_a_wiring_error() -> None:
    """Two models claiming the same component name fail loud at startup."""
    with pytest.raises(RuntimeError, match="claimed by more than one model"):
        TestClient(DupeNameApp())


# --- Class-level meta responses & body-carrying response specs ---


class Conflict(Struct):
    """A domain conflict body, documented via a class-level meta response."""

    reason: str


class ClassRespResource(
    Resource,
    path="/things",
    meta=ResourceMeta(responses=[ResponseSpec(409, "Already exists", model=Conflict)]),
):
    """A class-level ResourceMeta.responses entry with a body model, applied to every op."""

    async def create(self, json: Item) -> Item:
        """Create."""
        return json


class ClassRespApp(BaseApp):
    """A class-level meta response that carries a body model."""

    async def wire(self) -> None:
        self.include_resource(ClassRespResource())
        self.include_openapi(title="title", version="version")


def test_class_meta_response_with_model() -> None:
    """A class-level ResourceMeta.responses entry with a model documents a $ref body."""
    with TestClient(ClassRespApp()) as client:
        post = client.get("/openapi.json").json()["paths"]["/things"]["post"]
        response = post["responses"]["409"]
        assert response["description"] == "Already exists"
        assert response["content"]["application/json"]["schema"]["$ref"] == (
            "#/components/schemas/Conflict"
        )


class BytesStreamEndpoint(Endpoint, path="/bytes-stream"):
    """StreamingResponse[H] documents an octet-stream body with typed response headers."""

    async def _chunks(self) -> AsyncIterator[bytes]:
        yield b"chunk"

    async def get(self) -> StreamingResponse[RateHeaders]:
        """Get."""
        return StreamingResponse(stream=self._chunks(), headers=RateHeaders(x_rate_limit=1))


class BytesStreamApp(BaseApp):
    """A bytes-stream response carrying typed headers."""

    async def wire(self) -> None:
        self.include_endpoint(BytesStreamEndpoint())
        self.include_openapi(title="title", version="version")


def test_bytes_stream_documents_octet_stream_and_headers() -> None:
    """StreamingResponse[H] is octet-stream, and H becomes the documented response headers."""
    with TestClient(BytesStreamApp()) as client:
        ok = client.get("/openapi.json").json()["paths"]["/bytes-stream"]["get"]["responses"]["200"]
        assert "application/octet-stream" in ok["content"]
        assert "x-rate-limit" in ok["headers"]


# --- Union item payloads (mixed streams / union response bodies) ---


class TextChunk(Struct, tag=True):
    """A streamed text piece (one member of the union)."""

    text: str


class FooterChunk(Struct, tag=True):
    """The stream's trailing summary (the other member of the union)."""

    total: int


class MixedStreamEndpoint(Endpoint, path="/mixed-stream"):
    """NDJSON stream whose item type is a tagged union."""

    async def _chunks(self) -> AsyncIterator[TextChunk | FooterChunk]:
        yield TextChunk(text="text")
        yield FooterChunk(total=1)

    async def get(self) -> NDJSONStreamingResponse[TextChunk | FooterChunk]:
        """Stream mixed chunks."""
        return NDJSONStreamingResponse(stream=self._chunks())


class MixedSSEEndpoint(Endpoint, path="/mixed-sse"):
    """SSE stream whose data type is a tagged union."""

    async def _events(self) -> AsyncIterator[TextChunk | FooterChunk]:
        yield TextChunk(text="text")

    async def get(self) -> SSEResponse[TextChunk | FooterChunk]:
        """Stream mixed events."""
        return SSEResponse(stream=self._events())


class MixedJSONEndpoint(Endpoint, path="/mixed-json"):
    """JSONResponse whose body type is a union."""

    async def get(self) -> JSONResponse[TextChunk | FooterChunk]:
        """Get one of the chunk shapes."""
        chunk: TextChunk | FooterChunk = TextChunk(text="text")
        return JSONResponse(json=chunk)


class UnionPayloadApp(BaseApp):
    """Exercises union item types across NDJSON, SSE, and JSONResponse."""

    async def wire(self) -> None:
        self.include_endpoint(MixedStreamEndpoint())
        self.include_endpoint(MixedSSEEndpoint())
        self.include_endpoint(MixedJSONEndpoint())
        self.include_openapi(title="union", version="1")


def test_union_items_document_anyof_with_discriminator() -> None:
    """A tagged-union item type emits anyOf of the member $refs plus a discriminator, both
    members land in components, and the document stays valid OpenAPI 3.1."""
    with TestClient(UnionPayloadApp()) as client:
        document = client.get("/openapi.json").json()
        validate(document)
        schema = document["paths"]["/mixed-stream"]["get"]["responses"]["200"]["content"][
            "application/x-ndjson"
        ]["schema"]
        assert schema["anyOf"] == [
            {"$ref": "#/components/schemas/TextChunk"},
            {"$ref": "#/components/schemas/FooterChunk"},
        ]
        assert schema["discriminator"]["propertyName"] == "type"
        schemas = document["components"]["schemas"]
        assert "TextChunk" in schemas
        assert "FooterChunk" in schemas


def test_union_payload_covers_sse_and_json_response() -> None:
    """SSE and JSONResponse union payloads document the same anyOf schema."""
    with TestClient(UnionPayloadApp()) as client:
        paths = client.get("/openapi.json").json()["paths"]
        for path, content_type in [
            ("/mixed-sse", "text/event-stream"),
            ("/mixed-json", "application/json"),
        ]:
            schema = paths[path]["get"]["responses"]["200"]["content"][content_type]["schema"]
            assert schema["anyOf"] == [
                {"$ref": "#/components/schemas/TextChunk"},
                {"$ref": "#/components/schemas/FooterChunk"},
            ]


class RenamedChunk(JeroStruct, tag=True, meta=ModelMeta(name="PublicChunk")):
    """A union member whose component is renamed via ModelMeta."""

    text: str


class OtherChunk(Struct, tag=True):
    """The second member of the renamed union."""

    total: int


class RenamedUnionEndpoint(Endpoint, path="/renamed-union"):
    """NDJSON stream whose union member carries a ModelMeta rename."""

    async def _chunks(self) -> AsyncIterator[RenamedChunk | OtherChunk]:
        yield RenamedChunk(text="text")

    async def get(self) -> NDJSONStreamingResponse[RenamedChunk | OtherChunk]:
        """Stream renamed chunks."""
        return NDJSONStreamingResponse(stream=self._chunks())


class RenamedUnionApp(BaseApp):
    """App exercising a ModelMeta rename inside a union payload."""

    async def wire(self) -> None:
        self.include_endpoint(RenamedUnionEndpoint())
        self.include_openapi(title="renamed", version="1")


def test_union_member_rename_rewrites_refs_and_discriminator() -> None:
    """A ModelMeta(name=...) on a union member renames its $ref and its discriminator
    mapping target (the mapping key stays the wire tag)."""
    with TestClient(RenamedUnionApp()) as client:
        document = client.get("/openapi.json").json()
        schema = document["paths"]["/renamed-union"]["get"]["responses"]["200"]["content"][
            "application/x-ndjson"
        ]["schema"]
        assert {"$ref": "#/components/schemas/PublicChunk"} in schema["anyOf"]
        assert schema["discriminator"]["mapping"]["RenamedChunk"] == (
            "#/components/schemas/PublicChunk"
        )
        schemas = document["components"]["schemas"]
        assert "PublicChunk" in schemas
        assert "RenamedChunk" not in schemas


class DegradedStreamEndpoint(Endpoint, path="/degraded"):
    """Endpoint whose return annotation is degraded at runtime by the fail-loud test."""

    async def _chunks(self) -> AsyncIterator[TextChunk]:
        yield TextChunk(text="text")

    async def get(self) -> NDJSONStreamingResponse[TextChunk]:
        """Stream chunks (the annotation is replaced before wiring)."""
        return NDJSONStreamingResponse(stream=self._chunks())


class DegradedApp(BaseApp):
    """App wiring the degraded endpoint with OpenAPI enabled."""

    async def wire(self) -> None:
        self.include_endpoint(DegradedStreamEndpoint())
        self.include_openapi(title="degraded", version="1")


def test_unsupported_item_type_fails_loud() -> None:
    """An item type outside the contract is a startup failure, never a silent {} schema."""
    # The static bound (T: Struct) makes this unwritable in source; degrade the stored
    # annotation the way an untyped caller could, then wire.
    DegradedStreamEndpoint.get.__annotations__["return"] = "NDJSONStreamingResponse[int]"
    with pytest.raises(RuntimeError, match="item type must be a Struct or a union of Structs"):
        TestClient(DegradedApp())


# --- Declared exceptions (error classes -> response entries) ---


class ErrorBody(Struct, rename="camel"):
    """A house-style error body used by the declared-exception tests."""

    error_code: str
    error_message: str


class StatusErrorBody(Struct, rename="camel"):
    """A house-style error body carrying the status."""

    error_code: str
    error_message: str
    status_code: int


class QuotaError(
    StructHTTPError[StatusErrorBody],
    status=429,
    description="Quota exceeded",
    consts={"error_code": "quota-exceeded"},
    status_field="status_code",
):
    """A Struct-family error pinning its code and carrying its status in the body."""


class TeapotStructError(StructHTTPError[ErrorBody], status=418, description="Teapot"):
    """A Struct-family error without any pinned fields."""


class WidgetGoneError(HTTPError, type="widget-gone", title="Widget gone", status=410):
    """A Problem-family error declared at the class level."""


class MissingPartParams(Struct):
    """Params rendering the missing-part detail."""

    part_id: str


class MissingPartError(
    ParameterizedHTTPError[MissingPartParams],
    type="missing-part",
    title="Missing part",
    status=404,
    detail_template="Part {part_id} missing",
):
    """A parameterized Problem-family error."""


class DeclaredErrorsEndpoint(
    Endpoint,
    path="/declared",
    meta=EndpointMeta(exceptions=[WidgetGoneError]),
    meta_get=OperationMeta(exceptions=[QuotaError, TeapotStructError]),
):
    """Class-level and operation-level declarations cascade (both raiseable)."""

    async def get(self) -> Item:
        """Get an item."""
        return Item(id="id")


class MergedStatusEndpoint(
    Endpoint,
    path="/merged",
    meta_get=OperationMeta(exceptions=[NotFoundError, MissingPartError]),
):
    """Two declared errors share a status: the entry must merge as a oneOf."""

    async def get(self) -> Item:
        """Get an item."""
        return Item(id="id")


class PrecedencePath(Struct):
    """Path params so a 404 is source-derived."""

    item_id: str


class PrecedenceEndpoint(
    Endpoint,
    path="/precedence/{item_id}",
    meta_get=OperationMeta(
        exceptions=[MissingPartError, QuotaError],
        responses=[ResponseSpec(429, "Slow down")],
    ),
):
    """Derived < declared exceptions < explicit ResponseSpec, per status."""

    async def get(self, path: PrecedencePath) -> Item:
        """Get an item by id."""
        return Item(id=path.item_id)


class DeclaredErrorsApp(BaseApp):
    """App exercising exception declaration, merging, and precedence."""

    async def wire(self) -> None:
        self.include_endpoint(DeclaredErrorsEndpoint())
        self.include_endpoint(MergedStatusEndpoint())
        self.include_endpoint(PrecedenceEndpoint())
        self.include_openapi(title="declared", version="1")


def test_declared_exceptions_derive_and_cascade() -> None:
    """Entries derive entirely from the classes — status, schema, description — with the
    class-level meta's entries extending the operation's, and the document stays valid."""
    with TestClient(DeclaredErrorsApp()) as client:
        document = client.get("/openapi.json").json()
        validate(document)
        responses = document["paths"]["/declared"]["get"]["responses"]
        gone = responses["410"]
        assert gone["description"] == "Widget gone"
        assert gone["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/WidgetGoneProblem"
        }
        quota = responses["429"]
        assert quota["description"] == "Quota exceeded"
        assert quota["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/Quota"
        }
        teapot = responses["418"]
        assert teapot["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/TeapotStruct"
        }
        schemas = document["components"]["schemas"]
        assert schemas["WidgetGoneProblem"]["properties"]["type"] == {"const": "widget-gone"}
        assert schemas["WidgetGoneProblem"]["properties"]["status"] == {"const": 410}
        assert schemas["Quota"]["properties"]["statusCode"] == {"const": 429, "default": 429}
        assert schemas["Quota"]["properties"]["errorCode"] == {
            "const": "quota-exceeded",
            "default": "quota-exceeded",
        }


def test_declared_parameterized_error_documents_params() -> None:
    """A parameterized error's docs model carries detail and the params schema."""
    with TestClient(DeclaredErrorsApp()) as client:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]
        missing = schemas["MissingPartProblem"]
        assert missing["properties"]["type"] == {"const": "missing-part"}
        assert missing["properties"]["detail"] == {"type": "string"}
        assert missing["properties"]["params"] == {"$ref": "#/components/schemas/MissingPartParams"}


def test_declared_errors_sharing_a_status_merge_as_oneof() -> None:
    """Same-status declarations merge into one entry with a oneOf and joined description."""
    with TestClient(DeclaredErrorsApp()) as client:
        entry = client.get("/openapi.json").json()["paths"]["/merged"]["get"]["responses"]["404"]
        assert entry["description"] == "Not found / Missing part"
        assert entry["content"]["application/json"]["schema"] == {
            "oneOf": [
                {"$ref": "#/components/schemas/NotFoundProblem"},
                {"$ref": "#/components/schemas/MissingPartProblem"},
            ]
        }


def test_error_response_precedence_per_status() -> None:
    """Declared exceptions beat the derived entry; an explicit ResponseSpec beats both."""
    with TestClient(DeclaredErrorsApp()) as client:
        responses = client.get("/openapi.json").json()["paths"]["/precedence/{item_id}"]["get"][
            "responses"
        ]
        # the path-derived 404 is replaced by the declared error's precise schema
        assert responses["404"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/MissingPartProblem"
        }
        # the declared 429 (QuotaError) is replaced by the explicit ResponseSpec
        assert responses["429"]["description"] == "Slow down"
        assert "content" not in responses["429"]


class BadDeclApp(BaseApp):
    """Invalid app declaring a non-error class in exceptions."""

    async def wire(self) -> None:
        """Wire the invalid declaration with OpenAPI enabled."""
        self.include_endpoint(BadDeclEndpoint())
        self.include_openapi(title="bad", version="1")


def test_invalid_exceptions_entries_fail_at_wiring() -> None:
    """A non-error entry in ``exceptions`` is a startup failure."""
    with pytest.raises(RuntimeError, match="'exceptions' entries must be concrete jero error"):
        TestClient(BadDeclApp())


class BadDeclEndpoint(
    Endpoint,
    path="/bad-decl",
    meta_get=OperationMeta(exceptions=cast(list[type[BaseHTTPError]], [str])),
):
    """Declares a non-error class; wiring must reject it."""

    async def get(self) -> Item:
        """Get an item."""
        return Item(id="id")


class SpecHouseAdapter(ErrorBodyAdapter[ErrorBody]):
    """House adapter used to verify the derived error schemas follow it."""

    status_field = "status_code"

    def compose(self, error: HTTPError) -> ErrorBody:
        return ErrorBody(error_code=error.type, error_message=str(error))


class AdaptedSpecEndpoint(
    Endpoint,
    path="/adapted/{item_id}",
    meta_get=OperationMeta(exceptions=[WidgetGoneError]),
):
    """A path-sourced endpoint under an adapter: derived and declared errors follow it."""

    async def get(self, path: PrecedencePath) -> Item:
        """Get an item by id."""
        return Item(id=path.item_id)


class AdaptedSpecApp(BaseApp):
    """App with the adapter registered and OpenAPI served."""

    async def wire(self) -> None:
        self.include_error_adapter(SpecHouseAdapter())
        self.include_endpoint(AdaptedSpecEndpoint())
        self.include_openapi(title="adapted", version="1")


def test_adapter_switches_error_schemas_in_the_spec() -> None:
    """With an adapter registered, derived and Problem-family declared errors document the
    adapter's per-status body instead of Problem, and the document stays valid."""
    with TestClient(AdaptedSpecApp()) as client:
        document = client.get("/openapi.json").json()
        validate(document)
        responses = document["paths"]["/adapted/{item_id}"]["get"]["responses"]
        assert responses["404"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorBody404"
        }
        assert responses["410"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorBody410"
        }
        schemas = document["components"]["schemas"]
        assert schemas["ErrorBody404"]["properties"]["statusCode"] == {
            "const": 404,
            "default": 404,
        }
        assert "Problem" not in schemas


# --- Favicon ---


class FaviconApp(BaseApp):
    """App wiring the docs with a favicon (a Path, a URL, or none)."""

    def __init__(self, favicon: Path | str | None) -> None:
        self._favicon = favicon
        super().__init__()

    async def wire(self) -> None:
        """Serve the spec and docs with the configured favicon."""
        self.include_endpoint(OpenEndpoint())
        self.include_openapi(title="fav", version="1", favicon=self._favicon)


def test_favicon_path_is_served_and_linked(tmp_path: Path) -> None:
    """A Path favicon is read once at wiring, served precomputed at /favicon.ico,
    linked in the default docs page, and absent from the generated document."""
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"png-bytes")
    with TestClient(FaviconApp(icon)) as client:
        resp = client.get("/favicon.ico")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == b"png-bytes"
        assert '<link rel="icon" href="/favicon.ico">' in client.get("/docs").text
        assert "/favicon.ico" not in client.get("/openapi.json").json()["paths"]


def test_favicon_url_is_linked_verbatim() -> None:
    """A str favicon is a URL emitted verbatim in the link; nothing is served."""
    with TestClient(FaviconApp("https://cdn.example.com/icon.svg")) as client:
        docs = client.get("/docs").text
        assert '<link rel="icon" href="https://cdn.example.com/icon.svg">' in docs
        assert client.get("/favicon.ico").status_code == 404


def test_no_favicon_means_no_link() -> None:
    """Without a favicon the default docs page carries no icon link."""
    with TestClient(FaviconApp(None)) as client:
        assert 'rel="icon"' not in client.get("/docs").text


def test_favicon_failures_are_wiring_errors(tmp_path: Path) -> None:
    """A missing file or an unsupported suffix fails at startup, not at request time."""
    with pytest.raises(RuntimeError, match="not readable"):
        TestClient(FaviconApp(tmp_path / "missing.png"))
    unsupported = tmp_path / "icon.txt"
    unsupported.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="unsupported suffix"):
        TestClient(FaviconApp(unsupported))


# --- Param Structs are inlined into `parameters`, not emitted as components ---


class PruneSlug(StrEnum):
    """A nested enum referenced by a query param field."""

    A = "a"
    B = "b"


class PruneConvPath(Struct):
    """Path param source — expanded into `parameters`, never `$ref`'d."""

    conversation_id: str


class PruneSearchQuery(Struct):
    """Query param source — expanded into `parameters`, never `$ref`'d."""

    slug: PruneSlug = PruneSlug.A


class PruneEchoBody(Struct):
    """A request/response body — referenced, so it stays a component."""

    value: int


class PruneEndpoint(Endpoint, path="/c/{conversation_id}"):
    """Binds a path Struct, a query Struct, and a body Struct."""

    async def post(
        self, path: PruneConvPath, params: PruneSearchQuery, json: PruneEchoBody
    ) -> PruneEchoBody:
        """Echo the body; the point is the generated schema, not the behaviour."""
        _ = (path, params)
        return json


class PruneApp(BaseApp):
    """Expose the endpoint plus the OpenAPI document."""

    async def wire(self) -> None:
        """Wire the docs and the endpoint."""
        self.include_openapi(title="t", version="1")
        self.include_endpoint(PruneEndpoint())


def test_param_only_structs_are_not_emitted_as_components() -> None:
    """A path/query Struct is expanded field-by-field into `parameters`, so its own
    component is pruned — while a nested enum it references (reachable via the inlined
    params) and the body/response model are kept."""
    with TestClient(PruneApp()) as client:
        document = client.get("/openapi.json").json()
    schemas = document["components"]["schemas"]
    assert "PruneConvPath" not in schemas
    assert "PruneSearchQuery" not in schemas
    assert "PruneSlug" in schemas
    assert "PruneEchoBody" in schemas
    params = {
        (p["name"], p["in"])
        for p in document["paths"]["/c/{conversation_id}"]["post"]["parameters"]
    }
    assert params == {("conversation_id", "path"), ("slug", "query")}


class SharedInfo(Struct):
    """Used as *both* a path param source and the response model."""

    conversation_id: str


class SharedEndpoint(Endpoint, path="/y/{conversation_id}"):
    """Binds the path Struct that is also returned as the response body."""

    async def get(self, path: SharedInfo) -> SharedInfo:
        """Return the path Struct as the body."""
        return path


class SharedApp(BaseApp):
    """Expose the shared-Struct endpoint plus the OpenAPI document."""

    async def wire(self) -> None:
        """Wire the docs and the endpoint."""
        self.include_openapi(title="t", version="1")
        self.include_endpoint(SharedEndpoint())


def test_param_struct_also_used_as_a_body_is_kept() -> None:
    """A Struct used as a param *and* a response is referenced, so it is not pruned."""
    with TestClient(SharedApp()) as client:
        document = client.get("/openapi.json").json()
    schemas = document["components"]["schemas"]
    assert "SharedInfo" in schemas
    response = document["paths"]["/y/{conversation_id}"]["get"]["responses"]["200"]
    assert response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SharedInfo"
    }


# --- scalar_config passthrough to the Scalar UI ---


class DocsConfigEndpoint(Endpoint, path="/dc"):
    """A trivial endpoint so the app has an operation to document."""

    async def get(self) -> PruneEchoBody:
        """Return a trivial body."""
        return PruneEchoBody(value=1)


class DocsConfigApp(BaseApp):
    """Serve docs with a Scalar config passthrough."""

    async def wire(self) -> None:
        """Wire docs (with scalar_config) and the endpoint."""
        self.include_openapi(title="t", version="1", scalar_config=ScalarConfig(hide_models=True))
        self.include_endpoint(DocsConfigEndpoint())


def test_scalar_config_forwarded_to_scalar_ui() -> None:
    """A ScalarConfig's set fields render as Scalar's data-configuration (HTML-escaped JSON)."""
    with TestClient(DocsConfigApp()) as client:
        page = client.get("/docs").text
    assert 'data-configuration="{&quot;hideModels&quot;:true}"' in page


def test_scalar_config_absent_by_default() -> None:
    """No scalar_config → no data-configuration attribute; the default page is unchanged."""
    with TestClient(PruneApp()) as client:
        page = client.get("/docs").text
    assert "data-configuration" not in page


# --- Param prune follows discriminator mappings when scanning references ---


class TagA(Struct, tag=True):
    """One member of a tagged union."""

    a: int


class TagB(Struct, tag=True):
    """The other member of a tagged union."""

    b: int


class TaggedParamPath(Struct):
    """A path param source alongside a tagged-union response."""

    id: str


class TaggedStreamEndpoint(Endpoint, path="/t/{id}"):
    """An op with a param (triggers the prune) and a tagged-union NDJSON response."""

    async def _items(self) -> AsyncIterator[TagA | TagB]:
        """Yield a union item."""
        yield TagA(a=1)

    async def get(self, path: TaggedParamPath) -> NDJSONStreamingResponse[TagA | TagB]:
        """Stream the tagged union."""
        _ = path
        return NDJSONStreamingResponse(stream=self._items())


class TaggedStreamApp(BaseApp):
    """Expose the tagged-union streaming endpoint plus the docs."""

    async def wire(self) -> None:
        """Wire docs and the endpoint."""
        self.include_openapi(title="t", version="1")
        self.include_endpoint(TaggedStreamEndpoint())


def test_param_prune_keeps_tagged_union_members_via_discriminator() -> None:
    """With a param (triggering the prune) and a tagged-union response, the reference scan
    follows the discriminator mapping — so union members survive while the param Struct is
    dropped."""
    with TestClient(TaggedStreamApp()) as client:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert "TaggedParamPath" not in schemas
    assert "TagA" in schemas
    assert "TagB" in schemas
