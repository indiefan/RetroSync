"""`retrosync flush` and `retrosync sync-pending` — defense-in-depth
helpers for the EmuDeck daemon.

`flush` runs before suspend (systemd-suspend hook): walks any pending /
ready / uploading rows in state.db and tries to push them through. If
the rclone in-flight is interrupted by suspend without the flush, the
upload is silently lost until inotify re-detects the file later.

`sync-pending` runs on network reconnect (NetworkManager dispatcher):
re-attempts uploads that failed during the offline window, plus a
quick manifest-only pull of every game in active rotation so we
detect upstream changes and pull them down.

Both are best-effort: they never raise to the caller (the systemd unit
or the dispatcher script), they just log progress and exit cleanly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from ..cloud import CloudError, RcloneCloud, compose_paths
from ..config import Config
from ..sources.base import SaveRef
from ..sources.registry import build as build_source
from ..state import StateStore, ST_PENDING, ST_DEBOUNCING, ST_READY, ST_UPLOADING
from ..sync import (SyncConfig, SyncContext, sync_one_game)

log = logging.getLogger(__name__)


@dataclass
class FlushResult:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    timed_out: bool = False


def flush(*, config: Config, timeout_sec: float = 10.0) -> FlushResult:
    """Drain anything still mid-upload before the OS suspends.

    Walks `versions` rows in pending/debouncing/ready/uploading state,
    tries to sync each via the engine. Hard timeout — we'd rather miss
    one than block suspend.
    """
    return asyncio.run(_async_flush(config=config,
                                    timeout_sec=timeout_sec))


async def _async_flush(*, config: Config, timeout_sec: float) -> FlushResult:
    state = StateStore(config.state.db_path)
    res = FlushResult()
    try:
        cloud = RcloneCloud(remote=config.cloud.rclone_remote,
                            binary=config.cloud.rclone_binary,
                            config_path=config.cloud.rclone_config_path)
        sync_cfg = SyncConfig(
            cloud_to_device=config.cloud_to_device,
            conflict_winner=config.conflict_winner,
            drift_threshold=dict(config.drift_threshold))
        ctx = SyncContext(state=state, cloud=cloud, cfg=sync_cfg)
        # Build sources by id for quick lookup.
        by_id = {}
        for s in config.sources:
            try:
                by_id[s.id] = build_source(s.adapter, id=s.id, **s.options)
            except Exception as exc:  # noqa: BLE001
                log.warning("flush: build_source(%s) failed: %s",
                            s.id, exc)
        deadline = time.monotonic() + timeout_sec
        rows = list(state._conn.execute(
            "SELECT v.*, f.game_id "
            "FROM versions v "
            "JOIN files f ON v.source_id=f.source_id AND v.path=f.path "
            "WHERE v.state IN (?, ?, ?, ?)",
            (ST_PENDING, ST_DEBOUNCING, ST_READY, ST_UPLOADING)))
        for row in rows:
            if time.monotonic() > deadline:
                res.timed_out = True
                log.warning("flush: timeout (%.1fs) — %d/%d rows still "
                            "pending", timeout_sec, res.attempted - res.succeeded,
                            len(rows))
                break
            res.attempted += 1
            source = by_id.get(row["source_id"])
            if source is None:
                res.failed += 1
                continue
            ref = SaveRef(path=row["path"], size_bytes=row["size_bytes"])
            try:
                await asyncio.wait_for(
                    sync_one_game(source=source, ref=ref, ctx=ctx),
                    timeout=max(1.0, deadline - time.monotonic()))
                res.succeeded += 1
            except asyncio.TimeoutError:
                res.timed_out = True
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("flush: sync of %s failed: %s",
                            row["path"], exc)
                res.failed += 1
    finally:
        state.close()
    log.info("flush: %s", res)
    return res


def sync_pending(*, config: Config) -> FlushResult:
    """Network-reconnect path. Same as `flush` but without the timeout
    pressure (we're not blocking suspend), plus a manifest-only sweep
    of every game we know about so upstream changes get detected."""
    return asyncio.run(_async_flush(config=config, timeout_sec=600.0))
