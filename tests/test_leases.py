"""Lease lifecycle: acquire / heartbeat / release / contention modes."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync import leases  # noqa: E402
from retrosync.cloud import (  # noqa: E402
    ActiveLease, RcloneCloud, compose_paths,
)


def _setup() -> tuple[Path, RcloneCloud]:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-leases-"))
    cloud_root = workdir / "cloud"
    cloud_root.mkdir()
    fake_rclone = ROOT / "tests" / "fake_rclone.sh"
    os.chmod(fake_rclone, 0o755)
    os.environ["FAKE_RCLONE_ROOT"] = str(cloud_root)
    cloud = RcloneCloud(remote="gdrive:retro-saves",
                        binary=str(fake_rclone))
    return workdir, cloud


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


def test_acquire_on_empty_manifest() -> bool:
    """Empty cloud (no manifest yet) — acquire should succeed."""
    _, cloud = _setup()
    paths = compose_paths(remote=cloud.remote, system="snes",
                          game_id="metroid", save_filename="x.srm")
    out = leases.acquire(cloud=cloud, paths=paths,
                         source_id="deck-1", mode=leases.MODE_SOFT)
    ok = _check(out.acquired, True, "acquire on empty manifest")
    ok &= _check(out.contended, False, "no contention")
    ok &= _check(out.lease.source_id, "deck-1", "lease source_id is us")
    # Verify cloud now has the lease.
    manifest = cloud.read_manifest(paths)
    ok &= _check(manifest is not None, True, "manifest written")
    if manifest is not None:
        ok &= _check(manifest.active_lease.source_id, "deck-1",
                     "manifest.active_lease.source_id")
    return ok


def test_hard_mode_contention() -> bool:
    """Hard mode + held by other → LeaseContended."""
    _, cloud = _setup()
    paths = compose_paths(remote=cloud.remote, system="snes",
                          game_id="zelda", save_filename="x.srm")
    leases.acquire(cloud=cloud, paths=paths, source_id="pi",
                   mode=leases.MODE_SOFT)
    try:
        leases.acquire(cloud=cloud, paths=paths, source_id="deck-1",
                       mode=leases.MODE_HARD)
    except leases.LeaseContended as exc:
        return _check(exc.lease.source_id, "pi",
                      "hard-mode raises LeaseContended with holder")
    print("FAIL: hard-mode contention should have raised")
    return False


def test_soft_mode_steals_with_warning() -> bool:
    """Soft mode + held by other → acquire succeeds, contended=True."""
    _, cloud = _setup()
    paths = compose_paths(remote=cloud.remote, system="snes",
                          game_id="zelda", save_filename="x.srm")
    leases.acquire(cloud=cloud, paths=paths, source_id="pi",
                   mode=leases.MODE_SOFT)
    out = leases.acquire(cloud=cloud, paths=paths, source_id="deck-1",
                         mode=leases.MODE_SOFT)
    ok = _check(out.acquired, True, "soft mode steals")
    ok &= _check(out.contended, True, "contended flag set")
    ok &= _check(out.prior.source_id, "pi", "prior holder recorded")
    return ok


def test_expired_lease_can_be_reclaimed() -> bool:
    """Lease past its expires_at acts as released."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    stale = ActiveLease(source_id="ghost", started_at=past,
                        expires_at=past, last_heartbeat=past)
    return _check(leases.is_expired(stale), True,
                  "lease past expires_at is expired")


def test_release_only_clears_our_own() -> bool:
    """release() leaves another holder's lease alone unless force=True."""
    _, cloud = _setup()
    paths = compose_paths(remote=cloud.remote, system="snes",
                          game_id="zelda", save_filename="x.srm")
    leases.acquire(cloud=cloud, paths=paths, source_id="pi",
                   mode=leases.MODE_SOFT)
    cleared = leases.release(cloud=cloud, paths=paths,
                             source_id="deck-1", force=False)
    ok = _check(cleared, False, "release as wrong source → no-op")
    manifest = cloud.read_manifest(paths)
    ok &= _check(manifest.active_lease.source_id, "pi",
                 "lease still held by pi")
    cleared = leases.release(cloud=cloud, paths=paths,
                             source_id="deck-1", force=True)
    ok &= _check(cleared, True, "force release succeeds")
    manifest = cloud.read_manifest(paths)
    ok &= _check(manifest.active_lease, None, "lease cleared")
    return ok


def test_heartbeat_extends_expires_at() -> bool:
    """heartbeat() refreshes expires_at and last_heartbeat in place."""
    _, cloud = _setup()
    paths = compose_paths(remote=cloud.remote, system="snes",
                          game_id="zelda", save_filename="x.srm")
    out = leases.acquire(cloud=cloud, paths=paths, source_id="deck-1",
                         mode=leases.MODE_SOFT, ttl_minutes=5)
    first_expires = out.lease.expires_at
    # Heartbeat with longer TTL should extend.
    ok = leases.heartbeat(cloud=cloud, paths=paths, source_id="deck-1",
                          ttl_minutes=60)
    manifest = cloud.read_manifest(paths)
    return (_check(ok, True, "heartbeat returns True for own lease")
            and _check(manifest.active_lease.expires_at != first_expires,
                       True, "expires_at extended"))


def test_heartbeat_returns_false_when_stolen() -> bool:
    """heartbeat() detects when someone else has stolen the lease."""
    _, cloud = _setup()
    paths = compose_paths(remote=cloud.remote, system="snes",
                          game_id="zelda", save_filename="x.srm")
    leases.acquire(cloud=cloud, paths=paths, source_id="deck-1",
                   mode=leases.MODE_SOFT)
    leases.acquire(cloud=cloud, paths=paths, source_id="pi",
                   mode=leases.MODE_SOFT)  # steals
    ok = leases.heartbeat(cloud=cloud, paths=paths, source_id="deck-1",
                          ttl_minutes=5)
    return _check(ok, False, "heartbeat returns False after steal")


def main() -> int:
    ok = True
    for name, fn in [
        ("acquire_on_empty_manifest", test_acquire_on_empty_manifest),
        ("hard_mode_contention", test_hard_mode_contention),
        ("soft_mode_steals_with_warning", test_soft_mode_steals_with_warning),
        ("expired_lease_reclaimable", test_expired_lease_can_be_reclaimed),
        ("release_only_clears_our_own", test_release_only_clears_our_own),
        ("heartbeat_extends_expires_at", test_heartbeat_extends_expires_at),
        ("heartbeat_returns_false_when_stolen",
         test_heartbeat_returns_false_when_stolen),
    ]:
        print(f"--- {name} ---")
        ok &= fn()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
