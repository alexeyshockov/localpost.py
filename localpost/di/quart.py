"""Quart (async Flask) integration"""

from __future__ import annotations

from localpost.di._services import AsyncResolutionContext, ResolutionContext


class RequestContext(AsyncResolutionContext):
    pass


# TODO Implement, enter a new RequestContext scope for each request
