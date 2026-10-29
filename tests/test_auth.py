"""Auth: accept, reject, and user injection — via the demo app's /me endpoint; anonymous
callers (absent / valid / invalid credentials) via its /spotlight endpoint."""

from collections.abc import Generator
from dataclasses import dataclass
from typing import NewType, Protocol, cast, runtime_checkable

import pytest
from msgspec import Struct

from jero import Auth, BaseApp, Endpoint, HTTPError
from jero.testing import TestClient


def test_valid_token_injects_user(client: TestClient) -> None:
    """A valid token authenticates and the user is injected into the handler."""
    resp = client.get("/me", headers={"authorization": "Bearer token"})
    assert resp.status_code == 200
    assert resp.json() == {"id": "user-id", "name": "user-name", "maySeeSpotlight": True}


def test_bad_token_is_401(client: TestClient) -> None:
    """An incorrect bearer token is rejected with 401."""
    resp = client.get("/me", headers={"authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert resp.json() == {"type": "invalid-token", "title": "Invalid token", "status": 401}


def test_missing_auth_header_is_401(client: TestClient) -> None:
    """A missing authorization header is rejected with 401."""
    resp = client.get("/me")
    assert resp.status_code == 401
    assert resp.json() == {
        "type": "authentication-required",
        "title": "Authentication required",
        "status": 401,
    }


# --- Anonymous callers: credentials as an input, not a gate ---


def test_absent_credentials_bind_none(client: TestClient) -> None:
    """With no credentials at all, the route runs with user=None."""
    resp = client.get("/spotlight")
    assert resp.status_code == 200
    assert resp.json() == {"widgetId": "spotlight", "personalizedFor": None}


def test_valid_credentials_bind_the_user(client: TestClient) -> None:
    """A valid token on an anonymous-accepting route injects the authenticated user."""
    resp = client.get("/spotlight", headers={"authorization": "Bearer token"})
    assert resp.status_code == 200
    assert resp.json() == {"widgetId": "spotlight", "personalizedFor": "user-name"}


def test_invalid_credentials_are_still_401(client: TestClient) -> None:
    """Credentials that are present but invalid are rejected, exactly as under required auth."""
    resp = client.get("/spotlight", headers={"authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert resp.json() == {"type": "invalid-token", "title": "Invalid token", "status": 401}


def test_authenticated_but_unpermitted_caller_gets_no_content(client: TestClient) -> None:
    """An authenticated caller who may not see the spotlight gets 204, not a rejection —
    auth failure is only a 401 when the credentials themselves are the problem."""
    resp = client.get("/spotlight", headers={"authorization": "Bearer no-spotlight-token"})
    assert resp.status_code == 204
    assert resp.content == b""


# --- A gating authenticator still gates handlers that ignore the user ---


def test_gated_handler_without_user_argument_still_requires_credentials(
    client: TestClient,
) -> None:
    """The widget handlers declare no 'user'; the gate runs for them all the same."""
    assert client.get("/widgets").status_code == 401
    assert client.get("/widgets", headers={"authorization": "Bearer wrong"}).status_code == 401


# --- The three cases again on a local app, with a non-bearer authenticator ---


class Creds(Struct):
    """Credentials whose optional field lets an absent token reach the authenticator."""

    authorization: str | None = None


class Caller(Struct):
    """The authenticated caller."""

    id: str


class Greeting(Struct):
    """Who the caller turned out to be."""

    caller: str


class BadTokenError(HTTPError, type="bad-token", title="Bad token", status=401):
    """Raised for a token that is present but unknown."""


@dataclass
class OptionalCallerAuth:
    """An authenticator that reports absent credentials by returning None."""

    async def authenticate(self, headers: Creds) -> Caller | None:
        """Resolve the token, ``None`` when none was presented, or raise for a bad one."""
        if headers.authorization is None:
            return None
        if headers.authorization != "token":
            raise BadTokenError()
        return Caller(id="caller-id")


class GreetingEndpoint(Endpoint, path="/greeting"):
    """An endpoint that greets anonymous and authenticated callers differently."""

    async def get(self, user: Caller | None) -> Greeting:
        """Greet the caller by id, or as anonymous."""
        return Greeting(caller="anonymous" if user is None else user.id)


class _GreetingApp(BaseApp):
    """App mounting the greeting endpoint behind an anonymous-accepting authenticator."""

    async def wire(self) -> None:
        self._include_endpoint(GreetingEndpoint(), auth=OptionalCallerAuth())


@pytest.fixture(name="greeting_client")
def _greeting_client() -> Generator[TestClient]:
    with TestClient(_GreetingApp()) as client:
        yield client


def test_anonymous_caller_is_served(greeting_client: TestClient) -> None:
    """No credentials at all: the handler runs with user=None."""
    resp = greeting_client.get("/greeting")
    assert resp.status_code == 200
    assert resp.json() == {"caller": "anonymous"}


def test_authenticated_caller_is_served(greeting_client: TestClient) -> None:
    """Valid credentials: the handler runs with the resolved user."""
    resp = greeting_client.get("/greeting", headers={"authorization": "token"})
    assert resp.status_code == 200
    assert resp.json() == {"caller": "caller-id"}


def test_invalid_credentials_are_rejected(greeting_client: TestClient) -> None:
    """Present-but-invalid credentials are rejected by the authenticator's own error."""
    resp = greeting_client.get("/greeting", headers={"authorization": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["type"] == "bad-token"


# --- The framework's own two rejections, before and after authenticate ---


class RequiredCreds(Struct):
    """Credentials whose field is required, so an absent header cannot bind."""

    authorization: str


@dataclass
class RequiredCredsAuth:
    """An authenticator whose credentials Struct leaves no room for absence."""

    async def authenticate(self, headers: RequiredCreds) -> Caller:
        """Resolve the caller (never reached when the header is missing)."""
        return Caller(id=headers.authorization)


class StrictEndpoint(Endpoint, path="/strict"):
    """An endpoint behind an authenticator with required credentials."""

    async def get(self, user: Caller) -> Greeting:
        """Greet the caller."""
        return Greeting(caller=user.id)


class _StrictApp(BaseApp):
    """App whose authenticator cannot see absent credentials."""

    async def wire(self) -> None:
        self._include_endpoint(StrictEndpoint(), auth=RequiredCredsAuth())


def test_unbindable_credentials_are_401_before_authenticate() -> None:
    """When the credentials Struct has no room for a missing header, jero answers 401
    itself — authenticate never runs. This is why reporting absence needs an optional
    field; it is the documented cost of a required one."""
    with TestClient(_StrictApp()) as client:
        resp = client.get("/strict")
        assert resp.status_code == 401
        assert resp.json()["type"] == "authentication-required"
        assert client.get("/strict", headers={"authorization": "id"}).status_code == 200


@dataclass
class LyingAuth:
    """An authenticator that returns None despite declaring it never does."""

    async def authenticate(self, headers: Creds) -> Caller:
        """Violate the declared return type, as a mistyped authenticator would."""
        _ = headers
        return cast("Caller", None)


class LyingEndpoint(Endpoint, path="/lying"):
    """An endpoint whose handler was wired against a non-optional user."""

    async def get(self, user: Caller) -> Greeting:
        """Greet the caller (must never receive None)."""
        return Greeting(caller=user.id)


class _LyingApp(BaseApp):
    """App behind an authenticator that breaks its own contract."""

    async def wire(self) -> None:
        self._include_endpoint(LyingEndpoint(), auth=LyingAuth())


def test_none_from_a_gating_authenticator_is_rejected_not_bound() -> None:
    """A None return contradicts '-> Caller', which the handler was checked against, so the
    framework rejects rather than binding None into a 'user: Caller' argument."""
    with TestClient(_LyingApp()) as client:
        resp = client.get("/lying", headers={"authorization": "token"})
        assert resp.status_code == 401
        assert resp.json()["type"] == "authentication-required"


# --- The user type is the app's choice: jero hands it over untouched ---


@dataclass
class Account:
    """A plain dataclass user, as an app might already have one."""

    id: str


@dataclass
class AdminAccount(Account):
    """A more specific account, for a handler that declares the base class."""


@dataclass
class AdminAccountAuth:
    """An authenticator returning a dataclass subclass rather than a Struct."""

    async def authenticate(self, headers: Creds) -> AdminAccount:
        """Resolve the token to an admin account."""
        if headers.authorization is None:
            raise BadTokenError()
        return AdminAccount(id=headers.authorization)


class AccountEndpoint(Endpoint, path="/account"):
    """An endpoint declaring the base class of what its authenticator returns."""

    async def get(self, user: Account) -> Greeting:
        """Greet the account."""
        return Greeting(caller=user.id)


@dataclass
class UserIdAuth:
    """An authenticator whose user is a bare id, accepting anonymous callers."""

    async def authenticate(self, headers: Creds) -> int | None:
        """The token is the id; ``None`` when none was presented."""
        return None if headers.authorization is None else int(headers.authorization)


class UserIdEndpoint(Endpoint, path="/user-id"):
    """An endpoint whose user is a bare id."""

    async def get(self, user: int | None) -> Greeting:
        """Greet the caller by id, or as anonymous."""
        return Greeting(caller="anonymous" if user is None else str(user))


@dataclass
class CallerOrAccountAuth:
    """An authenticator returning one of two user types, or None for no credentials."""

    async def authenticate(self, headers: Creds) -> Caller | Account | None:
        """A service token resolves to a Caller, any other to an Account."""
        if headers.authorization is None:
            return None
        if headers.authorization == "service":
            return Caller(id="caller-id")
        return Account(id="account-id")


class CallerOrAccountEndpoint(Endpoint, path="/caller-or-account"):
    """An endpoint declaring exactly the union its authenticator returns."""

    async def get(self, user: Caller | Account | None) -> Greeting:
        """Greet whichever kind of user arrived."""
        return Greeting(caller="anonymous" if user is None else user.id)


class CallerOnlyEndpoint(Endpoint, path="/caller-only"):
    """An endpoint declaring only one member of its authenticator's union."""

    async def get(self, user: Caller | None) -> Greeting:
        """Greet the caller (never reached — wiring fails first)."""
        return Greeting(caller="anonymous" if user is None else user.id)


@dataclass
class NoUserAuth:
    """An authenticator that declares no user type at all."""

    async def authenticate(self, headers: Creds) -> None:
        """Return nothing (never reached — wiring fails first)."""
        _ = headers


class _UserTypeApp(BaseApp):
    """App mounting one endpoint behind one authenticator."""

    def __init__(self, endpoint: Endpoint, auth: Auth[Creds, object]) -> None:
        self._endpoint = endpoint
        self._auth = auth
        super().__init__()

    async def wire(self) -> None:
        self._include_endpoint(self._endpoint, auth=self._auth)


def test_dataclass_user_binds_to_a_base_class_handler() -> None:
    """A dataclass user binds, and a handler may declare a base class of it."""
    with TestClient(_UserTypeApp(AccountEndpoint(), AdminAccountAuth())) as client:
        resp = client.get("/account", headers={"authorization": "account-id"})
        assert resp.status_code == 200
        assert resp.json() == {"caller": "account-id"}


def test_bare_id_user_binds() -> None:
    """A bare scalar user binds, and ``| None`` still accepts anonymous callers."""
    with TestClient(_UserTypeApp(UserIdEndpoint(), UserIdAuth())) as client:
        assert client.get("/user-id").json() == {"caller": "anonymous"}
        assert client.get("/user-id", headers={"authorization": "1"}).json() == {"caller": "1"}


def test_union_user_binds_each_member() -> None:
    """A union user binds whichever member the authenticator returned; ``A | B | None`` is
    read as an anonymous-accepting ``A | B``."""
    with TestClient(_UserTypeApp(CallerOrAccountEndpoint(), CallerOrAccountAuth())) as client:
        assert client.get("/caller-or-account").json() == {"caller": "anonymous"}
        resp = client.get("/caller-or-account", headers={"authorization": "service"})
        assert resp.json() == {"caller": "caller-id"}
        resp = client.get("/caller-or-account", headers={"authorization": "token"})
        assert resp.json() == {"caller": "account-id"}


def test_union_user_must_cover_every_member() -> None:
    """A handler declaring one member of the authenticator's union fails at startup, since
    the other may arrive."""
    with pytest.raises(RuntimeError, match="'user' expects Caller but CallerOrAccountAuth"):
        TestClient(_UserTypeApp(CallerOnlyEndpoint(), CallerOrAccountAuth()))


def test_authenticator_without_a_user_type_fails() -> None:
    """``-> None`` declares no user, so there is nothing to hand the handler."""
    with pytest.raises(RuntimeError, match="must declare the user type it returns"):
        TestClient(_UserTypeApp(UserIdEndpoint(), NoUserAuth()))


@dataclass
class CallerOrAccountGateAuth:
    """An authenticator returning one of two user types, gating every caller."""

    async def authenticate(self, headers: Creds) -> Caller | Account:
        """A service token resolves to a Caller, any other to an Account."""
        if headers.authorization == "service":
            return Caller(id="caller-id")
        return Account(id="account-id")


class GatedCallerOrAccountEndpoint(Endpoint, path="/gated-caller-or-account"):
    """An endpoint declaring exactly the (non-optional) union its authenticator returns."""

    async def get(self, user: Caller | Account) -> Greeting:
        """Greet whichever kind of user arrived."""
        return Greeting(caller=user.id)


class NoneUserEndpoint(Endpoint, path="/none-user"):
    """An endpoint annotating 'user' as None, which names no user type."""

    async def get(self, user: None) -> Greeting:
        """Never reached — wiring fails first."""
        _ = user
        return Greeting(caller="anonymous")


class HasId(Protocol):
    """A structural user type that ``issubclass`` cannot check."""

    id: str


class HasIdEndpoint(Endpoint, path="/has-id"):
    """An endpoint declaring a Protocol its authenticator's class happens to satisfy."""

    async def get(self, user: HasId) -> Greeting:
        """Never reached — wiring fails first."""
        return Greeting(caller=user.id)


def test_gating_union_user_binds() -> None:
    """A union without ``None`` gates, and binds whichever member arrived."""
    with TestClient(
        _UserTypeApp(GatedCallerOrAccountEndpoint(), CallerOrAccountGateAuth())
    ) as client:
        resp = client.get("/gated-caller-or-account", headers={"authorization": "service"})
        assert resp.json() == {"caller": "caller-id"}


def test_none_user_annotation_fails() -> None:
    """``user: None`` names no user type, so it fails at startup."""
    with pytest.raises(RuntimeError, match="'user' must be annotated with the type"):
        TestClient(_UserTypeApp(NoneUserEndpoint(), AdminAccountAuth()))


def test_protocol_user_needs_explicit_inheritance() -> None:
    """A Protocol matches by explicit inheritance only: a dataclass that merely satisfies it
    structurally fails at startup."""
    with pytest.raises(RuntimeError, match="'user' expects HasId but AdminAccountAuth"):
        TestClient(_UserTypeApp(HasIdEndpoint(), AdminAccountAuth()))


type MaybeUserId = int | None


@dataclass
class AliasedUserIdAuth:
    """An authenticator declaring its anonymous-accepting return through a ``type`` alias."""

    async def authenticate(self, headers: Creds) -> MaybeUserId:
        """The token is the id; ``None`` when none was presented."""
        return None if headers.authorization is None else int(headers.authorization)


class AliasedUserIdEndpoint(Endpoint, path="/aliased-user-id"):
    """An endpoint declaring its optional user through the same alias."""

    async def get(self, user: MaybeUserId) -> Greeting:
        """Greet the caller by id, or as anonymous."""
        return Greeting(caller="anonymous" if user is None else str(user))


def test_aliased_optional_user_still_accepts_anonymous_callers() -> None:
    """``-> MaybeUserId`` (an alias of ``int | None``) is read through the alias, so the
    route accepts anonymous callers rather than silently gating them."""
    with TestClient(_UserTypeApp(AliasedUserIdEndpoint(), AliasedUserIdAuth())) as client:
        assert client.get("/aliased-user-id").json() == {"caller": "anonymous"}
        resp = client.get("/aliased-user-id", headers={"authorization": "1"})
        assert resp.json() == {"caller": "1"}


@dataclass
class OptionalAdminAccountAuth:
    """An anonymous-accepting authenticator returning a dataclass subclass."""

    async def authenticate(self, headers: Creds) -> AdminAccount | None:
        """Resolve the token to an admin account; ``None`` when none was presented."""
        return None if headers.authorization is None else AdminAccount(id=headers.authorization)


def test_optionality_error_names_the_authenticators_own_type() -> None:
    """The error names what the authenticator returns, not the handler's base class."""
    with pytest.raises(
        RuntimeError,
        match=r"'Account \| None' — OptionalAdminAccountAuth.authenticate returns "
        r"'AdminAccount \| None'",
    ):
        TestClient(_UserTypeApp(AccountEndpoint(), OptionalAdminAccountAuth()))


class AccountOrCallerEndpoint(Endpoint, path="/account-or-caller"):
    """An endpoint accepting a wider union than its authenticator returns."""

    async def get(self, user: Account | Caller) -> Greeting:
        """Greet whichever kind of user arrived."""
        return Greeting(caller=user.id)


def test_handler_may_accept_a_wider_union() -> None:
    """Every type the authenticator returns is accepted (``AdminAccount`` as an ``Account``),
    so a handler declaring more than that binds."""
    with TestClient(_UserTypeApp(AccountOrCallerEndpoint(), AdminAccountAuth())) as client:
        resp = client.get("/account-or-caller", headers={"authorization": "account-id"})
        assert resp.json() == {"caller": "account-id"}


@dataclass
class HasIdAccount(HasId):
    """An account that inherits the Protocol explicitly."""

    id: str


@dataclass
class HasIdAccountAuth:
    """An authenticator returning a class that explicitly inherits ``HasId``."""

    async def authenticate(self, headers: Creds) -> HasIdAccount:
        """Resolve the token to an account."""
        return HasIdAccount(id=headers.authorization or "account-id")


def test_protocol_user_matches_by_explicit_inheritance() -> None:
    """A class that explicitly inherits the handler's Protocol binds."""
    with TestClient(_UserTypeApp(HasIdEndpoint(), HasIdAccountAuth())) as client:
        resp = client.get("/has-id", headers={"authorization": "account-id"})
        assert resp.json() == {"caller": "account-id"}


@runtime_checkable
class Describable(Protocol):
    """A runtime-checkable, method-only Protocol ``issubclass`` would check structurally."""

    def describe(self) -> str:
        """Describe the user."""
        ...  # pylint: disable=unnecessary-ellipsis  # Protocol stub; pyright needs the body


@dataclass
class DescribableAccount:
    """An account that has ``describe`` without inheriting ``Describable``."""

    id: str

    def describe(self) -> str:
        """Describe the account."""
        return self.id


@dataclass
class DescribableAccountAuth:
    """An authenticator returning a class that only structurally satisfies ``Describable``."""

    async def authenticate(self, headers: Creds) -> DescribableAccount:
        """Resolve the token to an account (never reached — wiring fails first)."""
        return DescribableAccount(id=headers.authorization or "account-id")


class DescribableEndpoint(Endpoint, path="/describable"):
    """An endpoint declaring a runtime-checkable Protocol."""

    async def get(self, user: Describable) -> Greeting:
        """Never reached — wiring fails first."""
        return Greeting(caller=user.describe())


def test_runtime_checkable_protocol_still_needs_explicit_inheritance() -> None:
    """Even a Protocol ``issubclass`` could check structurally matches only by explicit
    inheritance, so the rule is one rule for every Protocol."""
    with pytest.raises(RuntimeError, match="'user' expects Describable but DescribableAccount"):
        TestClient(_UserTypeApp(DescribableEndpoint(), DescribableAccountAuth()))


UserId = NewType("UserId", int)


@dataclass
class NewTypeUserIdAuth:
    """An authenticator whose user is a ``NewType`` over ``int``."""

    async def authenticate(self, headers: Creds) -> UserId | None:
        """The token is the id (never reached — wiring fails first)."""
        return None if headers.authorization is None else UserId(int(headers.authorization))


def test_newtype_user_must_match_exactly() -> None:
    """A ``NewType`` is not a class, so it matches only itself: ``user: int`` fails."""
    with pytest.raises(RuntimeError, match="'user' expects int but NewTypeUserIdAuth"):
        TestClient(_UserTypeApp(UserIdEndpoint(), NewTypeUserIdAuth()))
