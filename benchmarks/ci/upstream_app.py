"""A real, API-key-protected local HTTP upstream, run on its own CPU."""

from msgspec import Struct

from benchmarks.ci.common import Product, Workload
from jero import AuthenticationRequiredError, BaseApp, Endpoint


class Headers(Struct):
    """Let missing credentials reach the explicit 401 check."""

    authorization: str | None = None


class Health(Struct):
    """Startup readiness, outside the measured requests."""

    status: str = "ok"


class HealthEndpoint(Endpoint, path="/health"):
    """Let the runner wait for successful upstream startup."""

    async def get(self) -> Health:
        """Return readiness without credentials."""
        return Health()


class DataEndpoint(Endpoint, path="/data"):
    """Check the API key on every upstream request."""

    async def get(self, headers: Headers) -> Product:
        """Return a nested product only for the correct bearer key."""
        if headers.authorization != f"Bearer {Workload.upstream_token()}":
            raise AuthenticationRequiredError()
        return Workload.product()


class App(BaseApp):
    """The supporting service is shared by both measured frameworks."""

    async def wire(self) -> None:
        """Mount only readiness and the authenticated data endpoint."""
        self._include_endpoint(HealthEndpoint())
        self._include_endpoint(DataEndpoint())


app = App()
