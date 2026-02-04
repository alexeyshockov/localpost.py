from __future__ import annotations

from dataclasses import dataclass, field
from typing import final

__all__ = [
    "LOGGER_NAME",
    "WorkerConfig",
    "ServerConfig",
]

LOGGER_NAME = "localpost.http"


@final
@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    backlog: int = 16
    """Maximum number of queued connections."""
    read_timeout: float = 5.0
    """Timeout (seconds) for read operations (keep-alive timeout too)."""


@final
@dataclass(frozen=True, slots=True)
class WorkerConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    max_concurrent_requests: int = 10
    """Max concurrent requests (connections)."""


@final
@dataclass(frozen=True, slots=True)
class ProcessWorkerConfig:
    pass  # For the future


@final
@dataclass(frozen=True, slots=True)
class InterpreterWorkerConfig:
    pass  # For the future
