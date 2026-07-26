"""Typed API errors: the HTTP contract, and two wire-body families on top of it.

``BaseHTTPError`` carries the contract (a class-level ``status``). The Problem family
(``HTTPError`` and friends) renders RFC 9457 Problem Details and is the blessed
default; ``StructHTTPError`` renders a body Struct you supply. ``ErrorBodyAdapter``
replaces the Problem family's rendering app-wide for house error formats.

jero's built-in errors use a short kebab-case code for ``type`` rather than the
RFC 9457 URI — a convention, not a rule: your own errors may use any non-empty
string (a code in your house style, or a full URI). Static error metadata lives on
the exception class; parameterized errors additionally carry a typed Struct whose
values render the human-only detail.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import fields as dataclass_fields
from string import Formatter
from types import get_original_bases
from typing import Annotated, Any, ClassVar, Literal, cast, get_args, get_origin

from msgspec import Struct, defstruct
from msgspec.structs import FieldInfo, asdict, fields


class Problem(Struct, kw_only=True, omit_defaults=True):
    """The wire representation of a static API error."""

    type: str
    title: str
    status: int
    docs: str | None = None


class ParameterizedProblem[P: Struct](Problem, kw_only=True, omit_defaults=True):
    """The wire representation of an API error with occurrence-specific context."""

    detail: str
    params: P


def _class_option(options: dict[str, object], name: str, owner: str) -> object:
    try:
        return options.pop(name)
    except KeyError as exc:
        raise TypeError(f"{owner} subclass is missing required class option {name!r}") from exc


class BaseHTTPError(Exception):
    """The abstract root of jero's API errors: the HTTP contract without a wire body.

    Every concrete error declares ``status`` as a class option — it drives the response
    status line and the OpenAPI docs. Subclass one of the two families, never this root
    directly: :class:`HTTPError` (RFC 9457 Problem Details, the blessed default) or
    :class:`StructHTTPError` (bring your own body Struct). ``except BaseHTTPError``
    means "any jero error"; ``except HTTPError`` catches only the Problem family.
    """

    status: ClassVar[int]

    def __init_subclass__(cls, *, status: object = None, _abstract: bool = False) -> None:
        super().__init_subclass__()
        if _abstract:
            return
        if BaseHTTPError in cls.__bases__:
            raise TypeError(
                f"{cls.__name__} subclasses BaseHTTPError directly; subclass HTTPError "
                "(Problem Details) or StructHTTPError (your own body Struct) instead",
            )
        if status is None:
            raise TypeError(f"{cls.__name__} is missing required class option 'status'")
        if not isinstance(status, int) or isinstance(status, bool) or not 400 <= status <= 599:
            raise TypeError(f"{cls.__name__} status must be an integer from 400 through 599")
        cls.status = status

    @property
    def response_body(self) -> Struct:
        """The Struct the framework encodes as this error's response body."""
        raise NotImplementedError


def _resolve_struct_arg(cls: type, origin_base: type) -> type[Struct] | None:
    """Find the concrete Struct type argument through a parameterized generic base."""
    for klass in cls.__mro__:
        for base in get_original_bases(klass):
            origin = get_origin(base)
            if not isinstance(origin, type) or not issubclass(origin, origin_base):
                continue
            args = get_args(base)
            if args and isinstance(args[0], type) and issubclass(args[0], Struct):
                return args[0]
    return None


def _resolve_params_type(cls: type) -> type[Struct] | None:
    """Find the concrete params Struct through either parameterized error base."""
    return _resolve_struct_arg(cls, ParameterizedHTTPError)


def _validated_status_field(
    status_field: object, body_type: type[Struct], owner: str
) -> str | None:
    """Validate a ``status_field`` class option against the body Struct it will extend."""
    if status_field is None:
        return None
    if not isinstance(status_field, str) or not status_field.strip():
        raise TypeError(f"{owner} status_field must be a non-blank string or None")
    if status_field in {field.name for field in fields(body_type)}:
        raise TypeError(
            f"{owner} status_field {status_field!r} collides with a field on "
            f"{body_type.__name__}; the framework adds that field when composing the wire body"
        )
    return status_field


