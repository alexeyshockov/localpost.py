"""Serve a Flask (WSGI) app under ``localpost.http`` via the WSGI bridge.

Run::

    uv run --group dev-http examples/http/wsgi_app_server.py

    curl http://localhost:8000/hello/world
    curl http://localhost:8000/hello-stream/world
"""

from __future__ import annotations

import logging
import sys

from flask import Flask
from flask import request as flask_request
from flask.helpers import stream_with_context  # type: ignore[import-untyped]

from localpost.hosting import run_app, service
from localpost.http import ServerConfig, http_server, thread_pool_handler, wrap_wsgi


def build_app() -> Flask:
    app = Flask(__name__)

    @app.route("/hello/<name>")
    def hello(name: str):
        user_agent = flask_request.headers.get("User-Agent", "Unknown")
        return f"Hello, {name}! Your User-Agent is: {user_agent}\n"

    @app.route("/hello-stream/<name>")
    def hello_stream(name: str):
        user_agent = flask_request.headers.get("User-Agent", "Unknown")

        def generate():
            yield f"Hello, {name}! "
            yield f"Your User-Agent is: {user_agent}\n"

        return stream_with_context(generate())

    return app


@service
async def wsgi_app_service():
    config = ServerConfig(host="127.0.0.1", port=8000)
    # WSGI views block on response-body iteration, so wrap with a thread pool
    # to serve more than one request at a time.
    async with thread_pool_handler(wrap_wsgi(build_app()), max_concurrency=8) as wrapped:
        async with http_server(config, wrapped):
            yield


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return run_app(wsgi_app_service())


if __name__ == "__main__":
    sys.exit(main())
