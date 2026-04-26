"""Source adapter protocol.

Every kind of save source — FXPak Pro, EverDrive, RetroArch directory, etc. —
implements this interface. The orchestrator only ever talks to this protocol,
which is what keeps the foundation extensible.

When adding a new source type:
1. Add a new module in retrosync.sources (e.g. everdrive_n64.py).
2. Implement SaveSource against your hardware/library.
3. Register it in retrosync.sources.registry.
4. Reference its `adapter` name in config.yaml.

That is the entire contract. Polling, hashing, debouncing, versioning, and
upload are all handled by the orchestrator and are source-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SaveRef:
    """A handle to a single save file on a source.

    `path` is opaque to the orchestrator — the source assigns it however it
    likes (e.g. a path on the cart's SD, a relative path under an emulator
    saves dir, an opaque handle for sources that don't have file paths).
    `size_bytes` is advisory; it is fine for sources that can't cheaply
    determine size to set it to None.
    """
    path: str
    size_bytes: int | None = None
    mtime_iso: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    detail: str = ""


@runtime_checkable
class SaveSource(Protocol):
    """The contract implemented by every save source.

    `id` is a stable, operator-chosen identifier (e.g. "fxpak-pro-1").
    `system` is the platform identifier used in cloud paths
        (e.g. "snes", "n64", "gb", "retroarch").
    `device_kind` is a short label for the *kind* of device, used purely
        for human-readable cloud-folder organization (e.g. cart vs.
        Pocket vs. emulator). Defaults to `system` for cart-style
        adapters; `PocketSource` overrides it to `"pocket"` so its
        uploads land under `versions/pocket/...`.

    Every method may raise SourceError on transient failures; the orchestrator
    will treat those as "unhealthy, retry later" and not as a permanent fault.
    """

    id: str
    system: str
    device_kind: str

    async def health(self) -> HealthStatus: ...

    async def list_saves(self) -> list[SaveRef]: ...

    async def read_save(self, ref: SaveRef) -> bytes: ...

    async def write_save(self, ref: SaveRef, data: bytes) -> None: ...

    def resolve_game_id(self, ref: SaveRef) -> str:
        """Map a SaveRef to a stable, human-readable game identifier.

        The orchestrator uses this to namespace cloud paths
        (`<system>/<game-id>/...`). Format: `<crc32>_<slug>` where the CRC
        is platform-specific and the slug is filesystem-safe.

        Sources are responsible for caching expensive identification.
        """
        ...


class SourceError(Exception):
    """Raised by source adapters on transient failures (cart unplugged, etc.)."""
