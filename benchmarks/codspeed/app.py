"""The app the CodSpeed benchmarks drive: one route per hot-path shape worth tracking."""

from dataclasses import dataclass

from msgspec import Struct

from benchmarks.codspeed.workloads import InfoEndpoint, JWTAuth, MoviesResource, Workload
from jero import (
    CORS,
    AuthenticationRequiredError,
    BaseApp,
    Created,
    Endpoint,
    JSONResponse,
    NotFoundError,
    Resource,
)


class Ok(Struct):
    """A tiny JSON body."""

    ok: bool


class ItemPath(Struct):
    """The item URL template slot."""

    item_id: str


class Page(Struct):
    """Query-string paging."""

    limit: int = 20
    offset: int = 0


class ItemIn(Struct, rename="camel"):
    """The validated JSON input of an item."""

    name: str
    price_cents: int
    tags: list[str]


class Item(Struct, rename="camel"):
    """An item on the wire."""

    id: str
    name: str
    price_cents: int
    tags: list[str]


class TraceHeaders(Struct):
    """Typed request and response headers."""

    x_trace_id: str


class Credentials(Struct):
    """The bearer credential, bound from the request headers."""

    authorization: str | None = None


class User(Struct):
    """The authenticated identity."""

    id: str


@dataclass
class TokenAuth:
    """A static bearer check, so the measured cost is jero's header binding and auth path."""

    _token: str

    def authenticate(self, headers: Credentials) -> User:
        """Accept exactly ``Bearer <token>``."""
        if headers.authorization != f"Bearer {self._token}":
            raise AuthenticationRequiredError()
        return User(id="user-1")


ITEMS = [Item(id=str(i), name=f"item-{i}", price_cents=i, tags=["a", "b"]) for i in range(50)]
BENCH_BEARER = "bench"
"""A fixed, public credential for the benchmark app only."""


class PingEndpoint(Endpoint, path="/ping"):
    """The cheapest possible route: a static path and a tiny body."""

    async def get(self) -> Ok:
        """Return a constant."""
        return Ok(ok=True)


class EchoHeadersEndpoint(Endpoint, path="/echo-headers"):
    """Typed request headers in, typed response headers out."""

    async def get(self, headers: TraceHeaders) -> JSONResponse[Ok, TraceHeaders]:
        """Echo the trace header back."""
        return JSONResponse(json=Ok(ok=True), headers=TraceHeaders(x_trace_id=headers.x_trace_id))


class ItemsResource(Resource, path="/items"):
    """CRUD with path, query, and JSON-body binding."""

    async def read_one(self, path: ItemPath, params: Page) -> Item:
        """Bind a path slot and a query string."""
        if path.item_id == "missing":
            raise NotFoundError()
        return Item(id=path.item_id, name="gizmo", price_cents=params.limit, tags=["a"])

    async def read_many(self, params: Page) -> list[Item]:
        """Serialize a list of Structs."""
        return ITEMS[params.offset : params.offset + params.limit]

    async def create(self, json: ItemIn) -> Created[Item]:
        """Decode and validate a JSON body, then echo it."""
        return Created(
            json=Item(id="new", name=json.name, price_cents=json.price_cents, tags=json.tags),
        )


class MeEndpoint(Endpoint, path="/me"):
    """An authenticated route."""

    async def get(self, user: User) -> User:
        """Return the authenticated user."""
        return user


class BenchApp(BaseApp):
    """Wire every benchmarked route, CORS, and the OpenAPI document."""

    async def wire(self) -> None:
        """Mount the routes."""
        self._include_endpoint(PingEndpoint())
        self._include_endpoint(EchoHeadersEndpoint())
        self._include_resource(ItemsResource(), cors=CORS(allow_origins=("https://example.com",)))
        self._include_endpoint(MeEndpoint(), auth=TokenAuth(BENCH_BEARER))
        self._include_openapi(title="Bench API", version="0.1.0")


class CIApp(BaseApp):
    """The info and JWT movie workloads, without OpenAPI."""

    async def wire(self) -> None:
        """Mount the two workload handlers and the JWT authenticator."""
        self._include_endpoint(InfoEndpoint())
        self._include_resource(MoviesResource(), auth=JWTAuth(Workload.signing_key()))
