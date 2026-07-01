"""OpenAPI 3.1 document assembly — the dependency-free builder.

This module never imports :mod:`jero.core`. ``core`` does all the jero-specific
introspection (resolving each handler's sources, return type, auth, and metadata) and
hands this module a list of fully-resolved :class:`OperationInput` records expressed in
terms of plain values plus msgspec ``Struct`` types. :func:`build_openapi` turns those
into the document.

The schema work is msgspec's: every referenced ``Struct`` is collected and passed to a
single :func:`msgspec.json.schema_components` call, which emits OpenAPI-3.1-compatible
JSON Schema — honoring ``rename`` (camelCase on the wire) and ``msgspec.Meta`` field
constraints (``ge`` -> ``minimum``, ``pattern``, ``description``, ``examples``, …) with
no extra work here.
"""

from dataclasses import dataclass
from typing import Any, Literal, cast

from msgspec import Struct
from msgspec.json import schema_components
from msgspec.structs import fields as struct_fields

type Location = Literal["path", "query", "header"]  # where a request parameter lives
type ApiKeyLocation = Literal["header", "query", "cookie"]  # the OpenAPI apiKey ``in``
type SchemeType = Literal["http", "apiKey", "oauth2", "openIdConnect"]


class SecurityScheme(Struct, frozen=True):
    """One OpenAPI security scheme, declared on an authenticator via the
    ``openapi_security`` class attribute (the :class:`~jero.BearerAuth` /
    :class:`~jero.BasicAuth` bases set it for you).

    Build one with a constructor rather than by hand. ``scheme_name`` is the key the
    scheme is registered under in ``components.securitySchemes`` and referenced by each
    operation's ``security``. Note that a bearer token carried in a *cookie* is not an
    ``http``/``bearer`` scheme (that is the ``Authorization`` header specifically) — it
    is ``api_key(location="cookie")``.
    """

    type: SchemeType
    scheme_name: str
    scheme: str | None = None  # http: "bearer" / "basic"
    bearer_format: str | None = None  # http bearer: e.g. "JWT"
    name: str | None = None  # apiKey: the header / query / cookie name
    location: ApiKeyLocation | None = None  # apiKey: the OpenAPI ``in`` (``in`` is a keyword)
    description: str | None = None

    @classmethod
    def http_bearer(
        cls,
        *,
        bearer_format: str | None = None,
        description: str | None = None,
        scheme_name: str = "bearerAuth",
    ) -> "SecurityScheme":
        """An ``Authorization: Bearer <token>`` scheme (the authed-route default)."""
        return cls(
            type="http",
            scheme="bearer",
            bearer_format=bearer_format,
            description=description,
            scheme_name=scheme_name,
        )

    @classmethod
    def http_basic(
        cls, *, description: str | None = None, scheme_name: str = "basicAuth"
    ) -> "SecurityScheme":
        """An ``Authorization: Basic <credentials>`` scheme."""
        return cls(type="http", scheme="basic", description=description, scheme_name=scheme_name)

    @classmethod
    def api_key(
        cls,
        *,
        name: str,
        location: ApiKeyLocation,
        description: str | None = None,
        scheme_name: str = "apiKeyAuth",
    ) -> "SecurityScheme":
        """A token carried in a named header, query param, or cookie."""
        return cls(
            type="apiKey",
            name=name,
            location=location,
            description=description,
            scheme_name=scheme_name,
        )

    def to_openapi(self) -> dict[str, Any]:
        """Render the OpenAPI ``securitySchemes`` entry for this scheme."""
        scheme: dict[str, Any]
        if self.type == "http":
            scheme = {"type": "http", "scheme": self.scheme}
            if self.bearer_format is not None:
                scheme["bearerFormat"] = self.bearer_format
        elif self.type == "apiKey":
            scheme = {"type": "apiKey", "in": self.location, "name": self.name}
        else:
            scheme = {"type": self.type}
        if self.description is not None:
            scheme["description"] = self.description
        return scheme


