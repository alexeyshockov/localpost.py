from __future__ import annotations

from dataclasses import dataclass
from typing import Final, final

__all__ = [
    "LOGGER_NAME",
    "ServerConfig",
]

DEFAULT_BUFFER_SIZE: Final = 64 * 1024  # 64 KiB

LOGGER_NAME: Final = "localpost.http"
# TODO Access logger?..


@final
@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    backlog: int = 1024
    """Maximum number of queued (in the kernel) connections."""
    rw_timeout: float = 1.0
    """Timeout (seconds) for receive/send operations on a borrowed client connection,
    and for the keep-alive read deadline extended after each chunk arrives."""
    keep_alive_timeout: float = 15.0  # TODO add it to the response
    """Timeout (seconds) for idle connections."""
    max_body_size: int = 10 * 1024 * 1024  # 10 MiB
    """Maximum request body size (bytes)."""
