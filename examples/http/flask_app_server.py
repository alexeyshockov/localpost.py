"""Flask app served via the native Flask adapter — streaming without ``@stream_with_context``.

Run::

    uv run --group dev-http examples/http/flask_app_server.py

    curl http://localhost:8000/hello/world
    curl http://localhost:8000/stream/world        # uses flask.request during streaming

Key difference from the WSGI example: the ``/stream/...`` view returns a
generator that reads ``flask.request.headers`` while yielding — and we did
**not** wrap it in ``@stream_with_context``. It works because the native
Flask handler (:func:`localpost.http.flask.flask_handler`) keeps Flask's
request context active through response-body iteration.
"""

from __future__ import annotations

import logging

from flask import Flask, Response
from flask import request as flask_request

from localpost.hosting import run_app, service
from localpost.http import ServerConfig, http_server, thread_pool_handler
from localpost.http.flask import flask_handler
from localpost.threadtools import WorkerExecutor


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
        # Under the native Flask handler this runs AFTER the body is fully sent.
        app.logger.info("teardown_request (exc=%r)", exc)

    return app


@service
async def app_svc():
    config = ServerConfig(host="127.0.0.1", port=8000)
    with WorkerExecutor() as ex:
        async with thread_pool_handler(flask_handler(build_app()), ex) as wrapped:
            async with http_server(config, wrapped):
                yield


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run_app(app_svc())


if __name__ == "__main__":
    main()
