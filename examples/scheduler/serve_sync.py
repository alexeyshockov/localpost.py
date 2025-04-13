#!/usr/bin/env python

import time

from localpost.scheduler import Scheduler, delay, every

scheduler = Scheduler()


@scheduler.task(every("3s") // delay((0, 1)))
async def task1():
    print(f"Hello from {task1.task.name}!")


def main():
    # Start the scheduler in a _separate_ thread, with its own event loop
    with scheduler.serve():
        try:
            while True:
                time.sleep(1)
                print("Main thread is running...")
        except KeyboardInterrupt:
            scheduler.shutdown()

    print("Main thread is done, exiting the app")


if __name__ == "__main__":
    import logging

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    main()
