"""Contract violations surface as startup failures (WiringError).

The TestClient drives the lifespan on construction, so a bad ``_wire``
raises ``RuntimeError`` wrapping the framework's ``WiringError`` message.
"""

from dataclasses import dataclass

import pytest
from msgspec import Struct

from jero import Auth, BaseApp, Endpoint, Resource, TestClient


class P(Struct):
    """Minimal struct used as a body/return payload in wiring tests."""

    name: str


class ItemPath(Struct):
    """Path params carrying an item id."""

    id: str


class _ResourceApp(BaseApp):
    def __init__(self, resource: Resource) -> None:
        self._resource = resource
        super().__init__()

    async def _wire(self) -> None:
        self._include_resource(self._resource)


class _EndpointApp(BaseApp):
    def __init__(self, endpoint: Endpoint) -> None:
        self._endpoint = endpoint
        super().__init__()

    async def _wire(self) -> None:
        self._include_endpoint(self._endpoint)


class BadArgResource(Resource, path="/x"):
    """Resource whose handler uses an unsupported argument name."""

    async def create(self, body: P) -> P:  # 'body' is not a supported source name
        """Handler with an invalid 'body' source argument."""
        return body


class BodyOnGetResource(Resource, path="/x"):
    """Resource whose GET handler illegally declares a body source."""

    async def read_one(self, json: P) -> P:  # GET handlers cannot take a body
        """GET handler that illegally requests a JSON body."""
        return json


class NonStructReturnResource(Resource, path="/x"):
    """Resource whose handler declares an unsupported return type."""

    async def read_many(self) -> int:  # must return a Struct / list[Struct] / bytes / Response
        """Handler returning an unsupported plain int type."""
        return 0


class ExactPathEndpoint(Endpoint, path="/x"):
    """Endpoint that illegally declares a path slot for an exact path."""

    async def get(self, path: ItemPath) -> P:  # /x has no {id} slot; endpoint paths are exact
        """GET handler requesting a path slot that the exact path lacks."""
        return P(name=path.id)


class BadRawHeadersResource(Resource, path="/x"):
    """Resource whose raw_headers argument has the wrong annotation."""

    async def read_many(self, raw_headers: dict[str, str]) -> P:  # must be RawHeaders
        """Handler annotating raw_headers as something other than RawHeaders."""
        return P(name=raw_headers["name"])


class EmptyResource(Resource, path="/x"):
    """Resource defining none of the CRUD methods."""


class BadContentResource(Resource, path="/x"):
    """Resource whose 'content' argument is not annotated as bytes."""

    async def create(self, content: str) -> P:  # 'content' must be bytes
        """Handler annotating the raw body as str instead of bytes."""
        return P(name=content)


class BadHeadersResource(Resource, path="/x"):
    """Resource whose 'headers' argument is not a msgspec Struct."""

    async def read_many(self, headers: int) -> P:  # must be a Struct
        """Handler annotating headers as a non-Struct type."""
        return P(name=str(headers))


def test_unsupported_argument_name() -> None:
    """Wiring a handler with an unsupported argument name fails at startup."""
    with pytest.raises(RuntimeError, match="unsupported argument 'body'"):
        TestClient(_ResourceApp(BadArgResource()))


def test_body_on_get_handler() -> None:
    """Wiring a GET handler that takes a body fails at startup."""
    with pytest.raises(RuntimeError, match="GET handlers cannot take 'json'"):
        TestClient(_ResourceApp(BodyOnGetResource()))


def test_non_struct_return_type() -> None:
    """Wiring a handler with an unsupported return type fails at startup."""
    with pytest.raises(RuntimeError, match="must declare a return type"):
        TestClient(_ResourceApp(NonStructReturnResource()))


def test_endpoint_path_must_be_exact() -> None:
    """Wiring an endpoint with a templated path slot fails at startup."""
    with pytest.raises(RuntimeError, match="path is exact"):
        TestClient(_EndpointApp(ExactPathEndpoint()))


def test_resource_with_no_crud_methods() -> None:
    """Wiring a resource that defines no CRUD methods fails at startup."""
    with pytest.raises(RuntimeError, match="defines none of"):
        TestClient(_ResourceApp(EmptyResource()))


def test_raw_headers_wrong_annotation() -> None:
    """Wiring a raw_headers argument not annotated as RawHeaders fails at startup."""
    with pytest.raises(RuntimeError, match="'raw_headers' must be annotated as RawHeaders"):
        TestClient(_ResourceApp(BadRawHeadersResource()))


def test_content_must_be_bytes() -> None:
    """Wiring a 'content' argument not annotated as bytes fails at startup."""
    with pytest.raises(RuntimeError, match="'content' must be annotated as bytes"):
        TestClient(_ResourceApp(BadContentResource()))


def test_source_must_be_struct() -> None:
    """Wiring a binding source not annotated with a Struct fails at startup."""
    with pytest.raises(RuntimeError, match="must be annotated with a "):
        TestClient(_ResourceApp(BadHeadersResource()))


def test_path_must_start_with_slash() -> None:
    """Wiring a mount path without a leading slash fails at startup."""
    with pytest.raises(RuntimeError, match="must start with"):
        TestClient(_ResourceApp(_NoSlashMount()))


# --- Path template & route-segment validations ---


class ReadManyMixin:
    """Minimal collection handler, mixed into resources mounted at deliberately-bad paths."""

    async def read_many(self) -> P:
        """Collection handler (never reached — wiring fails first)."""
        return P(name="name")


class _NoSlashMount(ReadManyMixin, Resource, path="x"):
    """ReadManyMixin at a path without a leading slash."""


class _BadSlotMount(ReadManyMixin, Resource, path="/x/{1bad}"):
    """ReadManyMixin at a path with a non-identifier slot."""


