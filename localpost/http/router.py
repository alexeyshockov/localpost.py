from __future__ import annotations

import re
from collections.abc import Mapping, Buffer, Callable, Iterable
from contextlib import ExitStack, AbstractContextManager
from dataclasses import dataclass, field
from http import HTTPMethod

import h11

from localpost.http.server import HTTPReq
from localpost.http.uritemplate import URITemplate


# See also: https://learn.microsoft.com/en-us/aspnet/core/fundamentals/use-http-context#httprequest
@dataclass(eq=False, slots=True)
class RESTContext:
    _context: HTTPReq
    exit_stack: ExitStack

    request: h11.Request
    path_info: str
    query_string: str
    query_args: Mapping[str, list[str]]
    path_args: Mapping[str, str]
    """Path params of the matched route."""

    result: OpResult = field(default_factory=lambda: Ok(None), init=False)

    _body: bytearray | None = field(default=None, init=False)

    def receive_all(self, cache=False) -> Buffer:
        """Read the request body."""
        if self._body is not None:
            return self._body
        body = bytearray()
        while True:
            chunk = self._context.receive()
            if not chunk:
                break
            body.extend(chunk)
        if cache:
            self._body = body
        return body

    def get_header(self, name: str, /, default=None) -> str | None:
        raw_name = name.encode()
        for header_name, header_value in self.request.headers:
            if header_name == raw_name:
                return header_value.decode()
        return default


RequestHandler = Callable[[RESTContext], None]
RequestHandlerMiddleware = Callable[[RequestHandler], RequestHandler]

HTTPResponse = tuple[int, Iterable[tuple[str, str]], AbstractContextManager[Iterable[bytes]]]
ResultConverter = Callable[[RESTContext], HTTPResponse]


def not_found(request: RESTContext) -> None:
    """Default handler for unmatched routes."""
    request.start_response(h11.Response(status_code=404, headers=[(b"Content-Type", b"text/plain")]))
    # TODO Problem details if the client supports JSON (Accept header contains "json")
    request.send(b"Not Found")


def method_not_allowed(request: RESTContext) -> None:
    """Default handler for unsupported HTTP methods."""
    request.start_response(h11.Response(status_code=405, headers=[(b"Content-Type", b"text/plain")]))
    # TODO Problem details if the client supports JSON (Accept header contains "json")
    request.send(b"Method Not Allowed")


class Router:
    paths: Mapping[URITemplate, Mapping[HTTPMethod, RequestHandler]]

    def __call__(self, request: HTTPReq) -> None:
        # FIXME Find a match, create RESTContext with extracted path_args, entered ExitStack, etc.
        pass

    def wsgi(self, environ, start_response):
        """WSGI app, to be used with any WSGI server, e.g. Gunicorn."""
        # TODO Adapt from WSGI, create RESTContext, ...
        pass



@dataclass(frozen=True, slots=True)
class OpResult:
    code: int
    headers: Mapping[str, str]
    body: object

    def __init__(self, code: int, body: object, /, *, headers: Mapping[str, str] | None = None):
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "headers", headers or {})


@dataclass(frozen=True, slots=True)
class Ok[T](OpResult):
    def __init__(self, body: T, /, *, headers: dict[str, str] | None = None):
        super().__init__(200, body, headers=headers)


@dataclass(frozen=True, slots=True)
class Created[T](OpResult):
    def __init__(self, body: T, /, *, headers: dict[str, str] | None = None):
        super().__init__(201, body, headers=headers)


@dataclass(frozen=True, slots=True)
class NoContent(OpResult):
    def __init__(self, /, *, headers: dict[str, str] | None = None):
        super().__init__(204, None, headers=headers)


@dataclass(frozen=True, slots=True)
class MovedPermanently(OpResult):
    def __init__(self, location: str, /, *, headers: dict[str, str] | None = None):
        super().__init__(301, None, headers={"Location": location, **(headers or {})})


@dataclass(frozen=True, slots=True)
class BadRequest[T](OpResult):
    def __init__(self, body: T, /, *, headers: dict[str, str] | None = None):
        super().__init__(400, body, headers=headers)


@dataclass(frozen=True, slots=True)
class Unauthorized[T](OpResult):
    def __init__(self, body: T, /, *, headers: dict[str, str] | None = None):
        super().__init__(401, body, headers=headers)


@dataclass(frozen=True, slots=True)
class Forbidden[T](OpResult):
    def __init__(self, body: T, /, *, headers: dict[str, str] | None = None):
        super().__init__(403, body, headers=headers)


@dataclass(frozen=True, slots=True)
class NotFound[T](OpResult):
    def __init__(self, body: T, /, *, headers: dict[str, str] | None = None):
        super().__init__(404, body, headers=headers)
