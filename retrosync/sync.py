"""Bidirectional sync engine.

The same engine drives both the FXPak orchestrator's per-poll path and the
Pocket trigger's per-plug-in path. Both feed it (source, save-ref, current
device bytes) and let it decide what to do — upload, download, or record
a conflict — by comparing three hashes:

  H_dev   : sha256 of the bytes currently on the device.
  H_cloud : the cloud's `current_hash` for this game.
  H_last  : the hash this device and the cloud last agreed on, from
            the per-(source, game_id) `source_sync_state` row.

See `docs/pocket-sync-design.md` §7 for the full decision matrix and the
why behind each branch.

This module is the only place that writes to `versions/` or `conflicts/`
in cloud. The FXPak orchestrator and the Pocket runner both go through
`sync_one_game()`; that's how the conflict logic and lineage tracking
stay in one place.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath

from .cloud import (
    CloudError, CloudPaths, ConflictEntry, DeviceState, ManifestEntry,
    Manifest, RcloneCloud, build_manifest, compose_paths, hash8,
    sha256_bytes, utc_iso,
)
from .sources.base import SaveRef, SaveSource, SourceError
from .state import (StateStore, VersionRow, ST_PENDING, ST_READY,
                    ST_UPLOADING)

log = logging.getLogger(__name__)


class SyncResult(str, Enum):
    IN_SYNC = "in_sync"
    UPLOADED = "uploaded"
    DOWNLOADED = "downloaded"
    BOOTSTRAP_UPLOADED = "bootstrap_uploaded"
    BOOTSTRAP_DOWNLOADED = "bootstrap_downloaded"
    CONFLICT = "conflict"             # preserved unresolved (conflict_winner=preserve)
    CONFLICT_RESOLVED = "conflict_resolved"   # auto-resolved on the spot
    SKIPPED = "skipped"
    DRIFTED = "drifted"          # source changed mid-upload; re-pended
    NO_DEVICE_DATA = "no_data"   # device has no save AND nothing to pull


# conflict_winner values
WINNER_DEVICE = "device"
WINNER_PRESERVE = "preserve"


@dataclass
class SyncOutcome:
    result: SyncResult
    game_id: str
    save_path: str
    paths: CloudPaths | None = None
    detail: str = ""


@dataclass
class SyncConfig:
    """Tunables controlling the engine's writes."""
    cloud_to_device: bool = False
    """Whether the engine may overwrite a device's save with a newer cloud copy.
    Off by default; flip on after byte-compatibility verification (§10)."""

    conflict_winner: str = WINNER_DEVICE
    """How to handle divergence between the device and cloud.

    - "device" (default): the device's bytes win automatically. They become a
      new versions/* entry and the cloud's `current.<ext>`. The previous cloud
      bytes stay in `versions/<previous-hash>.<ext>` (where they already
      lived), so nothing is destroyed. A resolved conflict row is recorded
      for forensics; recover the loser via `retrosync conflicts show <id>`.
    - "preserve": don't auto-pick. Upload the device bytes to `conflicts/`,
      leave cloud's current alone, and require an operator
      `retrosync conflicts resolve` decision. Conservative, more work.
    """

    drift_threshold: dict[str, int] = field(default_factory=dict)
    """Per-device-kind byte-count threshold for the "drift filter".

    When the engine detects a fast-forward upload (case 5: cloud unchanged
    since last sync, device advanced) AND the device's new bytes differ
    from the cloud's current by ≤ this many bytes, treat as in-sync
    instead of uploading a new version. Useful for the Analogue Pocket,
    whose openFPGA cores tick in-game counters in SRAM even when the
    operator doesn't think they're "playing" — single-byte drift
    produces a fresh cloud version on every plug-in otherwise.

    Default: empty (no filtering — every byte change uploads). Suggested
    Pocket value: 4 — covers most counter ticks; small enough to still
    catch a real save's first-byte HP/MP/inventory change.
    Example: drift_threshold = {"pocket": 4}
    """

    inter_op_sleep_sec: float = 0.5
    """Pacing between cloud writes inside one engine pass — protects rclone's
    per-minute Drive quota when bootstrapping many games at once."""


