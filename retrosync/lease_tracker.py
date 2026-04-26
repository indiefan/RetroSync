"""Per-source lease bookkeeping.

The orchestrators and per-shot runners use this to:

  - Acquire a lease the first time they see a game in a session (cart
    attach, Pocket plug-in, Deck pre-launch, etc).
  - Heartbeat existing leases on a timer so they don't expire while
    the activity is still live.
  - Release everything when the session ends (cart detach, sync run
    finishes, wrap exits).

The actual lease writes live in `leases.py`; this module is just the
"which leases am I currently holding, and when did I last refresh
each" state machine.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import leases as leases_mod
from .cloud import ActiveLease, CloudError, CloudPaths, RcloneCloud
from .config import LeaseConfig

log = logging.getLogger(__name__)


@dataclass
class _Held:
    paths: CloudPaths
    last_heartbeat_at: datetime
    contended: bool = False
    prior_holder: str | None = None


@dataclass
class LeaseTracker:
    """Holds zero or more leases for one source. Cheap to construct;
    the cloud writes only happen on `ensure` and `release_all`."""
    source_id: str
    cloud: RcloneCloud
    cfg: LeaseConfig = field(default_factory=LeaseConfig)
    _held: dict[str, _Held] = field(default_factory=dict)

    def ensure(self, *, game_id: str, paths: CloudPaths,
               current_hash: str | None = None) -> bool:
        """Acquire (or heartbeat) the lease for `game_id`.

        Returns True iff this device now holds the lease (or did so
        after the call). In hard mode with contention, returns False
        and the caller should skip the operation it was about to do.
        """
        existing = self._held.get(game_id)
        if existing is not None:
            # Heartbeat if we're past the configured heartbeat interval.
            now = datetime.now(timezone.utc)
            elapsed = (now - existing.last_heartbeat_at).total_seconds()
            if elapsed < self.cfg.heartbeat_minutes * 60:
                return True
            try:
                ok = leases_mod.heartbeat(
                    cloud=self.cloud, paths=paths,
                    source_id=self.source_id,
                    ttl_minutes=self.cfg.ttl_minutes)
            except CloudError as exc:
                log.warning("lease heartbeat for %s failed: %s", game_id, exc)
                return True  # keep believing we hold it; next pass retries
            if ok:
                existing.last_heartbeat_at = now
                return True
            # Someone stole it — drop our local record and try a fresh
            # acquire below.
            self._held.pop(game_id, None)
        try:
            outcome = leases_mod.acquire(
                cloud=self.cloud, paths=paths,
                source_id=self.source_id, mode=self.cfg.mode,
                ttl_minutes=self.cfg.ttl_minutes,
                current_hash=current_hash)
        except leases_mod.LeaseContended as exc:
            log.warning(
                "hard-mode lease contention for %s on %s: held by %s "
                "(expires %s); skipping",
                self.source_id, game_id, exc.lease.source_id,
                exc.lease.expires_at)
            return False
        except CloudError as exc:
            log.warning("lease acquire for %s failed: %s; proceeding "
                        "without lease", game_id, exc)
            return True
        self._held[game_id] = _Held(
            paths=paths,
            last_heartbeat_at=datetime.now(timezone.utc),
            contended=outcome.contended,
            prior_holder=outcome.prior.source_id if outcome.prior else None,
        )
        if outcome.contended and outcome.prior is not None:
            log.warning(
                "soft-mode lease for %s on %s: previously held by %s "
                "(last heartbeat %s) — taking over",
                self.source_id, game_id, outcome.prior.source_id,
                outcome.prior.last_heartbeat)
        return True

    def release_all(self) -> int:
        """Release every lease this tracker is holding. Returns count."""
        n = 0
        for game_id, held in list(self._held.items()):
            try:
                if leases_mod.release(
                        cloud=self.cloud, paths=held.paths,
                        source_id=self.source_id):
                    n += 1
            except CloudError as exc:
                log.warning("lease release for %s failed: %s "
                            "(will expire on its own at TTL)",
                            game_id, exc)
            self._held.pop(game_id, None)
        return n

    def held_game_ids(self) -> list[str]:
        return list(self._held.keys())
