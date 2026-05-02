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
    # Path under <mount>/Saves/ where the SNES core writes save files.
    # Empirically the openFPGA SNES core (agg23.SNES) writes to the shared
    # `snes/common/` directory rather than its own `agg23.SNES/` folder.
    # Operators with an unusual core can override.
    core: str = "snes/common"
    file_extension: str = ".sav"
    system: str = "snes"
    game_aliases: dict[str, list[str]] = field(default_factory=dict)
    # ROM extensions to scan when looking up a ROM filename for a game
    # we've never seen on the Pocket. The Pocket loads saves by ROM-stem
    # match, so a fresh save needs to be named after one of these ROMs.
    rom_extensions: tuple[str, ...] = (".smc", ".sfc")
    # Optional override: where to look for ROMs. Empty = mirror `core`'s
    # path under Assets/ (i.e. Assets/snes/common when core=snes/common).
    assets_subpath: str = ""
    # Region preference for picking among multiple ROMs that resolve to
    # the same game_id (e.g. USA + Europe + Japan dumps). The first
    # marker that case-insensitively appears in the filename wins.
    region_preference: tuple[str, ...] = (
        "usa", "world", "europe", "japan")


class PocketSource:
    """SaveSource over a mounted Pocket SD filesystem.

    Public attributes `id`, `system`, `device_kind` per the SaveSource
    protocol. `device_kind = "pocket"` keeps Pocket-authored versions
    visually grouped under `versions/pocket/...` regardless of which
    core (snes, nes, ...) they came from.
    """

    device_kind = "pocket"

    def __init__(self, config: PocketConfig):
        self._cfg = config
        self.id = config.id
        self.system = config.system

    @property
    def saves_dir(self) -> Path:
        # `core` may contain slashes ("snes/common") for cores that share
        # a save directory with the platform's other cores.
        return Path(self._cfg.mount_path) / "Saves" / self._cfg.core

    @property
    def assets_dir(self) -> Path:
        """Where the Pocket keeps ROMs for this core. Defaults to mirroring
        the saves layout (Assets/<core>/) — same convention openFPGA
        uses to pair Saves/ and Assets/."""
        sub = self._cfg.assets_subpath or self._cfg.core
        return Path(self._cfg.mount_path) / "Assets" / sub

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

    async def currently_playing_game_id(self) -> str | None:
        return None

    async def list_saves(self) -> list[SaveRef]:
        d = self.saves_dir
        if not d.exists():
            return []
        ext = self._cfg.file_extension.lower()
        candidates: list[SaveRef] = []
        try:
            for entry in sorted(d.iterdir()):
                if not entry.is_file():
                    continue
                # Skip macOS metadata sidecars (e.g. `._Final Fantasy.sav`)
                # that appear when the SD has been mounted on a Mac.
                if entry.name.startswith("._"):
                    continue
                if not entry.name.lower().endswith(ext):
                    continue
                stat = entry.stat()
                candidates.append(SaveRef(
                    path=str(entry),
                    size_bytes=stat.st_size,
                ))
        except OSError as exc:
            raise SourceError(f"listing {d}: {exc}") from exc

        # Dedupe by canonical game_id: when multiple files map to the
        # same game (e.g. a ROM-decorated original and a slug-named copy
        # from an earlier `load`), the engine would otherwise upload
        # both as separate versions on every sync. Pick the ROM-decorated
        # name if present (the Pocket loads that one at boot anyway);
        # warn about the others.
        by_game: dict[str, list[SaveRef]] = {}
        for ref in candidates:
            slug = resolve_game_id(Path(ref.path).name,
                                   aliases=self._cfg.game_aliases)
            by_game.setdefault(slug, []).append(ref)
        out: list[SaveRef] = []
        for slug, refs in by_game.items():
            if len(refs) == 1:
                out.append(refs[0])
                continue
            canonical_name = f"{slug}{ext}"
            decorated = [r for r in refs
                         if Path(r.path).name != canonical_name]
            chosen = decorated[0] if decorated else refs[0]
            others = [r for r in refs if r.path != chosen.path]
            log.warning(
                "Pocket %s: %d files map to game_id %r — using %s; "
                "other(s) will be ignored: %s. Delete the unused file(s) "
                "to silence this warning.",
                self.id, len(refs), slug, Path(chosen.path).name,
                [Path(r.path).name for r in others])
            out.append(chosen)
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

    def find_rom_for(self, game_id: str) -> Path | None:
        """Scan `assets_dir` for ROM files whose canonical slug matches
        `game_id`. Returns the best match, preferring USA region dumps
        (configurable via PocketConfig.region_preference). Returns None
        if no matching ROM exists.
        """
        if not self.assets_dir.exists():
            return None
        exts = tuple(e.lower() for e in self._cfg.rom_extensions)
        candidates: list[Path] = []
        try:
            for entry in self.assets_dir.iterdir():
                if not entry.is_file():
                    continue
                if entry.name.startswith("._"):
                    continue
                if not entry.name.lower().endswith(exts):
                    continue
                slug = resolve_game_id(entry.name,
                                       aliases=self._cfg.game_aliases)
                if slug == game_id:
                    candidates.append(entry)
        except OSError as exc:
            log.warning("find_rom_for: scanning %s failed: %s",
                        self.assets_dir, exc)
            return None
        if not candidates:
            return None
        # Lowest priority value wins; alphabetic for stability tiebreaker.
        prefs = tuple(p.lower() for p in self._cfg.region_preference)
        candidates.sort(key=lambda p: (
            _region_priority(p.name, prefs), p.name))
        return candidates[0]

    def target_save_path_for(self, game_id: str) -> Path:
        """Decide where to write a save for `game_id` on the Pocket.

        Priority:
          1. An existing on-device save matching the game (the Pocket
             already loads this file at boot).
          2. A ROM in assets_dir matching the game — use its stem +
             save extension. Prefers USA dumps over EUR/JP.
          3. Fall back to the slug-based filename. The Pocket likely
             won't load this — caller should warn the operator.
        """
        existing = self.existing_save_for(game_id)
        if existing is not None:
            return existing
        rom = self.find_rom_for(game_id)
        if rom is not None:
            return self.saves_dir / (rom.stem + self._cfg.file_extension)
        return self.canonical_save_path(game_id)

    def target_save_paths_for(self, game_id: str) -> dict[str, str]:
        """Generalized form of `target_save_path_for` — returns a
        single-entry dict keyed by save extension.

        Multi-format adapters (EverDrive 64) return multiple entries.
        Single-file adapters always return a one-entry dict. Callers
        that don't care about format multiplicity can iterate the
        values uniformly.
        """
        ext = self._cfg.file_extension.lstrip(".") or "sav"
        return {ext: str(self.target_save_path_for(game_id))}

    def existing_save_for(self, game_id: str) -> Path | None:
        """Return the on-device save file whose canonical slug matches
        `game_id`, if one already exists. The Pocket loads saves by
        ROM-filename match (e.g. `Final Fantasy III (U) (v1.1).sav`),
        not by slug, so a `load` operation needs to overwrite the
        existing file rather than create a new slug-named one that the
        ROM won't find.
        """
        if not self.saves_dir.exists():
            log.warning("existing_save_for: saves dir %s does not exist",
                        self.saves_dir)
            return None
        ext = self._cfg.file_extension.lower()
        # Iterate twice: once for visibility, once to find a match. The
        # first pass logs what we see so a "no match" outcome can be
        # debugged from the operator's terminal output.
        candidates: list[tuple[Path, str]] = []
        for entry in self.saves_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.name.startswith("._"):
                continue
            if not entry.name.lower().endswith(ext):
                continue
            slug = resolve_game_id(entry.name,
                                   aliases=self._cfg.game_aliases)
            candidates.append((entry, slug))
        matches = [(entry, slug) for entry, slug in candidates
                   if slug == game_id]
        if not matches:
            log.warning(
                "existing_save_for(%r): no match among %d file(s) in %s. "
                "Saw: %s",
                game_id, len(candidates), self.saves_dir,
                [(c.name, s) for c, s in candidates])
            return None
        # When multiple files share the same canonical slug — typically
        # because a previous `load` wrote the slug-named fallback
        # alongside the original ROM-named save — prefer the ROM-named
        # one (the only one the Pocket actually loads).
        canonical_name = f"{game_id}{ext}"
        for entry, _slug in matches:
            if entry.name != canonical_name:
                return entry
        return matches[0][0]


