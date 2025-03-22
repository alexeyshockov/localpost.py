from datetime import timedelta
from unittest.mock import patch, AsyncMock, Mock

import anyio
import pytest

from localpost._utils import _Event
from localpost.scheduler import every, ScheduledTask

pytestmark = pytest.mark.anyio

_real_anyio_sleep = anyio.sleep


async def test_every_trigger():
    scheduled_task_tpl = every(timedelta(seconds=5))

    # https://docs.python.org/3/library/unittest.mock.html#where-to-patch
    # https://pytest-mock.readthedocs.io/en/latest/usage.html#where-to-patch
    with patch("anyio.sleep", new=AsyncMock(side_effect=_real_anyio_sleep)) as mock_sleep:
        shutting_down = _Event()
        scheduled_task = Mock(ScheduledTask, shutting_down=shutting_down)
        trigger_factory = scheduled_task_tpl(scheduled_task)
        async with trigger_factory as trigger_events:
            # The first event should be available immediately, the next one in 5 minutes
            await trigger_events.__anext__()
            shutting_down.set()

        mock_sleep.assert_awaited_once_with(5.0)


async def test_after_trigger():
    pass  # TODO Implement
