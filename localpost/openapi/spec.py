"""Immutable dataclasses for the OpenAPI 3.2 specification.

The dataclasses are version-agnostic enough that a 3.1 / 3.0 serializer
could be added later as a sibling of :meth:`OpenAPI.to_dict` without
touching the data shape. v1 emits 3.2 only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

import msgspec

ParameterLocation = Literal["query", "header", "path", "cookie"]
SecuritySchemeType = Literal["apiKey", "http", "oauth2", "openIdConnect", "mutualTLS"]


@dataclass(frozen=True, slots=True)
class Reference:
    """JSON ``$ref`` to another component."""

    ref: str
    summary: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"$ref": self.ref}
        if self.summary:
            d["summary"] = self.summary
        if self.description:
            d["description"] = self.description
        return d


@dataclass(frozen=True, slots=True)
class Contact:
    name: str = ""
    url: str = ""
    email: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.name:
            d["name"] = self.name
        if self.url:
            d["url"] = self.url
        if self.email:
            d["email"] = self.email
        return d


@dataclass(frozen=True, slots=True)
class License:
    name: str
    identifier: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.identifier:
            d["identifier"] = self.identifier
        if self.url:
            d["url"] = self.url
        return d


@dataclass(frozen=True, slots=True)
class Info:
    title: str = "API"
    version: str = "0.1.0"
    summary: str = ""
    description: str = ""
    terms_of_service: str = ""
    contact: Contact | None = None
    license: License | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"title": self.title, "version": self.version}
        if self.summary:
            d["summary"] = self.summary
        if self.description:
            d["description"] = self.description
        if self.terms_of_service:
            d["termsOfService"] = self.terms_of_service
        if self.contact:
            d["contact"] = self.contact.to_dict()
        if self.license:
            d["license"] = self.license.to_dict()
        return d


@dataclass(frozen=True, slots=True)
class ServerVariable:
    default: str
    enum: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"default": self.default}
        if self.enum:
            d["enum"] = list(self.enum)
        if self.description:
            d["description"] = self.description
        return d


@dataclass(frozen=True, slots=True)
class Server:
    url: str
    description: str = ""
    variables: dict[str, ServerVariable] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"url": self.url}
        if self.description:
            d["description"] = self.description
        if self.variables:
            d["variables"] = {k: v.to_dict() for k, v in self.variables.items()}
        return d


@dataclass(frozen=True, slots=True)
class ExternalDocs:
    url: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"url": self.url}
        if self.description:
            d["description"] = self.description
        return d


@dataclass(frozen=True, slots=True)
class Tag:
    name: str
    description: str = ""
    summary: str = ""
    external_docs: ExternalDocs | None = None
    parent: str = ""
    """3.2 addition: name of the parent tag for nested grouping."""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.summary:
            d["summary"] = self.summary
        if self.description:
            d["description"] = self.description
        if self.external_docs:
            d["externalDocs"] = self.external_docs.to_dict()
        if self.parent:
            d["parent"] = self.parent
        return d


@dataclass(frozen=True, slots=True)
class TagGroup:
    """3.2 addition: tag group declared at the root level."""

    name: str
    tags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "tags": list(self.tags)}


@dataclass(frozen=True, slots=True)
class Encoding:
    content_type: str = ""
    headers: dict[str, Header | Reference] = field(default_factory=dict)
    style: str = ""
    explode: bool | None = None
    allow_reserved: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.content_type:
            d["contentType"] = self.content_type
        if self.headers:
            d["headers"] = {k: v.to_dict() for k, v in self.headers.items()}
        if self.style:
            d["style"] = self.style
        if self.explode is not None:
            d["explode"] = self.explode
        if self.allow_reserved:
            d["allowReserved"] = True
        return d


@dataclass(frozen=True, slots=True)
class Example:
    summary: str = ""
    description: str = ""
    value: Any = None
    external_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.summary:
            d["summary"] = self.summary
        if self.description:
            d["description"] = self.description
        if self.value is not None:
            d["value"] = self.value
        if self.external_value:
            d["externalValue"] = self.external_value
        return d


@dataclass(frozen=True, slots=True)
class MediaType:
    schema: dict[str, Any] = field(default_factory=dict)
    example: Any = None
    examples: dict[str, Example | Reference] = field(default_factory=dict)
    encoding: dict[str, Encoding] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.schema:
            d["schema"] = self.schema
        if self.example is not None:
            d["example"] = self.example
        if self.examples:
            d["examples"] = {k: v.to_dict() for k, v in self.examples.items()}
        if self.encoding:
            d["encoding"] = {k: v.to_dict() for k, v in self.encoding.items()}
        return d


@dataclass(frozen=True, slots=True)
class Header:
    description: str = ""
    required: bool = False
    deprecated: bool = False
    schema: dict[str, Any] = field(default_factory=dict)
    content: dict[str, MediaType] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.description:
            d["description"] = self.description
        if self.required:
            d["required"] = True
        if self.deprecated:
            d["deprecated"] = True
        if self.schema:
            d["schema"] = self.schema
        if self.content:
            d["content"] = {k: v.to_dict() for k, v in self.content.items()}
        return d


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    location: ParameterLocation = "query"
    required: bool = False
    description: str = ""
    deprecated: bool = False
    schema: dict[str, Any] = field(default_factory=dict)
    style: str = ""
    explode: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "in": self.location}
        if self.required:
            d["required"] = True
        if self.description:
            d["description"] = self.description
        if self.deprecated:
            d["deprecated"] = True
        if self.schema:
            d["schema"] = self.schema
        if self.style:
            d["style"] = self.style
        if self.explode is not None:
            d["explode"] = self.explode
        return d


@dataclass(frozen=True, slots=True)
class RequestBody:
    description: str = ""
    required: bool = False
    content: dict[str, MediaType] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.description:
            d["description"] = self.description
        if self.required:
            d["required"] = True
        if self.content:
            d["content"] = {k: v.to_dict() for k, v in self.content.items()}
        return d


@dataclass(frozen=True, slots=True)
class Response:
    description: str = ""
    headers: dict[str, Header | Reference] = field(default_factory=dict)
    content: dict[str, MediaType] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"description": self.description}
        if self.headers:
            d["headers"] = {k: v.to_dict() for k, v in self.headers.items()}
        if self.content:
            d["content"] = {k: v.to_dict() for k, v in self.content.items()}
        return d


SecurityRequirement = dict[str, tuple[str, ...]]
"""``{schemeName: (scope1, scope2, ...)}`` — multiple keys = AND; multiple
elements in the surrounding tuple = OR."""


@dataclass(frozen=True, slots=True)
class Operation:
    summary: str = ""
    operation_id: str = ""
    description: str = ""
    parameters: tuple[Parameter | Reference, ...] = ()
    request_body: RequestBody | Reference | None = None
    responses: dict[str, Response | Reference] = field(default_factory=dict)
    deprecated: bool = False
    tags: tuple[str, ...] = ()
    security: tuple[SecurityRequirement, ...] = ()
    servers: tuple[Server, ...] = ()
    external_docs: ExternalDocs | None = None
    callbacks: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.summary:
            d["summary"] = self.summary
        if self.operation_id:
            d["operationId"] = self.operation_id
        if self.description:
            d["description"] = self.description
        if self.tags:
            d["tags"] = list(self.tags)
        if self.parameters:
            d["parameters"] = [p.to_dict() for p in self.parameters]
        if self.request_body is not None:
            d["requestBody"] = self.request_body.to_dict()
        if self.responses:
            d["responses"] = {code: r.to_dict() for code, r in self.responses.items()}
        if self.callbacks:
            d["callbacks"] = dict(self.callbacks)
        if self.deprecated:
            d["deprecated"] = True
        if self.security:
            d["security"] = [{k: list(v) for k, v in req.items()} for req in self.security]
        if self.servers:
            d["servers"] = [s.to_dict() for s in self.servers]
        if self.external_docs:
            d["externalDocs"] = self.external_docs.to_dict()
        return d


@dataclass(frozen=True, slots=True)
class PathItem:
    summary: str = ""
    description: str = ""
    operations: dict[str, Operation] = field(default_factory=dict)
    """Keyed by lowercase HTTP method: get, post, put, delete, patch,
    head, options, trace, query (3.2)."""
    parameters: tuple[Parameter | Reference, ...] = ()
    servers: tuple[Server, ...] = ()
    ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        if self.ref:
            return {"$ref": self.ref}
        d: dict[str, Any] = {}
        if self.summary:
            d["summary"] = self.summary
        if self.description:
            d["description"] = self.description
        for method, op in self.operations.items():
            d[method] = op.to_dict()
        if self.parameters:
            d["parameters"] = [p.to_dict() for p in self.parameters]
        if self.servers:
            d["servers"] = [s.to_dict() for s in self.servers]
        return d


# --- Security schemes -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class OAuthFlow:
    scopes: dict[str, str] = field(default_factory=dict)
    authorization_url: str = ""
    token_url: str = ""
    refresh_url: str = ""
    device_authorization_url: str = ""
    """3.2 addition: device-authorization URL for the device flow."""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"scopes": dict(self.scopes)}
        if self.authorization_url:
            d["authorizationUrl"] = self.authorization_url
        if self.token_url:
            d["tokenUrl"] = self.token_url
        if self.refresh_url:
            d["refreshUrl"] = self.refresh_url
        if self.device_authorization_url:
            d["deviceAuthorizationUrl"] = self.device_authorization_url
        return d


@dataclass(frozen=True, slots=True)
class OAuthFlows:
    implicit: OAuthFlow | None = None
    password: OAuthFlow | None = None
    client_credentials: OAuthFlow | None = None
    authorization_code: OAuthFlow | None = None
    device_authorization: OAuthFlow | None = None
    """3.2 addition."""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.implicit:
            d["implicit"] = self.implicit.to_dict()
        if self.password:
            d["password"] = self.password.to_dict()
        if self.client_credentials:
            d["clientCredentials"] = self.client_credentials.to_dict()
        if self.authorization_code:
            d["authorizationCode"] = self.authorization_code.to_dict()
        if self.device_authorization:
            d["deviceAuthorization"] = self.device_authorization.to_dict()
        return d


@dataclass(frozen=True, slots=True)
class SecurityScheme:
    type: SecuritySchemeType
    description: str = ""
    name: str = ""
    """``apiKey``: parameter name."""
    location: Literal["query", "header", "cookie", ""] = ""
    """``apiKey``: where the key lives."""
    scheme: str = ""
    """``http``: e.g. ``basic`` / ``bearer``."""
    bearer_format: str = ""
    flows: OAuthFlows | None = None
    open_id_connect_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type}
        if self.description:
            d["description"] = self.description
        if self.name:
            d["name"] = self.name
        if self.location:
            d["in"] = self.location
        if self.scheme:
            d["scheme"] = self.scheme
        if self.bearer_format:
            d["bearerFormat"] = self.bearer_format
        if self.flows:
            d["flows"] = self.flows.to_dict()
        if self.open_id_connect_url:
            d["openIdConnectUrl"] = self.open_id_connect_url
        return d


@dataclass(frozen=True, slots=True)
class Components:
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    responses: dict[str, Response | Reference] = field(default_factory=dict)
    parameters: dict[str, Parameter | Reference] = field(default_factory=dict)
    request_bodies: dict[str, RequestBody | Reference] = field(default_factory=dict)
    headers: dict[str, Header | Reference] = field(default_factory=dict)
    security_schemes: dict[str, SecurityScheme | Reference] = field(default_factory=dict)
    examples: dict[str, Example | Reference] = field(default_factory=dict)
    path_items: dict[str, PathItem] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.schemas:
            d["schemas"] = dict(self.schemas)
        if self.responses:
            d["responses"] = {k: v.to_dict() for k, v in self.responses.items()}
        if self.parameters:
            d["parameters"] = {k: v.to_dict() for k, v in self.parameters.items()}
        if self.request_bodies:
            d["requestBodies"] = {k: v.to_dict() for k, v in self.request_bodies.items()}
        if self.headers:
            d["headers"] = {k: v.to_dict() for k, v in self.headers.items()}
        if self.security_schemes:
            d["securitySchemes"] = {k: v.to_dict() for k, v in self.security_schemes.items()}
        if self.examples:
            d["examples"] = {k: v.to_dict() for k, v in self.examples.items()}
        if self.path_items:
            d["pathItems"] = {k: v.to_dict() for k, v in self.path_items.items()}
        return d

    def is_empty(self) -> bool:
        return not (
            self.schemas
            or self.responses
            or self.parameters
            or self.request_bodies
            or self.headers
            or self.security_schemes
            or self.examples
            or self.path_items
        )


@dataclass(frozen=True, slots=True)
class OpenAPI:
    """Root of an OpenAPI 3.2 document."""

    openapi: str = "3.2.0"
    info: Info = field(default_factory=Info)
    servers: tuple[Server, ...] = ()
    paths: dict[str, PathItem] = field(default_factory=dict)
    webhooks: dict[str, PathItem | Reference] = field(default_factory=dict)
    components: Components = field(default_factory=Components)
    security: tuple[SecurityRequirement, ...] = ()
    tags: tuple[Tag, ...] = ()
    tag_groups: tuple[TagGroup, ...] = ()
    """3.2 addition."""
    external_docs: ExternalDocs | None = None
    json_schema_dialect: str = ""

    def add_operation(self, path: str, method: str, operation: Operation) -> OpenAPI:
        """Return a new :class:`OpenAPI` with ``operation`` registered at
        ``method`` (case-insensitive) on ``path``."""
        m = method.lower()
        new_paths = dict(self.paths)
        existing = new_paths.get(path)
        if existing is None:
            new_paths[path] = PathItem(operations={m: operation})
        else:
            new_ops = dict(existing.operations)
            new_ops[m] = operation
            new_paths[path] = replace(existing, operations=new_ops)
        return replace(self, paths=new_paths)

    def with_components(self, components: Components) -> OpenAPI:
        return replace(self, components=components)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"openapi": self.openapi, "info": self.info.to_dict()}
        if self.json_schema_dialect:
            d["jsonSchemaDialect"] = self.json_schema_dialect
        if self.servers:
            d["servers"] = [s.to_dict() for s in self.servers]
        if self.paths:
            d["paths"] = {p: item.to_dict() for p, item in self.paths.items()}
        if self.webhooks:
            d["webhooks"] = {k: v.to_dict() for k, v in self.webhooks.items()}
        if not self.components.is_empty():
            d["components"] = self.components.to_dict()
        if self.security:
            d["security"] = [{k: list(v) for k, v in req.items()} for req in self.security]
        if self.tags:
            d["tags"] = [t.to_dict() for t in self.tags]
        if self.tag_groups:
            d["tagGroups"] = [g.to_dict() for g in self.tag_groups]
        if self.external_docs:
            d["externalDocs"] = self.external_docs.to_dict()
        return d

    def to_json(self) -> bytes:
        return msgspec.json.encode(self.to_dict())
