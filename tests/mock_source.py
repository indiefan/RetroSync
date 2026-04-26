"""In-process mock source for dry-run testing.

Implements the SaveSource protocol directly — no websockets, no usb2snes,
no SNI. Used by tests/dry_run.py to exercise the orchestrator, state
store, cloud wrapper, and rclone integration end-to-end.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath

from retrosync.sources.base import HealthStatus, SaveRef


class MockFXPakSource:
    """Behaves like FXPakSource but pulls bytes from a python dict."""

    system = "snes"

    def __init__(self, *, id: str, files: dict[str, bytes]):
        self.id = id
        self.files = files
        self._fail_health = False

    def break_(self) -> None:
        self._fail_health = True

    def heal(self) -> None:
        self._fail_health = False

    async def health(self) -> HealthStatus:
        await asyncio.sleep(0)
        if self._fail_health:
            return HealthStatus(False, "mock cart unplugged")
        return HealthStatus(True, "mock cart attached")

    async def list_saves(self) -> list[SaveRef]:
        await asyncio.sleep(0)
        return [SaveRef(path=p, size_bytes=len(b))
                for p, b in self.files.items() if p.endswith(".srm")]

    async def read_save(self, ref: SaveRef) -> bytes:
        await asyncio.sleep(0)
        return self.files[ref.path]

    async def write_save(self, ref: SaveRef, data: bytes) -> None:
        self.files[ref.path] = data

    def resolve_game_id(self, ref: SaveRef) -> str:
        stem = PurePosixPath(ref.path).stem
        slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
        # Pretend CRC32 = first 8 chars of save's hash, to differentiate games.
        rom_path = ref.path.rsplit(".", 1)[0] + ".smc"
        rom = self.files.get(rom_path, b"")
        crc = sum(rom) & 0xFFFFFFFF
        return f"{crc:08x}_{slug or 'unnamed'}"

    async def async_resolve_game_id(self, ref: SaveRef) -> str:
        return self.resolve_game_id(ref)