class ResponseSpec(Struct):
    """A response to document on an operation, declared in ``*Meta.responses``.

    Use it for responses the framework can't infer — a domain ``409``, a ``429``, a
    richer error model — or to override a derived entry by reusing its ``status``.

    ``model`` is the response body Struct. ``content_type`` defaults to
    ``application/json`` when a ``model`` is given; set it (with no ``model``) to document
    a schemaless body of another media type (e.g. ``text/csv``). With neither, the
    response has no body.
    """

    status: int
    description: str
    model: type[Struct] | None = None
    content_type: str | None = None


class Error(Struct):
    """jero's uniform error envelope — the shared schema derived error responses point at."""

    error: str


class ModelMeta(Struct):
    """OpenAPI metadata for a model (a wire ``Struct``), attached via the ``meta=`` class
    keyword of :class:`~jero.Struct`.

    ``description`` becomes the model's schema description — explicit, never inferred from
    the class docstring. ``name`` overrides the key the model gets under
    ``components.schemas`` (and every ``$ref`` that points at it); use it to give a model a
    stable public name or to disambiguate two same-named Structs that would otherwise
    collide.
    """

    description: str | None = None
    name: str | None = None


class Tag(Struct):
    """A document-level tag — the only place a tag carries a ``description`` (docs UIs render
    it as the blurb under the tag's section). Declare them on ``include_openapi(tags=...)``
    to describe and order the groups; operations reference a tag by its ``name``."""

    name: str
    description: str | None = None

    def to_openapi(self) -> dict[str, Any]:
        """Render the OpenAPI ``tags`` entry for this tag."""
        entry: dict[str, Any] = {"name": self.name}
        if self.description is not None:
            entry["description"] = self.description
        return entry


@dataclass(slots=True)
class Info:
    """The document's ``info`` block, ``servers``, and declared document-level ``tags``."""

    title: str
    version: str
    description: str | None = None
    servers: tuple[str, ...] = ()
    tags: tuple[Tag, ...] = ()


@dataclass(slots=True)
class ParamSpec:
    """A Struct whose fields expand into parameters at one location."""

    location: Location
    struct: type[Struct]


@dataclass(slots=True)
class FormFieldSpec:
    """One multipart form field. ``payload`` is the field's value type (any msgspec-typeable
    annotation, ``Meta`` included), schema'd through the shared components pass; ``binary``
    fields (files / raw bytes) are documented as ``{type: string, format: binary}`` instead."""

    wire_name: str
    payload: object
    binary: bool
    repeated: bool
    required: bool


@dataclass(slots=True)
class BodySpec:
    """A request body: a ``model`` (referenced by ``$ref``), a set of multipart
    ``form_fields``, or neither (raw bytes -> binary schema)."""

    content_type: str
    model: type[Struct] | None = None
    form_fields: tuple[FormFieldSpec, ...] = ()
    required: bool = True


@dataclass(slots=True)
class ResponseEntry:
    """One documented response: a ``model`` ($ref), a ``list`` of it, a verbatim
    ``schema``, or no content. ``headers`` expands a Struct into response headers."""

    status: int
    description: str
    content_type: str | None = None
    model: type[Struct] | None = None
    is_list: bool = False
    schema: dict[str, Any] | None = None
    headers: type[Struct] | None = None


@dataclass(slots=True)
class OperationInput:
    """One fully-resolved operation, as ``core`` hands it over."""

    method: str  # lowercase HTTP verb
    path: str  # e.g. "/widgets/{widgetId}"
    operation_id: str
    responses: tuple[ResponseEntry, ...]
    tags: tuple[Tag, ...] = ()  # the operation's tags (bare names normalized to name-only Tags)
    summary: str | None = None
    description: str | None = None
    params: tuple[ParamSpec, ...] = ()
    body: BodySpec | None = None
    security: tuple[str, ...] = ()  # required scheme names (referenced from securitySchemes)


