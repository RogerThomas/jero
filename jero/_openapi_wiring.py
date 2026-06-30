"""The bridge from a wired operation to the OpenAPI builder's input.

This is the layer that knows *both* sides: jero's resolved wiring contracts (``Sources``,
``FormSpec``, ``OperationSpec``, the return-kind classification) and
:mod:`jero.openapi`'s plain input records. It is split out of :mod:`jero.core` — that file
is large, and this translation is the one place the two vocabularies meet. It imports the
contracts from the :mod:`jero._wiring_types` leaf rather than from ``core``, so the
dependency graph stays acyclic (``core`` imports *this* module, never the reverse).
"""

from collections.abc import Sequence
from typing import cast, get_args

from msgspec import Struct

from jero._wiring_types import (
    FormSpec,
    OperationMeta,
    OperationSpec,
    ReturnKind,
    Sources,
    is_struct_type,
    strip_list,
)
from jero.openapi import (
    BodySpec,
    Error,
    FormFieldSpec,
    OperationInput,
    ParamSpec,
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


def _item_struct(annotation: object) -> type[Struct] | None:
    """The streamed/enveloped item type — the *first* type arg (``T`` in ``Wrapper[T, H]``),
    returned only if it's a Struct. Positional, so a non-Struct ``T`` (e.g. ``SSEResponse``'s
    ``str``) is never mistaken for the (later) header type ``H``."""
    args = get_args(annotation)
    item = args[0] if args else None
    return cast("type[Struct]", item) if is_struct_type(item) else None


def _response_header_type(kind: ReturnKind, annotation: object) -> type[Struct] | None:
    """The typed response-header Struct ``H`` from a response wrapper's annotation, if any.
    Its position depends on the wrapper: ``Bytes``/``Streaming`` take only ``H``; the rest
    take ``T`` then ``H`` (so ``H`` is the second arg, present only when both are given)."""
    args = get_args(annotation)
    if kind in ("bytes-response", "stream-bytes"):
        candidate = args[0] if args else None
    elif kind in ("json-response", "stream-ndjson", "stream-sse"):
        candidate = args[1] if len(args) > 1 else None
    else:
        return None
    return cast("type[Struct]", candidate) if is_struct_type(candidate) else None


def _error_responses(sources: Sources, *, authed: bool) -> list[ResponseEntry]:
    """The error responses a handler can actually produce, derived from its sources."""
    has_body = sources.json_decoder is not None or sources.form is not None
    statuses: dict[int, str] = {}
    if has_body or sources.params is not None or sources.headers is not None:
        statuses[400] = "Bad request"
    if has_body:
        statuses[422] = "Request failed validation"
    if sources.form is not None:
        statuses[415] = "Unsupported media type"
    if sources.path is not None:
        statuses[404] = "Not found"
    if authed:
        statuses[401] = "Authentication failed"
    statuses[500] = "Internal server error"
    return [
        ResponseEntry(status, detail, "application/json", model=Error)
        for status, detail in statuses.items()
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


def _success_entry(status: int, sources: Sources) -> ResponseEntry:
    kind = sources.return_kind
    annotation = sources.return_annotation
    description = _STATUS_TEXT.get(status, "Successful response")
    headers = _response_header_type(kind, annotation)
    if kind in ("bytes", "bytes-response", "stream-bytes"):
        return ResponseEntry(status, description, "application/octet-stream", headers=headers)
    if kind == "stream-ndjson":
        item = _item_struct(annotation)
        if item is None:  # bare NDJSONStreamingResponse (no [T]) -> any JSON object per line
            return ResponseEntry(
                status, description, "application/x-ndjson", schema={}, headers=headers
            )
        return ResponseEntry(
            status, description, "application/x-ndjson", model=item, headers=headers
        )
    if kind == "stream-sse":
        item = _item_struct(annotation)
        if item is None:  # SSEResponse[str] / bare -> the data is a plain string
            return ResponseEntry(
                status, description, "text/event-stream", schema={"type": "string"}, headers=headers
            )
        return ResponseEntry(status, description, "text/event-stream", model=item, headers=headers)
    if kind == "json-response":
        item = _item_struct(annotation)
        if item is None:  # bare JSONResponse (no [T]) -> any JSON
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


def operation_input(spec: OperationSpec) -> OperationInput:
    """Translate a captured operation into the builder's input record.

    Un-underscored: it crosses the ``core`` / ``openapi`` boundary (``core`` imports it
    lazily). Everything else in this module is a private helper to this function.
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
    # Responses cascade: derived (lowest), then class-meta, then op-meta (highest), by status.
    responses: dict[int, ResponseEntry] = {}
    success = _success_entry(spec.success_status, spec.sources)
    responses[success.status] = success
    for entry in _error_responses(spec.sources, authed=spec.authed):
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
    )