def _unwrap_annotated(ann: object) -> object:
    """The bare type under ``Annotated[...]`` — msgspec ``Meta`` rides along on wire
    fields but must not defeat the engine's type checks."""
    return get_args(ann)[0] if get_origin(ann) is Annotated else ann


def _literal_of(value: object) -> type:
    """``Literal[value]`` built at runtime — subscripting ``Literal`` directly is a type
    form to checkers, so go through a cast and an ordinary mapping subscript."""
    return cast(Mapping[object, type], Literal)[value]


def _status_wire_model(
    name: str, body_type: type[Struct], status_field: str, status: int
) -> type[Struct]:
    """Compose a wire model: the body Struct's fields plus the status field, typed to the
    exact status (a ``Literal``, so the OpenAPI schema documents it as a const). Subclassing
    the body inherits its config — ``rename`` included — so the field renders in house
    style. The default keeps the model constructible whatever defaults the body declares;
    the framework always passes the status explicitly."""
    return defstruct(
        name,
        [(status_field, _literal_of(status), status)],
        bases=(body_type,),
        module=body_type.__module__,
    )


def _template_placeholders(template: str) -> frozenset[str]:
    """The ``{placeholder}`` names a format template references."""
    return frozenset(
        name.split(".", 1)[0].split("[", 1)[0]
        for _, name, _, _ in Formatter().parse(template)
        if name is not None
    )


def _parse_template_option(owner: str, option: object, kind: str) -> dict[str, str]:
    """Validate a templates-shaped class option: field-name strings -> format strings."""
    parsed: dict[str, str] = {}
    if option is None:
        return parsed
    if not isinstance(option, dict):
        raise TypeError(f"{owner} {kind} must be a dict of field -> template")
    for key, value in cast("dict[object, object]", option).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError(f"{owner} {kind} must map field-name strings to format strings")
        parsed[key] = value
    return parsed


class HTTPError(BaseHTTPError, _abstract=True):
    """A static typed API error rendered as RFC 9457 Problem Details (the blessed default).

    Subclasses declare their stable contract as class options::

        class AuthenticationRequiredError(
            HTTPError,
            type="authentication-required",
            title="Authentication required",
            status=401,
        ): ...
    """

    type: ClassVar[str]
    title: ClassVar[str]
    docs: ClassVar[str | None]

    def __init_subclass__(cls, **options: object) -> None:
        abstract = options.pop("_abstract", False)
        if abstract is True:
            super().__init_subclass__(_abstract=True)
            return

        error_type = _class_option(options, "type", "HTTPError")
        title = _class_option(options, "title", "HTTPError")
        status = _class_option(options, "status", "HTTPError")
        docs = options.pop("docs", None)
        if options:
            names = ", ".join(sorted(options))
            raise TypeError(f"unexpected HTTPError class option(s): {names}")
        if not isinstance(error_type, str) or not error_type.strip():
            raise TypeError("HTTPError type must be a non-blank string")
        if not isinstance(title, str) or not title:
            raise TypeError("HTTPError title must be a non-empty string")
        if docs is not None and not isinstance(docs, str):
            raise TypeError("HTTPError docs must be a string or None")

        super().__init_subclass__(status=status)
        cls.type = error_type
        cls.title = title
        cls.docs = docs

    def __init__(self) -> None:
        if not hasattr(type(self), "title"):
            raise TypeError("HTTPError must be subclassed with type, title, and status")
        super().__init__(self.title)

    @property
    def problem(self) -> Problem:
        """Build the typed wire body for this error occurrence."""
        return Problem(type=self.type, title=self.title, status=self.status, docs=self.docs)

    @property
    def response_body(self) -> Problem:
        """The Struct the framework encodes: this family's Problem body."""
        return self.problem


