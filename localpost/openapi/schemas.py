"""JSON Schema accumulator for OpenAPI 3.2 components.

Schema work is delegated to :class:`localpost.openapi.adapters.TypeAdapter`
implementations — see :mod:`localpost.openapi.adapters` for the protocol
and the built-in msgspec / pydantic bridges.
"""

from __future__ import annotations

import threading
from typing import Any

from localpost.openapi.adapters import AdapterRegistry, TypeAdapter, default_registry

__all__ = ["REF_TEMPLATE", "SchemaRegistry"]


REF_TEMPLATE = "#/components/schemas/{name}"


class SchemaRegistry:
    """Accumulator for types referenced across operations.

    Call :meth:`schema_for` for each type the spec needs to describe; the
    registry returns a JSON Schema fragment (``$ref`` for named types,
    inline for primitives / unions). At spec emission time, call
    :meth:`components` to resolve the accumulated named types into
    ``components.schemas`` entries.

    Schema/components production is delegated per type to the matching
    :class:`TypeAdapter` from the supplied :class:`AdapterRegistry`.

    Thread-safe: registration is guarded so concurrent doc builds don't
    race on the cached components dict.
    """

    __slots__ = ("_adapters", "_components", "_lock", "_types_by_adapter")

    def __init__(self, adapters: AdapterRegistry | None = None) -> None:
        # Re-entrant: adapters that recurse via the ``schema_for`` callback (attrs ->
        # msgspec for foreign nested types) take the same lock from the same thread.
        self._lock = threading.RLock()
        self._adapters = adapters or default_registry()
        # We bucket types by the adapter that claims them. Insertion order
        # is preserved (CPython dict semantics), so generated component
        # output is stable across runs.
        self._types_by_adapter: dict[TypeAdapter, list[Any]] = {}
        self._components: dict[str, dict[str, Any]] | None = None

    @property
    def adapters(self) -> AdapterRegistry:
        return self._adapters

    def schema_for(self, t: Any) -> dict[str, Any]:
        """Return a JSON Schema fragment describing ``t``.

        For named types (:class:`msgspec.Struct`, dataclass, pydantic model,
        ``TypedDict``, ``NamedTuple``, ``Enum``) this returns a ``$ref`` and
        registers the type. For primitives / unions / generics the schema is
        inlined.
        """
        if t is None or t is type(None):
            return {"type": "null"}
        adapter = self._adapters.for_type(t)
        with self._lock:
            self._components = None  # invalidate
            bucket = self._types_by_adapter.setdefault(adapter, [])
            if t not in bucket:
                bucket.append(t)
        return adapter.schema(t, ref_template=REF_TEMPLATE, schema_for=self.schema_for)

    def components(self) -> dict[str, dict[str, Any]]:
        """Return the resolved ``components.schemas`` dict for every type
        ever passed to :meth:`schema_for`.

        Result is cached until the next :meth:`schema_for` call.
        """
        with self._lock:
            if self._components is not None:
                return self._components
            schemas: dict[str, dict[str, Any]] = {}
            # Drain in passes: an adapter's ``components()`` may register more types via
            # ``schema_for`` (e.g. attrs adapter delegating a nested msgspec.Struct field
            # back to the registry). Track per-adapter how many items have been processed
            # and loop until no adapter has unprocessed work.
            processed: dict[TypeAdapter, int] = {}
            while True:
                made_progress = False
                # Snapshot keys: more adapters may appear during iteration.
                for adapter in list(self._types_by_adapter.keys()):
                    bucket = self._types_by_adapter[adapter]
                    start = processed.get(adapter, 0)
                    if start >= len(bucket):
                        continue
                    pending = bucket[start:]
                    processed[adapter] = len(bucket)
                    schemas.update(adapter.components(pending, ref_template=REF_TEMPLATE, schema_for=self.schema_for))
                    made_progress = True
                if not made_progress:
                    break
            self._components = schemas
            return schemas
