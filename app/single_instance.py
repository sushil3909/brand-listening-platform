"""Prevent the same script from running twice (e.g. double-clicking a .bat twice)."""
from __future__ import annotations

import socket
import sys

_held: list[socket.socket] = []  # keep sockets alive for the life of the process


def ensure_single_instance(name: str, port: int) -> None:
    """Bind a private localhost port as a mutex; exit if another copy already holds it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # Windows: no double bind
    except (AttributeError, OSError):
        pass
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
    except OSError:
        print(f"{name} is ALREADY RUNNING in another window. Close this window; use the existing one.")
        sys.exit(2)
    _held.append(s)
