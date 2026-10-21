"""The shared wire contract and local signing key for the benchmark apps."""

from time import time
from typing import Literal
from uuid import UUID

import ryjwt
from msgspec import Struct


class Nested(Struct):
    """The nested object in the info response."""

    level: int
    label: str


class Info(Struct, rename="camel"):
    """The eight-field info response, in the shared camelCase wire format."""

    id: str
    name: str
    count: int
    ratio: float
    active: bool
    tags: list[Literal["alpha", "beta", "gamma"]]
    created_at: str
    nested: Nested


class MovieIn(Struct):
    """The five input fields validated by both apps."""

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
    """The exact claims decoded by ryjwt in both apps."""

    sub: str
    name: str
    exp: int


class Workload:
    """Fixed, public test data; the key is only for these local benchmark services."""

    @staticmethod
    def signing_key() -> ryjwt.SecretKey:
        """Construct once per server process, outside the measured request path."""
        return ryjwt.SecretKey(bytes(range(32)), algorithms=["HS256"])

    @classmethod
    def token(cls) -> str:
        """Mint the same short-lived identity for both frameworks."""
        return cls.signing_key().encode(Claims(sub="user-1", name="name", exp=int(time()) + 3600))

    @staticmethod
    def movie() -> MovieIn:
        """The five-field JSON body shared by both frameworks."""
        return MovieIn(title="title", year=2026, director="director", genre="genre", rating=8.0)
