#!/usr/bin/env python

import anyio

from localpost.hosting import Host, hosted_service
from localpost.scheduler import delay, take_first
from localpost.scheduler import every, scheduled_task

channel_writer, channel_reader = anyio.create_memory_object_stream[str]()


@hosted_service
async def print_channel():
    """
    A hosted service, to read the channel in the background.
    """
    async with channel_reader as channel:
        async for message in channel:
            print(f"Message received: {message}")


@scheduled_task(every("3s") // delay((1, 3)) // take_first(3))
async def scheduled_background_task():
    print("Scheduled work here, writing to the channel...")
    await channel_writer.send("hello from the background task!")


# Stop printing after the scheduler is done
host = Host(print_channel >> scheduled_background_task)


if __name__ == "__main__":
    import logging
    import localpost

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(host))
