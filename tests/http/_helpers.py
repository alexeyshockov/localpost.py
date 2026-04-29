from __future__ import annotations

import socket


def drain_socket(sock: socket.socket, deadline: float = 2.0) -> bytes:
    """Read until the peer closes or no bytes arrive before ``deadline``."""
    sock.settimeout(deadline)
    out = bytearray()
    while True:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            break
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


def read_until(sock: socket.socket, marker: bytes, deadline: float = 2.0) -> bytes:
    """Read until ``marker`` appears in the accumulated bytes."""
    sock.settimeout(deadline)
    out = bytearray()
    while marker not in out:
        chunk = sock.recv(4096)
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


def read_http_response(sock: socket.socket, deadline: float = 2.0) -> bytes:
    """Read one HTTP/1.1 response with Content-Length framing."""
    data = read_until(sock, b"\r\n\r\n", deadline=deadline)
    header_end = data.find(b"\r\n\r\n")
    if header_end == -1:
        return data

    content_length = 0
    for line in data[:header_end].split(b"\r\n")[1:]:
        name, sep, value = line.partition(b":")
        if sep and name.lower() == b"content-length":
            content_length = int(value.strip())
            break

    body_start = header_end + 4
    remaining = content_length - (len(data) - body_start)
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            break
        data += chunk
        remaining -= len(chunk)
    return data
