import anyio
import pytest

from localpost.hosting import ServiceLifetime, ServiceLifetimeView, run, serve, Starting, Running, ShuttingDown, Stopped

pytestmark = pytest.mark.anyio


async def test_serve_and_state_transitions():
    """Test the full lifecycle: starting → running → shutting_down → stopped."""
    states_seen = []

    async def test_service(lt: ServiceLifetime):
        states_seen.append(type(lt.state))
        lt.set_started()
        states_seen.append(type(lt.state))
        await lt.shutting_down.wait()
        states_seen.append(type(lt.state))

    async with serve(test_service) as lt:
        await lt.started
        assert isinstance(lt.state, Running)
        lt.shutdown()
        await lt.stopped

    assert isinstance(lt.state, Stopped)
    assert states_seen == [Starting, Running, ShuttingDown]


async def test_serve_yields_lifetime_view():
    async def test_service(lt: ServiceLifetime):
        lt.set_started()
        await lt.shutting_down.wait()

    async with serve(test_service) as lt:
        assert isinstance(lt, ServiceLifetimeView)
        lt.shutdown()
        await lt.stopped


async def test_exit_code_on_error():
    async def failing_service(lt: ServiceLifetime):
        lt.set_started()
        raise Exception("Test error")

    async with serve(failing_service) as lt:
        await lt.stopped

    assert lt.exit_code == 1


async def test_exit_code_on_success():
    async def ok_service(lt: ServiceLifetime):
        lt.set_started()

    async with serve(ok_service) as lt:
        await lt.stopped

    assert lt.exit_code == 0


async def test_run():
    async def finite_service(lt: ServiceLifetime):
        lt.set_started()
        await anyio.sleep(0.05)

    exit_code = await run(finite_service)
    assert exit_code == 0


async def test_run_failing():
    async def failing_service(lt: ServiceLifetime):
        lt.set_started()
        raise Exception("boom")

    exit_code = await run(failing_service)
    assert exit_code == 1


async def test_shutdown():
    shutdown_communicated = False

    async def test_service(lt: ServiceLifetime):
        lt.set_started()
        await lt.shutting_down.wait()
        nonlocal shutdown_communicated
        shutdown_communicated = True

    async with serve(test_service) as lt:
        await lt.started
        assert not lt.shutting_down
        lt.shutdown(reason="Test shutdown")
        await lt.stopped

    assert shutdown_communicated
    assert isinstance(lt.state, Stopped)


async def test_child_service():
    child_started = False
    child_stopped = False

    async def child(lt: ServiceLifetime):
        nonlocal child_started, child_stopped
        lt.set_started()
        child_started = True
        await lt.shutting_down.wait()
        child_stopped = True

    async def parent(lt: ServiceLifetime):
        child_lt = lt.start(child)
        await child_lt.started
        lt.set_started()
        await lt.shutting_down.wait()
        child_lt.shutdown()
        await child_lt.stopped

    async with serve(parent) as lt:
        await lt.started
        assert child_started
        lt.shutdown()
        await lt.stopped

    assert child_stopped
