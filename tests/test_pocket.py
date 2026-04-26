"""End-to-end test for PocketSource against a local-dir 'mount'.

Builds a temp directory shaped like a Pocket SD (`Saves/agg23.SNES/Mario.sav`),
runs the sync engine against it, and exercises bootstrap-pull from cloud.

Run with:
    PYTHONPATH=. python3 tests/test_pocket.py
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
from retrosync.config import Config  # noqa: E402
from retrosync.pocket.sync_runner import run_pocket_sync  # noqa: E402
from retrosync.sources.pocket import PocketConfig, PocketSource  # noqa: E402
from retrosync.state import StateStore  # noqa: E402
from retrosync.sync import (  # noqa: E402
    SyncConfig, SyncContext, SyncResult, refresh_manifest, sync_one_game,
)
from tests.mock_source import MockFXPakSource  # noqa: E402


def _setup():
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-pocket-"))
    cloud_root = workdir / "cloud"
    cloud_root.mkdir()
    mount = workdir / "mount"
    (mount / "Saves" / "agg23.SNES").mkdir(parents=True)
    db_path = workdir / "state.db"

    fake_rclone = ROOT / "tests" / "fake_rclone.sh"
    os.chmod(fake_rclone, 0o755)
    os.environ["FAKE_RCLONE_ROOT"] = str(cloud_root)

    state = StateStore(str(db_path))
    cloud = RcloneCloud(remote="gdrive:retro-saves",
                        binary=str(fake_rclone))
    return workdir, mount, state, cloud


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


async def test_pocket_upload() -> bool:
    """A pocket save with no cloud counterpart bootstraps to cloud."""
    _, mount, state, cloud = _setup()
    save = mount / "Saves" / "agg23.SNES" / "Super Metroid.sav"
    save.write_bytes(b"POCKET-SAVE" + b"\x00" * 100)

    source = PocketSource(PocketConfig(
        id="pocket-1", mount_path=str(mount), core="agg23.SNES",
        file_extension=".sav", system="snes",
    ))
    state.upsert_source(id=source.id, system=source.system,
                        adapter="PocketSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))

    saves = await source.list_saves()
    if len(saves) != 1:
        print(f"FAIL: expected 1 save in pocket, got {len(saves)}")
        state.close()
        return False
    out = await sync_one_game(source=source, ref=saves[0], ctx=ctx)
    state.close()
    return _check(out.result, SyncResult.BOOTSTRAP_UPLOADED,
                  "pocket save → bootstrap upload")


async def test_pocket_to_fxpak_roundtrip() -> bool:
    """A save uploaded by the FXPak mock can be pulled to the Pocket
    via bootstrap-pull."""
    _, mount, state, cloud = _setup()
    # FXPak side uploads first.
    fx_files = {"/Super Metroid.srm": b"FX-SAVE" + b"\x00" * 200}
    fx = MockFXPakSource(id="fx", files=fx_files)
    state.upsert_source(id=fx.id, system=fx.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud,
                      cfg=SyncConfig(cloud_to_device=True))
    from retrosync.sources.base import SaveRef
    fx_out = await sync_one_game(
        source=fx, ref=SaveRef(path="/Super Metroid.srm"), ctx=ctx)
    refresh_manifest(source=fx, save_path="/Super Metroid.srm",
                     game_id=fx_out.game_id, paths=fx_out.paths, ctx=ctx)

    # Now the Pocket plugs in. Empty saves dir; bootstrap-pull should
    # write super_metroid.sav.
    source = PocketSource(PocketConfig(
        id="pocket-1", mount_path=str(mount), core="agg23.SNES",
        file_extension=".sav", system="snes",
    ))
    state.upsert_source(id=source.id, system=source.system,
                        adapter="PocketSource", config_json="{}")
    cfg = Config()
    cfg.cloud_to_device = True
    cfg.state.db_path = str(state._db_path)
    cfg.cloud.rclone_remote = "gdrive:retro-saves"
    cfg.cloud.rclone_binary = str(ROOT / "tests" / "fake_rclone.sh")
    summary = await run_pocket_sync(source=source, config=cfg)

    target = mount / "Saves" / "agg23.SNES" / "super_metroid.sav"
    state.close()

    ok = _check(target.exists(), True,
                "bootstrap-pulled file written to pocket")
    if target.exists():
        ok &= _check(target.read_bytes(), fx_files["/Super Metroid.srm"],
                     "pocket file matches what FXPak uploaded")
    ok &= _check(summary.downloaded, 1, "summary records 1 download")
    return ok


def test_existing_save_for_matches_by_slug() -> bool:
    """PocketSource.existing_save_for(<slug>) finds the on-device file
    whose ROM-style name canonicalizes to <slug>. Used by `load` so we
    overwrite the file the Pocket actually loads, not a slug-named copy
    the ROM doesn't look at."""
    workdir, mount, state, cloud = _setup()
    save = mount / "Saves" / "agg23.SNES" / "Final Fantasy III (U) (v1.1).sav"
    save.write_bytes(b"existing-bytes" + b"\x00" * 100)

    source = PocketSource(PocketConfig(
        id="pocket-1", mount_path=str(mount), core="agg23.SNES",
        file_extension=".sav", system="snes",
    ))
    state.close()

    found = source.existing_save_for("final_fantasy_iii")
    ok = _check(found, save,
                "existing_save_for finds ROM-named file by canonical slug")
    none = source.existing_save_for("nonexistent_game")
    ok &= _check(none, None,
                 "existing_save_for returns None when no file matches")
    return ok


