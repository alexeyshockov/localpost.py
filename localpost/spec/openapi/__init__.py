from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

import msgspec

DEFAULT_CONTENT_TYPE = "application/json"


@dataclass(frozen=True, slots=True)
class Schema:
    type: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    items: dict[str, Any] | None = None
    ref: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    """Any additional JSON Schema fields."""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.ref:
            d["$ref"] = self.ref
            return d
        if self.type:
            d["type"] = self.type
        if self.properties:
            d["properties"] = self.properties
        if self.required:
            d["required"] = self.required
        if self.items:
            d["items"] = self.items
        d.update(self.extra)
        return d


@dataclass(frozen=True, slots=True)
class MediaType:
    schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.schema:
            d["schema"] = self.schema
        return d


@dataclass(frozen=True, slots=True)
class RequestBody:
    content: dict[str, MediaType] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        if not self.content:
            return {}
        return {"content": {k: v.to_dict() for k, v in self.content.items()}}


@dataclass(frozen=True, slots=True)
class Response:
    description: str = ""
    content: dict[str, MediaType] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"description": self.description}
        if self.content:
            d["content"] = {k: v.to_dict() for k, v in self.content.items()}
        return d


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    location: Literal["query", "header", "cookie", "path"] = "query"
    required: bool = True
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "in": self.location, "required": self.required}
        if self.description:
            d["description"] = self.description
        if self.schema:
            d["schema"] = self.schema
        return d


@dataclass(frozen=True, slots=True)
class Operation:
    summary: str = ""
    operation_id: str = ""
    description: str = ""
    parameters: tuple[Parameter, ...] = ()
    request_body: RequestBody | None = None
    responses: dict[str, Response] = field(default_factory=dict)
    deprecated: bool = False
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.summary:
            d["summary"] = self.summary
        if self.operation_id:
            d["operationId"] = self.operation_id
        if self.description:
            d["description"] = self.description
        if self.parameters:
            d["parameters"] = [p.to_dict() for p in self.parameters]
        if self.request_body:
            rb = self.request_body.to_dict()
            if rb:
                d["requestBody"] = rb
        if self.responses:
            d["responses"] = {k: v.to_dict() for k, v in self.responses.items()}
        if self.deprecated:
            d["deprecated"] = True
        if self.tags:
            d["tags"] = list(self.tags)
        return d


@dataclass(frozen=True, slots=True)
class PathItem:
    operations: dict[str, Operation] = field(default_factory=dict)
    """Keyed by lowercase HTTP method: get, post, put, etc."""

    def to_dict(self) -> dict[str, Any]:
        return {method: op.to_dict() for method, op in self.operations.items()}


@dataclass(frozen=True, slots=True)
class Info:
    title: str = "API"
    description: str = ""
    version: str = "0.1.0"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"title": self.title, "version": self.version}
        if self.description:
            d["description"] = self.description
        return d


@dataclass(frozen=True, slots=True)
class Tag:
    name: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.description:
            d["description"] = self.description
        return d


@dataclass(frozen=True, slots=True)
class OpenAPI:
    openapi: str = "3.1.0"
    info: Info = field(default_factory=Info)
    paths: dict[str, PathItem] = field(default_factory=dict)
    tags: tuple[Tag, ...] = ()

    def add_operation(self, path: str, method: str, operation: Operation) -> OpenAPI:
        """Return a new OpenAPI instance with the operation added."""
        new_paths = dict(self.paths)
        if path in new_paths:
            existing = new_paths[path]
            new_ops = dict(existing.operations)
            new_ops[method.lower()] = operation
            new_paths[path] = PathItem(operations=new_ops)
        else:
            new_paths[path] = PathItem(operations={method.lower(): operation})
        return replace(self, paths=new_paths)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "openapi": self.openapi,
            "info": self.info.to_dict(),
        }
        if self.paths:
            d["paths"] = {path: item.to_dict() for path, item in self.paths.items()}
        if self.tags:
            d["tags"] = [t.to_dict() for t in self.tags]
        return d

    def to_json(self) -> bytes:
        return msgspec.json.encode(self.to_dict())