class ParameterizedHTTPError[P: Struct](HTTPError, _abstract=True):
    """An API error whose detail is rendered from a typed params Struct."""

    detail_template: ClassVar[str]
    params_type: ClassVar[type[Struct]]

    params: P
    detail: str

    def __init_subclass__(
        cls,
        *,
        detail_template: str | None = None,
        **options: object,
    ) -> None:
        abstract = options.get("_abstract") is True
        super().__init_subclass__(**options)
        if abstract:
            return
        if detail_template is None:
            raise TypeError("ParameterizedHTTPError subclass requires detail_template")
        params_type = _resolve_params_type(cls)
        if params_type is None:
            raise TypeError("ParameterizedHTTPError subclass requires a concrete params Struct")

        param_names = {field.name for field in fields(params_type)}
        template_names = _template_placeholders(detail_template)
        if not template_names:
            raise TypeError("detail_template must reference at least one params field")
        unknown = template_names - param_names
        if unknown:
            names = ", ".join(sorted(unknown))
            raise TypeError(f"detail_template references unknown params field(s): {names}")

        cls.detail_template = detail_template
        cls.params_type = params_type

    def __init__(self, params: P) -> None:
        super().__init__()
        self._set_params(params)

    def _set_params(self, params: P) -> None:
        if not isinstance(params, self.params_type):
            raise TypeError(
                f"{type(self).__name__} params must be {self.params_type.__name__}, "
                f"got {type(params).__name__}",
            )
        self.params = params
        self.detail = self.detail_template.format(**asdict(params))
        Exception.__init__(self, self.detail)

    @property
    def problem(self) -> ParameterizedProblem[P]:
        """Build the typed wire body for this error occurrence."""
        return ParameterizedProblem(
            type=self.type,
            title=self.title,
            status=self.status,
            docs=self.docs,
            detail=self.detail,
            params=self.params,
        )


class DataclassHTTPError[P: Struct](ParameterizedHTTPError[P], ABC, _abstract=True):
    """The ergonomic parameterized-error base used by dataclass error subclasses."""

    @abstractmethod
    def __post_init__(self) -> None:
        """Build the params Struct by calling ``self._set_params(...)``."""


# Names the engine itself uses as class/instance attributes; a raise-time param with
# one of these names (a dataclass field especially) would shadow the class contract —
# e.g. a `status` param overriding the validated status line.
_RESERVED_PARAM_NAMES = frozenset(
    {
        "args",
        "body",
        "body_type",
        "consts",
        "description",
        "param_body_fields",
        "param_names",
        "params",
        "params_field",
        "params_struct",
        "response_body",
        "status",
        "status_field",
        "templates",
        "wire_model",
    }
)


class _EngineSpec:
    """The parsed StructHTTPError class options (internal plumbing, not exported)."""

    __slots__ = (
        "consts",
        "description",
        "params_field",
        "status_field",
        "templates",
    )

    def __init__(
        self,
        *,
        description: str,
        consts: dict[str, object],
        templates: dict[str, str],
        status_field: str | None,
        params_field: str | None,
    ) -> None:
        self.description = description
        self.consts = consts
        self.templates = templates
        self.status_field = status_field
        self.params_field = params_field


def _parse_consts_option(option: object) -> dict[str, object]:
    """Validate a ``consts`` class option: field-name strings -> pinned values."""
    consts: dict[str, object] = {}
    if option is None:
        return consts
    if not isinstance(option, dict):
        raise TypeError("StructHTTPError consts must be a dict of field -> value")
    for key, value in cast("dict[object, object]", option).items():
        if not isinstance(key, str):
            raise TypeError("StructHTTPError consts keys must be field-name strings")
        consts[key] = value
    return consts


