from __future__ import annotations

import logging
import signal

import anyio

from localpost.http.config import WorkerConfig
from localpost.http.worker import http_server
from ._benchmark_app import app


def _flask_app_bench():
    logging.basicConfig(level=logging.DEBUG)

    async def _run():
        async with http_server(app, WorkerConfig(max_requests=100)) as w:
            with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                async for _ in signals:
                    w.shutdown()
                    break

    # noinspection PyTypeChecker
    anyio.run(_run)


if __name__ == "__main__":
    _flask_app_bench()
