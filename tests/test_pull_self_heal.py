"""_pull_to_device's hash-mismatch self-heal.

Cross-source manifest writes can race: device A uploads X to cloud
(current.srm = X, manifest.current_hash = X), then device B's stale
refresh_manifest writes manifest with B's view (= some older hash Y).
Now manifest claims Y but current.srm has X. The next pull sees the
mismatch.

Old behavior: raise CloudError, daemon logs traceback every poll.
New behavior: log a warning, trust the actual bytes (X), write to
device, set sync_state to X. The next refresh_manifest pass writes
a manifest whose current_hash matches reality, repairing the desync.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync.cloud import (  # noqa: E402
    Manifest, RcloneCloud, build_manifest, compose_paths, sha256_bytes,
    utc_iso,
)
from retrosync.sources.base import SaveRef  # noqa: E402
from retrosync.state import StateStore  # noqa: E402
from retrosync.sync import (  # noqa: E402
    SyncConfig, SyncContext, SyncResult, _pull_to_device, refresh_manifest,
    sync_one_game,
)
from tests.mock_source import MockFXPakSource  # noqa: E402


def _setup() -> tuple[Path, StateStore, RcloneCloud]:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-pull-heal-"))
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


async def test_self_heal_on_hash_mismatch() -> bool:
    """Manifest claims hash A, current.srm has hash B. Pull writes B
    to the device (not raising) and reports B as the new current."""
    _, state, cloud = _setup()
    # Seed initial state via a real upload so paths/files exist.
    files = {"/Super Metroid.srm": b"GOOD-SAVE" + b"\x00" * 200}
    src = MockFXPakSource(id="fxpak-pro-1", files=files)
    state.upsert_source(id=src.id, system=src.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out = await sync_one_game(
        source=src, ref=SaveRef(path="/Super Metroid.srm"), ctx=ctx)
    refresh_manifest(source=src, save_path="/Super Metroid.srm",
                     game_id=out.game_id, paths=out.paths, ctx=ctx)

    # Now manually create the desync: rewrite current.srm with new
    # bytes WITHOUT updating the manifest. Mimics the cross-source
    # race where another device wrote current.srm and a stale refresh
    # left the manifest pointing at an old hash.
    paths = out.paths
    new_bytes = b"NEWER-SAVE-FROM-OTHER-DEVICE" + b"\x01" * 200
    new_hash = sha256_bytes(new_bytes)
    cloud.overwrite_current(paths=paths, save_data=new_bytes)
    # Manifest still has the old current_hash from the earlier
    # refresh_manifest call. Confirm precondition.
    manifest = cloud.read_manifest(paths)
    expected_old = sha256_bytes(b"GOOD-SAVE" + b"\x00" * 200)
    if not _check(manifest.current_hash, expected_old,
                  "precondition: manifest stale"):
        state.close()
        return False

    # Now call _pull_to_device with the manifest's stale expected_hash.
    # OLD behavior: raises CloudError. NEW behavior: writes new_bytes
    # to device, returns new_hash.
    sink_files: dict[str, bytes] = {"/Super Metroid.srm": files["/Super Metroid.srm"]}
    sink = MockFXPakSource(id="fxpak-pro-1", files=sink_files)
    state.upsert_source(id=sink.id, system=sink.system,
                        adapter="MockFXPakSource", config_json="{}")
    actual = await _pull_to_device(
        source=sink, ref=SaveRef(path="/Super Metroid.srm"),
        paths=paths, expected_hash=expected_old, ctx=ctx)

    state.close()
    ok = _check(actual, new_hash, "_pull_to_device returned actual hash")
    ok &= _check(sink_files["/Super Metroid.srm"], new_bytes,
                 "device file overwritten with new bytes")
    return ok


async def test_no_warning_when_hashes_match() -> bool:
    """Sanity: when manifest agrees with current.srm, _pull_to_device
    returns the same hash as expected_hash and no warning fires."""
    _, state, cloud = _setup()
    files = {"/Super Metroid.srm": b"GOOD-SAVE" + b"\x00" * 200}
    src = MockFXPakSource(id="fxpak-pro-1", files=files)
    state.upsert_source(id=src.id, system=src.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out = await sync_one_game(
        source=src, ref=SaveRef(path="/Super Metroid.srm"), ctx=ctx)

    paths = out.paths
    expected = sha256_bytes(b"GOOD-SAVE" + b"\x00" * 200)
    sink_files: dict[str, bytes] = {"/Super Metroid.srm": b"OLD"}
    sink = MockFXPakSource(id="fxpak-pro-1", files=sink_files)
    state.upsert_source(id=sink.id, system=sink.system,
                        adapter="MockFXPakSource", config_json="{}")
    actual = await _pull_to_device(
        source=sink, ref=SaveRef(path="/Super Metroid.srm"),
        paths=paths, expected_hash=expected, ctx=ctx)
    state.close()
    return _check(actual, expected, "happy path: actual == expected")


async def test_self_heal_then_refresh_repairs_manifest() -> bool:
    """End-to-end: when manifest claims hash A, cloud current.srm has
    hash B (the desync state), and the device thinks it's at hash C,
    engine pulls B to device and the next refresh writes manifest
    with current_hash = B (matching reality)."""
    _, state, cloud = _setup()
    files = {"/Super Metroid.srm": b"DEVICE-LOCAL" + b"\x00" * 200}
    src = MockFXPakSource(id="fxpak-pro-1", files=files)
    state.upsert_source(id=src.id, system=src.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out = await sync_one_game(
        source=src, ref=SaveRef(path="/Super Metroid.srm"), ctx=ctx)
    refresh_manifest(source=src, save_path="/Super Metroid.srm",
                     game_id=out.game_id, paths=out.paths, ctx=ctx)

    # Manufacture the desync:
    #   manifest.current_hash = STALE (some hash unrelated to anything)
    #   cloud current.srm     = DECK_BYTES (the truth, what should land)
    #   device file           = DEVICE-LOCAL (cart's prior bytes)
    # Then make sync_state.last_synced = device's hash so case 6 fires.
    paths = out.paths
    deck_bytes = b"DECK-UPLOAD" + b"\x02" * 200
    deck_hash = sha256_bytes(deck_bytes)
    h_local = sha256_bytes(files["/Super Metroid.srm"])
    cloud.overwrite_current(paths=paths, save_data=deck_bytes)
    # Manually bash the manifest's current_hash to a stale value to
    # mimic the cross-source race — Pi's stale refresh after Deck's
    # write.
    stale_hash = "ff" * 32  # impossible-to-collide stale hash
    manifest = cloud.read_manifest(paths)
    manifest.current_hash = stale_hash
    cloud.write_manifest(paths=paths, manifest=manifest,
                         preserve_lease=False)
    state.set_sync_state(source_id="fxpak-pro-1", game_id=out.game_id,
                         last_synced_hash=h_local)

    # Fresh ctx so the manifest cache is empty and the engine reads
    # the (stale) manifest from cloud.
    ctx2 = SyncContext(state=state, cloud=cloud,
                       cfg=SyncConfig(cloud_to_device=True))
    out2 = await sync_one_game(
        source=src, ref=SaveRef(path="/Super Metroid.srm"), ctx=ctx2)
    refresh_manifest(source=src, save_path="/Super Metroid.srm",
                     game_id=out2.game_id, paths=out2.paths, ctx=ctx2)

    repaired = cloud.read_manifest(paths)
    device_now = sha256_bytes(files["/Super Metroid.srm"])
    state.close()
    ok = _check(out2.result, SyncResult.DOWNLOADED,
                "engine reports DOWNLOADED (case 6)")
    ok &= _check(device_now, deck_hash,
                 "device file now has Deck's bytes")
    ok &= _check(repaired.current_hash, deck_hash,
                 "manifest current_hash repaired to actual bytes")
    return ok


def main() -> int:
    ok = True
    for name, fn in [
        ("self_heal_on_hash_mismatch", test_self_heal_on_hash_mismatch),
        ("no_warning_when_hashes_match", test_no_warning_when_hashes_match),
        ("self_heal_then_refresh_repairs_manifest",
         test_self_heal_then_refresh_repairs_manifest),
    ]:
        print(f"--- {name} ---")
        ok &= asyncio.run(fn())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
