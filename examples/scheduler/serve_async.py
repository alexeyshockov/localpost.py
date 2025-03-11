#!/usr/bin/env python

import anyio

from .app import scheduler


async def main(duration: int):
    from localpost.scheduler import aserve

    # Scheduler in the same thread, same event loop
    async with aserve(scheduler):
        for _ in range(duration):
            await anyio.sleep(1)
            print("Main thread is running...")
        print("Main thread is done, exiting the app")


if __name__ == "__main__":
    anyio.run(main, 10)
