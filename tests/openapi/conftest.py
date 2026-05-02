"""Test fixtures for ``localpost.openapi`` integration tests.

Tests use a synchronous in-thread server (no worker pool, no anyio host)
to keep the harness small. The operation handlers run on the selector
thread; this is fine for tests since the handlers are short and
non-blocking.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator

import pytest

from localpost.http import RequestHandler, ServerConfig, start_http_server
from localpost.openapi import HttpApp


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerCM:
    def __init__(self, config: ServerConfig, handler: RequestHandler) -> None:
        self._stop = threading.Event()
        self._cm = start_http_server(config, handler)
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def __enter__(self) -> int:
        server = self._cm.__enter__()
        stop = self._stop

        def loop() -> None:
            while not stop.is_set():
                try:
                    server.run(timeout=0.05)
                except OSError:
                    return
                except BaseException as e:  # noqa: BLE001
                    self._error = e
                    return

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return server.port

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        try:
            t = self._thread
            assert t is not None
            t.join(timeout=5)
        finally:
            self._cm.__exit__(exc_type, exc, tb)
        if exc_type is None and self._error is not None:
            raise self._error


ServeApp = Callable[[HttpApp], _ServerCM]


@pytest.fixture
def serve_app() -> Iterator[ServeApp]:
    """Spin up an :class:`HttpApp` on a random local port. Use as ``with``::

    def test_x(serve_app):
        app = HttpApp()
        ...
        with serve_app(app) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/...")
    """
    active: list[_ServerCM] = []

    def make(app: HttpApp) -> _ServerCM:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        config = ServerConfig(host="127.0.0.1", port=port)
        handler = app._build_router_handler()
        cm = _ServerCM(config, handler)
        active.append(cm)
        return cm

    yield make

    for cm in active:
        t = cm._thread
        if t is not None and t.is_alive():
            cm._stop.set()
            t.join(timeout=5)
