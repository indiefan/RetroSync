"""Active-device lease — per-game cloud-stored coordinator.

Solves a multi-device race the upload-only design didn't have: when
several devices can sync the same save bidirectionally, two of them
playing the same game at the same time would otherwise overwrite each
other's saves through the cloud.

A lease lives inside the per-game cloud manifest (manifest schema 3).
A device acquiring the lease declares "I'm actively playing this game"
to the rest of the fleet. Other devices respect the lease per the
configured mode:

  - soft (default): warn the operator and proceed. Device-wins
    auto-resolve still preserves the loser's bytes in versions/, so
    nothing's destroyed; the lease just makes the contention visible.
  - hard: block the contending operation until the lease is released
    or expires. For operators who want a strict "no concurrent play".

Atomic CAS isn't a built-in for cloud-stored manifests — Drive's
last-writer-wins for the manifest.json upload is what we get. In
practice that's enough: lease grabs are infrequent (one per game
launch), the contention window is small (seconds), and the lease TTL
auto-releases stale leases from crashed devices.

The lease is also self-pruning: any lease whose `expires_at` is in
the past is treated as released, so a device that crashes mid-play
doesn't lock anyone out forever. A 5-minute heartbeat from the
holder keeps the lease alive while the game is actually being played.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .cloud import (
    ActiveLease, CloudError, CloudPaths, RcloneCloud, hash8, utc_iso,
)

log = logging.getLogger(__name__)

MODE_SOFT = "soft"
MODE_HARD = "hard"


class LeaseContended(Exception):
    """Raised when an `acquire` call finds another holder under hard mode.

    The exception carries the existing lease so the caller can surface
    a useful message ("device <X> last heartbeat <ts ago>"). Soft-mode
    callers don't see this — they get a `LeaseAcquisition` with
    `contended=True` instead.
    """

    def __init__(self, lease: ActiveLease):
        super().__init__(
            f"lease held by {lease.source_id} until {lease.expires_at}")
        self.lease = lease


@dataclass
class LeaseAcquisition:
    """Outcome of an `acquire` call.

    `acquired`  : True iff our lease block now sits in the manifest.
    `contended` : True iff another non-expired lease was on the manifest
                  at the moment we tried. In soft mode, we still set
                  `acquired=True` and overwrite (with our caller free to
                  print a warning); in hard mode, `acquire` raises
                  LeaseContended instead of returning a contended
                  acquisition.
    `prior`     : The lease we displaced or stole from, if any. Useful
                  for the soft-mode warning ("…held by deck-1, last
                  heartbeat 2 min ago").
    `lease`     : Our own lease block if `acquired`, else None.
    """
    acquired: bool
    contended: bool
    prior: ActiveLease | None
    lease: ActiveLease | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str) -> datetime | None:
    """Tolerant ISO-8601 parser.

    Accepts the `…Z` suffix our writes emit (and the various edge
    cases when an old daemon writes a slightly-different format).
    Returns None if the timestamp is unparseable, which the caller
    treats the same as "stale lease, claim it" — better to grab a
    seemingly-stale lease than to refuse for a parse glitch.
    """
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts[:-1]).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def is_expired(lease: ActiveLease, *, now: datetime | None = None) -> bool:
    """A lease is expired iff `expires_at` is in the past."""
    now = now or _now()
    expires = _parse_iso(lease.expires_at)
    if expires is None:
        return True
    return expires < now


def is_held_by_other(lease: ActiveLease | None, *, source_id: str,
                     now: datetime | None = None) -> bool:
    """True iff a non-expired lease for someone other than us is present."""
    if lease is None:
        return False
    if lease.source_id == source_id:
        return False
    return not is_expired(lease, now=now)


def acquire(*, cloud: RcloneCloud, paths: CloudPaths,
            source_id: str, mode: str = MODE_SOFT,
            ttl_minutes: int = 15,
            current_hash: str | None = None) -> LeaseAcquisition:
    """Acquire (or refresh) the lease for `paths`'s game on this device.

    Reads the manifest, decides whether we can take the lease, writes
    the updated lease block back. Always preserves everything else in
    the manifest (versions, conflicts, device_state) — the manifest
    writer's read-modify-write logic handles that.

    `current_hash`, when given, is recorded as `current_hash_at_lease`
    so a later auditor can tell what state the device started from.
    """
    now = _now()
    manifest = cloud.read_manifest(paths)
    prior: ActiveLease | None = (manifest.active_lease
                                 if manifest is not None else None)
    if is_held_by_other(prior, source_id=source_id, now=now):
        if mode == MODE_HARD:
            raise LeaseContended(prior)
        # soft mode: log + steal. The caller gets `contended=True` and
        # decides whether to surface a notification.
        log.warning(
            "lease for %s contended: %s holds it until %s "
            "(last heartbeat %s) — soft mode: stealing",
            paths.base.rsplit("/", 1)[-1], prior.source_id,
            prior.expires_at, prior.last_heartbeat)

    expires = (now + timedelta(minutes=ttl_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    started_at = utc_iso()
    new_lease = ActiveLease(
        source_id=source_id,
        started_at=started_at,
        expires_at=expires,
        last_heartbeat=started_at,
        current_hash_at_lease=current_hash,
    )
    cloud.write_active_lease(paths=paths, lease=new_lease)
    return LeaseAcquisition(
        acquired=True,
        contended=is_held_by_other(prior, source_id=source_id, now=now),
        prior=prior,
        lease=new_lease,
    )


def heartbeat(*, cloud: RcloneCloud, paths: CloudPaths,
              source_id: str, ttl_minutes: int = 15) -> bool:
    """Refresh our lease's `last_heartbeat` and `expires_at`.

    Returns True if the heartbeat landed (we still hold the lease),
    False if someone else has stolen it. A False return is the caller's
    cue to either re-acquire (soft) or stop the activity (hard).
    """
    manifest = cloud.read_manifest(paths)
    held: ActiveLease | None = (manifest.active_lease
                                if manifest is not None else None)
    if held is None or held.source_id != source_id:
        log.info("heartbeat: lease for %s no longer held by us "
                 "(holder=%s); abandoning",
                 paths.base.rsplit("/", 1)[-1],
                 held.source_id if held else "(none)")
        return False
    now = _now()
    refreshed = ActiveLease(
        source_id=source_id,
        started_at=held.started_at,
        expires_at=(now + timedelta(minutes=ttl_minutes)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        last_heartbeat=utc_iso(),
        current_hash_at_lease=held.current_hash_at_lease,
    )
    cloud.write_active_lease(paths=paths, lease=refreshed)
    return True


def release(*, cloud: RcloneCloud, paths: CloudPaths,
            source_id: str, force: bool = False) -> bool:
    """Clear our lease from the manifest. Returns True if we cleared it.

    `force=True` clears the lease even if held by someone else — the
    operator escape hatch (`retrosync lease release ... --force`) when
    a crashed device left a stale lease.
    """
    manifest = cloud.read_manifest(paths)
    held: ActiveLease | None = (manifest.active_lease
                                if manifest is not None else None)
    if held is None:
        return False
    if held.source_id != source_id and not force:
        log.info("release: lease for %s held by %s, not us — leaving alone",
                 paths.base.rsplit("/", 1)[-1], held.source_id)
        return False
    cloud.write_active_lease(paths=paths, lease=None)
    log.info("released lease on %s (was held by %s)",
             paths.base.rsplit("/", 1)[-1], held.source_id)
    return True


def describe(lease: ActiveLease | None) -> str:
    """Render a lease compactly for human reading. `(none)` when null."""
    if lease is None:
        return "(none)"
    state = "expired" if is_expired(lease) else "active"
    cur = (f" current={hash8(lease.current_hash_at_lease)}"
           if lease.current_hash_at_lease else "")
    return (f"{state} holder={lease.source_id} "
            f"expires={lease.expires_at} heartbeat={lease.last_heartbeat}"
            f"{cur}")
