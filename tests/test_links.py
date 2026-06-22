"""Reverse-routed ``Location`` / ``Link``: header emission through the ``TestClient``,
and the construction-time path guard on ``from_operation`` (its public surface)."""

from collections.abc import Generator

import pytest

from demo_app.models import Camel, WidgetPath
from jero import BaseApp, Endpoint, JSONResponse, Link, Location, Resource, TestClient, WiringError
from jero.links import PathTarget, URLTarget


class Job(Camel):
    """A created job; its id flows into the reversed ``Location`` / ``Link`` URLs."""

    id: str


class JobPath(Camel):
    """Path Struct for a single job — its ``job_id`` fills the reversed URL slot."""

    job_id: str


class JobsResource(Resource, path="/jobs", ref="jobs"):
    """Jobs collection exercising reverse-routed ``Location`` / ``Link`` headers. Kept local
    to this module and un-authed so the forwarded-header and error-contract tests stay
    focused on the link machinery rather than on auth."""

    async def read_many(self) -> list[Job]:
        """List jobs — a slot-less operation, the ``collection`` reversal target."""
        return [Job(id="job-id")]

    async def read_one(self, path: JobPath) -> Job:
        """Return a single job by path id (the reversal target)."""
        return Job(id=path.job_id)

    async def create(self, json: Job) -> JSONResponse[Job]:
        """Create a job, pointing the response at the new resource."""
        return JSONResponse(
            json=json,
            status_code=201,
            location=Location.from_operation(JobsResource.read_one, path=JobPath(job_id=json.id)),
            links=[
                Link.from_operation(
                    JobsResource.read_one, rel="self", path=JobPath(job_id=json.id)
                ),
                Link.from_operation(JobsResource.read_many, rel="collection"),
                Link.from_path("/docs/jobs", rel="help", title="Job docs", media_type="text/html"),
                Link.from_url("https://status.example.com", rel="status"),
            ],
        )


class JobLinkEndpoint(Endpoint, path="/job-link"):
    """A second 'module' that can't import ``JobsResource`` (imagine an import cycle), so it
    addresses the jobs route by string ref instead of by the operation reference."""

    async def get(self) -> JSONResponse[Job]:
        """Return a job carrying a cross-module ``Link`` resolved through the ref."""
        return JSONResponse(
            json=Job(id="job-id"),
            links=[Link.from_ref("jobs.read_one", rel="related", path=JobPath(job_id="job-id"))],
        )


class JobRedirectEndpoint(Endpoint, path="/latest-job"):
    """A 303 redirect to the canonical job URL, with the ``Location`` reverse-routed by
    string ref (the cross-module form)."""

    async def get(self) -> JSONResponse[Job]:
        """Redirect to the latest job's canonical URL."""
        return JSONResponse(
            json=Job(id="job-id"),
            status_code=303,
            location=Location.from_ref("jobs.read_one", path=JobPath(job_id="job-id")),
        )


class LinksDemoApp(BaseApp):
    """Demonstrates reverse-routed ``Location`` / ``Link``: typed ``from_operation``, a literal
    ``from_path`` / ``from_url``, and the ``from_ref`` string hatch across 'modules'.

    Whether the emitted URLs are relative or absolute is decided by the environment
    (``JERO_BASE_URL`` / ``JERO_TRUST_FORWARDED``), read once at construction."""

    async def _wire(self) -> None:
        """Wire the jobs resource and the cross-module link / redirect endpoints."""
        self._include_resource(JobsResource())
        self._include_endpoint(JobLinkEndpoint())
        self._include_endpoint(JobRedirectEndpoint())


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
    the path id filled from the path Struct."""
    resp = client.post("/jobs", json={"id": "job-id"})
    assert resp.status_code == 201
    assert resp.headers["location"] == "/jobs/job-id"


def test_links_join_into_one_header(client: TestClient) -> None:
    """A list of links becomes one RFC 8288 ``Link`` header: an item operation, a
    slot-less ``collection`` operation, a relative path link, and an absolute url link."""
    resp = client.post("/jobs", json={"id": "job-id"})
    link = resp.headers["link"]
    assert '</jobs/job-id>; rel="self"' in link
    assert '</jobs>; rel="collection"' in link
    assert '</docs/jobs>; rel="help"; title="Job docs"; type="text/html"' in link
    assert '<https://status.example.com>; rel="status"' in link
    assert link.count(", ") == 3


def test_read_many_lists_jobs(client: TestClient) -> None:
    """The slot-less ``read_many`` the ``collection`` link points at is really mounted."""
    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert resp.json() == [{"id": "job-id"}]


def test_redirect_uses_location_from_ref(client: TestClient) -> None:
    """A 303 with a ``Location`` reverse-routed by string ref resolves to the job URL."""
    resp = client.get("/latest-job")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/jobs/job-id"


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


def test_forwarded_host_header_fallback(forwarded_client: TestClient) -> None:
    """With no ``X-Forwarded-Host`` (or proto), the origin falls back to the ``Host``
    header and ``http``."""
    resp = forwarded_client.post(
        "/jobs", json={"id": "job-id"}, headers={"host": "app.example.com"}
    )
    assert resp.headers["location"] == "http://app.example.com/jobs/job-id"


def test_forwarded_no_host_stays_relative(forwarded_client: TestClient) -> None:
    """Trusted but no host determinable (only a proto) → degrade to relative rather than
    emit a hostless absolute URL."""
    resp = forwarded_client.post(
        "/jobs", json={"id": "job-id"}, headers={"x-forwarded-proto": "https"}
    )
    assert resp.headers["location"] == "/jobs/job-id"


def test_forwarded_default_port_omitted(forwarded_client: TestClient) -> None:
    """A default port for the scheme is not appended to the authority."""
    resp = forwarded_client.post(
        "/jobs",
        json={"id": "job-id"},
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "host",
            "x-forwarded-port": "443",
        },
    )
    assert resp.headers["location"] == "https://host/jobs/job-id"


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
    with pytest.raises(TypeError, match="expects path of type JobPath, got WidgetPath"):
        Location.from_operation(JobsResource.read_one, path=WidgetPath(widget_id="widget-id"))


def test_location_literal_constructors_wire_targets() -> None:
    """The literal ``Location`` constructors wire the right target (resolution then mirrors
    the ``Link`` variants exercised through the app)."""
    assert isinstance(Location.from_url("https://x/y").target, URLTarget)
    assert isinstance(Location.from_path("/y").target, PathTarget)


def test_path_struct_subclass_is_rejected() -> None:
    """Exact match, not is-a: even a subclass of the operation's path struct is rejected."""
    with pytest.raises(TypeError, match="expects path of type JobPath, got _SubJobPath"):
        Location.from_operation(JobsResource.read_one, path=_SubJobPath(job_id="job-id"))


