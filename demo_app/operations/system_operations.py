"""Standalone endpoints: the authenticated identity probe, the optionally-authenticated
spotlight, health checks, a raw-form echo, and a cross-module ``from_ref`` link demo."""

from demo_app.models import Health, RawForm, RawFormHeaders, Spotlight, User, Widget, WidgetPath
from jero import Endpoint, EndpointMeta, JSONResponse, Link, NoContent, RawHeaders, Tag


class WhoAmIEndpoint(Endpoint, path="/me"):
    """Authenticated endpoint returning the current user."""

    async def get(self, user: User) -> User:
        """Return the authenticated caller."""
        return user


class SpotlightEndpoint(Endpoint, path="/spotlight"):
    """Optionally-authenticated endpoint: everyone gets the spotlight widget, and a caller
    who presented credentials gets it personalized.

    Mounted behind ``OptionalTokenAuth`` (see ``demo_app.app``), whose ``-> User | None``
    return is what makes this route serve anonymous callers. Credentials that are *present
    but invalid* never reach here — jero still answers 401.

    Also the demo of a **union return**: the annotation names two success statuses, and both
    are derived into the OpenAPI document (200 with a ``Spotlight`` body, 204 with none).
    """

    async def get(self, user: User | None) -> Spotlight | NoContent:
        """Return the spotlight widget, personalized when authenticated, or 204 when the
        caller may not see it."""
        if user is not None and not user.may_see_spotlight:
            # A real app would probably raise ForbiddenError here — 403 is what "you may
            # not see this" means. 204 stands in so this endpoint demonstrates a union
            # return; don't read it as advice on which status to pick.
            return NoContent()
        return Spotlight(
            widget_id="spotlight", personalized_for=user.name if user is not None else None
        )


class HealthEndpoint(
    Endpoint,
    path="/healthz",
    # defines the "system" group's description...
    meta=EndpointMeta(tags=[Tag("system", "Health checks and diagnostics.")]),
):
    """Unauthenticated health-check endpoint."""

    async def get(self) -> Health:
        """Return an ok health status."""
        return Health(status="ok")


class RawHealthEndpoint(
    Endpoint,
    path="/raw-healthz",
    meta=EndpointMeta(tags=["system"]),  # ...and this one just uses it by name (bare str)
):
    """Unauthenticated health-check endpoint returning raw JSON."""

    async def get(self) -> bytes:
        """Return an ok health status as raw JSON."""
        return b'{"status":"ok"}'


class RawFormEndpoint(Endpoint, path="/form-raw-headers"):
    """Unauthenticated endpoint echoing request and form-part raw headers."""

    async def post(self, form: RawForm, raw_headers: RawHeaders) -> RawFormHeaders:
        """Return the raw request headers and the raw headers on the blob part."""
        return RawFormHeaders(
            request_header_names=raw_headers.keys(),
            part_header_names=form.blob.raw_headers.keys(),
            part_checksum_values=form.blob.raw_headers.getlist("x-checksum"),
            part_content_type=form.blob.content_type,
            part_typed_headers=form.blob.headers is not None,
        )


class FeaturedWidgetEndpoint(Endpoint, path="/featured-widget"):
    """Returns a widget carrying a cross-module ``Link`` resolved through the widgets ``ref``
    — the string hatch for when importing ``WidgetResource`` would form an import cycle."""

    async def get(self) -> JSONResponse[Widget]:
        """Return the featured widget with a link to its canonical URL via ``from_ref``."""
        widget = Widget(id="featured", name="featured-widget", price_cents=0)
        return JSONResponse(
            json=widget,
            links=[
                Link.from_ref(
                    "widgets.read_one", rel="related", path=WidgetPath(widget_id=widget.id)
                )
            ],
        )
