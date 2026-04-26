"""One-shot cloud-tree migration: collapse legacy `unknown_*` and
`<crc32>_*` game-id folders into the new canonical `<slug>` layout.

Pre-Pocket-Sync, FXPak Pro game IDs were `<crc32>_<slug>` (when the
partner ROM could be read) or `unknown_<slug>` (when it couldn't). The
new layout is just `<slug>`, derived from the save filename.

This module:
  1. Lists `<remote>/<system>/`.
  2. For each game-id folder, computes its canonical slug.
  3. If the canonical folder doesn't exist, renames in-place via
     `rclone moveto`.
  4. If it does exist, merges by moving each version file across; the
     receiving folder's manifest is rebuilt by the next sync pass.
  5. Updates `files.game_id` rows in state.db so the daemon's pointer
     stays consistent with what's now in cloud.

Idempotent: re-running on an already-migrated tree is a no-op.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass

from .cloud import CloudError, RcloneCloud
from .game_id import canonical_slug
from .state import StateStore

log = logging.getLogger(__name__)


# Strip a leading hex CRC + underscore (8 chars by convention but accept
# any 6-10) or a leading "unknown_". After stripping, the result is fed
# back through canonical_slug for normalization.
_PREFIX_RE = re.compile(r"^(?:unknown|[0-9a-f]{6,10})_", re.IGNORECASE)


def derive_canonical_id(legacy: str) -> str:
    """Strip a legacy CRC/`unknown_` prefix and re-normalize."""
    stripped = _PREFIX_RE.sub("", legacy, count=1)
    return canonical_slug(stripped)


@dataclass
class MigrationPlan:
    legacy_path: str        # cloud path of the old folder
    legacy_id: str          # name of the old folder
    canonical_id: str       # what it should be called
    canonical_path: str     # cloud path of the new folder
    action: str             # "noop" | "rename" | "merge"


def plan_migration(*, cloud: RcloneCloud, system: str) -> list[MigrationPlan]:
    """Produce a per-folder plan without modifying anything."""
    base = f"{cloud.remote.rstrip('/')}/{system}"
    try:
        entries = cloud.lsjson(base)
    except CloudError:
        return []
    # First pass: which canonical IDs already exist? Used to spot merges.
    canonical_existing: set[str] = set()
    for e in entries:
        if e.get("IsDir"):
            canonical_existing.add(e["Name"])

    plans: list[MigrationPlan] = []
    for e in entries:
        if not e.get("IsDir"):
            continue
        legacy = e["Name"]
        canonical = derive_canonical_id(legacy)
        legacy_path = f"{base}/{legacy}"
        canonical_path = f"{base}/{canonical}"
        if legacy == canonical:
            plans.append(MigrationPlan(
                legacy_path=legacy_path, legacy_id=legacy,
                canonical_id=canonical, canonical_path=canonical_path,
                action="noop",
            ))
            continue
        if canonical in canonical_existing and canonical != legacy:
            plans.append(MigrationPlan(
                legacy_path=legacy_path, legacy_id=legacy,
                canonical_id=canonical, canonical_path=canonical_path,
                action="merge",
            ))
        else:
            plans.append(MigrationPlan(
                legacy_path=legacy_path, legacy_id=legacy,
                canonical_id=canonical, canonical_path=canonical_path,
                action="rename",
            ))
            canonical_existing.add(canonical)
    return plans


def apply_migration(*, cloud: RcloneCloud, plan: list[MigrationPlan],
                    state: StateStore | None = None,
                    dry_run: bool = False) -> dict[str, int]:
    """Execute the plan. Returns a count summary by action."""
    counts: dict[str, int] = {"noop": 0, "rename": 0, "merge": 0,
                              "failed": 0}
    for p in plan:
        if p.action == "noop":
            counts["noop"] += 1
            continue
        try:
            if p.action == "rename":
                log.info("rename: %s → %s", p.legacy_path, p.canonical_path)
                if not dry_run:
                    _rclone_move(cloud, p.legacy_path, p.canonical_path)
                counts["rename"] += 1
            elif p.action == "merge":
                log.info("merge: %s into %s",
                         p.legacy_path, p.canonical_path)
                if not dry_run:
                    _merge_versions(cloud, p.legacy_path, p.canonical_path)
                counts["merge"] += 1
            if not dry_run and state is not None:
                _update_state_game_id(state, p.legacy_id, p.canonical_id)
        except (CloudError, subprocess.CalledProcessError) as exc:
            log.error("failed migrating %s: %s", p.legacy_path, exc)
            counts["failed"] += 1
    return counts


def _rclone_move(cloud: RcloneCloud, src: str, dst: str) -> None:
    """rclone moveto/move equivalent. We use `move` (move contents) to
    preserve directory structure. Then delete the now-empty source dir.
    """
    cloud._run("move", src, dst)
    # Best-effort cleanup of the empty source.
    try:
        cloud._run("rmdir", src, check=False)
    except CloudError:
        pass


def _merge_versions(cloud: RcloneCloud, src: str, dst: str) -> None:
    """Merge `src/versions/*` and `src/conflicts/*` into `dst`. We do
    NOT touch the destination's `current.<ext>` or `manifest.json` —
    those are owned by the live sync; the next pass rebuilds the
    manifest to include the merged versions.
    """
    for sub in ("versions", "conflicts"):
        src_sub = f"{src}/{sub}"
        dst_sub = f"{dst}/{sub}"
        try:
            entries = cloud.lsjson(src_sub)
        except CloudError:
            entries = []
        if not entries:
            continue
        cloud._run("move", src_sub, dst_sub)
    # Best-effort cleanup of the source root (current/manifest become
    # superseded by the destination's live copies).
    try:
        cloud._run("delete", src)
    except CloudError:
        pass


def _update_state_game_id(state: StateStore, legacy_id: str,
                          canonical_id: str) -> None:
    """Re-tag state.db rows so post-migration lookups land at the new path.

    `files.game_id` is the obvious one. We also rewrite the
    `<system>/<legacy_id>/` substring in any cloud_path columns
    (`versions.cloud_path`, `conflicts.cloud_path`,
    `conflicts.conflict_path`) so the rebuilt manifest and the
    `retrosync conflicts resolve` lookup find the new locations.
    """
    legacy_seg = f"/{legacy_id}/"
    canonical_seg = f"/{canonical_id}/"
    with state.tx() as c:
        c.execute(
            "UPDATE files SET game_id=? WHERE game_id=?",
            (canonical_id, legacy_id))
        c.execute(
            "UPDATE versions "
            "SET cloud_path = REPLACE(cloud_path, ?, ?) "
            "WHERE cloud_path LIKE ?",
            (legacy_seg, canonical_seg, f"%{legacy_seg}%"))
        c.execute(
            "UPDATE conflicts "
            "SET cloud_path = REPLACE(cloud_path, ?, ?) "
            "WHERE cloud_path LIKE ?",
            (legacy_seg, canonical_seg, f"%{legacy_seg}%"))
        c.execute(
            "UPDATE conflicts "
            "SET conflict_path = REPLACE(conflict_path, ?, ?) "
            "WHERE conflict_path LIKE ?",
            (legacy_seg, canonical_seg, f"%{legacy_seg}%"))


def migrate(*, cloud: RcloneCloud, system: str,
            state: StateStore | None = None,
            dry_run: bool = False) -> dict[str, int]:
    """Top-level entry: plan + apply. Returns the count summary."""
    plan = plan_migration(cloud=cloud, system=system)
    return apply_migration(cloud=cloud, plan=plan, state=state,
                           dry_run=dry_run)
