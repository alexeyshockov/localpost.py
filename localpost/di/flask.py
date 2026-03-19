"""Flask integration"""

from __future__ import annotations

from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from typing import final

from localpost.di._services import ResolutionContext


@final
@dataclass(frozen=True, eq=False, slots=True)
class AppContext(ResolutionContext):
    ctx: ExitStack = field(default_factory=ExitStack)

    def enter[T](self, cm: AbstractContextManager[T]) -> T:
        return self.ctx.enter_context(cm)


# TODO Implement, enter a new RequestContext scope for each request


# TODO Register Flask Request in the DI container, with RequestContext scope
