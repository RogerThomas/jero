"""FastAPI's idiomatic implementation of the same two HTTP workloads."""

from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import UUID, uuid4

import ryjwt
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

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


app = FastAPI()


@app.get("/info")
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


@app.post("/movies", status_code=201)
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