def _parse_engine_options(options: dict[str, object]) -> tuple[object, _EngineSpec]:
    """Pop and validate every StructHTTPError class option; the raw status passes
    through to ``BaseHTTPError`` (which owns its validation)."""
    status = _class_option(options, "status", "StructHTTPError")
    description = _class_option(options, "description", "StructHTTPError")
    consts = _parse_consts_option(options.pop("consts", None))
    templates = _parse_template_option(
        "StructHTTPError", options.pop("templates", None), "templates"
    )
    status_field = options.pop("status_field", None)
    params_field = options.pop("params_field", None)
    if options:
        names = ", ".join(sorted(options))
        raise TypeError(f"unexpected StructHTTPError class option(s): {names}")
    if not isinstance(description, str) or not description.strip():
        raise TypeError("StructHTTPError description must be a non-blank string")
    if status_field is not None and not isinstance(status_field, str):
        raise TypeError("StructHTTPError status_field must be a field-name string or None")
    if params_field is not None and not isinstance(params_field, str):
        raise TypeError("StructHTTPError params_field must be a field-name string or None")
    return status, _EngineSpec(
        description=description,
        consts=consts,
        templates=templates,
        status_field=status_field,
        params_field=params_field,
    )


def _field_sources(
    cls_name: str, body_name: str, body_fields: dict[str, FieldInfo], spec: _EngineSpec
) -> dict[str, str]:
    """Map each declared body field to its single source, failing loud on an unknown
    field or a field fed twice."""
    sources: dict[str, str] = {}
    declared: list[tuple[str, tuple[str, ...]]] = [
        ("consts", tuple(spec.consts)),
        ("templates", tuple(spec.templates)),
    ]
    if spec.status_field is not None:
        declared.append(("status_field", (spec.status_field,)))
    if spec.params_field is not None:
        declared.append(("params_field", (spec.params_field,)))
    for kind, names in declared:
        for name in names:
            if name not in body_fields:
                raise TypeError(
                    f"{cls_name} {kind} names {name!r}, which is not a field of {body_name}",
                )
            if name in sources:
                raise TypeError(
                    f"{cls_name} field {name!r} is fed by both {sources[name]} and {kind}; "
                    "one source per field",
                )
            sources[name] = kind
    return sources


def _validate_text_sources(
    cls_name: str, body_fields: dict[str, FieldInfo], spec: _EngineSpec
) -> None:
    """Template-fed fields must be strings with named placeholders; the status field
    an int. ``Annotated[...]`` wrappers (msgspec ``Meta``) are looked through."""
    for name, template in spec.templates.items():
        if _unwrap_annotated(body_fields[name].type) is not str:
            raise TypeError(f"{cls_name} templates carry text; field {name!r} is not a str")
        placeholders = _template_placeholders(template)
        if not placeholders:
            raise TypeError(
                f"{cls_name} template for {name!r} references no placeholders — "
                "use consts for a fixed value",
            )
        for placeholder in placeholders:
            if not placeholder.isidentifier():
                raise TypeError(
                    f"{cls_name} template for {name!r} uses placeholder "
                    f"{{{placeholder}}}; placeholders must be named params",
                )
    status_field = spec.status_field
    if status_field is not None and _unwrap_annotated(body_fields[status_field].type) is not int:
        raise TypeError(f"{cls_name} status_field {status_field!r} must be an int field")


def _validate_const_values(
    cls_name: str, body_fields: dict[str, FieldInfo], consts: dict[str, object]
) -> None:
    """Const values must be Literal-able scalars matching the field's declared type —
    a mismatch would otherwise surface as an ill-typed wire body, or an unattributable
    msgspec error when the OpenAPI document is built."""
    for name, value in consts.items():
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise TypeError(
                f"{cls_name} consts[{name!r}] must be a str or int (schema consts are "
                f"Literal-typed), got {type(value).__name__}",
            )
        field_type = _unwrap_annotated(body_fields[name].type)
        if get_origin(field_type) is Literal:
            if value not in get_args(field_type):
                raise TypeError(
                    f"{cls_name} consts[{name!r}] value {value!r} is not among the "
                    "field's literal values",
                )
        elif not (isinstance(field_type, type) and isinstance(value, field_type)):
            raise TypeError(
                f"{cls_name} consts[{name!r}] value {value!r} does not match the "
                "field's declared type",
            )