@dataclass
class SyncContext:
    state: StateStore
    cloud: RcloneCloud
    cfg: SyncConfig = field(default_factory=SyncConfig)
    # In-memory manifest cache, scoped to one engine pass. Keyed by
    # CloudPaths.base. None means "we looked and the manifest doesn't exist
    # in cloud yet" — distinguish from "cache miss".
    _manifest_cache: dict[str, Manifest | None] = field(default_factory=dict)
    _UNCACHED = object()

    def manifest_for(self, paths: CloudPaths) -> Manifest | None:
        """Read the cloud manifest. Raises CloudError on transient
        failures (rate limit, network blip) so callers can SKIP rather
        than mis-treat the absence as "no cloud version → bootstrap
        upload" and duplicate an unchanged save.
        """
        if paths.base in self._manifest_cache:
            return self._manifest_cache[paths.base]
        m = self.cloud.read_manifest(paths)
        self._manifest_cache[paths.base] = m
        return m

    def invalidate_manifest(self, paths: CloudPaths) -> None:
        self._manifest_cache.pop(paths.base, None)


# --------------------------------------------------------------------------
# Resolving game_id and composing paths
# --------------------------------------------------------------------------

async def _resolve_game_id(source: SaveSource, ref: SaveRef) -> str:
    async_resolve = getattr(source, "async_resolve_game_id", None)
    if async_resolve is not None:
        return await async_resolve(ref)
    return source.resolve_game_id(ref)


def _compose_paths(source: SaveSource, ref: SaveRef, *,
                   game_id: str, cloud: RcloneCloud) -> CloudPaths:
    save_filename = ref.path.rsplit("/", 1)[-1]
    return compose_paths(remote=cloud.remote, system=source.system,
                         game_id=game_id, save_filename=save_filename)


# --------------------------------------------------------------------------
# The decision matrix
# --------------------------------------------------------------------------

