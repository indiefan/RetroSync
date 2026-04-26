"""promote: force a historical version to be cloud's current."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync import promote as promote_mod  # noqa: E402
from retrosync.cloud import (  # noqa: E402
    CloudError, RcloneCloud, compose_paths, hash8, sha256_bytes,
)
from retrosync.sources.base import SaveRef  # noqa: E402
from retrosync.state import StateStore  # noqa: E402
from retrosync.sync import (  # noqa: E402
    SyncConfig, SyncContext, refresh_manifest, sync_one_game,
)
from tests.mock_source import MockFXPakSource  # noqa: E402


def _setup() -> tuple[Path, StateStore, RcloneCloud]:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-promote-"))
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


async def _seed_two_versions(state, cloud) -> tuple[str, str, str]:
    """Push two versions of the same game to cloud. Returns
    (older_hash, newer_hash, game_id)."""
    files = {"/Super Metroid.srm": b"OLD-SAVE" + b"\x00" * 200}
    src = MockFXPakSource(id="fxpak-pro-1", files=files)
    state.upsert_source(id=src.id, system=src.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out1 = await sync_one_game(
        source=src, ref=SaveRef(path="/Super Metroid.srm"), ctx=ctx)
    refresh_manifest(source=src, save_path="/Super Metroid.srm",
                     game_id=out1.game_id, paths=out1.paths, ctx=ctx)
    older = sha256_bytes(files["/Super Metroid.srm"])

    src.files["/Super Metroid.srm"] = b"NEW-SAVE" + b"\x01" * 200
    out2 = await sync_one_game(
        source=src, ref=SaveRef(path="/Super Metroid.srm"), ctx=ctx)
    refresh_manifest(source=src, save_path="/Super Metroid.srm",
                     game_id=out2.game_id, paths=out2.paths, ctx=ctx)
    newer = sha256_bytes(src.files["/Super Metroid.srm"])
    return older, newer, out1.game_id


async def test_promote_by_hash_prefix() -> bool:
    """Promote by hash8 → cloud current.<ext> matches that version,
    manifest's current_hash updates."""
    _, state, cloud = _setup()
    older, newer, game_id = await _seed_two_versions(state, cloud)
    paths = compose_paths(remote=cloud.remote, system="snes",
                          game_id=game_id, save_filename=f"{game_id}.bin")
    # Confirm precondition: current is the newer hash.
    cur_before = sha256_bytes(cloud.download_bytes(src=paths.current))
    if not _check(cur_before, newer, "precondition: current = newer"):
        state.close()
        return False
    # Promote the older one.
    result = promote_mod.promote(state=state, cloud=cloud,
                                 game_id=game_id, selector=hash8(older),
                                 system="snes")
    cur_after = sha256_bytes(cloud.download_bytes(src=paths.current))
    manifest = cloud.read_manifest(paths)
    state.close()
    ok = _check(result.promoted_hash, older, "result.promoted_hash")
    ok &= _check(cur_after, older, "current.<ext> bytes are now older")
    ok &= _check(manifest.current_hash, older,
                 "manifest.current_hash bumped")
    return ok


async def test_promote_leaves_sync_states_alone() -> bool:
    """Sync_state is intentionally NOT bumped — leaving it at the
    prior current means an in-sync device sees case 6 (cloud
    advanced) on next sync and downloads. Bumping would mis-trigger
    case 5 (fast-forward upload) and undo the promote."""
    _, state, cloud = _setup()
    older, newer, game_id = await _seed_two_versions(state, cloud)
    state.upsert_source(id="pocket-1", system="snes",
                        adapter="PocketSource", config_json="{}")
    state.set_sync_state(source_id="pocket-1", game_id=game_id,
                         last_synced_hash=newer)
    promote_mod.promote(state=state, cloud=cloud,
                        game_id=game_id, selector=hash8(older),
                        system="snes")
    fx = state.get_sync_state("fxpak-pro-1", game_id)
    pk = state.get_sync_state("pocket-1", game_id)
    state.close()
    ok = _check(fx.last_synced_hash, newer,
                "fxpak last_synced unchanged (still newer)")
    ok &= _check(pk.last_synced_hash, newer,
                 "pocket last_synced unchanged (still newer)")
    return ok


async def test_promote_by_cloud_path() -> bool:
    """Selector that's a cloud path is honored verbatim."""
    _, state, cloud = _setup()
    older, newer, game_id = await _seed_two_versions(state, cloud)
    paths = compose_paths(remote=cloud.remote, system="snes",
                          game_id=game_id, save_filename=f"{game_id}.bin")
    # Find the older version's cloud path from state.db.
    row = state._conn.execute(
        "SELECT cloud_path FROM versions WHERE hash=?", (older,)).fetchone()
    older_path = row["cloud_path"]
    result = promote_mod.promote(state=state, cloud=cloud,
                                 game_id=game_id, selector=older_path,
                                 system="snes")
    cur = sha256_bytes(cloud.download_bytes(src=paths.current))
    state.close()
    return (_check(result.promoted_path, older_path,
                   "promoted_path == passed selector")
            and _check(cur, older, "current matches"))


async def test_promote_no_match_raises() -> bool:
    """Selector that doesn't match anything raises ValueError."""
    _, state, cloud = _setup()
    older, newer, game_id = await _seed_two_versions(state, cloud)
    try:
        promote_mod.promote(state=state, cloud=cloud,
                            game_id=game_id, selector="deadbeef",
                            system="snes")
    except ValueError as exc:
        state.close()
        return _check("no version matching" in str(exc), True,
                      "ValueError raised with sensible message")
    state.close()
    print("FAIL: expected ValueError")
    return False


async def test_promote_then_device_pulls_via_engine() -> bool:
    """End-to-end: promote → next sync_one_game on the device gets
    the promoted bytes (not the current local stale ones)."""
    _, state, cloud = _setup()
    older, newer, game_id = await _seed_two_versions(state, cloud)

    # Simulate the device that has the newer locally (its last_synced
    # was set to newer in _seed_two_versions). Promote older → next
    # sync should download older to the device.
    src = MockFXPakSource(id="fxpak-pro-1", files={
        "/Super Metroid.srm": b"NEW-SAVE" + b"\x01" * 200})
    promote_mod.promote(state=state, cloud=cloud, game_id=game_id,
                        selector=hash8(older), system="snes")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out = await sync_one_game(
        source=src, ref=SaveRef(path="/Super Metroid.srm"), ctx=ctx)
    state.close()
    return (_check(sha256_bytes(src.files["/Super Metroid.srm"]),
                   older, "device file overwritten with older")
            and _check(out.result.value in ("downloaded",
                                            "bootstrap_downloaded"),
                       True, "engine reports download"))


def main() -> int:
    ok = True
    for name, fn in [
        ("promote_by_hash_prefix", test_promote_by_hash_prefix),
        ("promote_leaves_sync_states_alone",
         test_promote_leaves_sync_states_alone),
        ("promote_by_cloud_path", test_promote_by_cloud_path),
        ("promote_no_match_raises", test_promote_no_match_raises),
        ("promote_then_device_pulls_via_engine",
         test_promote_then_device_pulls_via_engine),
    ]:
        print(f"--- {name} ---")
        ok &= asyncio.run(fn())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
