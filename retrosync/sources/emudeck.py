"""EmuDeck source adapter — RetroArch saves on a Steam Deck.

Watches one EmuDeck saves directory for one system (typically `snes`
for v0.3, but the adapter is generic). The Deck-side daemon registers
one EmuDeckSource per system the operator wants synced.

Key differences from the Pocket adapter:
  - Saves live in a normal Linux directory under
    `<emudeck_root>/saves/retroarch/saves/` (or wherever
    `savefile_directory` in retroarch.cfg points). No mount/unmount.
  - Save filenames are dictated by the user's ROM filenames
    (RetroArch derives the save path from the loaded ROM path).
    Bootstrap-pull therefore consults `filename_map` (cached) → ROM
    scan to pick the right name.
  - Push triggering is via inotify, not udev. The daemon registers an
    InotifyWatcher on the saves dir; when an .srm changes, sync_one_game
    fires for that game.

The adapter itself is "look like a SaveSource" — discovery, read, write.
The push trigger is wired up by the daemon (see daemon.py).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import filename_map as fnm
from ..game_id import resolve_game_id
from .base import HealthStatus, SaveRef, SourceError
from .registry import register

log = logging.getLogger(__name__)


@dataclass
class EmuDeckConfig:
    id: str
    saves_root: str
    """Absolute path to the directory RetroArch writes saves into.
    Trumps any auto-detection — the EmuDeckSource doesn't probe
    retroarch.cfg itself; the daemon does that and passes the result."""

    roms_root: str | None = None
    """Where ROMs for this system live, used when the cloud has a save
    we need to bootstrap-pull but the device has no save yet (we have
    to derive a filename from a matching ROM). None disables bootstrap-
    pull for unknown games — they stay in cloud."""

    save_extension: str = ".srm"
    """RetroArch SNES saves are .srm. Other systems vary; configurable
    per-system via the operator's config."""

    rom_extensions: tuple[str, ...] = (".sfc", ".smc", ".swc", ".fig")
    """ROM extensions to consider when scanning for a matching ROM.
    Defaults to SNES extensions; operators with other systems pass
    their own list."""

    system: str = "snes"
    game_aliases: dict[str, list[str]] = field(default_factory=dict)
    region_preference: tuple[str, ...] = (
        "usa", "world", "europe", "japan")


