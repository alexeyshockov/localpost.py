from __future__ import annotations

from typing import TypeVar, final

from anyio.streams.memory import MemoryObjectReceiveStream

from localpost.flow import AsyncHandlerManager
from localpost.flow._flow import stream_consumer
from localpost.hosting import ServiceLifetimeManager

T = TypeVar("T")

__all__ = ["StreamConsumer"]


@final
class StreamConsumer:
    def __init__(
        self,
        handler: AsyncHandlerManager[T],
        reader: MemoryObjectReceiveStream[T],
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
            stream_consumer(
                self.reader,
                message_handler,
                self.concurrency,
                self.process_leftovers,
            ),
        ):
            service_lifetime.set_started()
            # Wait until the source channel is closed
