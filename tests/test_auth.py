"""Auth: accept, reject, and user injection — via the demo app's /me endpoint."""

from jero import TestClient


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
