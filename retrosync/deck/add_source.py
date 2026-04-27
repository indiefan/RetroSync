"""Append an EmuDeck source block to an existing config.yaml.

Used by `retrosync deck add-source` so adding a new system (e.g. N64
on a Deck that already has SNES set up) doesn't require hand-editing
YAML. Appends raw text so existing comments / formatting survive (a
PyYAML round-trip would erase them).

The detection chain mirrors setup-deck.sh:
  1. detect_paths(system) — picks `<emudeck_root>/roms/<system>` and
     reads RetroArch's savefile_directory for saves.
  2. If the resulting roms_root doesn't exist, probe SD-card mounts
     for one that does.
  3. ROMS_ROOT env var (caller-provided) wins over both.
"""
from __future__ import annotations

import glob
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import emudeck_paths
from . import systems as deck_systems

log = logging.getLogger(__name__)


SD_CARD_GLOBS = (
    "/run/media/mmcblk0p1/Emulation",
    "/run/media/deck/mmcblk0p1/Emulation",
    "/run/media/*/Emulation",
    "/run/media/*/*/Emulation",
)


@dataclass
class AddSourceResult:
    source_id: str
    system: str
    saves_root: Path
    roms_root: Path
    appended_to: Path
    block_text: str


def resolve_roms_root(system: str, default: Path,
                      override: Path | None = None) -> Path:
    """Pick a roms_root for `system`. Mirrors setup-deck.sh:
       1. explicit override (e.g. ROMS_ROOT env var)
       2. `default` if it exists (typically `<emudeck_root>/roms/<system>`)
       3. probe SD-card mounts for `<sd>/roms/<system>`
       4. fall back to `default` (caller may warn)
    """
    if override is not None:
        return override
    if default.is_dir():
        return default
    for pattern in SD_CARD_GLOBS:
        for match in glob.glob(pattern):
            cand = Path(match) / "roms" / system
            if cand.is_dir():
                log.info("ROMs found on SD card: %s", cand)
                return cand
    return default


def render_source_block(*, source_id: str, system: str,
                        saves_root: Path, roms_root: Path,
                        rom_extensions: tuple[str, ...],
                        save_extension: str) -> str:
    rom_exts = ", ".join(f'"{e}"' for e in rom_extensions)
    return (
        f"\n  - id: {source_id}\n"
        f"    adapter: emudeck\n"
        f"    options:\n"
        f"      saves_root: {saves_root}\n"
        f"      roms_root:  {roms_root}\n"
        f"      save_extension: {save_extension}\n"
        f"      rom_extensions: [{rom_exts}]\n"
        f"      system: {system}\n")


def existing_source_ids(config_path: Path) -> dict[str, dict]:
    """Parse the YAML and return {source_id: source_dict}. Used to
    skip duplicates and to build a unique id when one's auto-generated.
    """
    if not config_path.is_file():
        return {}
    with config_path.open() as fp:
        raw = yaml.safe_load(fp) or {}
    return {s["id"]: s for s in (raw.get("sources") or [])
            if isinstance(s, dict) and "id" in s}


def derive_source_id(system: str, existing: dict[str, dict],
                     base: str = "deck-1") -> str:
    """Pick an unused id like `deck-1-n64`. If `deck-1-n64` is taken,
    try `deck-2-n64`, etc."""
    candidate = f"{base}-{system}"
    if candidate not in existing:
        return candidate
    n = 2
    while True:
        candidate = f"deck-{n}-{system}"
        if candidate not in existing:
            return candidate
        n += 1


class AddSourceError(Exception):
    pass


def add_source(*, config_path: Path, system: str,
               emudeck_root_override: Path | None = None,
               saves_root_override: Path | None = None,
               roms_root_override: Path | None = None,
               source_id: str | None = None) -> AddSourceResult:
    """Detect paths and append an `emudeck` source block for `system`
    to `config_path`. Idempotent — bails if a source for the same
    system already exists.
    """
    sys_def = deck_systems.get(system)
    existing = existing_source_ids(config_path)
    for sid, sdict in existing.items():
        if sdict.get("adapter") == "emudeck" \
                and (sdict.get("options") or {}).get("system") == system:
            raise AddSourceError(
                f"source {sid!r} already configured for system "
                f"{system!r}; nothing to do")

    paths = emudeck_paths.detect_paths(
        system=system,
        emudeck_root_override=emudeck_root_override)
    if paths is None:
        raise AddSourceError(
            "EmuDeck install not detected. Set EMUDECK_ROOT to your "
            "Emulation/ directory, or pass --emudeck-root.")

    saves_root = saves_root_override or paths.saves_root
    if not saves_root.is_dir():
        raise AddSourceError(
            f"saves_root {saves_root} doesn't exist. Confirm RetroArch "
            "is set up for this system, or pass --saves-root.")

    roms_root = resolve_roms_root(
        system=system,
        default=paths.roms_root or (paths.emudeck_root / "roms" / system),
        override=roms_root_override)
    if not roms_root.is_dir():
        raise AddSourceError(
            f"roms_root {roms_root} doesn't exist. Drop your {system} "
            f"ROMs there, or pass --roms-root.")

    sid = source_id or derive_source_id(system, existing)
    block = render_source_block(
        source_id=sid, system=system,
        saves_root=saves_root, roms_root=roms_root,
        rom_extensions=sys_def.rom_extensions,
        save_extension=sys_def.save_extension)

    if not config_path.is_file():
        raise AddSourceError(
            f"config {config_path} not found. Run setup-deck.sh first.")

    body = config_path.read_text()
    if "sources:" not in body:
        raise AddSourceError(
            f"config {config_path} has no `sources:` key. Edit by hand.")
    # An empty inline list (`sources: []`) won't accept a block-list
    # append below it. Convert to block form first.
    body = re.sub(r"^sources:\s*\[\s*\]\s*$", "sources:",
                  body, flags=re.MULTILINE)
    if not body.endswith("\n"):
        body += "\n"
    new_body = body + block
    # Verify it still parses before writing.
    try:
        yaml.safe_load(new_body)
    except yaml.YAMLError as exc:
        raise AddSourceError(f"appended block broke YAML parse: {exc}")
    config_path.write_text(new_body)

    return AddSourceResult(
        source_id=sid, system=system,
        saves_root=saves_root, roms_root=roms_root,
        appended_to=config_path, block_text=block)
