"""Async-flavour public API for ``localpost.openapi``.

Symbols here mirror the sync API at ``localpost.openapi`` — :class:`HttpApp`
becomes :class:`HttpAsyncApp`, :class:`OpMiddleware` becomes
:class:`AsyncOpMiddleware`, etc. The two flavours share the type-level
machinery (signature parsing, OpenAPI doc emission, ``OpResult`` hierarchy,
SSE encoding) and only diverge at the request-time invocation: async apps
``await`` user functions, async middleware, and async ASGI send/receive.

The async app is deployed to an ASGI server by default —
:meth:`HttpAsyncApp.asgi` returns the ASGI 3 callable; :meth:`HttpAsyncApp.service`
wires it up under uvicorn for ``localpost.hosting``.
"""

from localpost.openapi.aio._ctx import AsyncHTTPReqCtx
from localpost.openapi.aio.app import HttpAsyncApp
from localpost.openapi.aio.auth import AsyncHttpBasicAuth, AsyncHttpBearerAuth
from localpost.openapi.aio.middleware import (
    AsyncApiOperation,
    AsyncOpMiddleware,
    async_op_middleware,
)
from localpost.openapi.aio.operation import AsyncOperation

__all__ = [
    "HttpAsyncApp",
    "AsyncOperation",
    "AsyncHTTPReqCtx",
    "AsyncOpMiddleware",
    "AsyncApiOperation",
    "async_op_middleware",
    "AsyncHttpBearerAuth",
    "AsyncHttpBasicAuth",
]
