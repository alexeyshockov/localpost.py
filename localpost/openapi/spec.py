"""Immutable dataclasses for the OpenAPI 3.2 specification.

The dataclasses are version-agnostic enough that a 3.1 / 3.0 serializer
could be added later as a sibling of :meth:`OpenAPI.to_dict` without
touching the data shape. v1 emits 3.2 only.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import msgspec

ParameterLocation = Literal["query", "header", "path", "cookie"]
SecuritySchemeType = Literal["apiKey", "http", "oauth2", "openIdConnect", "mutualTLS"]


# --- Serialisation helpers ------------------------------------------------

# Field-name → output-key overrides for fields whose JSON name doesn't
# follow the snake_case → camelCase rule (e.g. ``ref`` → ``$ref``).
_KEY_OVERRIDES: dict[str, str] = {
    "ref": "$ref",
    "location": "in",
}


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(p.capitalize() for p in tail)


def _key(name: str) -> str:
    return _KEY_OVERRIDES.get(name, _camel(name))


def _dump(value: Any) -> Any:
    """Recursively serialise dataclasses/containers for JSON output.

    Tuples become lists; nested dataclasses are flattened via their own
    ``to_dict``. Plain JSON-compatible values pass through.
    """
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_dump(v) for v in value]
    return value


_MISSING = dataclasses.MISSING


def _to_dict(obj: Any, *, always: tuple[str, ...] = ()) -> dict[str, Any]:
    """Generic ``to_dict`` for the spec dataclasses.

    Walks ``dataclasses.fields(obj)``, skipping any field whose value
    matches its declared default (so ``""`` strings, empty dicts/tuples,
    ``None`` and ``False`` are omitted unless their name is listed in
    ``always``). Field names map to JSON keys via :func:`_key`
    (snake_case → camelCase, with overrides for ``ref`` and ``location``).
    """
    out: dict[str, Any] = {}
    for f in dataclasses.fields(obj):
        v = getattr(obj, f.name)
        if f.name not in always:
            if f.default is not _MISSING and v == f.default:
                continue
            if f.default_factory is not _MISSING and v == f.default_factory():
                continue
        out[_key(f.name)] = _dump(v)
    return out


# --- Spec dataclasses -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reference:
    """JSON ``$ref`` to another component."""

    ref: str
    summary: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self, always=("ref",))


@dataclass(frozen=True, slots=True)
class Contact:
    name: str = ""
    url: str = ""
    email: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class License:
    name: str
    identifier: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


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
        return _to_dict(self, always=("title", "version"))


@dataclass(frozen=True, slots=True)
class ServerVariable:
    default: str
    enum: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class Server:
    url: str
    description: str = ""
    variables: dict[str, ServerVariable] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ExternalDocs:
    url: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class Tag:
    name: str
    description: str = ""
    summary: str = ""
    external_docs: ExternalDocs | None = None
    parent: str = ""
    """3.2 addition: name of the parent tag for nested grouping."""

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class TagGroup:
    """3.2 addition: tag group declared at the root level."""

    name: str
    tags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class Encoding:
    content_type: str = ""
    headers: dict[str, Header | Reference] = field(default_factory=dict)
    style: str = ""
    explode: bool | None = None
    allow_reserved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class Example:
    summary: str = ""
    description: str = ""
    value: Any = None
    external_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class MediaType:
    schema: dict[str, Any] = field(default_factory=dict)
    example: Any = None
    examples: dict[str, Example | Reference] = field(default_factory=dict)
    encoding: dict[str, Encoding] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class Header:
    description: str = ""
    required: bool = False
    deprecated: bool = False
    schema: dict[str, Any] = field(default_factory=dict)
    content: dict[str, MediaType] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


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
        return _to_dict(self, always=("location",))


@dataclass(frozen=True, slots=True)
class RequestBody:
    description: str = ""
    required: bool = False
    content: dict[str, MediaType] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class Response:
    description: str = ""
    headers: dict[str, Header | Reference] = field(default_factory=dict)
    content: dict[str, MediaType] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self, always=("description",))


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
        return _to_dict(self)


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
            d["parameters"] = [_dump(p) for p in self.parameters]
        if self.servers:
            d["servers"] = [_dump(s) for s in self.servers]
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
        return _to_dict(self, always=("scopes",))


@dataclass(frozen=True, slots=True)
class OAuthFlows:
    implicit: OAuthFlow | None = None
    password: OAuthFlow | None = None
    client_credentials: OAuthFlow | None = None
    authorization_code: OAuthFlow | None = None
    device_authorization: OAuthFlow | None = None
    """3.2 addition."""

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


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
        return _to_dict(self)


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
        return _to_dict(self)

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
        return _to_dict(self, always=("openapi", "info"))

    def to_json(self) -> bytes:
        return msgspec.json.encode(self.to_dict())
