"""jero's idiomatic implementation of the four HTTP workloads."""

from dataclasses import dataclass, field
from os import getpid
from time import monotonic, process_time
from uuid import UUID, uuid4

import msgspec
import ryjwt
from msgspec import Struct
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pyreqwest.client import Client

from benchmarks.ci.backends import Settings, seed_database
from benchmarks.ci.common import (
    Claims,
    CPUCounter,
    Info,
    Movie,
    MovieIn,
    Nested,
    Product,
    Profile,
    User,
    Workload,
)
from jero import (
    AuthenticationRequiredError,
    BaseApp,
    Endpoint,
    JSONResponse,
    NotFoundError,
    Resource,
)


class InfoHeaders(Struct):
    """The response ID generated for each request."""

    x_response_id: UUID


class InfoEndpoint(Endpoint, path="/info"):
    """Build and serialize medium JSON with a fresh response ID."""

    async def get(self) -> JSONResponse[Info, InfoHeaders]:
        """Return the shared info payload."""
        return JSONResponse(
            json=Info(
                id="info-1",
                name="Benchmark Info",
                count=42,
                ratio=3.14159,
                active=True,
                tags=["alpha", "beta", "gamma"],
                created_at="2026-01-01T00:00:00Z",
                nested=Nested(level=2, label="nested"),
            ),
            headers=InfoHeaders(x_response_id=uuid4()),
        )


class Credentials(Struct):
    """Optional binding lets the authenticator reject missing credentials with 401."""

    authorization: str | None = None


@dataclass
class JWTAuth:
    """Validate an HS256 bearer token with the PyPI ryjwt package."""

    _key: ryjwt.SecretKey

    def authenticate(self, headers: Credentials) -> User:
        """Reject missing, malformed, expired, or incorrectly signed credentials."""
        if headers.authorization is None or not headers.authorization.startswith("Bearer "):
            raise AuthenticationRequiredError()
        try:
            claims = self._key.decode(headers.authorization[7:], into=Claims)
        except ryjwt.InvalidTokenError as exc:
            raise AuthenticationRequiredError() from exc
        return User(id=claims.sub, name=claims.name)


class MoviesResource(Resource, path="/movies"):
    """Authenticate, validate the input, and serialize the created movie."""

    async def create(self, json: MovieIn, user: User) -> Movie:
        """Return a new movie with the authenticated identity."""
        return Movie(
            id=uuid4(),
            user=user,
            title=json.title,
            year=json.year,
            director=json.director,
            genre=json.genre,
            rating=json.rating,
        )


@dataclass
class CatalogEndpoint(Endpoint, path="/catalog"):
    """Fetch real HTTP bytes, validate the product, and serialize it again."""

    _client: Client
    _decoder: msgspec.json.Decoder[Product] = field(
        default_factory=lambda: msgspec.json.Decoder(Product)
    )

    async def get(self) -> Product:
        """Use the app-owned HTTP pool and the upstream's bearer credential."""
        response = await self._client.get("/data").build().send()
        return self._decoder.decode(await response.bytes())


@dataclass
class ProfileEndpoint(Endpoint, path="/users/me"):
    """Read a profile from PostgreSQL using the verified JWT subject."""

    _pool: AsyncConnectionPool[AsyncConnection[Profile]]

    async def get(self, user: User) -> Profile:
        """Execute a parameterized query for every request, without a response cache."""
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT id, name, email, address, city, country FROM benchmark_users WHERE id = %s",
                (user.id,),
            )
            profile = await cursor.fetchone()
        if profile is None:
            raise NotFoundError()
        return profile


class App(BaseApp):
    """Wire only the services needed by these workloads."""

    async def wire(self) -> None:
        """Own both connection pools and mount the four workloads."""
        settings = Settings.from_environment()
        await seed_database(settings)
        client = await self._aenter(settings.client())
        pool = await self._aenter(settings.pool(Profile))
        await pool.wait(timeout=15)
        auth = JWTAuth(Workload.signing_key())
        self._include_endpoint(InfoEndpoint())
        self._include_resource(MoviesResource(), auth=auth)
        self._include_endpoint(CatalogEndpoint(client))
        self._include_endpoint(ProfileEndpoint(pool), auth=auth)
        self._include_endpoint(CPUEndpoint())


class CPUEndpoint(Endpoint, path="/_benchmark/cpu"):
    """Measure the sole server worker without sampling during load."""

    async def get(self) -> CPUCounter:
        """Include CPU time from every thread in the worker process."""
        return CPUCounter(pid=getpid(), cpu_seconds=process_time(), wall_seconds=monotonic())


app = App()
