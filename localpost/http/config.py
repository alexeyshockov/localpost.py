from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import final, Final

__all__ = [
    "LOGGER_NAME",
    "WorkerConfig",
    "ServerConfig",
]

DEFAULT_BUFFER_SIZE: Final = io.DEFAULT_BUFFER_SIZE

LOGGER_NAME: Final = "localpost.http"
# TODO Access logger?..


@final
@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    backlog: int = 1024
    """Maximum number of queued (in the kernel) connections."""
    # rw_timeout: float = 3.0
    # """Timeout (seconds) for receive/send operations on a client connection."""
    keep_alive_timeout: float = 15.0
    """Timeout (seconds) for idle connections."""
    max_body_size: int = 10 * 1024 * 1024  # 10 MiB
    """Maximum request body size (bytes)."""
    # TODO Support Keep-Alive response header (timeout, max requests)


# FIXME Remove, just move to ...
@final
@dataclass(frozen=True, slots=True)
class WorkerConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    max_requests: int = 5
    """Max parallel requests."""
