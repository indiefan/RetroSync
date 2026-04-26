"""filename_map: ROM-scan + cache for ROM-stem-named save sources."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync import filename_map  # noqa: E402
from retrosync.state import StateStore  # noqa: E402


def _setup() -> tuple[Path, StateStore]:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-fnm-"))
    state = StateStore(str(workdir / "state.db"))
    return workdir, state


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


def test_scan_picks_usa_dump() -> bool:
    workdir, state = _setup()
    roms = workdir / "roms"
    roms.mkdir()
    (roms / "Super Metroid (USA).sfc").write_bytes(b"x")
    (roms / "Super Metroid (Japan).sfc").write_bytes(b"x")
    (roms / "Super Metroid (Europe).sfc").write_bytes(b"x")
    match = filename_map.scan_roms_for_game(
        roms_root=roms, game_id="super_metroid",
        save_extension=".srm")
    state.close()
    return _check(match.save_filename if match else None,
                  "Super Metroid (USA).srm",
                  "USA dump preferred")


def test_scan_returns_none_when_no_match() -> bool:
    workdir, state = _setup()
    roms = workdir / "roms"
    roms.mkdir()
    (roms / "Other Game.sfc").write_bytes(b"x")
    match = filename_map.scan_roms_for_game(
        roms_root=roms, game_id="super_metroid",
        save_extension=".srm")
    state.close()
    return _check(match, None, "no matching ROM → None")


def test_resolve_caches_after_scan() -> bool:
    """Subsequent resolve() returns cached filename, no rescan needed."""
    workdir, state = _setup()
    roms = workdir / "roms"
    roms.mkdir()
    (roms / "Super Metroid (USA).sfc").write_bytes(b"x")
    saves = workdir / "saves"
    saves.mkdir()
    name1 = filename_map.resolve(
        state=state, source_id="deck-1", game_id="super_metroid",
        roms_root=roms, save_extension=".srm")
    cached = state.get_filename_map("deck-1", "super_metroid")
    name2 = filename_map.resolve(
        state=state, source_id="deck-1", game_id="super_metroid",
        roms_root=None, save_extension=".srm")  # roms_root=None proves cache hit
    state.close()
    ok = _check(name1, "Super Metroid (USA).srm", "first resolve scans")
    ok &= _check(cached["filename"], "Super Metroid (USA).srm",
                 "cached row exists")
    ok &= _check(name2, name1, "second resolve hits cache")
    return ok


def test_resolve_invalidates_when_file_missing() -> bool:
    """Cached filename whose backing save file no longer exists →
    invalidated, fresh scan happens."""
    workdir, state = _setup()
    roms = workdir / "roms"
    roms.mkdir()
    (roms / "Super Metroid (USA).sfc").write_bytes(b"x")
    saves = workdir / "saves"
    saves.mkdir()
    # Pre-seed cache with a file that doesn't exist on disk.
    state.set_filename_map(source_id="deck-1", game_id="super_metroid",
                           filename="some_old_name.srm", rom_stem=None)
    # Now resolve passing saves_root → should invalidate + re-scan.
    name = filename_map.resolve(
        state=state, source_id="deck-1", game_id="super_metroid",
        roms_root=roms, save_extension=".srm",
        saves_root=saves)
    state.close()
    return _check(name, "Super Metroid (USA).srm",
                  "stale cache invalidated → fresh scan")


def main() -> int:
    ok = True
    for name, fn in [
        ("scan_picks_usa_dump", test_scan_picks_usa_dump),
        ("scan_returns_none_when_no_match", test_scan_returns_none_when_no_match),
        ("resolve_caches_after_scan", test_resolve_caches_after_scan),
        ("resolve_invalidates_when_file_missing",
         test_resolve_invalidates_when_file_missing),
    ]:
        print(f"--- {name} ---")
        ok &= fn()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
