"""Offline-only test suite: real network connections are always forbidden."""

import socket
import threading
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    local = threading.local()
    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair

    def blocked(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Network access is forbidden in tests; inject a fake transport")

    def connect(sock: socket.socket, address: Any) -> None:
        if getattr(local, "socketpair", False):
            original_connect(sock, address)
            return
        blocked()

    def socketpair(*args: Any, **kwargs: Any) -> tuple[socket.socket, socket.socket]:
        # Windows implements asyncio's private wakeup pipe using local sockets.
        # This exemption applies only during socketpair creation, never HTTP/DNS.
        local.socketpair = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            local.socketpair = False

    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
