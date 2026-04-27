"""`retrosync deck add-source` — config append + idempotency.

Covers the cases the operator hits on a real Deck:
  - First-time scaffold + N64 source append.
  - Re-running for an already-configured system is a no-op.
  - Different systems coexist (deck-1-snes + deck-1-n64).
  - YAML stays valid after the append.
  - Auto-derived source ids dodge collisions (deck-2-snes if
    deck-1-snes is taken).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync.deck import add_source as add_source_mod  # noqa: E402
from retrosync.deck import emudeck_paths  # noqa: E402
from retrosync.deck import systems as deck_systems  # noqa: E402


def _check(actual, expected, label) -> bool:
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


def _make_emudeck_layout(tmp: Path, systems: list[str]) -> Path:
    """Build a fake EmuDeck root with saves/ and roms/<system>/ dirs."""
    root = tmp / "Emulation"
    (root / "saves" / "retroarch" / "saves").mkdir(parents=True)
    for s in systems:
        (root / "roms" / s).mkdir(parents=True)
    return root


def _scaffold_config(path: Path) -> None:
    path.write_text("cloud:\n  rclone_remote: gdrive:retro-saves\n"
                    "sources: []\n")


def test_append_n64_source() -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="retrosync-add-src-"))
    root = _make_emudeck_layout(tmp, ["snes", "n64"])
    cfg = tmp / "config.yaml"
    _scaffold_config(cfg)

    result = add_source_mod.add_source(
        config_path=cfg, system="n64",
        emudeck_root_override=root)

    parsed = yaml.safe_load(cfg.read_text())
    sources = parsed["sources"]
    ok = _check(len(sources), 1, "one source after append")
    ok &= _check(sources[0]["id"], "deck-1-n64", "auto-derived id")
    ok &= _check(sources[0]["adapter"], "emudeck", "adapter is emudeck")
    opts = sources[0]["options"]
    ok &= _check(opts["system"], "n64", "system is n64")
    ok &= _check(opts["save_extension"], ".srm", "save_extension")
    ok &= _check(opts["rom_extensions"], [".z64", ".n64", ".v64"],
                 "rom_extensions")
    ok &= _check(str(opts["roms_root"]), str(root / "roms" / "n64"),
                 "roms_root resolved")
    ok &= _check(result.source_id, "deck-1-n64", "result.source_id")
    return ok


def test_idempotent_skip_existing_system() -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="retrosync-add-src-"))
    root = _make_emudeck_layout(tmp, ["snes", "n64"])
    cfg = tmp / "config.yaml"
    _scaffold_config(cfg)

    add_source_mod.add_source(
        config_path=cfg, system="n64", emudeck_root_override=root)
    try:
        add_source_mod.add_source(
            config_path=cfg, system="n64", emudeck_root_override=root)
    except add_source_mod.AddSourceError as exc:
        return _check("already configured for system 'n64'" in str(exc),
                      True, "second add_source for same system raises")
    print("FAIL: expected AddSourceError on duplicate system")
    return False


def test_two_systems_coexist() -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="retrosync-add-src-"))
    root = _make_emudeck_layout(tmp, ["snes", "n64"])
    cfg = tmp / "config.yaml"
    _scaffold_config(cfg)

    add_source_mod.add_source(
        config_path=cfg, system="snes", emudeck_root_override=root)
    add_source_mod.add_source(
        config_path=cfg, system="n64", emudeck_root_override=root)

    parsed = yaml.safe_load(cfg.read_text())
    ids = sorted(s["id"] for s in parsed["sources"])
    return _check(ids, ["deck-1-n64", "deck-1-snes"],
                  "both systems present with distinct ids")


def test_id_collision_picks_unused() -> bool:
    """If `deck-1-snes` is already taken by something else, the auto-
    derived id moves on to `deck-2-snes`."""
    tmp = Path(tempfile.mkdtemp(prefix="retrosync-add-src-"))
    root = _make_emudeck_layout(tmp, ["snes"])
    cfg = tmp / "config.yaml"
    cfg.write_text(
        "cloud: {rclone_remote: gdrive:retro-saves}\n"
        "sources:\n"
        "  - id: deck-1-snes\n"
        "    adapter: pocket\n"   # different adapter, same id
        "    options: {}\n")

    result = add_source_mod.add_source(
        config_path=cfg, system="snes", emudeck_root_override=root)
    return _check(result.source_id, "deck-2-snes",
                  "auto-derived id steps over taken slot")


def test_missing_emudeck_root_errors() -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="retrosync-add-src-"))
    cfg = tmp / "config.yaml"
    _scaffold_config(cfg)
    try:
        add_source_mod.add_source(
            config_path=cfg, system="n64",
            emudeck_root_override=tmp / "nope")
    except add_source_mod.AddSourceError as exc:
        return _check("EmuDeck install not detected" in str(exc),
                      True, "missing root → AddSourceError")
    print("FAIL: expected AddSourceError")
    return False


def test_missing_roms_dir_errors() -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="retrosync-add-src-"))
    root = _make_emudeck_layout(tmp, ["snes"])  # no n64 dir
    cfg = tmp / "config.yaml"
    _scaffold_config(cfg)
    try:
        add_source_mod.add_source(
            config_path=cfg, system="n64", emudeck_root_override=root)
    except add_source_mod.AddSourceError as exc:
        return _check("doesn't exist" in str(exc), True,
                      "missing roms_root → AddSourceError")
    print("FAIL: expected AddSourceError")
    return False


def test_unknown_system_errors() -> bool:
    try:
        deck_systems.get("dreamcast")
    except ValueError as exc:
        return _check("unknown system" in str(exc), True,
                      "unknown system → ValueError")
    print("FAIL: expected ValueError")
    return False


def test_render_format_is_yaml_compatible() -> bool:
    """The rendered block parses cleanly as YAML when concatenated to
    a list — covers the spacing / indent contract."""
    block = add_source_mod.render_source_block(
        source_id="deck-1-n64", system="n64",
        saves_root=Path("/home/deck/Emulation/saves/retroarch/saves"),
        roms_root=Path("/home/deck/Emulation/roms/n64"),
        rom_extensions=(".z64", ".n64", ".v64"),
        save_extension=".srm")
    body = "sources:\n" + block
    parsed = yaml.safe_load(body)
    src = parsed["sources"][0]
    ok = _check(src["id"], "deck-1-n64", "rendered id parses")
    ok &= _check(src["options"]["system"], "n64", "rendered system parses")
    return ok


def main() -> int:
    ok = True
    for name, fn in [
        ("append_n64_source", test_append_n64_source),
        ("idempotent_skip_existing_system",
         test_idempotent_skip_existing_system),
        ("two_systems_coexist", test_two_systems_coexist),
        ("id_collision_picks_unused", test_id_collision_picks_unused),
        ("missing_emudeck_root_errors", test_missing_emudeck_root_errors),
        ("missing_roms_dir_errors", test_missing_roms_dir_errors),
        ("unknown_system_errors", test_unknown_system_errors),
        ("render_format_is_yaml_compatible",
         test_render_format_is_yaml_compatible),
    ]:
        print(f"--- {name} ---")
        ok &= fn()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
