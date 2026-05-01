#!/usr/bin/env python
import sys
import time

from localpost.hosting import ServiceLifetime, run_app, service


@service
def a_sync_service():
    def svc(lt: ServiceLifetime):
        print("Service started")
        lt.set_started()
        print("Service running")
        time.sleep(5)
        print("Service is done")
        # The host should also stop after this point, as all the services have stopped

    return svc


if __name__ == "__main__":
    import logging

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    sys.exit(run_app(a_sync_service()))