def _params_nesting(
    cls_name: str, body_fields: dict[str, FieldInfo], spec: _EngineSpec
) -> tuple[type[Struct] | None, tuple[str, ...]]:
    """Resolve the params_field's Struct type and its field names (the nested params)."""
    if spec.params_field is None:
        return None, ()
    nested_type = _unwrap_annotated(body_fields[spec.params_field].type)
    if not (isinstance(nested_type, type) and issubclass(nested_type, Struct)):
        raise TypeError(
            f"{cls_name} params_field {spec.params_field!r} must be a Struct-typed field",
        )
    return nested_type, tuple(field.name for field in fields(nested_type))


def _validate_coverage(
    cls_name: str,
    spec: _EngineSpec,
    sources: dict[str, str],
    nested_params: tuple[str, ...],
    param_body_fields: tuple[str, ...],
) -> frozenset[str]:
    """Enforce one flat param namespace (no reserved names); returns the full
    raise-time param namespace."""
    ambiguous = set(nested_params) & set(param_body_fields)
    if ambiguous:
        names = ", ".join(sorted(ambiguous))
        raise TypeError(
            f"{cls_name} params_field fields {names} collide with same-named body fields; "
            "one flat namespace, one owner per name",
        )
    placeholder_names: frozenset[str] = frozenset()
    for template in spec.templates.values():
        placeholder_names |= _template_placeholders(template)
    overlap = placeholder_names & set(sources)
    if overlap:
        names = ", ".join(sorted(overlap))
        raise TypeError(
            f"{cls_name} template placeholder(s) {names} collide with declared body-field "
            "sources; placeholders are raise-time params",
        )
    param_names = frozenset(param_body_fields) | frozenset(nested_params) | placeholder_names
    reserved = param_names & _RESERVED_PARAM_NAMES
    if reserved:
        names = ", ".join(sorted(reserved))
        raise TypeError(
            f"{cls_name} param name(s) {names} are reserved by the error engine; "
            "rename the body field, nested field, or placeholder",
        )
    return param_names


def _engine_wire_model(
    error_cls: type,
    body_type: type[Struct],
    body_fields: dict[str, FieldInfo],
    spec: _EngineSpec,
    status: int,
) -> type[Struct]:
    """Compose the wire model: ``B``'s shape, wire names, and tag config, const-fed
    fields narrowed to ``Literal`` types with the pinned value as default — schema enum
    consts, and raise-time construction only supplies the variable fields. The model's
    module is the error class's qualname (unique per error class), keeping it distinct
    from a same-named user Struct in msgspec's schema pass."""
    wire_name = error_cls.__name__.removesuffix("Error") or error_cls.__name__
    owner_module = f"{error_cls.__module__}.{error_cls.__qualname__}"
    pinned: dict[str, object] = dict(spec.consts)
    model_fields: list[object] = []
    for name, field in body_fields.items():
        if name in pinned:
            model_fields.append((name, _literal_of(pinned[name]), pinned[name]))
        elif name == spec.status_field:
            model_fields.append((name, _literal_of(status), status))
        else:
            model_fields.append((name, field.type))
    config = body_type.__struct_config__
    return defstruct(
        wire_name,
        cast("list[tuple[str, type]]", model_fields),
        kw_only=True,  # lifts default-ordering rules, so B's field order is kept
        rename={field.name: field.encode_name for field in fields(body_type)},
        module=owner_module,
        tag=config.tag,
        tag_field=config.tag_field,
    )


