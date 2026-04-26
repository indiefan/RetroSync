"""One-shot Pocket sync runner.

Invoked by the systemd template unit `retrosync-pocket-sync@<dev>.service`
when udev sees the Analogue Pocket plug in. The sequence is:

  1. Wait briefly for the device to settle.
  2. Mount the partition at a known path under /run.
  3. Build a PocketSource pointed at the mount.
  4. Run a full bidirectional sync of every save the device has,
     plus a bootstrap-pull of any cloud games the device is missing
     a save for (when `cloud_to_device` is enabled).
  5. Unmount + power off the device.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..cloud import CloudError, RcloneCloud, compose_paths
from ..config import Config
from ..lease_tracker import LeaseTracker
from ..sources.base import SaveRef
from ..sources.pocket import PocketConfig, PocketSource
from ..state import StateStore
from ..sync import (SyncConfig, SyncContext, SyncResult, refresh_manifest,
                    sync_one_game)

log = logging.getLogger(__name__)


@dataclass
class PocketSyncSummary:
    uploaded: int = 0
    downloaded: int = 0
    in_sync: int = 0
    conflicts: int = 0
    skipped: int = 0
    errors: int = 0

    def add(self, result: SyncResult) -> None:
        if result in (SyncResult.UPLOADED, SyncResult.BOOTSTRAP_UPLOADED,
                      SyncResult.CONFLICT_RESOLVED):
            # CONFLICT_RESOLVED is a divergence we auto-resolved by uploading
            # the device's bytes — net effect on cloud is the same as UPLOADED.
            self.uploaded += 1
        elif result in (SyncResult.DOWNLOADED, SyncResult.BOOTSTRAP_DOWNLOADED):
            self.downloaded += 1
        elif result == SyncResult.IN_SYNC:
            self.in_sync += 1
        elif result == SyncResult.CONFLICT:
            self.conflicts += 1
        else:
            self.skipped += 1

    def render(self) -> str:
        return (f"{self.uploaded} uploads, {self.downloaded} downloads, "
                f"{self.in_sync} in-sync, {self.conflicts} conflicts, "
                f"{self.skipped} skipped, {self.errors} errors")


def _run(cmd: list[str], *, check: bool = True,
         capture: bool = False) -> subprocess.CompletedProcess:
    log.info("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=check,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None,
                          timeout=120)


def read_device_uuid(device: str) -> str | None:
    """Return the filesystem UUID of <device> via `blkid`, or None on
    error. Used to give each physical Pocket SD a stable identity so
    two Pockets don't share a single source_id and clobber each other's
    last_synced_hash pointer.
    """
    try:
        proc = subprocess.run(
            ["blkid", "-o", "value", "-s", "UUID", device],
            capture_output=True, text=True, check=True, timeout=5)
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        log.warning("blkid for %s failed: %s", device, exc)
        return None
    uuid = proc.stdout.strip()
    return uuid or None


def derive_source_id_for_device(*, device: str | None,
                                fallback: str = "pocket-1") -> str:
    """Auto-derive a per-physical-device source_id from the SD card's
    filesystem UUID. Multiple Analogue Pockets each get a distinct
    identity (`pocket-<uuid>`); falls back to <fallback> if blkid
    isn't available or the device has no UUID."""
    if device is None:
        return fallback
    uuid = read_device_uuid(device)
    if not uuid:
        return fallback
    safe = "".join(c if c.isalnum() else "-" for c in uuid)
    return f"pocket-{safe}"


def mount_pocket(*, device: str, mount_path: str,
                 settle_seconds: float = 1.0) -> None:
    """Mount /dev/sdX1 at <mount_path>. Lets the kernel auto-detect the
    filesystem so vfat (small SDs) and exfat (>32 GB SDs, the default
    above the FAT32 size limit) both work.
    """
    Path(mount_path).mkdir(parents=True, exist_ok=True)
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    # Re-mkdir after the settle: a parallel systemd unit's RuntimeDirectory
    # cleanup can race us between the initial mkdir and the mount call.
    Path(mount_path).mkdir(parents=True, exist_ok=True)
    _run(["mount", "-o", "rw,noatime", device, mount_path])


def unmount_pocket(*, mount_path: str, device: str | None = None) -> None:
    """umount + udisksctl power-off. Power-off lets the user pull the
    cable cleanly; on systems without udisks it's a no-op."""
    try:
        _run(["umount", mount_path], check=False)
    except subprocess.CalledProcessError as exc:
        log.warning("umount %s failed: %s", mount_path, exc)
    if device is not None:
        try:
            _run(["udisksctl", "power-off", "-b", device], check=False)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            log.debug("udisksctl power-off skipped: %s", exc)


