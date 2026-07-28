"""The resolved wiring contracts, shared by the request path and the OpenAPI generator.

These are the data types produced once at wiring time — the per-handler source model
(``Sources``), the compiled form spec, the class/operation metadata, and the captured
``OperationSpec`` handed to the OpenAPI builder. They live in their own leaf module
(depending only on msgspec and :mod:`jero.openapi`) so that both :mod:`jero.core` (which
builds them on the request path) and :mod:`jero._openapi_wiring` (which reads them to
build the spec) can import them without importing each other. Keeping the contracts
below both modules is what makes the dependency graph acyclic.
"""

from collections.abc import Mapping, Sequence
from types import UnionType
from typing import Literal, TypeAliasType, TypeVar, Union, cast, get_args, get_origin

from msgspec import Struct
from msgspec.json import Decoder

from jero.errors import BaseHTTPError
from jero.openapi import ResponseSpec, SecurityScheme, Tag

# How a handler's return value is encoded onto the wire; see _return_kind / _result_sender.
# "no-content" / "created" / "accepted" are JSONResponse-shaped (NoContent excepted) but fix
# their status regardless of the verb's own default; "union" is a union of the other kinds,
# resolved member-by-member into Sources.return_members.
type ReturnKind = Literal[
    "json",
    "json-response",
    "no-content",
    "created",
    "accepted",
    "bytes",
    "bytes-response",
    "stream-bytes",
    "stream-ndjson",
    "stream-sse",
    "union",
]
# How a multipart form field's body is decoded; see _payload_kind / _decode_form_payload.
type PayloadKind = Literal["bytes", "struct", "scalar"]
# How auth gates one operation: mandatory (the authenticator returns ``TUser``),
# credentials-if-offered (it returns ``TUser | None``), or None for a route with no auth at
# all. One value rather than two bools, so "optional but unauthed" is unrepresentable.
type AuthMode = Literal["required", "optional"] | None


class WiringError(TypeError):
    """A router does not meet the framework contract. Raised at startup.

    Lives here, on the shared wiring leaf, so both :mod:`jero.core` and the sender-free
    :mod:`jero._exception_handlers` (which validates handlers at wiring time) can raise it
    without importing each other.
    """


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


def substitute(ann: object, bindings: Mapping[TypeVar, object]) -> object:
    """``ann`` with every bound type parameter replaced by what it is bound to, recursively:
    ``JSONResponse[T, TraceHeaders]`` under ``{T: Item}`` becomes
    ``JSONResponse[Item, TraceHeaders]``. Unbound parameters are left as they are.

    Rebuilding a type expression means subscripting its origin — a type form to checkers, so it
    goes through a cast and an ordinary mapping subscript. A union is composed with ``|`` rather
    than subscripted, so that arm casts too."""
    if isinstance(ann, TypeVar):
        return bindings.get(ann, ann)
    origin, args = get_origin(ann), get_args(ann)
    if origin is None or not args:
        return ann  # not a parameterized generic, so nothing to substitute into
    replaced = tuple(substitute(arg, bindings) for arg in args)
    if replaced == args:
        return ann
    if origin in (Union, UnionType):
        merged: type | UnionType = cast("type", replaced[0])
        for arg in replaced[1:]:
            merged = merged | cast("type", arg)
        return merged
    return cast(Mapping[tuple[object, ...], object], origin)[replaced]


def param_bindings(params: Sequence[object], supplied: tuple[object, ...]) -> dict[TypeVar, object]:
    """Each type parameter mapped to what an annotation binds it to: the argument supplied at
    that position, else the parameter's own PEP 696 default. A parameter with neither is left
    out, so :func:`substitute` leaves its TypeVar in place and callers can see that the
    annotation stated nothing there.

    Shared by the class walk and the alias walk so both fill defaults by one rule: a parameter's
    default is part of what the annotation *states*, and reading only the supplied positions
    would drop it — silently, since a dropped body or header type documents as absent rather
    than failing.

    Only ``TypeVar`` parameters bind: a ``ParamSpec``/``TypeVarTuple`` names something no
    response annotation can be, so it is left in place to fail the return-kind check like any
    other unusable type."""
    bindings: dict[TypeVar, object] = {}
    for index, param in enumerate(params):
        if not isinstance(param, TypeVar):
            continue
        if index < len(supplied):
            bindings[param] = supplied[index]
        elif param.has_default():
            bindings[param] = param.__default__
    return bindings


def unwrap_alias(ann: object) -> object:
    """A PEP 695 ``type`` alias replaced by whatever it aliases, so every downstream check sees
    the real annotation. ``type WidgetResponse = JSONResponse[Widget]`` is the spelling 3.13+
    recommends for naming a response type, and it is the alternative jero points users at, so it
    has to resolve rather than fail the return-kind check as an unrecognized object.

    Recursive, so an alias of an alias resolves. A *generic* alias arrives as a subscripted
    ``TypeAliasType`` — ``type Api[T] = JSONResponse[T, TraceHeaders]`` used as ``Api[Widget]`` —
    so its arguments substitute into the aliased expression on the way through. Both forms go
    through :func:`param_bindings`, so a parameter left off still contributes its default: a
    partially applied ``type Api[T, H = TraceHeaders]`` states ``H`` just as surely as if it had
    been written out."""
    if isinstance(ann, TypeAliasType):
        bindings = param_bindings(ann.__type_params__, ())
        return unwrap_alias(substitute(ann.__value__, bindings))
    origin = get_origin(ann)
    if isinstance(origin, TypeAliasType):
        bindings = param_bindings(origin.__type_params__, get_args(ann))
        return unwrap_alias(substitute(origin.__value__, bindings))
    return ann


