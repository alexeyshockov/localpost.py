import socket

import pytest


@pytest.fixture
def free_port() -> int:
    """Bind a temporary socket to port 0 and return the assigned port number.

    The socket is closed before returning; there is a tiny race window where the
    OS may reuse the port. Good enough for tests, and avoids ``port=0`` read-back.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
