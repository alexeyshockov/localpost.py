"""Run a Click command (or group) as a hosted service.

The command runs once with ``standalone_mode=False`` so its outcome maps onto
the hosting layer's exit code instead of calling ``sys.exit`` directly. Click's
own exception types are translated; anything else propagates and surfaces as a
non-zero exit code.

The Click callback executes synchronously in a worker thread (the ``@service``
decorator handles the sync-to-async offload). Inside the callback,
``localpost.hosting.current_service()`` returns this service's lifetime view —
use ``shutting_down`` for a non-blocking poll, or ``wait_shutting_down()`` to
block from sync code. ``await ...wait()`` will not work here because the
callback is sync.

When the command returns, only this service stops; sibling services composed
via ``run_app`` keep running until they finish or shut down on their own.
"""

from collections.abc import Sequence

import click

from localpost.hosting._host import ServiceLifetime, service


@service
def click_cmd(
    cmd: click.Command,
    *,
    args: Sequence[str] | None = None,
    prog_name: str | None = None,
):
    def run(lt: ServiceLifetime) -> None:
        lt.set_started()
        try:
            # In non-standalone mode, ``ctx.exit(code)`` causes ``main()`` to
            # *return* the integer exit code instead of raising — mirror Click's
            # own ``sys.exit(rv if isinstance(rv, int) else 0)``.
            rv = cmd.main(
                args=list(args) if args is not None else None,
                prog_name=prog_name,
                standalone_mode=False,
            )
        except click.ClickException as e:
            e.show()
            lt.exit_code = e.exit_code
        except click.exceptions.Abort:
            lt.exit_code = 1
        except SystemExit as e:
            code = e.code
            lt.exit_code = code if isinstance(code, int) else (1 if code else 0)
        else:
            if isinstance(rv, int):
                lt.exit_code = rv

    return run
