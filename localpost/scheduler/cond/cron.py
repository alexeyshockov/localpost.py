import dataclasses as dc
from datetime import timedelta
from typing import final

from croniter import croniter

from .._cond import wait_trigger
from .._scheduler import ScheduledTask, ScheduledTaskTemplate, Trigger

__all__ = ["cron"]


@final
@dc.dataclass()
class Cron:
    schedule: croniter

    def __repr__(self):
        schedule_expr = " ".join(self.schedule.expressions)
        return f"cron({schedule_expr!r})"

    def __call__(self, task: ScheduledTask) -> Trigger[None]:
        def intervals():
            schedule = self.schedule  # Clone, to be reproducible?..
            while True:
                cur = schedule.cur
                # get_next() mutates the iterator (schedule object)
                yield timedelta(seconds=schedule.get_next() - cur)

        return wait_trigger(intervals(), task.shutting_down)


def cron(schedule: str | croniter, /) -> ScheduledTaskTemplate[None]:
    """
    Trigger events according to the cron schedule.
    """
    return ScheduledTaskTemplate(Cron(croniter(schedule) if isinstance(schedule, str) else schedule))
