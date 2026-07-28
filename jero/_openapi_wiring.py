"""The bridge from a wired operation to the OpenAPI builder's input.

This is the layer that knows *both* sides: jero's resolved wiring contracts (``Sources``,
``FormSpec``, ``OperationSpec``, the return-kind classification) and
:mod:`jero.openapi`'s plain input records. It is split out of :mod:`jero.core` — that file
is large, and this translation is the one place the two vocabularies meet. It imports the
contracts from the :mod:`jero._wiring_types` leaf rather than from ``core``, so the
dependency graph stays acyclic (``core`` imports *this* module, never the reverse).
"""

from collections.abc import Mapping, Sequence
from functools import cache
from types import UnionType
from typing import Any, Literal, Union, cast, get_args, get_origin

from msgspec import Struct, defstruct
from msgspec.structs import fields as struct_fields

from jero._wiring_types import (
    FormSpec,
    OperationMeta,
    OperationSpec,
    ReturnKind,
    Sources,
    WiringError,
    is_struct_type,
    strip_list,
)
from jero.errors import (
    AuthenticationRequiredError,
    BaseHTTPError,
    ErrorBodyAdapter,
    HTTPError,
    InternalServerError,
    MalformedRequestError,
    NotFoundError,
    ParameterizedHTTPError,
    StructHTTPError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)
from jero.openapi import (
    BodySpec,
    FormFieldSpec,
    OperationInput,
    ParamSpec,
    ResponseBody,
    ResponseEntry,
    ResponseSpec,
    Tag,
)

# Human-readable text for the success response of a given status (OpenAPI requires a
# ``description`` on every response).
_STATUS_TEXT: dict[int, str] = {
    200: "Successful response",
    201: "Created",
    202: "Accepted",
    204: "No content",
}


def _as_tags(items: Sequence[str | Tag]) -> tuple[Tag, ...]:
    """Normalize meta tag entries to ``Tag``\\ s: a bare ``str`` becomes a name-only tag."""
    return tuple(Tag(item) if isinstance(item, str) else item for item in items)


def _merge_tags(class_meta: object, op_meta: OperationMeta | None) -> tuple[Tag, ...]:
    """Merge class-level and operation tags (``str`` names or describing ``Tag``\\ s), with
    the operation's *container type* choosing the rule: a ``list`` extends the class tags, a
    (non-empty) ``tuple`` replaces them. Empty operation tags inherit the class tags (the
    default ``()`` never clobbers).

    Returns the merged tags *without* de-duplicating: names are de-duped only when rendering
    the operation's ``tags`` array, and descriptions are reconciled (and conflicts caught)
    globally at build time — so two conflicting descriptions in one meta fail loud too,
    exactly as they do across operations."""
    class_tags = _as_tags(getattr(class_meta, "tags", ()))
    op_tags = op_meta.tags if op_meta is not None else ()
    if not op_tags:
        return class_tags  # nothing declared -> inherit the class tags
    if isinstance(op_tags, tuple):
        return _as_tags(op_tags)  # a tuple overrides
    return (*class_tags, *_as_tags(op_tags))  # a list extends


def _form_fields(form: FormSpec) -> tuple[FormFieldSpec, ...]:
    """Describe each multipart field for the builder. A file or raw-``bytes`` field is
    binary; every other field carries its resolved payload type (``Meta`` intact) for the
    builder to schema through the shared ``schema_components`` pass."""
    return tuple(
        FormFieldSpec(
            wire_name=field.wire_name,
            payload=field.payload_type,
            binary=field.file or field.payload_kind == "bytes",
            repeated=field.repeated,
            required=field.required,
        )
        for field in form.fields
    )


def _params_for(sources: Sources) -> tuple[ParamSpec, ...]:
    params: list[ParamSpec] = []
    if sources.path is not None:
        params.append(ParamSpec("path", sources.path))
    if sources.params is not None:
        params.append(ParamSpec("query", sources.params))
    if sources.headers is not None:
        params.append(ParamSpec("header", sources.headers))
    return tuple(params)


def _body_for(sources: Sources) -> BodySpec | None:
    if sources.json is not None:
        return BodySpec("application/json", model=sources.json)
    if sources.form is not None:
        return BodySpec("multipart/form-data", form_fields=_form_fields(sources.form))
    if sources.content:
        return BodySpec("application/octet-stream")  # raw bytes -> binary schema
    return None


