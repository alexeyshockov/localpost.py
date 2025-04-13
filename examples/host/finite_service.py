#!/usr/bin/env python

import time

from localpost.hosting import ServiceLifetimeManager, hosted_service


@hosted_service
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
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(a_sync_service))
