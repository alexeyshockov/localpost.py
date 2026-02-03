import asyncio
import dataclasses as dc
import logging
import math
import sys
from collections.abc import Callable
from typing import Generic, TypeVar, cast

from anyio import ClosedResourceError, EndOfStream
from anyio.abc import ObjectReceiveStream

from localpost._utils import NOT_SET, Event, wait_any
from localpost.flow import AnyHandlerManager, ensure_async_handler
from localpost.flow._stream import create_stream_consumer
from localpost.hosting import ServiceLifetimeManager

if sys.version_info >= (3, 13):
    from asyncio import QueueShutDown
else:

    class QueueShutDown(Exception):
        pass


T = TypeVar("T")

__all__ = ["async_queue_consumer"]

logger = logging.getLogger(__name__)


@dc.dataclass(frozen=True, slots=True, eq=False)
class _AsyncQueueAdapter(ObjectReceiveStream[T]):
    source: asyncio.Queue[T]
    closed: Event = dc.field(default_factory=Event)

    async def receive(self) -> T:
        if self.closed:
            raise ClosedResourceError()
        source = self.source
        item = cast(T, NOT_SET)

        async def get_item() -> None:
            nonlocal item
            item = (await source.get()) if source.empty() else source.get_nowait()

        try:
            await wait_any(get_item, self.closed)
            if item is NOT_SET:
                raise ClosedResourceError()
            return item
        except QueueShutDown:
            raise EndOfStream() from None

    async def aclose(self) -> None:
        # Because it's not possible to close just the receiver end for asyncio.Queue
        self.closed.set()


class AsyncQueueConsumerService(Generic[T]):
    def __init__(
        self,
        target: asyncio.Queue[T],
        handler_m: AnyHandlerManager[T],
        /,
        *,
        concurrency: int | float,
        process_leftovers: bool,
        task_tracking: bool,
    ):
        if not (math.isinf(concurrency) or (isinstance(concurrency, int) and concurrency >= 1)):
            raise ValueError("Number of consumers must be at least 1")

        self.target = target
        self.handler_m = handler_m
        self.concurrency = concurrency
        self.process_leftovers = process_leftovers
        self.task_tracking = task_tracking

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        queue = self.target
        stream = _AsyncQueueAdapter(queue)
        async with self.handler_m as handler:
            handle_async = ensure_async_handler(handler, max_threads=self.concurrency)

            async def handle_and_ack(item: T) -> None:
                await handle_async(item)
                queue.task_done()

            async with create_stream_consumer(
                stream,
                handle_and_ack if self.task_tracking else handle_async,
                concurrency=self.concurrency,
            ) as consumer_state:
                service_lifetime.set_started()
                # TODO Timeout, if the queue is not closed at all
                if not self.process_leftovers:
                    await wait_any(service_lifetime.shutting_down, consumer_state.closed)
                    await stream.aclose()
                # Otherwise just wait for the stream to be exhausted and closed


def async_queue_consumer(
    target: asyncio.Queue[T],
    /,
    *,
    concurrency: int = 1,
    process_leftovers: bool = True,
    task_tracking: bool = True,
) -> Callable[[AnyHandlerManager[T]], AsyncQueueConsumerService[T]]:
    """Decorator to create an asyncio.Queue consumer hosted service."""
    return lambda handler_m: AsyncQueueConsumerService(
        target, handler_m, concurrency=concurrency, process_leftovers=process_leftovers, task_tracking=task_tracking
    )