class StructHTTPError[B: Struct](BaseHTTPError, _abstract=True):
    """An API error generic over your own wire Struct ``B`` — the bring-your-own-body
    family. Class options declare how *every* field of ``B`` gets its value:

    - ``consts={"field": value}`` — a pinned constant: on the wire exactly as given,
      and an enum const in the OpenAPI schema.
    - ``templates={"field": "..."}`` — rendered at raise time from the raise-time params
      (``{{brace}}`` escaping ships literal braces, per ``str.format``).
    - ``status_field="field"`` — an existing int field fed the class's ``status``
      (an enum const in the schema).
    - ``params_field="field"`` — a Struct-typed field the raise-time params nest into.
    - anything left over — fed by a same-named raise-time param.

    Total coverage is enforced loud at class creation: every field of ``B`` has exactly
    one source. Raise-time params are one flat namespace (template placeholders, nested
    params-Struct fields, and leftover body fields); pass them as keyword arguments, or —
    the blessed, statically-typed form — decorate the subclass with ``@dataclass`` and
    declare them as fields, so the generated ``__init__`` carries real names and types::

        class QuotaBody(Struct, rename="camel"):
            error_code: str
            error_message: str
            status_code: int

        @dataclass
        class QuotaExceededError(
            StructHTTPError[QuotaBody],
            status=429,
            description="Quota exceeded",
            consts={"error_code": "quota-exceeded"},
            templates={"error_message": "Limit is {limit} requests per {window}"},
            status_field="status_code",
        ):
            limit: int
            window: str

        raise QuotaExceededError(limit=100, window="minute")
        # 429 {"errorCode": "quota-exceeded",
        #      "errorMessage": "Limit is 100 requests per minute", "statusCode": 429}

    ``description`` is the OpenAPI response description (explicit — docstrings are never
    published). The wire model — ``B``'s shape with const-fed fields narrowed to
    ``Literal`` types — is composed once here at class creation; the request path is
    construct-and-encode, and nothing you pass is ever mutated.
    """

    description: ClassVar[str]
    body_type: ClassVar[type[Struct]]
    wire_model: ClassVar[type[Struct]]
    consts: ClassVar[dict[str, object]]
    templates: ClassVar[dict[str, str]]
    status_field: ClassVar[str | None]
    params_field: ClassVar[str | None]
    params_struct: ClassVar[type[Struct] | None]
    param_body_fields: ClassVar[tuple[str, ...]]  # body fields fed by a same-named param
    param_names: ClassVar[frozenset[str]]  # all raise-time params (incl. template-only)
    _nested_params: ClassVar[tuple[str, ...]]  # the params_field Struct's field names

    params: dict[str, object]  # the bound raise-time params (set by _bind)

    def __init_subclass__(cls, **options: object) -> None:
        abstract = options.pop("_abstract", False)
        if abstract is True:
            super().__init_subclass__(_abstract=True)
            return

        status, spec = _parse_engine_options(options)
        super().__init_subclass__(status=status)

        body_type = _resolve_struct_arg(cls, StructHTTPError)
        if body_type is None:
            raise TypeError("StructHTTPError subclass requires a concrete body Struct")
        body_fields = {field.name: field for field in fields(body_type)}

        sources = _field_sources(cls.__name__, body_type.__name__, body_fields, spec)
        _validate_text_sources(cls.__name__, body_fields, spec)
        _validate_const_values(cls.__name__, body_fields, spec.consts)
        params_struct, nested_params = _params_nesting(cls.__name__, body_fields, spec)
        param_body_fields = tuple(name for name in body_fields if name not in sources)
        param_names = _validate_coverage(
            cls.__name__, spec, sources, nested_params, param_body_fields
        )
        cls.wire_model = _engine_wire_model(cls, body_type, body_fields, spec, cls.status)

        cls.description = spec.description
        cls.body_type = body_type
        cls.consts = spec.consts
        cls.templates = spec.templates
        cls.status_field = spec.status_field
        cls.params_field = spec.params_field
        cls.params_struct = params_struct
        cls.param_body_fields = param_body_fields
        cls.param_names = param_names
        cls._nested_params = nested_params

    def __init__(self, **params: object) -> None:
        if not hasattr(type(self), "body_type"):
            raise TypeError(
                "StructHTTPError must be subclassed with a body, status, and description"
            )
        expected = type(self).param_names
        missing = expected - set(params)
        unexpected = set(params) - expected
        if missing or unexpected:
            parts: list[str] = []
            if missing:
                parts.append(f"missing: {', '.join(sorted(missing))}")
            if unexpected:
                parts.append(f"unexpected: {', '.join(sorted(unexpected))}")
            raise TypeError(f"{type(self).__name__}() params — {'; '.join(parts)}")
        self._bind(params)

    def __post_init__(self) -> None:
        """The statically-typed tier: called by an ``@dataclass`` subclass's generated
        ``__init__``. The declared dataclass fields ARE the params — validated against
        what the class options require, so a missing or extra field fails on first
        raise."""
        declared = {field.name for field in dataclass_fields(cast(Any, self))}
        expected = set(type(self).param_names)
        if declared != expected:
            missing = ", ".join(sorted(expected - declared)) or "-"
            extra = ", ".join(sorted(declared - expected)) or "-"
            raise TypeError(
                f"{type(self).__name__} dataclass fields must match its params "
                f"(missing: {missing}; extra: {extra})",
            )
        self._bind({name: getattr(self, name) for name in declared})

    def _bind(self, params: dict[str, object]) -> None:
        # object.__setattr__, so frozen @dataclass subclasses can bind too.
        object.__setattr__(self, "params", params)
        Exception.__init__(self, self.description)

    def _variable_values(self) -> dict[str, object]:
        """The variable fields' values: same-named params, rendered templates, and the
        nested params Struct when ``params_field`` is declared."""
        values: dict[str, object] = {name: self.params[name] for name in self.param_body_fields}
        for name, template in self.templates.items():
            values[name] = template.format(**self.params)
        if self.params_field is not None and self.params_struct is not None:
            nested = {name: self.params[name] for name in self._nested_params}
            values[self.params_field] = self.params_struct(**nested)
        return values

    @property
    def body(self) -> B:
        """This occurrence as *your* body type — every field populated (consts, status,
        templates, params), typed as ``B`` for code that inspects the error."""
        values = self._variable_values() | self.consts
        if self.status_field is not None:
            values[self.status_field] = self.status
        return cast("B", self.body_type(**values))

    @property
    def response_body(self) -> Struct:
        """Compose the wire body fresh: params, rendered templates, and the nested params
        Struct; consts and the status arrive through the wire model's ``Literal``
        defaults. Nothing is ever mutated."""
        return self.wire_model(**self._variable_values())