async def sync_one_game(*, source: SaveSource, ref: SaveRef,
                        ctx: SyncContext,
                        primed_data: bytes | None = None,
                        primed_hash: str | None = None,
                        version_row: VersionRow | None = None,
                        ) -> SyncOutcome:
    """Reconcile a single (source, save) with the cloud.

    `primed_data` / `primed_hash` are an optimization: the orchestrator
    already read+hashed the bytes, so it can hand them in instead of
    making us re-read. If only `primed_hash` is given (e.g. just to
    decide intent), bytes are re-read on demand.
    """
    game_id = await _resolve_game_id(source, ref)
    paths = _compose_paths(source, ref, game_id=game_id, cloud=ctx.cloud)
    try:
        manifest = ctx.manifest_for(paths)
    except CloudError as exc:
        # Transient cloud failure (rate limit, network blip). DON'T treat
        # this as "no manifest → bootstrap upload" — that's how we got
        # spurious duplicate uploads of unchanged saves. Skip this game
        # for this pass; the next poll will retry.
        log.warning("sync: skipping %s on %s — manifest read failed: %s",
                    game_id, source.id, exc)
        return SyncOutcome(SyncResult.SKIPPED, game_id, ref.path, paths,
                           f"manifest read failed: {exc}")
    h_cloud = manifest.current_hash if manifest else None
    sync_state = ctx.state.get_sync_state(source.id, game_id)
    h_last = sync_state.last_synced_hash if sync_state else None

    # Read or use primed bytes / hash. We always need a hash to run the
    # decision matrix, so if neither was primed, read now. Bytes are
    # deferred only if we already have a hash (rare — usually they come
    # together).
    h_dev = primed_hash
    data = primed_data
    if h_dev is None and primed_data is not None:
        h_dev = sha256_bytes(primed_data)
    if h_dev is None:
        try:
            data = await source.read_save(ref)
            h_dev = sha256_bytes(data)
        except (SourceError, FileNotFoundError, KeyError) as exc:
            log.debug("sync: no save bytes for %s on %s (%s)",
                      ref.path, source.id, exc)
            data = None
            h_dev = None

    async def _ensure_device_bytes() -> bytes | None:
        nonlocal data, h_dev
        if data is not None:
            return data
        try:
            data = await source.read_save(ref)
        except (SourceError, FileNotFoundError, KeyError) as exc:
            log.warning("sync: read_save failed for %s on %s: %s",
                        ref.path, source.id, exc)
            data = None
            h_dev = None
            return None
        h_dev = sha256_bytes(data)
        return data

    # --------- decision matrix per design §7.1 ---------

    # 1. Already in sync.
    if h_dev is not None and h_dev == h_cloud:
        ctx.state.set_sync_state(
            source_id=source.id, game_id=game_id,
            last_synced_hash=h_dev, device_seen_path=ref.path)
        return SyncOutcome(SyncResult.IN_SYNC, game_id, ref.path, paths)

    # 2. No cloud version yet → bootstrap upload from device.
    if h_cloud is None:
        if await _ensure_device_bytes() is None:
            return SyncOutcome(SyncResult.NO_DEVICE_DATA, game_id, ref.path,
                               paths, "no device bytes to bootstrap")
        await _upload_version_path(
            source=source, ref=ref, data=data, h=h_dev,
            paths=paths, game_id=game_id, ctx=ctx,
            parent_hash=None, version_row=version_row,
        )
        ctx.state.set_sync_state(
            source_id=source.id, game_id=game_id,
            last_synced_hash=h_dev, device_seen_path=ref.path)
        ctx.invalidate_manifest(paths)
        return SyncOutcome(SyncResult.BOOTSTRAP_UPLOADED, game_id,
                           ref.path, paths)

    # 3. Device has nothing for this game → bootstrap-pull (§7.2).
    if h_dev is None or data is None:
        if not ctx.cfg.cloud_to_device:
            return SyncOutcome(
                SyncResult.SKIPPED, game_id, ref.path, paths,
                "cloud_to_device disabled — not pulling missing save")
        await _pull_to_device(source=source, ref=ref, paths=paths,
                              expected_hash=h_cloud, ctx=ctx)
        ctx.state.set_sync_state(
            source_id=source.id, game_id=game_id,
            last_synced_hash=h_cloud, device_seen_path=ref.path)
        return SyncOutcome(SyncResult.BOOTSTRAP_DOWNLOADED, game_id,
                           ref.path, paths)

    # 4. Cloud and device disagree, no prior agreement → conflict.
    if h_last is None:
        return await _handle_divergence(
            source=source, ref=ref, data=data, h_dev=h_dev,
            h_cloud=h_cloud, base=None, paths=paths, game_id=game_id,
            ctx=ctx, version_row=version_row,
            detail="no prior agreement between source and cloud")

    # 5. Cloud unchanged since last sync, device advanced → upload.
    if h_last == h_cloud and h_dev != h_cloud:
        # Drift filter: when configured for this source's device_kind,
        # check whether the new bytes differ from cloud's current by
        # only a small number of bytes. If so, this is almost certainly
        # an in-place SRAM counter tick from the device's emulator
        # core, not a real save — silently move sync_state forward and
        # skip the upload.
        threshold = ctx.cfg.drift_threshold.get(
            getattr(source, "device_kind", source.system), 0)
        if threshold > 0:
            try:
                cloud_bytes = ctx.cloud.download_bytes(src=paths.current)
            except CloudError as exc:
                log.warning("drift filter: skipping check for %s "
                            "(download failed: %s)", game_id, exc)
                cloud_bytes = None
            if cloud_bytes is not None and len(cloud_bytes) == len(data):
                n_diff = sum(1 for a, b in zip(cloud_bytes, data)
                             if a != b)
                if n_diff <= threshold:
                    log.info("drift filter: %s on %s differs from cloud "
                             "by %d byte(s) (≤ %d) — treating as in-sync",
                             game_id, source.id, n_diff, threshold)
                    ctx.state.set_sync_state(
                        source_id=source.id, game_id=game_id,
                        last_synced_hash=h_dev, device_seen_path=ref.path)
                    return SyncOutcome(SyncResult.IN_SYNC, game_id,
                                       ref.path, paths,
                                       f"drift filter: {n_diff} byte(s)")
        await _upload_version_path(
            source=source, ref=ref, data=data, h=h_dev,
            paths=paths, game_id=game_id, ctx=ctx,
            parent_hash=h_cloud, version_row=version_row,
        )
        ctx.state.set_sync_state(
            source_id=source.id, game_id=game_id,
            last_synced_hash=h_dev, device_seen_path=ref.path)
        ctx.invalidate_manifest(paths)
        return SyncOutcome(SyncResult.UPLOADED, game_id, ref.path, paths)

    # 6. Device unchanged since last sync, cloud advanced → download.
    if h_last == h_dev and h_cloud != h_dev:
        if not ctx.cfg.cloud_to_device:
            return SyncOutcome(
                SyncResult.SKIPPED, game_id, ref.path, paths,
                "cloud_to_device disabled — not pulling cloud-newer save")
        await _pull_to_device(source=source, ref=ref, paths=paths,
                              expected_hash=h_cloud, ctx=ctx)
        ctx.state.set_sync_state(
            source_id=source.id, game_id=game_id,
            last_synced_hash=h_cloud, device_seen_path=ref.path)
        return SyncOutcome(SyncResult.DOWNLOADED, game_id, ref.path, paths)

    # 7. Both moved since last sync → conflict.
    return await _handle_divergence(
        source=source, ref=ref, data=data, h_dev=h_dev,
        h_cloud=h_cloud, base=h_last, paths=paths, game_id=game_id,
        ctx=ctx, version_row=version_row,
        detail=f"divergence from common base {hash8(h_last)}")


