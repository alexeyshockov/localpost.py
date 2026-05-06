"""Native Flask adapter — drives a Flask app without going through WSGI.

Compared to :func:`localpost.http.wrap_wsgi` / :func:`localpost.http.wsgi_server`:

- Flask's request context stays active for the **entire** request lifetime,
  including response-body iteration. Generators returned from a view can use
  ``flask.request`` / ``session`` / ``g`` without ``@stream_with_context``.
  ``stream_with_context`` still works (becomes a no-op).
- ``teardown_request`` / ``teardown_appcontext`` callbacks run **after** the
  response body has been fully sent, not before — the opposite of standard
  WSGI. This means DB sessions and similar resources live through streaming,
  and teardown sees the true end of the request.

Trade-off: this couples to Flask/Werkzeug internals
(``app.request_context``, ``app.full_dispatch_request``, ``app.handle_exception``,
``werkzeug.Response.iter_encoded``). All documented, stable across Flask 3.x.
"""

from __future__ import annotations

from flask import Flask

from localpost.http._base import HTTPReqCtx, RequestHandler
from localpost.http._body import read_body
from localpost.http._service import http_server
from localpost.http._types import Response as _Response
from localpost.http.config import ServerConfig
from localpost.http.wsgi import _build_environ

__all__ = ["flask_handler", "flask_server"]


def flask_handler(app: Flask) -> RequestHandler:
    """Wrap a Flask app as a native :class:`RequestHandler`.

    The handler reads the full request body via
    :func:`localpost.http.read_body`, exposes it to Flask as
    ``wsgi.input``, and streams the response straight to h11.

    Reading the body blocks on the request socket, so this handler
    **must** be composed with :func:`localpost.http.thread_pool_handler`
    (or run inside an explicit ``ctx.borrow()`` block) — running it on
    the selector thread will stall the loop while the upload finishes.

    See the module docstring for behaviour differences vs. WSGI — notably,
    the request context stays active during response body streaming.
    """

    def run_flask(http_ctx: HTTPReqCtx) -> None:
        body = read_body(http_ctx)
        environ = _build_environ(http_ctx, body)
        with app.request_context(environ):
            try:
                response = app.full_dispatch_request()
            except Exception as exc:  # noqa: BLE001
                # app.handle_exception returns a Response; it only re-raises in
                # debug / propagate_exceptions mode. If it does re-raise, let
                # the exception bubble up to the service task group.
                response = app.handle_exception(exc)
            try:
                _write_response(http_ctx, response)
            finally:
                response.close()  # Fire werkzeug's call_on_close callbacks

    return run_flask


def _write_response(http_ctx: HTTPReqCtx, response) -> None:
    # response is a werkzeug.Response (Flask's Response subclasses it)
    reason = (response.status.split(" ", 1)[1] if " " in response.status else "").encode("iso-8859-1")
    wire_headers = [(name.encode("iso-8859-1"), value.encode("iso-8859-1")) for name, value in response.headers.items()]
    http_ctx.stream(
        _Response(
            status_code=response.status_code,
            headers=wire_headers,
            reason=reason,
        ),
        (chunk for chunk in response.iter_encoded() if chunk),
    )


def flask_server(config: ServerConfig, app: Flask, /):
    """Hosted service serving a Flask app via :func:`flask_handler`.

    Flask views block during request handling, so to serve more than one
    request at a time wrap the handler with
    :func:`localpost.http.thread_pool_handler`::

        async with thread_pool_handler(flask_handler(app)) as h:
            async with http_server(config, h):
                ...
    """
    return http_server(config, flask_handler(app))
