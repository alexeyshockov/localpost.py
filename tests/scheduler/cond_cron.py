from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import anyio
import pytest
from croniter import croniter

# noinspection PyProtectedMember
from localpost._utils import Event
from localpost.scheduler import ScheduledTask
from localpost.scheduler.cond.cron import cron

pytestmark = pytest.mark.anyio

_real_anyio_sleep = anyio.sleep


async def test_cron_trigger():
    base = datetime(2022, 1, 1)
    schedule = croniter("*/5 * * * *", base)
    scheduled_task_tpl = cron(schedule)

    # https://docs.python.org/3/library/unittest.mock.html#where-to-patch
    # https://pytest-mock.readthedocs.io/en/latest/usage.html#where-to-patch
    with patch("anyio.sleep", new=AsyncMock(side_effect=_real_anyio_sleep)) as mock_sleep:
        shutting_down = Event()
        scheduled_task = Mock(ScheduledTask, shutting_down=shutting_down)
        trigger_factory = scheduled_task_tpl(scheduled_task)
        async with trigger_factory as trigger_events:
            # The first event should be available immediately, the next one in 5 minutes
            await trigger_events.__anext__()
            shutting_down.set()

        mock_sleep.assert_awaited_once_with(300.0)
