#!yeet
import msgspec
from msgspec import Struct
from abc import ABC, abstractmethod
from dataclasses import dataclass
from types import get_original_bases
from typing import Any, ClassVar, get_args, get_origin

from rich import print


class Problem(Struct, omit_defaults=True, kw_only=True):
    type: str
    title: str
    status: int
    docs: str | None = None


class ParameterizedProblem[P: Struct](Problem, kw_only=True):
    detail: str
    params: P


class HTTPError(Exception):
    type: ClassVar[str]
    title: ClassVar[str]
    status: ClassVar[int]
    docs: ClassVar[str | None] = None

    def __init_subclass__(
        cls,
        *,
        type: str | None = None,
        title: str | None = None,
        status: int | None = None,
        docs: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if type is not None:
            cls.type = type
        if title is not None:
            cls.title = title
        if status is not None:
            cls.status = status
        if docs is not None:
            cls.docs = docs

    @property
    def problem(self) -> Problem:
        return Problem(type=self.type, title=self.title, status=self.status, docs=self.docs)


def _resolve_params_type(cls: type) -> type[Struct] | None:
    """Find the concrete params Struct from a ParameterizedHTTPError[...] base,
    walking the MRO so intermediate layers (e.g. DataclassHTTPError) resolve too."""
    for klass in cls.__mro__:
        for base in get_original_bases(klass):
            origin = get_origin(base)
            if isinstance(origin, type) and issubclass(origin, ParameterizedHTTPError):
                args = get_args(base)
                if args and isinstance(args[0], type):
                    return args[0]
    return None


class ParameterizedHTTPError[P: Struct](HTTPError):
    detail_template: ClassVar[str]
    params_type: ClassVar[type[Struct]]
    example_params: ClassVar[Struct]

    params: P
    detail: str

    def __init_subclass__(
        cls,
        *,
        detail_template: str | None = None,
        example_params: P | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if detail_template is not None:
            cls.detail_template = detail_template
        if example_params is not None:
            cls.example_params = example_params
        params_type = _resolve_params_type(cls)
        if params_type is not None:
            cls.params_type = params_type

    def __init__(self, params: P) -> None:
        # Escape hatch (Approach 2): construct directly from the params Struct.
        self._set_params(params)

    def _set_params(self, params: P) -> None:
        self.params = params
        # asdict uses the Python field names, matching detail_template.
        self.detail = self.detail_template.format(**msgspec.structs.asdict(params))

    @property
    def problem(self) -> ParameterizedProblem[P]:
        return ParameterizedProblem(
            type=self.type,
            title=self.title,
            status=self.status,
            docs=self.docs,
            detail=self.detail,
            params=self.params,
        )

    @classmethod
    def example_problem(cls) -> ParameterizedProblem[Struct]:
        detail = cls.detail_template.format(**msgspec.structs.asdict(cls.example_params))
        return ParameterizedProblem(
            type=cls.type,
            title=cls.title,
            status=cls.status,
            docs=cls.docs,
            detail=detail,
            params=cls.example_params,
        )

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        return msgspec.json.schema(cls.params_type)

    @classmethod
    def problem_schema(cls) -> dict[str, Any]:
        # Pull the params schema as a $ref with its $defs hoisted to the top level.
        (params_ref,), defs = msgspec.json.schema_components([cls.params_type])
        properties: dict[str, Any] = {
            "type": {"const": cls.type},
            "title": {"const": cls.title},
            "status": {"const": cls.status},
            "detail": {"type": "string"},
            "params": params_ref,
        }
        required = ["type", "title", "status", "detail", "params"]
        if cls.docs is not None:
            properties["docs"] = {"const": cls.docs}
            required.append("docs")
        return {
            "$defs": defs,
            "type": "object",
            "properties": properties,
            "required": required,
            "examples": [msgspec.to_builtins(cls.example_problem())],
        }


class DataclassHTTPError[P: Struct](ParameterizedHTTPError[P], ABC):
    """Convention (Approach 1): subclass as a @dataclass with typed fields and a
    small __post_init__ that builds the params Struct. The abstract __post_init__
    makes forgetting to wire params a static (pyright) error, and callers get an
    ergonomic keyword constructor without ever importing the params Struct."""

    @abstractmethod
    def __post_init__(self) -> None:
        # e.g. self._set_params(MyParams(field=self.field))
        ...


class InternalServerError(
    HTTPError,
    type="internal-server-error",
    title="Internal Server Error",
    status=500,
): ...


# Each parameterized error is a dataclass exposing ergonomic, typed fields and
# a small __post_init__ that builds its params Struct. The field<->params link
# is checked statically (rename a field and the Params(...) call stops type-
# checking), and the abstract __post_init__ makes forgetting to wire params a
# static error too. The call site only ever imports the error:
#
#     from app.errors import UserNotFoundError
#     raise UserNotFoundError(user_id=user.id)


class UserNotFoundErrorParams(Struct, rename="camel"):
    user_id: str


@dataclass
class UserNotFoundError(
    DataclassHTTPError[UserNotFoundErrorParams],
    type="user-not-found",
    title="User Not Found",
    status=404,
    docs="https://docs.example.com/errors/user-not-found",
    detail_template="User {user_id} does not exist.",
    example_params=UserNotFoundErrorParams(user_id="abcd"),
):
    user_id: str

    def __post_init__(self) -> None:
        self._set_params(UserNotFoundErrorParams(user_id=self.user_id))


# Escape hatch (Approach 2): subclass ParameterizedHTTPError directly and pass
# the params Struct at the raise site. No dataclass, no __post_init__. Worth it
# when the params have many fields — Approach 1 would force redeclaring every one
# on the dataclass; here the fields are declared exactly once, on the Struct.


class PaymentDeclinedErrorParams(Struct, rename="camel"):
    payment_id: str
    customer_id: str
    amount_cents: int
    currency: str
    decline_code: str
    decline_reason: str
    retry_after_seconds: int


class PaymentDeclinedError(
    ParameterizedHTTPError[PaymentDeclinedErrorParams],
    type="payment-declined",
    title="Payment Declined",
    status=402,
    detail_template=(
        "Payment {payment_id} for {amount_cents} {currency} was declined: "
        "{decline_reason} ({decline_code})."
    ),
    example_params=PaymentDeclinedErrorParams(
        payment_id="pay_abc",
        customer_id="cus_abc",
        amount_cents=4200,
        currency="USD",
        decline_code="insufficient_funds",
        decline_reason="Insufficient funds",
        retry_after_seconds=3600,
    ),
): ...


def main():
    try:
        raise UserNotFoundError(user_id="12345")
    except UserNotFoundError as e:
        print(f"Detail: {e.detail}")
        print(f"Problem: {e.problem}")
        print(f"Encoded: {msgspec.json.encode(e.problem).decode()}")

    try:
        raise PaymentDeclinedError(
            PaymentDeclinedErrorParams(
                payment_id="pay_999",
                customer_id="cus_999",
                amount_cents=15000,
                currency="GBP",
                decline_code="card_expired",
                decline_reason="Card has expired",
                retry_after_seconds=0,
            )
        )
    except PaymentDeclinedError as e:
        print(f"Detail: {e.detail}")
        print(f"Problem: {e.problem}")
        print(f"Encoded: {msgspec.json.encode(e.problem).decode()}")

    print(f"Problem schema: {PaymentDeclinedError.problem_schema()}")
