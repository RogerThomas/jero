"""The demo app's msgspec ``Struct`` contracts: wire models and the background event.

Everything that crosses the wire (bodies, query/path params, typed headers, the auth
user) is a ``Struct``. ``Camel`` is the shared base giving camelCase on the wire and
snake_case in code.
"""

from typing import Annotated

from msgspec import Meta

from jero import FormPart, ModelMeta, Struct


class Camel(Struct, rename="camel"):
    """camelCase on the wire, snake_case in code.

    Uses jero's ``Struct`` (a drop-in for ``msgspec.Struct``) so any model can carry a
    ``meta=ModelMeta(...)`` description. (This docstring is for maintainers — it never
    reaches the OpenAPI spec.)
    """


class WidgetIn(Camel, meta=ModelMeta(description="The fields needed to create a widget.")):
    """Inbound widget payload (no id yet).

    The ``Meta`` constraints are validation *and* documentation: msgspec enforces them on
    the request body and the OpenAPI generator emits them as ``minLength`` / ``minimum`` /
    ``description`` on the schema. The model-level description comes from ``meta=`` above,
    not this docstring.
    """

    name: Annotated[str, Meta(min_length=1, description="Human-readable widget name")]
    price_cents: Annotated[int, Meta(ge=0, description="Price in cents, never negative")]


class Widget(WidgetIn, meta=ModelMeta(description="A stored widget, including its id.")):
    """A stored widget, including its assigned id."""

    id: str


class WidgetPatch(Camel):
    """Partial widget update; omitted fields are left unchanged."""

    name: str | None = None
    price_cents: int | None = None


class WidgetPath(Camel):
    """Path params carrying a widget id."""

    widget_id: str


class Page(Camel):
    """Pagination query params for listing widgets."""

    limit: int = 20
    offset: int = 0


class Deleted(Camel):
    """Response confirming a widget was removed."""

    id: str
    deleted: bool


class WidgetEvent(Camel):
    """An analytics event recorded off the request path when a widget changes."""

    action: str
    widget_id: str


class Credentials(Camel):
    """The bearer token lifted from the request's Authorization header."""

    authorization: str


class User(Camel):
    """The authenticated caller."""

    id: str
    name: str


class Health(Camel):
    """Health-check response body."""

    status: str


class Question(Camel):
    """Inbound question for the streaming answers endpoint."""

    text: str


class AnswerChunk(Camel):
    """One streamed fragment of an answer (one NDJSON line)."""

    text: str


class Notification(Camel):
    """A notification delivered over the Server-Sent Events feed."""

    message: str


class RawForm(Camel):
    """Multipart form whose part exposes raw part headers."""

    blob: FormPart[bytes]


class RawFormHeaders(Camel):
    """Response echoing request and form-part raw headers."""

    request_header_names: list[str]
    part_header_names: list[str]
    part_checksum_values: list[str]
    part_content_type: str | None
    part_typed_headers: bool
