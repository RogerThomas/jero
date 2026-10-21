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


class CPUCounter(Struct):
    """Worker-wide CPU and monotonic clocks sampled outside measured requests."""

    pid: int
    cpu_seconds: float
    wall_seconds: float


class Vendor(Struct):
    """The nested vendor in the upstream product."""

    id: str
    name: str


class Product(Struct, rename="camel"):
    """The complete upstream response decoded and serialized by each framework."""

    id: str
    title: str
    price: float
    in_stock: bool
    tags: list[str]
    rating: float
    description: str
    vendor: Vendor


class Profile(Struct):
    """The six database columns returned for the authenticated user."""

    id: str
    name: str
    email: str
    address: str
    city: str
    country: str


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

    @staticmethod
    def upstream_token() -> str:
        """A public benchmark credential checked by the local upstream."""
        return "benchmark-upstream-key"

    @staticmethod
    def product() -> Product:
        """The upstream's fixed, nested catalog response."""
        return Product(
            id="prod-1",
            title="Mechanical Keyboard",
            price=129.99,
            in_stock=True,
            tags=["electronics", "peripherals", "keyboard"],
            rating=4.7,
            description="A tactile mechanical keyboard with hot-swappable switches.",
            vendor=Vendor(id="vendor-9", name="KeyCo"),
        )

    @staticmethod
    def profile() -> Profile:
        """The row seeded before either framework starts."""
        return Profile(
            id="user-1",
            name="name",
            email="user-1@example.com",
            address="1 Benchmark Street",
            city="Dublin",
            country="Ireland",
        )
