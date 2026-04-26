"""Long-lived inotify-driven orchestrator for the EmuDeck source kind.

The polling BackupOrchestrator is great for the FXPak Pro (we have to
poll the cart anyway because there's no notify channel) but wrong for
the Deck — RetroArch's writes happen via fsync+rename, the kernel
emits an event the moment they land, and we want sub-10s latency to
cloud. So the Deck daemon runs this instead.

Lifecycle:
  1. On start: do one full pass (list_saves → sync_one_game per save)
     so anything that changed while the daemon was down catches up.
  2. Register the saves dir with InotifyWatcher.
  3. Wait forever; on each debounced event, sync_one_game for the
     matching game.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .cloud import CloudError, RcloneCloud, compose_paths
from .config import LeaseConfig
from .inotify_watch import InotifyWatcher
from .lease_tracker import LeaseTracker
from .sources.base import SaveRef
from .sources.emudeck import EmuDeckSource
from .state import StateStore
from .sync import (SyncConfig, SyncContext, SyncResult, refresh_manifest,
                   sync_one_game)

log = logging.getLogger(__name__)


# Directory write events also trigger a watch entry — we ignore those
# and only act on file-name-bearing events that match the configured
# save extension.


class InotifyOrchestrator:
    """Owns one EmuDeckSource. Runs an initial pass + an inotify loop."""

    def __init__(self, *, source: EmuDeckSource, state: StateStore,
                 cloud: RcloneCloud, sync_cfg: SyncConfig,
                 lease_cfg: LeaseConfig,
                 debounce_seconds: float = 5.0):
        self._source = source
        self._state = state
        self._cloud = cloud
        self._sync_cfg = sync_cfg
        self._debounce_seconds = debounce_seconds
        self._stop = asyncio.Event()
        self._lease_tracker = LeaseTracker(
            source_id=source.id, cloud=cloud, cfg=lease_cfg)

    async def run(self) -> None:
        """Run forever. Stop with `cancel()`."""
        log.info("inotify orchestrator starting for %s", self._source.id)
        await self._initial_pass()
        try:
            await self._inotify_loop()
        finally:
            n = self._lease_tracker.release_all()
            if n:
                log.info("released %d lease(s) on shutdown for %s",
                         n, self._source.id)

    def cancel(self) -> None:
        self._stop.set()

    def poke(self) -> None:
        """No-op — inotify is already event-driven, there's nothing to
        wake up. Defined so SIGUSR1 fan-out from daemon.py doesn't
        AttributeError when an InotifyOrchestrator is in the mix."""
        return

    async def _initial_pass(self) -> None:
        """Sync every save the Deck currently has — catches changes
        made while the daemon was down."""
        ctx = SyncContext(state=self._state, cloud=self._cloud,
                          cfg=self._sync_cfg)
        try:
            refs = await self._source.list_saves()
        except Exception as exc:  # noqa: BLE001
            log.warning("initial pass list_saves failed for %s: %s",
                        self._source.id, exc)
            return
        log.info("initial pass: %d save file(s) under %s",
                 len(refs), self._source.saves_root)
        refresh_targets: dict[str, tuple[str, str, object]] = {}
        for ref in refs:
            await self._sync_ref(ref, ctx, refresh_targets)
        for game_id, save_path, paths in refresh_targets.values():
            try:
                refresh_manifest(source=self._source, save_path=save_path,
                                 game_id=game_id, paths=paths, ctx=ctx)
            except CloudError as exc:
                log.warning("manifest refresh for %s failed: %s",
                            game_id, exc)

    async def _sync_ref(self, ref: SaveRef, ctx: SyncContext,
                        refresh_targets: dict) -> None:
        game_id = self._source.resolve_game_id(ref)
        # Cache the filename so bootstrap-pull on another device can
        # find it.
        try:
            self._source.remember_filename(
                state=self._state, game_id=game_id,
                filename=Path(ref.path).name)
        except Exception:  # noqa: BLE001
            pass
        paths = compose_paths(remote=self._cloud.remote,
                              system=self._source.system,
                              game_id=game_id, save_filename=ref.path)
        if not self._lease_tracker.ensure(game_id=game_id, paths=paths):
            log.info("skipping %s — hard-mode lease contention",
                     ref.path)
            return
        try:
            outcome = await sync_one_game(source=self._source, ref=ref,
                                          ctx=ctx)
        except CloudError as exc:
            log.warning("sync of %s failed: %s", ref.path, exc)
            return
        log.info("  %s → %s", Path(ref.path).name, outcome.result.value)
        if outcome.paths is not None and outcome.result in (
                SyncResult.UPLOADED, SyncResult.BOOTSTRAP_UPLOADED,
                SyncResult.DOWNLOADED, SyncResult.BOOTSTRAP_DOWNLOADED,
                SyncResult.CONFLICT, SyncResult.CONFLICT_RESOLVED):
            refresh_targets[outcome.game_id] = (
                outcome.game_id, outcome.save_path, outcome.paths)

    async def _inotify_loop(self) -> None:
        watcher = InotifyWatcher()
        watcher.add_path(self._source.saves_root)

        async def handler(_key: str, paths: list[Path]) -> None:
            ctx = SyncContext(state=self._state, cloud=self._cloud,
                              cfg=self._sync_cfg)
            refresh_targets: dict[str, tuple[str, str, object]] = {}
            for p in paths:
                if not p.exists():
                    continue
                ref = SaveRef(path=str(p),
                              size_bytes=p.stat().st_size)
                await self._sync_ref(ref, ctx, refresh_targets)
            for game_id, save_path, gpaths in refresh_targets.values():
                try:
                    refresh_manifest(source=self._source,
                                     save_path=save_path,
                                     game_id=game_id, paths=gpaths,
                                     ctx=ctx)
                except CloudError as exc:
                    log.warning("manifest refresh for %s failed: %s",
                                game_id, exc)

        # Filter to the configured save extension so we don't fire on
        # save states (.state*), screenshots, etc.
        ext = self._source._cfg.save_extension.lower()

        def _filter(p: Path) -> bool:
            return p.name.lower().endswith(ext)

        # Key by canonical game id so .srm + .srm.bak + .auto collapse
        # under one debounce timer per game.
        from .game_id import resolve_game_id

        def _key_for(p: Path) -> str:
            return resolve_game_id(
                p.name,
                aliases=self._source._cfg.game_aliases)

        await watcher.run(
            handler=handler, stop=self._stop,
            debounce_seconds=self._debounce_seconds,
            key_for=_key_for, filter_path=_filter)
