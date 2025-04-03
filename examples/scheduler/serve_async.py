#!/usr/bin/env python

import anyio

from localpost.hosting import Host
from .app import scheduler


async def main(duration: int):
    async with Host(scheduler).aserve():  # Scheduler in the same thread, same event loop
        for _ in range(duration):
            await anyio.sleep(1)
            print("Main thread is running...")
        print("Main thread is done, exiting the app")


if __name__ == "__main__":
    import logging

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    anyio.run(main, 10)
