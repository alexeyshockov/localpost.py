from __future__ import annotations

import time

CHECK_TIMEOUT: float = 1.0
"""Timeout (seconds) for cancellation checks (e.g. in the server loop)."""


def _noop_check() -> None:
    """Default cancellation probe — a no-op.

    Pass ``anyio.from_thread.check_cancelled`` (or any custom callable) to a
    primitive's ``check_cancelled`` argument to opt in to cancellation.
    """


current_time = time.monotonic
