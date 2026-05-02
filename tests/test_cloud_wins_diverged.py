"""cloud_wins_on_diverged_device: case 7 with the policy on prefers
cloud's current and preserves the device's bytes as a versions/ entry.

Without this opt-in, case 7 falls through to conflict_winner=device,
which uploads the device's bytes and overrides cloud's current —
fine when the device's edits are real, terrible when the device's
"edits" are stale SRAM from a power-cycle (the FXPak case).
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
    RcloneCloud, compose_paths, sha256_bytes,
)
from retrosync.sources.base import SaveRef  # noqa: E402
from retrosync.state import StateStore  # noqa: E402
from retrosync.sync import (  # noqa: E402
    SyncConfig, SyncContext, SyncResult, refresh_manifest, sync_one_game,
)
from tests.mock_source import MockFXPakSource  # noqa: E402


def _setup() -> tuple[Path, StateStore, RcloneCloud]:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-divwin-"))
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


async def _seed_case_7(state, cloud):
    """Build a scenario where sync_one_game will hit case 7.

    1. Cart uploads OLD as cloud's current.
    2. Cloud's current advances to NEW (simulated via direct upload —
       another device wrote it).
    3. Cart's local bytes change to DIVERGED (cart-side edit since OLD).
    4. Cart's sync_state still points at OLD.

    Now: h_dev=DIVERGED, h_cloud=NEW, h_last=OLD. h_last != h_cloud
    AND h_last != h_dev → case 7 divergence.
    """
    files = {"/Foo.srm": b"OLD-SAVE" + b"\x00" * 200}
    src = MockFXPakSource(id="fxpak-pro-1", files=files)
    state.upsert_source(id=src.id, system=src.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out = await sync_one_game(
        source=src, ref=SaveRef(path="/Foo.srm"), ctx=ctx)
    await refresh_manifest(source=src, save_path="/Foo.srm",
                     game_id=out.game_id, paths=out.paths, ctx=ctx)
    old_hash = sha256_bytes(files["/Foo.srm"])

    # Another device pushes NEW (we just rewrite cloud's current
    # directly + bump manifest's current_hash to mimic the result).
    new_bytes = b"NEW-FROM-OTHER" + b"\x01" * 200
    new_hash = sha256_bytes(new_bytes)
    cloud.overwrite_current(paths=out.paths, save_data=new_bytes)
    manifest = cloud.read_manifest(out.paths)
    manifest.current_hash = new_hash
    cloud.write_manifest(paths=out.paths, manifest=manifest,
                         preserve_lease=False)

    # Cart-side edit: bytes diverge from OLD.
    files["/Foo.srm"] = b"DIVERGED-CART" + b"\x02" * 200
    diverged_hash = sha256_bytes(files["/Foo.srm"])
    return src, files, old_hash, new_hash, diverged_hash, out


async def test_case_7_played_recently_device_wins() -> bool:
    """When a game has been actively played since the last sync,
    case 7 → conflict_winner=device → upload."""
    _, state, cloud = _setup()
    src, files, old_h, new_h, div_h, out0 = await _seed_case_7(state, cloud)
    # Simulate the game being played AFTER the last sync
    state.record_gameplay_session(src.id, "foo", "9999-12-31T23:59:59Z")
    
    ctx = SyncContext(state=state, cloud=cloud, cfg=SyncConfig(
        cloud_to_device=True))
    out = await sync_one_game(
        source=src, ref=SaveRef(path="/Foo.srm"), ctx=ctx)
    state.close()
    return _check(out.result, SyncResult.CONFLICT_RESOLVED,
                  "played recently: conflict_winner=device auto-resolves")


async def test_case_7_unplayed_cloud_wins() -> bool:
    """When a game has NOT been played recently: case 7 → preserve device bytes + cloud wins."""
    _, state, cloud = _setup()
    src, files, old_h, new_h, div_h, out0 = await _seed_case_7(state, cloud)
    ctx = SyncContext(state=state, cloud=cloud, cfg=SyncConfig(
        cloud_to_device=True))
    out = await sync_one_game(
        source=src, ref=SaveRef(path="/Foo.srm"), ctx=ctx)
    state.close()
    ok = _check(out.result, SyncResult.DOWNLOADED,
                "unplayed: cloud wins, device gets cloud's bytes")
    ok &= _check(sha256_bytes(files["/Foo.srm"]), new_h,
                 "device file overwritten with cloud's NEW bytes")
    return ok


async def test_case_7_unplayed_cloud_wins_preserves_device_bytes() -> bool:
    """The diverged device bytes still land in versions/ (recoverable
    via `retrosync promote`) even though cloud wins."""
    _, state, cloud = _setup()
    src, files, old_h, new_h, div_h, out0 = await _seed_case_7(state, cloud)
    ctx = SyncContext(state=state, cloud=cloud, cfg=SyncConfig(
        cloud_to_device=True))
    await sync_one_game(
        source=src, ref=SaveRef(path="/Foo.srm"), ctx=ctx)
    # Was there a versions/* row inserted with the diverged hash?
    row = state._conn.execute(
        "SELECT * FROM versions WHERE hash=? AND state='uploaded'",
        (div_h,)).fetchone()
    state.close()
    return _check(row is not None, True,
                  "diverged bytes uploaded to versions/ for recovery")


def main() -> int:
    ok = True
    for name, fn in [
        ("case_7_played_recently_device_wins", test_case_7_played_recently_device_wins),
        ("case_7_unplayed_cloud_wins", test_case_7_unplayed_cloud_wins),
        ("case_7_unplayed_cloud_wins_preserves_device_bytes", test_case_7_unplayed_cloud_wins_preserves_device_bytes),
    ]:
        print(f"--- {name} ---")
        ok &= asyncio.run(fn())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
