"""EmuDeck filesystem-layout detection.

EmuDeck installs everything under either:
  - ~/Emulation/                              (default; internal SSD)
  - /run/media/mmcblk0p1/Emulation/           (SD-card mirror)
  - /run/media/<user>/<label>/Emulation/      (any SD-card mount)

Within that root, the conventions we lean on are:
  - ROMs:        <root>/roms/<system>/<game>.<rom-ext>
  - Saves:       <root>/saves/retroarch/saves/<game>.srm    (default)
                 OR <root>/storage/retroarch/saves/...      (3.x flow)

The actual saves directory is whatever RetroArch's `savefile_directory`
is set to in `retroarch.cfg` — we read that to be authoritative.

Two RetroArch install flavors:
  - Flatpak:  ~/.var/app/org.libretro.RetroArch/config/retroarch/retroarch.cfg
  - Native:   ~/.config/retroarch/retroarch.cfg

We probe the Flatpak path first since EmuDeck installs RetroArch as a
Flatpak by default.
"""
from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

EMUDECK_ROOT_CANDIDATES = (
    Path.home() / "Emulation",
    Path("/run/media/mmcblk0p1/Emulation"),
    Path("/run/media/deck/mmcblk0p1/Emulation"),
)

# Glob patterns for SD-card mounts whose mountpoint isn't the
# canonical `mmcblk0p1` (e.g. SteamOS sometimes uses the volume
# label, KDE Plasma re-mounts under /run/media/<user>/<label>/).
SD_CARD_GLOBS = (
    "/run/media/*/Emulation",
    "/run/media/*/*/Emulation",
)

RETROARCH_CFG_CANDIDATES = (
    Path.home() / ".var/app/org.libretro.RetroArch/config/retroarch/retroarch.cfg",
    Path.home() / ".config/retroarch/retroarch.cfg",
)


@dataclass
class EmuDeckPaths:
    emudeck_root: Path
    saves_root: Path
    roms_root: Path | None = None
    retroarch_cfg: Path | None = None


@dataclass
class CoreOverrideWarning:
    core_name: str
    detail: str


def detect_emudeck_root(extra: list[Path] | None = None) -> Path | None:
    """Return the first existing root, or None.

    Probe order:
      1. `extra` (operator/test overrides — `--emudeck-root` lands here)
      2. `EMUDECK_ROOT` env var
      3. fixed candidates (`~/Emulation`, the two canonical SD mounts)
      4. `/run/media/*/Emulation` and `/run/media/*/*/Emulation` globs
         for arbitrary SD-card mounts (volume label / multi-user setups)
    """
    candidates: list[Path] = list(extra or [])
    env = os.environ.get("EMUDECK_ROOT")
    if env:
        candidates.append(Path(env))
    candidates.extend(EMUDECK_ROOT_CANDIDATES)
    for pattern in SD_CARD_GLOBS:
        for match in sorted(glob.glob(pattern)):
            candidates.append(Path(match))
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_dir():
            log.info("EmuDeck root detected: %s", candidate)
            return candidate
    return None


def find_retroarch_cfg(extra: list[Path] | None = None) -> Path | None:
    for candidate in (extra or []) + list(RETROARCH_CFG_CANDIDATES):
        if candidate.is_file():
            return candidate
    return None


_CFG_KEY_RE = re.compile(r'^\s*([a-zA-Z0-9_]+)\s*=\s*"?(.*?)"?\s*$')


def parse_retroarch_cfg(path: Path) -> dict[str, str]:
    """Parse RetroArch's flat key="value" config into a dict.

    RetroArch writes ASCII / UTF-8 with quoted values. We tolerate
    unquoted values too. Comments (`#`) are skipped.
    """
    out: dict[str, str] = {}
    try:
        content = path.read_text(errors="replace")
    except OSError as exc:
        log.warning("could not read retroarch.cfg %s: %s", path, exc)
        return out
    for line in content.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        m = _CFG_KEY_RE.match(line)
        if not m:
            continue
        out[m.group(1)] = m.group(2)
    return out


def expand_retroarch_path(value: str, *,
                          retroarch_cfg: Path | None) -> Path:
    """Expand RetroArch's path tokens.

    `~` and `$HOME` are user-home; `:` (no path component) is a special
    RetroArch "default" — we map it to the cfg's parent directory.
    """
    expanded = os.path.expandvars(os.path.expanduser(value))
    if expanded in ("", ":"):
        if retroarch_cfg is not None:
            return retroarch_cfg.parent / "saves"
        return Path("saves")
    # ":/foo" syntax in some RetroArch builds means "default-relative".
    if expanded.startswith(":"):
        rel = expanded.lstrip(":/")
        if retroarch_cfg is not None:
            return retroarch_cfg.parent / rel
        return Path(rel)
    return Path(expanded)


def detect_paths(*,
                 emudeck_root_override: Path | None = None,
                 retroarch_cfg_override: Path | None = None,
                 system: str = "snes") -> EmuDeckPaths | None:
    """End-to-end discovery: returns the populated EmuDeckPaths or None.

    Honors `system` to set `roms_root` to `<emudeck_root>/roms/<system>`.
    Reads the RetroArch config for `savefile_directory` if available;
    falls back to EmuDeck's default `<root>/saves/retroarch/saves`.
    """
    root = detect_emudeck_root(
        extra=[emudeck_root_override] if emudeck_root_override else None)
    if root is None:
        return None
    cfg = find_retroarch_cfg(
        extra=[retroarch_cfg_override] if retroarch_cfg_override else None)
    saves_root: Path | None = None
    if cfg is not None:
        parsed = parse_retroarch_cfg(cfg)
        raw = parsed.get("savefile_directory", "")
        if raw:
            saves_root = expand_retroarch_path(raw, retroarch_cfg=cfg)
            log.info("RetroArch savefile_directory = %s", saves_root)
    if saves_root is None:
        saves_root = root / "saves" / "retroarch" / "saves"
        # Some EmuDeck 3.x flows put it under storage/.
        alt = root / "storage" / "retroarch" / "saves"
        if not saves_root.exists() and alt.exists():
            saves_root = alt
    roms_root = root / "roms" / system
    return EmuDeckPaths(
        emudeck_root=root,
        saves_root=saves_root,
        roms_root=roms_root,
        retroarch_cfg=cfg,
    )


def check_core_save_overrides(
        cfg_path: Path | None) -> list[CoreOverrideWarning]:
    """Spot the 'Save files in content directory' footgun.

    When that core option is enabled, RetroArch writes saves alongside
    the ROM rather than in `savefile_directory`. The Deck installer
    surfaces a warning with instructions; we don't silently fix it
    (the operator may have set it intentionally).

    Returns the list of cores that have an override of concern.
    Empty list = nothing to worry about.
    """
    if cfg_path is None or not cfg_path.exists():
        return []
    parsed = parse_retroarch_cfg(cfg_path)
    warnings: list[CoreOverrideWarning] = []
    # The relevant retroarch.cfg key is `sort_savefiles_by_content_enable`
    # / `savefiles_in_content_dir` depending on RetroArch version.
    # Cover both.
    val = (parsed.get("savefiles_in_content_dir")
           or parsed.get("sort_savefiles_by_content_enable") or "")
    if val.lower() == "true":
        warnings.append(CoreOverrideWarning(
            core_name="(global)",
            detail=("savefiles_in_content_dir is true — RetroArch will "
                    "write saves next to ROMs, not under "
                    "savefile_directory. RetroSync's inotify watcher "
                    "won't see them. Disable in RetroArch → Settings → "
                    "Saving."),
        ))
    return warnings
