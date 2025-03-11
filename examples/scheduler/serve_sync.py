#!/usr/bin/env python

import time

from .app import scheduler


def main():
    from localpost.scheduler import serve

    # Scheduler in a _separate_ thread, _separate_ event loop
    with serve(scheduler) as scheduler_host:
        try:
            while True:
                time.sleep(1)
                print("Main thread is running...")
        except KeyboardInterrupt:
            # Scheduler service is the only one in the host, so after the scheduler is done, the host will stop too
            scheduler_host.shutdown()

    print("Main thread is done, exiting the app")


if __name__ == "__main__":
    main()