# --------------------------------------------------------------------------
# Action helpers — the only places that touch cloud writes.
# --------------------------------------------------------------------------

async def _upload_version_path(*, source: SaveSource, ref: SaveRef,
                               data: bytes, h: str, paths: CloudPaths,
                               game_id: str, ctx: SyncContext,
                               parent_hash: str | None,
                               version_row: VersionRow | None) -> None:
    """Upload <data> as a new versions/* entry and overwrite current."""
    # If the orchestrator already inserted a pending version row for this
    # exact hash, reuse it; otherwise create one. Either way we end up with
    # a row marked uploaded with cloud_path set.
    if version_row is not None and version_row.hash == h:
        v_id = version_row.id
    else:
        # The versions(source_id, path) FK requires a files row. The
        # orchestrator's _poll_one already touches; for callers that
        # arrive here directly (Pocket trigger, tests) we touch here too.
        ctx.state.touch_file(source_id=source.id, path=ref.path,
                             game_id=game_id)
        v_id = ctx.state.insert_pending(
            source_id=source.id, path=ref.path, h=h,
            size_bytes=len(data), parent_hash=parent_hash,
        )
        ctx.state.set_current_hash(
            source_id=source.id, path=ref.path, h=h)
        ctx.state.bump_debounce(v_id)
        ctx.state.promote_to_ready(v_id)

    ctx.state.mark_uploading(v_id)
    try:
        version_path = ctx.cloud.upload_version(
            paths=paths, save_data=data, full_hash=h,
            observed_at=utc_iso(),
            device_kind=getattr(source, "device_kind", source.system),
        )
        await asyncio.sleep(ctx.cfg.inter_op_sleep_sec)
        ctx.cloud.overwrite_current(paths=paths, save_data=data)
        ctx.state.mark_uploaded(v_id, cloud_path=version_path)
        log.info("sync: uploaded %s for %s → %s",
                 hash8(h), game_id, version_path)
    except CloudError as exc:
        ctx.state.revert_to_ready(v_id)
        raise


async def _pull_to_device(*, source: SaveSource, ref: SaveRef,
                          paths: CloudPaths, expected_hash: str,
                          ctx: SyncContext) -> None:
    """Download cloud's current.* and write to the device.

    Also keeps state.db in step so the next poll sees the new hash as
    "current" instead of treating it as a fresh local edit.
    """
    data = ctx.cloud.download_bytes(src=paths.current)
    got = sha256_bytes(data)
    if got != expected_hash:
        raise CloudError(
            f"download hash mismatch for {paths.current}: "
            f"manifest says {hash8(expected_hash)}, got {hash8(got)}")
    await source.write_save(ref, data)
    game_id = paths.base.rsplit("/", 1)[-1]
    ctx.state.touch_file(source_id=source.id, path=ref.path,
                         game_id=game_id)
    ctx.state.set_current_hash(source_id=source.id, path=ref.path,
                               h=expected_hash)
    log.info("sync: pulled %s for %s → device %s",
             hash8(expected_hash), game_id, ref.path)


