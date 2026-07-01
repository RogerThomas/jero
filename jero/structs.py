"""``Struct`` — jero's ``msgspec.Struct`` base, extended to accept a ``meta=`` class
keyword that mirrors how ``Resource`` / ``Endpoint`` take their ``meta=``.

It is a faithful drop-in for ``msgspec.Struct`` (same fields, config keywords, encode/
decode/convert, ``isinstance`` checks) — the only addition is the optional ``meta=``,
which defaults to ``None``. Import this instead of ``msgspec.Struct`` and any model can
carry an OpenAPI description; a plain ``msgspec.Struct`` keeps working, just without one.

msgspec builds every ``Struct`` through its metaclass (``msgspec.StructMeta``), which
rejects unknown class keywords — so a plain ``class M(Struct, meta=...)`` fails. We
subclass that metaclass to intercept ``meta`` (storing it as ``__model_meta__``) before
msgspec sees it, then hand the rest of the keywords on for normal struct construction.
The base also *declares* ``meta`` on ``__init_subclass__`` purely so the static
type-checkers accept the keyword (they match class keywords against ``__init_subclass__``,
not the metaclass); at runtime the metaclass is what consumes it.
"""

from typing import Any, ClassVar

import msgspec
from msgspec import Struct as _Struct

from jero.openapi import ModelMeta


class _MetaCarrier(msgspec.StructMeta):
    """msgspec's struct metaclass, extended to capture a ``meta=`` class keyword."""

    def __new__(
        cls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        *,
        meta: ModelMeta | None = None,
        **kwargs: Any,  # noqa: ANN401  # only Any is assignable to msgspec's heterogeneous config kwargs
    ) -> type:
        # **kwargs forwards msgspec's own config (rename=, frozen=, …); its typed overload
        # can't be matched by **Any, hence the ignore. The result is a Struct class.
        struct_cls = super().__new__(cls, name, bases, namespace, **kwargs)  # pyright: ignore[reportArgumentType]  # pylint: disable=too-many-function-args  # msgspec's C-extension StructMeta is opaque to astroid
        if meta is not None:
            setattr(struct_cls, "__model_meta__", meta)  # noqa: B010  # dynamic on the freshly-built class
        return struct_cls


class Struct(_Struct, metaclass=_MetaCarrier):  # pylint: disable=invalid-metaclass  # _MetaCarrier subclasses msgspec's C-extension StructMeta, opaque to astroid
    """jero's ``Struct`` base. Use it instead of ``msgspec.Struct`` so a model may carry
    an OpenAPI description via the ``meta=`` class keyword:

    ``class Widget(Struct, meta=ModelMeta(description="A widget.")): ...``

    The ``meta`` is read by the OpenAPI generator (as ``__model_meta__``); a wire field
    named ``meta`` is unaffected (the class *keyword* and a *field* are different
    namespaces).
    """

    __model_meta__: ClassVar[ModelMeta | None] = None

    def __init_subclass__(cls, *, meta: ModelMeta | None = None, **kwargs: Any) -> None:  # noqa: ANN401
        # Declared so the static checkers accept the ``meta=`` class keyword; the metaclass
        # actually consumes it at runtime, so this never receives it.
        super().__init_subclass__()
