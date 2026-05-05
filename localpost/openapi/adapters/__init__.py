"""Pluggable type adapters for JSON Schema, request decode, and response encode.

A :class:`TypeAdapter` owns a family of types — msgspec for the catch-all,
pydantic for :class:`pydantic.BaseModel` subclasses, and so on. The
:class:`AdapterRegistry` dispatches by type, with the catch-all (msgspec)
always last.

The default registry is auto-built from what's installed: msgspec is a
hard runtime dependency; pydantic is detected at import time. To plug in
a custom adapter (e.g. attrs, protobuf), pass an explicit registry to
:class:`localpost.openapi.HttpApp`::

    from localpost.openapi import HttpApp
    from localpost.openapi.adapters import AdapterRegistry, default_registry

    registry = AdapterRegistry([MyAttrsAdapter(), *default_registry().adapters])
    app = HttpApp(adapters=registry)
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from typing import Any, Protocol

__all__ = [
    "AdapterRegistry",
    "SchemaFor",
    "TypeAdapter",
    "default_registry",
]


SchemaFor = Callable[[Any], dict[str, Any]]
"""Registry callback an adapter can use to delegate nested-type schemas back to the registry.

Returns a JSON Schema fragment (a ``$ref`` for named types, inline for primitives / unions),
exactly like :meth:`SchemaRegistry.schema_for`. Adapters that don't recurse into foreign types
can ignore the kwarg.
"""


class TypeAdapter(Protocol):
    """Library-specific bridge for JSON Schema, decode, and encode.

    Implementations are expected to be stateless and cheap to instantiate.
    The catch-all adapter (msgspec by default) is the last one in the
    registry; its :meth:`claims` always returns ``True``.
    """

    name: str
    validation_errors: tuple[type[Exception], ...]

    def claims(self, t: Any, /) -> bool:
        """True if this adapter handles ``t``.

        The first adapter in the registry that returns ``True`` wins.
        """
        ...

    def is_body_type(self, t: Any, /) -> bool:
        """True if ``t`` should be parsed from the request body.

        Falsey types (primitives, ``Annotated`` scalars, …) are left as
        query / header parameters. Implementations should return ``False``
        for types that look scalar even if they technically claim them.
        """
        ...

    def schema(self, t: Any, /, *, ref_template: str, schema_for: SchemaFor | None = None) -> dict[str, Any]:
        """Return the JSON Schema fragment for ``t``.

        For named types, return ``{"$ref": ...}`` and let
        :meth:`components` produce the body.

        ``schema_for`` is the registry's own :meth:`SchemaRegistry.schema_for`,
        passed when the adapter may need to recurse into types it doesn't
        own (e.g. an attrs class with a nested :class:`msgspec.Struct`
        field). Adapters that own a closed type universe can ignore it.
        """
        ...

    def components(
        self,
        types: Sequence[Any],
        /,
        *,
        ref_template: str,
        schema_for: SchemaFor | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return the ``components.schemas`` entries for every type
        previously passed to :meth:`schema`.

        ``schema_for`` has the same meaning as in :meth:`schema`.
        """
        ...

    def decode(self, body: bytes, t: Any, /, *, content_type: str) -> object:
        """Parse ``body`` (raw request bytes) into an instance of ``t``.

        ``content_type`` is the request's declared content type — adapters
        carrying multiple wire formats (e.g. protobuf binary vs JSON) use
        it to dispatch; pure-JSON adapters can ignore it.
        """
        ...

    def encode(self, value: object, /) -> tuple[bytes, str]:
        """Serialise ``value`` to ``(body_bytes, content_type)``."""
        ...


class AdapterRegistry:
    """Ordered list of :class:`TypeAdapter` s with type-keyed dispatch.

    The last adapter in the list is the catch-all; its :meth:`claims` is
    expected to return ``True`` for every type, so :meth:`for_type` always
    yields a usable adapter.
    """

    __slots__ = ("_adapters", "_validation_errors")

    def __init__(self, adapters: Sequence[TypeAdapter]) -> None:
        if not adapters:
            raise ValueError("AdapterRegistry requires at least one adapter")
        self._adapters: tuple[TypeAdapter, ...] = tuple(adapters)
        # Flatten + dedupe per-adapter validation errors so call sites can
        # use a single ``except`` clause.
        seen: set[type[Exception]] = set()
        flat: list[type[Exception]] = []
        for a in self._adapters:
            for exc in a.validation_errors:
                if exc in seen:
                    continue
                seen.add(exc)
                flat.append(exc)
        self._validation_errors: tuple[type[Exception], ...] = tuple(flat)

    @property
    def adapters(self) -> tuple[TypeAdapter, ...]:
        return self._adapters

    @property
    def validation_errors(self) -> tuple[type[Exception], ...]:
        return self._validation_errors

    def for_type(self, t: Any) -> TypeAdapter:
        """Return the first adapter that claims ``t`` (catch-all if none)."""
        for adapter in self._adapters:
            if adapter.claims(t):
                return adapter
        return self._adapters[-1]

    def for_value(self, value: object) -> TypeAdapter:
        return self.for_type(type(value))


@functools.cache
def default_registry() -> AdapterRegistry:
    """Return a process-wide :class:`AdapterRegistry` autoconfigured from
    installed libraries.

    Order: pydantic (if importable), then msgspec as the catch-all. Cached.
    """
    from localpost.openapi.adapters._msgspec import MsgspecAdapter  # noqa: PLC0415

    adapters: list[TypeAdapter] = []
    try:
        from localpost.openapi.adapters._pydantic import PydanticAdapter  # noqa: PLC0415

        adapters.append(PydanticAdapter())
    except ImportError:
        pass
    adapters.append(MsgspecAdapter())
    return AdapterRegistry(adapters)
