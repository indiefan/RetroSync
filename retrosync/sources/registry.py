"""Adapter registry: maps config 'adapter' strings to constructor callables.

Adapters are imported lazily (on first `build()` for that name) so a host
that doesn't need usb2snes / websockets / etc. doesn't pay for those
imports. This also means a missing optional dependency won't break the
daemon at import time — only when its adapter is actually instantiated.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import SaveSource

_REGISTRY: dict[str, Callable[..., SaveSource]] = {}

# Maps an adapter name to a "loader" that, when called, imports its module
# (which is expected to register() itself as a side effect). This is what
# lets us avoid importing websockets-using adapters until they're needed.
_LAZY_LOADERS: dict[str, Callable[[], None]] = {}


def register(name: str, ctor: Callable[..., SaveSource]) -> None:
    # Allow re-registration silently — the lazy loader may run more than
    # once if `register_lazy` is called repeatedly during tests.
    _REGISTRY[name] = ctor


def register_lazy(name: str, loader: Callable[[], None]) -> None:
    _LAZY_LOADERS[name] = loader


def build(name: str, **kwargs: Any) -> SaveSource:
    if name not in _REGISTRY and name in _LAZY_LOADERS:
        _LAZY_LOADERS[name]()
    if name not in _REGISTRY:
        raise KeyError(f"unknown adapter: {name!r}; "
                       f"registered: {sorted(_REGISTRY) + sorted(_LAZY_LOADERS)}")
    return _REGISTRY[name](**kwargs)


def known() -> list[str]:
    return sorted(set(_REGISTRY) | set(_LAZY_LOADERS))


# ---- bundled adapters (lazy) ----
def _load_fxpak() -> None:
    from . import fxpak  # noqa: F401  (registers itself on import)


def _load_pocket() -> None:
    from . import pocket  # noqa: F401  (registers itself on import)


def _load_emudeck() -> None:
    from . import emudeck  # noqa: F401  (registers itself on import)


def _load_everdrive64() -> None:
    from . import everdrive64  # noqa: F401  (adapter registers on import)


register_lazy("fxpak", _load_fxpak)
register_lazy("pocket", _load_pocket)
register_lazy("emudeck", _load_emudeck)
register_lazy("everdrive64", _load_everdrive64)
