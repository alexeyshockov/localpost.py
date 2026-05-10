"""``HttpAsyncApp`` — async sibling of :class:`localpost.openapi.HttpApp`.

Same decorator API, same OpenAPI doc emission, same ``OpResult``
hierarchy. The user fns are ``async def``; the app deploys to ASGI by
default — :meth:`asgi` returns an ASGI 3 callable suitable for
``uvicorn``, ``hypercorn``, or ``granian --interface asgi``.
:meth:`service` wires up uvicorn under :mod:`localpost.hosting` for
``hosting.run_app(app.service(config))`` parity with the sync flavour.

Transport translation lives in :mod:`localpost.http.asgi` —
:meth:`asgi` is sugar over :func:`localpost.http.to_asgi`. The route
table + 404/405/built-ins are built once in
:meth:`_build_async_handler` as a plain :data:`AsyncRequestHandler`,
exactly mirroring how :class:`HttpApp` builds a sync
:data:`RequestHandler` for :func:`localpost.http.to_wsgi`.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from http import HTTPMethod
from typing import Any, Literal

from localpost import hosting
from localpost.http import AsyncHTTPReqCtx, AsyncRequestHandler, to_asgi, to_rsgi
from localpost.http._types import Response
from localpost.http.asgi import ASGIApp
from localpost.http.router import RouteMatch, URITemplate
from localpost.http.rsgi import RSGIApplication
from localpost.openapi import spec as openapi_spec
from localpost.openapi._docs import redoc_html, scalar_html, swagger_html
from localpost.openapi.adapters import AdapterRegistry, default_registry
from localpost.openapi.aio.middleware import AsyncOpMiddleware
from localpost.openapi.aio.operation import AsyncOperation
from localpost.openapi.schemas import SchemaRegistry

__all__ = ["HttpAsyncApp"]


_FluentDecorator = Callable[[Callable[..., Any]], Callable[..., Any]]
DocsUI = Literal["swagger", "redoc", "scalar", "all"]


class HttpAsyncApp:
    """Async type-driven HTTP application that emits an OpenAPI 3.2 spec.

    Mirrors :class:`localpost.openapi.HttpApp` argument-for-argument; the
    differences are:

    - Handlers must be ``async def`` (or ``async def`` generators for SSE).
    - Middlewares must implement :class:`AsyncOpMiddleware`.
    - :meth:`asgi` returns an ASGI 3 callable; :meth:`service` runs it
      under uvicorn (or hypercorn) via :mod:`localpost.hosting`.

    Args:
        info: Top-level OpenAPI :class:`Info` block.
        middlewares: App-level :class:`AsyncOpMiddleware`-s.
        openapi_path: URL the generated spec is served on; ``None`` to disable.
        docs_path: Base URL for the built-in doc UIs.
        docs_ui: Which doc UIs to mount.
        adapters: Type adapters used for JSON Schema / decode / encode.
        max_body_size: Maximum request-body bytes buffered before
            dispatch. ``-1`` disables the limit. Defaults to 1 MiB.
    """

    def __init__(
        self,
        *,
        info: openapi_spec.Info | None = None,
        middlewares: Sequence[AsyncOpMiddleware] = (),
        openapi_path: str | None = "/openapi.json",
        docs_path: str | None = "/docs",
        docs_ui: DocsUI = "all",
        adapters: AdapterRegistry | None = None,
        max_body_size: int = 1 << 20,
    ) -> None:
        for mw in middlewares:
            _ensure_async_middleware(mw)
        self._info = info or openapi_spec.Info()
        self._middlewares = tuple(middlewares)
        self._openapi_path = openapi_path
        self._docs_path = docs_path
        self._docs_ui = docs_ui
        self._adapters = adapters or default_registry()
        self._max_body_size = max_body_size
        self._operations: list[AsyncOperation] = []
        self._lock = threading.Lock()
        self._cached_spec: openapi_spec.OpenAPI | None = None
        self._cached_spec_bytes: bytes | None = None

    # ----- Decorators -----

    def get(self, path: str, *, middlewares: Sequence[AsyncOpMiddleware] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.GET, path, middlewares)

    def post(self, path: str, *, middlewares: Sequence[AsyncOpMiddleware] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.POST, path, middlewares)

    def put(self, path: str, *, middlewares: Sequence[AsyncOpMiddleware] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.PUT, path, middlewares)

    def delete(self, path: str, *, middlewares: Sequence[AsyncOpMiddleware] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.DELETE, path, middlewares)

    def patch(self, path: str, *, middlewares: Sequence[AsyncOpMiddleware] = ()) -> _FluentDecorator:
        return self._decorator(HTTPMethod.PATCH, path, middlewares)

    def _decorator(
        self,
        method: HTTPMethod,
        path: str,
        op_middlewares: Sequence[AsyncOpMiddleware],
    ) -> _FluentDecorator:
        for mw in op_middlewares:
            _ensure_async_middleware(mw)
        combined = (*self._middlewares, *op_middlewares)

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            op = AsyncOperation.create(method, path, fn, middlewares=combined, adapters=self._adapters)
            with self._lock:
                self._operations.append(op)
                self._cached_spec = None
                self._cached_spec_bytes = None
            return fn

        return deco

    # ----- Spec -----

    @property
    def operations(self) -> Sequence[AsyncOperation]:
        return tuple(self._operations)

    @property
    def openapi_doc(self) -> openapi_spec.OpenAPI:
        """Return the (cached) OpenAPI 3.2 document."""
        with self._lock:
            cached = self._cached_spec
            if cached is not None:
                return cached
            registry = SchemaRegistry(self._adapters)
            doc = openapi_spec.OpenAPI(info=self._info)
            seen: set[int] = set()
            for mw in self._all_middlewares():
                key = id(mw)
                if key in seen:
                    continue
                seen.add(key)
                doc = mw.contribute_root(doc, registry)
            for op in self._operations:
                spec_op = op.build_spec(registry)
                doc = doc.add_operation(op.path, op.method.value, spec_op)
            doc = doc.with_components(
                replace(doc.components, schemas={**doc.components.schemas, **registry.components()})
            )
            self._cached_spec = doc
            return doc

    def _all_middlewares(self):
        yield from self._middlewares
        for op in self._operations:
            yield from op.middlewares

    def _openapi_bytes(self) -> bytes:
        with self._lock:
            cached = self._cached_spec_bytes
            if cached is not None:
                return cached
        body = self.openapi_doc.to_json()
        with self._lock:
            self._cached_spec_bytes = body
        return body

    # ----- Hosting -----

    def asgi(self) -> ASGIApp:
        """Return an ASGI 3 callable that dispatches this app.

        Sugar over :func:`localpost.http.to_asgi` — the app's route
        table plus built-in ``/openapi.json`` and ``/docs`` UIs are
        compiled once into a single :data:`AsyncRequestHandler`, then
        wrapped by the ASGI bridge. Deploy with e.g. ``uvicorn
        myapp:asgi_app`` or ``granian --interface asgi myapp:asgi_app``.
        """
        return to_asgi(self._build_async_handler(), max_body_size=self._max_body_size)

    def as_rsgi(self) -> RSGIApplication:
        """Return an RSGI application object for native Granian deployment.

        Sugar over :func:`localpost.http.to_rsgi`. Same handler chain as
        :meth:`asgi` (route table + built-ins), but wrapped for RSGI
        instead of ASGI — single eager ``response_bytes`` per response,
        zero-copy ``response_file_range`` for sendfile, no pump task on
        streaming uploads. Deploy with::

            granian --interface rsgi myapp:rsgi_app

        For deployments with **other hosted services** (scheduler etc.)
        co-located with the HTTP app inside each Granian worker, use
        :class:`localpost.hosting.rsgi.HostRSGIApp` instead — it drives
        the full hosting lifecycle from Granian's per-worker hooks.
        """
        return to_rsgi(self._build_async_handler(), max_body_size=self._max_body_size)

    def service(
        self,
        config: Any,
        *,
        server: Literal["uvicorn", "hypercorn"] = "uvicorn",
    ) -> hosting.ServiceF:
        """Return a :func:`localpost.hosting.service` running this app.

        ``config`` is the host server's config object —
        :class:`uvicorn.Config` for ``server="uvicorn"`` (the default) or
        :class:`hypercorn.Config` for ``server="hypercorn"``. Both
        servers are configured with the ASGI app from :meth:`asgi`.
        """
        asgi_app = self.asgi()
        if server == "uvicorn":
            from localpost.hosting.services.uvicorn import uvicorn_server

            config.app = asgi_app
            return uvicorn_server(config)
        if server == "hypercorn":
            from localpost.hosting.services.hypercorn import hypercorn_server

            return hypercorn_server(asgi_app, config)
        raise ValueError(f"Unknown ASGI server: {server!r}")

    # ----- Internals -----

    def _build_async_handler(self) -> AsyncRequestHandler:
        """Compile the route table + built-ins into a single
        :data:`AsyncRequestHandler`. Symmetric with
        :meth:`HttpApp._build_router_handler`."""
        bucket: dict[str, _RouteEntry] = {}

        def add(method: HTTPMethod, template: URITemplate, handler: _AsyncMatchedHandler) -> None:
            entry = bucket.setdefault(template.template, _RouteEntry(template=template, methods={}))
            if method in entry.methods:
                raise ValueError(f"Duplicate route: {method.value} {template.template}")
            entry.methods[method] = handler

        for op in self._operations:
            add(op.method, op.template, _async_op_handler(op))

        if self._openapi_path:
            add(
                HTTPMethod.GET,
                URITemplate.parse(self._openapi_path),
                _static_handler(self._openapi_bytes, b"application/json"),
            )
        if self._docs_path and self._openapi_path:
            ui = self._docs_ui
            html_ct = b"text/html; charset=utf-8"
            if ui in ("swagger", "all"):
                add(
                    HTTPMethod.GET,
                    URITemplate.parse(self._docs_path),
                    _static_handler(_const_bytes(swagger_html(self._openapi_path)), html_ct),
                )
            if ui in ("redoc", "all"):
                add(
                    HTTPMethod.GET,
                    URITemplate.parse(f"{self._docs_path}/redoc"),
                    _static_handler(_const_bytes(redoc_html(self._openapi_path)), html_ct),
                )
            if ui in ("scalar", "all"):
                add(
                    HTTPMethod.GET,
                    URITemplate.parse(f"{self._docs_path}/scalar"),
                    _static_handler(_const_bytes(scalar_html(self._openapi_path)), html_ct),
                )

        entries = tuple(bucket.values())

        async def dispatch(ctx: AsyncHTTPReqCtx) -> None:
            path = ctx.request.path.decode("iso-8859-1")
            method_str = ctx.request.method.decode("ascii")
            try:
                method = HTTPMethod(method_str)
            except ValueError:
                method = None

            match = _match(entries, path, method)
            if isinstance(match, _MatchNotFound):
                await ctx.complete(_canned_response(404, b"Not Found"), b"Not Found")
                return
            if isinstance(match, _MatchMethodNotAllowed):
                await ctx.complete(
                    _canned_response(
                        405,
                        b"Method Not Allowed",
                        extra_headers=[(b"allow", match.allow.encode("ascii"))],
                    ),
                    b"Method Not Allowed",
                )
                return

            ctx.attrs[RouteMatch] = RouteMatch(
                method=method or HTTPMethod.GET,
                matched_template=match.template,
                path_args=match.path_args,
            )
            await match.handler(ctx)

        return dispatch


def _ensure_async_middleware(mw: object) -> None:
    """Reject sync :class:`OpMiddleware` instances at registration time.

    The whole point of the split is two parallel pipelines — silently
    treating a sync middleware as async would block the event loop.
    The :class:`AsyncOpMiddleware` Protocol is structural (it shares
    method names with the sync :class:`OpMiddleware`), so isinstance
    isn't enough — we additionally check that ``__call__`` is a
    coroutine function.
    """
    import inspect

    call = getattr(type(mw), "__call__", None)  # noqa: B004 — inspecting unbound method, not testing callability
    if not inspect.iscoroutinefunction(call):
        name = type(mw).__name__
        raise TypeError(
            f"HttpAsyncApp middleware {name!r} is not an AsyncOpMiddleware "
            f"(its ``__call__`` must be ``async def``). "
            f"Use @async_op_middleware (or AsyncHttpBearerAuth / AsyncHttpBasicAuth) for the async app."
        )


# --- Route-table helpers ------------------------------------------------

# Matched-handler shape — same as AsyncRequestHandler but spelled out so
# the route table types stay readable.
type _AsyncMatchedHandler = Callable[[AsyncHTTPReqCtx], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _RouteEntry:
    template: URITemplate
    methods: dict[HTTPMethod, _AsyncMatchedHandler]


@dataclass(frozen=True, slots=True)
class _MatchOk:
    template: URITemplate
    path_args: dict[str, str]
    handler: _AsyncMatchedHandler


@dataclass(frozen=True, slots=True)
class _MatchNotFound:
    pass


@dataclass(frozen=True, slots=True)
class _MatchMethodNotAllowed:
    allow: str


_MATCH_NOT_FOUND = _MatchNotFound()


def _match(
    entries: tuple[_RouteEntry, ...],
    path: str,
    method: HTTPMethod | None,
) -> _MatchOk | _MatchNotFound | _MatchMethodNotAllowed:
    any_template_matched = False
    allow: set[HTTPMethod] = set()
    for entry in entries:
        args = entry.template.match(path)
        if args is None:
            continue
        any_template_matched = True
        allow.update(entry.methods)
        if method is None:
            continue
        handler = entry.methods.get(method)
        if handler is None:
            continue
        return _MatchOk(template=entry.template, path_args=args, handler=handler)
    if any_template_matched:
        return _MatchMethodNotAllowed(allow=", ".join(sorted(m.value for m in allow)))
    return _MATCH_NOT_FOUND


def _async_op_handler(op: AsyncOperation) -> _AsyncMatchedHandler:
    async def run(ctx: AsyncHTTPReqCtx) -> None:
        await op.run(ctx)

    return run


def _static_handler(body_provider: Callable[[], bytes], content_type: bytes) -> _AsyncMatchedHandler:
    async def run(ctx: AsyncHTTPReqCtx) -> None:
        body = body_provider()
        response = Response(
            status_code=200,
            headers=[
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        )
        await ctx.complete(response, body)

    return run


def _const_bytes(value: bytes) -> Callable[[], bytes]:
    def provider() -> bytes:
        return value

    return provider


def _canned_response(
    status: int,
    body: bytes,
    *,
    extra_headers: Sequence[tuple[bytes, bytes]] = (),
) -> Response:
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        *extra_headers,
    ]
    return Response(status_code=status, headers=headers)
