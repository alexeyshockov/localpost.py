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
  :data:`localpost.http.Response` → as-is, ``(Response, bytes)``
  tuple → with body, ``None`` → 204.
- Worker-pool dispatch for matched routes (each handler runs on a
  worker with a borrowed conn — use :func:`localpost.http.read_body`
  to consume the request body when needed).
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
        profile = json.loads(read_body(ctx))
        return {"updated": name, "profile": profile}


    @app.post("/{name}/avatar")
    def upload_avatar(ctx: HTTPReqCtx, name: str):
        with open(f"/tmp/{name}.jpg", "wb") as f:
            while chunk := ctx.receive(8192):
                f.write(chunk)
        return Response(status_code=204, headers=[(b"content-length", b"0")])


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
from localpost.http._base import HTTPReqCtx, Middleware, RequestHandler, compose
from localpost.http._pool import _Pool, _pool_context
from localpost.http._service import http_server
from localpost.http._types import Response
from localpost.http.config import ServerConfig
from localpost.http.router import Routes, URITemplate, route_match

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


def _wrap_response(value: Any) -> tuple[Response, bytes]:
    """Convert a handler's return value into ``(Response, body)``.

    Supported shapes:

    - ``str`` — ``200 text/plain; charset=utf-8``
    - ``bytes`` / ``bytearray`` / ``memoryview`` — ``200 application/octet-stream``
    - ``dict`` / ``list`` — ``200 application/json`` (via ``json.dumps``)
    - :class:`Response` — passed through, empty body
    - ``(Response, bytes)`` tuple — passed through, with body
    - ``None`` — ``204 No Content``
    """
    if isinstance(value, Response):
        return value, b""
    if value is None:
        return (
            Response(status_code=204, headers=[(b"content-length", b"0")]),
            b"",
        )
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], Response):
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
        f"return str, bytes, dict, list, Response, (Response, bytes), or None"
    )


def _build_response(status: int, content_type: bytes, body: bytes) -> Response:
    return Response(
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


class HttpApp:
    """Decorator-driven HTTP app on top of :class:`Router`.

    Args:
        pooled: When ``True`` (default), each matched-route handler runs
            on a shared worker pool (:func:`thread_pool_handler`). Set to
            ``False`` to run handlers inline on the selector thread —
            only viable when every handler is short and non-blocking
            (no body reads, no slow I/O).
        middleware: App-level middlewares wrapping the entire dispatcher
            (after Router). Outermost-first.
    """

    def __init__(
        self,
        *,
        pooled: bool = True,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        self.pooled = pooled
        self._middleware = tuple(middleware)
        self._routes: list[_Route] = []

    # ----- Decorators -----

    def get(
        self,
        path: str,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.GET, path, middleware)

    def post(
        self,
        path: str,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.POST, path, middleware)

    def put(
        self,
        path: str,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.PUT, path, middleware)

    def delete(
        self,
        path: str,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.DELETE, path, middleware)

    def patch(
        self,
        path: str,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.PATCH, path, middleware)

    def _decorator(
        self,
        method: HTTPMethod,
        path: str,
        middleware: Sequence[Middleware],
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
                )
            )
            return fn  # return original for tests

        return deco

    # ----- Building -----

    def _build_route_handler(self, route: _Route, pool: _Pool | None) -> RequestHandler:
        resolvers = _build_resolvers(route.fn, route.path)
        fn = route.fn

        def inner(ctx: HTTPReqCtx) -> None:
            kwargs = {n: r(ctx) for n, r in resolvers.items()}
            result = fn(**kwargs)
            response, body = _wrap_response(result)
            ctx.complete(response, body)

        handler: RequestHandler = pool.dispatch(inner) if pool is not None else inner  # type: ignore[arg-type]
        return self._with_route_middleware(handler, route.middleware)

    def _with_route_middleware(self, handler: RequestHandler, middleware: tuple[Middleware, ...]) -> RequestHandler:
        if not middleware:
            return handler
        return compose(*middleware)(handler)

    def _build_router_handler(self, pool: _Pool | None) -> RequestHandler:
        routes = Routes()
        for route in self._routes:
            handler = self._build_route_handler(route, pool)
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
        pooled = self.pooled

        @hosting.service
        async def _app_service():
            if not pooled:
                inner = self._build_router_handler(None)
                async with http_server(config, inner, selectors=selectors, acceptor=acceptor):
                    yield
                return
            async with _pool_context() as pool:
                inner = self._build_router_handler(pool)
                async with http_server(config, inner, selectors=selectors, acceptor=acceptor):
                    yield

        return _app_service()
