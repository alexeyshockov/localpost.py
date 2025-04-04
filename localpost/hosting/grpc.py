from typing import final

import grpc

from localpost._utils import wait_any

from ._host import ServiceLifetimeManager


@final
class AsyncGrpcService:
    def __init__(self, server: grpc.aio.Server):
        self._server = server
        self.name = "grpc"
        self.grace_termination_period = 5

    @property
    def server(self) -> grpc.aio.Server:
        return self._server

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        async def handle_svc_shutdown():
            await service_lifetime.shutting_down
            # During the grace period, the server won't accept new connections and allow existing RPCs to continue
            # within the grace period.
            await self._server.stop(self.grace_termination_period)

        await self._server.start()
        service_lifetime.set_started()
        await wait_any(handle_svc_shutdown, self._server.wait_for_termination)
