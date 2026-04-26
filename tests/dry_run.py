"""End-to-end dry-run of the RetroSync daemon, with the cart and rclone
both mocked. Exercises orchestrator + state store + cloud wrapper.

Run with:
    cd retrosync
    PYTHONPATH=. python3 tests/dry_run.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync.cloud import RcloneCloud  # noqa: E402
from retrosync.config import (  # noqa: E402
    CloudConfig, OrchestratorConfig, SourceConfig, StateConfig, Config,
)
from retrosync.orchestrator import (  # noqa: E402
    BackupOrchestrator, OrchestratorDeps,
)
from retrosync.sources.registry import register  # noqa: E402
from retrosync.state import StateStore  # noqa: E402
from tests.mock_source import MockFXPakSource  # noqa: E402

log = logging.getLogger("dry_run")


def setup_mock_adapter():
    """Register a 'mock_fxpak' adapter so the orchestrator can build it."""
    def _build(*, id, files):
        return MockFXPakSource(id=id, files=files)
    try:
        register("mock_fxpak", _build)
    except ValueError:
        pass  # already registered (re-runs)


async def drive() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    setup_mock_adapter()

    workdir = Path(tempfile.mkdtemp(prefix="retrosync-dryrun-"))
    cloud_root = workdir / "cloud"
    cloud_root.mkdir()
    db_path = workdir / "state.db"

    fake_rclone = ROOT / "tests" / "fake_rclone.sh"
    os.chmod(fake_rclone, 0o755)
    os.environ["FAKE_RCLONE_ROOT"] = str(cloud_root)
    log.info("workdir=%s", workdir)

    files = {
        "/Super Metroid.smc": b"\x00ROM\x00" * 1024,
        "/Super Metroid.srm": b"SAVE-A" + b"\x00" * (32*1024 - 6),
        "/Zelda.smc": b"\x44ROM\x44" * 1024,
        "/Zelda.srm": b"ZELDA-A" + b"\x00" * (8*1024 - 7),
    }
    cart = MockFXPakSource(id="fxpak-pro-1", files=files)

    state = StateStore(str(db_path))
    cloud = RcloneCloud(remote="gdrive:retro-saves",
                        binary=str(fake_rclone))
    deps = OrchestratorDeps(
        state=state, cloud=cloud,
        cfg=OrchestratorConfig(poll_interval_sec=1, debounce_polls=2),
    )
    state.upsert_source(id=cart.id, system=cart.system,
                        adapter="MockFXPakSource", config_json="{}")

    orch = BackupOrchestrator(cart, deps)
    task = asyncio.create_task(orch.run())

    failures: list[str] = []

    async def settle(seconds: float):
        await asyncio.sleep(seconds)

    try:
        # ----- phase 1: initial ingest -----
        log.info("PHASE 1 — initial ingest")
        await settle(5)
        log.info("phase 1 cloud tree:\n%s", _tree(cloud_root))
        if not (cloud_root / "retro-saves" / "snes").exists():
            failures.append("phase 1: snes/ tree not created")
        else:
            game_dirs = list((cloud_root / "retro-saves" / "snes").iterdir())
            if len(game_dirs) != 2:
                failures.append(
                    f"phase 1: expected 2 game dirs, got {len(game_dirs)}")
            for gd in game_dirs:
                if not (gd / "current.srm").exists():
                    failures.append(f"phase 1: {gd.name}/current.srm missing")
                if not (gd / "manifest.json").exists():
                    failures.append(f"phase 1: {gd.name}/manifest.json missing")
                if not (gd / "versions").exists():
                    failures.append(f"phase 1: {gd.name}/versions missing")

        # ----- phase 2: change one save → expect a new version -----
        log.info("PHASE 2 — change Super Metroid save")
        cart.files["/Super Metroid.srm"] = b"SAVE-B" + b"\x01" * (32*1024 - 6)
        await settle(5)
        log.info("phase 2 cloud tree:\n%s", _tree(cloud_root))
        sm = next((d for d in (cloud_root / "retro-saves" / "snes").iterdir()
                   if "super_metroid" in d.name), None)
        if sm is None:
            failures.append("phase 2: super_metroid dir disappeared")
        else:
            versions = sorted((sm / "versions").iterdir())
            log.info("super_metroid versions: %s",
                     [v.name for v in versions])
            if len(versions) != 2:
                failures.append(
                    f"phase 2: expected 2 versions, got {len(versions)}")
            current = (sm / "current.srm").read_bytes()
            if not current.startswith(b"SAVE-B"):
                failures.append("phase 2: current.srm not updated to SAVE-B")
            manifest = json.loads((sm / "manifest.json").read_text())
            if len(manifest["versions"]) != 2:
                failures.append(
                    f"phase 2: manifest lists {len(manifest['versions'])} "
                    "versions, expected 2")

        # ----- phase 3: torn-write churn (debounce should absorb) -----
        log.info("PHASE 3 — torn-write churn")
        cart.files["/Super Metroid.srm"] = b"TORN-1"
        await settle(1)
        cart.files["/Super Metroid.srm"] = b"TORN-2"
        await settle(1)
        cart.files["/Super Metroid.srm"] = b"FINAL-D" + b"\x03" * 200
        await settle(5)
        sm = next(d for d in (cloud_root / "retro-saves" / "snes").iterdir()
                  if "super_metroid" in d.name)
        versions = sorted((sm / "versions").iterdir())
        log.info("after churn, versions: %s", [v.name for v in versions])
        # We expect: V1=SAVE-A, V2=SAVE-B, V3=FINAL-D. Torn-1 and Torn-2 should
        # have been superseded before they could promote.
        current = (sm / "current.srm").read_bytes()
        if not current.startswith(b"FINAL-D"):
            failures.append(
                f"phase 3: current.srm = {current[:8]!r}, expected FINAL-D")
        if len(versions) != 3:
            failures.append(
                f"phase 3: expected 3 versions after churn, got {len(versions)}")

        # ----- phase 4: idempotency — no save change → no new version -----
        log.info("PHASE 4 — quiescence")
        before = sorted((sm / "versions").iterdir())
        await settle(4)
        after = sorted((sm / "versions").iterdir())
        if [v.name for v in before] != [v.name for v in after]:
            failures.append(
                f"phase 4: versions changed despite no save change "
                f"({len(before)} -> {len(after)})")

        # ----- phase 5: source goes unhealthy → no errors, no upload churn -----
        log.info("PHASE 5 — cart unplugged")
        cart.break_()
        await settle(3)
        cart.heal()
        await settle(2)

        # Bonus: state store sanity.
        rows = list(state._conn.execute(
            "SELECT state, COUNT(*) AS n FROM versions GROUP BY state"))
        log.info("state-store version counts: %s",
                 {r["state"]: r["n"] for r in rows})

    finally:
        orch.cancel()
        try:
            await asyncio.wait_for(task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        state.close()

    if failures:
        log.error("DRY-RUN FAILED:")
        for f in failures:
            log.error("  - %s", f)
        return 1
    log.info("DRY-RUN PASSED — workdir preserved at %s", workdir)
    return 0


def _tree(root: Path, prefix: str = "") -> str:
    lines: list[str] = []
    for p in sorted(root.iterdir()):
        if p.is_dir():
            lines.append(f"{prefix}{p.name}/")
            sub = _tree(p, prefix + "  ")
            if sub:
                lines.append(sub)
        else:
            size = p.stat().st_size
            lines.append(f"{prefix}{p.name}  ({size} B)")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(asyncio.run(drive()))
