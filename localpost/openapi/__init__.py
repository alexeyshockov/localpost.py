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


    sys.exit(hosting.run_app(app.service(ServerConfig(port=8000))))

Pydantic models are recognised automatically — install pydantic
yourself; it isn't a runtime dependency of localpost.
"""

from localpost.openapi import spec
from localpost.openapi.app import HttpApp
from localpost.openapi.auth import HttpBasicAuth, HttpBearerAuth
from localpost.openapi.filter import OpFilter
from localpost.openapi.operation import Operation
from localpost.openapi.resolvers import (
    ArgResolver,
    ArgResolverFactory,
    FromBody,
    FromHeader,
    FromPath,
    FromQuery,
)
from localpost.openapi.results import (
    Accepted,
    BadRequest,
    Conflict,
    Created,
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
from localpost.openapi.schemas import REF_TEMPLATE, SchemaRegistry, is_pydantic_model
from localpost.openapi.sse import Event, EventStream

__all__ = [
    # core
    "HttpApp",
    "Operation",
    # spec sub-module (advanced; not all symbols flattened)
    "spec",
    # filters
    "OpFilter",
    "HttpBearerAuth",
    "HttpBasicAuth",
    # arg resolvers
    "ArgResolver",
    "ArgResolverFactory",
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
    "is_pydantic_model",
    # SSE
    "Event",
    "EventStream",
]