class OpenAPINameConflictError(Exception):
    """Two OpenAPI components would claim the same ``components.schemas`` name — e.g. two
    models with the same ``ModelMeta(name=...)``, or a ``name`` that hits an existing key."""


def _ref_name(ref_schema: dict[str, Any]) -> str:
    """The bare component name from a ``{"$ref": "#/components/schemas/Name"}`` schema."""
    return ref_schema["$ref"].rsplit("/", 1)[-1]


def _collect_types(operations: tuple[OperationInput, ...]) -> list[object]:
    """Every type referenced anywhere — param/body/response Structs and non-binary form
    field payloads (which may be ``Annotated`` scalars) — de-duplicated in first-seen
    order, plus the shared ``Error`` envelope. One list feeds the ``schema_components`` call."""
    seen: dict[object, None] = {}
    for op in operations:
        for param in op.params:
            seen.setdefault(param.struct, None)
        if op.body is not None:
            if op.body.model is not None:
                seen.setdefault(op.body.model, None)
            for field in op.body.form_fields:
                if not field.binary:
                    seen.setdefault(field.payload, None)
        for response in op.responses:
            if response.model is not None:
                seen.setdefault(response.model, None)
            if response.headers is not None:
                seen.setdefault(response.headers, None)
    seen.setdefault(Error, None)
    return list(seen)


@dataclass(slots=True)
class _Schemas:
    """The result of the single ``schema_components`` call: the assembled
    ``components.schemas`` plus a per-Struct ``$ref`` map and the components by name."""

    refs: dict[object, dict[str, Any]]
    components: dict[str, dict[str, Any]]

    def ref(self, struct: type[Struct]) -> dict[str, Any]:
        """The ``{"$ref": …}`` schema for a Struct."""
        return self.refs[struct]

    def schema_for(self, payload: object) -> dict[str, Any]:
        """The schema msgspec produced for a payload type — inline for a scalar (with any
        ``Meta`` applied), a ``$ref`` for a Struct."""
        return self.refs[payload]

    def properties(self, struct: type[Struct]) -> dict[str, Any]:
        """The component's ``properties`` map for a Struct (field name -> schema)."""
        return self.components[_ref_name(self.refs[struct])].get("properties", {})

    def model_examples(self, model: type[Struct]) -> list[dict[str, Any]]:
        """Whole-object examples composed from the model's per-field ``examples``."""
        return _model_examples(self.properties(model))


def _model_examples(properties: dict[str, Any]) -> list[dict[str, Any]]:
    """Whole-object examples composed from the fields' own ``examples``, zipped by index:
    if every property carries ``examples`` (e.g. via ``msgspec.Meta(examples=...)``), the
    k-th value of each builds the k-th object example. The count is the largest field's;
    a field with fewer reuses its last. If any property lacks examples, none are composed
    (a partial object would omit required fields)."""
    by_field: dict[str, list[Any]] = {}
    for name, prop in properties.items():
        examples = prop.get("examples")
        if not examples:
            return []
        by_field[name] = examples
    if not by_field:
        return []
    count = max(len(examples) for examples in by_field.values())
    return [
        {
            name: examples[i] if i < len(examples) else examples[-1]
            for name, examples in by_field.items()
        }
        for i in range(count)
    ]


def _rewrite_refs(node: object, renames: dict[str, str]) -> None:
    """Rewrite, in place, every ``$ref`` under ``node`` whose component was renamed."""
    if isinstance(node, dict):
        mapping = cast("dict[str, Any]", node)
        ref = mapping.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            if name in renames:
                mapping["$ref"] = f"#/components/schemas/{renames[name]}"
        for value in mapping.values():
            _rewrite_refs(value, renames)
    elif isinstance(node, list):
        for item in cast("list[Any]", node):
            _rewrite_refs(item, renames)