# Region tags as they typically appear inside parens in dumped ROM
# filenames: "USA", "U", "Europe", "E", "Japan", "J", "World", "W".
# We collapse single-letter forms into the full word for matching.
_SINGLE_LETTER_REGIONS = {"u": "usa", "e": "europe", "j": "japan",
                          "w": "world"}


def _region_priority(filename: str,
                     preference: tuple[str, ...]) -> int:
    """Return the index of the first preference that matches the filename
    (case-insensitive substring on the full or single-letter region tag).
    Lower = better. Returns len(preference) for filenames with no
    recognized region tag (so they sort last but still sort)."""
    lname = filename.lower()
    for i, want in enumerate(preference):
        # Full-word match (e.g. "(usa," / "(usa)" / "(usa, ").
        if want in lname:
            return i
        # Single-letter equivalent (e.g. "(u)" → usa).
        for letter, full in _SINGLE_LETTER_REGIONS.items():
            if full == want and (
                    f"({letter})" in lname
                    or f"({letter}," in lname
                    or f", {letter})" in lname
                    or f", {letter}," in lname):
                return i
    return len(preference)


def _build(*, id: str, mount_path: str,
           core: str = "snes/common",
           file_extension: str = ".sav",
           system: str = "snes",
           game_aliases: dict[str, list[str]] | None = None,
           rom_extensions: list[str] | None = None,
           assets_subpath: str = "",
           region_preference: list[str] | None = None) -> PocketSource:
    cfg = PocketConfig(
        id=id, mount_path=mount_path, core=core,
        file_extension=file_extension, system=system,
        game_aliases=dict(game_aliases or {}),
        assets_subpath=assets_subpath,
    )
    if rom_extensions:
        cfg.rom_extensions = tuple(rom_extensions)
    if region_preference:
        cfg.region_preference = tuple(region_preference)
    return PocketSource(cfg)


register("pocket", _build)
