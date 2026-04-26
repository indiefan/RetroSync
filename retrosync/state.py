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
  FOREIGN KEY (source_id, path) REFERENCES files(source_id, path)
);

CREATE INDEX IF NOT EXISTS versions_by_file
  ON versions(source_id, path, observed_at);
CREATE INDEX IF NOT EXISTS versions_pending
  ON versions(state) WHERE state IN ('pending','debouncing','ready','uploading');
"""

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
                       size_bytes: int) -> int:
        with self.tx() as c:
            cur = c.execute("""
                INSERT INTO versions
                  (source_id, path, hash, size_bytes, observed_at, state, retention, stable_polls)
                VALUES (?, ?, ?, ?, ?, ?, 'keep', 0)
            """, (source_id, path, h, size_bytes, _utcnow_iso(), ST_PENDING))
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
                      "WHERE id=? AND state IN (?, ?)",
                      (version_id, ST_PENDING, ST_DEBOUNCING))

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

    def list_versions(self, source_id: str,
                      path: str) -> list[VersionRow]:
        return [_row_to_version(r) for r in self._conn.execute("""
            SELECT * FROM versions
            WHERE source_id=? AND path=?
            ORDER BY id DESC
        """, (source_id, path))]

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


def _row_to_version(row: sqlite3.Row) -> VersionRow:
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
    )
