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

    cloud_wins_on_unknown_device: bool = False
    """When a device with no prior sync_state shows up with bytes that
    differ from cloud's current AND don't match any known historical
    version (case 4), what do we do?

    - False (default): conflict_winner kicks in — typically `device`
      wins, the device's bytes become the new cloud current. Best when
      the device is genuinely new and you trust its data.
    - True: preserve the device's bytes as a versions/* entry (so they
      can be recovered later) but make cloud's existing current the
      winner; if cloud_to_device is on, write cloud's bytes onto the
      device. Best when the device is a reused-but-stale source — e.g.
      a Pocket whose source_id changed (per-physical-device UUID
      migration) and you'd rather inherit the cloud-side latest than
      regress to a stale local copy.
    """

    cloud_wins_on_diverged_device: bool = False
    """Like `cloud_wins_on_unknown_device` but for case 7 (both sides
    moved since the last agreed hash). Default False preserves the
    legacy `conflict_winner=device` auto-resolve, which uploads the
    device's bytes and effectively overrides cloud — fine when the
    device's edits are real, terrible when the device's "edits" are
    just stale SRAM from a power-cycle.

    True is recommended on the Pi/FXPak side: a cart's bytes that
    diverged from the last agreed hash are usually unrelated session
    artifacts (prior savestate hot-swap, a different game's autosave
    leftover, etc.) rather than a deliberate user save. Letting cloud
    win on case 7 means the cart picks up another device's deliberate
    save instead of overwriting it with cart-side noise. The device's
    bytes are still preserved as a versions/* entry for recovery via
    `retrosync promote <game> <hash>`.
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

    # 1. Already in sync (per the manifest).
    if h_dev is not None and h_dev == h_cloud:
        # Cheap drift check: the manifest's `current_hash` may have
        # drifted from the actual `current.<ext>` bytes — happens
        # when an operator does an rclone-level migration (moves
        # current.<ext> without updating the manifest), or in a
        # cross-device manifest write race. The pull path has a
        # hash-based self-heal but we'd skip silently here if we
        # trusted the manifest alone. _manifest_drifted does a single
        # lsjson and compares size + ModTime against the manifest;
        # if either looks off, force a re-pull via _pull_to_device
        # (which itself self-heals on hash mismatch).
        if manifest is not None and await _manifest_drifted(
                ctx=ctx, paths=paths,
                expected_size=manifest.current_size,
                manifest_updated_at=manifest.updated_at):
            log.warning(
                "%s on %s: cloud's current.<ext> drifted from manifest "
                "(claims hash=%s) — forcing re-pull to self-heal",
                game_id, source.id, hash8(h_cloud))
            if not ctx.cfg.cloud_to_device:
                return SyncOutcome(
                    SyncResult.SKIPPED, game_id, ref.path, paths,
                    "manifest drift but cloud_to_device disabled")
            actual = await _pull_to_device(
                source=source, ref=ref, paths=paths,
                expected_hash=h_cloud, ctx=ctx)
            ctx.state.set_sync_state(
                source_id=source.id, game_id=game_id,
                last_synced_hash=actual, device_seen_path=ref.path)
            ctx.invalidate_manifest(paths)
            return SyncOutcome(SyncResult.DOWNLOADED, game_id,
                               ref.path, paths,
                               "manifest drift self-heal")
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
        actual = await _pull_to_device(source=source, ref=ref, paths=paths,
                                       expected_hash=h_cloud, ctx=ctx)
        ctx.state.set_sync_state(
            source_id=source.id, game_id=game_id,
            last_synced_hash=actual, device_seen_path=ref.path)
        return SyncOutcome(SyncResult.BOOTSTRAP_DOWNLOADED, game_id,
                           ref.path, paths)

    # 4. Cloud and device disagree, no prior agreement.
    if h_last is None:
        # Stale-device check: if h_dev matches a hash we've already
        # uploaded for this game (any source), the device is presenting
        # a known-old version, not new content. Treat as case 6 download.
        # Also catches the drift case — bytes a few ticks off a known
        # historical version, common after per-UUID migration.
        stale_match: str | None = None
        if ctx.state.hash_in_versions_for_game(game_id, h_dev):
            stale_match = h_dev
        else:
            threshold_4 = ctx.cfg.drift_threshold.get(
                getattr(source, "device_kind", source.system), 0)
            if threshold_4 > 0:
                stale_match = await _is_drift_from_any_known(
                    ctx=ctx, manifest=manifest, data=data,
                    threshold=threshold_4, game_id=game_id,
                    source_id=source.id)
        if stale_match is not None:
            log.info(
                "%s on %s: device bytes %s ~match historical version "
                "%s — treating as stale device, pulling cloud's "
                "current %s",
                game_id, source.id, hash8(h_dev), hash8(stale_match),
                hash8(h_cloud))
            if not ctx.cfg.cloud_to_device:
                return SyncOutcome(
                    SyncResult.SKIPPED, game_id, ref.path, paths,
                    "stale device but cloud_to_device disabled")
            actual = await _pull_to_device(
                source=source, ref=ref, paths=paths,
                expected_hash=h_cloud, ctx=ctx)
            ctx.state.set_sync_state(
                source_id=source.id, game_id=game_id,
                last_synced_hash=actual, device_seen_path=ref.path)
            return SyncOutcome(SyncResult.DOWNLOADED, game_id,
                               ref.path, paths,
                               f"stale device had ~{hash8(h_dev)}")
        # Unknown-device policy: when configured, preserve the device's
        # bytes as a versions/* entry (so they're recoverable) but let
        # cloud's current win. Useful for Pockets whose source_id has
        # changed (per-UUID migration) and which now look "unknown" but
        # actually have stale data.
        if (ctx.cfg.cloud_wins_on_unknown_device
                and manifest is not None
                and len(manifest.versions) > 0):
            log.info(
                "%s on %s: cloud_wins_on_unknown_device set; preserving "
                "device bytes %s as a versions/* entry (NOT touching "
                "current.srm), then pulling cloud's current %s",
                game_id, source.id, hash8(h_dev), hash8(h_cloud))
            await _upload_version_path(
                source=source, ref=ref, data=data, h=h_dev,
                paths=paths, game_id=game_id, ctx=ctx,
                parent_hash=None, version_row=version_row,
                update_current=False,
            )
            if ctx.cfg.cloud_to_device:
                actual = await _pull_to_device(
                    source=source, ref=ref, paths=paths,
                    expected_hash=h_cloud, ctx=ctx)
                ctx.state.set_sync_state(
                    source_id=source.id, game_id=game_id,
                    last_synced_hash=actual, device_seen_path=ref.path)
                ctx.invalidate_manifest(paths)
                return SyncOutcome(SyncResult.DOWNLOADED, game_id,
                                   ref.path, paths,
                                   "device bytes preserved; cloud wins")
            # Without cloud_to_device, we preserved the device's bytes
            # in versions/ but can't write cloud's bytes back to the
            # device. Mark sync_state pointing to cloud so the next sync
            # doesn't reupload, and skip.
            ctx.state.set_sync_state(
                source_id=source.id, game_id=game_id,
                last_synced_hash=h_cloud, device_seen_path=ref.path)
            ctx.invalidate_manifest(paths)
            return SyncOutcome(SyncResult.SKIPPED, game_id, ref.path,
                               paths,
                               "device bytes preserved; cloud_to_device off")
        return await _handle_divergence(
            source=source, ref=ref, data=data, h_dev=h_dev,
            h_cloud=h_cloud, base=None, paths=paths, game_id=game_id,
            ctx=ctx, version_row=version_row,
            detail="no prior agreement between source and cloud")

    threshold = ctx.cfg.drift_threshold.get(
        getattr(source, "device_kind", source.system), 0)

    # 5. Cloud unchanged since last sync, device advanced → upload (or
    #    drift, see below).
    if h_last == h_cloud and h_dev != h_cloud:
        if threshold > 0 and await _is_drift_from_last(
                ctx=ctx, manifest=manifest, h_last=h_last,
                data=data, threshold=threshold,
                paths=paths, game_id=game_id, source_id=source.id):
            ctx.state.set_sync_state(
                source_id=source.id, game_id=game_id,
                last_synced_hash=h_dev, device_seen_path=ref.path)
            return SyncOutcome(SyncResult.IN_SYNC, game_id, ref.path,
                               paths, "drift filter (case 5)")
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
        actual = await _pull_to_device(source=source, ref=ref, paths=paths,
                                       expected_hash=h_cloud, ctx=ctx)
        ctx.state.set_sync_state(
            source_id=source.id, game_id=game_id,
            last_synced_hash=actual, device_seen_path=ref.path)
        return SyncOutcome(SyncResult.DOWNLOADED, game_id, ref.path, paths)

    # 7. Both moved since last sync. May actually be drift on the
    #    device's side (an SRAM counter ticked) plus a real cloud
    #    advance from another device — in which case treat as case 6.
    if threshold > 0 and await _is_drift_from_last(
            ctx=ctx, manifest=manifest, h_last=h_last,
            data=data, threshold=threshold,
            paths=paths, game_id=game_id, source_id=source.id):
        log.info("drift filter (case 7): %s on %s drifted from h_last "
                 "but cloud genuinely advanced — pulling cloud current",
                 game_id, source.id)
        if not ctx.cfg.cloud_to_device:
            return SyncOutcome(
                SyncResult.SKIPPED, game_id, ref.path, paths,
                "drift but cloud_to_device disabled")
        actual = await _pull_to_device(source=source, ref=ref, paths=paths,
                                       expected_hash=h_cloud, ctx=ctx)
        ctx.state.set_sync_state(
            source_id=source.id, game_id=game_id,
            last_synced_hash=actual, device_seen_path=ref.path)
        return SyncOutcome(SyncResult.DOWNLOADED, game_id, ref.path,
                           paths, "drift filter case 7 → cloud wins")

    # Diverged-device policy: preserve the device's bytes as a
    # versions/* entry (recoverable via `retrosync promote`) but let
    # cloud's current win. Mirrors `cloud_wins_on_unknown_device` for
    # case 7 — typical use case is the FXPak Pro where cart-side
    # divergence is usually session noise rather than a deliberate
    # save the operator wants to publish.
    if (ctx.cfg.cloud_wins_on_diverged_device
            and manifest is not None
            and len(manifest.versions) > 0):
        log.info(
            "%s on %s: cloud_wins_on_diverged_device set; preserving "
            "device bytes %s as a versions/* entry (NOT touching "
            "current.<ext>), then pulling cloud's current %s",
            game_id, source.id, hash8(h_dev), hash8(h_cloud))
        await _upload_version_path(
            source=source, ref=ref, data=data, h=h_dev,
            paths=paths, game_id=game_id, ctx=ctx,
            parent_hash=h_last, version_row=version_row,
            update_current=False,
        )
        if ctx.cfg.cloud_to_device:
            actual = await _pull_to_device(
                source=source, ref=ref, paths=paths,
                expected_hash=h_cloud, ctx=ctx)
            ctx.state.set_sync_state(
                source_id=source.id, game_id=game_id,
                last_synced_hash=actual, device_seen_path=ref.path)
            ctx.invalidate_manifest(paths)
            return SyncOutcome(SyncResult.DOWNLOADED, game_id,
                               ref.path, paths,
                               "device bytes preserved; cloud wins (case 7)")
        ctx.state.set_sync_state(
            source_id=source.id, game_id=game_id,
            last_synced_hash=h_cloud, device_seen_path=ref.path)
        ctx.invalidate_manifest(paths)
        return SyncOutcome(SyncResult.SKIPPED, game_id, ref.path, paths,
                           "device bytes preserved; cloud_to_device off")

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
                               version_row: VersionRow | None,
                               update_current: bool = True) -> None:
    """Upload <data> as a new versions/* entry. When `update_current` is
    True (the usual case), also overwrite cloud's `current.<ext>` with
    these bytes. Set False when you want to preserve the bytes for
    history but leave cloud's current pointing at something else (used
    by the cloud_wins_on_unknown_device policy)."""
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
        if update_current:
            await asyncio.sleep(ctx.cfg.inter_op_sleep_sec)
            ctx.cloud.overwrite_current(paths=paths, save_data=data)
        ctx.state.mark_uploaded(v_id, cloud_path=version_path)
        log.info("sync: uploaded %s for %s → %s",
                 hash8(h), game_id, version_path)
    except CloudError as exc:
        ctx.state.revert_to_ready(v_id)
        raise


async def _is_drift_from_any_known(*, ctx: SyncContext,
                                   manifest: Manifest | None,
                                   data: bytes | None, threshold: int,
                                   game_id: str, source_id: str,
                                   max_check: int = 5) -> str | None:
    """Like _is_drift_from_last, but compares against the most recent
    `max_check` versions in the manifest (any source). Returns the
    matching version's hash if a drift match is found, else None.

    Used in case 4 (no sync_state for this device) to recognize a
    "stale device whose bytes drift from a known historical upload"
    — the per-UUID-migration scenario where the device's last sync
    was attributed to an older source_id, so h_last is None but the
    bytes are basically the same as one of the older uploads.

    Cost: up to `max_check` cloud downloads. Cap is intentional —
    drift relevance drops off sharply for older versions.
    """
    if manifest is None or threshold <= 0 or data is None:
        return None
    recent = sorted(manifest.versions,
                    key=lambda v: v.uploaded_at or "",
                    reverse=True)[:max_check]
    for v in recent:
        if not v.cloud_path:
            continue
        try:
            v_bytes = ctx.cloud.download_bytes(src=v.cloud_path)
        except CloudError as exc:
            log.debug("drift scan: download of %s failed: %s",
                      v.cloud_path, exc)
            continue
        if len(v_bytes) != len(data):
            continue
        n_diff = sum(1 for a, b in zip(v_bytes, data) if a != b)
        if n_diff <= threshold:
            log.info("drift scan: %s on %s differs from historical "
                     "version %s by %d byte(s) (≤ %d)",
                     game_id, source_id, hash8(v.hash),
                     n_diff, threshold)
            return v.hash
    return None


async def _is_drift_from_last(*, ctx: SyncContext,
                              manifest: Manifest | None,
                              h_last: str | None, data: bytes | None,
                              threshold: int, paths: CloudPaths,
                              game_id: str, source_id: str) -> bool:
    """Return True iff the device's bytes are within `threshold` byte
    differences of what this source uploaded as h_last.

    Used by the drift filter in cases 5 and 7 to recognize "the device's
    SRAM counter ticked but no real save happened" — the engine
    otherwise sees a hash change and uploads, even though the user
    didn't play. Costs one cloud download per check.
    """
    if h_last is None or threshold <= 0 or data is None:
        return False
    last_path = _find_cloud_version_path(manifest, h_last)
    if last_path is None:
        log.debug("drift filter: h_last %s not in manifest for %s; "
                  "cannot compare", hash8(h_last), game_id)
        return False
    try:
        last_bytes = ctx.cloud.download_bytes(src=last_path)
    except CloudError as exc:
        log.warning("drift filter: download of %s failed: %s",
                    last_path, exc)
        return False
    if len(last_bytes) != len(data):
        return False
    n_diff = sum(1 for a, b in zip(last_bytes, data) if a != b)
    if n_diff <= threshold:
        log.info("drift filter: %s on %s differs from h_last %s by "
                 "%d byte(s) (≤ %d)",
                 game_id, source_id, hash8(h_last), n_diff, threshold)
        return True
    return False


async def _manifest_drifted(*, ctx: SyncContext, paths: CloudPaths,
                            expected_size: int | None,
                            manifest_updated_at: str) -> bool:
    """Cheap drift probe: lsjson the cloud's `current.<ext>` and
    compare against what the manifest claims.

    Mismatch = the manifest is stale (e.g. an rclone-level migration
    moved current.<ext> without updating the manifest, or a cross-
    device manifest write race). Returns True iff drift is confirmed;
    False on "matches" or "couldn't tell" (transient cloud error).
    Conservative: on any cloud failure we say "no drift" so a flaky
    network doesn't pointlessly re-pull every IN_SYNC game.

    Two checks, in order:
      1. Size — only when the manifest is schema 4+ and recorded
         `current_size`. Catches drift where the new bytes differ
         in length (most cross-system migrations).
      2. ModTime — works on any manifest. The cloud-side ModTime of
         current.<ext> is set at upload; manifest.updated_at is set
         right after (always slightly later in normal writes). A
         current.<ext> ModTime more than 60 seconds NEWER than
         manifest.updated_at means current.<ext> got rewritten
         without a matching manifest update — drift. (60s gives
         clock-skew slack; same-size drift in the user's
         GoodTools-vs-No-Intro N64 migration was the motivating case.)
    """
    try:
        entries = ctx.cloud.lsjson(paths.current)
    except CloudError as exc:
        log.debug("manifest drift check: lsjson failed (%s); skipping",
                  exc)
        return False
    if not entries:
        log.warning(
            "manifest drift check: %s missing from cloud — manifest "
            "references a current.<ext> that doesn't exist",
            paths.current)
        return False
    entry = entries[0]
    if expected_size is not None:
        actual_size = entry.get("Size")
        if isinstance(actual_size, int) and actual_size != expected_size:
            return True
    mod_time_str = entry.get("ModTime")
    if not mod_time_str or not manifest_updated_at:
        return False
    try:
        from datetime import datetime
        mod_time = datetime.fromisoformat(
            mod_time_str.replace("Z", "+00:00"))
        manifest_at = datetime.fromisoformat(
            manifest_updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    delta_sec = (mod_time - manifest_at).total_seconds()
    # >60s newer → drift. Negative or near-zero is the normal case
    # (manifest is written right after current.<ext>).
    return delta_sec > 60


async def _pull_to_device(*, source: SaveSource, ref: SaveRef,
                          paths: CloudPaths, expected_hash: str,
                          ctx: SyncContext) -> str:
    """Download cloud's current.* and write to the device.

    Also keeps state.db in step so the next poll sees the new hash as
    "current" instead of treating it as a fresh local edit.

    Returns the actual sha256 of the bytes that landed on the device.
    Usually equals `expected_hash`, but can differ if the manifest is
    out of sync with current.<ext> (a cross-source manifest write
    race — each device's refresh_manifest writes its own view of
    current_hash, and a stale view can overwrite a fresh one). In
    that case we self-heal: trust the actual bytes, write them to
    the device, and update state.db with the actual hash. The next
    refresh_manifest pass will then write a manifest whose
    current_hash matches reality, repairing the cloud-side desync.
    """
    data = ctx.cloud.download_bytes(src=paths.current)
    got = sha256_bytes(data)
    if got != expected_hash:
        log.warning(
            "manifest/current.<ext> desync for %s: manifest says %s, "
            "actual current.<ext> is %s. Trusting the bytes; manifest "
            "current_hash will be repaired by the next refresh.",
            paths.current, hash8(expected_hash), hash8(got))
    await source.write_save(ref, data)
    game_id = paths.base.rsplit("/", 1)[-1]
    ctx.state.touch_file(source_id=source.id, path=ref.path,
                         game_id=game_id)
    ctx.state.set_current_hash(source_id=source.id, path=ref.path,
                               h=got)
    log.info("sync: pulled %s for %s → device %s",
             hash8(got), game_id, ref.path)
    return got


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

    Cross-source: pulls every uploaded version for this game across all
    sources, not just `source`. Otherwise each refresh would overwrite
    the manifest with only the running source's slice and we'd lose
    other devices' version history every plug-in.
    """
    rows = list(ctx.state._conn.execute("""
        SELECT v.* FROM versions v
        JOIN files f ON v.source_id = f.source_id AND v.path = f.path
        WHERE f.game_id = ? AND v.state = 'uploaded'
          AND v.cloud_path IS NOT NULL AND v.uploaded_at IS NOT NULL
        ORDER BY v.uploaded_at
    """, (game_id,)))
    entries = [
        ManifestEntry(
            cloud_path=r["cloud_path"],
            hash=r["hash"],
            size_bytes=r["size_bytes"],
            observed_at=r["observed_at"],
            uploaded_at=r["uploaded_at"] or utc_iso(),
            retention=r["retention"],
            parent_hash=(r["parent_hash"] if "parent_hash" in r.keys()
                         else None),
            uploaded_by=r["source_id"],
        )
        for r in rows
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
    # Find the size of `current_hash` by looking it up in the versions
    # table — we want the manifest to record (hash, size) so the engine
    # can spot drift cheaply on the IN_SYNC path. None if the hash isn't
    # in the versions table for this source/path (e.g. a hash that came
    # in via a cross-device upload we haven't observed locally).
    current_size: int | None = None
    if current_hash is not None:
        row = ctx.state._conn.execute(
            "SELECT size_bytes FROM versions "
            "WHERE source_id=? AND path=? AND hash=? "
            "ORDER BY id DESC LIMIT 1",
            (source.id, save_path, current_hash)).fetchone()
        if row is not None:
            current_size = row["size_bytes"]
    manifest = build_manifest(
        source_id=source.id, system=source.system, game_id=game_id,
        save_path=save_path, save_filename=save_filename,
        current_hash=current_hash, current_size=current_size,
        versions=entries,
        device_state=device_state, conflicts=conflicts,
    )
    ctx.cloud.write_manifest(paths=paths, manifest=manifest)
    ctx.invalidate_manifest(paths)
