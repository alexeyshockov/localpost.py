"""URI-template router for LocalPost HTTP — a thin dispatcher middleware.

Matches the request URI against a table of compiled :class:`URITemplate` s,
attaches the :class:`RouteMatch` to ``ctx.attrs[RouteMatch]``, and
delegates to the matched route's :data:`localpost.http.RequestHandler`.

404 / 405 are answered inline on the selector. The :class:`Router`
itself is just a :data:`localpost.http.RequestHandler` — wrap it with
:func:`localpost.http.thread_pool_handler` if you want matched routes
to run on workers, or compose with middleware via
:func:`localpost.http.compose`.

For Pythonic helpers (decorators, response conversion, param injection),
see :class:`localpost.http.app.HttpApp`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPMethod
from http.client import responses as _http_phrases
from typing import Self, final

from localpost.http._types import Response as _NativeResponse
from localpost.http.server import BodyHandler, HTTPReqCtx
from localpost.http.server import RequestHandler as NativeRequestHandler

__all__ = [
    "URITemplate",
    "RouteMatch",
    "Route",
    "Routes",
    "Router",
    "route_match",
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
@dataclass(frozen=True, slots=True)
class RouteMatch:
    """Attached to ``ctx.attrs[RouteMatch]`` by :class:`Router` on a successful match.

    Read via :func:`route_match` from inside a route handler or middleware.
    """

    method: HTTPMethod
    matched_template: URITemplate
    path_args: Mapping[str, str]


def route_match(ctx: HTTPReqCtx) -> RouteMatch:
    """Return the :class:`RouteMatch` the :class:`Router` placed on ``ctx``.

    Raises :exc:`KeyError` if called outside a route handler (or before
    the Router has run).
    """
    return ctx.attrs[RouteMatch]


@final
@dataclass(frozen=True, eq=False, slots=True)
class Route:
    """One compiled route inside a :class:`Router`."""

    template: URITemplate
    methods: Mapping[HTTPMethod, NativeRequestHandler]
    """Method → handler table for this template."""

    allow_header: str
    """Pre-rendered ``Allow`` header value (e.g. ``"GET, POST"``)."""

    method_not_allowed: tuple[_NativeResponse, bytes]
    """Pre-built ``(Response, body)`` for the 405 path on this route. Avoids
    rebuilding the response (status, headers list, ``content-length`` ASCII
    encode) on every method-mismatch."""

    _group_prefix: str
    """Internal: the named-group prefix this route uses inside the combined regex."""


@final
@dataclass(eq=False, slots=True)
class Routes:
    """Mutable route builder.

    The decorator forms (``.get``, ``.post``, ...) are thin sugar for
    :meth:`add` — they register the handler as a plain
    :data:`localpost.http.RequestHandler`, no framework wrapping. For
    decorator-driven registration with response conversion, param
    injection, etc. use :class:`localpost.http.app.HttpApp`.

    Usage::

        routes = Routes()


        @routes.get("/hello/{name}")
        def hello(ctx: HTTPReqCtx) -> BodyHandler | None:
            match = route_match(ctx)
            ctx.complete(NativeResponse(...), b"hi " + match.path_args["name"].encode())
            return None


        router = routes.build()
    """

    paths: dict[URITemplate, dict[HTTPMethod, NativeRequestHandler]] = field(default_factory=dict)

    def add(
        self,
        method: HTTPMethod | str,
        template: str,
        handler: NativeRequestHandler,
    ) -> None:
        m = method if isinstance(method, HTTPMethod) else HTTPMethod(method.upper())
        key = _find_template(self.paths, template) or URITemplate.parse(template)
        self.paths.setdefault(key, {})[m] = handler

    def _decorator(self, method: HTTPMethod, template: str) -> Callable[[NativeRequestHandler], NativeRequestHandler]:
        def deco(handler: NativeRequestHandler) -> NativeRequestHandler:
            self.add(method, template, handler)
            return handler

        return deco

    def get(self, template: str) -> Callable[[NativeRequestHandler], NativeRequestHandler]:
        return self._decorator(HTTPMethod.GET, template)

    def post(self, template: str) -> Callable[[NativeRequestHandler], NativeRequestHandler]:
        return self._decorator(HTTPMethod.POST, template)

    def put(self, template: str) -> Callable[[NativeRequestHandler], NativeRequestHandler]:
        return self._decorator(HTTPMethod.PUT, template)

    def delete(self, template: str) -> Callable[[NativeRequestHandler], NativeRequestHandler]:
        return self._decorator(HTTPMethod.DELETE, template)

    def patch(self, template: str) -> Callable[[NativeRequestHandler], NativeRequestHandler]:
        return self._decorator(HTTPMethod.PATCH, template)

    def build(self) -> Router:
        return Router.from_routes(self)


@final
@dataclass(frozen=True, eq=False, slots=True)
class Router:
    """Immutable, compiled URI-template dispatcher.

    Build via :meth:`from_routes` or :meth:`Routes.build`. As a
    :data:`localpost.http.RequestHandler`-producer, the dispatcher is
    just :meth:`as_handler`'s return value — wrap it with middleware
    via :func:`localpost.http.compose` and feed it to
    :func:`localpost.http.http_server`.
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
                    methods=dict(method_map),
                    allow_header=allow_header,
                    method_not_allowed=_build_method_not_allowed(allow_header),
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
            return _MATCH_NOT_FOUND

        for route in self.routes:
            if m.group(route._group_prefix) is None:
                continue
            tmpl = route.template
            method_map = route.methods
            path_args = {v: m.group(f"{route._group_prefix}_{v}") or "" for v in tmpl.variable_names}

            try:
                method = HTTPMethod(method_str)
            except ValueError:
                return _MatchMethodNotAllowed(route=route)

            handler = method_map.get(method)
            if handler is None:
                return _MatchMethodNotAllowed(route=route)

            return _MatchOk(
                handler=handler,
                match=RouteMatch(
                    method=method,
                    matched_template=tmpl,
                    path_args=path_args,
                ),
            )

        raise AssertionError("unreachable: regex matched but no outer group set")

    def as_handler(self) -> NativeRequestHandler:
        """Return a :data:`localpost.http.RequestHandler` that dispatches via this router.

        On a match, attaches :class:`RouteMatch` to ``ctx.attrs[RouteMatch]``
        and delegates to the registered per-route handler. 404 / 405 are
        answered inline (via ``ctx.complete``) using pre-built responses;
        the body bytes (if any) are silently drained by the http layer.
        """

        def dispatch(ctx: HTTPReqCtx) -> BodyHandler | None:
            req = ctx.request
            # ``req.path`` and ``req.method`` are pre-split / pre-uppercased
            # by the backend (httptools.parse_url for httptools, manual
            # split for h11) — no per-dispatch decode + split.
            path = req.path.decode("iso-8859-1")
            method_str = req.method.decode("ascii")

            match = self._match(path, method_str)

            if isinstance(match, _MatchNotFound):
                ctx.complete(_NOT_FOUND_RESPONSE, _NOT_FOUND_BODY)
                return None
            if isinstance(match, _MatchMethodNotAllowed):
                response, body = match.route.method_not_allowed
                ctx.complete(response, body)
                return None

            ctx.attrs[RouteMatch] = match.match
            return match.handler(ctx)

        return dispatch


