import anyio
import pytest

# noinspection PyProtectedMember
from localpost._utils import Event, EventViewProxy, cancellable_from, wait_all, wait_any

pytestmark = pytest.mark.anyio


async def test_event_view_proxy():
    proxy = EventViewProxy()
    assert not proxy.is_set()

    event = anyio.Event()  # Real event
    proxy.resolve(event)

    assert not proxy.is_set()
    event.set()
    assert proxy.is_set()


async def test_wait_all():
    event1 = Event()
    event2 = Event()

    async def set_soon(event, delay):
        await anyio.sleep(delay)
        event.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(set_soon, event1, 0.1)
        tg.start_soon(set_soon, event2, 0.3)
        await wait_all([event1, event2])
        tg.cancel_scope.cancel()

    assert event1.is_set() and event2.is_set()
    assert tg.cancel_scope.cancel_called


async def test_wait_any():
    events = [anyio.Event() for _ in range(3)]

    async def set_first_event():
        await anyio.sleep(0.1)
        events[0].set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(set_first_event)
        await wait_any(*events)

        # Only first event should be set
        assert events[0].is_set()
        assert not any(event.is_set() for event in events[1:])


async def test_cancellable_from():
    event = Event()

    @cancellable_from(event)
    async def test_func():
        await anyio.sleep_forever()

    async with anyio.create_task_group() as tg:
        tg.start_soon(test_func)
        await anyio.sleep(0.1)
        event.set()
        await anyio.sleep(0.1)

    assert not tg.cancel_scope.cancel_called


async def test_wait_any_from():
    event1 = Event()
    event2 = Event()

    async def stop_soon():
        await anyio.sleep(0.1)
        event2.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(stop_soon)
        await wait_any(event1, event2, anyio.sleep_forever)

    assert event2.is_set()
    assert not event1.is_set()
