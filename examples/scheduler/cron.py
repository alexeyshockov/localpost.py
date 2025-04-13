#!/usr/bin/env python

import logging

from localpost.scheduler import delay, scheduled_task
from localpost.scheduler.cond.cron import cron


# Runs every 1 minute, with a random delay (jitter)
@scheduled_task(cron("*/1 * * * *") // delay((0, 10)))
async def cron_job():
    print("cron task triggered!")


if __name__ == "__main__":
    import localpost

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(cron_job))
