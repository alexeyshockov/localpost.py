"""Re-export the async ctx Protocol from :mod:`localpost.http`.

Kept as a thin alias for back-compat with code that imported
``AsyncHTTPReqCtx`` from this module before the Protocol was hoisted
into ``localpost.http``. New code should import from
``localpost.http`` (or the top-level ``localpost.openapi`` namespace).
"""

from __future__ import annotations

from localpost.http._async_base import AsyncHTTPReqCtx

__all__ = ["AsyncHTTPReqCtx"]
