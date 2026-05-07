import random
from datetime import timedelta

import anyio
import pytest
from anyio import fail_after

from localpost.hosting import serve
from localpost.scheduler import Scheduler, after, every, scheduled_task, take_first

pytestmark = pytest.mark.anyio


async def test_task_decorator():
    results = []
    scheduler = Scheduler()

    @scheduler.task(every(timedelta(seconds=1)))
    def sample_task():
        results.append(random.randint(0, 10))

    assert callable(sample_task)

    async with serve(scheduler) as lt:
        await anyio.sleep(0.5)  # "App is working"
        lt.shutdown()

    assert len(results) == 1  # Only one initial run


async def test_scheduler_tasks():
    results = []
    scheduler = Scheduler()

    @scheduler.task(every(timedelta(seconds=1)))
    def sample_task1():
        results.append("task1 result")

    @scheduler.task(every(timedelta(seconds=1)))
    def sample_task2():
        results.append("task2 result")

    assert callable(sample_task1)

    async with serve(scheduler) as lt:
        await anyio.sleep(0.5)  # "App is working"
        lt.shutdown()

    assert len(results) == 2  # Only one initial run for each task
    assert "task1 result" in results
    assert "task2 result" in results


# An empty scheduler (without any tasks) should complete immediately without errors
async def test_empty_scheduler():
    scheduler = Scheduler()

    with fail_after(1):
        async with serve(scheduler):
            pass


async def test_single_scheduled_task():
    """A single ``@scheduled_task``-decorated function is itself a ServiceF."""
    results = []

    @scheduled_task(every(timedelta(seconds=1)))
    def sample_task():
        results.append(1)

    async with serve(sample_task) as lt:
        await anyio.sleep(0.5)
        lt.shutdown()

    assert len(results) == 1


async def test_handler_failure_does_not_kill_loop():
    """A raising handler must be logged and published as Result.failure, but the schedule loop must keep firing."""
    runs = 0

    @scheduled_task(every(timedelta(seconds=0.05)) // take_first(3))
    def flaky():
        nonlocal runs
        runs += 1
        raise RuntimeError("boom")

    with fail_after(2):
        async with serve(flaky) as lt:
            await lt.stopped  # ``take_first(3)`` makes the service stop on its own

    assert runs == 3


async def test_after_trigger_chains_results():
    """``after(task1)`` fires task2 with task1's return value on every successful run."""
    scheduler = Scheduler()
    seen: list[int] = []

    @scheduler.task(every(timedelta(seconds=0.05)) // take_first(3))
    def producer() -> int:
        return 42

    @scheduler.task(after(producer))
    def _consumer(value: int) -> None:
        seen.append(value)

    with fail_after(2):
        async with serve(scheduler) as lt:
            await lt.stopped  # producer stops after 3 firings → consumer sees EndOfStream → scheduler stops

    assert seen == [42, 42, 42]


async def test_after_trigger_skips_failures():
    """``after(task1)`` only fires when task1 succeeds; failures are dropped (``after_all`` would see them)."""
    scheduler = Scheduler()
    seen: list[int] = []
    n = 0

    @scheduler.task(every(timedelta(seconds=0.05)) // take_first(4))
    def flaky() -> int:
        nonlocal n
        n += 1
        if n % 2 == 0:
            raise RuntimeError("even runs fail")
        return n

    @scheduler.task(after(flaky))
    def _consumer(value: int) -> None:
        seen.append(value)

    with fail_after(2):
        async with serve(scheduler) as lt:
            await lt.stopped

    # n = 1, 3 succeed; n = 2, 4 raise. Only odd values reach ``after``.
    assert seen == [1, 3]
