"""Simple application framework on top of LocalPost HTTP server.

`HttpApp` is the user-friendly layer above :class:`localpost.http.Router`
and the HTTP server. It provides:

- Decorators (``.get``, ``.post``, ``.put``, ``.delete``, ``.patch``)
  that register handlers under a URI template.
- Automatic parameter injection: handler params named to match path
  variables get the matched value as ``str``; params annotated as
  :data:`localpost.http.HTTPReqCtx` get the request context.
- Automatic response conversion: ``str`` → ``text/plain``, ``bytes`` →
  ``application/octet-stream``, ``dict`` / ``list`` → ``application/json``,
  :data:`localpost.http.NativeResponse` → as-is, ``(NativeResponse, bytes)``
  tuple → with body, ``None`` → 204.
- Worker-pool dispatch for matched routes.
- Two body modes per route:
  - **Buffered (default):** selector buffers the full body into
    ``ctx.body``; the handler runs on a worker with body in hand.
  - **Streaming (``buffer_body=False``):** the handler runs on a worker
    on a borrowed conn *before* the body is read; reads via
    ``ctx.receive(...)``. Useful for large uploads.
- App-level and per-route middleware composition.

For lower-level control, drop down to :class:`localpost.http.Router` +
hand-written :data:`localpost.http.RequestHandler` s.

Example::

    app = HttpApp()


    @app.get("/{name}")
    def hello(name: str):
        return f"Hello, {name}!"


    @app.post("/{name}/profile")
    def update_profile(ctx: HTTPReqCtx, name: str):
        profile = json.loads(ctx.body)
        return {"updated": name, "profile": profile}


    @app.post("/{name}/avatar", buffer_body=False)
    def upload_avatar(ctx: HTTPReqCtx, name: str):
        # Streaming: ctx.body is empty here; read chunks via ctx.receive(...)
        with open(f"/tmp/{name}.jpg", "wb") as f:
            while chunk := ctx.receive(8192):
                f.write(chunk)
        return NativeResponse(status_code=204, headers=[(b"content-length", b"0")])


    sys.exit(run_app(app.service(ServerConfig(host="127.0.0.1", port=8000))))
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPMethod
from typing import Any, get_type_hints

from localpost import hosting
from localpost.http._pool import _Pool, _pool_context
from localpost.http._service import http_server
from localpost.http._types import Response as NativeResponse
from localpost.http.config import ServerConfig
from localpost.http.router import Routes, URITemplate, route_match
from localpost.http.server import BodyHandler, HTTPReqCtx, Middleware, RequestHandler, compose

__all__ = ["HttpApp"]


_ParamResolver = Callable[[HTTPReqCtx], Any]


def _build_resolvers(fn: Callable[..., Any], path: str) -> dict[str, _ParamResolver]:
    """Inspect ``fn``'s signature once, build a name → resolver dict.

    Resolution rules (checked in order per parameter):
      1. Param name matches a ``{var}`` in the path template → injected
         as the matched ``str``.
      2. Annotated as :data:`HTTPReqCtx` → injected as the request ctx.

    Anything else fails at registration time.
    """
    template_vars = set(URITemplate.parse(path).variable_names)
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:  # noqa: BLE001
        hints = {}
    fn_name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))

    resolvers: dict[str, _ParamResolver] = {}
    for name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise ValueError(f"handler {fn_name!r}: *args / **kwargs not supported in handler signatures")
        if name in template_vars:
            resolvers[name] = _make_path_arg_resolver(name)
            continue
        ann = hints.get(name)
        if ann is HTTPReqCtx:
            resolvers[name] = _resolve_ctx
            continue
        raise ValueError(
            f"handler {fn_name!r}: cannot resolve parameter {name!r} "
            f"(not a path variable {sorted(template_vars)!r}, not annotated as HTTPReqCtx)"
        )
    return resolvers


def _resolve_ctx(ctx: HTTPReqCtx) -> HTTPReqCtx:
    return ctx


def _make_path_arg_resolver(name: str) -> _ParamResolver:
    return lambda ctx: route_match(ctx).path_args[name]


def _wrap_response(value: Any) -> tuple[NativeResponse, bytes]:
    """Convert a handler's return value into ``(NativeResponse, body)``.

    Supported shapes:

    - ``str`` — ``200 text/plain; charset=utf-8``
    - ``bytes`` / ``bytearray`` / ``memoryview`` — ``200 application/octet-stream``
    - ``dict`` / ``list`` — ``200 application/json`` (via ``json.dumps``)
    - :class:`NativeResponse` — passed through, empty body
    - ``(NativeResponse, bytes)`` tuple — passed through, with body
    - ``None`` — ``204 No Content``
    """
    if isinstance(value, NativeResponse):
        return value, b""
    if value is None:
        return (
            NativeResponse(status_code=204, headers=[(b"content-length", b"0")]),
            b"",
        )
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], NativeResponse):
        response, body = value
        return response, body if isinstance(body, bytes) else bytes(body)
    if isinstance(value, str):
        body = value.encode("utf-8")
        return _build_response(200, b"text/plain; charset=utf-8", body), body
    if isinstance(value, (bytes, bytearray, memoryview)):
        body = bytes(value)
        return _build_response(200, b"application/octet-stream", body), body
    if isinstance(value, (dict, list)):
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return _build_response(200, b"application/json", body), body
    raise TypeError(
        f"unsupported return type {type(value).__name__!r} from handler — "
        f"return str, bytes, dict, list, NativeResponse, (NativeResponse, bytes), or None"
    )


def _build_response(status: int, content_type: bytes, body: bytes) -> NativeResponse:
    return NativeResponse(
        status_code=status,
        headers=[
            (b"content-type", content_type),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    )


@dataclass(slots=True)
class _Route:
    """One registered route — the inputs needed to wire it at service time."""

    method: HTTPMethod
    path: str
    fn: Callable[..., Any]
    middleware: tuple[Middleware, ...]
    buffer_body: bool


class HttpApp:
    """Decorator-driven HTTP app on top of :class:`Router`.

    Args:
        max_concurrency: Worker-pool size. Default 32. Set to ``0`` to
            disable the pool — buffered handlers then run inline on the
            selector thread (only viable when every handler is short
            and non-blocking). Streaming routes (``buffer_body=False``)
            require a pool — registering one with ``max_concurrency=0``
            raises at service-startup time.
        backlog: Additional channel buffer between selector and workers.
            Default ``0`` = rendezvous: a request is dispatched only when
            a worker is currently waiting; otherwise the selector replies
            503 immediately. ``backlog=K`` allows up to K extra requests
            to sit in the channel before 503s start. Total system capacity
            is ``max_concurrency + backlog``.
        middleware: App-level middlewares wrapping the entire dispatcher
            (after Router). Outermost-first.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = 32,
        backlog: int = 0,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        if max_concurrency < 0:
            raise ValueError("max_concurrency must be >= 0")
        if backlog < 0:
            raise ValueError("backlog must be >= 0")
        self.max_concurrency = max_concurrency
        self.backlog = backlog
        self._middleware = tuple(middleware)
        self._routes: list[_Route] = []

    # ----- Decorators -----

    def get(
        self,
        path: str,
        *,
        middleware: Sequence[Middleware] = (),
        buffer_body: bool = True,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.GET, path, middleware, buffer_body)

    def post(
        self,
        path: str,
        *,
        middleware: Sequence[Middleware] = (),
        buffer_body: bool = True,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.POST, path, middleware, buffer_body)

    def put(
        self,
        path: str,
        *,
        middleware: Sequence[Middleware] = (),
        buffer_body: bool = True,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.PUT, path, middleware, buffer_body)

    def delete(
        self,
        path: str,
        *,
        middleware: Sequence[Middleware] = (),
        buffer_body: bool = True,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.DELETE, path, middleware, buffer_body)

    def patch(
        self,
        path: str,
        *,
        middleware: Sequence[Middleware] = (),
        buffer_body: bool = True,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.PATCH, path, middleware, buffer_body)

    def _decorator(
        self,
        method: HTTPMethod,
        path: str,
        middleware: Sequence[Middleware],
        buffer_body: bool,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            # Validate signature eagerly so registration-time errors fire
            # at decoration, not at service() time.
            _build_resolvers(fn, path)
            self._routes.append(
                _Route(
                    method=method,
                    path=path,
                    fn=fn,
                    middleware=tuple(middleware),
                    buffer_body=buffer_body,
                )
            )
            return fn  # return original for tests

        return deco

    # ----- Building -----

    def _build_buffered_handler(self, route: _Route, pool: _Pool | None) -> RequestHandler:
        """Buffered route: pre-body returns BodyHandler that, if pooled,
        dispatches to a worker; otherwise runs inline on selector."""
        resolvers = _build_resolvers(route.fn, route.path)
        fn = route.fn

        def post_body(ctx: HTTPReqCtx) -> None:
            kwargs = {n: r(ctx) for n, r in resolvers.items()}
            result = fn(**kwargs)
            response, body = _wrap_response(result)
            ctx.complete(response, body)

        if pool is not None:
            dispatched: BodyHandler = pool.dispatch_buffered(post_body)

            def pre_body_pooled(_ctx: HTTPReqCtx) -> BodyHandler:
                return dispatched

            return self._with_route_middleware(pre_body_pooled, route.middleware)

        def pre_body_inline(_ctx: HTTPReqCtx) -> BodyHandler:
            return post_body

        return self._with_route_middleware(pre_body_inline, route.middleware)

    def _build_streaming_handler(self, route: _Route, pool: _Pool) -> RequestHandler:
        """Streaming route: pre-body borrows + queues for a worker,
        which runs the user fn on a blocking conn (body not buffered).
        """
        resolvers = _build_resolvers(route.fn, route.path)
        fn = route.fn

        def streaming_inner(ctx: HTTPReqCtx) -> None:
            kwargs = {n: r(ctx) for n, r in resolvers.items()}
            result = fn(**kwargs)
            response, body = _wrap_response(result)
            ctx.complete(response, body)

        return self._with_route_middleware(pool.dispatch_streaming(streaming_inner), route.middleware)

    def _with_route_middleware(self, handler: RequestHandler, middleware: tuple[Middleware, ...]) -> RequestHandler:
        if not middleware:
            return handler
        return compose(*middleware)(handler)

    def _build_router_handler(self, pool: _Pool | None) -> RequestHandler:
        routes = Routes()
        for route in self._routes:
            if route.buffer_body:
                handler = self._build_buffered_handler(route, pool)
            else:
                if pool is None:
                    raise RuntimeError(
                        f"streaming route {route.method.value} {route.path!r} requires "
                        f"a worker pool (HttpApp(max_concurrency > 0))"
                    )
                handler = self._build_streaming_handler(route, pool)
            routes.add(route.method, route.path, handler)
        router = routes.build().as_handler()
        if self._middleware:
            router = compose(*self._middleware)(router)
        return router

    # ----- Hosting -----

    def service(
        self,
        config: ServerConfig,
        *,
        selectors: int = 1,
        acceptor: bool = False,
    ):
        """Return a :func:`localpost.hosting.service` that runs the app.

        Composes worker pool + the HTTP server (backend selected via
        :attr:`ServerConfig.backend`). Use with
        :func:`localpost.hosting.run_app` or :func:`localpost.hosting.serve`.

        ``selectors`` and ``acceptor`` forward to :func:`http_server` — see
        its docstring for the full topology rules.
        """
        max_concurrency = self.max_concurrency
        backlog = self.backlog

        @hosting.service
        async def _app_service():
            if max_concurrency == 0:
                inner = self._build_router_handler(None)
                async with http_server(config, inner, selectors=selectors, acceptor=acceptor):
                    yield
                return
            async with _pool_context(max_concurrency, backlog) as pool:
                inner = self._build_router_handler(pool)
                async with http_server(config, inner, selectors=selectors, acceptor=acceptor):
                    yield

        return _app_service()
