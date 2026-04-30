"""Manifest drift detection on the IN_SYNC path.

Cross-device manifest write races and operator-side `rclone moveto`
migrations can leave the cloud in this state:

  manifest.current_hash = X        (stale)
  cloud current.<ext>   = bytes B  (real, hash != X)

If a device's local file ALSO hashes to X (typical after a partial
sync), the engine takes the IN_SYNC fast-path and skips this game
forever. The bytes on cloud never make it to the device.

The fix: when h_dev == h_cloud, do a cheap lsjson on current.<ext>
and compare its size + ModTime against the manifest. If either
disagrees, force a re-pull via `_pull_to_device` (which itself
self-heals on hash). End-to-end the device ends up with the real
bytes and the manifest gets repaired on the next refresh.

The motivating real-world case: an N64 save migrated from one
canonical-slug to another with `rclone copyto` — same size but
different bytes. The size check alone wouldn't catch it; ModTime
does.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync.cloud import (  # noqa: E402
    RcloneCloud, sha256_bytes,
)
from retrosync.sources.base import SaveRef  # noqa: E402
from retrosync.state import StateStore  # noqa: E402
from retrosync.sync import (  # noqa: E402
    SyncConfig, SyncContext, SyncResult, refresh_manifest, sync_one_game,
)
from tests.mock_source import MockFXPakSource  # noqa: E402


def _setup() -> tuple[Path, StateStore, RcloneCloud, Path]:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-drift-"))
    cloud_root = workdir / "cloud"
    cloud_root.mkdir()
    fake_rclone = ROOT / "tests" / "fake_rclone.sh"
    os.chmod(fake_rclone, 0o755)
    os.environ["FAKE_RCLONE_ROOT"] = str(cloud_root)
    state = StateStore(str(workdir / "state.db"))
    cloud = RcloneCloud(remote="gdrive:retro-saves",
                        binary=str(fake_rclone))
    return workdir, state, cloud, cloud_root


def _check(actual, expected, label) -> bool:
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


def _set_mtime(path: Path, dt_iso: str) -> None:
    """`touch -d` the given path to a specific ISO timestamp so
    fake_rclone's lsjson reports it. Bash `touch -d` accepts ISO
    timestamps natively on Linux/macOS."""
    subprocess.check_call(["touch", "-d", dt_iso, str(path)])


async def test_drift_detected_via_modtime() -> bool:
    """Same size, different bytes — only ModTime catches it.

    1. Sync a save normally (current.srm and manifest both written).
    2. Overwrite current.srm with same-size, different-content bytes,
       AFTER the manifest's updated_at (simulating an operator's
       `rclone moveto`-based migration).
    3. Run sync_one_game on a device whose local hash matches the
       OLD current_hash (would normally be IN_SYNC).
    4. Engine should detect the drift via ModTime comparison and
       pull the new bytes to the device.
    """
    workdir, state, cloud, cloud_root = _setup()
    files = {"/Foo.srm": b"AAA" * 100}
    src = MockFXPakSource(id="fxpak-pro-1", files=files)
    state.upsert_source(id=src.id, system=src.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out = await sync_one_game(
        source=src, ref=SaveRef(path="/Foo.srm"), ctx=ctx)
    refresh_manifest(source=src, save_path="/Foo.srm",
                     game_id=out.game_id, paths=out.paths, ctx=ctx)

    # The manifest now points at hash(AAA*100). Compute the cloud-
    # relative path for current.srm and overwrite with same-size bytes.
    current_rel = out.paths.current.split(":", 1)[1]
    current_path = cloud_root / current_rel
    new_bytes = b"BBB" * 100
    if not _check(len(new_bytes), len(b"AAA" * 100),
                  "precondition: same size as original"):
        state.close(); return False
    new_hash = sha256_bytes(new_bytes)
    current_path.write_bytes(new_bytes)
    # Make current.srm look 5 minutes newer than the manifest.
    # That's well past the 60s tolerance and unambiguously a drift.
    _set_mtime(current_path, "2030-01-01T00:05:00Z")
    manifest_rel = out.paths.manifest.split(":", 1)[1]
    manifest_path = cloud_root / manifest_rel
    _set_mtime(manifest_path, "2030-01-01T00:00:00Z")

    # Fresh ctx — manifest cache is empty, engine reads from cloud.
    ctx2 = SyncContext(state=state, cloud=cloud,
                       cfg=SyncConfig(cloud_to_device=True))
    # Device's local file still has the original bytes (hash AAA*100).
    # Engine sees h_dev == h_cloud (both = old hash), would normally
    # return IN_SYNC. Drift check should fire and trigger a re-pull.
    out2 = await sync_one_game(
        source=src, ref=SaveRef(path="/Foo.srm"), ctx=ctx2)

    state.close()
    ok = _check(out2.result, SyncResult.DOWNLOADED,
                "engine forced re-pull on drift")
    ok &= _check(files["/Foo.srm"], new_bytes,
                 "device file rewritten with cloud's actual bytes")
    ok &= _check(sha256_bytes(files["/Foo.srm"]), new_hash,
                 "device hash now matches cloud's actual current.srm")
    return ok


async def test_drift_detected_via_size() -> bool:
    """Different size catches drift even if ModTime is suspect.

    Size mismatch is unambiguous — set up a manifest written far in
    the future (so ModTime is technically older than the manifest),
    but size of current.srm doesn't match what the manifest says it
    should. Engine should detect via the size check.
    """
    workdir, state, cloud, cloud_root = _setup()
    files = {"/Bar.srm": b"AAA" * 100}
    src = MockFXPakSource(id="fxpak-pro-1", files=files)
    state.upsert_source(id=src.id, system=src.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out = await sync_one_game(
        source=src, ref=SaveRef(path="/Bar.srm"), ctx=ctx)
    refresh_manifest(source=src, save_path="/Bar.srm",
                     game_id=out.game_id, paths=out.paths, ctx=ctx)

    # Replace current.srm with DIFFERENT-size bytes. Set ModTime
    # OLDER than the manifest so the ModTime check by itself wouldn't
    # fire — only the size check should catch this.
    current_rel = out.paths.current.split(":", 1)[1]
    current_path = cloud_root / current_rel
    new_bytes = b"X" * 50  # different size
    new_hash = sha256_bytes(new_bytes)
    current_path.write_bytes(new_bytes)
    _set_mtime(current_path, "2030-01-01T00:00:00Z")
    manifest_rel = out.paths.manifest.split(":", 1)[1]
    manifest_path = cloud_root / manifest_rel
    _set_mtime(manifest_path, "2030-01-01T00:30:00Z")

    ctx2 = SyncContext(state=state, cloud=cloud,
                       cfg=SyncConfig(cloud_to_device=True))
    out2 = await sync_one_game(
        source=src, ref=SaveRef(path="/Bar.srm"), ctx=ctx2)

    state.close()
    ok = _check(out2.result, SyncResult.DOWNLOADED,
                "size-mismatch caught the drift")
    ok &= _check(files["/Bar.srm"], new_bytes,
                 "device file rewritten")
    ok &= _check(sha256_bytes(files["/Bar.srm"]), new_hash,
                 "device hash matches cloud actual")
    return ok


async def test_no_drift_no_pull() -> bool:
    """Sanity: when manifest agrees with current.srm (no drift),
    IN_SYNC fires and we DON'T re-pull pointlessly."""
    workdir, state, cloud, cloud_root = _setup()
    files = {"/Baz.srm": b"OK" * 100}
    src = MockFXPakSource(id="fxpak-pro-1", files=files)
    state.upsert_source(id=src.id, system=src.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out = await sync_one_game(
        source=src, ref=SaveRef(path="/Baz.srm"), ctx=ctx)
    refresh_manifest(source=src, save_path="/Baz.srm",
                     game_id=out.game_id, paths=out.paths, ctx=ctx)

    ctx2 = SyncContext(state=state, cloud=cloud,
                       cfg=SyncConfig(cloud_to_device=True))
    out2 = await sync_one_game(
        source=src, ref=SaveRef(path="/Baz.srm"), ctx=ctx2)
    state.close()
    return _check(out2.result, SyncResult.IN_SYNC,
                  "no drift → IN_SYNC, no spurious download")


async def test_drift_skipped_when_cloud_to_device_disabled() -> bool:
    """If `cloud_to_device` is off, we still detect the drift but
    return SKIPPED (not DOWNLOADED) — never overwrite the device
    silently when the operator hasn't opted in."""
    workdir, state, cloud, cloud_root = _setup()
    files = {"/Q.srm": b"AAA" * 100}
    src = MockFXPakSource(id="fxpak-pro-1", files=files)
    state.upsert_source(id=src.id, system=src.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out = await sync_one_game(
        source=src, ref=SaveRef(path="/Q.srm"), ctx=ctx)
    refresh_manifest(source=src, save_path="/Q.srm",
                     game_id=out.game_id, paths=out.paths, ctx=ctx)

    current_rel = out.paths.current.split(":", 1)[1]
    current_path = cloud_root / current_rel
    current_path.write_bytes(b"BBB" * 100)
    _set_mtime(current_path, "2030-01-01T00:05:00Z")
    manifest_rel = out.paths.manifest.split(":", 1)[1]
    manifest_path = cloud_root / manifest_rel
    _set_mtime(manifest_path, "2030-01-01T00:00:00Z")

    ctx2 = SyncContext(state=state, cloud=cloud,
                       cfg=SyncConfig(cloud_to_device=False))
    out2 = await sync_one_game(
        source=src, ref=SaveRef(path="/Q.srm"), ctx=ctx2)
    state.close()
    ok = _check(out2.result, SyncResult.SKIPPED,
                "cloud_to_device=False → drift detected but SKIPPED")
    ok &= _check(files["/Q.srm"], b"AAA" * 100,
                 "device bytes unchanged when cloud_to_device disabled")
    return ok


def main() -> int:
    ok = True
    for name, fn in [
        ("drift_detected_via_modtime", test_drift_detected_via_modtime),
        ("drift_detected_via_size", test_drift_detected_via_size),
        ("no_drift_no_pull", test_no_drift_no_pull),
        ("drift_skipped_when_cloud_to_device_disabled",
         test_drift_skipped_when_cloud_to_device_disabled),
    ]:
        print(f"--- {name} ---")
        ok &= asyncio.run(fn())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