def test_existing_save_for_prefers_decorated_name() -> bool:
    """When both `final_fantasy_iii.sav` (slug fallback from a previous
    load) and `Final Fantasy III (U) (v1.1).sav` (ROM-named original)
    coexist, existing_save_for must return the ROM-named one — that's
    the file the Pocket actually loads."""
    workdir, mount, state, cloud = _setup()
    saves_dir = mount / "Saves" / "agg23.SNES"
    decorated = saves_dir / "Final Fantasy III (U) (v1.1).sav"
    slug_named = saves_dir / "final_fantasy_iii.sav"
    decorated.write_bytes(b"DECORATED")
    slug_named.write_bytes(b"SLUG")

    source = PocketSource(PocketConfig(
        id="pocket-1", mount_path=str(mount), core="agg23.SNES",
        file_extension=".sav", system="snes",
    ))
    state.close()

    found = source.existing_save_for("final_fantasy_iii")
    return _check(found, decorated,
                  "prefers ROM-decorated name when slug-named also exists")


def test_derive_source_id_for_device() -> bool:
    """Two SDs with different UUIDs map to different source_ids; missing
    device falls back to the default."""
    from unittest.mock import patch
    from retrosync.pocket.sync_runner import (derive_source_id_for_device,
                                              read_device_uuid)
    ok = _check(derive_source_id_for_device(device=None), "pocket-1",
                "no device → fallback")

    with patch("retrosync.pocket.sync_runner.read_device_uuid",
               return_value="6434-3362"):
        ok &= _check(
            derive_source_id_for_device(device="/dev/sda1"),
            "pocket-6434-3362",
            "uuid 6434-3362 → pocket-6434-3362")
    with patch("retrosync.pocket.sync_runner.read_device_uuid",
               return_value="ABCD-EF01"):
        ok &= _check(
            derive_source_id_for_device(device="/dev/sda1"),
            "pocket-ABCD-EF01",
            "different uuid → different source_id")
    with patch("retrosync.pocket.sync_runner.read_device_uuid",
               return_value=None):
        ok &= _check(
            derive_source_id_for_device(device="/dev/sda1",
                                        fallback="pocket-1"),
            "pocket-1",
            "blkid failure → fallback")
    return ok


def main() -> int:
    ok = True
    for name, factory in [
        ("test_pocket_upload", test_pocket_upload),
        ("test_pocket_to_fxpak_roundtrip", test_pocket_to_fxpak_roundtrip),
    ]:
        print(f"--- {name} ---")
        ok &= asyncio.run(factory())
    print("--- test_existing_save_for_matches_by_slug ---")
    ok &= test_existing_save_for_matches_by_slug()
    print("--- test_existing_save_for_prefers_decorated_name ---")
    ok &= test_existing_save_for_prefers_decorated_name()
    print("--- test_derive_source_id_for_device ---")
    ok &= test_derive_source_id_for_device()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
