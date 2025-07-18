import dataclasses as dc
import math
from collections.abc import AsyncGenerator, Callable, Collection
from contextlib import asynccontextmanager
from typing import Any, Generic, cast, final

from anyio import ClosedResourceError, EndOfStream, create_task_group, move_on_after
from anyio.abc import ObjectReceiveStream
from typing_extensions import TypeVar

from localpost._utils import Event, EventView, start_task_soon

from ._flow import AsyncHandler, logger

T = TypeVar("T", default=Any)
TC = TypeVar("TC", bound=Collection[object], default=Collection[object])


@final
@dc.dataclass(frozen=True, eq=False, slots=True)
class StreamConsumerState:
    closed: EventView


@asynccontextmanager
async def create_stream_consumer(
    stream: ObjectReceiveStream[T],
    h: AsyncHandler[T],
    /,
    *,
    concurrency: int | float = 1,
) -> AsyncGenerator[StreamConsumerState, None]:
    async def schedule_handler(item: T) -> None:
        # Infinite concurrency, just spawn a new task for each item
        tg.start_soon(h, item)

    handle = schedule_handler if math.isinf(concurrency) else h

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
        for _ in range(1 if math.isinf(concurrency) else cast(int, concurrency)):
            start_task_soon(tg, consume)
        yield StreamConsumerState(closed)


@final
class BatchReceiver(Generic[T, TC], ObjectReceiveStream[TC]):
    def __init__(
        self,
        source: ObjectReceiveStream[T],
        /,
        *,
        batch_size: int,
        batch_window: float,
        items_f: Callable[[list[T]], TC] | None = None,
    ):
        if batch_size < 1:
            raise ValueError("Batch size must be at least 1")
        if batch_window <= 0:
            raise ValueError("Batch window must be greater than 0 seconds")

        self.source = source
        self.batch_size = batch_size
        self.batch_window = batch_window
        self.items_f: Callable[[list[T]], TC] = items_f if items_f is not None else lambda x: cast(TC, x)

    async def receive(self) -> TC:
        while True:
            items: list[T] = []
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
