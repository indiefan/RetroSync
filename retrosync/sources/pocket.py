"""Analogue Pocket source adapter.

The Pocket presents itself as a USB mass-storage device when the user
enables "Tools → USB → Mount as USB Drive" on the device. From the host's
POV it's a FAT32 filesystem rooted at the SD card. We mount it at
`mount_path` (driven by the systemd unit when udev fires) and read/write
saves under `Saves/<core>/`.

Game-ID resolution uses the same `canonical_slug` as the FXPak adapter,
so a save called `Super Metroid.sav` collapses to `super_metroid` and
shares cloud history with `Super Metroid (USA).srm` from the cart.

Atomicity: write_save writes to `<path>.tmp` and renames into place. If
the cable is yanked mid-write, the prior file is intact and the next
sync recovers from scratch.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..game_id import resolve_game_id
from .base import HealthStatus, SaveRef, SourceError
from .registry import register

log = logging.getLogger(__name__)


@dataclass
class PocketConfig:
    id: str
    mount_path: str
    core: str = "agg23.SNES"
    file_extension: str = ".sav"
    system: str = "snes"
    game_aliases: dict[str, list[str]] = field(default_factory=dict)


class PocketSource:
    """SaveSource over a mounted Pocket SD filesystem.

    Public attributes `id` and `system` per the SaveSource protocol.
    """

    def __init__(self, config: PocketConfig):
        self._cfg = config
        self.id = config.id
        self.system = config.system

    @property
    def saves_dir(self) -> Path:
        return Path(self._cfg.mount_path) / "Saves" / self._cfg.core

    # ----------- SaveSource methods -----------

    async def health(self) -> HealthStatus:
        d = self.saves_dir
        if not d.exists():
            # The directory may legitimately not exist yet on a brand-new
            # Pocket; that's still "healthy" — list_saves will just return
            # an empty list. But the *mount* must exist.
            mount = Path(self._cfg.mount_path)
            if not mount.exists() or not mount.is_dir():
                return HealthStatus(False, f"mount {mount} not present")
            return HealthStatus(True, f"mounted, no {d.name}/ yet")
        if not d.is_dir():
            return HealthStatus(False, f"{d} is not a directory")
        return HealthStatus(True, f"mounted at {self._cfg.mount_path}")

    async def list_saves(self) -> list[SaveRef]:
        d = self.saves_dir
        if not d.exists():
            return []
        out: list[SaveRef] = []
        ext = self._cfg.file_extension.lower()
        try:
            for entry in sorted(d.iterdir()):
                if not entry.is_file():
                    continue
                if not entry.name.lower().endswith(ext):
                    continue
                stat = entry.stat()
                out.append(SaveRef(
                    path=str(entry),
                    size_bytes=stat.st_size,
                ))
        except OSError as exc:
            raise SourceError(f"listing {d}: {exc}") from exc
        return out

    async def read_save(self, ref: SaveRef) -> bytes:
        try:
            return Path(ref.path).read_bytes()
        except OSError as exc:
            raise SourceError(f"reading {ref.path}: {exc}") from exc

    async def write_save(self, ref: SaveRef, data: bytes) -> None:
        path = Path(ref.path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as fp:
                fp.write(data)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise SourceError(f"writing {ref.path}: {exc}") from exc

    def resolve_game_id(self, ref: SaveRef) -> str:
        return resolve_game_id(ref.path, aliases=self._cfg.game_aliases)

    # ----------- helpers used by the trigger -----------

    def canonical_save_path(self, game_id: str) -> Path:
        """Where on the SD a save for <game_id> should be written when
        bootstrap-pulling a game the device has never seen."""
        # Default convention: <slug>.sav. Operators with non-standard
        # filenames can populate the alias config to point at the right
        # place; for v0.2 we keep this simple.
        return self.saves_dir / f"{game_id}{self._cfg.file_extension}"


def _build(*, id: str, mount_path: str,
           core: str = "agg23.SNES",
           file_extension: str = ".sav",
           system: str = "snes",
           game_aliases: dict[str, list[str]] | None = None) -> PocketSource:
    return PocketSource(PocketConfig(
        id=id, mount_path=mount_path, core=core,
        file_extension=file_extension, system=system,
        game_aliases=dict(game_aliases or {}),
    ))


register("pocket", _build)
