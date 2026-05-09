"""Type-driven HTTP framework with OpenAPI 3.2 generation built in.

Sits on top of the ``localpost.http`` server. The user surface mirrors
FastAPI's decorator API; the difference is that the OpenAPI doc and the
runtime request handling are derived from the *same* type annotations,
including return-type unions for response shapes::

    from typing import Annotated
    from dataclasses import dataclass

    from localpost import hosting
    from localpost.http import ServerConfig
    from localpost.openapi import HttpApp, NotFound


    @dataclass
    class Book:
        id: str
        title: str


    app = HttpApp()


    @app.get("/books/{book_id}")
    def get_book(book_id: str) -> Book | NotFound[str]:
        if book_id != "42":
            return NotFound(f"Book not found: {book_id}")
        return Book(id=book_id, title="The Hitchhiker's Guide")


    hosting.run_app(app.service(ServerConfig(port=8000)))

Pydantic models are recognised automatically when pydantic is installed.
To plug in another schema library, supply a custom
:class:`localpost.openapi.adapters.AdapterRegistry` via ``HttpApp(adapters=...)``.
"""

from localpost.openapi import spec
from localpost.openapi.adapters import AdapterRegistry, TypeAdapter, default_registry
from localpost.openapi.aio import (
    AsyncApiOperation,
    AsyncHttpBasicAuth,
    AsyncHttpBearerAuth,
    AsyncHTTPReqCtx,
    AsyncOperation,
    AsyncOpMiddleware,
    HttpAsyncApp,
    async_op_middleware,
)
from localpost.openapi.app import HttpApp
from localpost.openapi.auth import HttpBasicAuth, HttpBearerAuth
from localpost.openapi.middleware import ApiOperation, OpMiddleware, op_middleware
from localpost.openapi.operation import Operation
from localpost.openapi.resolvers import (
    ArgResolver,
    ArgResolverFactory,
    FromBody,
    FromHeader,
    FromPath,
    FromQuery,
    ResolverCtx,
)
from localpost.openapi.results import (
    Accepted,
    BadRequest,
    Conflict,
    Created,
    EventStreamResult,
    Forbidden,
    InternalServerError,
    NoContent,
    NotFound,
    Ok,
    OpResult,
    TooManyRequests,
    Unauthorized,
    UnprocessableEntity,
)
from localpost.openapi.schemas import REF_TEMPLATE, SchemaRegistry
from localpost.openapi.sse import Event, EventStream

__all__ = [
    # core (sync)
    "HttpApp",
    "Operation",
    # core (async)
    "HttpAsyncApp",
    "AsyncOperation",
    "AsyncHTTPReqCtx",
    # spec sub-module (advanced; not all symbols flattened)
    "spec",
    # middleware (sync)
    "ApiOperation",
    "OpMiddleware",
    "op_middleware",
    "HttpBearerAuth",
    "HttpBasicAuth",
    # middleware (async)
    "AsyncApiOperation",
    "AsyncOpMiddleware",
    "async_op_middleware",
    "AsyncHttpBearerAuth",
    "AsyncHttpBasicAuth",
    # arg resolvers
    "ArgResolver",
    "ArgResolverFactory",
    "ResolverCtx",
    "FromPath",
    "FromQuery",
    "FromHeader",
    "FromBody",
    # OpResult hierarchy
    "OpResult",
    "Ok",
    "Created",
    "Accepted",
    "NoContent",
    "EventStreamResult",
    "BadRequest",
    "Unauthorized",
    "Forbidden",
    "NotFound",
    "Conflict",
    "UnprocessableEntity",
    "TooManyRequests",
    "InternalServerError",
    # schema utilities
    "SchemaRegistry",
    "REF_TEMPLATE",
    # type adapters (pluggable schema libraries)
    "TypeAdapter",
    "AdapterRegistry",
    "default_registry",
    # SSE
    "Event",
    "EventStream",
]
