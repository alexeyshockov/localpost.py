from __future__ import annotations

import logging

import uvicorn
from a2wsgi import WSGIMiddleware
from ._benchmark_app import app


def _flask_app_bench():
    logging.basicConfig(level=logging.DEBUG)

    asgi_app = WSGIMiddleware(app)
    uvicorn.run(asgi_app, host="0.0.0.0", port=8000, access_log=False)


if __name__ == "__main__":
    _flask_app_bench()
