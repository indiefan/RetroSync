"""SQLite state store.

Schema mirrors the design doc. We use WAL mode so an unclean shutdown
mid-write doesn't corrupt the DB. All writes happen on the orchestrator's
single thread, so no app-level locking is needed.

The store is intentionally narrow — it does not understand cloud paths or
the path scheme. The orchestrator builds those and passes them in.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id          TEXT PRIMARY KEY,
  system      TEXT NOT NULL,
  adapter     TEXT NOT NULL,
  config_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS files (
  source_id    TEXT NOT NULL REFERENCES sources(id),
  path         TEXT NOT NULL,
  game_id      TEXT,
  first_seen   TEXT NOT NULL,
  last_seen    TEXT NOT NULL,
  current_hash TEXT,
  PRIMARY KEY (source_id, path)
);

CREATE TABLE IF NOT EXISTS versions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id     TEXT NOT NULL,
  path          TEXT NOT NULL,
  hash          TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  observed_at   TEXT NOT NULL,
  uploaded_at   TEXT,
  cloud_path    TEXT,
  state         TEXT NOT NULL,
  retention     TEXT NOT NULL DEFAULT 'keep',
  stable_polls  INTEGER NOT NULL DEFAULT 0,
  parent_hash   TEXT,
  FOREIGN KEY (source_id, path) REFERENCES files(source_id, path)
);

CREATE INDEX IF NOT EXISTS versions_by_file
  ON versions(source_id, path, observed_at);
CREATE INDEX IF NOT EXISTS versions_pending
  ON versions(state) WHERE state IN ('pending','debouncing','ready','uploading');

-- Per-(source, game) pointer to the hash this device and the cloud last
-- agreed on. Lets the sync engine distinguish a fast-forward (one side
-- moved past the agreed hash) from a divergence (both moved).
CREATE TABLE IF NOT EXISTS source_sync_state (
  source_id          TEXT NOT NULL,
  game_id            TEXT NOT NULL,
  last_synced_hash   TEXT NOT NULL,
  last_synced_at     TEXT NOT NULL,
  device_seen_path   TEXT,
  PRIMARY KEY (source_id, game_id)
);

-- Open or resolved conflicts: a row exists per (game, divergence event).
-- Resolved rows keep their history for forensics; the engine treats only
-- those with resolved_at IS NULL as live.
CREATE TABLE IF NOT EXISTS conflicts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id         TEXT NOT NULL,
  system          TEXT NOT NULL,
  source_id       TEXT NOT NULL,
  detected_at     TEXT NOT NULL,
  base_hash       TEXT,
  cloud_hash      TEXT NOT NULL,
  device_hash     TEXT NOT NULL,
  cloud_path      TEXT,
  conflict_path   TEXT,
  resolved_at     TEXT,
  winner_hash     TEXT
);

CREATE INDEX IF NOT EXISTS conflicts_open
  ON conflicts(resolved_at) WHERE resolved_at IS NULL;

-- Per-(source, game) record of the canonical save filename on the device.
-- For sources where the save filename is dictated by the user's ROM
-- filename (the EmuDeck case: RetroArch derives the save path from the
-- loaded ROM path), this table caches that mapping so a bootstrap-pull
-- knows where to write the bytes without re-scanning the ROMs directory
-- on every sync.
CREATE TABLE IF NOT EXISTS device_filename_map (
  source_id   TEXT NOT NULL,
  game_id     TEXT NOT NULL,
  filename    TEXT NOT NULL,
  rom_stem    TEXT,
  observed_at TEXT NOT NULL,
  PRIMARY KEY (source_id, game_id)
);

CREATE TABLE IF NOT EXISTS gameplay_sessions (
    source_id TEXT NOT NULL,
    game_id TEXT NOT NULL,
    last_played_at TEXT NOT NULL,
    PRIMARY KEY (source_id, game_id)
);
"""