class EndpointMeta(Struct):
    """OpenAPI metadata shared by all of an ``Endpoint``'s operations.

    ``tags`` are the groups this endpoint belongs to. An entry is a bare ``str`` (the tag
    name — the OpenAPI operation-tag shape; it picks up a description if one is defined for
    that name, else stands alone) or a ``Tag`` to define the name *with* a description
    inline (hoisted to the document's tag list). ``responses`` declares extra/override
    responses applied to every operation (a blanket ``401``, say); a per-operation
    ``OperationMeta`` overrides it. ``exceptions`` declares jero error classes every
    operation can raise — their status, body schema, and description derive from the
    class; a per-operation ``OperationMeta`` *extends* it (both remain raiseable).
    """

    tags: Sequence[str | Tag] = ()
    responses: Sequence[ResponseSpec] = ()
    exceptions: Sequence[type[BaseHTTPError]] = ()


class ResourceMeta(Struct):
    """OpenAPI metadata shared by all of a ``Resource``'s operations.

    ``tags`` are the groups every operation belongs to — a bare ``str`` name or a ``Tag``
    that defines it with a description (see :class:`EndpointMeta`). ``responses`` declares
    extra/override responses applied to every operation (a blanket ``401``, say); a
    per-operation ``OperationMeta`` overrides it. ``exceptions`` declares jero error
    classes every operation can raise — derived entirely from the class; a
    per-operation ``OperationMeta`` *extends* it (both remain raiseable).
    """

    tags: Sequence[str | Tag] = ()
    responses: Sequence[ResponseSpec] = ()
    exceptions: Sequence[type[BaseHTTPError]] = ()


class OperationMeta(Struct):
    """OpenAPI metadata for a single operation (``meta_get``, ``meta_create``, …).

    ``operation_id`` lives here, never on the class-level ``meta`` — operation ids must
    be unique, so they can't sensibly cascade to every operation. ``summary`` /
    ``description`` are the operation's prose (explicit — docstrings are never published).
    ``responses`` declares extra responses or overrides a derived one by reusing its status.
    ``exceptions`` declares jero error classes this operation can raise — status, body
    schema, and description all derive from the class; entries *extend* the class-level
    ``meta``'s (both remain raiseable), several sharing a status document as a ``oneOf``,
    and an explicit ``responses`` entry for the same status wins.

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
    exceptions: Sequence[type[BaseHTTPError]] = ()


class FormField(Struct, frozen=True):
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


class FormSpec(Struct, frozen=True):
    """A handler's resolved multipart form: the form Struct and its compiled fields."""

    struct_type: type[Struct]
    fields: tuple[FormField, ...]


class ResponseMember(Struct, frozen=True):
    """One resolved member of a union return annotation (``JSONResponse[Widget] |
    NoContent``): its concrete response class (for the runtime ``isinstance`` dispatch),
    the raw — possibly subscripted — annotation (for OpenAPI item/header derivation), its
    resolved kind (never ``"union"``), its effective status (the verb's default for a
    plain ``JSONResponse``/``BytesResponse`` member, else the member type's fixed status),
    and the generic wrapper class its type arguments resolve against (``None`` for a plain
    ``Struct``/``bytes`` member, which has no wrapper)."""

    response_type: type
    annotation: object
    kind: ReturnKind
    status: int
    wrapper: type | None = None


class Sources(Struct):
    """The resolved Struct types for one handler's arguments."""

    json: type[Struct] | None = None
    json_decoder: Decoder[Struct] | None = None  # prebuilt decoder for the json body type
    form: FormSpec | None = None
    params: type[Struct] | None = None
    path: type[Struct] | None = None
    headers: type[Struct] | None = None
    user: type[Struct] | None = None
    # True when 'user' was declared ``UserStruct | None`` (the anonymous-caller contract). Set
    # together with ``user``; cross-checked at wiring time against whether the route's
    # authenticator reports absence.
    user_optional: bool = False
    content: bool = False
    raw_headers: bool = False
    return_kind: ReturnKind = "json"
    return_annotation: object = None  # the raw return hint, kept for OpenAPI response derivation
    # The generic response-wrapper class ``return_annotation``'s type arguments resolve against,
    # so the OpenAPI layer can read (T, H) positionally without importing the wrapper classes
    # (``core`` imports that module, never the reverse). None for a plain Struct/bytes return.
    return_wrapper: type | None = None
    return_members: tuple[ResponseMember, ...] = ()  # populated iff return_kind == "union"
    arity: int = 0  # number of binding args the handler declares


class OperationSpec(Struct):
    """One captured operation, all the inputs the OpenAPI document needs. Built at wiring
    time (in ``BaseApp._include``) and translated to an ``OperationInput`` at ``_finalize``."""

    path: str  # OpenAPI path template, e.g. "/widgets/{widgetId}"
    method: str  # lowercase HTTP verb
    success_status: int
    sources: Sources
    auth_mode: AuthMode
    security_scheme: SecurityScheme | None
    class_meta: ResourceMeta | EndpointMeta | None
    op_meta: OperationMeta | None
    operation_id_default: str
