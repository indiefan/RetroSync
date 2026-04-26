"""Tests for `retrosync load <game_id> <target>` — the convenience
'pull cloud current → write to device' shortcut.

Run with:
    PYTHONPATH=. python3 tests/test_load.py
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
from retrosync.config import Config, SourceConfig  # noqa: E402
from retrosync.sources.base import SaveRef  # noqa: E402
from retrosync.sources.registry import register  # noqa: E402
from retrosync.state import StateStore  # noqa: E402
from retrosync.sync import (  # noqa: E402
    SyncConfig, SyncContext, refresh_manifest, sync_one_game,
)
from tests.mock_source import MockFXPakSource  # noqa: E402


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


def _setup():
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-load-"))
    cloud_root = workdir / "cloud"
    cloud_root.mkdir()
    db_path = workdir / "state.db"

    fake_rclone = ROOT / "tests" / "fake_rclone.sh"
    os.chmod(fake_rclone, 0o755)
    os.environ["FAKE_RCLONE_ROOT"] = str(cloud_root)

    state = StateStore(str(db_path))
    cloud = RcloneCloud(remote="gdrive:retro-saves",
                        binary=str(fake_rclone))
    return workdir, state, cloud, str(fake_rclone)


def _seed_cloud_with(state: StateStore, cloud: RcloneCloud, *,
                     source_id: str, files: dict[str, bytes]):
    """Helper: run a sync_one_game pass synchronously to seed the cloud."""
    src = MockFXPakSource(id=source_id, files=files)
    state.upsert_source(id=src.id, system=src.system,
                        adapter="MockFXPakSource", config_json="{}")
    ctx = SyncContext(state=state, cloud=cloud, cfg=SyncConfig())
    outs = []
    for path in files:
        out = asyncio.run(
            sync_one_game(source=src, ref=SaveRef(path=path), ctx=ctx))
        refresh_manifest(source=src, save_path=path,
                         game_id=out.game_id, paths=out.paths, ctx=ctx)
        outs.append(out)
    return outs


def test_load_to_cart() -> bool:
    """Upload from one MockFXPakSource, then load back via `load(... 'snes')`
    to a different MockFXPakSource (simulating a fresh cart) — the cart's
    bytes should now match cloud."""
    workdir, state, cloud, fake_rclone = _setup()

    outs = _seed_cloud_with(state, cloud, source_id="fxpak-pro-1",
                            files={"/Mario.srm":
                                   b"FXPAK-A" + b"\x00" * 200})
    out = outs[0]
    state.close()

    # Step 2: register a custom mock_fxpak adapter so load() can build it
    # from the config the way it would in production.
    target_files = {"/Mario.srm": b"OLD-CART" + b"\x00" * 200}

    def _build(*, id, **_):
        s = MockFXPakSource(id=id, files=target_files)
        return s

    try:
        register("mock_fxpak_for_load", _build)
    except ValueError:
        pass

    cfg = Config(
        cloud=Config().cloud, orchestrator=Config().orchestrator,
        state=Config().state,
        sources=[SourceConfig(id="fxpak-pro-1",
                              adapter="mock_fxpak_for_load",
                              options={})],
    )
    cfg.cloud.rclone_remote = "gdrive:retro-saves"
    cfg.cloud.rclone_binary = fake_rclone
    cfg.state.db_path = str(workdir / "state.db")

    # state.db has the file row pointing at /Mario.srm — load should find it.
    from retrosync.load import load
    result = load(cfg=cfg, game_id=out.game_id, target="snes")

    return (
        _check(target_files["/Mario.srm"], b"FXPAK-A" + b"\x00" * 200,
               "cart bytes overwritten with cloud current")
        and _check(result.target, "snes", "result.target")
        and _check(result.written_path, "/Mario.srm", "result.written_path")
        and _check(result.sha256,
                   sha256_bytes(b"FXPAK-A" + b"\x00" * 200),
                   "result.sha256")
    )


def test_load_target_unknown_game_id() -> bool:
    """When game_id has no current.srm in cloud, load raises a clear error."""
    workdir, state, cloud, fake_rclone = _setup()
    state.close()
    cfg = Config()
    cfg.cloud.rclone_remote = "gdrive:retro-saves"
    cfg.cloud.rclone_binary = fake_rclone
    cfg.state.db_path = str(workdir / "state.db")

    from retrosync.load import load
    try:
        load(cfg=cfg, game_id="nonexistent", target="snes")
    except FileNotFoundError as exc:
        return _check("no current save in cloud" in str(exc), True,
                      "missing game_id → FileNotFoundError")
    print("FAIL: expected FileNotFoundError for missing game_id")
    return False


def test_load_no_cart_source_for_system() -> bool:
    """When no cart adapter is configured for the requested system,
    load raises a clear error."""
    workdir, state, cloud, fake_rclone = _setup()
    outs = _seed_cloud_with(state, cloud, source_id="fxpak-pro-1",
                            files={"/Mario.srm": b"x" * 50})
    out = outs[0]
    state.close()

    cfg = Config()  # empty sources list
    cfg.cloud.rclone_remote = "gdrive:retro-saves"
    cfg.cloud.rclone_binary = fake_rclone
    cfg.state.db_path = str(workdir / "state.db")

    from retrosync.load import load
    try:
        load(cfg=cfg, game_id=out.game_id, target="snes")
    except ValueError as exc:
        return _check("no non-pocket source configured" in str(exc), True,
                      "missing cart adapter → ValueError")
    print("FAIL: expected ValueError for missing cart source")
    return False


def main() -> int:
    ok = True
    for name, fn in [
        ("test_load_to_cart", test_load_to_cart),
        ("test_load_target_unknown_game_id", test_load_target_unknown_game_id),
        ("test_load_no_cart_source_for_system",
         test_load_no_cart_source_for_system),
    ]:
        print(f"--- {name} ---")
        ok &= fn()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
