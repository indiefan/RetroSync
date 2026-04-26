"""Force-promote any historical version to be the new cloud current.

Use case: an unwanted save became cloud's `current` (e.g. a stale
device's empty SRAM overwrote a real save before you noticed) and
you want to revert to a specific prior version. Without `promote`,
the recovery is the multi-step "stop daemon → pull bytes by hand →
write to device → start daemon" dance from the cookbook in
docs/pocket-sync-design.md §16.17.

Mechanism (kept identical in spirit to conflicts.resolve so the two
share the same safety properties):

  1. Resolve the selector (hash or cloud path) to a cloud path.
  2. Download the bytes, verify the hash matches.
  3. Overwrite `current.<ext>` with those bytes.
  4. Update the manifest so `current_hash` reflects the new bytes.

We deliberately leave `source_sync_state.last_synced_hash` alone for
each device. After promote, the typical "device was in sync with the
prior current" case becomes:
    h_dev == h_last == prior_current  != h_cloud (now promoted)
which is case 6 (cloud advanced; device unchanged → download). That's
what we want — every keeping-up device pulls the promoted bytes on
next sync, no extra plumbing needed. Bumping `last_synced_hash` to
the promoted hash would actually MIS-trigger case 5 (fast-forward
upload of the device's stale prior_current), undoing the promote.

A device with unsynced local edits triggers a divergence on next
sync — handled per `conflict_winner`. Default `device` lets those
edits win, which preserves the operator's in-flight work; if you
want promote to stick even against in-flight edits, set
`conflict_winner: preserve` first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .cloud import (CloudError, CloudPaths, RcloneCloud, compose_paths,
                    hash8, sha256_bytes, utc_iso)
from .state import StateStore

log = logging.getLogger(__name__)


@dataclass
class PromoteResult:
    game_id: str
    promoted_hash: str
    promoted_path: str          # cloud path of the bytes we promoted
    new_current_path: str       # base/current.<ext>


def promote(*, state: StateStore, cloud: RcloneCloud,
            game_id: str, selector: str,
            system: str = "snes") -> PromoteResult:
    """Promote a historical version to be cloud's new `current.<ext>`.

    `selector` is one of:
      - A full sha256 hash (preferred — unambiguous).
      - A hash8 prefix (the first 8 hex chars; matches what
        `retrosync versions` prints).
      - A cloud path (e.g. `gdrive:retro-saves/snes/game/versions/...`).
        The path's bytes are downloaded and the hash is recorded as
        the new current_hash; no extra validation.
    """
    paths = compose_paths(remote=cloud.remote, system=system,
                          game_id=game_id,
                          save_filename=f"{game_id}.bin")

    chosen_path = _find_version(
        state=state, cloud=cloud, paths=paths,
        game_id=game_id, selector=selector)
    if chosen_path is None:
        raise ValueError(
            f"no version matching {selector!r} found for {game_id} "
            f"(searched state.db and {paths.base}/versions/). "
            f"Run `retrosync versions {game_id}` to see what's known.")
    log.info("promote: matched %s for %s", chosen_path, game_id)

    data = cloud.download_bytes(src=chosen_path)
    h = sha256_bytes(data)
    if not _selector_matches(selector, h):
        raise CloudError(
            f"downloaded bytes from {chosen_path} hash to {hash8(h)} "
            f"which doesn't match selector {selector!r}; refusing to "
            f"promote (the file may have been replaced under us).")

    # Overwrite cloud current.<ext> with the chosen bytes.
    cloud.overwrite_current(paths=paths, save_data=data)

    # Patch the manifest's current_hash so daemons reading it on the
    # next pass see the new state.
    manifest = cloud.read_manifest(paths)
    if manifest is not None:
        manifest.current_hash = h
        manifest.updated_at = utc_iso()
        # preserve_lease=True is a belt-and-suspenders against a lease
        # acquire racing our write — the writer reads the lease again
        # and merges it in.
        cloud.write_manifest(paths=paths, manifest=manifest,
                             preserve_lease=True)

    return PromoteResult(
        game_id=game_id, promoted_hash=h,
        promoted_path=chosen_path,
        new_current_path=paths.current,
    )


def _find_version(*, state: StateStore, cloud: RcloneCloud,
                  paths: CloudPaths, game_id: str,
                  selector: str) -> str | None:
    """Resolve selector → cloud path. Tries state.db, then scans cloud.

    Cloud-scan covers cross-device cases: e.g. running promote on the
    Pi for a version that the Deck uploaded — Deck's state.db has
    the row, the Pi's doesn't, but the file is in cloud either way.
    """
    if "/" in selector or selector.startswith(cloud.remote.rstrip(":") + ":"):
        return selector if cloud.exists(selector) else None

    sel_lower = selector.lower()
    for row in state._conn.execute("""
        SELECT v.* FROM versions v
        JOIN files f ON v.source_id = f.source_id AND v.path = f.path
        WHERE f.game_id = ? AND v.state = 'uploaded'
          AND v.cloud_path IS NOT NULL
        ORDER BY v.uploaded_at DESC
    """, (game_id,)):
        h = (row["hash"] or "").lower()
        if h == sel_lower or h.startswith(sel_lower):
            return row["cloud_path"]

    # Cloud scan fallback. Walks versions/ one level deep (covers the
    # device-kind subfolder layout introduced for at-a-glance browsing).
    root = f"{paths.base}/versions"
    try:
        entries = cloud.lsjson(root)
    except CloudError:
        return None
    for e in entries:
        name = e.get("Name", "")
        if e.get("IsDir"):
            child_root = f"{root}/{name}"
            try:
                children = cloud.lsjson(child_root)
            except CloudError:
                continue
            for c in children:
                cname = c.get("Name", "")
                if not c.get("IsDir") and sel_lower in cname.lower():
                    return f"{child_root}/{cname}"
        else:
            if sel_lower in name.lower():
                return f"{root}/{name}"
    return None


def _selector_matches(selector: str, full_hash: str) -> bool:
    """Refuse to promote bytes whose hash doesn't match a hash-shaped
    selector. Path selectors are trusted as-is."""
    if "/" in selector or ":" in selector:
        return True
    sel_lower = selector.lower()
    h_lower = full_hash.lower()
    return h_lower == sel_lower or h_lower.startswith(sel_lower)


