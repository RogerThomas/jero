"""Auth: accept, reject, and user injection — via the demo app's /me endpoint; anonymous
callers (absent / valid / invalid credentials) via its /spotlight endpoint."""

from collections.abc import Generator
from dataclasses import dataclass

import pytest
from msgspec import Struct

from jero import BaseApp, Endpoint, HTTPError
from jero.testing import TestClient


def test_valid_token_injects_user(client: TestClient) -> None:
    """A valid token authenticates and the user is injected into the handler."""
    resp = client.get("/me", headers={"authorization": "Bearer token"})
    assert resp.status_code == 200
    assert resp.json() == {"id": "user-id", "name": "user-name"}


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
        self.include_endpoint(GreetingEndpoint(), auth=OptionalCallerAuth())


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
