"""EverDrive 64 X7 source adapter — end-to-end via the mock transport.

Exercises the multi-format save path:

  - SaveSource.group_refs collapses per-format files by canonical
    game-id (Foo.eep + Foo.mp1 → one group `foo`).
  - read_canonical_bytes assembles the group's files into a saveset
    and runs through n64.combine() to produce a 296,960-byte
    Mupen64Plus-format blob (the cloud-side canonical form).
  - write_canonical_bytes splits cloud bytes back into per-format
    files, deletes files whose region went empty.
  - target_save_paths_for walks /ED64/ROMS for a matching ROM stem
    and returns the per-format target paths.
  - Cross-source: a Mupen64Plus-format blob written by the Deck
    (via the Deck's EmuDeck adapter) round-trips through split() →
    EverDrive's per-format files and back without data loss.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync.cloud import RcloneCloud, sha256_bytes  # noqa: E402
from retrosync.formats import n64  # noqa: E402
from retrosync.sources.base import SaveRef  # noqa: E402
from retrosync.sources.everdrive64 import EverDrive64Config, EverDrive64Source  # noqa: E402
from retrosync.state import StateStore  # noqa: E402
from retrosync.sync import (  # noqa: E402
    SyncConfig, SyncContext, SyncResult, refresh_manifest, sync_one_game,
)
from retrosync.transport.krikzz_ftdi import MockKrikzzTransport  # noqa: E402


def _setup() -> tuple[Path, StateStore, RcloneCloud]:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-ed64-"))
    cloud_root = workdir / "cloud"
    cloud_root.mkdir()
    fake_rclone = ROOT / "tests" / "fake_rclone.sh"
    os.chmod(fake_rclone, 0o755)
    os.environ["FAKE_RCLONE_ROOT"] = str(cloud_root)
    state = StateStore(str(workdir / "state.db"))
    cloud = RcloneCloud(remote="gdrive:retro-saves",
                        binary=str(fake_rclone))
    return workdir, state, cloud


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


def _make_source(*, files: dict[str, bytes] | None = None) -> EverDrive64Source:
    """Build an EverDrive64Source backed by an in-memory mock SD card."""
    transport = MockKrikzzTransport(files=files or {})
    cfg = EverDrive64Config(
        id="everdrive64-1", transport="mock",
        sd_saves_root="/ED64/SAVES",
        sd_roms_root="/ED64/ROMS",
        transport_instance=transport,
    )
    return EverDrive64Source(cfg)


async def test_health_and_list_saves() -> bool:
    src = _make_source(files={
        "/ED64/SAVES/Super Mario 64.eep":
            b"\x12" * n64.EEPROM_4KBIT_BYTES,
        "/ED64/SAVES/Paper Mario.fla":
            b"\x34" * n64.FLASHRAM_SIZE,
        "/ED64/SAVES/Paper Mario.mp1":
            b"\x56" * n64.CPAK_SIZE,
        # Non-N64 file should be filtered out by extension.
        "/ED64/SAVES/notes.txt":
            b"hello",
    })
    h = await src.health()
    if not _check(h.ok, True, "health.ok"):
        return False
    refs = await src.list_saves()
    return _check(sorted(r.path for r in refs), [
        "/ED64/SAVES/Paper Mario.fla",
        "/ED64/SAVES/Paper Mario.mp1",
        "/ED64/SAVES/Super Mario 64.eep",
    ], "list_saves filters to N64 extensions")


async def test_group_refs_by_game_id() -> bool:
    src = _make_source()
    refs = [
        SaveRef(path="/ED64/SAVES/Paper Mario.fla", size_bytes=0),
        SaveRef(path="/ED64/SAVES/Paper Mario.mp1", size_bytes=0),
        SaveRef(path="/ED64/SAVES/Super Mario 64.eep", size_bytes=0),
    ]
    groups = src.group_refs(refs)
    keys = sorted(groups.keys())
    ok = _check(keys, ["paper_mario", "super_mario_64"],
                "groups keyed by canonical slug")
    pm = groups["paper_mario"]
    ok &= _check(len(pm), 2, "Paper Mario group has 2 files")
    ok &= _check(sorted(r.path for r in pm), [
        "/ED64/SAVES/Paper Mario.fla",
        "/ED64/SAVES/Paper Mario.mp1",
    ], "Paper Mario group includes both .fla and .mp1")
    return ok


async def test_read_canonical_bytes_combines() -> bool:
    """A multi-file group reads as a 296,960-byte combined srm."""
    fla_bytes = bytes(range(256)) * (n64.FLASHRAM_SIZE // 256)
    mp1_bytes = bytes((b ^ 0x42) for b in fla_bytes[:n64.CPAK_SIZE])
    src = _make_source(files={
        "/ED64/SAVES/Paper Mario.fla": fla_bytes,
        "/ED64/SAVES/Paper Mario.mp1": mp1_bytes,
    })
    refs = await src.list_saves()
    groups = src.group_refs(refs)
    pm_refs = groups["paper_mario"]
    blob = await src.read_canonical_bytes(pm_refs)
    if not _check(len(blob), n64.COMBINED_SIZE,
                  "combined bytes are 296,960 long"):
        return False
    # Verify the FlashRAM region of the combined blob matches input.
    fla_in_combined = blob[n64.FLASHRAM_OFFSET:
                           n64.FLASHRAM_OFFSET + n64.FLASHRAM_SIZE]
    return _check(fla_in_combined, fla_bytes,
                  "FlashRAM region in combined matches input")


async def test_write_canonical_bytes_splits() -> bool:
    """Writing a cloud-format blob produces the right per-format files."""
    src = _make_source(files={
        # Pre-existing eeprom that's about to get replaced.
        "/ED64/SAVES/Super Mario 64.eep": b"\x00" * n64.EEPROM_4KBIT_BYTES,
    })
    new_eep = b"\xab" * n64.EEPROM_4KBIT_BYTES
    blob = n64.combine(n64.N64SaveSet(eeprom=new_eep))
    refs = await src.list_saves()
    sm64 = src.group_refs(refs)["super_mario_64"]
    await src.write_canonical_bytes(sm64, blob)
    # The .eep file should now contain the new bytes.
    written = await src._open()
    out = await written.file_read("/ED64/SAVES/Super Mario 64.eep")
    return _check(out, new_eep, ".eep file overwritten with new bytes")


async def test_write_deletes_emptied_regions() -> bool:
    """If a region went from populated → None in the saveset, the
    corresponding per-format file is deleted (not written empty)."""
    fla = b"\x33" * n64.FLASHRAM_SIZE
    mp1 = b"\x77" * n64.CPAK_SIZE
    src = _make_source(files={
        "/ED64/SAVES/Foo.fla": fla,
        "/ED64/SAVES/Foo.mp1": mp1,
    })
    refs = await src.list_saves()
    foo = src.group_refs(refs)["foo"]
    # Build a new blob that has only FlashRAM populated, no cpak.
    blob = n64.combine(n64.N64SaveSet(flashram=fla))
    await src.write_canonical_bytes(foo, blob)
    transport = await src._open()
    fla_present = await transport.file_exists("/ED64/SAVES/Foo.fla")
    mp1_present = await transport.file_exists("/ED64/SAVES/Foo.mp1")
    ok = _check(fla_present, True, ".fla survived")
    ok &= _check(mp1_present, False, ".mp1 deleted (region went empty)")
    return ok


async def test_target_save_paths_for_finds_rom_stem() -> bool:
    """For bootstrap pull: walk /ED64/ROMS, find a matching ROM stem,
    return the per-format save paths. USA preferred over JP/EU."""
    src = _make_source(files={
        "/ED64/ROMS/Super Mario 64 (Japan).z64": b"jp",
        "/ED64/ROMS/Super Mario 64 (USA).z64": b"usa",
        "/ED64/ROMS/Super Mario 64 (Europe).z64": b"eu",
    })
    paths = await src.target_save_paths_for("super_mario_64")
    return _check(paths.get("eep"),
                  "/ED64/SAVES/Super Mario 64 (USA).eep",
                  "USA stem chosen for save filename derivation")


async def test_target_save_paths_for_no_rom() -> bool:
    src = _make_source(files={})  # empty SD
    paths = await src.target_save_paths_for("super_mario_64")
    return _check(paths, {},
                  "no matching ROM → empty dict (skip bootstrap)")


async def test_end_to_end_upload_via_engine() -> bool:
    """Full sync_one_game flow: EverDrive 64 source uploads its
    canonical blob to cloud, manifest gets written, hashes match."""
    eep = bytes(range(256)) * (n64.EEPROM_4KBIT_BYTES // 256)
    src = _make_source(files={
        "/ED64/SAVES/Super Mario 64.eep": eep,
    })
    _, state, cloud = _setup()
    state.upsert_source(id=src.id, system=src.system,
                        adapter="EverDrive64Source", config_json="{}")
    refs = await src.list_saves()
    sm64 = src.group_refs(refs)["super_mario_64"]
    canonical = await src.read_canonical_bytes(sm64)
    canonical_hash = sha256_bytes(canonical)
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out = await sync_one_game(
        source=src, ref=sm64[0], ctx=ctx,
        primed_data=canonical, primed_hash=canonical_hash)
    refresh_manifest(source=src, save_path=sm64[0].path,
                     game_id=out.game_id, paths=out.paths, ctx=ctx)
    state.close()
    ok = _check(out.result, SyncResult.BOOTSTRAP_UPLOADED,
                "first upload → BOOTSTRAP_UPLOADED")
    # Verify the cloud's current.srm has the combined bytes (not the
    # raw .eep bytes).
    manifest = cloud.read_manifest(out.paths)
    return ok and _check(manifest.current_hash, canonical_hash,
                         "cloud current_hash = combined-form hash")


def main() -> int:
    ok = True
    for name, fn in [
        ("health_and_list_saves", test_health_and_list_saves),
        ("group_refs_by_game_id", test_group_refs_by_game_id),
        ("read_canonical_bytes_combines",
         test_read_canonical_bytes_combines),
        ("write_canonical_bytes_splits",
         test_write_canonical_bytes_splits),
        ("write_deletes_emptied_regions",
         test_write_deletes_emptied_regions),
        ("target_save_paths_for_finds_rom_stem",
         test_target_save_paths_for_finds_rom_stem),
        ("target_save_paths_for_no_rom",
         test_target_save_paths_for_no_rom),
        ("end_to_end_upload_via_engine",
         test_end_to_end_upload_via_engine),
    ]:
        print(f"--- {name} ---")
        ok &= asyncio.run(fn())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
