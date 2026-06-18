"""Reverse-routed ``Location`` / ``Link``: header emission through the ``TestClient``,
and the construction-time params guard on ``from_operation`` (its public surface)."""

from collections.abc import Generator

import pytest

from jero import Link, Location, TestClient
from tests.demo_app import JobPath, JobsResource, LinksDemoApp, WidgetPath


@pytest.fixture(name="client")
def _client() -> Generator[TestClient]:
    with TestClient(LinksDemoApp()) as client:
        yield client


def test_location_reverse_routes_to_read_one(client: TestClient) -> None:
    """``create`` returns 201 with a ``Location`` reverse-routed to ``read_one``,
    the path id filled from the params Struct."""
    resp = client.post("/jobs", json={"id": "job-id"})
    assert resp.status_code == 201
    assert resp.headers["location"] == "/jobs/job-id"


def test_links_join_into_one_header(client: TestClient) -> None:
    """A list of links becomes one RFC 8288 ``Link`` header: the operation link plus a
    literal ``from_url`` link with its title/type params."""
    resp = client.post("/jobs", json={"id": "job-id"})
    link = resp.headers["link"]
    assert '</jobs/job-id>; rel="self"' in link
    assert '</docs/jobs>; rel="help"; title="Job docs"; type="text/html"' in link
    assert link.count(", ") == 1


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


def test_wrong_params_type_fails_at_construction() -> None:
    """The headline guard: the wrong path Struct is rejected the instant the link is
    built, without an app — introspected from the operation's own signature."""
    with pytest.raises(TypeError, match="expects params of type JobPath, got WidgetPath"):
        Location.from_operation(JobsResource.read_one, params=WidgetPath(widget_id="widget-id"))


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
