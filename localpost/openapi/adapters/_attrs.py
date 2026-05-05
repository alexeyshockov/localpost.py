"""attrs/cattrs :class:`TypeAdapter`. Imported lazily by ``default_registry``;
import succeeds only when both :mod:`attrs` and :mod:`cattrs` are installed.

Schema generation walks ``attrs.fields(cls)`` directly. Container and union
field types are decomposed by the walker; leaf types fall back through
the supplied ``schema_for`` callback so foreign nested types (msgspec
Structs, pydantic models, plain primitives) hit the right adapter via
:class:`SchemaRegistry`.

Decode/encode is delegated to a ``cattrs`` JSON converter (default:
:func:`cattrs.preconf.json.make_converter`). Pass a custom converter to
``AttrsAdapter(converter=...)`` to register your own structure hooks.
"""

from __future__ import annotations

import json
import types
import typing
from collections.abc import Mapping, Sequence
from typing import Any

import attrs
import cattrs
from cattrs.errors import BaseValidationError

from localpost.openapi.adapters import SchemaFor

__all__ = ["AttrsAdapter"]


_JSON_CONTENT_TYPE = "application/json"

_PRIMITIVE_SCHEMAS: dict[type, dict[str, Any]] = {
    str: {"type": "string"},
    bool: {"type": "boolean"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bytes: {"type": "string", "format": "binary"},
}


class AttrsAdapter:
    name = "attrs"
    validation_errors: tuple[type[Exception], ...] = (BaseValidationError,)

    def __init__(self, converter: cattrs.Converter | None = None) -> None:
        # We deliberately use a plain ``Converter`` rather than the JSON preconf so users can
        # plug in their own with custom structure hooks. The (un)structure step handles the
        # type system; ``json`` handles the wire format.
        self._converter = converter or cattrs.Converter()
        # Classes whose forward-ref annotations have already been resolved. ``attrs.resolve_types``
        # mutates the class in place, so once is enough.
        self._resolved: set[type] = set()

    def claims(self, t: Any, /) -> bool:
        return isinstance(t, type) and attrs.has(t)

    def is_body_type(self, t: Any, /) -> bool:
        return self.claims(t)

    def schema(self, t: Any, /, *, ref_template: str, schema_for: SchemaFor | None = None) -> dict[str, Any]:
        del schema_for  # The class body itself is rendered in components(); we only emit a ref here.
        return {"$ref": ref_template.format(name=t.__name__)}

    def components(
        self,
        types_: Sequence[Any],
        /,
        *,
        ref_template: str,
        schema_for: SchemaFor | None = None,
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for t in types_:
            self._resolve(t)
            out[t.__name__] = self._object_schema(t, ref_template=ref_template, schema_for=schema_for)
        return out

    def decode(self, body: bytes, t: Any, /, *, content_type: str) -> object:
        del content_type
        if not body:
            raise ValueError("empty request body")
        return self._converter.structure(json.loads(body), t)

    def encode(self, value: object, /) -> tuple[bytes, str]:
        return json.dumps(self._converter.unstructure(value)).encode("utf-8"), _JSON_CONTENT_TYPE

    # --- internals ------------------------------------------------------

    def _resolve(self, t: type) -> None:
        if t in self._resolved:
            return
        # ``attrs.resolve_types`` walks ``__annotations__`` and replaces string forward refs
        # with real types. Idempotent and safe to call multiple times, but we cache to avoid
        # re-entering ``typing.get_type_hints`` on every spec rebuild.
        attrs.resolve_types(t)
        self._resolved.add(t)

    def _object_schema(self, t: type, *, ref_template: str, schema_for: SchemaFor | None) -> dict[str, Any]:
        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []
        for f in attrs.fields(t):
            field_schema = self._field_schema(f.type, ref_template=ref_template, schema_for=schema_for)
            properties[f.name] = field_schema
            if f.default is attrs.NOTHING:
                required.append(f.name)
        out: dict[str, Any] = {
            "type": "object",
            "title": t.__name__,
            "properties": properties,
        }
        if required:
            out["required"] = required
        return out

    def _field_schema(self, t: Any, *, ref_template: str, schema_for: SchemaFor | None) -> dict[str, Any]:
        if t is None or t is type(None):
            return {"type": "null"}
        if t in _PRIMITIVE_SCHEMAS:
            return dict(_PRIMITIVE_SCHEMAS[t])

        origin = typing.get_origin(t)
        if origin is typing.Union or origin is types.UnionType:
            args = typing.get_args(t)
            return {"oneOf": [self._field_schema(a, ref_template=ref_template, schema_for=schema_for) for a in args]}
        if origin is typing.Literal:
            return {"enum": list(typing.get_args(t))}
        if origin in (list, set, frozenset) or (
            isinstance(origin, type) and issubclass(origin, Sequence) and origin is not str
        ):
            (item_t,) = typing.get_args(t) or (Any,)
            return {
                "type": "array",
                "items": self._field_schema(item_t, ref_template=ref_template, schema_for=schema_for),
            }
        if origin is tuple:
            args = typing.get_args(t)
            # ``tuple[T, ...]`` -> homogeneous array; fixed-length tuples fall through to a
            # heterogeneous prefix-items array.
            if len(args) == 2 and args[1] is Ellipsis:
                return {
                    "type": "array",
                    "items": self._field_schema(args[0], ref_template=ref_template, schema_for=schema_for),
                }
            return {
                "type": "array",
                "prefixItems": [self._field_schema(a, ref_template=ref_template, schema_for=schema_for) for a in args],
                "minItems": len(args),
                "maxItems": len(args),
            }
        if origin is dict or (isinstance(origin, type) and issubclass(origin, Mapping)):
            args = typing.get_args(t)
            value_t = args[1] if len(args) == 2 else Any
            return {
                "type": "object",
                "additionalProperties": self._field_schema(value_t, ref_template=ref_template, schema_for=schema_for),
            }

        # Nested attrs class — emit ref + register via the registry so its body lands in
        # components.schemas.
        if isinstance(t, type) and attrs.has(t):
            if schema_for is not None:
                return schema_for(t)
            return {"$ref": ref_template.format(name=t.__name__)}

        # Anything else (foreign types: msgspec.Struct, dataclass, pydantic model, datetime,
        # UUID, …): defer to the registry. Without a callback, fall back to a permissive schema.
        if schema_for is not None:
            return schema_for(t)
        return {}
