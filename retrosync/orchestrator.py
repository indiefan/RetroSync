"""The main poll/diff/debounce/upload loop.

One BackupOrchestrator instance per source. Multiple sources run as parallel
asyncio tasks and share a single StateStore + RcloneCloud.

Flow per poll:
  1. source.list_saves()
  2. For each save:
        bytes  = source.read_save(ref)
        h      = sha256(bytes)
        prev_h = state.get_current_hash(source_id, path)
        if h == prev_h:
            advance debounce counter on the latest active version (if any)
        else:
            insert a new ST_PENDING row; supersede any older non-uploaded rows
            for this file; update files.current_hash
  3. Promote any version whose stable_polls >= debounce_polls to READY.
  4. Drain the READY queue: for each, perform the upload sequence.

Failure model:
  - Any source error → backs off; orchestrator continues for other sources.
  - Any upload error → version reverts to READY; retried next pass.
  - Daemon kill mid-upload → reconciliation on next start re-resolves.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .cloud import (
    CloudError, ManifestEntry, RcloneCloud, build_manifest, compose_paths,
    sha256_bytes, utc_iso,
)
from .config import Config, LeaseConfig, OrchestratorConfig, SourceConfig
from .lease_tracker import LeaseTracker
from .sources.base import (SaveRef, SaveSource, SourceError,
                           default_group_refs,
                           default_read_canonical_bytes,
                           default_write_canonical_bytes)
from .sources.registry import build as build_source
from .state import (ST_DEBOUNCING, ST_READY, StateStore, VersionRow)
from .sync import (
    SyncConfig, SyncContext, SyncOutcome, SyncResult, refresh_manifest,
    sync_one_game,
)

log = logging.getLogger(__name__)


@dataclass
class OrchestratorDeps:
    state: StateStore
    cloud: RcloneCloud
    cfg: OrchestratorConfig
    sync_cfg: SyncConfig = None  # type: ignore[assignment]
    lease_cfg: LeaseConfig = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.sync_cfg is None:
            self.sync_cfg = SyncConfig()
        if self.lease_cfg is None:
            self.lease_cfg = LeaseConfig()


class BackupOrchestrator:
    """Owns one source's poll loop. Stop with `cancel()`."""

    def __init__(self, source: SaveSource, deps: OrchestratorDeps):
        self._source = source
        self._deps = deps
        self._stop = asyncio.Event()
        # Set externally (e.g. by a SIGUSR1 handler in daemon.py) to
        # interrupt the inter-pass wait and run a pass immediately.
        # Used by the udev rule that fires on FXPak Pro USB attach so
        # cart-on → first sync latency is sub-second instead of waiting
        # up to poll_interval_sec.
        self._poke = asyncio.Event()
        # Track the last health-check outcome so we can throttle the
        # "unhealthy" log (otherwise the fast-recheck path would spam
        # the journal every 2s while the cart is off).
        self._last_health_ok: bool | None = None
        # Per-source lease holder. Acquires per-game leases when the
        # source becomes healthy (proxy for "console just turned on"),
        # heartbeats them while it stays healthy, releases them on the
        # health → unhealthy transition (proxy for "console powered off").
        self._lease_tracker = LeaseTracker(
            source_id=source.id, cloud=deps.cloud, cfg=deps.lease_cfg)

    def poke(self) -> None:
        """Signal the orchestrator to interrupt its current wait and run
        a pass immediately. Safe from any thread (asyncio.Event.set is
        thread-safe in CPython)."""
        self._poke.set()

    async def run(self) -> None:
        """Run forever. Designed to be wrapped in asyncio.Task."""
        log.info("orchestrator starting for source %s", self._source.id)
        await self._reconcile_on_start()
        while not self._stop.is_set():
            try:
                healthy = await self._one_pass_returning_health()
            except Exception:  # noqa: BLE001
                log.exception("uncaught error in poll pass for %s",
                              self._source.id)
                healthy = False
            # Pick the wait duration based on current health: full
            # poll interval when the source is healthy (every 30s by
            # default), shorter recheck when it's not (so cart-on is
            # detected quickly).
            wait = (self._deps.cfg.poll_interval_sec if healthy
                    else self._deps.cfg.unhealthy_recheck_sec)
            await self._wait_or_poke(wait)

    async def _wait_or_poke(self, timeout: float) -> None:
        """Wait up to `timeout` seconds, returning early if `_stop` or
        `_poke` is set. Clears `_poke` on return."""
        wait_stop = asyncio.create_task(self._stop.wait())
        wait_poke = asyncio.create_task(self._poke.wait())
        try:
            done, _ = await asyncio.wait(
                {wait_stop, wait_poke},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (wait_stop, wait_poke):
                if not t.done():
                    t.cancel()
        if self._poke.is_set():
            log.info("source %s: poke received → immediate pass",
                     self._source.id)
            self._poke.clear()

    def cancel(self) -> None:
        self._stop.set()

    # ----------- the pass -----------

    async def _one_pass_returning_health(self) -> bool:
        """Run one pass. Return whether the source was healthy at the
        start (drives the wait-time choice in the run loop)."""
        health = await self._source.health()
        if not health.ok:
            # Throttle the unhealthy log: only emit on the first
            # transition, debug-level afterwards. Fast-recheck mode
            # would otherwise spam the journal every 2s.
            if self._last_health_ok is not False:
                log.info("source %s unhealthy: %s",
                         self._source.id, health.detail)
                # Health → unhealthy transition: release any leases
                # we were holding so other devices don't have to wait
                # out the TTL after a cart-detach.
                if self._lease_tracker.held_game_ids():
                    n = self._lease_tracker.release_all()
                    log.info("released %d lease(s) on cart-detach for %s",
                             n, self._source.id)
            else:
                log.debug("source %s still unhealthy: %s",
                          self._source.id, health.detail)
            self._last_health_ok = False
            return False
        if self._last_health_ok is False:
            log.info("source %s came back healthy: %s",
                     self._source.id, health.detail)
        self._last_health_ok = True
        await self._one_pass(health=health)
        return True

    async def _one_pass(self, *, health=None) -> None:
        if health is None:
            health = await self._source.health()
            if not health.ok:
                log.info("source %s unhealthy: %s",
                         self._source.id, health.detail)
                return

        try:
            refs = await self._source.list_saves()
        except SourceError as exc:
            log.warning("list_saves failed for %s: %s", self._source.id, exc)
            return

        ctx = SyncContext(state=self._deps.state, cloud=self._deps.cloud,
                          cfg=self._deps.sync_cfg)
        refresh_targets: dict[str, tuple[str, str, object]] = {}

        present_paths = {ref.path for ref in refs}
        # Group refs by source-defined key. For single-file sources the
        # default groups one-per-ref (preserving existing behavior).
        # Multi-file sources (EverDrive 64) override `group_refs` to
        # group per-format files by game_id so the engine sees one
        # logical save per group.
        grouper = getattr(self._source, "group_refs", None)
        groups = (grouper(refs) if grouper is not None
                  else default_group_refs(self._source, refs))
        # Path-to-group map so _drain_ready can re-read the full group
        # for a stuck-promoted version row (whose path is just the
        # group's first ref).
        path_to_group: dict[str, list[SaveRef]] = {}
        for group_refs in groups.values():
            for ref in group_refs:
                path_to_group[ref.path] = group_refs
        for group_key, group_refs in groups.items():
            await self._poll_one_group(group_key, group_refs, ctx,
                                       refresh_targets)

        # Mark files that vanished from the source.
        gone = self._deps.state.tombstone_missing(self._source.id, present_paths)
        if gone:
            log.info("tombstoned %d missing file(s) for %s",
                     gone, self._source.id)

        # Promote stable pending → ready.
        self._promote_stable()

        # Drain ready queue.
        await self._drain_ready(ctx, refresh_targets, path_to_group)

        # Final manifest refresh per affected game — once per pass.
        for game_id, save_path, paths in refresh_targets.values():
            try:
                refresh_manifest(source=self._source, save_path=save_path,
                                 game_id=game_id, paths=paths, ctx=ctx)
            except CloudError as exc:
                log.warning("manifest refresh failed for %s; next pass "
                            "will retry: %s", save_path, exc)

    async def _poll_one_group(self, group_key: str,
                              group_refs: list[SaveRef],
                              ctx: SyncContext,
                              refresh_targets: dict[str, tuple[str, str, object]]
                              ) -> None:
        """Process one logical save (one or more device-side files).

        Group_key is opaque (the source's `group_refs` key, default
        ref.path). The first ref of the group is the conventional
        state.db tracking key — for single-file sources that's the
        only ref; for multi-file sources it's whichever per-format
        file came first when the source listed them.
        """
        if not group_refs:
            return
        ref = group_refs[0]
        try:
            reader = getattr(self._source, "read_canonical_bytes", None)
            if reader is not None:
                data = await reader(group_refs)
            else:
                data = await default_read_canonical_bytes(self._source, group_refs)
        except SourceError as exc:
            log.warning("read_canonical_bytes failed (%s): %s", ref.path, exc)
            return

        h = sha256_bytes(data)
        size = len(data)
        prev_h = self._deps.state.get_current_hash(self._source.id, ref.path)

        # Resolve game id (cheap if cached). FXPak path uses async resolve.
        game_id = await self._resolve_game_id(ref)
        self._deps.state.touch_file(source_id=self._source.id,
                                    path=ref.path, game_id=game_id)

        # Ensure we hold the lease for this game (acquire on first sight,
        # heartbeat on subsequent passes). In hard mode with contention
        # this returns False and we skip the per-game work — sync would
        # otherwise potentially overwrite another device's in-flight save.
        paths = compose_paths(remote=self._deps.cloud.remote,
                              system=self._source.system,
                              game_id=game_id, save_filename=ref.path)
        if not self._lease_tracker.ensure(
                game_id=game_id, paths=paths, current_hash=h):
            log.info("hard-mode lease contention for %s on %s; skipping "
                     "this game until the holder releases",
                     game_id, self._source.id)
            return

        if h == prev_h:
            latest = self._deps.state.latest_active_version(
                self._source.id, ref.path)
            if latest is not None and latest.hash == h:
                count = self._deps.state.bump_debounce(latest.id)
                log.debug("debounce++ for %s: %d", ref.path, count)
            elif self._deps.sync_cfg.cloud_to_device:
                # Local save is steady. Run the sync engine to detect cloud-
                # newer copies and pull them down. Engine returns IN_SYNC
                # cheaply when nothing's changed, courtesy of the per-pass
                # manifest cache.
                outcome = await sync_one_game(
                    source=self._source, ref=ref, ctx=ctx,
                    primed_data=data, primed_hash=h,
                )
                self._record_refresh(outcome, refresh_targets)
            return

        # Hash changed (or first sighting). Supersede any active row, insert pending.
        latest = self._deps.state.latest_active_version(
            self._source.id, ref.path)
        if latest is not None:
            self._deps.state.supersede(latest.id)
            log.info("superseded version %d for %s (was %s, now %s)",
                     latest.id, ref.path, latest.hash[:8], h[:8])

        # parent_hash is whatever the device-side hash was the last time we
        # saw it (or None on first sighting). Used by the sync engine to
        # reason about lineage during conflict detection.
        vid = self._deps.state.insert_pending(
            source_id=self._source.id, path=ref.path,
            h=h, size_bytes=size, parent_hash=prev_h,
        )
        self._deps.state.set_current_hash(
            source_id=self._source.id, path=ref.path, h=h)
        log.info("new pending version %d for %s (hash=%s, %d bytes)",
                 vid, ref.path, h[:8], size)

        # First sighting counts as one stable poll.
        self._deps.state.bump_debounce(vid)

    def _promote_stable(self) -> None:
        threshold = self._deps.cfg.debounce_polls
        # We don't have a ready-made query for "stable but not promoted";
        # we'll promote anything whose stable_polls >= threshold and state
        # is still pending or debouncing. Done via raw SQL for clarity.
        rows = self._deps.state._conn.execute(
            "SELECT id FROM versions "
            "WHERE state IN ('pending','debouncing') AND stable_polls >= ?",
            (threshold,),
        ).fetchall()
        for r in rows:
            self._deps.state.promote_to_ready(r["id"])
            log.info("promoted version %d to READY", r["id"])

    async def _drain_ready(self, ctx: SyncContext,
                           refresh_targets: dict[str, tuple[str, str, object]],
                           path_to_group: dict[str, list[SaveRef]] | None = None,
                           ) -> None:
        # Funnel each ready version through the shared sync engine. The
        # per-game manifest refresh is batched at the pass level so Drive's
        # per-minute write quota isn't burned on per-upload writes.
        ready = [v for v in self._deps.state.ready_versions()
                 if v.source_id == self._source.id]
        for i, v in enumerate(ready):
            outcome = await self._sync_promoted(v, ctx,
                                                path_to_group=path_to_group)
            self._record_refresh(outcome, refresh_targets)
            # Inter-upload pacing — small sleep between rclone invocations
            # to spread API calls across the per-minute rate-limit window.
            if i + 1 < len(ready):
                await asyncio.sleep(self._deps.sync_cfg.inter_op_sleep_sec)

    def _record_refresh(self, outcome: SyncOutcome | None,
                        refresh_targets: dict[str, tuple[str, str, object]]
                        ) -> None:
        if outcome is None or outcome.paths is None:
            return
        if outcome.result not in (SyncResult.UPLOADED,
                                  SyncResult.BOOTSTRAP_UPLOADED,
                                  SyncResult.DOWNLOADED,
                                  SyncResult.BOOTSTRAP_DOWNLOADED,
                                  SyncResult.CONFLICT,
                                  SyncResult.CONFLICT_RESOLVED):
            return
        key = f"{outcome.game_id}::{outcome.save_path}"
        refresh_targets[key] = (outcome.game_id, outcome.save_path,
                                outcome.paths)

    async def _sync_promoted(self, v: VersionRow,
                             ctx: SyncContext,
                             path_to_group: dict[str, list[SaveRef]] | None = None,
                             ) -> SyncOutcome | None:
        """Re-read the device, drift-check, hand off to the sync engine.

        For multi-file sources, re-reads all per-format files in the
        version row's group via `read_canonical_bytes`. For single-file
        sources, re-reads the one file. Either way, `data` is the
        canonical byte representation that gets hashed.
        """
        ref = SaveRef(path=v.path, size_bytes=v.size_bytes)
        # Find the group this version's path belongs to (multi-file
        # sources). If no group is provided (e.g. tests), fall back to
        # treating the version's path as its own one-element group.
        group_refs = (path_to_group or {}).get(v.path, [ref])
        try:
            reader = getattr(self._source, "read_canonical_bytes", None)
            if reader is not None:
                data = await reader(group_refs)
            else:
                data = await default_read_canonical_bytes(self._source,
                                                          group_refs)
        except SourceError as exc:
            log.warning("re-read failed for upload of %s: %s", v.path, exc)
            return None
        h = sha256_bytes(data)
        if h != v.hash:
            log.info("source changed under us during upload of %s "
                     "(was %s, now %s); re-pending",
                     v.path, v.hash[:8], h[:8])
            self._deps.state.supersede(v.id)
            new_id = self._deps.state.insert_pending(
                source_id=self._source.id, path=v.path,
                h=h, size_bytes=len(data),
                parent_hash=v.hash,
            )
            self._deps.state.set_current_hash(
                source_id=self._source.id, path=v.path, h=h)
            self._deps.state.bump_debounce(new_id)
            return SyncOutcome(SyncResult.DRIFTED, "", v.path, None,
                               "source drifted; re-pended")

        try:
            return await sync_one_game(
                source=self._source, ref=ref, ctx=ctx,
                primed_data=data, primed_hash=h, version_row=v,
            )
        except CloudError as exc:
            log.warning("sync failed for v%d (%s); will retry next pass: %s",
                        v.id, v.path, exc)
            return None

    async def _resolve_game_id(self, ref: SaveRef) -> str:
        """Use the source's async resolver if it exposes one (duck-typed)."""
        async_resolve = getattr(self._source, "async_resolve_game_id", None)
        if async_resolve is not None:
            return await async_resolve(ref)
        return self._source.resolve_game_id(ref)

    # ----------- startup reconciliation -----------

    async def _reconcile_on_start(self) -> None:
        """Anything left in `uploading` from a crashed run reverts to ready."""
        stuck = list(self._deps.state.stuck_uploading())
        for v in stuck:
            if v.source_id == self._source.id:
                self._deps.state.revert_to_ready(v.id)
                log.info("reconcile: reverted stuck upload v%d (%s)",
                         v.id, v.path)


# --------------------------------------------------------------------------
# Entry point: build orchestrators from config and run them all forever.
# --------------------------------------------------------------------------

def build_sources(sources: list[SourceConfig]) -> list[SaveSource]:
    out: list[SaveSource] = []
    for s in sources:
        out.append(build_source(s.adapter, id=s.id, **s.options))
    return out


async def run_all(config: Config, *,
                  on_started=None) -> None:
    """Build orchestrators from `config` and run them all forever.

    `on_started`, when given, is called with the list of orchestrators
    once they're constructed. Lets the daemon register signal handlers
    that need to reach into the orchestrators (e.g. SIGUSR1 → poke
    every orchestrator to run an immediate pass).

    Source kind dispatch:
      - EmuDeckSource → InotifyOrchestrator (event-driven push)
      - everything else → BackupOrchestrator (polling)

    Pocket sources are NOT instantiated by the daemon — they're
    udev-fired one-shots via `retrosync pocket-sync`.
    """
    # Lazy imports so the polling-only Pi daemon doesn't pay for the
    # inotify wrapper's ctypes init at import time.
    from .inotify_orchestrator import InotifyOrchestrator
    from .sources.emudeck import EmuDeckSource

    state = StateStore(config.state.db_path)
    cloud = RcloneCloud(remote=config.cloud.rclone_remote,
                        binary=config.cloud.rclone_binary,
                        config_path=config.cloud.rclone_config_path)
    sync_cfg = SyncConfig(
        cloud_to_device=config.cloud_to_device,
        conflict_winner=config.conflict_winner,
        cloud_wins_on_unknown_device=config.cloud_wins_on_unknown_device,
        cloud_wins_on_diverged_device=config.cloud_wins_on_diverged_device,
        drift_threshold=dict(config.drift_threshold))
    deps = OrchestratorDeps(state=state, cloud=cloud,
                            cfg=config.orchestrator,
                            sync_cfg=sync_cfg,
                            lease_cfg=config.lease)
    sources = []
    for s in config.sources:
        # Pocket sources are wired up via udev — daemon doesn't
        # instantiate them.
        if s.adapter == "pocket":
            log.info("daemon: skipping pocket source %s "
                     "(udev-fired one-shot)", s.id)
            continue
        sources.append(build_source(s.adapter, id=s.id, **s.options))
    if not sources:
        raise SystemExit("config has no daemon-managed sources; "
                         "nothing to do")

    # Register sources in DB so foreign keys are happy.
    for src in sources:
        state.upsert_source(id=src.id, system=src.system,
                            adapter=type(src).__name__, config_json="{}")

    orchestrators: list = []
    for src in sources:
        if isinstance(src, EmuDeckSource):
            orchestrators.append(InotifyOrchestrator(
                source=src, state=state, cloud=cloud,
                sync_cfg=sync_cfg, lease_cfg=config.lease,
                periodic_rescan_seconds=config.orchestrator.inotify_rescan_sec))
        else:
            orchestrators.append(BackupOrchestrator(src, deps))
    if on_started is not None:
        on_started(orchestrators)
    tasks = [asyncio.create_task(o.run()) for o in orchestrators]

    try:
        await asyncio.gather(*tasks)
    finally:
        for o in orchestrators:
            o.cancel()
        state.close()
