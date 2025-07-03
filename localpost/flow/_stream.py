import dataclasses as dc
import math
from collections.abc import Callable, Collection
from contextlib import asynccontextmanager
from typing import Any, Generic

from anyio import EndOfStream, ClosedResourceError, create_task_group, move_on_after
from anyio.abc import ObjectReceiveStream
from typing_extensions import TypeVar

from localpost._utils import start_task_soon, EventView, Event
from ._flow import AsyncHandler, logger

T = TypeVar("T", default=Any)
TC = TypeVar("TC", bound=Collection[object], default=Collection[object])


@dc.dataclass(frozen=True, eq=False, slots=True)
class StreamConsumerState:
    closed: EventView


@asynccontextmanager
async def create_stream_consumer(
    stream: ObjectReceiveStream[T],
    h: AsyncHandler[T],
    /,
    *,
    concurrency: int = 1,
    process_leftovers: bool = True,  # FIXME Remove
):
    if math.isinf(concurrency):
        async def handle(item: T) -> None:
            # Infinite concurrency, just spawn a new task for each item
            tg.start_soon(h, item)
    else:
        handle = h

    async def source_items():
        try:
            while True:
                yield await stream.receive()
        except EndOfStream:
            logger.debug("Source stream has been completed, no more items to process")
        except ClosedResourceError:
            logger.debug("Receiver has been closed (according to consumer's process_leftovers setting)")
        finally:
            closed.set()

    async def consume():
        async for item in source_items():
            await handle(item)

    closed = Event()
    async with stream, create_task_group() as tg:
        for _ in range(1 if math.isinf(concurrency) else concurrency):
            start_task_soon(tg, consume)
        yield StreamConsumerState(closed)



@dc.dataclass(frozen=True, eq=False, slots=True)
class BatchReceiver(Generic[T, TC], ObjectReceiveStream[TC]):
    source: ObjectReceiveStream[T]
    batch_size: int
    batch_window: float
    items_f: Callable[[list[T]], TC] = lambda x: x

    def __post_init__(self):
        if self.batch_size < 1:
            raise ValueError("Batch size must be at least 1")
        if self.batch_window <= 0:
            raise ValueError("Batch window must be greater than 0 seconds")

    async def receive(self) -> TC:
        while True:
            items = []
            try:
                with move_on_after(self.batch_window):
                    while len(items) < self.batch_size:
                        message = await self.source.receive()
                        items.append(message)
                if not items:
                    continue
                return self.items_f(items)
            except EndOfStream:
                if items:
                    return self.items_f(items)  # Return the last batch first
                raise

    async def aclose(self) -> None:
        await self.source.aclose()