# Lightweight migrations applied on every open. SQLite doesn't have an
# IF NOT EXISTS for ADD COLUMN, so we probe with PRAGMA table_info first.
_MIGRATIONS = [
    ("versions", "parent_hash", "ALTER TABLE versions ADD COLUMN parent_hash TEXT"),
]

# The states a version row passes through.
ST_PENDING    = "pending"     # Inserted, no debounce evidence yet.
ST_DEBOUNCING = "debouncing"  # Has been seen ≥1 time identical; not yet stable.
ST_READY      = "ready"       # Stable enough; queued for upload.
ST_UPLOADING  = "uploading"   # Upload attempt in flight.
ST_UPLOADED   = "uploaded"    # Live cloud entry, immutable from here on.
ST_TOMBSTONED = "tombstoned"  # File disappeared from source after this version.

ACTIVE_STATES = (ST_PENDING, ST_DEBOUNCING, ST_READY, ST_UPLOADING)


@dataclass
class VersionRow:
    id: int
    source_id: str
    path: str
    hash: str
    size_bytes: int
    observed_at: str
    uploaded_at: str | None
    cloud_path: str | None
    state: str
    retention: str
    stable_polls: int
    parent_hash: str | None = None


@dataclass
class SourceSyncState:
    source_id: str
    game_id: str
    last_synced_hash: str
    last_synced_at: str
    device_seen_path: str | None


