"""Conflict storage helpers + resolve flow.

When the sync engine detects a divergence (cloud and device both moved
since the last agreed hash, or device disagrees with cloud and there is
no prior agreement), it preserves both sides:

  - Cloud's `current.<ext>` is left untouched (still pointing at the
    cloud-side bytes).
  - The device's bytes are uploaded to `<base>/conflicts/<...>` so they
    don't overwrite anything live.
  - A row in `conflicts` (state.db) and an entry in `manifest.conflicts[]`
    record the divergence with its cloud_hash, device_hash, and base_hash.

`retrosync conflicts list / show / resolve` are this module's CLI
surfaces. Resolving picks a winner, writes it to cloud's `current.<ext>`
(uploading a new versions/* entry if needed) and clears every device's
sync_state for that game so the next sync converges.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePosixPath

from .cloud import (CloudError, CloudPaths, RcloneCloud, compose_paths,
                    hash8, sha256_bytes, utc_iso)
from .state import ConflictRow, StateStore

log = logging.getLogger(__name__)


WIN_CLOUD = "cloud"
WIN_DEVICE = "device"


@dataclass
class ResolveResult:
    conflict_id: int
    winner_hash: str
    winner_path: str   # cloud path of the bytes we promoted to current
    new_current_path: str


def list_open(state: StateStore) -> list[ConflictRow]:
    return state.list_conflicts(open_only=True)


def list_all(state: StateStore) -> list[ConflictRow]:
    return state.list_conflicts(open_only=False)


def get(state: StateStore, conflict_id: int) -> ConflictRow | None:
    return state.get_conflict(conflict_id)


def resolve(*, state: StateStore, cloud: RcloneCloud,
            conflict_id: int, winner: str,
            remote: str) -> ResolveResult:
    """Promote one side of a conflict to be the new `current`.

    `winner` is one of:
      "cloud"          — keep the cloud's existing current (just resolves the row)
      "device"         — promote the device-side conflict bytes to current
      "<full-hash>"    — promote whichever side matches this hash
    """
    row = state.get_conflict(conflict_id)
    if row is None:
        raise ValueError(f"unknown conflict id: {conflict_id}")
    if row.resolved_at is not None:
        raise ValueError(
            f"conflict {conflict_id} already resolved at {row.resolved_at}")

    if winner == WIN_CLOUD:
        winner_hash = row.cloud_hash
        winner_path = row.cloud_path
    elif winner == WIN_DEVICE:
        winner_hash = row.device_hash
        winner_path = row.conflict_path
    else:
        if winner == row.cloud_hash:
            winner_hash, winner_path = row.cloud_hash, row.cloud_path
        elif winner == row.device_hash:
            winner_hash, winner_path = row.device_hash, row.conflict_path
        else:
            raise ValueError(
                f"hash {winner[:8]} matches neither side of conflict "
                f"{conflict_id} (cloud={hash8(row.cloud_hash)}, "
                f"device={hash8(row.device_hash)})")
    if not winner_path:
        raise ValueError(
            f"conflict {conflict_id}: winner side has no preserved cloud "
            f"path; cannot promote to current")

    paths = _compose_paths_for(state, row, remote=remote)

    # Pull the winning bytes and write them as the new `current`.
    data = cloud.download_bytes(src=winner_path)
    got = sha256_bytes(data)
    if got != winner_hash:
        raise CloudError(
            f"hash mismatch downloading winner: expected "
            f"{hash8(winner_hash)}, got {hash8(got)}")
    cloud.overwrite_current(paths=paths, save_data=data)

    # Mark resolved in DB. Sync state is cleared so next sync re-syncs
    # everyone to this winner.
    state.resolve_conflict(conflict_id, winner_hash=winner_hash)
    state.clear_sync_state_for_game(row.game_id)
    log.info("resolved conflict #%d: winner=%s (%s) for %s",
             conflict_id, winner, hash8(winner_hash), row.game_id)

    return ResolveResult(
        conflict_id=conflict_id,
        winner_hash=winner_hash,
        winner_path=winner_path,
        new_current_path=paths.current,
    )


def _compose_paths_for(state: StateStore, row: ConflictRow, *,
                       remote: str) -> CloudPaths:
    """Recover CloudPaths for a conflict by deriving the save filename
    from the cloud or conflict path's extension."""
    sample = row.cloud_path or row.conflict_path or ""
    ext = (PurePosixPath(sample).suffix or ".bin")
    return compose_paths(remote=remote, system=row.system,
                         game_id=row.game_id,
                         save_filename=f"current{ext}")
