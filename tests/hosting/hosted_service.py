import anyio
import pytest

from localpost.hosting import ServiceLifetime, service, serve

pytestmark = pytest.mark.anyio


async def test_service_decorator_async():
    @service
    def my_service():
        async def svc(lt: ServiceLifetime):
            lt.set_started()
            await lt.shutting_down.wait()

        return svc

    resolved = my_service()
    async with serve(resolved) as lt:
        await lt.started
        lt.shutdown()
        await lt.stopped

    assert lt.exit_code == 0


async def test_service_decorator_sync():
    work_done = False

    @service
    def my_sync_service():
        def svc(lt: ServiceLifetime):
            nonlocal work_done
            lt.set_started()
            lt.view.wait_shutting_down()
            work_done = True

        return svc

    resolved = my_sync_service()
    async with serve(resolved) as lt:
        await lt.started
        lt.shutdown()
        await lt.stopped

    assert work_done


async def test_service_decorator_context_manager():
    entered = False
    exited = False

    @service
    async def my_cm_service():
        nonlocal entered, exited
        entered = True
        yield
        exited = True

    resolved = my_cm_service()
    async with serve(resolved) as lt:
        await lt.started
        assert entered
        lt.shutdown()
        await lt.stopped

    assert exited


async def test_service_as_context_manager():
    """_ResolvedService can be used as an async context manager directly."""

    @service
    def my_service():
        async def svc(lt: ServiceLifetime):
            lt.set_started()
            await lt.shutting_down.wait()

        return svc

    # When used inside another service context
    async def parent(lt: ServiceLifetime):
        async with my_service() as child_lt:
            lt.set_started()
            await lt.shutting_down.wait()
            child_lt.shutdown()
            await child_lt.stopped

    async with serve(parent) as lt:
        await lt.started
        lt.shutdown()
        await lt.stopped
