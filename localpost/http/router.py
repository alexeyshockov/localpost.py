from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from http import HTTPMethod
from typing import Self, final
from urllib.parse import parse_qs

from localpost._utils import NOT_SET
from localpost.http.config import DEFAULT_BUFFER_SIZE

_VAR_PATTERN = re.compile(r"\{([^}]+)\}")


@final
@dataclass(frozen=True, slots=True)
class URITemplate:
    """Basic URI Template (RFC 6570) implementation, just the first level from the spec."""

    template: str
    variable_names: tuple[str, ...]
    _regex: re.Pattern[str]

    @classmethod
    def parse(cls, template: str) -> Self:
        variable_names: list[str] = []
        regex_parts: list[str] = []
        last_end = 0
        for m in _VAR_PATTERN.finditer(template):
            # Escape literal text between variables
            regex_parts.append(re.escape(template[last_end : m.start()]))
            var_name = m.group(1)
            variable_names.append(var_name)
            regex_parts.append(f"(?P<{var_name}>[^/]+)")
            last_end = m.end()
        regex_parts.append(re.escape(template[last_end:]))
        pattern = re.compile("^" + "".join(regex_parts) + "$")
        return cls(
            template=template,
            variable_names=tuple(variable_names),
            _regex=pattern,
        )

    def match(self, uri: str) -> dict[str, str] | None:
        m = self._regex.match(uri)
        return m.groupdict() if m else None


@final
@dataclass(frozen=True, eq=False, slots=True)
class RequestCtx:
    exit_stack: ExitStack

    headers: Mapping[str, str]
    method: HTTPMethod
    matched_template: URITemplate
    path: str
    query_string: str
    query_args: Mapping[str, list[str]]
    path_args: Mapping[str, str]
    """Path params of the matched route."""

    receive: Callable[[int], bytes]
    """Receive the body from the connection."""

    _req_body: bytearray | None | object = field(default=NOT_SET, init=False, repr=False)

    def body(self, cache: bool = False) -> bytes:
        """Read the request body."""
        if self._req_body is None:
            raise RuntimeError("body has been read and not cached")
        if isinstance(self._req_body, bytearray):
            return bytes(self._req_body)

        body = bytearray()
        object.__setattr__(self, "_req_body", body if cache else None)
        while True:
            chunk = self.receive(DEFAULT_BUFFER_SIZE)
            if not chunk:
                break
            body.extend(chunk)
        return bytes(body)


@dataclass(frozen=True, eq=False, slots=True)
class Response:
    status_code: int
    headers: Mapping[str, str]
    body: Iterable[bytes]


RequestHandler = Callable[[RequestCtx], Response]
RequestHandlerMiddleware = Callable[[RequestHandler], RequestHandler]


def _headers_from_environ(environ: dict) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            header_name = key[5:].replace("_", "-").lower()
            headers[header_name] = value
    if "CONTENT_TYPE" in environ:
        headers["content-type"] = environ["CONTENT_TYPE"]
    if "CONTENT_LENGTH" in environ:
        headers["content-length"] = environ["CONTENT_LENGTH"]
    return headers


@dataclass(eq=False, frozen=True, slots=True)
class Router:
    paths: Mapping[URITemplate, Mapping[HTTPMethod, RequestHandler]]

    def wsgi(self, environ: dict, start_response) -> Iterable[bytes]:
        """WSGI app, to be used with any WSGI server, e.g. Gunicorn."""
        request_path = environ.get("PATH_INFO", "/")
        request_method_str = environ.get("REQUEST_METHOD", "GET").upper()

        # Find matching template
        matched_template: URITemplate | None = None
        path_args: dict[str, str] = {}
        matched_methods: Mapping[HTTPMethod, RequestHandler] | None = None

        for template, methods in self.paths.items():
            result = template.match(request_path)
            if result is not None:
                matched_template = template
                path_args = result
                matched_methods = methods
                break

        if matched_template is None or matched_methods is None:
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"Not Found"]

        try:
            method = HTTPMethod(request_method_str)
        except ValueError:
            start_response("405 Method Not Allowed", [("Content-Type", "text/plain")])
            return [b"Method Not Allowed"]

        handler = matched_methods.get(method)
        if handler is None:
            allowed = ", ".join(m.value for m in matched_methods)
            start_response("405 Method Not Allowed", [("Content-Type", "text/plain"), ("Allow", allowed)])
            return [b"Method Not Allowed"]

        query_string = environ.get("QUERY_STRING", "")
        query_args = parse_qs(query_string)
        headers = _headers_from_environ(environ)

        wsgi_input = environ.get("wsgi.input")

        def receive(size: int) -> bytes:
            if wsgi_input is None:
                return b""
            return wsgi_input.read(size) or b""

        with ExitStack() as stack:
            ctx = RequestCtx(
                exit_stack=stack,
                headers=headers,
                method=method,
                matched_template=matched_template,
                path=request_path,
                query_string=query_string,
                query_args=query_args,
                path_args=path_args,
                receive=receive,
            )
            response = handler(ctx)

        response_headers = [(k, v) for k, v in response.headers.items()]
        status_line = f"{response.status_code} {_status_phrase(response.status_code)}"
        start_response(status_line, response_headers)
        return response.body


def _status_phrase(code: int) -> str:
    phrases = {
        200: "OK",
        201: "Created",
        204: "No Content",
        301: "Moved Permanently",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
    }
    return phrases.get(code, "Unknown")