def test_missing_path_fails_at_construction() -> None:
    """An operation with path slots requires a path Struct."""
    with pytest.raises(TypeError, match="requires path of type JobPath"):
        Link.from_operation(JobsResource.read_one, rel="self")


def test_unexpected_path_fails_at_construction() -> None:
    """An operation with no path slots rejects a path Struct."""
    with pytest.raises(TypeError, match="takes no path params"):
        Location.from_operation(JobsResource.create, path=JobPath(job_id="job-id"))


def test_malformed_ref_fails_at_construction() -> None:
    """``from_ref`` needs a ``name.operation`` string."""
    with pytest.raises(TypeError, match="ref must be"):
        Link.from_ref("jobs", rel="self")


# --- error contracts: reverse-routing misconfigurations fail loud ---


class _SharedReadMixin:
    """A read_one shared by two resources; mounting both makes its reversal ambiguous."""

    async def read_one(self, path: JobPath) -> Job:
        """Return a job by id."""
        return Job(id=path.job_id)


class _JobsAtA(_SharedReadMixin, Resource, path="/a"):
    """First mount of the shared handler."""


class _JobsAtB(_SharedReadMixin, Resource, path="/b"):
    """Second mount of the shared handler."""


class _AmbiguousApp(BaseApp):
    """Mounts the same inherited handler at two paths, so its reversal is ambiguous."""

    async def _wire(self) -> None:
        self._include_resource(_JobsAtA())
        self._include_resource(_JobsAtB())


class _DupRefAEndpoint(Endpoint, path="/dup-a", ref="dup"):
    """First class claiming ref 'dup'."""

    async def get(self) -> Job:
        """Return a job."""
        return Job(id="job-id")


class _DupRefBEndpoint(Endpoint, path="/dup-b", ref="dup"):
    """Second class claiming the same ref 'dup'."""

    async def get(self) -> Job:
        """Return a job."""
        return Job(id="job-id")


class _DupRefApp(BaseApp):
    """Mounts two classes that claim the same ref."""

    async def _wire(self) -> None:
        self._include_endpoint(_DupRefAEndpoint())
        self._include_endpoint(_DupRefBEndpoint())


class _UnmountedJobs(Resource, path="/unmounted"):
    """A resource deliberately left out of the app — linked at, but never mounted."""

    async def read_one(self, path: JobPath) -> Job:
        """Return a job by id."""
        return Job(id=path.job_id)


class _DanglingOpEndpoint(Endpoint, path="/dangling-op"):
    """Links via from_operation to an operation whose class is never included."""

    async def get(self) -> JSONResponse[Job]:
        """Return a job carrying a link to an unmounted operation."""
        return JSONResponse(
            json=Job(id="job-id"),
            location=Location.from_operation(
                _UnmountedJobs.read_one, path=JobPath(job_id="job-id")
            ),
        )


class _DanglingRefEndpoint(Endpoint, path="/dangling-ref"):
    """Links via from_ref to a ref that no mounted class declares."""

    async def get(self) -> JSONResponse[Job]:
        """Return a job carrying a link to an unknown ref."""
        return JSONResponse(
            json=Job(id="job-id"),
            location=Location.from_ref("nope.read_one", path=JobPath(job_id="job-id")),
        )


class _DanglingApp(BaseApp):
    """Wires only the dangling-link endpoints, not their targets."""

    async def _wire(self) -> None:
        self._include_endpoint(_DanglingOpEndpoint())
        self._include_endpoint(_DanglingRefEndpoint())


def test_ambiguous_reverse_target_fails_at_wiring() -> None:
    """A handler shared (via a mixin) across two mounts can't be reversed unambiguously."""
    with pytest.raises(RuntimeError, match="ambiguous reverse target"):
        TestClient(_AmbiguousApp())


def test_duplicate_ref_fails_at_wiring() -> None:
    """Two classes can't claim the same ref."""
    with pytest.raises(RuntimeError, match="duplicate ref"):
        TestClient(_DupRefApp())


def test_link_to_unmounted_operation_fails_at_resolution() -> None:
    """A link to an operation whose class was never included fails when the response sends."""
    with TestClient(_DanglingApp()) as client, pytest.raises(WiringError, match="not a mounted"):
        client.get("/dangling-op")


def test_link_to_unknown_ref_fails_at_resolution() -> None:
    """A from_ref to a ref no class declares fails when the response sends."""
    with (
        TestClient(_DanglingApp()) as client,
        pytest.raises(WiringError, match="no mounted operation"),
    ):
        client.get("/dangling-ref")