async def _handle_divergence(*, source: SaveSource, ref: SaveRef,
                             data: bytes, h_dev: str, h_cloud: str,
                             base: str | None, paths: CloudPaths,
                             game_id: str, ctx: SyncContext,
                             version_row: VersionRow | None,
                             detail: str) -> SyncOutcome:
    """Branch on `conflict_winner`: either auto-resolve to the device, or
    park the device bytes in `conflicts/` for a manual decision.

    The cloud's previous bytes are *always* preserved — they're already in
    `versions/<previous-hash>.<ext>` from when they were first uploaded —
    so neither path is destructive.
    """
    if ctx.cfg.conflict_winner == WINNER_DEVICE:
        return await _auto_resolve_device_wins(
            source=source, ref=ref, data=data, h_dev=h_dev,
            h_cloud=h_cloud, base=base, paths=paths, game_id=game_id,
            ctx=ctx, version_row=version_row, detail=detail)
    # WINNER_PRESERVE — leave open, require operator decision.
    await _record_conflict(
        source=source, ref=ref, data=data, h_dev=h_dev,
        h_cloud=h_cloud, base=base, paths=paths, game_id=game_id, ctx=ctx)
    return SyncOutcome(SyncResult.CONFLICT, game_id, ref.path, paths, detail)


async def _auto_resolve_device_wins(*, source: SaveSource, ref: SaveRef,
                                    data: bytes, h_dev: str, h_cloud: str,
                                    base: str | None, paths: CloudPaths,
                                    game_id: str, ctx: SyncContext,
                                    version_row: VersionRow | None,
                                    detail: str) -> SyncOutcome:
    """Make the device's bytes the new current. Cloud's previous current
    stays in `versions/` (where it already is); a resolved conflict row
    records the divergence so the loser is discoverable later via
    `retrosync conflicts show <id>`."""
    cloud_version = _find_cloud_version_path(
        ctx.manifest_for(paths), h_cloud)
    await _upload_version_path(
        source=source, ref=ref, data=data, h=h_dev,
        paths=paths, game_id=game_id, ctx=ctx,
        parent_hash=h_cloud, version_row=version_row,
    )
    # Only update THIS source's sync state. Other devices keep their
    # pointers and will see a divergence next time they sync, which is the
    # correct "be quiet, you're behind" behavior — see pocket-sync-design
    # discussion of multi-device convergence.
    ctx.state.set_sync_state(
        source_id=source.id, game_id=game_id,
        last_synced_hash=h_dev, device_seen_path=ref.path)
    cid = ctx.state.insert_conflict(
        game_id=game_id, system=source.system, source_id=source.id,
        base_hash=base, cloud_hash=h_cloud, device_hash=h_dev,
        cloud_path=cloud_version, conflict_path=None,
    )
    ctx.state.resolve_conflict(cid, winner_hash=h_dev)
    # Close any pre-existing OPEN conflicts for this game — they represent
    # divergences from earlier syncs (likely under conflict_winner=preserve)
    # that this auto-resolve has now superseded. The bytes those rows point
    # to (in conflicts/<...>.srm) stay in cloud, so nothing's lost; only
    # the "needs operator attention" status changes.
    superseded = ctx.state.open_conflicts_for_game(game_id)
    for prior in superseded:
        if prior.id == cid:
            continue
        ctx.state.resolve_conflict(prior.id, winner_hash=h_dev)
        log.info("  closed prior OPEN conflict #%d (superseded by #%d)",
                 prior.id, cid)
    ctx.invalidate_manifest(paths)
    log.info(
        "auto-resolved divergence #%d for %s on %s: device wins (%s); "
        "previous cloud bytes (%s) preserved at %s",
        cid, game_id, source.id, hash8(h_dev), hash8(h_cloud),
        cloud_version or "(no versions/ entry — manifest pre-v2)")
    return SyncOutcome(SyncResult.CONFLICT_RESOLVED, game_id, ref.path,
                       paths, detail)


