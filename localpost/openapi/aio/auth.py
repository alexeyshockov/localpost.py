"""Async sibling of :mod:`localpost.openapi.auth`.

Same API surface as the sync :class:`HttpBearerAuth` /
:class:`HttpBasicAuth` — the differences:

- Wraps an ``async def`` middleware (built via :func:`async_op_middleware`).
- The validator may be sync **or** async; async validators are awaited.
- :class:`AsyncOpMiddleware` :meth:`__call__` is async.

OpenAPI contributions are identical to the sync flavour: the
``Authorization`` header parameter and the ``401`` response come from
the wrapped function's signature; the :class:`SecurityScheme` registration
is hand-written.
"""

from __future__ import annotations

import base64
import binascii
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Annotated, Any

from localpost.openapi import spec
from localpost.openapi.aio._ctx import AsyncHTTPReqCtx
from localpost.openapi.aio.middleware import AsyncApiOperation, _AsyncFunctionMiddleware, async_op_middleware
from localpost.openapi.resolvers import FromHeader
from localpost.openapi.results import OpResult, Unauthorized
from localpost.openapi.schemas import SchemaRegistry

__all__ = ["AsyncHttpBearerAuth", "AsyncHttpBasicAuth"]


def _add_security_scheme(doc: spec.OpenAPI, name: str, scheme: spec.SecurityScheme) -> spec.OpenAPI:
    components = replace(
        doc.components,
        security_schemes={**doc.components.security_schemes, name: scheme},
    )
    return replace(doc, components=components)


def _add_security_requirement(op: spec.Operation, scheme_name: str, scopes: tuple[str, ...] = ()) -> spec.Operation:
    requirement: dict[str, tuple[str, ...]] = {scheme_name: scopes}
    return replace(op, security=(*op.security, requirement))


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it's awaitable; otherwise return it unchanged.

    Lets the user pick a sync or async validator without two parallel
    code paths in the auth middleware.
    """
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(slots=True, eq=False)
class AsyncHttpBearerAuth:
    """Async ``Authorization: Bearer <token>`` middleware.

    Args:
        validator: ``token_str -> principal | None`` (sync or async).
            Async validators are awaited.
        scheme_name: Key under ``components.securitySchemes``.
        bearer_format: Hint shown in the OpenAPI doc (e.g. ``"JWT"``).
        description: Optional ``description`` on the security scheme.
    """

    validator: Callable[[str], Any | None] | Callable[[str], Awaitable[Any | None]]
    scheme_name: str = "bearerAuth"
    bearer_format: str = "JWT"
    description: str = ""

    _wrapped: _AsyncFunctionMiddleware = field(init=False, repr=False)

    def __post_init__(self) -> None:
        validator = self.validator
        principal_key = self  # stable identity across requests

        @async_op_middleware
        async def _bearer(
            ctx: AsyncHTTPReqCtx,
            call_next: AsyncApiOperation,
            authorization: Annotated[str, FromHeader("Authorization")] = "",
        ) -> Unauthorized[str] | OpResult:
            if not authorization.startswith("Bearer "):
                return Unauthorized("Missing or malformed Authorization header")
            principal = await _maybe_await(validator(authorization[7:]))
            if principal is None:
                return Unauthorized("Invalid token")
            ctx.attrs[principal_key] = principal
            return await call_next(ctx)

        self._wrapped = _bearer

    async def __call__(self, ctx: AsyncHTTPReqCtx, call_next: AsyncApiOperation, /) -> OpResult:
        return await self._wrapped(ctx, call_next)

    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
        scheme = spec.SecurityScheme(
            type="http",
            scheme="bearer",
            bearer_format=self.bearer_format,
            description=self.description,
        )
        return _add_security_scheme(doc, self.scheme_name, scheme)

    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
        op = self._wrapped.contribute_operation(op, registry)
        return _add_security_requirement(op, self.scheme_name)


@dataclass(slots=True, eq=False)
class AsyncHttpBasicAuth:
    """Async ``Authorization: Basic <base64(user:pass)>`` middleware.

    Args:
        validator: ``(username, password) -> principal | None`` (sync or async).
        scheme_name: Key under ``components.securitySchemes``.
        realm: Realm sent in the ``WWW-Authenticate`` header on 401.
        description: Optional ``description`` on the security scheme.
    """

    validator: Callable[[str, str], Any | None] | Callable[[str, str], Awaitable[Any | None]]
    scheme_name: str = "basicAuth"
    realm: str = "localpost"
    description: str = ""

    _wrapped: _AsyncFunctionMiddleware = field(init=False, repr=False)

    def __post_init__(self) -> None:
        validator = self.validator
        challenge = {"WWW-Authenticate": f'Basic realm="{self.realm}"'}
        principal_key = self

        @async_op_middleware
        async def _basic(
            ctx: AsyncHTTPReqCtx,
            call_next: AsyncApiOperation,
            authorization: Annotated[str, FromHeader("Authorization")] = "",
        ) -> Unauthorized[str] | OpResult:
            if not authorization.startswith("Basic "):
                return Unauthorized("Missing or malformed Authorization header", headers=challenge)
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return Unauthorized("Malformed Basic credentials", headers=challenge)
            username, sep, password = decoded.partition(":")
            if not sep:
                return Unauthorized("Malformed Basic credentials", headers=challenge)
            principal = await _maybe_await(validator(username, password))
            if principal is None:
                return Unauthorized("Invalid credentials", headers=challenge)
            ctx.attrs[principal_key] = principal
            return await call_next(ctx)

        self._wrapped = _basic

    async def __call__(self, ctx: AsyncHTTPReqCtx, call_next: AsyncApiOperation, /) -> OpResult:
        return await self._wrapped(ctx, call_next)

    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
        scheme = spec.SecurityScheme(type="http", scheme="basic", description=self.description)
        return _add_security_scheme(doc, self.scheme_name, scheme)

    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
        op = self._wrapped.contribute_operation(op, registry)
        return _add_security_requirement(op, self.scheme_name)