# --- Match result types -------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class _MatchOk:
    handler: NativeRequestHandler
    match: RouteMatch


@final
@dataclass(frozen=True, slots=True)
class _MatchNotFound:
    pass


@final
@dataclass(frozen=True, slots=True)
class _MatchMethodNotAllowed:
    route: Route


_MatchResult = _MatchOk | _MatchNotFound | _MatchMethodNotAllowed
_MATCH_NOT_FOUND = _MatchNotFound()


# --- Internal helpers ---------------------------------------------------


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
    paths: Mapping[URITemplate, Mapping[HTTPMethod, NativeRequestHandler]],
    template_str: str,
) -> URITemplate | None:
    for t in paths:
        if t.template == template_str:
            return t
    return None


def _build_plain_response(
    status_code: int,
    body: bytes,
    *,
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> _NativeResponse:
    return _NativeResponse(
        status_code=status_code,
        headers=[
            (b"content-type", b"text/plain"),
            (b"content-length", str(len(body)).encode("ascii")),
            *extra_headers,
        ],
        reason=_http_phrases.get(status_code, "Unknown").encode("iso-8859-1"),
    )


_NOT_FOUND_BODY = b"Not Found"
_NOT_FOUND_RESPONSE = _build_plain_response(404, _NOT_FOUND_BODY)
_METHOD_NOT_ALLOWED_BODY = b"Method Not Allowed"


def _build_method_not_allowed(allow_header: str) -> tuple[_NativeResponse, bytes]:
    """Pre-build the 405 response for a route. Called once at ``Routes.build()``
    time so the dispatch hot path skips the list / encode / Response build."""
    response = _build_plain_response(
        405,
        _METHOD_NOT_ALLOWED_BODY,
        extra_headers=((b"Allow", allow_header.encode("ascii")),),
    )
    return response, _METHOD_NOT_ALLOWED_BODY