async def _record_conflict(*, source: SaveSource, ref: SaveRef,
                           data: bytes, h_dev: str, h_cloud: str,
                           base: str | None, paths: CloudPaths,
                           game_id: str, ctx: SyncContext) -> None:
    """Preserve the device's divergent bytes in cloud `conflicts/` and
    record a conflicts row. Cloud's current is left untouched."""
    # Dedupe: if there's already an open conflict for this exact device
    # hash, don't re-upload or insert another row.
    existing = ctx.state.open_conflict_for(
        game_id=game_id, source_id=source.id, device_hash=h_dev)
    if existing is not None:
        log.info("sync: conflict already on file (#%d) for %s on %s — "
                 "leaving alone", existing.id, game_id, source.id)
        return

    ext = (PurePosixPath(paths.current).suffix or ".bin")
    conflict_path = paths.conflict(
        utc_iso(), hash8(h_dev), ext, source.id,
        device_kind=getattr(source, "device_kind", source.system),
    )
    ctx.cloud.upload_bytes(data=data, dest=conflict_path)
    cloud_version = _find_cloud_version_path(ctx.manifest_for(paths), h_cloud)
    cid = ctx.state.insert_conflict(
        game_id=game_id, system=source.system, source_id=source.id,
        base_hash=base, cloud_hash=h_cloud, device_hash=h_dev,
        cloud_path=cloud_version, conflict_path=conflict_path,
    )
    log.warning(
        "CONFLICT #%d: %s diverged on %s — device %s vs cloud %s "
        "(base=%s). Device bytes preserved at %s. Use "
        "`retrosync conflicts resolve %d` to fix.",
        cid, game_id, source.id, hash8(h_dev), hash8(h_cloud),
        hash8(base) if base else "n/a", conflict_path, cid)


def _find_cloud_version_path(manifest: Manifest | None,
                             h: str) -> str | None:
    if manifest is None:
        return None
    for v in manifest.versions:
        if v.hash == h:
            return v.cloud_path
    return None


# --------------------------------------------------------------------------
# Manifest refresh — orchestrator calls this after a batch of sync ops.
# --------------------------------------------------------------------------

def refresh_manifest(*, source: SaveSource, save_path: str, game_id: str,
                     paths: CloudPaths, ctx: SyncContext) -> None:
    """Rebuild manifest.json for one game from SQLite + sync state.

    Called by orchestrators after a batch of sync_one_game calls so
    Drive's per-minute write budget isn't burned on per-version writes.
    """
    rows = ctx.state.list_versions(source.id, save_path)
    entries = [
        ManifestEntry(
            cloud_path=r.cloud_path,
            hash=r.hash,
            size_bytes=r.size_bytes,
            observed_at=r.observed_at,
            uploaded_at=r.uploaded_at or utc_iso(),
            retention=r.retention,
            parent_hash=r.parent_hash,
            uploaded_by=source.id,
        )
        for r in rows
        if r.cloud_path is not None and r.uploaded_at is not None
    ]
    save_filename = save_path.rsplit("/", 1)[-1]

    # Aggregate device_state across all sources we've ever synced this game
    # with. Avoids dropping other devices' pointers when one device writes.
    device_state: dict[str, DeviceState] = {}
    for row in ctx.state._conn.execute(
            "SELECT * FROM source_sync_state WHERE game_id=?", (game_id,)):
        device_state[row["source_id"]] = DeviceState(
            last_synced_hash=row["last_synced_hash"],
            last_synced_at=row["last_synced_at"],
        )

    conflicts_db = ctx.state._conn.execute(
        "SELECT * FROM conflicts WHERE game_id=? ORDER BY id", (game_id,)
    ).fetchall()
    conflicts: list[ConflictEntry] = []
    for c in conflicts_db:
        conflicts.append(ConflictEntry(
            id=c["id"],
            detected_at=c["detected_at"],
            base_hash=c["base_hash"],
            cloud={"hash": c["cloud_hash"], "path": c["cloud_path"]},
            device={"hash": c["device_hash"], "path": c["conflict_path"],
                    "from": c["source_id"]},
            resolved_at=c["resolved_at"],
            winner_hash=c["winner_hash"],
        ))

    current_hash = ctx.state.get_current_hash(source.id, save_path)
    manifest = build_manifest(
        source_id=source.id, system=source.system, game_id=game_id,
        save_path=save_path, save_filename=save_filename,
        current_hash=current_hash, versions=entries,
        device_state=device_state, conflicts=conflicts,
    )
    ctx.cloud.write_manifest(paths=paths, manifest=manifest)
    ctx.invalidate_manifest(paths)
