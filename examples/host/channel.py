#!/usr/bin/env python

import anyio

from localpost.hosting.app import App
from localpost.scheduler import Scheduler, every
from localpost.scheduler import delay, take_first

channel_writer, channel_reader = anyio.create_memory_object_stream[str]()

scheduler = Scheduler()

host = App()
host.add_service(scheduler, name=scheduler.name)


# Does not hold the host alive, if the scheduler is done
@host.service(daemon=True)
async def print_channel():
    """
    A hosted service, to read the channel in the background.
    """
    async with channel_reader as channel:
        async for message in channel:
            print(f"Message received: {message}")


@scheduler.task(every("3s") // delay((1, 3)) // take_first(3))
async def scheduled_background_task():
    print("Scheduled work here, writing to the channel...")
    await channel_writer.send("hello from the background task!")


if __name__ == "__main__":
    import logging
    import localpost

    logging.basicConfig()
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(host))
