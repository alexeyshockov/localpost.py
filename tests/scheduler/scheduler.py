import random
from datetime import timedelta

import anyio
import pytest
from anyio import fail_after

from localpost.hosting import Host
from localpost.scheduler import Scheduler, every, scheduled_task

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_task_decorator():
    results = []

    @scheduled_task(every(timedelta(seconds=1)))
    def sample_task():
        results.append(random.randint(0, 10))

    assert callable(sample_task)

    host = Host(sample_task)
    async with host.aserve():
        await anyio.sleep(0.5)  # "App is working"
        host.shutdown()

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

    async with scheduler.aserve():
        await anyio.sleep(0.5)  # "App is working"
        scheduler.shutdown()

    assert len(results) == 2  # Only one initial run for each task
    assert "task1 result" in results
    assert "task2 result" in results


# An empty scheduler (without any tasks) should complete immediately without errors
async def test_empty_scheduler():
    scheduler = Scheduler()

    with fail_after(1):
        async with scheduler.aserve():
            pass
