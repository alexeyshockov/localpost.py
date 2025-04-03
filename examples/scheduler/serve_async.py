#!/usr/bin/env python

import anyio

from .app import scheduler


async def main(duration: int):
    from localpost.hosting import Host

    host = Host(scheduler)
    async with host.aserve():  # Scheduler in the same thread, same event loop
        for _ in range(duration):
            await anyio.sleep(1)
            print("Main thread is running...")
        print("Main thread is done, exiting the app")


if __name__ == "__main__":
    anyio.run(main, 10)