def build_pocket_source(*, source_id: str, mount_path: str,
                        config: Config) -> PocketSource:
    """Build a PocketSource for the configured pocket entry, overriding
    its `mount_path` with the mount we just made. If the operator hasn't
    declared a `pocket` source in config.yaml, we fall back to defaults.
    """
    src_cfg = next((s for s in config.sources
                    if s.id == source_id and s.adapter == "pocket"), None)
    if src_cfg is None:
        log.warning("no 'pocket' source %r in config; using defaults",
                    source_id)
        return PocketSource(PocketConfig(
            id=source_id, mount_path=mount_path,
            game_aliases=dict(config.game_aliases),
        ))
    opts = dict(src_cfg.options)
    opts["mount_path"] = mount_path
    if "game_aliases" not in opts and config.game_aliases:
        opts["game_aliases"] = config.game_aliases
    return PocketSource(PocketConfig(
        id=src_cfg.id,
        mount_path=opts["mount_path"],
        core=opts.get("core", "agg23.SNES"),
        file_extension=opts.get("file_extension", ".sav"),
        system=opts.get("system", "snes"),
        game_aliases=dict(opts.get("game_aliases") or {}),
    ))


async def run_pocket_sync(*, source: PocketSource, config: Config,
                          ) -> PocketSyncSummary:
    """Run one full bidirectional sync pass on the mounted Pocket."""
    state = StateStore(config.state.db_path)
    cloud = RcloneCloud(remote=config.cloud.rclone_remote,
                        binary=config.cloud.rclone_binary,
                        config_path=config.cloud.rclone_config_path)
    state.upsert_source(id=source.id, system=source.system,
                        adapter="PocketSource", config_json="{}")
    sync_cfg = SyncConfig(
        cloud_to_device=config.cloud_to_device,
        conflict_winner=config.conflict_winner,
        cloud_wins_on_unknown_device=config.cloud_wins_on_unknown_device,
        cloud_wins_on_diverged_device=config.cloud_wins_on_diverged_device,
        drift_threshold=dict(config.drift_threshold))
    ctx = SyncContext(state=state, cloud=cloud, cfg=sync_cfg)
    summary = PocketSyncSummary()
    refresh_targets: dict[str, tuple[str, str, object]] = {}
    # One lease per game we sync; released in the `finally` below so
    # an exception mid-pass doesn't leave leases hanging.
    lease_tracker = LeaseTracker(source_id=source.id, cloud=cloud,
                                 cfg=config.lease)

    health = await source.health()
    if not health.ok:
        log.error("pocket health: %s", health.detail)
        state.close()
        summary.errors += 1
        return summary

    try:
        saves = await source.list_saves()
        log.info("pocket: %d save file(s) found in %s",
                 len(saves), source.saves_dir)
        seen_game_ids: set[str] = set()
        for ref in saves:
            game_id = source.resolve_game_id(ref)
            paths = compose_paths(remote=cloud.remote, system=source.system,
                                  game_id=game_id, save_filename=ref.path)
            if not lease_tracker.ensure(game_id=game_id, paths=paths):
                log.info("  %s → SKIPPED (hard-mode lease contention)",
                         Path(ref.path).name)
                summary.skipped += 1
                continue
            try:
                outcome = await sync_one_game(source=source, ref=ref, ctx=ctx)
            except CloudError as exc:
                log.warning("sync of %s failed: %s", ref.path, exc)
                summary.errors += 1
                continue
            log.info("  %s → %s", Path(ref.path).name, outcome.result.value)
            summary.add(outcome.result)
            seen_game_ids.add(outcome.game_id)
            if outcome.paths is not None and outcome.result in (
                    SyncResult.UPLOADED, SyncResult.BOOTSTRAP_UPLOADED,
                    SyncResult.DOWNLOADED, SyncResult.BOOTSTRAP_DOWNLOADED,
                    SyncResult.CONFLICT, SyncResult.CONFLICT_RESOLVED):
                refresh_targets[outcome.game_id] = (
                    outcome.game_id, outcome.save_path, outcome.paths)

        # Bootstrap-pull: cloud has games the device doesn't.
        if config.cloud_to_device:
            for game_id, paths in _cloud_games(cloud, source.system):
                if game_id in seen_game_ids:
                    continue
                if not lease_tracker.ensure(game_id=game_id, paths=paths):
                    log.info("  bootstrap %s → SKIPPED (lease contention)",
                             game_id)
                    summary.skipped += 1
                    continue
                await _bootstrap_pull(source=source, game_id=game_id,
                                      ctx=ctx, summary=summary,
                                      refresh_targets=refresh_targets)

        for game_id, save_path, paths in refresh_targets.values():
            try:
                refresh_manifest(source=source, save_path=save_path,
                                 game_id=game_id, paths=paths, ctx=ctx)
            except CloudError as exc:
                log.warning("manifest refresh failed for %s: %s",
                            save_path, exc)
    finally:
        lease_tracker.release_all()
        state.close()
    log.info("pocket sync complete: %s", summary.render())
    return summary


