import click
import pytest

from localpost.hosting import current_service, serve
from localpost.hosting.services.click import click_cmd

pytestmark = pytest.mark.anyio


async def test_success():
    ran: list[bool] = []

    @click.command()
    def ok():
        ran.append(True)

    async with serve(click_cmd(ok, args=[])) as lt:
        await lt.stopped

    assert ran == [True]
    assert lt.exit_code == 0


async def test_click_exception_maps_to_exit_code():
    @click.command()
    def boom():
        raise click.ClickException("boom")

    async with serve(click_cmd(boom, args=[])) as lt:
        await lt.stopped

    assert lt.exit_code == 1


async def test_usage_error_maps_to_exit_code_2():
    @click.command()
    def bad():
        raise click.UsageError("bad")

    async with serve(click_cmd(bad, args=[])) as lt:
        await lt.stopped

    assert lt.exit_code == 2


async def test_abort_maps_to_exit_code_1():
    @click.command()
    def aborted():
        raise click.exceptions.Abort()

    async with serve(click_cmd(aborted, args=[])) as lt:
        await lt.stopped

    assert lt.exit_code == 1


async def test_ctx_exit_propagates_exit_code():
    # In non-standalone mode ``ctx.exit(N)`` makes ``cmd.main()`` *return* N.
    @click.command()
    @click.pass_context
    def exit42(ctx: click.Context):
        ctx.exit(42)

    async with serve(click_cmd(exit42, args=[])) as lt:
        await lt.stopped

    assert lt.exit_code == 42


async def test_raw_sys_exit_propagates():
    # ``sys.exit`` from inside the command body bypasses Click and surfaces as
    # ``SystemExit`` — the wrapper still maps it onto the lifetime's exit code.
    import sys

    @click.command()
    def hard_exit():
        sys.exit(7)

    async with serve(click_cmd(hard_exit, args=[])) as lt:
        await lt.stopped

    assert lt.exit_code == 7


async def test_current_service_visible_inside_callback():
    captured: list[object] = []

    @click.command()
    def grab():
        captured.append(current_service())

    async with serve(click_cmd(grab, args=[])) as lt:
        await lt.stopped

    assert len(captured) == 1
    assert lt.exit_code == 0
