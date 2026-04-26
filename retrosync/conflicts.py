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
        candidate_paths = [row.cloud_path]
    elif winner == WIN_DEVICE:
        winner_hash = row.device_hash
        candidate_paths = [row.conflict_path]
    else:
        if winner == row.cloud_hash:
            winner_hash = row.cloud_hash
            candidate_paths = [row.cloud_path]
        elif winner == row.device_hash:
            winner_hash = row.device_hash
            candidate_paths = [row.conflict_path]
        else:
            raise ValueError(
                f"hash {winner[:8]} matches neither side of conflict "
                f"{conflict_id} (cloud={hash8(row.cloud_hash)}, "
                f"device={hash8(row.device_hash)})")

    paths = _compose_paths_for(state, row, remote=remote)

    # The path stored on the conflict row may be stale — e.g. an earlier
    # `migrate-paths` moved the cloud folder, so the row points at a
    # location that no longer exists. Try the stored path first, then fall
    # back to scanning the canonical game folder for a versions/* or
    # conflicts/* file matching the winner_hash.
    candidate_paths += list(_find_paths_by_hash(
        cloud=cloud, paths=paths, h=winner_hash))
    candidate_paths = [p for p in candidate_paths if p]
    if not candidate_paths:
        raise ValueError(
            f"conflict {conflict_id}: no cloud bytes found for "
            f"{hash8(winner_hash)}; cannot promote to current")

    data = None
    last_err: Exception | None = None
    winner_path = ""
    for cp in candidate_paths:
        try:
            data = cloud.download_bytes(src=cp)
        except CloudError as exc:
            log.info("conflict %d: candidate path %s unreachable (%s); "
                     "trying next", conflict_id, cp, exc)
            last_err = exc
            continue
        got = sha256_bytes(data)
        if got != winner_hash:
            log.info("conflict %d: candidate path %s has wrong hash "
                     "(%s vs expected %s); trying next",
                     conflict_id, cp, hash8(got), hash8(winner_hash))
            data = None
            continue
        winner_path = cp
        break
    if data is None:
        raise CloudError(
            f"could not locate winning bytes for conflict {conflict_id} "
            f"(hash {hash8(winner_hash)}). Last error: {last_err}")
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


def _find_paths_by_hash(*, cloud: RcloneCloud, paths: CloudPaths,
                        h: str) -> list[str]:
    """Scan the canonical game folder's versions/ and conflicts/ trees
    for files whose name encodes <hash8>. Returns matching cloud paths.

    Used as a fallback when the conflict row's stored cloud_path is
    stale (e.g. after `migrate-paths` moved the cloud folder out from
    under it). Filenames embed only the first 8 hex chars of the hash,
    so the caller still needs to verify the full hash by downloading.

    Recurses one level into device-kind subfolders (`versions/snes/`,
    `versions/pocket/`) introduced for at-a-glance browsing — older
    unprefixed paths still match.
    """
    h8 = hash8(h)
    out: list[str] = []
    for sub in ("versions", "conflicts"):
        root = f"{paths.base}/{sub}"
        try:
            entries = cloud.lsjson(root)
        except CloudError:
            continue
        for e in entries:
            name = e.get("Name", "")
            if e.get("IsDir"):
                # Recurse one level into device-kind subfolders.
                child_root = f"{root}/{name}"
                try:
                    children = cloud.lsjson(child_root)
                except CloudError:
                    continue
                for c in children:
                    cname = c.get("Name", "")
                    if c.get("IsDir"):
                        continue
                    if h8 in cname:
                        out.append(f"{child_root}/{cname}")
                continue
            if h8 in name:
                out.append(f"{root}/{name}")
    return out