def _cloud_games(cloud: RcloneCloud, system: str):
    """Yield (game_id, paths) for every cloud game under <remote>/<system>/.
    Cloud paths are composed via the system-canonical extension so they
    point at the right `current.<ext>` regardless of which adapter
    originally uploaded.
    """
    base = f"{cloud.remote.rstrip('/')}/{system}"
    try:
        entries = cloud.lsjson(base)
    except CloudError:
        return
    for e in entries:
        if not e.get("IsDir"):
            continue
        game_id = e["Name"]
        yield game_id, compose_paths(
            remote=cloud.remote, system=system,
            game_id=game_id, save_filename=f"{game_id}.bin")


async def _bootstrap_pull(*, source: PocketSource, game_id: str,
                          ctx: SyncContext,
                          summary: PocketSyncSummary,
                          refresh_targets: dict) -> None:
    """For a game the device has no save for, write the cloud's current
    bytes to the right filename under Saves/<core>/.

    Uses target_save_path_for which prefers ROM-stem-derived names so
    the Pocket actually loads the resulting file. Falls back to the
    slug-based filename only if no matching ROM is in Assets/ — in
    which case the operator probably needs to drop the ROM in or
    rename the save manually.
    """
    target = source.target_save_path_for(game_id)
    if target.exists():
        # Filename collision but unread by list_saves (different ext, etc.).
        # Skip rather than overwrite.
        return
    ref = SaveRef(path=str(target))
    try:
        outcome = await sync_one_game(source=source, ref=ref, ctx=ctx)
    except CloudError as exc:
        log.warning("bootstrap-pull %s failed: %s", game_id, exc)
        summary.errors += 1
        return
    log.info("  bootstrap %s → %s", game_id, outcome.result.value)
    summary.add(outcome.result)
    if outcome.paths is not None and outcome.result in (
            SyncResult.DOWNLOADED, SyncResult.BOOTSTRAP_DOWNLOADED):
        refresh_targets[outcome.game_id] = (
            outcome.game_id, outcome.save_path, outcome.paths)


# Path of the auto-sync skip flag (mirrored from retrosync.load).
# When this file exists, the udev-fired pocket-sync exits without
# touching the device — so a manual `retrosync load <game> pocket`
# can do the mount/write/unmount itself without racing the daemon.
_SKIP_AUTO_SYNC_FLAG = Path("/run/retrosync/skip-auto-sync")


def cli_pocket_sync(*, device: str, source_id: str,
                    mount_path: str, config: Config,
                    skip_mount: bool = False) -> int:
    """Top-level entry point invoked by `retrosync pocket-sync` (and by
    the systemd unit)."""
    if _SKIP_AUTO_SYNC_FLAG.exists():
        log.info("skip-auto-sync flag at %s present; exiting without "
                 "syncing (a manual `retrosync load` is in progress)",
                 _SKIP_AUTO_SYNC_FLAG)
        return 0
    # If the operator didn't override source_id (the default is the
    # generic "pocket-1"), derive a per-physical-device id from the SD
    # card's filesystem UUID so two Pockets each get their own
    # sync_state row instead of clobbering one another.
    if source_id == "pocket-1":
        derived = derive_source_id_for_device(device=device,
                                              fallback="pocket-1")
        if derived != source_id:
            log.info("pocket: using per-device source_id %s "
                     "(derived from SD UUID of %s)", derived, device)
            source_id = derived
    summary = PocketSyncSummary()
    try:
        if not skip_mount:
            log.info("pocket detected at %s; mounting at %s",
                     device, mount_path)
            mount_pocket(device=device, mount_path=mount_path)
        source = build_pocket_source(
            source_id=source_id, mount_path=mount_path, config=config)
        summary = asyncio.run(run_pocket_sync(source=source, config=config))
    finally:
        if not skip_mount:
            log.info("unmounting %s", mount_path)
            unmount_pocket(mount_path=mount_path, device=device)
    if summary.errors > 0:
        return 1
    return 0
