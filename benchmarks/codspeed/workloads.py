"""The info and JWT movie workloads: realistic CPU-bound handlers with fixed test data."""

from dataclasses import dataclass
from time import time
from typing import Literal
from uuid import UUID, uuid4

import ryjwt
from msgspec import Struct

from jero import AuthenticationRequiredError, Endpoint, JSONResponse, Resource


class Nested(Struct):
    """The nested object in the info response."""

    level: int
    label: str


class Info(Struct, rename="camel"):
    """The eight-field info response, in camelCase on the wire."""

    id: str
    name: str
    count: int
    ratio: float
    active: bool
    tags: list[Literal["alpha", "beta", "gamma"]]
    created_at: str
    nested: Nested


class MovieIn(Struct):
    """The five validated input fields."""

    title: str
    year: int
    director: str
    genre: str
    rating: float


class User(Struct):
    """The authenticated identity returned with a movie."""

    id: str
    name: str


class Movie(MovieIn):
    """The input echoed with a new UUID and authenticated user."""

    id: UUID
    user: User


class Claims(Struct):
    """The exact claims decoded by ryjwt."""

    sub: str
    name: str
    exp: int


class Workload:
    """Fixed, public test data; the key is only for these local benchmarks."""

    @staticmethod
    def signing_key() -> ryjwt.SecretKey:
        """Construct once per app, outside the measured request path."""
        return ryjwt.SecretKey(bytes(range(32)), algorithms=["HS256"])

    @classmethod
    def token(cls) -> str:
        """Mint a short-lived identity."""
        return cls.signing_key().encode(Claims(sub="user-1", name="name", exp=int(time()) + 3600))

    @staticmethod
    def movie() -> MovieIn:
        """The five-field JSON body."""
        return MovieIn(title="title", year=2026, director="director", genre="genre", rating=8.0)


class InfoHeaders(Struct):
    """The response ID generated for each request."""

    x_response_id: UUID


class InfoEndpoint(Endpoint, path="/info"):
    """Build and serialize medium JSON with a fresh response ID."""

    async def get(self) -> JSONResponse[Info, InfoHeaders]:
        """Return the info payload."""
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
    """Validate an HS256 bearer token with ryjwt."""

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