# The user-facing wrapper name per return kind, for the item-type error message.
_WRAPPER_NAMES: dict[ReturnKind, str] = {
    "stream-ndjson": "NDJSONStreamingResponse",
    "stream-sse": "SSEResponse",
    "json-response": "JSONResponse",
    "created": "Created",
    "accepted": "Accepted",
}


def _item_payload(
    annotation: object, kind: ReturnKind, operation_id: str, *, allow_str: bool = False
) -> type[Struct] | UnionType | None:
    """The streamed/enveloped item type — the *first* type arg (``T`` in ``Wrapper[T, H]``,
    positional so a non-Struct ``T`` is never mistaken for the later header type ``H``) —
    as a schema payload: a Struct, or a union of Structs normalized to ``A | B`` form
    (msgspec schemas a union directly: ``anyOf``, plus a ``discriminator`` when the members
    are tagged). ``None`` for an unparameterized wrapper — and for ``str`` where the
    wrapper's bound allows it (SSE) — each caller documents its bare fallback. Any other
    ``T`` fails loud: a typed return annotation must never silently lose its schema."""
    args = get_args(annotation)
    item = args[0] if args else None
    if item is None or (allow_str and item is str):
        return None
    if is_struct_type(item):
        return cast("type[Struct]", item)
    if get_origin(item) in (Union, UnionType):
        members = get_args(item)
        if all(is_struct_type(member) or (allow_str and member is str) for member in members):
            union = members[0]
            for member in members[1:]:
                union = union | member
            return cast("UnionType", union)
    allowed = "a Struct, str, or a union of them" if allow_str else "a Struct or a union of Structs"
    raise WiringError(
        f"{operation_id}: {_WRAPPER_NAMES[kind]} item type must be {allowed}, got {item!r}"
    )


def _response_header_type(kind: ReturnKind, annotation: object) -> type[Struct] | None:
    """The typed response-header Struct ``H`` from a response wrapper's annotation, if any.
    Its position depends on the wrapper: ``Bytes``/``Streaming``/``NoContent`` take only
    ``H``; the rest take ``T`` then ``H`` (so ``H`` is the second arg, present only when
    both are given)."""
    args = get_args(annotation)
    if kind in ("bytes-response", "stream-bytes", "no-content"):
        candidate = args[0] if args else None
    elif kind in ("json-response", "stream-ndjson", "stream-sse", "created", "accepted"):
        candidate = args[1] if len(args) > 1 else None
    else:
        return None
    return cast("type[Struct]", candidate) if is_struct_type(candidate) else None


def _literal_of(value: object) -> type:
    """``Literal[value]`` built at runtime — subscripting ``Literal`` directly is a type
    form to checkers, so go through a cast and an ordinary mapping subscript."""
    return cast(Mapping[object, type], Literal)[value]


@cache
def _problem_docs_model(cls: type[HTTPError]) -> type[Struct]:
    """The per-class Problem schema model: the real body shape with ``type`` and
    ``status`` as consts, so the spec says exactly which codes an operation emits (clients
    dispatch on ``type``), plus the params schema for parameterized errors. Built once per
    error class and never instantiated — it exists for schema generation only."""
    name = cls.__name__.removesuffix("Error") or cls.__name__
    model_fields: list[object] = [
        ("type", _literal_of(cls.type)),
        ("title", str),
        ("status", _literal_of(cls.status)),
        ("docs", str | None, None),
    ]
    if issubclass(cls, ParameterizedHTTPError):
        model_fields += [("detail", str), ("params", cls.params_type)]
    return defstruct(
        f"{name}Problem",
        cast("Sequence[tuple[str, type]]", model_fields),
        kw_only=True,
        omit_defaults=True,
        module=cls.__module__,
    )


def _exception_docs_model(
    cls: type[BaseHTTPError], adapter: ErrorBodyAdapter[Any] | None
) -> type[Struct]:
    """The schema model a declared error class documents: the Struct family's composed
    wire model (consts and status as documented constants), the Problem family's
    per-class const model — or, when an adapter is registered, the adapter's body for
    that status (what the wire really carries)."""
    if issubclass(cls, StructHTTPError):
        return cls.wire_model
    problem_cls = cast("type[HTTPError]", cls)
    if adapter is not None:
        return adapter.docs_model(problem_cls.status)
    return _problem_docs_model(problem_cls)