class EmuDeckSource:
    """SaveSource for a directory of RetroArch save files.

    Public attributes `id`, `system`, `device_kind` per the SaveSource
    protocol. `device_kind = "deck"` so versions land under
    `versions/deck/...` regardless of the system, making cloud browsing
    distinguish Deck-authored saves from cart-authored ones at a glance.
    """

    device_kind = "deck"

    def __init__(self, config: EmuDeckConfig):
        self._cfg = config
        self.id = config.id
        self.system = config.system

    @property
    def saves_root(self) -> Path:
        return Path(self._cfg.saves_root)

    @property
    def roms_root(self) -> Path | None:
        if self._cfg.roms_root is None:
            return None
        return Path(self._cfg.roms_root)

    # ----------- SaveSource methods -----------

    async def health(self) -> HealthStatus:
        if not self.saves_root.is_dir():
            return HealthStatus(False,
                                f"saves_root {self.saves_root} missing")
        return HealthStatus(True, f"watching {self.saves_root}")

    async def list_saves(self) -> list[SaveRef]:
        d = self.saves_root
        if not d.exists():
            return []
        ext = self._cfg.save_extension.lower()
        # On EmuDeck, RetroArch puts saves for every system into the
        # same directory by default. With multiple adapters configured
        # (deck-1-snes, deck-1-n64, ...), each one would see every
        # other system's `.srm` and try to sync them — uploading N64
        # saves to `snes/<game>/`, etc. Filter to saves whose slug has
        # a matching ROM in THIS adapter's roms_root.
        #
        # Backwards-compat: if roms_root is missing or empty, don't
        # filter (single-system setups that haven't populated their
        # ROM library still work the way they always did).
        rom_slugs = self._scan_rom_slugs()
        out: list[SaveRef] = []
        try:
            for entry in sorted(d.iterdir()):
                if not entry.is_file():
                    continue
                if entry.name.startswith("._"):
                    continue
                if not entry.name.lower().endswith(ext):
                    continue
                if rom_slugs:
                    slug = resolve_game_id(
                        entry.name, aliases=self._cfg.game_aliases)
                    if slug not in rom_slugs:
                        log.debug(
                            "emudeck %s: skip %s — no %s ROM matches "
                            "slug %r",
                            self.id, entry.name, self._cfg.system, slug)
                        continue
                stat = entry.stat()
                out.append(SaveRef(
                    path=str(entry),
                    size_bytes=stat.st_size,
                ))
        except OSError as exc:
            raise SourceError(f"listing {d}: {exc}") from exc
        return out

    def _scan_rom_slugs(self) -> set[str]:
        """Build the set of canonical slugs for ROMs present in this
        adapter's `roms_root`. Used by `list_saves` to filter out
        saves whose canonical slug has no matching ROM here — those
        belong to a different system's adapter (which would have its
        own roms_root pointing at e.g. roms/n64).

        Returns an empty set if `roms_root` is None / missing / empty.
        Empty result disables filtering (preserve single-system setups
        that haven't set up roms_root).
        """
        if self.roms_root is None or not self.roms_root.exists():
            return set()
        rom_exts = tuple(e.lower() for e in self._cfg.rom_extensions)
        slugs: set[str] = set()
        try:
            for entry in self.roms_root.iterdir():
                if not entry.is_file():
                    continue
                if entry.name.startswith("._"):
                    continue
                if not entry.name.lower().endswith(rom_exts):
                    continue
                slugs.add(resolve_game_id(
                    entry.name, aliases=self._cfg.game_aliases))
        except OSError as exc:
            log.warning("emudeck %s: scanning roms_root %s failed: %s",
                        self.id, self.roms_root, exc)
            return set()
        return slugs

    async def read_save(self, ref: SaveRef) -> bytes:
        try:
            return Path(ref.path).read_bytes()
        except OSError as exc:
            raise SourceError(f"reading {ref.path}: {exc}") from exc

    async def write_save(self, ref: SaveRef, data: bytes) -> None:
        # Atomic-replace pattern. RetroArch may have the file open;
        # rename-into-place leaves any open fd pointing at the old
        # inode, which is fine because RetroArch won't read from it
        # again until the next launch (and by then we've renamed).
        target = Path(ref.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".retrosync-tmp")
        try:
            with open(tmp, "wb") as fp:
                fp.write(data)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise SourceError(f"writing {ref.path}: {exc}") from exc

    def resolve_game_id(self, ref: SaveRef) -> str:
        return resolve_game_id(Path(ref.path).name,
                               aliases=self._cfg.game_aliases)

    # ----------- adapter-specific helpers (used by daemon + wrap) -----------

    def filename_for(self, *, state, game_id: str) -> str | None:
        """Return the basename to write under saves_root for `game_id`.

        Cache hit → return the cached name. Cache miss → ROM scan via
        filename_map. None means "no ROM in roms_root for this game",
        in which case the caller should skip the bootstrap-pull and
        log a warning so the operator can drop the ROM in.
        """
        roms_root = self.roms_root
        return fnm.resolve(
            state=state, source_id=self.id, game_id=game_id,
            roms_root=roms_root, save_extension=self._cfg.save_extension,
            saves_root=self.saves_root,
            rom_extensions=self._cfg.rom_extensions,
            region_preference=self._cfg.region_preference,
            aliases=self._cfg.game_aliases,
        )

    def target_save_paths_for(self, *, state,
                              game_id: str) -> dict[str, str]:
        """Generalized per-format target paths. Single-entry dict for
        single-file systems (SNES, GBA, Genesis on EmuDeck — all use
        RetroArch's combined `.srm`); same shape as Pocket and (later)
        EverDrive 64 so engine code consumes a uniform API."""
        filename = self.filename_for(state=state, game_id=game_id)
        if filename is None:
            return {}
        ext = self._cfg.save_extension.lstrip(".") or "srm"
        return {ext: str(self.saves_root / filename)}

    def remember_filename(self, *, state, game_id: str,
                          filename: str) -> None:
        """Cache the (game_id → filename) mapping after observing a save
        for the first time. The inotify-driven push path calls this for
        every save it sees, so the bootstrap-pull side can skip the
        ROM scan when we've already learned the answer."""
        fnm.remember(state=state, source_id=self.id, game_id=game_id,
                     filename=filename)


def _build(*, id: str, saves_root: str,
           roms_root: str | None = None,
           save_extension: str = ".srm",
           rom_extensions: list[str] | None = None,
           system: str = "snes",
           game_aliases: dict[str, list[str]] | None = None,
           region_preference: list[str] | None = None) -> EmuDeckSource:
    cfg = EmuDeckConfig(
        id=id, saves_root=saves_root, roms_root=roms_root,
        save_extension=save_extension, system=system,
        game_aliases=dict(game_aliases or {}),
    )
    if rom_extensions:
        cfg.rom_extensions = tuple(rom_extensions)
    if region_preference:
        cfg.region_preference = tuple(region_preference)
    return EmuDeckSource(cfg)


register("emudeck", _build)
