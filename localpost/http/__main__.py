"""CLI entry point: ``python -m localpost.http module:handler``."""

from __future__ import annotations

import importlib
import logging

import click

from localpost import hosting
from localpost.http import RequestHandler, ServerConfig, http_server, thread_pool_handler
from localpost.threadtools import WorkerExecutor


def _load_handler(app_str: str) -> RequestHandler:
    if ":" not in app_str:
        raise click.BadParameter(f"Expected 'module:attr', got {app_str!r}", param_hint="APP")
    module_path, attr = app_str.rsplit(":", 1)
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise click.ClickException(f"Cannot import {module_path!r}: {e}") from e
    try:
        return getattr(module, attr)  # type: ignore[return-value]
    except AttributeError as e:
        raise click.ClickException(f"{module_path!r} has no attribute {attr!r}") from e


@click.command()
@click.argument("app")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host.")
@click.option("--port", default=8000, show_default=True, help="Bind port.")
@click.option("--pool/--no-pool", default=False, show_default=True, help="Run handlers on a thread pool.")
@click.option("--selectors", default=1, show_default=True, help="Selector threads.")
@click.option("--acceptor", is_flag=True, default=False, help="Use acceptor topology.")
def main(app: str, host: str, port: int, pool: bool, selectors: int, acceptor: bool) -> None:
    """Run a LocalPost HTTP/1.1 server.

    APP is a 'module:handler' reference — e.g. ``myapp:router_handler``.
    The attribute must be a :data:`localpost.http.RequestHandler` callable.
    Pass ``--pool`` to wrap it with :func:`localpost.http.thread_pool_handler`.
    """
    logging.basicConfig(level=logging.INFO)
    handler = _load_handler(app)
    config = ServerConfig(host=host, port=port)

    @hosting.service
    async def _serve():
        if pool:
            with WorkerExecutor() as executor:
                async with thread_pool_handler(handler, executor) as h:
                    async with http_server(config, h, selectors=selectors, acceptor=acceptor):
                        yield
        else:
            async with http_server(config, handler, selectors=selectors, acceptor=acceptor):
                yield

    hosting.run_app(_serve())


if __name__ == "__main__":
    main()
