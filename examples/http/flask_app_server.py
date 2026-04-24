"""Flask app served via ``flask_server`` — streaming without ``@stream_with_context``.

Run::

    uv run --group dev-http examples/http/flask_app_server.py

    curl http://localhost:8000/hello/world
    curl http://localhost:8000/stream/world        # uses flask.request during streaming

Key difference from the WSGI example: the ``/stream/...`` view returns a
generator that reads ``flask.request.headers`` while yielding — and we did
**not** wrap it in ``@stream_with_context``. It works because
:func:`localpost.http.flask.flask_server` keeps Flask's request context
active through response-body iteration.
"""

from __future__ import annotations

import logging
import sys

from flask import Flask, Response
from flask import request as flask_request

from localpost.hosting import run_app
from localpost.http import ServerConfig
from localpost.http.flask import flask_server


def build_app() -> Flask:
    app = Flask(__name__)

    @app.route("/hello/<name>")
    def hello(name: str):
        ua = flask_request.headers.get("User-Agent", "unknown")
        return f"Hello, {name}! (UA={ua})\n"

    @app.route("/stream/<name>")
    def stream(name: str):
        def generate():
            yield f"Hello, {name}! "
            # flask.request is still live here — no stream_with_context needed.
            yield f"Your User-Agent: {flask_request.headers.get('User-Agent', '?')}\n"

        return Response(generate(), mimetype="text/plain")

    @app.teardown_request
    def _log_teardown(exc):
        # Under flask_server this runs AFTER the body is fully sent.
        app.logger.info("teardown_request (exc=%r)", exc)

    return app


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    config = ServerConfig(host="127.0.0.1", port=8000)
    return run_app(flask_server(config, build_app(), max_concurrency=8))


if __name__ == "__main__":
    sys.exit(main())
