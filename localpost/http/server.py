"""Back-compat re-export for the historic ``localpost.http.server`` import path.

The h11 server lives in :mod:`localpost.http.server_h11` and is the default
backend. New code should import from :mod:`localpost.http`.
"""

from __future__ import annotations

from localpost.http._base import (
    BaseServer as Server,
)
from localpost.http._base import (
    BodyHandler,
    HTTPReqCtx,
    RequestHandler,
    emit_handler_error,
)
from localpost.http._types import BodyTooLarge
from localpost.http.server_h11 import HTTPConnH11 as HTTPConn
from localpost.http.server_h11 import HTTPReqCtxH11, start_http_server

# Historic alias — concrete type, used in some test type hints.
HTTPReqCtxConcrete = HTTPReqCtxH11

__all__ = [
    "start_http_server",
    "Server",
    "HTTPConn",
    "HTTPReqCtx",
    "HTTPReqCtxH11",
    "BodyHandler",
    "RequestHandler",
    "BodyTooLarge",
    "emit_handler_error",
]