class ErrorBodyAdapter[B: Struct](ABC):
    """App-wide renderer for the Problem family: compose your own wire body from any
    ``HTTPError`` — the framework built-ins included — registered via
    ``BaseApp.include_error_adapter``. ``StructHTTPError``\\ s render themselves, so every
    error has exactly one renderer.

    ``status_field`` optionally names a field the framework *adds* (typed to the exact
    status) when composing the wire body; it must not exist on the body Struct, and the
    Struct ``compose`` returns is never mutated. Keep ``compose`` pure — it receives only
    the error; request-correlated data belongs in exception handlers, not here.
    """

    status_field: ClassVar[str | None] = None
    body_type: ClassVar[type[Struct]]
    _wire_models: ClassVar[dict[int, type[Struct]]]

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        body_type = _resolve_struct_arg(cls, ErrorBodyAdapter)
        if body_type is None:
            # A generic intermediate (B still unbound) — concrete subclasses bind and
            # validate; registering an unbound adapter fails at include_error_adapter.
            return
        cls.body_type = body_type
        cls.status_field = _validated_status_field(cls.status_field, body_type, "ErrorBodyAdapter")
        cls._wire_models = {}

    @abstractmethod
    def compose(self, error: HTTPError) -> B:
        """Build your body from a Problem-family error (its ``type``/``title``/``status``,
        and ``str(error)`` for the human message — the rendered detail when parameterized)."""

    def _wire_model_for(self, status: int) -> type[Struct]:
        """The model encoded/documented for ``status``: the body Struct itself, or — when
        ``status_field`` is declared — the composed per-status model, built once and cached."""
        if self.status_field is None:
            return self.body_type
        model = self._wire_models.get(status)
        if model is None:
            name = f"{self.body_type.__name__}{status}"
            model = _status_wire_model(name, self.body_type, self.status_field, status)
            self._wire_models[status] = model
        return model

    def compose_wire(self, error: HTTPError) -> Struct:
        """``compose`` plus the declared status splice, mutating nothing. Un-underscored:
        core calls it across the module boundary when rendering a Problem-family error."""
        body = self.compose(error)
        if not isinstance(body, self.body_type):
            raise TypeError(
                f"{type(self).__name__}.compose must return {self.body_type.__name__}, "
                f"got {type(body).__name__}",
            )
        if self.status_field is None:
            return body
        # Compose by field name: works for kw_only bodies, and a compose() returning a
        # body *subclass* contributes only the declared fields.
        values: dict[str, object] = {
            field.name: getattr(body, field.name) for field in fields(self.body_type)
        }
        values[self.status_field] = error.status
        return self._wire_model_for(error.status)(**values)

    def docs_model(self, status: int) -> type[Struct]:
        """The wire model documented for errors of ``status`` — what the OpenAPI build
        references for derived error responses once this adapter is registered."""
        return self._wire_model_for(status)


