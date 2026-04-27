"""Flask app shared by every WSGI-side stack."""

from __future__ import annotations

from flask import Flask, Response
from flask import request as flask_request

from benchmarks.http.scenarios import HELLO_PREFIX, PING_BODY


def build_app() -> Flask:
    app = Flask(__name__)

    @app.get("/ping")
    def ping() -> Response:
        return Response(PING_BODY, mimetype="text/plain")

    @app.get("/hello/<name>")
    def hello(name: str) -> Response:
        return Response(f"{HELLO_PREFIX}{name}".encode(), mimetype="text/plain")

    @app.post("/echo")
    def echo() -> Response:
        return Response(flask_request.get_data(cache=False), mimetype="application/json")

    return app
