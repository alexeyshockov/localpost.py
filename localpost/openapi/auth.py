"""Concrete :class:`OpMiddleware` implementations for HTTP authentication.

Both middlewares are thin wrappers around an
:func:`@op_middleware`-decorated inner function that reads the
``Authorization`` header and validates it. The OpenAPI parameter
(``Authorization``) and the ``401`` response are contributed
*automatically* by ``@op_middleware`` — only the
:class:`SecurityScheme` registration at the root is custom code here.

On success the validated principal is stashed on ``ctx.attrs[<middleware>]``
so user code can pull it via a tiny custom resolver — see the README
for the pattern. The validator is a sync callable returning either the
principal (any object) on success or ``None`` on rejection.

Use either app-wide:

    app = HttpApp(middlewares=[HttpBearerAuth(validate_token)])

or per-operation:

    @app.get("/me", middlewares=[HttpBearerAuth(validate_token)])
    def me(ctx: HTTPReqCtx) -> dict: ...
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Annotated, Any

from localpost.http import HTTPReqCtx
from localpost.openapi import spec
from localpost.openapi.middleware import ApiOperation, _FunctionMiddleware, op_middleware
from localpost.openapi.resolvers import FromHeader
from localpost.openapi.results import OpResult, Unauthorized
from localpost.openapi.schemas import SchemaRegistry

__all__ = ["HttpBearerAuth", "HttpBasicAuth"]


def _add_security_scheme(doc: spec.OpenAPI, name: str, scheme: spec.SecurityScheme) -> spec.OpenAPI:
    components = replace(
        doc.components,
        security_schemes={**doc.components.security_schemes, name: scheme},
    )
    return replace(doc, components=components)


def _add_security_requirement(op: spec.Operation, scheme_name: str, scopes: tuple[str, ...] = ()) -> spec.Operation:
    requirement: dict[str, tuple[str, ...]] = {scheme_name: scopes}
    return replace(op, security=(*op.security, requirement))


@dataclass(slots=True, eq=False)
class HttpBearerAuth:
    """``Authorization: Bearer <token>`` middleware.

    Args:
        validator: ``token_str -> principal | None``. Called for every
            request that carries a ``Bearer`` Authorization header. Return
            anything truthy on success (it gets stashed on
            ``ctx.attrs[self]``); return ``None`` to reject with 401.
        scheme_name: Key under ``components.securitySchemes``. Default
            ``"bearerAuth"``.
        bearer_format: Hint shown in the OpenAPI doc (e.g. ``"JWT"``).
            Default ``"JWT"``.
        description: Optional ``description`` on the security scheme.
    """

    validator: Callable[[str], Any | None]
    scheme_name: str = "bearerAuth"
    bearer_format: str = "JWT"
    description: str = ""

    _wrapped: _FunctionMiddleware = field(init=False, repr=False)

    def __post_init__(self) -> None:
        validator = self.validator
        principal_key = self  # stable identity across requests

        @op_middleware
        def _bearer(
            ctx: HTTPReqCtx,
            call_next: ApiOperation,
            authorization: Annotated[str, FromHeader("Authorization")] = "",
        ) -> Unauthorized[str] | OpResult:
            # Default ``""`` makes the header optional at the resolver level so
            # absence is reported as 401 (with the spec-aware Unauthorized) rather
            # than a generic 400 from FromHeader's "missing required" branch.
            if not authorization.startswith("Bearer "):
                return Unauthorized("Missing or malformed Authorization header")
            principal = validator(authorization[7:])
            if principal is None:
                return Unauthorized("Invalid token")
            ctx.attrs[principal_key] = principal
            return call_next(ctx)

        self._wrapped = _bearer

    def __call__(self, ctx: HTTPReqCtx, call_next: ApiOperation, /) -> OpResult:
        return self._wrapped(ctx, call_next)

    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
        scheme = spec.SecurityScheme(
            type="http",
            scheme="bearer",
            bearer_format=self.bearer_format,
            description=self.description,
        )
        return _add_security_scheme(doc, self.scheme_name, scheme)

    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
        # ``@op_middleware`` already adds the Authorization header parameter
        # and the 401 response from the wrapped function's signature.
        # We only add the security requirement on top.
        op = self._wrapped.contribute_operation(op, registry)
        return _add_security_requirement(op, self.scheme_name)


@dataclass(slots=True, eq=False)
class HttpBasicAuth:
    """``Authorization: Basic <base64(user:pass)>`` middleware.

    Args:
        validator: ``(username, password) -> principal | None``. Called
            for every request that carries a ``Basic`` Authorization
            header. Return anything truthy on success (stashed on
            ``ctx.attrs[self]``); ``None`` to reject with 401.
        scheme_name: Key under ``components.securitySchemes``. Default
            ``"basicAuth"``.
        realm: Realm sent in the ``WWW-Authenticate`` header on 401.
            Default ``"localpost"``.
        description: Optional ``description`` on the security scheme.
    """

    validator: Callable[[str, str], Any | None]
    scheme_name: str = "basicAuth"
    realm: str = "localpost"
    description: str = ""

    _wrapped: _FunctionMiddleware = field(init=False, repr=False)

    def __post_init__(self) -> None:
        validator = self.validator
        challenge = {"WWW-Authenticate": f'Basic realm="{self.realm}"'}
        principal_key = self

        @op_middleware
        def _basic(
            ctx: HTTPReqCtx,
            call_next: ApiOperation,
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
            principal = validator(username, password)
            if principal is None:
                return Unauthorized("Invalid credentials", headers=challenge)
            ctx.attrs[principal_key] = principal
            return call_next(ctx)

        self._wrapped = _basic

    def __call__(self, ctx: HTTPReqCtx, call_next: ApiOperation, /) -> OpResult:
        return self._wrapped(ctx, call_next)

    def contribute_root(self, doc: spec.OpenAPI, registry: SchemaRegistry, /) -> spec.OpenAPI:
        scheme = spec.SecurityScheme(type="http", scheme="basic", description=self.description)
        return _add_security_scheme(doc, self.scheme_name, scheme)

    def contribute_operation(self, op: spec.Operation, registry: SchemaRegistry, /) -> spec.Operation:
        op = self._wrapped.contribute_operation(op, registry)
        return _add_security_requirement(op, self.scheme_name)
