#!/usr/bin/env python

import time

from anyio.from_thread import start_blocking_portal

from localpost.hosting import serve
from localpost.scheduler import Scheduler, delay, every

scheduler = Scheduler()


@scheduler.task(every("3s") // delay((0, 1)))
async def task1():
    print(f"Hello from {task1.task.name}!")


def main():
    # Drive the async hosting layer from sync code: portal runs the event loop
    # in a background thread; ``wrap_async_context_manager`` exposes
    # ``serve(scheduler)`` as a regular ``with`` block. ``lt.shutdown()`` is
    # already cross-thread safe.
    with start_blocking_portal() as portal:
        with portal.wrap_async_context_manager(serve(scheduler)) as lt:
            try:
                while True:
                    time.sleep(1)
                    print("Main thread is running...")
            except KeyboardInterrupt:
                pass
            finally:
                lt.shutdown()

    print("Main thread is done, exiting the app")


if __name__ == "__main__":
    import logging

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    main()
