"""Flask app shared by every WSGI-side stack."""

from __future__ import annotations

import time

from flask import Flask, Response, jsonify
from flask import request as flask_request

from benchmarks.macro.http.scenarios import HELLO_PREFIX, PING_BODY, PROFILE_WORK_DELAYS_S, build_profile_update


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

    @app.post("/users/<user_id>/profile")
    def profile_update(user_id: str) -> Response:
        response = build_profile_update(user_id, flask_request.get_json())
        for delay_s in PROFILE_WORK_DELAYS_S:
            time.sleep(delay_s)
        return jsonify(response)

    return app
