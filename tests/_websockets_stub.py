"""Minimal websockets API stub.

Just enough surface for retrosync.sources.usb2snes to import. The Pi will
have the real `websockets` package; this stub is for dry-run validation
in environments where it isn't installed.

It does NOT actually speak the WebSocket protocol — calling connect() or
serve() will raise. Use it only to exercise import-time correctness.
"""
from __future__ import annotations

from typing import Any


class ConnectionClosed(Exception):
    pass


class WebSocketClientProtocol:  # type stub only
    pass


class _NotImplementedConnect:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(
            "websockets stub: use the real `websockets` package at runtime")

    def __await__(self):
        raise RuntimeError("websockets stub")


def connect(*_args: Any, **_kwargs: Any) -> "_NotImplementedConnect":
    return _NotImplementedConnect()


def serve(*_args: Any, **_kwargs: Any):
    raise RuntimeError("websockets stub: use the real package")


# Sub-namespace that retrosync.sources.usb2snes references.
class _Client:
    WebSocketClientProtocol = WebSocketClientProtocol


client = _Client()