def _error_description(cls: type[BaseHTTPError]) -> str:
    """The response description an error class carries: the Problem family's ``title``,
    the Struct family's explicit ``description``."""
    if issubclass(cls, StructHTTPError):
        return cls.description
    return cast("type[HTTPError]", cls).title


def _exception_entries(
    spec: OperationSpec, adapter: ErrorBodyAdapter[Any] | None, operation_id: str
) -> list[ResponseEntry]:
    """Response entries derived from the declared ``exceptions``: the class-level meta's
    first, extended by the operation's (both remain raiseable — the union, deduped),
    validated as concrete jero error classes and grouped by status. Several errors sharing
    a status merge into one entry whose body is a ``oneOf`` of their schemas."""
    declared: dict[type[BaseHTTPError], None] = {}
    for meta in (spec.class_meta, spec.op_meta):
        entries = cast("Sequence[object]", meta.exceptions if meta is not None else ())
        for entry in entries:
            concrete = (
                isinstance(entry, type)
                and hasattr(entry, "status")
                and issubclass(entry, (HTTPError, StructHTTPError))
            )
            if not concrete:
                raise WiringError(
                    f"{operation_id}: 'exceptions' entries must be concrete jero error "
                    f"classes (HTTPError or StructHTTPError subclasses); got {entry!r}",
                )
            declared.setdefault(cast("type[BaseHTTPError]", entry), None)
    by_status: dict[int, list[type[BaseHTTPError]]] = {}
    for cls in declared:
        by_status.setdefault(cls.status, []).append(cls)
    responses: list[ResponseEntry] = []
    for status, classes in by_status.items():
        description = " / ".join(dict.fromkeys(_error_description(cls) for cls in classes))
        models = tuple(dict.fromkeys(_exception_docs_model(cls, adapter) for cls in classes))
        if len(models) == 1:
            responses.append(
                ResponseEntry(status, description, "application/json", model=models[0])
            )
        else:
            responses.append(ResponseEntry(status, description, "application/json", one_of=models))
    return responses


# Each framework-generated error status maps to the built-in error it is raised as, so a
# derived response documents that error's real body — parameterized errors (400/422)
# contribute their ``detail`` + ``params`` schema — and stays consistent with the
# ``exceptions=``-declared path, which runs the same class through the same helpers.
_STATUS_ERRORS: dict[int, type[BaseHTTPError]] = {
    400: MalformedRequestError,
    422: ValidationFailedError,
    415: UnsupportedMediaTypeError,
    404: NotFoundError,
    401: AuthenticationRequiredError,
    500: InternalServerError,
}


def _error_responses(
    sources: Sources, *, authed: bool, adapter: ErrorBodyAdapter[Any] | None
) -> list[ResponseEntry]:
    """The error responses a handler can actually produce, derived from its sources. Each
    documents the built-in error it is raised as: the RFC 9457 Problem body — with
    ``detail`` + ``params`` for the parameterized 400/422 — or, when an error body adapter
    is registered, the adapter's per-status body (what the wire really carries)."""
    has_body = sources.json_decoder is not None or sources.form is not None
    statuses: list[int] = []
    if has_body or sources.params is not None or sources.headers is not None:
        statuses.append(400)
    if has_body:
        statuses.append(422)
    if sources.form is not None:
        statuses.append(415)
    if sources.path is not None:
        statuses.append(404)
    if authed:
        statuses.append(401)
    statuses.append(500)
    return [
        ResponseEntry(
            status,
            _error_description(_STATUS_ERRORS[status]),
            "application/json",
            model=_exception_docs_model(_STATUS_ERRORS[status], adapter),
        )
        for status in statuses
    ]


def _entry_from_spec(spec: ResponseSpec) -> ResponseEntry:
    """A user-declared ``ResponseSpec`` (from meta) as an internal response entry: a body
    referencing ``model``, a schemaless body of an explicit ``content_type``, or no body."""
    if spec.model is not None:
        return ResponseEntry(
            spec.status, spec.description, spec.content_type or "application/json", model=spec.model
        )
    if spec.content_type is not None:
        return ResponseEntry(spec.status, spec.description, spec.content_type, schema={})
    return ResponseEntry(spec.status, spec.description)


