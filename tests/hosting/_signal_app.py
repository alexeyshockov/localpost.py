"""Subprocess entrypoint for signal-handling tests.

Boots a tiny long-running service under ``run_app`` (which wires
``shutdown_on_signal``) and prints a marker on shutdown so the parent test
can verify it observed the signal cleanly.
"""

from __future__ import annotations

import sys

from localpost.hosting import ServiceLifetime, run_app


async def long_running(lt: ServiceLifetime) -> None:
    print("READY", flush=True)  # noqa: T201
    lt.set_started()
    await lt.shutting_down.wait()
    print("STOPPED", flush=True)  # noqa: T201


if __name__ == "__main__":
    sys.exit(run_app(long_running))
