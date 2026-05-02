"""JSON Schema generation for OpenAPI 3.2 components.

Backed by :func:`msgspec.json.schema_components`, which understands
:class:`msgspec.Struct`, dataclasses, ``TypedDict``, ``NamedTuple``,
primitives, collections, ``Annotated[T, msgspec.Meta(...)]``, ``Literal``,
``Enum``, ``Union``, ``UUID``, ``datetime`` and so on. The output is JSON
Schema 2020-12, which is the dialect OpenAPI 3.1 / 3.2 use natively.

Pydantic models are recognised lazily and routed through
:meth:`pydantic.BaseModel.model_json_schema` — pydantic itself is *not* an
import dependency of this module.
"""

from __future__ import annotations

import threading
from typing import Any

import msgspec

try:
    from pydantic import BaseModel  # type: ignore[import-not-found]

    _PydanticBaseModel: type | None = BaseModel
except ImportError:
    _PydanticBaseModel = None

__all__ = ["SchemaRegistry", "REF_TEMPLATE", "is_pydantic_model"]


REF_TEMPLATE = "#/components/schemas/{name}"


def is_pydantic_model(t: Any) -> bool:
    """True if ``t`` is a subclass of :class:`pydantic.BaseModel`.

    Returns ``False`` if pydantic isn't installed.
    """
    return _PydanticBaseModel is not None and isinstance(t, type) and issubclass(t, _PydanticBaseModel)


class SchemaRegistry:
    """Accumulator for types referenced across operations.

    Call :meth:`schema_for` for each type the spec needs to describe; the
    registry returns a JSON Schema fragment (``$ref`` for named types,
    inline for primitives / unions). At spec emission time, call
    :meth:`components` to resolve the accumulated named types into
    ``components.schemas`` entries.

    The registry is thread-safe: registration is guarded so concurrent doc
    builds don't race on the cached components dict.
    """

    __slots__ = ("_components", "_lock", "_msgspec_types", "_pydantic_types")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Types we'll feed to msgspec.json.schema_components. We store a list
        # (not a set) to preserve order — msgspec keys components by their
        # name, but order matters for stable doc output.
        self._msgspec_types: list[Any] = []
        self._pydantic_types: list[type] = []
        self._components: dict[str, dict[str, Any]] | None = None

    def schema_for(self, t: Any) -> dict[str, Any]:
        """Return a JSON Schema fragment describing ``t``.

        For named types (:class:`msgspec.Struct`, dataclass, pydantic model,
        ``TypedDict``, ``NamedTuple``, ``Enum``) this returns a ``$ref`` and
        registers the type. For primitives / unions / generics the schema is
        inlined.
        """
        if t is None or t is type(None):
            return {"type": "null"}
        with self._lock:
            self._components = None  # invalidate
            if is_pydantic_model(t):
                if t not in self._pydantic_types:
                    self._pydantic_types.append(t)
                return {"$ref": REF_TEMPLATE.format(name=t.__name__)}
            self._msgspec_types.append(t)
        # msgspec returns ``(schemas, components)`` — for a single type the
        # ``schemas`` list has one element, which is either the inline schema
        # or a ``$ref``. We discard the components dict here and re-derive
        # everything in :meth:`components` so we have a single source of truth.
        try:
            (schema,), _ = msgspec.json.schema_components([t], ref_template=REF_TEMPLATE)
        except (TypeError, RuntimeError):
            # Fall back to a permissive schema for types msgspec can't introspect.
            return {}
        return schema

    def components(self) -> dict[str, dict[str, Any]]:
        """Return the resolved ``components.schemas`` dict for every type
        ever passed to :meth:`schema_for`.

        Result is cached until the next :meth:`schema_for` call.
        """
        with self._lock:
            if self._components is not None:
                return self._components
            schemas: dict[str, dict[str, Any]] = {}
            if self._msgspec_types:
                _, components = msgspec.json.schema_components(self._msgspec_types, ref_template=REF_TEMPLATE)
                schemas.update(components)
            for t in self._pydantic_types:
                schemas[t.__name__] = _pydantic_json_schema(t)
            self._components = schemas
            return schemas


def _pydantic_json_schema(model: type) -> dict[str, Any]:
    """Extract a pydantic model's JSON Schema, stripped of pydantic-specific
    framing (``$defs``, top-level ``title`` we'd duplicate).

    Pydantic's nested ``$defs`` are inlined under ``components.schemas`` —
    callers should merge them in. v1 keeps the simpler path of treating each
    pydantic model as a standalone component; deep cross-model refs may need
    work later.
    """
    # Pydantic uses ``{model}`` as the placeholder, not ``{name}`` like msgspec.
    pydantic_ref_template = REF_TEMPLATE.replace("{name}", "{model}")
    raw: dict[str, Any] = getattr(model, "model_json_schema")(ref_template=pydantic_ref_template)  # noqa: B009
    raw.pop("$defs", None)
    return raw