def _apply_component_names(
    components: dict[str, dict[str, Any]],
    refs: dict[object, dict[str, Any]],
    renames: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Rename component keys per ``renames`` and rewrite every ``$ref`` (in the components
    and in the per-type ref map) to match, in place. Raises ``OpenAPINameConflictError`` if two
    components would end up sharing a name."""
    seen: set[str] = set()
    for name in (renames.get(name, name) for name in components):
        if name in seen:
            raise OpenAPINameConflictError(
                f"OpenAPI component name {name!r} is claimed by more than one model — "
                f"give each a unique ModelMeta(name=...)",
            )
        seen.add(name)
    renamed = {renames.get(name, name): schema for name, schema in components.items()}
    for schema in renamed.values():
        _rewrite_refs(schema, renames)
    for ref in refs.values():
        _rewrite_refs(ref, renames)
    return renamed


def _build_schemas(types: list[object]) -> _Schemas:
    schemas, components = schema_components(types, ref_template="#/components/schemas/{name}")
    refs = dict(zip(types, schemas, strict=True))
    # Docstrings never surface to the public spec: strip msgspec's docstring-derived
    # component descriptions, then inject any explicit ModelMeta description (attached via
    # jero.Struct's meta=). Field-level descriptions live in `properties`, untouched.
    for component in components.values():
        component.pop("description", None)
    renames: dict[str, str] = {}
    for typ, ref in refs.items():
        # Read from the type's own __dict__, not getattr (which walks the MRO): a subclass
        # without its own meta= must not inherit — and publish — its parent's metadata.
        meta = typ.__dict__.get("__model_meta__") if isinstance(typ, type) else None
        if not isinstance(meta, ModelMeta):
            continue
        name = _ref_name(ref)
        if meta.description is not None:
            components[name]["description"] = meta.description
        if meta.name is not None and meta.name != name:
            renames[name] = meta.name
    if renames:
        components = _apply_component_names(components, refs, renames)
    return _Schemas(refs=refs, components=components)


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _binary_schema() -> dict[str, Any]:
    return {"type": "string", "format": "binary"}


def _examples_map(values: list[Any]) -> dict[str, dict[str, Any]]:
    """An OpenAPI Media-Type ``examples`` map (named, selectable in the docs UI)."""
    return {f"example {i + 1}": {"value": value} for i, value in enumerate(values)}


def _content(
    content_type: str, schema: dict[str, Any], examples: dict[str, dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    media: dict[str, Any] = {"schema": schema}
    if examples:
        media["examples"] = examples
    return {content_type: media}


def _parameters(params: tuple[ParamSpec, ...], schemas: _Schemas) -> list[dict[str, Any]]:
    """Expand each param Struct's fields into individual parameter objects. Path/query
    names use the wire (encode) name; header names invert the request mangle
    (``x_token`` -> ``x-token``), mirroring how responses name typed headers."""
    parameters: list[dict[str, Any]] = []
    for param in params:
        props = schemas.properties(param.struct)
        for field_info in struct_fields(param.struct):
            if param.location == "header":
                name = field_info.name.replace("_", "-")
            else:
                name = field_info.encode_name
            parameters.append(
                {
                    "name": name,
                    "in": param.location,
                    # path params are always required; elsewhere a default makes it optional
                    "required": param.location == "path" or field_info.required,
                    "schema": props.get(field_info.encode_name, {}),
                }
            )
    return parameters


def _form_object_schema(fields: tuple[FormFieldSpec, ...], schemas: _Schemas) -> dict[str, Any]:
    """The ``multipart/form-data`` object schema: each field's value schema (binary for
    files, else msgspec's schema for the payload — ``Meta`` and ``$ref``s included),
    wrapped in an array when the field repeats."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in fields:
        item = _binary_schema() if field.binary else schemas.schema_for(field.payload)
        properties[field.wire_name] = _array(item) if field.repeated else item
        if field.required:
            required.append(field.wire_name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _body_schema(body: BodySpec, schemas: _Schemas) -> dict[str, Any]:
    if body.model is not None:
        return schemas.ref(body.model)
    if body.form_fields:
        return _form_object_schema(body.form_fields, schemas)
    return _binary_schema()


def _request_body(body: BodySpec, schemas: _Schemas) -> dict[str, Any]:
    examples = None
    if body.model is not None:
        composed = schemas.model_examples(body.model)
        if composed:
            examples = _examples_map(composed)
    return {
        "required": body.required,
        "content": _content(body.content_type, _body_schema(body, schemas), examples),
    }


def _response_headers(headers: type[Struct], schemas: _Schemas) -> dict[str, Any]:
    props = schemas.properties(headers)
    return {
        field_info.name.replace("_", "-"): {"schema": props.get(field_info.encode_name, {})}
        for field_info in struct_fields(headers)
    }


def _response(entry: ResponseEntry, schemas: _Schemas) -> dict[str, Any]:
    response: dict[str, Any] = {"description": entry.description}
    if entry.content_type is not None:
        examples = None
        if entry.schema is not None:
            schema = entry.schema
        elif entry.model is not None:
            schema = _array(schemas.ref(entry.model)) if entry.is_list else schemas.ref(entry.model)
            composed = schemas.model_examples(entry.model)
            if composed:
                # a list response example is the whole array; a single response, each object
                examples = _examples_map([composed] if entry.is_list else composed)
        else:
            schema = _binary_schema()
        response["content"] = _content(entry.content_type, schema, examples)
    if entry.headers is not None:
        response["headers"] = _response_headers(entry.headers, schemas)
    return response


def _operation(op: OperationInput, schemas: _Schemas) -> dict[str, Any]:
    operation: dict[str, Any] = {}
    if op.tags:  # de-dupe names (the merged tags may repeat a name, e.g. class + op extend)
        operation["tags"] = list(dict.fromkeys(tag.name for tag in op.tags))
    operation["operationId"] = op.operation_id
    if op.summary is not None:
        operation["summary"] = op.summary
    if op.description is not None:
        operation["description"] = op.description
    parameters = _parameters(op.params, schemas)
    if parameters:
        operation["parameters"] = parameters
    if op.body is not None:
        operation["requestBody"] = _request_body(op.body, schemas)
    operation["responses"] = {
        str(entry.status): _response(entry, schemas) for entry in op.responses
    }
    if op.security:
        operation["security"] = [{name: []} for name in op.security]
    return operation


def _paths(operations: tuple[OperationInput, ...], schemas: _Schemas) -> dict[str, Any]:
    paths: dict[str, dict[str, Any]] = {}
    for op in operations:
        paths.setdefault(op.path, {})[op.method] = _operation(op, schemas)
    return paths


def _root_tags(declared: tuple[Tag, ...]) -> list[dict[str, Any]]:
    """The document-level ``tags`` array — the declared tags, in order, with their
    descriptions. Every tag an operation references must be among these (``core`` enforces
    that at wiring), so there is nothing to synthesize here."""
    return [tag.to_openapi() for tag in declared]


def build_openapi(
    info: Info,
    operations: tuple[OperationInput, ...],
    security_schemes: dict[str, SecurityScheme],
) -> dict[str, Any]:
    """Assemble the OpenAPI 3.1 document from resolved operations.

    Every referenced Struct is collected and schema'd in one pass, so models shared
    across operations are emitted once and referenced by ``$ref``.
    """
    schemas = _build_schemas(_collect_types(operations))
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": info.title, "version": info.version},
        "paths": _paths(operations, schemas),
    }
    if info.description is not None:
        document["info"]["description"] = info.description
    if info.servers:
        document["servers"] = [{"url": url} for url in info.servers]
    root_tags = _root_tags(info.tags)
    if root_tags:
        document["tags"] = root_tags
    components: dict[str, Any] = {}
    if schemas.components:
        components["schemas"] = schemas.components
    if security_schemes:
        components["securitySchemes"] = {
            name: scheme.to_openapi() for name, scheme in security_schemes.items()
        }
    if components:
        document["components"] = components
    return document
