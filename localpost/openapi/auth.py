"""Concrete :class:`OpFilter` implementations for HTTP authentication.

Each filter runs at request time *and* contributes to the OpenAPI doc:

- :meth:`contribute_root` registers a ``SecurityScheme`` under
  ``components.securitySchemes``.
- :meth:`contribute_operation` adds a ``security`` requirement and a
  ``401`` response.

Identity (the validated principal) is stashed on ``ctx.attrs[filter]``
so user code can pull it via a tiny custom resolver — see the README
for the pattern. The validator is a sync callable returning either the
principal (any object) on success or ``None`` on rejection.

Use either app-wide:

    app = HttpApp(filters=[HttpBearerAuth(validate_token)])

or per-operation:

    @app.get("/me", filters=[HttpBearerAuth(validate_token)])
    def me(ctx: HTTPReqCtx) -> dict: ...
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from localpost.openapi import spec
from localpost.openapi.results import OpResult, Unauthorized

if TYPE_CHECKING:
    from localpost.http.server import HTTPReqCtx
    from localpost.openapi.schemas import SchemaRegistry

__all__ = ["HttpBearerAuth", "HttpBasicAuth"]


_AUTHORIZATION_HEADER = b"authorization"


def _read_authorization(ctx: HTTPReqCtx) -> bytes | None:
    for name, value in ctx.request.headers:
        if name == _AUTHORIZATION_HEADER:
            return value
    return None


def _add_security_scheme(
    doc: spec.OpenAPI, name: str, scheme: spec.SecurityScheme
) -> spec.OpenAPI:
    components = replace(
        doc.components,
        security_schemes={**doc.components.security_schemes, name: scheme},
    )
    return replace(doc, components=components)


def _add_security_requirement(
    op: spec.Operation, scheme_name: str, scopes: tuple[str, ...] = ()
) -> spec.Operation:
    requirement: dict[str, tuple[str, ...]] = {scheme_name: scopes}
    return replace(
        op,
        security=(*op.security, requirement),
        responses={
            "401": spec.Response(description="Unauthorized"),
            **op.responses,
        },
    )


@dataclass(slots=True, eq=False)
class HttpBearerAuth:
    """``Authorization: Bearer <token>`` filter.

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

    def __call__(self, ctx: HTTPReqCtx, /) -> None | OpResult:
        raw = _read_authorization(ctx)
        if raw is None or not raw.startswith(b"Bearer "):
            return Unauthorized("Missing or malformed Authorization header")
        token = raw[7:].decode("ascii", errors="replace")
        principal = self.validator(token)
        if principal is None:
            return Unauthorized("Invalid token")
        ctx.attrs[self] = principal
        return None

    def contribute_root(
        self, doc: spec.OpenAPI, registry: SchemaRegistry, /
    ) -> spec.OpenAPI:
        scheme = spec.SecurityScheme(
            type="http",
            scheme="bearer",
            bearer_format=self.bearer_format,
            description=self.description,
        )
        return _add_security_scheme(doc, self.scheme_name, scheme)

    def contribute_operation(
        self, op: spec.Operation, registry: SchemaRegistry, /
    ) -> spec.Operation:
        return _add_security_requirement(op, self.scheme_name)


@dataclass(slots=True, eq=False)
class HttpBasicAuth:
    """``Authorization: Basic <base64(user:pass)>`` filter.

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

    def __call__(self, ctx: HTTPReqCtx, /) -> None | OpResult:
        raw = _read_authorization(ctx)
        challenge = {"WWW-Authenticate": f'Basic realm="{self.realm}"'}
        if raw is None or not raw.startswith(b"Basic "):
            return Unauthorized(
                "Missing or malformed Authorization header", headers=challenge
            )
        try:
            decoded = base64.b64decode(raw[6:], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return Unauthorized("Malformed Basic credentials", headers=challenge)
        username, sep, password = decoded.partition(":")
        if not sep:
            return Unauthorized("Malformed Basic credentials", headers=challenge)
        principal = self.validator(username, password)
        if principal is None:
            return Unauthorized("Invalid credentials", headers=challenge)
        ctx.attrs[self] = principal
        return None

    def contribute_root(
        self, doc: spec.OpenAPI, registry: SchemaRegistry, /
    ) -> spec.OpenAPI:
        scheme = spec.SecurityScheme(
            type="http", scheme="basic", description=self.description
        )
        return _add_security_scheme(doc, self.scheme_name, scheme)

    def contribute_operation(
        self, op: spec.Operation, registry: SchemaRegistry, /
    ) -> spec.Operation:
        return _add_security_requirement(op, self.scheme_name)
