"""FastAPI's idiomatic implementation of the same four HTTP workloads."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from os import getpid
from time import monotonic, process_time
from typing import Annotated, Literal
from uuid import UUID, uuid4

import ryjwt
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from pyreqwest.client import Client

from benchmarks.ci.backends import Settings, seed_database
from benchmarks.ci.common import Claims, Workload


class Nested(BaseModel):
    """The nested object in the info response."""

    level: int
    label: str


class Info(BaseModel):
    """The eight-field info response, in the shared camelCase wire format."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    name: str
    count: int
    ratio: float
    active: bool
    tags: list[Literal["alpha", "beta", "gamma"]]
    created_at: str
    nested: Nested


class MovieIn(BaseModel):
    """The five input fields validated by both apps."""

    title: str
    year: int
    director: str
    genre: str
    rating: float


class User(BaseModel):
    """The authenticated identity returned with a movie."""

    id: str
    name: str


class Movie(MovieIn):
    """The input echoed with a new UUID and authenticated user."""

    id: UUID
    user: User


class Vendor(BaseModel):
    """The nested vendor in the upstream product."""

    id: str
    name: str


class Product(BaseModel):
    """Validate all upstream fields before FastAPI's normal response serialization."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    title: str
    price: float
    in_stock: bool
    tags: list[str]
    rating: float
    description: str
    vendor: Vendor


class Profile(BaseModel):
    """The same six database columns as jero's response model."""

    id: str
    name: str
    email: str
    address: str
    city: str
    country: str


@dataclass
class Services:
    """Resources initialized by lifespan and passed through an async dependency."""

    client: Client
    pool: AsyncConnectionPool[AsyncConnection[Profile]]

    async def catalog(self) -> Product:
        """Decode and validate the real upstream HTTP response."""
        response = await self.client.get("/data").build().send()
        return Product.model_validate_json(bytes(await response.bytes()))

    async def profile(self, user_id: str) -> Profile:
        """Execute the same parameterized SELECT as the jero app."""
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT id, name, email, address, city, country FROM benchmark_users WHERE id = %s",
                (user_id,),
            )
            profile = await cursor.fetchone()
        if profile is None:
            raise HTTPException(status_code=404, detail="user not found")
        return profile


class CPUCounter(BaseModel):
    """Worker-wide CPU and monotonic clocks sampled outside measured requests."""

    pid: int
    cpu_seconds: float
    wall_seconds: float


async def get_cpu() -> CPUCounter:
    """Include CPU time from every thread in the sole worker process."""
    return CPUCounter(pid=getpid(), cpu_seconds=process_time(), wall_seconds=monotonic())


@dataclass(eq=False)
class JWTAuth:
    """An async dependency keeps authentication on the server's event loop."""

    _key: ryjwt.SecretKey = field(default_factory=Workload.signing_key)

    async def __call__(self, authorization: Annotated[str | None, Header()] = None) -> User:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="invalid token")
        try:
            claims = self._key.decode(authorization[7:], into=Claims)
        except ryjwt.InvalidTokenError as exc:
            raise HTTPException(status_code=401, detail="invalid token") from exc
        return User(id=claims.sub, name=claims.name)


async def get_info(response: Response) -> Info:
    """Return the shared info payload with a fresh response ID."""
    response.headers["x-response-id"] = str(uuid4())
    return Info(
        id="info-1",
        name="Benchmark Info",
        count=42,
        ratio=3.14159,
        active=True,
        tags=["alpha", "beta", "gamma"],
        created_at="2026-01-01T00:00:00Z",
        nested=Nested(level=2, label="nested"),
    )


async def create_movie(movie: MovieIn, user: Annotated[User, Depends(JWTAuth())]) -> Movie:
    """Return a new movie, using FastAPI's normal response validation."""
    return Movie(
        id=uuid4(),
        user=user,
        title=movie.title,
        year=movie.year,
        director=movie.director,
        genre=movie.genre,
        rating=movie.rating,
    )


def create_app() -> FastAPI:
    """Initialize shared resources through lifespan without untyped app.state access."""
    services: Services | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        nonlocal services
        settings = Settings.from_environment()
        await seed_database(settings)
        async with settings.client() as client, settings.pool(Profile) as pool:
            await pool.wait(timeout=15)
            services = Services(client, pool)
            try:
                yield
            finally:
                services = None

    async def get_services() -> Services:
        if services is None:
            raise RuntimeError("Benchmark services have not started")
        return services

    async def get_catalog(service: Annotated[Services, Depends(get_services)]) -> Product:
        return await service.catalog()

    async def get_profile(
        user: Annotated[User, Depends(JWTAuth())],
        service: Annotated[Services, Depends(get_services)],
    ) -> Profile:
        return await service.profile(user.id)

    application = FastAPI(lifespan=lifespan)
    application.get("/info")(get_info)
    application.post("/movies", status_code=201)(create_movie)
    application.get("/catalog")(get_catalog)
    application.get("/users/me")(get_profile)
    application.get("/_benchmark/cpu")(get_cpu)
    return application


app = create_app()
