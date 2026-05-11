from __future__ import annotations

import dataclasses
import socket
import threading
from collections.abc import Callable, Iterator
from typing import Literal, Protocol

import pytest

from localpost.http import RequestHandler, ServerConfig, start_http_server

ServeInThread = Callable[[RequestHandler], "_ServerCM"]


class ServeBackendInThread(Protocol):
    def __call__(self, handler: RequestHandler, config: ServerConfig | None = None) -> _ServerCM: ...


Backend = Literal["h11", "httptools"]


@pytest.fixture(params=("h11", "httptools"))
def http_backend(request) -> Backend:
    name: Backend = request.param
    if name == "httptools":
        try:
            import httptools  # noqa: F401
        except ImportError as e:
            pytest.skip(str(e))
    return name


@pytest.fixture
def free_port() -> int:
    """Bind a temporary socket to port 0 and return the assigned port number.

    The socket is closed before returning; there is a tiny race window where the
    OS may reuse the port. Good enough for tests, and avoids ``port=0`` read-back.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server_config() -> ServerConfig:
    """Default config for in-thread server tests: localhost + auto-assigned port."""
    return ServerConfig(host="127.0.0.1", port=0)


class _ServerCM:
    """Returned by ``serve_in_thread(handler)``; use as a context manager.

    Yields the live port; on exit, signals the worker to stop, joins it,
    and asserts no thread leak. Uncaught exceptions from the server loop
    are captured in ``loop_error`` and re-raised on exit unless the test
    sets ``expect_loop_error = True`` (used to characterize crash paths).
    """

    def __init__(self, config: ServerConfig, handler: RequestHandler) -> None:
        self._config = config
        self._handler = handler
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cm = start_http_server(config, handler)
        self.loop_error: BaseException | None = None
        self.expect_loop_error: bool = False

    def __enter__(self) -> int:
        server = self._cm.__enter__()
        stop = self._stop

        def loop() -> None:
            while not stop.is_set():
                try:
                    server.run(timeout=0.05)
                except OSError:
                    return  # listening socket closed
                except BaseException as e:  # noqa: BLE001
                    self.loop_error = e
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
            assert not t.is_alive(), "server thread did not stop within 5s"
        finally:
            self._cm.__exit__(exc_type, exc, tb)

        # Surface unexpected loop errors only if the test body succeeded —
        # otherwise the original failure is more informative.
        if exc_type is None:
            if self.loop_error is not None and not self.expect_loop_error:
                raise self.loop_error
            if self.loop_error is None and self.expect_loop_error:
                raise AssertionError("expected a server-loop error but none was raised")


@pytest.fixture
def serve_in_thread(server_config: ServerConfig) -> Iterator[ServeInThread]:
    """Run an HTTP server in a background thread for the duration of a ``with`` block.

    Usage::

        def test_thing(serve_in_thread):
            def handler(ctx): ...

            with serve_in_thread(handler) as port:
                resp = httpx.get(f"http://127.0.0.1:{port}/")
            assert resp.status_code == 200

    The worker thread reacts to shutdown within ~50 ms (selector poll interval),
    so tests don't need magic iteration counts.
    """
    active: list[_ServerCM] = []

    def make(handler: RequestHandler) -> _ServerCM:
        cm = _ServerCM(server_config, handler)
        active.append(cm)
        return cm

    yield make

    # Safety net for tests that forgot the ``with`` block.
    for cm in active:
        t = cm._thread
        if t is not None and t.is_alive():
            cm._stop.set()
            t.join(timeout=5)


@pytest.fixture
def serve_backend_in_thread(server_config: ServerConfig, http_backend: Backend) -> Iterator[ServeBackendInThread]:
    active: list[_ServerCM] = []
    backend_config = dataclasses.replace(server_config, backend=http_backend)

    def make(handler: RequestHandler, config: ServerConfig | None = None) -> _ServerCM:
        cm = _ServerCM(
            dataclasses.replace(config, backend=http_backend) if config is not None else backend_config,
            handler,
        )
        active.append(cm)
        return cm

    yield make

    for cm in active:
        t = cm._thread
        if t is not None and t.is_alive():
            cm._stop.set()
            t.join(timeout=5)
