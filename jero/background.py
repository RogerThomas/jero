"""In-process, fire-and-forget background tasks.

Endpoints drop a ``Struct`` on a bounded queue; a single worker dispatches each item to
the handler registered for its type. Built in ``_wire`` via the factory and opened with
``_aenter`` (it is an async context manager), so the worker starts at startup and
drains/stops at shutdown — riding the app's existing exit stack.

Not durable: queued items live only in memory and are lost on crash or restart. For
must-run work, use a real broker.
"""

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Self, get_type_hints

from msgspec import Struct

from jero.core import WiringError

logger = logging.getLogger(__name__)


def _infer_item_type(handler: Callable[..., Awaitable[None]]) -> type[Struct]:
    """Infer the ``Struct`` type a handler accepts, from its single parameter.

    The signature is the contract (as with request binding and responses): a background
    handler takes exactly one argument, annotated with the ``Struct`` it processes.
    """
    name = getattr(handler, "__qualname__", repr(handler))
    params = list(inspect.signature(handler).parameters.values())
    if len(params) != 1:
        raise WiringError(
            f"BackgroundTasks: handler {name} must take exactly one argument "
            f"(the item Struct); got {len(params)}",
        )
    item_type = get_type_hints(handler).get(params[0].name)
    if not (isinstance(item_type, type) and issubclass(item_type, Struct)):
        raise WiringError(
            f"BackgroundTasks: handler {name} parameter {params[0].name!r} must be "
            f"annotated with a Struct subclass",
        )
    return item_type


class BackgroundTasks:
    """A queue of fire-and-forget background work, dispatched by item type.

    Register one handler per ``Struct`` type (inferred from the handler's parameter);
    endpoints call :meth:`add` to enqueue an item, and a single serial worker dispatches
    each to its handler. Open it with ``self._aenter`` inside ``_wire`` so the worker
    starts at startup and drains/stops at shutdown.

    Enter it *after* the resources its handlers use, so reverse-order shutdown drains the
    queue before those resources are torn down.
    """

    def __init__(
        self,
        *,
        maxsize: int = 1024,
        drain_timeout: float | None = 30.0,
        allow_one_to_many: bool = False,
    ) -> None:
        self._queue: asyncio.Queue[Struct] = asyncio.Queue(maxsize)
        self._drain_timeout = drain_timeout
        self._allow_one_to_many = allow_one_to_many
        self._handlers: dict[type[Struct], list[Callable[[Any], Awaitable[None]]]] = {}
        self._worker: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        """Pull items forever, dispatching each to its handler(s). Errors are isolated."""
        while True:
            item = await self._queue.get()
            try:
                handlers = self._handlers.get(type(item))
                if not handlers:
                    logger.error("background: no handler registered for %s", type(item).__name__)
                    continue
                for handler in handlers:
                    try:
                        await handler(item)
                    except Exception:  # pylint: disable=broad-exception-caught
                        logger.exception("background: handler failed for %s", type(item).__name__)
            finally:
                self._queue.task_done()

    def register[T: Struct](self, handler: Callable[[T], Awaitable[None]]) -> None:
        """Register a handler; its item type is inferred from its single parameter.

        One handler per type by default — a second for the same type is a ``WiringError``
        unless this was built with ``allow_one_to_many=True``.
        """
        item_type = _infer_item_type(handler)
        if item_type in self._handlers and not self._allow_one_to_many:
            raise WiringError(
                f"BackgroundTasks: a handler is already registered for {item_type.__name__!r}; "
                f"pass allow_one_to_many=True to register more than one",
            )
        self._handlers.setdefault(item_type, []).append(handler)

    async def add(self, item: Struct) -> None:
        """Enqueue an item for background processing (awaits if the queue is full)."""
        await self._queue.put(item)

    async def __aenter__(self) -> Self:
        self._worker = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._worker is None:
            return
        if self._drain_timeout is not None:
            try:
                await asyncio.wait_for(self._queue.join(), self._drain_timeout)
            except TimeoutError:
                logger.warning(
                    "background: drain timed out after %ss; dropping %d queued item(s)",
                    self._drain_timeout,
                    self._queue.qsize(),
                )
        self._worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._worker
