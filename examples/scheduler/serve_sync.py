#!/usr/bin/env python

import time

from localpost.hosting import Host
from .app import scheduler


def main():
    # Scheduler in a _separate_ thread, _separate_ event loop
    with Host(scheduler).serve() as host:
        try:
            while True:
                time.sleep(1)
                print("Main thread is running...")
        except KeyboardInterrupt:
            # Scheduler service is the only one in the host, so after the scheduler is done, the host will stop too
            host.shutdown()

    print("Main thread is done, exiting the app")


if __name__ == "__main__":
    import logging

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    main()