class NotFoundError(
    HTTPError,
    type="not-found",
    title="Not found",
    status=404,
):
    """No route or resource matches the requested path."""


class MethodNotAllowedError(
    HTTPError,
    type="method-not-allowed",
    title="Method not allowed",
    status=405,
):
    """The path exists but does not support the requested method."""


class MalformedRequestError(
    HTTPError,
    type="malformed-request",
    title="Malformed request",
    status=400,
):
    """The request cannot be parsed or bound."""


class ValidationFailedError(
    HTTPError,
    type="validation-failed",
    title="Validation failed",
    status=422,
):
    """The request is syntactically valid but does not match its typed contract."""


class AuthenticationRequiredError(
    HTTPError,
    type="authentication-required",
    title="Authentication required",
    status=401,
):
    """Authentication credentials are absent or invalid."""


class UnsupportedMediaTypeError(
    HTTPError,
    type="unsupported-media-type",
    title="Unsupported media type",
    status=415,
):
    """The request body does not use the media type required by the operation."""


class ForbiddenError(
    HTTPError,
    type="forbidden",
    title="The caller is authenticated but not allowed to perform this operation",
    status=403,
):
    """The caller is authenticated but not allowed to perform this operation."""


class ConflictError(
    HTTPError,
    type="conflict",
    title="The request conflicts with the current state of the resource",
    status=409,
):
    """The request conflicts with the current state of the resource."""


class GoneError(
    HTTPError,
    type="gone",
    title="The resource existed but has been permanently removed",
    status=410,
):
    """The resource existed but has been permanently removed."""


class TooManyRequestsError(
    HTTPError,
    type="too-many-requests",
    title="The caller has exceeded a rate limit",
    status=429,
):
    """The caller has exceeded a rate limit."""


class InternalServerError(
    HTTPError,
    type="internal-server-error",
    title="Internal server error",
    status=500,
):
    """An unexpected server-side failure whose internals are not exposed."""
