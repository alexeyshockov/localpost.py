from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, final, Generic

from anyio.streams.memory import MemoryObjectReceiveStream

from localpost.flow import AnyHandlerManager, ensure_async_handler_manager, AsyncHandlerManager
from localpost.flow._stream import stream_consumer as _stream_consumer
from localpost.hosting import ServiceLifetimeManager

T = TypeVar("T")

__all__ = ["StreamConsumer", "stream_consumer"]


@final
class StreamConsumer(Generic[T]):
    def __init__(
        self,
        handler: AsyncHandlerManager[T],
        reader: MemoryObjectReceiveStream[T],
        /,
        *,
        concurrency: int = 1,
        process_leftovers: bool = True,
    ):
        if concurrency < 1:
            raise ValueError("Number of consumers must be at least 1")

        self.handler = handler
        self.reader = reader
        self.concurrency = concurrency
        self.process_leftovers = process_leftovers

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        async with (
            self.handler as message_handler,
            _stream_consumer(
                self.reader,
                message_handler,
                self.concurrency,
                self.process_leftovers,
            ),
        ):
            service_lifetime.set_started()
            # Wait until the source channel is closed


def stream_consumer(
    reader: MemoryObjectReceiveStream[T], /, *, concurrency: int = 1, process_leftovers: bool = True,
) -> Callable[[AnyHandlerManager[T]], StreamConsumer[T]]:
    """ Decorator to create a stream consumer hosted service. """
    return lambda handler: StreamConsumer(
        ensure_async_handler_manager(handler),
        reader,
        concurrency=concurrency,
        process_leftovers=process_leftovers
    )
