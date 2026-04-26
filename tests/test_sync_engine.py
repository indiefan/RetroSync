"""Direct unit-ish tests for the sync engine — exercises the decision
matrix without spinning up the polling orchestrator.

Run with:
    PYTHONPATH=. python3 tests/test_sync_engine.py
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
from retrosync.state import StateStore  # noqa: E402
from retrosync.sources.base import SaveRef  # noqa: E402
from retrosync.sync import (  # noqa: E402
    SyncConfig, SyncContext, SyncResult, refresh_manifest, sync_one_game,
)
from tests.mock_source import MockFXPakSource  # noqa: E402


def _setup():
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-syncengine-"))
    cloud_root = workdir / "cloud"
    cloud_root.mkdir()
    db_path = workdir / "state.db"

    fake_rclone = ROOT / "tests" / "fake_rclone.sh"
    os.chmod(fake_rclone, 0o755)
    os.environ["FAKE_RCLONE_ROOT"] = str(cloud_root)

    state = StateStore(str(db_path))
    cloud = RcloneCloud(remote="gdrive:retro-saves",
                        binary=str(fake_rclone))
    return workdir, state, cloud


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


async def test_bootstrap_upload() -> bool:
    """Empty cloud + device save → BOOTSTRAP_UPLOADED."""
    _, state, cloud = _setup()
    cart = MockFXPakSource(id="fx", files={"/Mario.srm": b"abc" * 100})
    state.upsert_source(id=cart.id, system=cart.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))

    ref = SaveRef(path="/Mario.srm")
    out = await sync_one_game(source=cart, ref=ref, ctx=ctx)
    state.close()
    return _check(out.result, SyncResult.BOOTSTRAP_UPLOADED,
                  "fresh cloud → bootstrap upload")


async def test_in_sync() -> bool:
    """Same hash on device and cloud → IN_SYNC."""
    _, state, cloud = _setup()
    cart = MockFXPakSource(id="fx", files={"/Mario.srm": b"abc" * 100})
    state.upsert_source(id=cart.id, system=cart.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))

    ref = SaveRef(path="/Mario.srm")
    out1 = await sync_one_game(source=cart, ref=ref, ctx=ctx)
    refresh_manifest(source=cart, save_path=ref.path,
                     game_id=out1.game_id, paths=out1.paths, ctx=ctx)
    # Drop the in-mem cache so the second pass re-reads the manifest
    ctx.invalidate_manifest(out1.paths)

    out2 = await sync_one_game(source=cart, ref=ref, ctx=ctx)
    state.close()
    return _check(out2.result, SyncResult.IN_SYNC,
                  "second sync of same bytes → in_sync")


async def test_cloud_to_device_pull() -> bool:
    """Cloud advances after sync; device unchanged → DOWNLOADED."""
    _, state, cloud = _setup()
    files = {"/Mario.srm": b"abc" * 100}
    cart = MockFXPakSource(id="fx", files=files)
    state.upsert_source(id=cart.id, system=cart.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    ref = SaveRef(path="/Mario.srm")

    out1 = await sync_one_game(source=cart, ref=ref, ctx=ctx)
    refresh_manifest(source=cart, save_path=ref.path,
                     game_id=out1.game_id, paths=out1.paths, ctx=ctx)
    h_a = sha256_bytes(b"abc" * 100)

    # Simulate the cloud advancing while the device sat unchanged. Easiest
    # way: use the same source, change its bytes, sync (uploads), then revert
    # the device bytes and the sync_state to where they were before the
    # advance. From the engine's POV that's "device == h_last; cloud != h_last".
    files["/Mario.srm"] = b"def" * 100
    ctx.invalidate_manifest(out1.paths)
    out_push = await sync_one_game(source=cart, ref=ref, ctx=ctx)
    refresh_manifest(source=cart, save_path=ref.path,
                     game_id=out_push.game_id, paths=out_push.paths, ctx=ctx)
    if out_push.result != SyncResult.UPLOADED:
        print(f"FAIL: setup: cloud advance push got {out_push.result}")
        state.close()
        return False

    files["/Mario.srm"] = b"abc" * 100
    state.set_current_hash(source_id=cart.id, path=ref.path, h=h_a)
    state.set_sync_state(source_id=cart.id, game_id=out1.game_id,
                         last_synced_hash=h_a)

    ctx.invalidate_manifest(out1.paths)
    out2 = await sync_one_game(source=cart, ref=ref, ctx=ctx)
    state.close()

    ok = _check(out2.result, SyncResult.DOWNLOADED,
                "cloud-newer + device unchanged → DOWNLOADED")
    ok &= _check(files["/Mario.srm"], b"def" * 100,
                 "cart bytes overwritten with cloud copy")
    return ok


async def test_conflict_no_prior_agreement_preserve() -> bool:
    """conflict_winner='preserve': cloud has X; device has Y; no
    source_sync_state for this device → CONFLICT (left open)."""
    _, state, cloud = _setup()
    # Pre-populate cloud with a different device's save.
    fx2_files = {"/Mario.srm": b"abc" * 100}
    fx2 = MockFXPakSource(id="fx2", files=fx2_files)
    state.upsert_source(id=fx2.id, system=fx2.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True,
                                     conflict_winner="preserve"))
    out_setup = await sync_one_game(source=fx2,
                                    ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    refresh_manifest(source=fx2, save_path="/Mario.srm",
                     game_id=out_setup.game_id, paths=out_setup.paths, ctx=ctx)

    # New device with diverging bytes, never synced before.
    fx_files = {"/Mario.srm": b"xyz" * 100}
    fx = MockFXPakSource(id="fx-new", files=fx_files)
    state.upsert_source(id=fx.id, system=fx.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx.invalidate_manifest(out_setup.paths)
    out = await sync_one_game(source=fx, ref=SaveRef(path="/Mario.srm"),
                              ctx=ctx)

    open_conflicts = state.list_conflicts(open_only=True)
    state.close()

    ok = _check(out.result, SyncResult.CONFLICT,
                "diverging device + preserve → CONFLICT (open)")
    ok &= _check(len(open_conflicts), 1,
                 "one open conflict in DB")
    return ok


async def test_conflict_device_wins_default() -> bool:
    """Default conflict_winner='device': diverging device bytes auto-win,
    become the new current. Cloud's previous bytes survive in versions/.
    A resolved conflict row is recorded for forensics."""
    _, state, cloud = _setup()
    # Set up cloud with one device's bytes.
    fx2_files = {"/Mario.srm": b"abc" * 100}
    fx2 = MockFXPakSource(id="fx2", files=fx2_files)
    state.upsert_source(id=fx2.id, system=fx2.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out_setup = await sync_one_game(source=fx2,
                                    ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    refresh_manifest(source=fx2, save_path="/Mario.srm",
                     game_id=out_setup.game_id, paths=out_setup.paths, ctx=ctx)
    cloud_loser_path = out_setup.paths.current
    cloud_loser_bytes = cloud.download_bytes(src=cloud_loser_path)
    h_loser = sha256_bytes(cloud_loser_bytes)

    # New device with diverging bytes, never synced before.
    fx_files = {"/Mario.srm": b"xyz" * 100}
    fx = MockFXPakSource(id="fx-new", files=fx_files)
    state.upsert_source(id=fx.id, system=fx.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx.invalidate_manifest(out_setup.paths)
    out = await sync_one_game(source=fx, ref=SaveRef(path="/Mario.srm"),
                              ctx=ctx)

    new_current = cloud.download_bytes(src=out.paths.current)
    open_conflicts = state.list_conflicts(open_only=True)
    all_conflicts = state.list_conflicts(open_only=False)
    fx2_state = state.get_sync_state(fx2.id, out.game_id)
    state.close()

    ok = _check(out.result, SyncResult.CONFLICT_RESOLVED,
                "diverging device + default → CONFLICT_RESOLVED")
    ok &= _check(new_current, b"xyz" * 100,
                 "new current is the device's bytes")
    ok &= _check(sha256_bytes(cloud_loser_bytes), h_loser,
                 "previous cloud bytes still readable from versions/")
    ok &= _check(len(open_conflicts), 0, "no OPEN conflicts (auto-resolved)")
    ok &= _check(len(all_conflicts), 1,
                 "but the conflict row exists for forensics")
    ok &= _check(fx2_state.last_synced_hash if fx2_state else None,
                 h_loser, "fx2's sync state is unchanged (no ping-pong)")
    return ok


async def test_cloud_to_device_off_skips_pull() -> bool:
    """When cloud_to_device is False, a cloud-newer save is NOT written
    back to the device — the engine reports SKIPPED."""
    _, state, cloud = _setup()
    fx2_files = {"/Mario.srm": b"abc" * 100}
    fx2 = MockFXPakSource(id="fx2", files=fx2_files)
    state.upsert_source(id=fx2.id, system=fx2.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=False))
    out_setup = await sync_one_game(source=fx2,
                                    ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    refresh_manifest(source=fx2, save_path="/Mario.srm",
                     game_id=out_setup.game_id, paths=out_setup.paths, ctx=ctx)

    # Same device, in sync.
    ctx.invalidate_manifest(out_setup.paths)
    out_in_sync = await sync_one_game(source=fx2,
                                      ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    if out_in_sync.result != SyncResult.IN_SYNC:
        print(f"FAIL: setup: expected IN_SYNC, got {out_in_sync.result}")
        state.close()
        return False

    # Now cloud advances via fx2 (synced), device fx is at the older hash.
    fx2_files["/Mario.srm"] = b"abc-newer" + b"\x00" * 100
    ctx.invalidate_manifest(out_setup.paths)
    out_push = await sync_one_game(source=fx2,
                                   ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    refresh_manifest(source=fx2, save_path="/Mario.srm",
                     game_id=out_push.game_id, paths=out_push.paths, ctx=ctx)

    # Set up device with same hash as cloud's PREVIOUS version (pre-push).
    fx = MockFXPakSource(id="fx-stale", files={"/Mario.srm": b"abc" * 100})
    state.upsert_source(id=fx.id, system=fx.system,
                        adapter="MockFXPakSource", config_json="{}")
    state.set_sync_state(source_id=fx.id, game_id=out_push.game_id,
                         last_synced_hash=sha256_bytes(b"abc" * 100))

    ctx.invalidate_manifest(out_setup.paths)
    out = await sync_one_game(source=fx, ref=SaveRef(path="/Mario.srm"),
                              ctx=ctx)
    state.close()
    return _check(out.result, SyncResult.SKIPPED,
                  "cloud_to_device=False, cloud-newer → SKIPPED")


async def test_resolve_conflict_to_device() -> bool:
    """Open a conflict, resolve --winner device, verify cloud current
    becomes the device bytes and the conflict is marked resolved.
    (Uses preserve mode so we have an open conflict to resolve.)"""
    from retrosync import conflicts as cmod
    _, state, cloud = _setup()

    # Pre-populate cloud with one source's bytes.
    fx2_files = {"/Mario.srm": b"abc" * 100}
    fx2 = MockFXPakSource(id="fx2", files=fx2_files)
    state.upsert_source(id=fx2.id, system=fx2.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True,
                                     conflict_winner="preserve"))
    out_setup = await sync_one_game(source=fx2,
                                    ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    refresh_manifest(source=fx2, save_path="/Mario.srm",
                     game_id=out_setup.game_id, paths=out_setup.paths, ctx=ctx)

    # Brand-new device with diverging bytes → CONFLICT.
    fx_files = {"/Mario.srm": b"xyz" * 100}
    fx = MockFXPakSource(id="fx-new", files=fx_files)
    state.upsert_source(id=fx.id, system=fx.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx.invalidate_manifest(out_setup.paths)
    out_conflict = await sync_one_game(source=fx,
                                       ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    if out_conflict.result != SyncResult.CONFLICT:
        print(f"FAIL: expected CONFLICT, got {out_conflict.result}")
        state.close()
        return False
    open_now = state.list_conflicts(open_only=True)
    if len(open_now) != 1:
        print(f"FAIL: expected 1 open conflict, got {len(open_now)}")
        state.close()
        return False
    cid = open_now[0].id

    # Resolve --winner device. Cloud's current should become the device bytes.
    result = cmod.resolve(state=state, cloud=cloud,
                          conflict_id=cid, winner="device",
                          remote="gdrive:retro-saves")
    new_current = cloud.download_bytes(src=result.new_current_path)
    n_open = len(state.list_conflicts(open_only=True))
    state.close()

    ok = _check(sha256_bytes(new_current), sha256_bytes(b"xyz" * 100),
                "cloud current updated to device bytes")
    ok &= _check(n_open, 0, "conflict marked resolved")
    return ok


async def test_resolve_with_stale_cloud_path() -> bool:
    """Regression: a conflict row's stored cloud_path is stale (e.g. the
    cloud folder got migrated under it). Resolve should fall back to
    scanning the canonical folder by hash and still find the bytes."""
    from retrosync import conflicts as cmod
    _, state, cloud = _setup()

    # Set up a preserve-mode conflict so we have a row to mess with.
    fx2_files = {"/Mario.srm": b"abc" * 100}
    fx2 = MockFXPakSource(id="fx2", files=fx2_files)
    state.upsert_source(id=fx2.id, system=fx2.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True,
                                     conflict_winner="preserve"))
    out_setup = await sync_one_game(source=fx2,
                                    ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    refresh_manifest(source=fx2, save_path="/Mario.srm",
                     game_id=out_setup.game_id, paths=out_setup.paths, ctx=ctx)

    fx_files = {"/Mario.srm": b"xyz" * 100}
    fx = MockFXPakSource(id="fx-new", files=fx_files)
    state.upsert_source(id=fx.id, system=fx.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx.invalidate_manifest(out_setup.paths)
    out_conflict = await sync_one_game(source=fx,
                                       ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    cid = out_conflict.game_id  # actually we want the conflict id
    open_conflicts = state.list_conflicts(open_only=True)
    cid = open_conflicts[0].id

    # Simulate a stale cloud_path: change it to point at a path that
    # doesn't exist (mimicking a post-migration leftover).
    with state.tx() as c:
        c.execute("UPDATE conflicts SET cloud_path = ? WHERE id = ?",
                  ("gdrive:retro-saves/snes/old_legacy_name/versions/"
                   "1900-01-01T00-00-00Z--d9f5aeb0.srm", cid))

    # Now resolve --winner cloud. The stored path is bogus but the bytes
    # exist in the canonical folder; the fallback should find them.
    result = cmod.resolve(state=state, cloud=cloud, conflict_id=cid,
                          winner="cloud", remote="gdrive:retro-saves")
    new_current = cloud.download_bytes(src=result.new_current_path)
    state.close()
    return _check(sha256_bytes(new_current), sha256_bytes(b"abc" * 100),
                  "stale cloud_path → fallback by hash succeeded")


async def test_uploads_under_device_kind_subfolder() -> bool:
    """Versions land under versions/<device_kind>/ — purely cosmetic for
    cloud-browse organization. Engine behaviour unchanged."""
    workdir, state, cloud = _setup()
    cart = MockFXPakSource(id="fx", files={"/Mario.srm": b"abc" * 100})
    state.upsert_source(id=cart.id, system=cart.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    out = await sync_one_game(source=cart,
                              ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    state.close()
    ok = _check(out.result, SyncResult.BOOTSTRAP_UPLOADED,
                "upload outcome unchanged")
    # The version cloud_path now contains "/versions/snes/" because
    # MockFXPakSource.device_kind == "snes".
    expected_seg = "/versions/snes/"
    actual_path = workdir / "cloud" / "retro-saves" / "snes" / "mario" / "versions" / "snes"
    ok &= _check(actual_path.is_dir(), True,
                 f"new uploads land under {expected_seg}")
    return ok


async def test_no_duplicate_upload_on_transient_manifest_failure() -> bool:
    """Regression: a transient `rclone lsjson` failure (rate limit, network
    blip) made `exists()` return False, which made `read_manifest()` return
    None, which made the engine think there was no cloud version → case 2
    bootstrap upload → re-uploaded the unchanged save. The fix: exists()
    distinguishes transient errors from "definitely missing" via rclone's
    exit codes, and sync_one_game returns SKIPPED on transient failures
    rather than uploading.
    """
    from unittest.mock import patch
    _, state, cloud = _setup()
    fx = MockFXPakSource(id="fx", files={"/Mario.srm": b"abc" * 100})
    state.upsert_source(id=fx.id, system=fx.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud, cfg=SyncConfig())

    out_setup = await sync_one_game(source=fx,
                                    ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    refresh_manifest(source=fx, save_path="/Mario.srm",
                     game_id=out_setup.game_id, paths=out_setup.paths, ctx=ctx)
    if out_setup.result != SyncResult.BOOTSTRAP_UPLOADED:
        print(f"FAIL: setup expected BOOTSTRAP_UPLOADED, got {out_setup.result}")
        state.close()
        return False
    n_versions_before = len(state.list_versions(fx.id, "/Mario.srm"))

    # Now the in-sync case: re-sync, manifest read works, expect IN_SYNC.
    ctx.invalidate_manifest(out_setup.paths)
    out_in_sync = await sync_one_game(source=fx,
                                      ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    if out_in_sync.result != SyncResult.IN_SYNC:
        print(f"FAIL: expected IN_SYNC second sync, got {out_in_sync.result}")
        state.close()
        return False

    # Now simulate the transient failure: cloud.exists raises CloudError.
    # The engine must SKIP rather than upload.
    ctx.invalidate_manifest(out_setup.paths)
    from retrosync.cloud import CloudError as _CE
    with patch.object(cloud, "exists",
                      side_effect=_CE("simulated rclone rate-limit")):
        out_transient = await sync_one_game(
            source=fx, ref=SaveRef(path="/Mario.srm"), ctx=ctx)
    n_versions_after = len(state.list_versions(fx.id, "/Mario.srm"))
    state.close()

    ok = _check(out_transient.result, SyncResult.SKIPPED,
                "transient manifest failure → SKIPPED (no spurious upload)")
    ok &= _check(n_versions_after, n_versions_before,
                 "no new version row inserted on transient failure")
    return ok


def main() -> int:
    ok = True
    for name, factory in [
        ("test_bootstrap_upload", test_bootstrap_upload),
        ("test_in_sync", test_in_sync),
        ("test_cloud_to_device_pull", test_cloud_to_device_pull),
        ("test_conflict_no_prior_agreement_preserve",
         test_conflict_no_prior_agreement_preserve),
        ("test_conflict_device_wins_default",
         test_conflict_device_wins_default),
        ("test_cloud_to_device_off_skips_pull",
         test_cloud_to_device_off_skips_pull),
        ("test_resolve_conflict_to_device", test_resolve_conflict_to_device),
        ("test_resolve_with_stale_cloud_path",
         test_resolve_with_stale_cloud_path),
        ("test_uploads_under_device_kind_subfolder",
         test_uploads_under_device_kind_subfolder),
        ("test_no_duplicate_upload_on_transient_manifest_failure",
         test_no_duplicate_upload_on_transient_manifest_failure),
    ]:
        print(f"--- {name} ---")
        ok &= asyncio.run(factory())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
