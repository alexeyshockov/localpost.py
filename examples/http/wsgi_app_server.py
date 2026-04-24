"""Serve a Flask (WSGI) app via ``localpost.http.wsgi_server``.

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

from localpost.hosting import run_app
from localpost.http import ServerConfig, wsgi_server


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


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    config = ServerConfig(host="127.0.0.1", port=8000)
    return run_app(wsgi_server(config, build_app(), max_concurrency=8))


if __name__ == "__main__":
    sys.exit(main())
