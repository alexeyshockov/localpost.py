from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import cast, final


@contextmanager
def create_scope():
    with ExitStack() as exit_stack:
        yield Scope(exit_stack)


@dataclass(frozen=True, eq=False, slots=True)
class NamedTypeEntry:
    type: type
    name: str | None
    value: object

    def __call__(self, key: type, name: str | None = None) -> None | object:
        if self.type == key and self.name == name:
            return self.value
        return None


@final
@dataclass(frozen=True, eq=False, slots=True)
class Scope:
    exit_stack: ExitStack
    resolvers: list[Callable[[type, str | None], None | object]] = field(default_factory=list)

    # Kinda like "deffer" in Go or Zig
    def add(self, val, /, *, name: str | None = None) -> object:
        res = self.exit_stack.enter_context(val) if inspect.isgenerator(val) else val
        key = type(res)
        self.resolvers.append(NamedTypeEntry(key, name, res))
        return res

    def get[T](self, key: type[T], /, *, name: str | None = None, default: T | None = None) -> T | None:
        for resolver in self.resolvers:
            if result := resolver(key, name) is not None:
                return cast(T, result)
        return default

    def __getitem__[T](self, key: type[T]) -> T:
        if result := self.get(key):
            return cast(T, result)
        raise KeyError(f"{key} cannot be resolved")
