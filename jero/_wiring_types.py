"""The resolved wiring contracts, shared by the request path and the OpenAPI generator.

These are the data types produced once at wiring time — the per-handler source model
(``Sources``), the compiled form spec, the class/operation metadata, and the captured
``OperationSpec`` handed to the OpenAPI builder. They live in their own leaf module
(depending only on msgspec and :mod:`jero.openapi`) so that both :mod:`jero.core` (which
builds them on the request path) and :mod:`jero._openapi_wiring` (which reads them to
build the spec) can import them without importing each other. Keeping the contracts
below both modules is what makes the dependency graph acyclic.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, get_args, get_origin

from msgspec import Struct
from msgspec.json import Decoder

from jero.openapi import ResponseSpec, SecurityScheme, Tag

# How a handler's return value is encoded onto the wire; see _return_kind / _result_sender.
type ReturnKind = Literal[
    "json",
    "json-response",
    "bytes",
    "bytes-response",
    "stream-bytes",
    "stream-ndjson",
    "stream-sse",
]
# How a multipart form field's body is decoded; see _payload_kind / _decode_form_payload.
type PayloadKind = Literal["bytes", "struct", "scalar"]


def is_struct_type(ann: object) -> bool:
    """True if ``ann`` is a ``msgspec.Struct`` subclass (i.e. a wire model)."""
    return isinstance(ann, type) and issubclass(ann, Struct)


def strip_list(ann: object) -> tuple[object, bool]:
    """Unwrap ``list[T]`` to ``(T, True)``; any other annotation to ``(ann, False)``."""
    if get_origin(ann) is not list:
        return ann, False
    args = get_args(ann)
    if len(args) != 1:
        return ann, False
    return args[0], True


class EndpointMeta(Struct):
    """OpenAPI metadata shared by all of an ``Endpoint``'s operations.

    ``tags`` are the groups this endpoint belongs to. An entry is a bare ``str`` (the tag
    name — the OpenAPI operation-tag shape; it picks up a description if one is defined for
    that name, else stands alone) or a ``Tag`` to define the name *with* a description
    inline (hoisted to the document's tag list). ``responses`` declares extra/override
    responses applied to every operation (a blanket ``401``, say); a per-operation
    ``OperationMeta`` overrides it.
    """

    tags: Sequence[str | Tag] = ()
    responses: Sequence[ResponseSpec] = ()


class ResourceMeta(Struct):
    """OpenAPI metadata shared by all of a ``Resource``'s operations.

    ``tags`` are the groups every operation belongs to — a bare ``str`` name or a ``Tag``
    that defines it with a description (see :class:`EndpointMeta`). ``responses`` declares
    extra/override responses applied to every operation (a blanket ``401``, say); a
    per-operation ``OperationMeta`` overrides it.
    """

    tags: Sequence[str | Tag] = ()
    responses: Sequence[ResponseSpec] = ()


class OperationMeta(Struct):
    """OpenAPI metadata for a single operation (``meta_get``, ``meta_create``, …).

    ``operation_id`` lives here, never on the class-level ``meta`` — operation ids must
    be unique, so they can't sensibly cascade to every operation. ``summary`` /
    ``description`` are the operation's prose (explicit — docstrings are never published).
    ``responses`` declares extra responses or overrides a derived one by reusing its status.

    ``tags`` (bare ``str`` names or describing ``Tag``\\ s) cascade from the class-level
    ``meta`` by the *container type*: a ``list`` extends the class tags
    (``meta_get=OperationMeta(tags=["unsafe"])`` -> class tags + ``unsafe``), a non-empty
    ``tuple`` replaces them (``tags=("admin",)`` -> just ``admin``); the default ``()``
    inherits.
    """

    tags: Sequence[str | Tag] = ()
    operation_id: str | None = None
    summary: str | None = None
    description: str | None = None
    responses: Sequence[ResponseSpec] = ()


@dataclass(frozen=True, slots=True)
class FormField:
    """One resolved multipart form field: its wire name, payload type, and reusable decoder."""

    name: str
    wire_name: str
    payload_type: object
    headers_type: type[Struct] | None
    payload_kind: PayloadKind
    decoder: Decoder[Struct] | None  # reusable typed decoder; set iff payload_kind == "struct"
    required: bool
    repeated: bool
    enveloped: bool
    file: bool


@dataclass(frozen=True, slots=True)
class FormSpec:
    """A handler's resolved multipart form: the form Struct and its compiled fields."""

    struct_type: type[Struct]
    fields: tuple[FormField, ...]


@dataclass(slots=True)
class Sources:
    """The resolved Struct types for one handler's arguments."""

    json: type[Struct] | None = None
    json_decoder: Decoder[Struct] | None = None  # prebuilt decoder for the json body type
    form: FormSpec | None = None
    params: type[Struct] | None = None
    path: type[Struct] | None = None
    headers: type[Struct] | None = None
    user: type[Struct] | None = None
    content: bool = False
    raw_headers: bool = False
    return_kind: ReturnKind = "json"
    return_annotation: object = None  # the raw return hint, kept for OpenAPI response derivation
    arity: int = 0  # number of binding args the handler declares


@dataclass(slots=True)
class OperationSpec:
    """One captured operation, all the inputs the OpenAPI document needs. Built at wiring
    time (in ``BaseApp._include``) and translated to an ``OperationInput`` at ``_finalize``."""

    path: str  # OpenAPI path template, e.g. "/widgets/{widgetId}"
    method: str  # lowercase HTTP verb
    success_status: int
    sources: Sources
    authed: bool
    security_scheme: SecurityScheme | None
    class_meta: ResourceMeta | EndpointMeta | None
    op_meta: OperationMeta | None
    operation_id_default: str
