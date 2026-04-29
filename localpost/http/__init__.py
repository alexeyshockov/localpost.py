from localpost.http._cancel import RequestCancelled, check_cancelled
from localpost.http._pool import thread_pool_handler
from localpost.http._service import http_server, httptools_server, wsgi_server
from localpost.http._types import BodyTooLarge, InformationalResponse, Request
from localpost.http._types import Response as NativeResponse
from localpost.http.config import LOGGER_NAME, ServerConfig
from localpost.http.router import (
    RequestCtx,
    Response,
    Route,
    Router,
    Routes,
    URITemplate,
)
from localpost.http.router import (
    RequestHandler as RouterRequestHandler,
)
from localpost.http.server import BodyHandler, HTTPReqCtx, RequestHandler, start_http_server
from localpost.http.wsgi import wrap_wsgi

# ``Response`` (the public name) is the high-level router response.
# ``NativeResponse`` is the low-level wire-format response used directly
# with ``HTTPReqCtx.complete`` / ``start_response``.
__all__ = [
    # config
    "ServerConfig",
    "LOGGER_NAME",
    # server
    "start_http_server",
    "HTTPReqCtx",
    "RequestHandler",
    "BodyHandler",
    # backend selection
    "httptools_server",
    # neutral wire types (used directly with HTTPReqCtx)
    "Request",
    "NativeResponse",
    "InformationalResponse",
    "BodyTooLarge",
    # router
    "Router",
    "Routes",
    "Route",
    "URITemplate",
    "RequestCtx",
    "Response",
    "RouterRequestHandler",
    # wsgi
    "wrap_wsgi",
    # hosting
    "http_server",
    "wsgi_server",
    "thread_pool_handler",
    # cancellation
    "check_cancelled",
    "RequestCancelled",
]
