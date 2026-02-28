from __future__ import annotations

from flask import Flask, stream_with_context
from flask import request as flask_request

app = Flask(__name__)


@app.route("/hello/<name>")
def hello(name):
    user_agent = flask_request.headers.get("User-Agent", "Unknown")
    return f"Hello, {name}! Your User-Agent is: {user_agent}\n"


@app.route("/hello-stream/<name>")
@stream_with_context
def hello_stream(name):
    user_agent = flask_request.headers.get("User-Agent", "Unknown")
    yield f"Hello, {name}! "
    yield f"Your User-Agent is: {user_agent}\n"


@app.route("/hello-data/<name>", methods=["POST"])
@stream_with_context
def hello_data(name):
    user_agent = flask_request.headers.get("User-Agent", "Unknown")
    yield f"Hello, {name}! "
    yield f"Your User-Agent is: {user_agent}\n"
    json_data = flask_request.get_json(force=True, cache=False)
    yield f"And you sent: {json_data}\n"
