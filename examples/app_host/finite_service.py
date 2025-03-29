#!/usr/bin/env python

import time

from localpost.hosting import ServiceLifetimeManager
from localpost.hosting.app import App

host = App()


@host.service()
def a_sync_service(service_lifetime: ServiceLifetimeManager):
    print("Service started")
    service_lifetime.set_started()
    print("Service running")
    time.sleep(5)
    print("Service is done")
    # The host should also stop after this point, as all the services have stopped


if __name__ == "__main__":
    import logging
    import localpost

    logging.basicConfig()
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(host))
