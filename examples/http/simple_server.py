import logging

import h11

from localpost import threadtools
from localpost.http.config import ServerConfig
from localpost.http.server import HTTPReqCtx, start_http_server


def _main():
    logging.basicConfig(level=logging.DEBUG)
    threadtools.check_cancelled = lambda: None

    def simple_app(ctx: HTTPReqCtx):
        ctx.complete(h11.Response(status_code=200, headers=[(b"Content-Type", b"text/plain")]), b"Hello, World!\n")

    with start_http_server(ServerConfig()) as server:
        while True:
            server.run(simple_app)


if __name__ == "__main__":
    _main()
