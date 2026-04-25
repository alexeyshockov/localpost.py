from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from http import HTTPMethod
from http.client import responses as _http_phrases
from typing import Self, final
from urllib.parse import parse_qs

import h11

from localpost._utils import NOT_SET
from localpost.http.config import DEFAULT_BUFFER_SIZE
from localpost.http.server import HTTPReqCtx
from localpost.http.server import RequestHandler as NativeRequestHandler

__all__ = [
    "URITemplate",
    "RequestCtx",
    "Response",
    "RequestHandler",
    "Route",
    "Routes",
    "Router",
]

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

    receive: Callable[[int], bytes]

    _req_body: bytearray | None | object = field(default=NOT_SET, init=False, repr=False)

    def body(self, cache: bool = False) -> bytes:
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


@final
@dataclass(frozen=True, eq=False, slots=True)
class Route:
    """One compiled route inside a :class:`Router`."""

    template: URITemplate
    methods: Mapping[HTTPMethod, RequestHandler]
    """Method → handler table for this template."""

    allow_header: str
    """Pre-rendered ``Allow`` header value (e.g. ``"GET, POST"``)."""

    _group_prefix: str
    """Internal: the named-group prefix this route uses inside the combined regex."""


@final
@dataclass(eq=False, slots=True)
class Routes:
    """Mutable route builder. Accumulate routes via decorators, then ``build()`` a Router.

    Usage::

        routes = Routes()

        @routes.get("/hello/{name}")
        def hello(ctx: RequestCtx) -> Response:
            return Response(200, {"content-type": "text/plain"}, [b"hi"])

        router = routes.build()
        # or equivalently: router = Router.from_routes(routes)
    """

    paths: dict[URITemplate, dict[HTTPMethod, RequestHandler]] = field(default_factory=dict)

    def add(self, method: HTTPMethod | str, template: str, handler: RequestHandler) -> None:
        m = method if isinstance(method, HTTPMethod) else HTTPMethod(method.upper())
        key = _find_template(self.paths, template) or URITemplate.parse(template)
        self.paths.setdefault(key, {})[m] = handler

    def _decorator(self, method: HTTPMethod, template: str) -> Callable[[RequestHandler], RequestHandler]:
        def deco(handler: RequestHandler) -> RequestHandler:
            self.add(method, template, handler)
            return handler

        return deco

    def get(self, template: str) -> Callable[[RequestHandler], RequestHandler]:
        return self._decorator(HTTPMethod.GET, template)

    def post(self, template: str) -> Callable[[RequestHandler], RequestHandler]:
        return self._decorator(HTTPMethod.POST, template)

    def put(self, template: str) -> Callable[[RequestHandler], RequestHandler]:
        return self._decorator(HTTPMethod.PUT, template)

    def delete(self, template: str) -> Callable[[RequestHandler], RequestHandler]:
        return self._decorator(HTTPMethod.DELETE, template)

    def patch(self, template: str) -> Callable[[RequestHandler], RequestHandler]:
        return self._decorator(HTTPMethod.PATCH, template)

    def build(self) -> Router:
        return Router.from_routes(self)


