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
  :data:`localpost.http.NativeResponse` → as-is, ``None`` → 204.
- Worker-pool dispatch for matched routes via the existing
  :func:`localpost.http.thread_pool_handler` machinery — bodies are
  buffered into ``ctx.body`` and the user handler runs on a worker.
- App-level and per-route middleware composition via the standard
  :data:`localpost.http.Middleware` decorator pattern.

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

    sys.exit(run_app(app.service(ServerConfig(host="127.0.0.1", port=8000))))
"""

from __future__ import annotations

import inspect
import json
import sys
from collections.abc import Callable, Sequence
from http import HTTPMethod
from typing import Any, Literal, get_type_hints

from localpost import hosting
from localpost.http._pool import thread_pool_handler
from localpost.http._service import http_server, httptools_server
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
            raise ValueError(
                f"handler {fn_name!r}: *args / **kwargs not supported in handler signatures"
            )
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
    - :class:`NativeResponse` — passed through, empty body (caller is
      expected to declare ``Content-Length: 0``)
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


_BACKEND_FACTORIES = {"h11": http_server, "httptools": httptools_server}


class HttpApp:
    """Decorator-driven HTTP app on top of :class:`Router`.

    Args:
        max_concurrency: Worker-pool size for body-handler continuations.
            Default 32. Set to ``0`` to disable the pool — handlers then
            run inline on the selector thread (only viable when every
            handler is short and non-blocking).
        middleware: App-level middlewares wrapping the entire dispatcher
            (after Router). Outermost-first.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = 32,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        if max_concurrency < 0:
            raise ValueError("max_concurrency must be >= 0")
        self.max_concurrency = max_concurrency
        self._middleware = tuple(middleware)
        self._routes = Routes()

    # ----- Decorators -----

    def get(
        self, path: str, *, middleware: Sequence[Middleware] = ()
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.GET, path, middleware)

    def post(
        self, path: str, *, middleware: Sequence[Middleware] = ()
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.POST, path, middleware)

    def put(
        self, path: str, *, middleware: Sequence[Middleware] = ()
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.PUT, path, middleware)

    def delete(
        self, path: str, *, middleware: Sequence[Middleware] = ()
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.DELETE, path, middleware)

    def patch(
        self, path: str, *, middleware: Sequence[Middleware] = ()
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._decorator(HTTPMethod.PATCH, path, middleware)

    def _decorator(
        self,
        method: HTTPMethod,
        path: str,
        middleware: Sequence[Middleware],
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            handler = self._wrap_user_handler(fn, path)
            if middleware:
                handler = compose(*middleware)(handler)
            self._routes.add(method, path, handler)
            return fn  # return original for tests

        return deco

    def _wrap_user_handler(self, fn: Callable[..., Any], path: str) -> RequestHandler:
        """Build a :data:`RequestHandler` that resolves params, calls the
        user handler post-body, and converts the return value to a wire
        response."""
        resolvers = _build_resolvers(fn, path)

        def post_body(ctx: HTTPReqCtx) -> None:
            kwargs = {n: r(ctx) for n, r in resolvers.items()}
            result = fn(**kwargs)
            response, body = _wrap_response(result)
            ctx.complete(response, body)

        def pre_body(_ctx: HTTPReqCtx) -> BodyHandler:
            return post_body

        return pre_body

    # ----- Building / hosting -----

    def as_router_handler(self) -> RequestHandler:
        """Build the bare Router-as-handler chain — Router + app-level
        middleware, no worker pool.

        Useful when you want to compose your own pool / lifecycle around
        the dispatcher. For the default hosted setup, use
        :meth:`service`.
        """
        handler = self._routes.build().as_handler()
        if self._middleware:
            handler = compose(*self._middleware)(handler)
        return handler

    def service(
        self,
        config: ServerConfig,
        *,
        backend: Literal["h11", "httptools"] = "h11",
        selectors: int = 1,
    ):
        """Return a :func:`localpost.hosting.service` that runs the app.

        Composes worker pool + chosen backend's server. Use with
        :func:`localpost.hosting.run_app` or :func:`localpost.hosting.serve`.

        ``selectors`` is forwarded to the underlying server — see
        :func:`localpost.http.http_server` for the multi-selector contract.
        """
        if backend not in _BACKEND_FACTORIES:
            raise ValueError(f"unknown backend {backend!r} (expected 'h11' or 'httptools')")
        server_fn = _BACKEND_FACTORIES[backend]
        inner = self.as_router_handler()
        max_concurrency = self.max_concurrency

        @hosting.service
        async def _app_service():
            if max_concurrency == 0:
                # Pool disabled — handlers run inline on the selector thread.
                # Body-handler continuations still fire after the selector
                # buffers the body; they just don't hop to a worker.
                async with server_fn(config, inner, selectors=selectors):
                    yield
                return
            async with thread_pool_handler(inner, max_concurrency=max_concurrency) as wrapped:
                async with server_fn(config, wrapped, selectors=selectors):
                    yield

        return _app_service()


# ---- Example usage ----------------------------------------------------

if __name__ == "__main__":
    app = HttpApp()

    @app.get("/{name}")
    def hello(name: str):
        return f"Hello, {name}!"

    @app.post("/{name}/profile")
    def update_user_profile(ctx: HTTPReqCtx, name: str):
        profile = json.loads(ctx.body)
        return {"updated_for": name, "profile": profile}

    sys.exit(hosting.run_app(app.service(ServerConfig(host="127.0.0.1", port=8000))))
