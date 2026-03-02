import grpc

from localpost.hosting._host import ServiceLifetime, service


@service
def grpc_async_server(server: grpc.aio.Server, /, *, grace_termination_period: float | None = 5.0):
    async def run(sl: ServiceLifetime):
        await server.start()
        sl.set_started()
        await sl.shutting_down
        # During the grace period, the server won't accept new connections and allow existing RPCs to continue
        # within the grace period.
        await server.stop(grace_termination_period)

    return run