def _success_entry(
    kind: ReturnKind, status: int, annotation: object, operation_id: str
) -> ResponseEntry:
    """The documented response for one return kind — a union's member, or the sole
    kind/annotation/status of a non-union return."""
    description = _STATUS_TEXT.get(status, "Successful response")
    header_type = _response_header_type(kind, annotation)
    headers = () if header_type is None else (header_type,)
    if kind == "no-content":
        return ResponseEntry(status, description, headers=headers)
    if kind in ("bytes", "bytes-response", "stream-bytes"):
        return ResponseEntry(status, description, "application/octet-stream", headers=headers)
    if kind == "stream-ndjson":
        item = _item_payload(annotation, kind, operation_id)
        if item is None:  # bare NDJSONStreamingResponse (no [T]) -> any JSON object per line
            return ResponseEntry(
                status, description, "application/x-ndjson", schema={}, headers=headers
            )
        return ResponseEntry(
            status, description, "application/x-ndjson", model=item, headers=headers
        )
    if kind == "stream-sse":
        item = _item_payload(annotation, kind, operation_id, allow_str=True)
        if item is None:  # SSEResponse[str] / bare -> the data is a plain string
            return ResponseEntry(
                status, description, "text/event-stream", schema={"type": "string"}, headers=headers
            )
        return ResponseEntry(status, description, "text/event-stream", model=item, headers=headers)
    if kind in ("json-response", "created", "accepted"):
        item = _item_payload(annotation, kind, operation_id)
        if item is None:  # bare wrapper (no [T]) -> any JSON
            return ResponseEntry(
                status, description, "application/json", schema={}, headers=headers
            )
        return ResponseEntry(status, description, "application/json", model=item, headers=headers)
    # kind == "json": a Struct or list[Struct]
    item_ann, is_list = strip_list(annotation)
    if is_struct_type(item_ann):
        return ResponseEntry(
            status,
            description,
            "application/json",
            model=cast("type[Struct]", item_ann),
            is_list=is_list,
        )
    return ResponseEntry(status, description, "application/json", schema={})


def _merged_body_model(bodies: Sequence[ResponseBody], operation_id: str) -> UnionType:
    """The ``A | B`` body for several members sharing one status *and* media type, so they
    document as one ``anyOf`` (with msgspec's ``discriminator`` when the members are
    tagged) — identical to what those Structs produce inside ``JSONResponse[A | B]``."""
    merged: type[Struct] | UnionType | None = None
    for body in bodies:
        if body.model is None or body.is_list:
            # A bare/unparameterized wrapper documents an open `{}` body, and a list
            # documents an array; neither composes into an anyOf that still says anything.
            raise WiringError(
                f"{operation_id}: union return members sharing a status and media type must "
                f"each declare a Struct body to merge into one anyOf; a bare wrapper or a "
                f"list[Struct] member cannot be combined — give it its own status",
            )
        merged = body.model if merged is None else merged | body.model
    return cast("UnionType", merged)


def _merged_header_types(
    entries: Sequence[ResponseEntry], operation_id: str
) -> tuple[type[Struct], ...]:
    """Every member's ``H`` for one status, de-duplicated. A status has exactly one header
    map, so two members describing the same wire name with *different* field types cannot
    both be documented — that is a loud wiring failure rather than a silent last-wins."""
    by_wire_name: dict[str, object] = {}
    collected: dict[type[Struct], None] = {}
    for entry in entries:
        for header_type in entry.headers:
            for field_info in struct_fields(header_type):
                wire_name = field_info.name.replace("_", "-")
                previous = by_wire_name.get(wire_name)
                if previous is not None and previous != field_info.type:
                    raise WiringError(
                        f"{operation_id}: union return members sharing a status disagree on "
                        f"response header {wire_name!r} ({previous!r} vs {field_info.type!r}); "
                        f"one status has one header map, so give them distinct statuses or "
                        f"one shared header Struct",
                    )
                by_wire_name[wire_name] = field_info.type
            collected.setdefault(header_type, None)
    return tuple(collected)


