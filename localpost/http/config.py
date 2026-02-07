from __future__ import annotations

from dataclasses import dataclass, field
from typing import final

from localpost._sync_utils import CHECK_TIMEOUT

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
    rw_timeout: float = 3.0
    """Timeout (seconds) for receive/send operations on a client connection."""
    keep_alive_timeout: float = 15.0
    """Timeout (seconds) for idle connections."""
    max_body_size: int = 10 * 1024 * 1024  # 10 MiB
    """Maximum request body size (bytes)."""


@final
@dataclass(frozen=True, slots=True)
class WorkerConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    max_connections: int = 100
    """Max open connections (including idle)."""
    max_requests: int = 5
    """Max parallel requests."""
