"""EmuDeckSource end-to-end + wrap dispatcher unit tests."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync.cloud import RcloneCloud  # noqa: E402
from retrosync.config import Config  # noqa: E402
from retrosync.deck import emudeck_paths, wrap  # noqa: E402
from retrosync.sources.base import SaveRef  # noqa: E402
from retrosync.sources.emudeck import EmuDeckConfig, EmuDeckSource  # noqa: E402
from retrosync.state import StateStore  # noqa: E402
from retrosync.sync import (  # noqa: E402
    SyncConfig, SyncContext, SyncResult, refresh_manifest, sync_one_game,
)


def _setup() -> tuple[Path, Path, Path, StateStore, RcloneCloud]:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-deck-"))
    cloud_root = workdir / "cloud"
    cloud_root.mkdir()
    saves_root = workdir / "Emulation" / "saves" / "retroarch" / "saves"
    saves_root.mkdir(parents=True)
    roms_root = workdir / "Emulation" / "roms" / "snes"
    roms_root.mkdir(parents=True)
    fake_rclone = ROOT / "tests" / "fake_rclone.sh"
    os.chmod(fake_rclone, 0o755)
    os.environ["FAKE_RCLONE_ROOT"] = str(cloud_root)
    state = StateStore(str(workdir / "state.db"))
    cloud = RcloneCloud(remote="gdrive:retro-saves",
                        binary=str(fake_rclone))
    return workdir, saves_root, roms_root, state, cloud


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


async def test_emudeck_lists_and_uploads() -> bool:
    """Save under saves_root → list_saves picks it up → bootstrap upload."""
    workdir, saves_root, roms_root, state, cloud = _setup()
    save = saves_root / "Super Metroid (USA).srm"
    save.write_bytes(b"DECK-SAVE" + b"\x00" * 100)

    src = EmuDeckSource(EmuDeckConfig(
        id="deck-1", saves_root=str(saves_root),
        roms_root=str(roms_root), system="snes"))
    state.upsert_source(id=src.id, system=src.system,
                        adapter="EmuDeckSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    saves = await src.list_saves()
    if not _check(len(saves), 1, "EmuDeck saves discovered"):
        state.close()
        return False
    out = await sync_one_game(source=src, ref=saves[0], ctx=ctx)
    state.close()
    return _check(out.result, SyncResult.BOOTSTRAP_UPLOADED,
                  "EmuDeck save → bootstrap upload")


def test_emudeck_filename_for_uses_rom_scan() -> bool:
    """filename_for(game_id) consults the ROM scan when no cached
    filename exists."""
    workdir, saves_root, roms_root, state, cloud = _setup()
    (roms_root / "Chrono Trigger (USA).sfc").write_bytes(b"x")
    src = EmuDeckSource(EmuDeckConfig(
        id="deck-1", saves_root=str(saves_root),
        roms_root=str(roms_root), system="snes"))
    name = src.filename_for(state=state, game_id="chrono_trigger")
    state.close()
    return _check(name, "Chrono Trigger (USA).srm",
                  "ROM-stem-derived save filename")


def test_emudeck_remember_filename() -> bool:
    """remember_filename() caches the (game_id, filename) pair."""
    workdir, saves_root, roms_root, state, cloud = _setup()
    src = EmuDeckSource(EmuDeckConfig(
        id="deck-1", saves_root=str(saves_root),
        roms_root=str(roms_root), system="snes"))
    src.remember_filename(state=state, game_id="zelda",
                          filename="A Link to the Past.srm")
    cached = state.get_filename_map("deck-1", "zelda")
    state.close()
    return _check(cached["filename"], "A Link to the Past.srm",
                  "remember_filename → cache hit")


def test_extract_rom_from_args() -> bool:
    """wrap-extract-rom finds the ROM path among emulator args."""
    workdir, saves_root, roms_root, state, cloud = _setup()
    rom = roms_root / "Foo.sfc"
    rom.write_bytes(b"x")
    found = wrap.extract_rom_from_args([
        "/usr/bin/retroarch", "-L", "/libretro/snes9x.so", str(rom)])
    return _check(found, rom, "extract_rom_from_args picks the ROM")


def test_derive_from_rom_emudeck_path() -> bool:
    """derive_from_rom maps a ROM under emudeck_root/roms/<system>/ to
    (system, slug)."""
    workdir, saves_root, roms_root, state, cloud = _setup()
    rom = roms_root / "Super Metroid (USA).sfc"
    rom.write_bytes(b"x")
    derived = wrap.derive_from_rom(
        rom, emudeck_root=workdir / "Emulation")
    state.close()
    ok = _check(derived.system, "snes", "system inferred from path")
    ok &= _check(derived.game_id, "super_metroid", "canonical slug")
    return ok


def test_emudeck_paths_parses_savefile_directory() -> bool:
    """parse_retroarch_cfg pulls savefile_directory out of a real-shape
    retroarch.cfg."""
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-cfg-"))
    cfg = workdir / "retroarch.cfg"
    cfg.write_text(
        '# autogenerated\n'
        'savefile_directory = "/home/deck/Emulation/saves/retroarch/saves"\n'
        'savefiles_in_content_dir = "false"\n'
    )
    parsed = emudeck_paths.parse_retroarch_cfg(cfg)
    return _check(parsed.get("savefile_directory"),
                  "/home/deck/Emulation/saves/retroarch/saves",
                  "savefile_directory parsed")


def _make_resolve_config(sources: list[dict]):
    """Tiny config-shaped object for resolve_source_id tests — has
    just enough surface area for the function to introspect."""
    from retrosync.config import SourceConfig

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.sources = [SourceConfig(id=s["id"], adapter=s["adapter"],
                                options=s.get("options", {}))
                   for s in sources]
    return cfg


def test_resolve_source_id_uses_matching_system() -> bool:
    """The wrap dispatcher hardcodes SOURCE_ID=deck-1, but in a
    multi-system config that's the SNES source. When N64 launches,
    resolve_source_id picks the N64 source instead."""
    cfg = _make_resolve_config([
        {"id": "deck-1-snes", "adapter": "emudeck",
         "options": {"system": "snes"}},
        {"id": "deck-1-n64", "adapter": "emudeck",
         "options": {"system": "n64"}},
    ])
    out = wrap.resolve_source_id(
        source_id="deck-1", system="n64", config=cfg)
    return _check(out, "deck-1-n64",
                  "wrong source_id auto-corrected by system match")


def test_resolve_source_id_keeps_correct_source() -> bool:
    """If the passed source_id already matches the system, leave it
    alone — don't switch to a different source for the same system."""
    cfg = _make_resolve_config([
        {"id": "deck-2-n64", "adapter": "emudeck",
         "options": {"system": "n64"}},
        {"id": "deck-1-n64", "adapter": "emudeck",
         "options": {"system": "n64"}},
    ])
    out = wrap.resolve_source_id(
        source_id="deck-2-n64", system="n64", config=cfg)
    return _check(out, "deck-2-n64",
                  "correct source_id preserved")


