"""jero's idiomatic implementation of the two initial HTTP workloads."""

from dataclasses import dataclass
from uuid import UUID, uuid4

import ryjwt
from msgspec import Struct

from benchmarks.ci.common import Claims, Info, Movie, MovieIn, Nested, User, Workload
from jero import AuthenticationRequiredError, BaseApp, Endpoint, JSONResponse, Resource


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


class App(BaseApp):
    """Wire only the services needed by these workloads."""

    async def wire(self) -> None:
        """Mount the public info endpoint and authenticated movie resource."""
        self._include_endpoint(InfoEndpoint())
        self._include_resource(MoviesResource(), auth=JWTAuth(Workload.signing_key()))


app = App()
