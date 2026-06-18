"""Reverse-routed ``Location`` / ``Link``: header emission through the ``TestClient``,
and the construction-time params guard on ``from_operation`` (its public surface)."""

from collections.abc import Generator

import pytest

from jero import Link, Location, TestClient, WiringError
from tests.demo_app import JobPath, JobsResource, LinksDemoApp, WidgetPath


class _SubJobPath(JobPath):
    """A subclass of an operation's path struct — used to prove exact-type rejection."""


@pytest.fixture(name="client")
def _client() -> Generator[TestClient]:
    with TestClient(LinksDemoApp()) as client:
        yield client


@pytest.fixture(name="forwarded_client")
def _forwarded_client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("JERO_TRUST_FORWARDED", "1")
    with TestClient(LinksDemoApp()) as client:
        yield client


def test_location_reverse_routes_to_read_one(client: TestClient) -> None:
    """``create`` returns 201 with a ``Location`` reverse-routed to ``read_one``,
    the path id filled from the params Struct."""
    resp = client.post("/jobs", json={"id": "job-id"})
    assert resp.status_code == 201
    assert resp.headers["location"] == "/jobs/job-id"


def test_links_join_into_one_header(client: TestClient) -> None:
    """A list of links becomes one RFC 8288 ``Link`` header: the operation link, a
    relative link (with title/type params), and an absolute link."""
    resp = client.post("/jobs", json={"id": "job-id"})
    link = resp.headers["link"]
    assert '</jobs/job-id>; rel="self"' in link
    assert '</docs/jobs>; rel="help"; title="Job docs"; type="text/html"' in link
    assert '<https://status.example.com>; rel="status"' in link
    assert link.count(", ") == 2


def test_from_ref_resolves_across_modules(client: TestClient) -> None:
    """The ``from_ref`` string hatch resolves through the class's ``ref=`` to the same URL
    the operation reference would produce."""
    resp = client.get("/job-link")
    assert resp.headers["link"] == '</jobs/job-id>; rel="related"'


def test_read_one_serves_the_reversed_url(client: TestClient) -> None:
    """The URL a ``Location`` points at is really mounted."""
    resp = client.get("/jobs/job-id")
    assert resp.status_code == 200
    assert resp.json() == {"id": "job-id"}


def test_trust_forwarded_builds_absolute_public_url(forwarded_client: TestClient) -> None:
    """With ``trust_forwarded``, the reversed URL is the public absolute URL rebuilt from
    ``X-Forwarded-Proto`` / ``-Host`` and prefixed with the stripped ``X-Forwarded-Prefix``."""
    resp = forwarded_client.post(
        "/jobs",
        json={"id": "job-id"},
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "api.example.com",
            "x-forwarded-prefix": "/api",
        },
    )
    assert resp.headers["location"] == "https://api.example.com/api/jobs/job-id"


def test_relative_links_rewritten_absolute_links_verbatim_under_forwarding(
    forwarded_client: TestClient,
) -> None:
    """Under forwarding, operation and ``from_path`` links get the public origin;
    ``from_url`` links pass through unchanged."""
    resp = forwarded_client.post(
        "/jobs",
        json={"id": "job-id"},
        headers={"x-forwarded-proto": "https", "x-forwarded-host": "api.example.com"},
    )
    link = resp.headers["link"]
    assert '<https://api.example.com/jobs/job-id>; rel="self"' in link
    assert '<https://api.example.com/docs/jobs>; rel="help"' in link
    assert '<https://status.example.com>; rel="status"' in link


def test_forwarded_port_is_included_when_non_default(forwarded_client: TestClient) -> None:
    """A non-default ``X-Forwarded-Port`` lands in the authority."""
    resp = forwarded_client.post(
        "/jobs",
        json={"id": "job-id"},
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "host",
            "x-forwarded-port": "8443",
        },
    )
    assert resp.headers["location"] == "https://host:8443/jobs/job-id"


def test_forwarded_headers_ignored_without_trust(client: TestClient) -> None:
    """The default app does not trust forwarded headers, so a spoofed host can't poison
    the URL — it stays relative."""
    resp = client.post(
        "/jobs", json={"id": "job-id"}, headers={"x-forwarded-host": "evil.example.com"}
    )
    assert resp.headers["location"] == "/jobs/job-id"


def test_base_url_makes_urls_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    """``JERO_BASE_URL`` makes reversed URLs absolute against a static origin — no header
    trust, so a spoofed ``X-Forwarded-Host`` is ignored."""
    monkeypatch.setenv("JERO_BASE_URL", "https://api.example.com")
    with TestClient(LinksDemoApp()) as client:
        resp = client.post(
            "/jobs", json={"id": "job-id"}, headers={"x-forwarded-host": "evil.example.com"}
        )
        assert resp.headers["location"] == "https://api.example.com/jobs/job-id"


def test_base_url_and_trust_forwarded_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting both env vars is a startup error — one source for the URL base."""
    monkeypatch.setenv("JERO_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("JERO_TRUST_FORWARDED", "1")
    with pytest.raises(WiringError, match="mutually exclusive"):
        LinksDemoApp()


def test_wrong_params_type_fails_at_construction() -> None:
    """The headline guard: the wrong path Struct is rejected the instant the link is
    built, without an app — introspected from the operation's own signature."""
    with pytest.raises(TypeError, match="expects params of type JobPath, got WidgetPath"):
        Location.from_operation(JobsResource.read_one, params=WidgetPath(widget_id="widget-id"))


def test_path_struct_subclass_is_rejected() -> None:
    """Exact match, not is-a: even a subclass of the operation's path struct is rejected."""
    with pytest.raises(TypeError, match="expects params of type JobPath, got _SubJobPath"):
        Location.from_operation(JobsResource.read_one, params=_SubJobPath(job_id="job-id"))


def test_missing_params_fails_at_construction() -> None:
    """An operation with path slots requires params."""
    with pytest.raises(TypeError, match="requires params of type JobPath"):
        Link.from_operation(JobsResource.read_one, rel="self")


def test_unexpected_params_fails_at_construction() -> None:
    """An operation with no path slots rejects params."""
    with pytest.raises(TypeError, match="takes no path params"):
        Location.from_operation(JobsResource.create, params=JobPath(job_id="job-id"))


def test_malformed_ref_fails_at_construction() -> None:
    """``from_ref`` needs a ``name.operation`` string."""
    with pytest.raises(TypeError, match="ref must be"):
        Link.from_ref("jobs", rel="self")