class _DupSlotMount(ReadManyMixin, Resource, path="/x/{id}/{id}"):
    """ReadManyMixin at a path with a duplicate slot."""


class _UnbalancedMount(ReadManyMixin, Resource, path="/x/{id"):
    """ReadManyMixin at a path with an unbalanced brace."""


class _UncoveredSlotMount(ReadManyMixin, Resource, path="/x/{id}"):
    """ReadManyMixin at a templated path with no covering 'path' Struct."""


class DefaultedPath(Struct):
    """A path Struct whose field illegally carries a default."""

    id: str = "id"


class DefaultedPathResource(Resource, path="/x/{id}"):
    """Resource whose path Struct field has a default."""

    async def read_one(self, path: DefaultedPath) -> P:
        """Item handler with a defaulted path field."""
        return P(name=path.id)


class OtherPath(Struct):
    """A path Struct missing the template's slot."""

    other: str


class MissingSlotResource(Resource, path="/x/{id}"):
    """Resource whose path Struct omits a template slot."""

    async def read_one(self, path: OtherPath) -> P:
        """Item handler whose path Struct is missing the {id} slot."""
        return P(name=path.other)


class TrailingReadMany(Resource, path="/x"):
    """Resource whose read_many declares a trailing path field."""

    async def read_many(self, path: ItemPath) -> P:
        """Collection handler that illegally tries to extend the mount path."""
        return P(name=path.id)


def test_invalid_path_slot_identifier() -> None:
    """A template slot that isn't a valid identifier fails at startup."""
    with pytest.raises(RuntimeError, match="is not a valid identifier"):
        TestClient(_ResourceApp(_BadSlotMount()))


def test_duplicate_path_slot() -> None:
    """A template with a repeated slot fails at startup."""
    with pytest.raises(RuntimeError, match="duplicate slot"):
        TestClient(_ResourceApp(_DupSlotMount()))


def test_malformed_path_segment() -> None:
    """A segment with an unbalanced brace fails at startup."""
    with pytest.raises(RuntimeError, match="malformed segment"):
        TestClient(_ResourceApp(_UnbalancedMount()))


def test_missing_path_struct_for_slots() -> None:
    """A templated mount with no 'path' Struct to cover its slots fails at startup."""
    with pytest.raises(RuntimeError, match="must declare 'path' covering"):
        TestClient(_ResourceApp(_UncoveredSlotMount()))


def test_path_fields_cannot_have_defaults() -> None:
    """A path Struct field with a default fails at startup."""
    with pytest.raises(RuntimeError, match="path fields cannot have defaults"):
        TestClient(_ResourceApp(DefaultedPathResource()))


def test_path_struct_missing_template_slot() -> None:
    """A path Struct that doesn't cover a template slot fails at startup."""
    with pytest.raises(RuntimeError, match="missing template slots"):
        TestClient(_ResourceApp(MissingSlotResource()))


def test_read_many_cannot_extend_path() -> None:
    """A read_many declaring a trailing path field fails at startup."""
    with pytest.raises(RuntimeError, match="collections live at the mount path"):
        TestClient(_ResourceApp(TrailingReadMany()))


# --- Auth / user-source validations ---


class Creds(Struct):
    """Bearer credentials lifted from the request headers."""

    authorization: str


class User(Struct):
    """The authenticated caller."""

    id: str


class Other(Struct):
    """A user type that does not match the authenticator's return."""

    name: str


@dataclass
class GoodAuth:
    """A valid authenticator returning a User."""

    async def authenticate(self, headers: Creds) -> User:
        """Resolve credentials to a user (never reached — wiring fails first)."""
        _ = headers
        return User(id="id")


class UserWithoutAuthResource(Resource, path="/x"):
    """Resource declaring 'user' but wired without any auth."""

    async def read_many(self, user: User) -> User:
        """Handler requesting the auth result where no auth is configured."""
        return user


class UserMismatchResource(Resource, path="/x"):
    """Resource whose 'user' type disagrees with the authenticator's return."""

    async def read_many(self, user: Other) -> Other:
        """Handler annotating 'user' as a type the auth doesn't return."""
        return user


class _AuthApp(BaseApp):
    def __init__(self, resource: Resource, auth: Auth[Creds, User] | None = None) -> None:
        self._resource = resource
        self._auth = auth
        super().__init__()

    async def _wire(self) -> None:
        self._include_resource(self._resource, auth=self._auth)


def test_user_declared_without_auth() -> None:
    """Declaring a 'user' argument with no auth configured fails at startup."""
    with pytest.raises(RuntimeError, match="declares 'user' but no auth"):
        TestClient(_AuthApp(UserWithoutAuthResource()))


def test_user_type_mismatch_with_auth() -> None:
    """A 'user' type that disagrees with the authenticator's return fails at startup."""
    with pytest.raises(RuntimeError, match="'user' expects"):
        TestClient(_AuthApp(UserMismatchResource(), auth=GoodAuth()))


# --- Duplicate route registration ---


class FirstEndpoint(Endpoint, path="/dup"):
    """An endpoint at a shared path."""

    async def get(self) -> P:
        """First GET handler at the shared path."""
        return P(name="first")


class SecondEndpoint(Endpoint, path="/dup"):
    """A second endpoint colliding on the same method and path."""

    async def get(self) -> P:
        """Second GET handler that collides with the first."""
        return P(name="second")


class _DuplicateRouteApp(BaseApp):
    async def _wire(self) -> None:
        self._include_endpoint(FirstEndpoint())
        self._include_endpoint(SecondEndpoint())


def test_duplicate_route_registration() -> None:
    """Registering two handlers for the same method and path fails at startup."""
    with pytest.raises(RuntimeError, match="already registered"):
        TestClient(_DuplicateRouteApp())
