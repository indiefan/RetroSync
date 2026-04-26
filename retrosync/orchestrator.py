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
from .config import Config, OrchestratorConfig, SourceConfig
from .sources.base import SaveRef, SaveSource, SourceError
from .sources.registry import build as build_source
from .state import (ST_DEBOUNCING, ST_READY, StateStore, VersionRow)

log = logging.getLogger(__name__)


@dataclass
class OrchestratorDeps:
    state: StateStore
    cloud: RcloneCloud
    cfg: OrchestratorConfig


class BackupOrchestrator:
    """Owns one source's poll loop. Stop with `cancel()`."""

    def __init__(self, source: SaveSource, deps: OrchestratorDeps):
        self._source = source
        self._deps = deps
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Run forever. Designed to be wrapped in asyncio.Task."""
        log.info("orchestrator starting for source %s", self._source.id)
        await self._reconcile_on_start()
        while not self._stop.is_set():
            try:
                await self._one_pass()
            except Exception:  # noqa: BLE001
                log.exception("uncaught error in poll pass for %s", self._source.id)
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=self._deps.cfg.poll_interval_sec)
            except asyncio.TimeoutError:
                pass

    def cancel(self) -> None:
        self._stop.set()

    # ----------- the pass -----------

    async def _one_pass(self) -> None:
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

        present_paths = {ref.path for ref in refs}
        for ref in refs:
            await self._poll_one(ref)

        # Mark files that vanished from the source.
        gone = self._deps.state.tombstone_missing(self._source.id, present_paths)
        if gone:
            log.info("tombstoned %d missing file(s) for %s",
                     gone, self._source.id)

        # Promote stable pending → ready.
        self._promote_stable()

        # Drain ready queue.
        await self._drain_ready()

    async def _poll_one(self, ref: SaveRef) -> None:
        try:
            data = await self._source.read_save(ref)
        except SourceError as exc:
            log.warning("read_save failed (%s): %s", ref.path, exc)
            return

        h = sha256_bytes(data)
        size = len(data)
        prev_h = self._deps.state.get_current_hash(self._source.id, ref.path)

        # Resolve game id (cheap if cached). FXPak path uses async resolve.
        game_id = await self._resolve_game_id(ref)
        self._deps.state.touch_file(source_id=self._source.id,
                                    path=ref.path, game_id=game_id)

        if h == prev_h:
            latest = self._deps.state.latest_active_version(
                self._source.id, ref.path)
            if latest is not None and latest.hash == h:
                count = self._deps.state.bump_debounce(latest.id)
                log.debug("debounce++ for %s: %d", ref.path, count)
            return

        # Hash changed (or first sighting). Supersede any active row, insert pending.
        latest = self._deps.state.latest_active_version(
            self._source.id, ref.path)
        if latest is not None:
            self._deps.state.supersede(latest.id)
            log.info("superseded version %d for %s (was %s, now %s)",
                     latest.id, ref.path, latest.hash[:8], h[:8])

        vid = self._deps.state.insert_pending(
            source_id=self._source.id, path=ref.path,
            h=h, size_bytes=size,
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

    async def _drain_ready(self) -> None:
        for v in list(self._deps.state.ready_versions()):
            if v.source_id != self._source.id:
                continue
            await self._upload_version(v)

    async def _upload_version(self, v: VersionRow) -> None:
        # Re-read the current bytes from source so we upload the actual hash.
        # The save may have changed again since we promoted; if so, the
        # post-upload hash check will catch it and we'll re-poll.
        ref = SaveRef(path=v.path, size_bytes=v.size_bytes)
        try:
            data = await self._source.read_save(ref)
        except SourceError as exc:
            log.warning("re-read failed for upload of %s: %s", v.path, exc)
            return
        h = sha256_bytes(data)
        if h != v.hash:
            log.info("source changed under us during upload of %s "
                     "(was %s, now %s); re-pending",
                     v.path, v.hash[:8], h[:8])
            self._deps.state.supersede(v.id)
            new_id = self._deps.state.insert_pending(
                source_id=self._source.id, path=v.path,
                h=h, size_bytes=len(data),
            )
            self._deps.state.set_current_hash(
                source_id=self._source.id, path=v.path, h=h)
            self._deps.state.bump_debounce(new_id)
            return

        game_id = await self._resolve_game_id(ref)
        save_filename = v.path.rsplit("/", 1)[-1]
        paths = compose_paths(remote=self._deps.cloud.remote,
                              system=self._source.system,
                              game_id=game_id,
                              save_filename=save_filename)
        self._deps.state.mark_uploading(v.id)
        try:
            cloud_version_path = self._deps.cloud.upload_version(
                paths=paths, save_data=data,
                full_hash=h, observed_at=v.observed_at,
            )
            self._deps.cloud.overwrite_current(paths=paths, save_data=data)
            # Mark the row uploaded BEFORE writing the manifest so the
            # rebuilt manifest includes this version. If the manifest write
            # itself fails, the version is still safely recorded; the next
            # pass will rebuild the manifest from SQLite.
            self._deps.state.mark_uploaded(v.id, cloud_path=cloud_version_path)
            try:
                self._refresh_manifest(paths=paths, source_id=self._source.id,
                                       system=self._source.system,
                                       game_id=game_id, save_path=v.path)
            except CloudError as exc:
                log.warning("manifest refresh failed for %s; next pass "
                            "will retry: %s", v.path, exc)
            log.info("uploaded %s → %s (%d bytes)",
                     v.path, cloud_version_path, v.size_bytes)
        except CloudError as exc:
            log.warning("upload failed for v%d (%s); will retry next pass: %s",
                        v.id, v.path, exc)
            self._deps.state.revert_to_ready(v.id)

    def _refresh_manifest(self, *, paths, source_id, system, game_id, save_path):
        # Build manifest entries from the SQLite history of uploaded versions
        # for this file. This is the source of truth — it doesn't matter if
        # the cloud's previous manifest is missing or stale.
        rows = self._deps.state.list_versions(source_id, save_path)
        entries = [
            ManifestEntry(
                cloud_path=r.cloud_path,
                hash=r.hash,
                size_bytes=r.size_bytes,
                observed_at=r.observed_at,
                uploaded_at=r.uploaded_at or utc_iso(),
                retention=r.retention,
            )
            for r in rows
            if r.cloud_path is not None and r.uploaded_at is not None
        ]
        manifest = build_manifest(
            source_id=source_id, system=system, game_id=game_id,
            save_path=save_path,
            current_hash=self._deps.state.get_current_hash(source_id, save_path),
            versions=entries,
        )
        self._deps.cloud.write_manifest(paths=paths, manifest=manifest)

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


async def run_all(config: Config) -> None:
    state = StateStore(config.state.db_path)
    cloud = RcloneCloud(remote=config.cloud.rclone_remote,
                        binary=config.cloud.rclone_binary)
    deps = OrchestratorDeps(state=state, cloud=cloud,
                            cfg=config.orchestrator)
    sources = build_sources(config.sources)
    if not sources:
        raise SystemExit("config has no sources; nothing to do")

    # Register sources in DB so foreign keys are happy.
    for src in sources:
        state.upsert_source(id=src.id, system=src.system,
                            adapter=type(src).__name__, config_json="{}")

    orchestrators = [BackupOrchestrator(s, deps) for s in sources]
    tasks = [asyncio.create_task(o.run()) for o in orchestrators]

    try:
        await asyncio.gather(*tasks)
    finally:
        for o in orchestrators:
            o.cancel()
        state.close()
