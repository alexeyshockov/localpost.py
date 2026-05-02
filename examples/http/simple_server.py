import logging

from localpost.http import HTTPReqCtx, Response, ServerConfig, start_http_server


def _main():
    def simple_app(ctx: HTTPReqCtx):
        ctx.complete(
            Response(
                status_code=200,
                headers=[(b"Content-Type", b"text/plain"), (b"Content-Length", b"14")],
            ),
            b"Hello, World!\n",
        )

    with start_http_server(ServerConfig(), simple_app) as server:
        while True:
            server.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    _main()
