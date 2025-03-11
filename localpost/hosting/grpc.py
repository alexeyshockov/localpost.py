from typing import final

import grpc

from localpost._utils import wait_any

from ._host import ServiceLifetimeManager


@final
class AsyncGrpcService:
    def __init__(self, server: grpc.aio.Server):
        self.server = server

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        await self.server.start()
        service_lifetime.set_started()
        await wait_any(service_lifetime.shutting_down, self.server.wait_for_termination)
        # During the grace period, the server won't accept new connections and allow existing RPCs to continue within
        # the grace period.
        await self.server.stop(service_lifetime.shutdown_timeout)