def _merge_status_group(
    entries: Sequence[ResponseEntry], status: int, operation_id: str
) -> ResponseEntry:
    """Several union members resolving to one status, as the single response OpenAPI keys
    by that status: header maps unioned, and bodies grouped by media type — several at one
    media type merge into an ``anyOf``, while different media types sit side by side in
    ``content`` (which is keyed by media type precisely so they can).
    """
    by_media_type: dict[str, list[ResponseBody]] = {}
    for entry in entries:
        # A bodyless member (NoContent) contributes headers only; it has no media type.
        for body in entry.bodies:
            by_media_type.setdefault(body.content_type, []).append(body)
    bodies: list[ResponseBody] = []
    for media_type, group in by_media_type.items():
        # Two members can describe the *same* body: `BytesResponse[A] | BytesResponse[B]`
        # differ only in their headers, and both render the one binary schema. Identical
        # descriptions are a single response body, so dedupe before merging — otherwise a
        # pair that needs no merge at all would be sent to _merged_body_model, which has
        # nothing to build an anyOf from and would reject a perfectly sayable response.
        unique: list[ResponseBody] = []
        for body in group:
            if body not in unique:
                unique.append(body)
        bodies.append(
            unique[0]
            if len(unique) == 1
            else ResponseBody(media_type, model=_merged_body_model(unique, operation_id))
        )
    return ResponseEntry.over_media_types(
        status,
        _STATUS_TEXT.get(status, "Successful response"),
        tuple(bodies),
        _merged_header_types(entries, operation_id),
    )


def _success_entries(status: int, sources: Sources, operation_id: str) -> list[ResponseEntry]:
    """One documented response per success *status*: a non-union return contributes the one
    it declared, and a union contributes one entry per status its members resolve to, with
    several members at one status merged into the single response OpenAPI allows there."""
    if sources.return_kind != "union":
        return [
            _success_entry(sources.return_kind, status, sources.return_annotation, operation_id)
        ]
    by_status: dict[int, list[ResponseEntry]] = {}
    for member in sources.return_members:
        entry = _success_entry(member.kind, member.status, member.annotation, operation_id)
        by_status.setdefault(member.status, []).append(entry)
    return [
        group[0] if len(group) == 1 else _merge_status_group(group, member_status, operation_id)
        for member_status, group in by_status.items()
    ]


def operation_input(
    spec: OperationSpec, adapter: ErrorBodyAdapter[Any] | None = None
) -> OperationInput:
    """Translate a captured operation into the builder's input record.

    Un-underscored: it crosses the ``core`` / ``openapi`` boundary (``core`` imports it).
    Everything else in this module is a private helper to this function.
    """
    op_meta = spec.op_meta
    # Summary/description are explicit (OperationMeta) — never inferred from the docstring.
    summary = op_meta.summary if op_meta is not None else None
    description = op_meta.description if op_meta is not None else None
    operation_id = (
        op_meta.operation_id
        if op_meta is not None and op_meta.operation_id is not None
        else spec.operation_id_default
    )
    # Responses cascade by status: derived (lowest), then declared exceptions, then
    # explicit ResponseSpecs — class-meta before op-meta within each layer.
    responses: dict[int, ResponseEntry] = {}
    for entry in _success_entries(spec.success_status, spec.sources, operation_id):
        responses[entry.status] = entry
    for entry in _error_responses(spec.sources, authed=spec.auth_mode is not None, adapter=adapter):
        responses[entry.status] = entry
    for entry in _exception_entries(spec, adapter, operation_id):
        responses[entry.status] = entry
    if spec.class_meta is not None:
        for declared in spec.class_meta.responses:
            responses[declared.status] = _entry_from_spec(declared)
    if op_meta is not None:
        for declared in op_meta.responses:
            responses[declared.status] = _entry_from_spec(declared)
    return OperationInput(
        method=spec.method,
        path=spec.path,
        operation_id=operation_id,
        responses=tuple(responses.values()),
        tags=_merge_tags(spec.class_meta, op_meta),
        summary=summary,
        description=description,
        params=_params_for(spec.sources),
        body=_body_for(spec.sources),
        security=(spec.security_scheme.scheme_name,) if spec.security_scheme is not None else (),
        security_optional=spec.auth_mode == "optional",
    )