@dataclass
class ConflictRow:
    id: int
    game_id: str
    system: str
    source_id: str
    detected_at: str
    base_hash: str | None
    cloud_hash: str
    device_hash: str
    cloud_path: str | None
    conflict_path: str | None
    resolved_at: str | None
    winner_hash: str | None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StateStore:
    """Synchronous SQLite wrapper. Single-writer assumption."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("PRAGMA journal_mode=WAL;"
                                 "PRAGMA synchronous=NORMAL;"
                                 "PRAGMA foreign_keys=ON;")
        self._conn.executescript(SCHEMA)
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        """Add columns missing from old DBs that predate the v2 schema."""
        for table, col, sql in _MIGRATIONS:
            cols = {row["name"] for row in self._conn.execute(
                f"PRAGMA table_info({table})")}
            if col not in cols:
                self._conn.execute(sql)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        # isolation_level=None means autocommit; BEGIN/COMMIT explicitly.
        self._conn.execute("BEGIN")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ----------- sources -----------

    def upsert_source(self, *, id: str, system: str, adapter: str,
                      config_json: str = "{}") -> None:
        with self.tx() as c:
            c.execute("""
                INSERT INTO sources(id, system, adapter, config_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    system=excluded.system,
                    adapter=excluded.adapter,
                    config_json=excluded.config_json
            """, (id, system, adapter, config_json))

    # ----------- files -----------

    def touch_file(self, *, source_id: str, path: str,
                   game_id: str | None) -> None:
        now = _utcnow_iso()
        with self.tx() as c:
            c.execute("""
                INSERT INTO files(source_id, path, game_id, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_id, path) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    game_id = COALESCE(excluded.game_id, files.game_id)
            """, (source_id, path, game_id, now, now))

    def set_current_hash(self, *, source_id: str, path: str, h: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE files SET current_hash=? "
                      "WHERE source_id=? AND path=?",
                      (h, source_id, path))

    def get_current_hash(self, source_id: str, path: str) -> str | None:
        row = self._conn.execute(
            "SELECT current_hash FROM files WHERE source_id=? AND path=?",
            (source_id, path),
        ).fetchone()
        return row["current_hash"] if row else None

    def known_paths(self, source_id: str) -> set[str]:
        return {row["path"] for row in self._conn.execute(
            "SELECT path FROM files WHERE source_id=?", (source_id,))}

    # ----------- versions -----------

    def latest_active_version(self, source_id: str,
                              path: str) -> VersionRow | None:
        row = self._conn.execute("""
            SELECT * FROM versions
            WHERE source_id=? AND path=? AND state IN ({})
            ORDER BY id DESC LIMIT 1
        """.format(",".join("?" * len(ACTIVE_STATES))),
            (source_id, path, *ACTIVE_STATES)).fetchone()
        return _row_to_version(row) if row else None

    def insert_pending(self, *, source_id: str, path: str, h: str,
                       size_bytes: int,
                       parent_hash: str | None = None) -> int:
        with self.tx() as c:
            cur = c.execute("""
                INSERT INTO versions
                  (source_id, path, hash, size_bytes, observed_at, state,
                   retention, stable_polls, parent_hash)
                VALUES (?, ?, ?, ?, ?, ?, 'keep', 0, ?)
            """, (source_id, path, h, size_bytes, _utcnow_iso(),
                  ST_PENDING, parent_hash))
            return cur.lastrowid

    def bump_debounce(self, version_id: int) -> int:
        """Increment the stable-poll counter; returns new value."""
        with self.tx() as c:
            c.execute("""
                UPDATE versions
                SET state = CASE state WHEN ? THEN ? ELSE state END,
                    stable_polls = stable_polls + 1
                WHERE id = ?
            """, (ST_PENDING, ST_DEBOUNCING, version_id))
            row = c.execute("SELECT stable_polls FROM versions WHERE id=?",
                            (version_id,)).fetchone()
            return row["stable_polls"]

    def promote_to_ready(self, version_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE versions SET state=? WHERE id=?",
                      (ST_READY, version_id))

    def supersede(self, version_id: int) -> None:
        """Drop a stale pending/debouncing version when a newer hash arrives."""
        with self.tx() as c:
            c.execute("UPDATE versions SET state='tombstoned' "
                      "WHERE id=? AND state IN (?, ?, ?)",
                      (version_id, ST_PENDING, ST_DEBOUNCING, ST_READY))

    def mark_uploading(self, version_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE versions SET state=? WHERE id=?",
                      (ST_UPLOADING, version_id))

    def mark_uploaded(self, version_id: int, *, cloud_path: str) -> None:
        with self.tx() as c:
            c.execute("""
                UPDATE versions SET state=?, uploaded_at=?, cloud_path=?
                WHERE id=?
            """, (ST_UPLOADED, _utcnow_iso(), cloud_path, version_id))

    def revert_to_ready(self, version_id: int) -> None:
        """Used when an upload attempt fails so the orchestrator retries."""
        with self.tx() as c:
            c.execute("UPDATE versions SET state=? WHERE id=?",
                      (ST_READY, version_id))

    def stuck_uploading(self) -> Iterable[VersionRow]:
        """Reconciliation: rows that were 'uploading' when the daemon died."""
        for row in self._conn.execute(
                "SELECT * FROM versions WHERE state=?", (ST_UPLOADING,)):
            yield _row_to_version(row)

    def ready_versions(self) -> Iterable[VersionRow]:
        for row in self._conn.execute(
                "SELECT * FROM versions WHERE state=? ORDER BY id",
                (ST_READY,)):
            yield _row_to_version(row)

    def hash_in_versions_for_game(self, game_id: str, h: str) -> bool:
        """Return True iff any source has ever uploaded a version with
        hash `h` for `game_id`. Used by the sync engine to detect a
        device presenting bytes that match a known historical version
        — likely a stale device, not new content."""
        row = self._conn.execute("""
            SELECT 1 FROM versions v
            JOIN files f ON v.source_id = f.source_id AND v.path = f.path
            WHERE f.game_id = ? AND v.hash = ? AND v.state = 'uploaded'
            LIMIT 1
        """, (game_id, h)).fetchone()
        return row is not None

    def list_versions(self, source_id: str,
                      path: str) -> list[VersionRow]:
        return [_row_to_version(r) for r in self._conn.execute("""
            SELECT * FROM versions
            WHERE source_id=? AND path=?
            ORDER BY id DESC
        """, (source_id, path))]

    # ----------- source_sync_state -----------

    def get_sync_state(self, source_id: str,
                       game_id: str) -> SourceSyncState | None:
        row = self._conn.execute(
            "SELECT * FROM source_sync_state "
            "WHERE source_id=? AND game_id=?",
            (source_id, game_id),
        ).fetchone()
        if not row:
            return None
        return SourceSyncState(
            source_id=row["source_id"],
            game_id=row["game_id"],
            last_synced_hash=row["last_synced_hash"],
            last_synced_at=row["last_synced_at"],
            device_seen_path=row["device_seen_path"],
        )

    def set_sync_state(self, *, source_id: str, game_id: str,
                       last_synced_hash: str,
                       device_seen_path: str | None = None) -> None:
        now = _utcnow_iso()
        with self.tx() as c:
            c.execute("""
                INSERT INTO source_sync_state
                  (source_id, game_id, last_synced_hash, last_synced_at,
                   device_seen_path)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_id, game_id) DO UPDATE SET
                  last_synced_hash = excluded.last_synced_hash,
                  last_synced_at   = excluded.last_synced_at,
                  device_seen_path = COALESCE(excluded.device_seen_path,
                                              source_sync_state.device_seen_path)
            """, (source_id, game_id, last_synced_hash, now,
                  device_seen_path))

    def clear_sync_state_for_game(self, game_id: str) -> int:
        """After a conflict resolve, drop every source's sync pointer for
        this game so the next sync re-syncs them all to the new winner.
        Returns number of rows cleared."""
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM source_sync_state WHERE game_id=?", (game_id,))
            return cur.rowcount

    # ----------- conflicts -----------

    def insert_conflict(self, *, game_id: str, system: str, source_id: str,
                        base_hash: str | None,
                        cloud_hash: str, device_hash: str,
                        cloud_path: str | None,
                        conflict_path: str | None) -> int:
        with self.tx() as c:
            cur = c.execute("""
                INSERT INTO conflicts
                  (game_id, system, source_id, detected_at, base_hash,
                   cloud_hash, device_hash, cloud_path, conflict_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (game_id, system, source_id, _utcnow_iso(), base_hash,
                  cloud_hash, device_hash, cloud_path, conflict_path))
            return cur.lastrowid

    def open_conflict_for(self, *, game_id: str, source_id: str,
                          device_hash: str) -> ConflictRow | None:
        """Return an unresolved conflict already on file for this exact
        (game, source, device-hash) — used to dedupe repeated polls of
        the same divergence."""
        row = self._conn.execute("""
            SELECT * FROM conflicts
            WHERE game_id=? AND source_id=? AND device_hash=?
              AND resolved_at IS NULL
            ORDER BY id DESC LIMIT 1
        """, (game_id, source_id, device_hash)).fetchone()
        return _row_to_conflict(row) if row else None

    def list_conflicts(self, *, open_only: bool = True) -> list[ConflictRow]:
        sql = "SELECT * FROM conflicts"
        if open_only:
            sql += " WHERE resolved_at IS NULL"
        sql += " ORDER BY id DESC"
        return [_row_to_conflict(r) for r in self._conn.execute(sql)]

    def get_conflict(self, conflict_id: int) -> ConflictRow | None:
        row = self._conn.execute(
            "SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        return _row_to_conflict(row) if row else None

    def resolve_conflict(self, conflict_id: int, *, winner_hash: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE conflicts SET resolved_at=?, winner_hash=? WHERE id=?",
                (_utcnow_iso(), winner_hash, conflict_id))

    def open_conflicts_for_game(self, game_id: str) -> list[ConflictRow]:
        return [_row_to_conflict(r) for r in self._conn.execute(
            "SELECT * FROM conflicts "
            "WHERE game_id=? AND resolved_at IS NULL "
            "ORDER BY id", (game_id,))]

    # ----------- device_filename_map -----------

    def get_filename_map(self, source_id: str,
                         game_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM device_filename_map "
            "WHERE source_id=? AND game_id=?",
            (source_id, game_id)).fetchone()
        if not row:
            return None
        return {
            "source_id": row["source_id"],
            "game_id": row["game_id"],
            "filename": row["filename"],
            "rom_stem": row["rom_stem"],
            "observed_at": row["observed_at"],
        }

    def set_filename_map(self, *, source_id: str, game_id: str,
                         filename: str, rom_stem: str | None) -> None:
        with self.tx() as c:
            c.execute("""
                INSERT INTO device_filename_map
                  (source_id, game_id, filename, rom_stem, observed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_id, game_id) DO UPDATE SET
                    filename = excluded.filename,
                    rom_stem = COALESCE(excluded.rom_stem, device_filename_map.rom_stem),
                    observed_at = excluded.observed_at
            """, (source_id, game_id, filename, rom_stem, _utcnow_iso()))

    def invalidate_filename_map(self, source_id: str,
                                game_id: str | None = None) -> int:
        with self.tx() as c:
            if game_id is None:
                cur = c.execute(
                    "DELETE FROM device_filename_map WHERE source_id=?",
                    (source_id,))
            else:
                cur = c.execute(
                    "DELETE FROM device_filename_map "
                    "WHERE source_id=? AND game_id=?",
                    (source_id, game_id))
            return cur.rowcount

    def list_filename_map(self,
                          source_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM device_filename_map"
        args: tuple = ()
        if source_id is not None:
            sql += " WHERE source_id=?"
            args = (source_id,)
        sql += " ORDER BY source_id, game_id"
        return [{
            "source_id": r["source_id"],
            "game_id": r["game_id"],
            "filename": r["filename"],
            "rom_stem": r["rom_stem"],
            "observed_at": r["observed_at"],
        } for r in self._conn.execute(sql, args)]

    def tombstone_missing(self, source_id: str,
                          present_paths: set[str]) -> int:
        """Mark files no longer reported by the source as tombstoned at the file level.

        We don't delete the cloud copies — versioning is the safety net.
        Returns the count of newly-tombstoned files.
        """
        known = self.known_paths(source_id)
        gone = known - present_paths
        if not gone:
            return 0
        with self.tx() as c:
            for p in gone:
                c.execute(
                    "UPDATE files SET current_hash=NULL "
                    "WHERE source_id=? AND path=?", (source_id, p))
        return len(gone)

    # ----------------------------------------------------------------------
    # Gameplay Sessions
    # ----------------------------------------------------------------------

    def record_gameplay_session(self, source_id: str, game_id: str,
                                played_at: str) -> None:
        """Record the last time a game was observed playing on a source."""
        with self._conn:
            self._conn.execute("""
                INSERT INTO gameplay_sessions (source_id, game_id, last_played_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id, game_id) DO UPDATE SET
                    last_played_at = excluded.last_played_at
            """, (source_id, game_id, played_at))

    def get_last_played_at(self, source_id: str, game_id: str) -> str | None:
        """Get the last time a game was observed playing on a source."""
        row = self._conn.execute("""
            SELECT last_played_at FROM gameplay_sessions
            WHERE source_id = ? AND game_id = ?
        """, (source_id, game_id)).fetchone()
        return row["last_played_at"] if row else None


def _row_to_conflict(row: sqlite3.Row) -> ConflictRow:
    return ConflictRow(
        id=row["id"],
        game_id=row["game_id"],
        system=row["system"],
        source_id=row["source_id"],
        detected_at=row["detected_at"],
        base_hash=row["base_hash"],
        cloud_hash=row["cloud_hash"],
        device_hash=row["device_hash"],
        cloud_path=row["cloud_path"],
        conflict_path=row["conflict_path"],
        resolved_at=row["resolved_at"],
        winner_hash=row["winner_hash"],
    )


def _row_to_version(row: sqlite3.Row) -> VersionRow:
    keys = row.keys()
    return VersionRow(
        id=row["id"],
        source_id=row["source_id"],
        path=row["path"],
        hash=row["hash"],
        size_bytes=row["size_bytes"],
        observed_at=row["observed_at"],
        uploaded_at=row["uploaded_at"],
        cloud_path=row["cloud_path"],
        state=row["state"],
        retention=row["retention"],
        stable_polls=row["stable_polls"],
        parent_hash=row["parent_hash"] if "parent_hash" in keys else None,
    )
