"""A JSON body that is one of several shapes: a union of tagged Structs, narrowed with
``match`` in the handler."""

from demo_app.models import Discount, FixedDiscount, PercentDiscount, PriceQuote
from jero import Endpoint


class PriceQuotesEndpoint(Endpoint, path="/price-quotes"):
    """Quotes a discounted price for either kind of discount."""

    async def post(self, json: Discount) -> PriceQuote:
        """Apply the discount the body's ``kind`` tag names."""
        match json:
            case FixedDiscount():
                price_cents = json.price_cents - json.discount_cents
            case PercentDiscount():
                price_cents = json.price_cents * (100 - json.percent_off) // 100
        return PriceQuote(price_cents=max(price_cents, 0))
