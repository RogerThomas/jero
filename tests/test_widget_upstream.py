"""Exercise the demo app's real HTTP client against a local upstream server."""

from collections import deque
from collections.abc import Callable, Generator
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import cast

import pytest
from msgspec import Struct
from pytest_mock import MockerFixture

from demo_app import DemoApp
from demo_app.config import Settings
from jero.testing import TestClient


class UpstreamRequest(Struct):
    """The request received by the upstream, including the original body bytes."""

    method: str
    path: str
    authorization: str | None
    content_type: str | None
    body: bytes


class UpstreamHandler(BaseHTTPRequestHandler):
    """Serve queued responses and record actual HTTP requests."""

    protocol_version = "HTTP/1.1"

    def _respond(self) -> None:
        server = cast(UpstreamServer, self.server)
        server.requests.append(
            UpstreamRequest(
                method=self.command,
                path=self.path,
                authorization=self.headers.get("Authorization"),
                content_type=self.headers.get("Content-Type"),
                body=self.rfile.read(int(self.headers.get("Content-Length", "0"))),
            )
        )
        status, body = server.responses.popleft()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        middle = len(body) // 2
        self.wfile.write(body[:middle])
        self.wfile.flush()
        self.wfile.write(body[middle:])

    def __getattr__(self, name: str) -> Callable[[], None]:
        """BaseHTTPRequestHandler discovers verb handlers by their do_<VERB> name."""
        if name in {"do_GET", "do_POST", "do_PUT", "do_PATCH", "do_DELETE"}:
            return self._respond
        raise AttributeError(name)


class UpstreamServer(ThreadingHTTPServer):
    """An ephemeral local upstream with observable HTTP traffic."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), UpstreamHandler)
        self.requests: list[UpstreamRequest] = []
        self.responses: deque[tuple[int, bytes]] = deque()

    @property
    def url(self) -> str:
        """The bound address, for the demo app's service configuration."""
        return f"http://127.0.0.1:{self.server_port}"


@pytest.fixture(name="upstream")
def _upstream() -> Generator[UpstreamServer]:
    with UpstreamServer() as server:
        thread = Thread(target=partial(server.serve_forever, poll_interval=0.01), daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture(name="upstream_client")
def _upstream_client(upstream: UpstreamServer, mocker: MockerFixture) -> Generator[TestClient]:
    mocker.patch(
        "demo_app.factory.get_settings",
        autospec=True,
        return_value=Settings(
            widget_api_key="api-key",
            openai_api_key="openai-api-key",
            widget_base_url=upstream.url,
        ),
    )
    with TestClient(DemoApp()) as client:
        yield client


def test_reads_use_the_real_upstream_client(
    upstream: UpstreamServer, upstream_client: TestClient
) -> None:
    """Repeated requests decode the complete upstream body and retain the typed shape."""
    upstream.responses += [
        (200, b'[{"id":"widget-id","name":"name","priceCents":1}]'),
        (200, b'{"id":"widget-id","name":"name","priceCents":1}'),
    ]

    collection = upstream_client.get(
        "/widgets", params={"limit": "2", "offset": "3"}, headers={"authorization": "Bearer token"}
    )
    item = upstream_client.get("/widgets/widget-id", headers={"authorization": "Bearer token"})

    assert collection.status_code == 200
    assert collection.json() == [{"id": "widget-id", "name": "name", "priceCents": 1}]
    assert item.status_code == 200
    assert item.json() == {"id": "widget-id", "name": "name", "priceCents": 1}
    assert upstream.requests == [
        UpstreamRequest("GET", "/widgets?limit=2&offset=3", "Bearer api-key", None, b""),
        UpstreamRequest("GET", "/widgets/widget-id", "Bearer api-key", None, b""),
    ]


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("POST", "/widgets", 201),
        ("PUT", "/widgets/widget-id", 200),
        ("PATCH", "/widgets/widget-id", 200),
    ],
)
def test_writes_send_msgspec_json_to_the_upstream(
    upstream: UpstreamServer,
    upstream_client: TestClient,
    method: str,
    path: str,
    status: int,
) -> None:
    """Write operations send a JSON body with upstream auth and decode the reply."""
    upstream.responses.append((status, b'{"id":"widget-id","name":"name","priceCents":1}'))

    response = upstream_client.request(
        method,
        path,
        json={"name": "name", "priceCents": 1},
        headers={"authorization": "Bearer token"},
    )

    assert response.status_code == status
    assert response.json() == {"id": "widget-id", "name": "name", "priceCents": 1}
    expected_body = b'{"name":"name","priceCents":1}'
    assert upstream.requests == [
        UpstreamRequest(method, path, "Bearer api-key", "application/json", expected_body)
    ]


def test_delete_accepts_an_empty_upstream_body(
    upstream: UpstreamServer, upstream_client: TestClient
) -> None:
    """A successful deletion has no JSON response body to decode upstream."""
    upstream.responses.append((204, b""))

    response = upstream_client.delete(
        "/widgets/widget-id", headers={"authorization": "Bearer token"}
    )

    assert response.status_code == 200
    assert response.json() == {"id": "widget-id", "deleted": True}
    assert upstream.requests == [
        UpstreamRequest("DELETE", "/widgets/widget-id", "Bearer api-key", None, b"")
    ]


@pytest.mark.parametrize(
    ("method", "path", "upstream_status", "body", "status", "problem"),
    [
        ("GET", "/widgets/widget-id", 404, b"", 404, "widget-not-found"),
        ("POST", "/widgets", 409, b"", 409, "conflict"),
        ("GET", "/widgets/widget-id", 503, b"unavailable", 503, "upstream-unavailable"),
        ("GET", "/widgets/widget-id", 200, b"", 502, "empty-upstream-response"),
    ],
)
def test_upstream_errors_keep_the_application_error_contract(
    upstream: UpstreamServer,
    upstream_client: TestClient,
    method: str,
    path: str,
    upstream_status: int,
    body: bytes,
    status: int,
    problem: str,
) -> None:
    """Upstream statuses reach the demo app's typed error mapping."""
    upstream.responses.append((upstream_status, body))

    response = upstream_client.request(
        method,
        path,
        json={"name": "name", "priceCents": 1} if method == "POST" else None,
        headers={"authorization": "Bearer token"},
    )

    assert response.status_code == status
    assert response.json()["type"] == problem