def test_resolve_source_id_no_match_passes_through() -> bool:
    """No emudeck source for `system` → return source_id unchanged so
    the caller can fail-open and just launch the emulator."""
    cfg = _make_resolve_config([
        {"id": "deck-1-snes", "adapter": "emudeck",
         "options": {"system": "snes"}},
    ])
    out = wrap.resolve_source_id(
        source_id="deck-1", system="genesis", config=cfg)
    return _check(out, "deck-1",
                  "no match → original source_id passed through")


async def test_list_saves_filters_by_system_via_roms_root() -> bool:
    """Multi-system setups share saves_root across adapters. Each
    adapter must filter to saves whose slug has a matching ROM in
    ITS own roms_root, otherwise the SNES adapter picks up the N64
    saves and uploads them to the wrong cloud system."""
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-multisys-"))
    saves_root = workdir / "saves"
    saves_root.mkdir()
    snes_roms = workdir / "roms" / "snes"
    snes_roms.mkdir(parents=True)
    n64_roms = workdir / "roms" / "n64"
    n64_roms.mkdir(parents=True)

    # Put one ROM per system.
    (snes_roms / "Super Metroid (USA).sfc").write_bytes(b"x")
    (n64_roms / "Star Wars - Shadows of the Empire (U) (V1.2) [!].z64"
     ).write_bytes(b"x")

    # Both saves land in the same shared dir (RetroArch's default).
    (saves_root / "Super Metroid (USA).srm").write_bytes(b"snes-save")
    (saves_root / "Star Wars - Shadows of the Empire (U) (V1.2) [!].srm"
     ).write_bytes(b"n64-save")

    snes = EmuDeckSource(EmuDeckConfig(
        id="deck-1-snes", saves_root=str(saves_root),
        roms_root=str(snes_roms), system="snes"))
    n64 = EmuDeckSource(EmuDeckConfig(
        id="deck-1-n64", saves_root=str(saves_root),
        roms_root=str(n64_roms), system="n64",
        rom_extensions=(".z64", ".n64", ".v64")))

    snes_saves = sorted(s.path for s in await snes.list_saves())
    n64_saves = sorted(s.path for s in await n64.list_saves())

    ok = _check(snes_saves, [str(saves_root / "Super Metroid (USA).srm")],
                "SNES adapter sees only SNES save")
    ok &= _check(n64_saves,
                 [str(saves_root /
                      "Star Wars - Shadows of the Empire (U) (V1.2) [!].srm")],
                 "N64 adapter sees only N64 save")
    return ok


