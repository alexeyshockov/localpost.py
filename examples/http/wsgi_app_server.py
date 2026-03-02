import logging

from anyio import from_thread, to_thread
from flask import Flask, stream_with_context
from flask import request as flask_request

from localpost.http.config import ServerConfig
from localpost.http.server import start_http_server
from localpost.http.wsgi import wrap_wsgi


async def main():
    logging.basicConfig(level=logging.DEBUG)

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

    def run_server():
        with start_http_server(ServerConfig()) as server:
            while True:
                from_thread.check_cancelled()
                server.run(req_handler)

    async with wrap_wsgi(app) as req_handler:
        await to_thread.run_sync(run_server)


if __name__ == "__main__":
    main()