@final
@dataclass(frozen=True, eq=False, slots=True)
class Router:
    """Immutable, compiled URI-template dispatcher.

    Build via :meth:`from_routes` or :meth:`Routes.build`. The constructor fields are
    an implementation detail — treat the class as read-only outside of ``from_routes``.
    """

    routes: tuple[Route, ...]
    """Routes in dispatch order — longest literal prefix first, stable on ties."""

    _regex: re.Pattern[str]

    @classmethod
    def from_routes(cls, routes: Routes) -> Self:
        # Deduplicate: Routes.add already merges by template string, but guard
        # against anyone constructing the builder's paths dict by hand.
        seen: set[str] = set()
        for t in routes.paths:
            if t.template in seen:
                raise ValueError(f"duplicate template: {t.template!r}")
            seen.add(t.template)

        ordered = sorted(
            routes.paths.items(),
            key=lambda item: _literal_prefix_len(item[0]),
            reverse=True,
        )

        compiled_routes: list[Route] = []
        alternatives: list[str] = []

        for i, (tmpl, method_map) in enumerate(ordered):
            prefix = f"r{i}"
            allow_header = ", ".join(m.value for m in sorted(method_map, key=lambda hm: hm.value))
            compiled_routes.append(
                Route(
                    template=tmpl,
                    # Freeze the handler map at the type level (still a plain dict
                    # underneath, but exposed as Mapping — external mutation is a type error).
                    methods=dict(method_map),
                    allow_header=allow_header,
                    _group_prefix=prefix,
                )
            )
            alternatives.append(f"(?P<{prefix}>{_reprefixed_regex_source(tmpl, prefix)})")

        combined = re.compile("^(?:" + "|".join(alternatives) + ")$") if alternatives else re.compile(r"^(?!)")

        return cls(
            routes=tuple(compiled_routes),
            _regex=combined,
        )

    # --- Dispatch -----------------------------------------------------

    def _match(self, path: str, method_str: str) -> _MatchResult:
        m = self._regex.match(path)
        if m is None:
            return _MatchResult(kind="not_found")

        for route in self.routes:
            if m.group(route._group_prefix) is None:
                continue
            tmpl = route.template
            method_map = route.methods
            path_args = {v: m.group(f"{route._group_prefix}_{v}") or "" for v in tmpl.variable_names}

            try:
                method = HTTPMethod(method_str)
            except ValueError:
                return _MatchResult(
                    kind="method_not_allowed",
                    allowed=tuple(method_map),
                    allow_header=route.allow_header,
                )

            handler = method_map.get(method)
            if handler is None:
                return _MatchResult(
                    kind="method_not_allowed",
                    allowed=tuple(method_map),
                    allow_header=route.allow_header,
                )

            return _MatchResult(
                kind="ok",
                handler=handler,
                method=method,
                matched_template=tmpl,
                path_args=path_args,
                allowed=tuple(method_map),
                allow_header=route.allow_header,
            )

        raise AssertionError("unreachable: regex matched but no outer group set")

    def wsgi(self, environ: dict, start_response) -> Iterable[bytes]:
        """WSGI app, to be used with any WSGI server, e.g. Gunicorn."""
        request_path = environ.get("PATH_INFO", "/")
        request_method_str = environ.get("REQUEST_METHOD", "GET").upper()
        match = self._match(request_path, request_method_str)

        if match.kind == "not_found":
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"Not Found"]
        if match.kind == "method_not_allowed":
            start_response(
                "405 Method Not Allowed",
                [("Content-Type", "text/plain"), ("Allow", match.allow_header)],
            )
            return [b"Method Not Allowed"]

        assert match.handler is not None
        assert match.matched_template is not None
        assert match.method is not None

        query_string = environ.get("QUERY_STRING", "")
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
                method=match.method,
                matched_template=match.matched_template,
                path=request_path,
                query_string=query_string,
                query_args=parse_qs(query_string),
                path_args=match.path_args,
                receive=receive,
            )
            response = match.handler(ctx)

        status_line = f"{response.status_code} {_http_phrases.get(response.status_code, 'Unknown')}"
        start_response(status_line, [(k, v) for k, v in response.headers.items()])
        return response.body

    def as_handler(self) -> NativeRequestHandler:
        """Return a :class:`localpost.http.RequestHandler` that dispatches via this router."""

        def handle(http_ctx: HTTPReqCtx) -> None:
            req = http_ctx.request
            target = req.target.decode("iso-8859-1")
            if "?" in target:
                path, query_string = target.split("?", 1)
            else:
                path, query_string = target, ""
            method_str = req.method.decode("ascii").upper()

            match = self._match(path, method_str)

            if match.kind == "not_found":
                _send_plain(http_ctx, 404, b"Not Found")
                return
            if match.kind == "method_not_allowed":
                _send_plain(
                    http_ctx,
                    405,
                    b"Method Not Allowed",
                    extra_headers=[(b"Allow", match.allow_header.encode("ascii"))],
                )
                return

            assert match.handler is not None
            assert match.matched_template is not None
            assert match.method is not None

            headers = {name.decode("iso-8859-1").lower(): value.decode("iso-8859-1") for name, value in req.headers}

            with ExitStack() as stack:
                ctx = RequestCtx(
                    exit_stack=stack,
                    headers=headers,
                    method=match.method,
                    matched_template=match.matched_template,
                    path=path,
                    query_string=query_string,
                    query_args=parse_qs(query_string),
                    path_args=match.path_args,
                    receive=http_ctx.receive,
                )
                response = match.handler(ctx)

            h11_headers = [(k.encode("iso-8859-1"), v.encode("iso-8859-1")) for k, v in response.headers.items()]
            http_ctx.start_response(
                h11.Response(
                    status_code=response.status_code,
                    headers=h11_headers,
                    reason=_http_phrases.get(response.status_code, "Unknown").encode("iso-8859-1"),
                )
            )
            for chunk in response.body:
                if chunk:
                    http_ctx.send(chunk)
            http_ctx.finish_response()

        return handle


@final
@dataclass(frozen=True, slots=True)
class _MatchResult:
    kind: str  # "ok" | "not_found" | "method_not_allowed"
    handler: RequestHandler | None = None
    method: HTTPMethod | None = None
    matched_template: URITemplate | None = None
    path_args: Mapping[str, str] = field(default_factory=dict)
    allowed: tuple[HTTPMethod, ...] = ()
    allow_header: str = ""


def _literal_prefix_len(t: URITemplate) -> int:
    """Length of the literal prefix up to the first ``{var}`` placeholder."""
    m = _VAR_PATTERN.search(t.template)
    return len(t.template) if m is None else m.start()


def _reprefixed_regex_source(t: URITemplate, prefix: str) -> str:
    """Rebuild ``t``'s regex source with ``{prefix}_`` prepended to every named group."""
    parts: list[str] = []
    last_end = 0
    for m in _VAR_PATTERN.finditer(t.template):
        parts.append(re.escape(t.template[last_end : m.start()]))
        var_name = m.group(1)
        parts.append(f"(?P<{prefix}_{var_name}>[^/]+)")
        last_end = m.end()
    parts.append(re.escape(t.template[last_end:]))
    return "".join(parts)


def _find_template(
    paths: Mapping[URITemplate, Mapping[HTTPMethod, RequestHandler]], template_str: str
) -> URITemplate | None:
    for t in paths:
        if t.template == template_str:
            return t
    return None


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


def _send_plain(
    ctx: HTTPReqCtx,
    status_code: int,
    body: bytes,
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"text/plain"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    ctx.complete(
        h11.Response(
            status_code=status_code,
            headers=headers,
            reason=_http_phrases.get(status_code, "Unknown").encode("iso-8859-1"),
        ),
        body,
    )
