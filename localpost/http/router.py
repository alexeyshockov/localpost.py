from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Buffer, Callable, Iterable, Mapping
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from http import Headers, HTTPMethod
from typing import cast, final, override

import h11

from localpost._utils import NOT_SET
from localpost.http.config import DEFAULT_BUFFER_SIZE
from localpost.http.server import HTTPReqCtx
from localpost.http.uritemplate import URITemplate


@final
@dataclass(frozen=True, eq=False, slots=True)
class RequestCtx:
    exit_stack: ExitStack

    headers: Headers
    method: HTTPMethod
    matched_template: URITemplate
    path: str
    query_string: str
    query_args: Mapping[str, list[str]]
    path_args: Mapping[str, str]
    """Path params of the matched route."""

    receive: Callable[[int], bytes]
    """Receive the body from the connection."""

    _req_body: bytearray | None | object = field(default=NOT_SET, init=False)

    def body(self, cache: bool = False) -> Buffer:
        """Read the request body."""
        if self._req_body is None:
            raise RuntimeError("body has been read and not cached")
        if isinstance(self._req_body, bytearray):
            return self._req_body

        body = bytearray()
        self._req_body = body if cache else None
        while True:
            chunk = self.receive(DEFAULT_BUFFER_SIZE)
            if not chunk:
                break
            body.extend(chunk)
        return body


@dataclass(frozen=True, eq=False, slots=True)
class Response:
    status_code: int
    headers: Headers
    body: Iterable[bytes]


RequestHandler = Callable[[RequestCtx], Response]
RequestHandlerMiddleware = Callable[[RequestHandler], RequestHandler]


@dataclass(eq=False, frozen=True, slots=True)
class Router:
    paths: Mapping[URITemplate, Mapping[HTTPMethod, RequestHandler]]

    def __call__(self, req_ctx: HTTPReqCtx) -> None:
        # If match not found — respond immediately with 404.
        # FIXME Then — borrow, move to thread, create RequestCtx with extracted path_args, entered ExitStack, etc.
        pass

    def wsgi(self, environ, start_response):
        """WSGI app, to be used with any WSGI server, e.g. Gunicorn."""
        # TODO Create RequestCtx with extracted path_args, entered ExitStack, etc.

    async def asgi(self, scope, receive, send):
        """ASGI app, to be used with any ASGI server, e.g. Uvicorn."""
        # If match not found — respond immediately with 404.
        # TODO Then — execute in a thread, create RequestCtx with extracted path_args, entered ExitStack, etc.
