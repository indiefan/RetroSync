"""Save filename ↔ game-id reconciliation for ROM-named save sources.

The Pocket loads saves by ROM-stem match (`Foo (USA).smc` →
`Foo (USA).sav`). EmuDeck's RetroArch does the same — `savefile_directory/
<rom-stem>.srm`. So when the cloud has a save we want to bootstrap onto a
device that doesn't have one yet, we have to figure out *what filename
to write under*. The filename has to match the ROM's stem so the
emulator finds it on launch.

Two layers:
  1. `state.device_filename_map` caches the answer per (source, game_id).
     Populated when the source first sees a save for that game, OR when
     a bootstrap-pull successfully writes one.
  2. `scan_roms_for_game` is the cache-miss path: scan a roms directory
     for ROMs whose canonical slug matches the target game_id, return
     the best match.

Cache invalidation: a stale cache could point at a file that no longer
exists on disk (user deleted the ROM, switched dumps, etc). Callers
that act on the cache should `verify_or_invalidate` first; the
periodic-reconciliation case can `purge_stale` over the whole table.

This module is adapter-agnostic — it doesn't import anything emulator-
specific. EmuDeckSource and PocketSource (in the future) both use it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .game_id import canonical_slug, resolve_game_id
from .state import StateStore

log = logging.getLogger(__name__)


@dataclass
class RomMatch:
    """Result of a ROM-directory scan for one game_id."""
    rom_path: Path
    """The matched ROM file."""
    save_filename: str
    """The save filename to write under: <rom-stem><save_extension>."""

    @property
    def rom_stem(self) -> str:
        return self.rom_path.stem


def scan_roms_for_game(*, roms_root: Path, game_id: str,
                       save_extension: str,
                       rom_extensions: tuple[str, ...] = (
                           ".sfc", ".smc", ".swc", ".fig",
                           ".gb", ".gbc", ".gba",
                           ".nes", ".md", ".gen"),
                       region_preference: tuple[str, ...] = (
                           "usa", "world", "europe", "japan"),
                       aliases: dict[str, list[str]] | None = None,
                       ) -> RomMatch | None:
    """Scan `roms_root` for ROM files whose canonical slug matches
    `game_id`. Returns the best match, or None if nothing matches.

    Selection rules per design §7.2:
      - Prefer matches whose stem normalizes WITHOUT alias-stripping —
        the most "literal" name.
      - Tie-break by region preference (USA-first by default).
      - Final tie-break: most recently modified (proxy for "user's
        primary copy").
    """
    if not roms_root.exists():
        return None
    rom_extensions = tuple(e.lower() for e in rom_extensions)
    region_preference = tuple(p.lower() for p in region_preference)
    candidates: list[Path] = []
    try:
        for entry in roms_root.iterdir():
            if not entry.is_file():
                continue
            if entry.name.startswith("._"):
                continue
            if not entry.name.lower().endswith(rom_extensions):
                continue
            slug = resolve_game_id(entry.name, aliases=aliases)
            if slug != game_id:
                continue
            candidates.append(entry)
    except OSError as exc:
        log.warning("scan_roms_for_game: scanning %s failed: %s",
                    roms_root, exc)
        return None
    if not candidates:
        return None
    # Prefer the most "literal" match (canonical_slug == game_id without
    # needing the alias table); then USA region; then mtime.
    def sort_key(p: Path) -> tuple:
        literal_match = (canonical_slug(p.name) == game_id)
        region_rank = _region_priority(p.name, region_preference)
        return (
            0 if literal_match else 1,
            region_rank,
            -p.stat().st_mtime,
            p.name,
        )
    candidates.sort(key=sort_key)
    chosen = candidates[0]
    if len(candidates) > 1:
        log.info("scan_roms_for_game(%r): %d ROMs match — picked %s",
                 game_id, len(candidates), chosen.name)
    return RomMatch(
        rom_path=chosen,
        save_filename=chosen.stem + save_extension,
    )


# --------------------------------------------------------------------------
# Cache layer: device_filename_map table in state.db.
# --------------------------------------------------------------------------


def remember(*, state: StateStore, source_id: str, game_id: str,
             filename: str, rom_stem: str | None = None) -> None:
    """Insert / update the (source, game_id) → filename row.

    Callers should set `rom_stem` when they derived the filename from a
    ROM scan; the field is informational (lets `retrosync filename-map
    list` show which ROM we matched against)."""
    state.set_filename_map(source_id=source_id, game_id=game_id,
                           filename=filename, rom_stem=rom_stem)


def lookup(*, state: StateStore, source_id: str,
           game_id: str) -> str | None:
    row = state.get_filename_map(source_id, game_id)
    return row["filename"] if row else None


def resolve(*, state: StateStore, source_id: str, game_id: str,
            roms_root: Path | None, save_extension: str,
            saves_root: Path | None = None,
            aliases: dict[str, list[str]] | None = None,
            **scan_kwargs) -> str | None:
    """Full filename lookup: cache hit, then ROM scan, then None.

    `saves_root` is consulted to verify the cached filename still
    actually exists on disk; if not, the cache entry is invalidated and
    we fall through to a fresh ROM scan. Skip `saves_root` when the
    caller is the upload-side and just wants what's stashed.

    Returns the canonical save filename (no path prefix). Caller joins
    with their own saves dir to write."""
    cached = lookup(state=state, source_id=source_id, game_id=game_id)
    if cached is not None:
        if saves_root is None or (saves_root / cached).exists():
            return cached
        log.info("filename_map: cached %s/%s → %s no longer exists; "
                 "invalidating", source_id, game_id, cached)
        state.invalidate_filename_map(source_id, game_id)
    if roms_root is None:
        return None
    match = scan_roms_for_game(
        roms_root=roms_root, game_id=game_id,
        save_extension=save_extension, aliases=aliases, **scan_kwargs)
    if match is None:
        return None
    remember(state=state, source_id=source_id, game_id=game_id,
             filename=match.save_filename, rom_stem=match.rom_stem)
    return match.save_filename


def purge_stale(*, state: StateStore, source_id: str,
                roms_root: Path) -> int:
    """Walk every map row for `source_id`; drop entries whose
    backing ROM no longer exists. Returns the count purged.

    Cheap scan over the table — meant for periodic reconciliation
    (daily, e.g. via a systemd timer). The caller knows the
    saves_root + roms_root layout for this source.
    """
    purged = 0
    for row in state.list_filename_map(source_id=source_id):
        rom_stem = row.get("rom_stem")
        if not rom_stem:
            continue
        # Probe a few common ROM extensions; we don't store the original
        # extension, so we have to guess. If any match exists we keep
        # the row.
        any_exists = any(
            (roms_root / f"{rom_stem}{ext}").exists()
            for ext in (".sfc", ".smc", ".swc", ".fig",
                        ".gb", ".gbc", ".gba", ".nes",
                        ".md", ".gen"))
        if not any_exists:
            state.invalidate_filename_map(source_id, row["game_id"])
            purged += 1
    return purged


_SINGLE_LETTER_REGIONS = {"u": "usa", "e": "europe", "j": "japan",
                          "w": "world"}


def _region_priority(filename: str,
                     preference: tuple[str, ...]) -> int:
    """Index of the first preference matched in the filename's region
    tags. Lower = better. len(preference) for filenames with no
    recognized region tag (so they sort last)."""
    lname = filename.lower()
    for i, want in enumerate(preference):
        if want in lname:
            return i
        for letter, full in _SINGLE_LETTER_REGIONS.items():
            if full == want and (
                    f"({letter})" in lname
                    or f"({letter}," in lname
                    or f", {letter})" in lname
                    or f", {letter}," in lname):
                return i
    return len(preference)
