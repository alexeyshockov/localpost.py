import logging
from contextlib import AbstractContextManager

import anyio
import h11
from anyio import from_thread, to_thread

from localpost.http.config import ServerConfig
from localpost.http.server import HTTPReqCtx, start_http_server
from localpost.threadtools import create_executor


async def main():
    logging.basicConfig(level=logging.DEBUG)

    routes = {
        (b"GET", b"/"),
        (b"GET", b"/hello"),
    }

    def handle_route(req_ctx: AbstractContextManager[HTTPReqCtx]):
        with req_ctx as req:
            req.start_response(h11.Response(status_code=200, headers=[(b"Content-Type", b"text/plain")]))
            req.send(b"Hello, World!\n")
            req.finish_response()

    def complex_app(ctx: HTTPReqCtx):
        key = (ctx.request.method, ctx.request.target)
        if key in routes:
            if handle_soon := executor.acquire_nowait():
                handle_soon(ctx.borrow())
            else:
                ctx.complete(
                    h11.Response(status_code=503, headers=[(b"Content-Type", b"text/plain")]), b"Server is busy\n"
                )
        else:
            response = h11.Response(status_code=404, headers=[(b"Content-Type", b"text/plain")])
            body = b"Not found\n"
            ctx.complete(response, body)

    def run_server():
        with start_http_server(ServerConfig()) as server:
            while True:
                from_thread.check_cancelled()
                server.run(complex_app)

    async with create_executor(handle_route) as executor:
        await to_thread.run_sync(run_server)


if __name__ == "__main__":
    anyio.run(main)
