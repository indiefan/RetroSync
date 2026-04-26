"""Test the migrate-paths CLI logic against the fake rclone tree.

Run with:
    PYTHONPATH=. python3 tests/test_migrate.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync.cloud import RcloneCloud  # noqa: E402
from retrosync.migrate import (derive_canonical_id, migrate,  # noqa: E402
                               plan_migration)
from retrosync.state import StateStore  # noqa: E402


def _setup() -> tuple[Path, Path, RcloneCloud, StateStore]:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-migrate-"))
    cloud_root = workdir / "cloud"
    snes = cloud_root / "retro-saves" / "snes"
    snes.mkdir(parents=True)

    fake_rclone = ROOT / "tests" / "fake_rclone.sh"
    os.chmod(fake_rclone, 0o755)
    os.environ["FAKE_RCLONE_ROOT"] = str(cloud_root)
    cloud = RcloneCloud(remote="gdrive:retro-saves",
                        binary=str(fake_rclone))
    state = StateStore(str(workdir / "state.db"))
    return workdir, snes, cloud, state


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


def test_derive_canonical_id() -> bool:
    cases = [
        ("unknown_super_metroid", "super_metroid"),
        ("0a1b2c3d_super_metroid", "super_metroid"),
        ("0003b800_super_metroid", "super_metroid"),
        ("super_metroid", "super_metroid"),
        ("UNKNOWN_chrono_trigger", "chrono_trigger"),
    ]
    ok = True
    for legacy, want in cases:
        ok &= _check(derive_canonical_id(legacy), want,
                     f"derive({legacy!r})")
    return ok


def test_rename_only() -> bool:
    """unknown_X with no canonical X → straightforward rename."""
    workdir, snes, cloud, state = _setup()
    legacy_dir = snes / "unknown_super_metroid"
    legacy_dir.mkdir()
    (legacy_dir / "current.srm").write_bytes(b"X" * 100)
    (legacy_dir / "manifest.json").write_text("{}")
    (legacy_dir / "versions").mkdir()
    (legacy_dir / "versions" / "2026-04-25T18-23-11Z--abc.srm"
     ).write_bytes(b"V1")

    plan = plan_migration(cloud=cloud, system="snes")
    actions = sorted([(p.legacy_id, p.action) for p in plan])
    _check(actions, [("unknown_super_metroid", "rename")],
           "plan: rename unknown_super_metroid")

    counts = migrate(cloud=cloud, system="snes", state=state)
    state.close()

    canonical_dir = snes / "super_metroid"
    return (
        _check(canonical_dir.exists(), True,
               "canonical dir exists post-migrate")
        and _check(legacy_dir.exists(), False,
                   "legacy dir is gone")
        and _check((canonical_dir / "current.srm").exists(), True,
                   "current.srm moved into place")
        and _check(counts["rename"], 1, "rename count = 1")
    )


def test_merge_into_existing() -> bool:
    """legacy unknown_X already coexists with canonical X. Merge."""
    workdir, snes, cloud, state = _setup()
    canonical = snes / "super_metroid"
    canonical.mkdir()
    (canonical / "current.srm").write_bytes(b"FRESH")
    (canonical / "versions").mkdir()
    (canonical / "versions" / "2026-04-26T00-00-00Z--new.srm"
     ).write_bytes(b"new")

    legacy = snes / "unknown_super_metroid"
    legacy.mkdir()
    (legacy / "current.srm").write_bytes(b"OLD")
    (legacy / "versions").mkdir()
    (legacy / "versions" / "2026-04-25T00-00-00Z--old.srm"
     ).write_bytes(b"old")

    plan = plan_migration(cloud=cloud, system="snes")
    actions = sorted([(p.legacy_id, p.action) for p in plan])
    _check(actions,
           [("super_metroid", "noop"),
            ("unknown_super_metroid", "merge")],
           "plan: merge legacy into canonical")

    migrate(cloud=cloud, system="snes", state=state)
    state.close()

    new_versions = sorted((canonical / "versions").iterdir())
    return (
        _check(legacy.exists(), False, "legacy dir removed")
        and _check(len(new_versions), 2,
                   "canonical now has both version files")
        and _check((canonical / "current.srm").read_bytes(), b"FRESH",
                   "canonical's current.srm preserved (not overwritten)")
    )


def test_idempotent() -> bool:
    """Running migrate again on a clean tree does nothing."""
    workdir, snes, cloud, state = _setup()
    (snes / "super_metroid").mkdir()
    (snes / "chrono_trigger").mkdir()
    counts = migrate(cloud=cloud, system="snes", state=state)
    state.close()
    return _check(counts.get("rename", 0) + counts.get("merge", 0), 0,
                  "no actions on already-canonical tree")


def main() -> int:
    ok = True
    for name, fn in [
        ("test_derive_canonical_id", test_derive_canonical_id),
        ("test_rename_only", test_rename_only),
        ("test_merge_into_existing", test_merge_into_existing),
        ("test_idempotent", test_idempotent),
    ]:
        print(f"--- {name} ---")
        ok &= fn()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