async def test_list_saves_no_roms_root_disables_filter() -> bool:
    """Single-system setups that haven't populated roms_root yet
    (or operators using only the cart side) shouldn't get saves
    filtered out. Empty roms_root → no filtering, keep old behavior."""
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-noroms-"))
    saves_root = workdir / "saves"
    saves_root.mkdir()
    empty_roms = workdir / "roms" / "snes"
    empty_roms.mkdir(parents=True)
    (saves_root / "Some Game.srm").write_bytes(b"save")

    src = EmuDeckSource(EmuDeckConfig(
        id="deck-1", saves_root=str(saves_root),
        roms_root=str(empty_roms), system="snes"))
    saves = sorted(s.path for s in await src.list_saves())
    return _check(saves, [str(saves_root / "Some Game.srm")],
                  "empty roms_root preserves all saves")


def test_check_core_save_overrides_flags_footgun() -> bool:
    """savefiles_in_content_dir=true triggers a warning."""
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-cfg-"))
    cfg = workdir / "retroarch.cfg"
    cfg.write_text('savefiles_in_content_dir = "true"\n')
    warnings = emudeck_paths.check_core_save_overrides(cfg)
    return _check(len(warnings), 1, "warning emitted on footgun setting")


def main() -> int:
    ok = True
    for name, fn in [
        ("emudeck_lists_and_uploads", test_emudeck_lists_and_uploads),
        ("list_saves_filters_by_system_via_roms_root",
         test_list_saves_filters_by_system_via_roms_root),
        ("list_saves_no_roms_root_disables_filter",
         test_list_saves_no_roms_root_disables_filter),
    ]:
        print(f"--- {name} ---")
        ok &= asyncio.run(fn())
    for name, fn in [
        ("emudeck_filename_for_uses_rom_scan",
         test_emudeck_filename_for_uses_rom_scan),
        ("emudeck_remember_filename", test_emudeck_remember_filename),
        ("extract_rom_from_args", test_extract_rom_from_args),
        ("derive_from_rom_emudeck_path", test_derive_from_rom_emudeck_path),
        ("resolve_source_id_uses_matching_system",
         test_resolve_source_id_uses_matching_system),
        ("resolve_source_id_keeps_correct_source",
         test_resolve_source_id_keeps_correct_source),
        ("resolve_source_id_no_match_passes_through",
         test_resolve_source_id_no_match_passes_through),
        ("emudeck_paths_parses_savefile_directory",
         test_emudeck_paths_parses_savefile_directory),
        ("check_core_save_overrides_flags_footgun",
         test_check_core_save_overrides_flags_footgun),
    ]:
        print(f"--- {name} ---")
        ok &= fn()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
