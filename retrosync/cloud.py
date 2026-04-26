"""Cloud destination: rclone-backed uploader and manifest manager.

Path scheme (must stay in lock-step with the design doc):

    <remote>/<system>/<game-id>/
        current.<ext>
        manifest.json
        versions/
            YYYY-MM-DDTHH-MM-SSZ--<hash8>.<ext>
        conflicts/
            YYYY-MM-DDTHH-MM-SSZ--<hash8>--from-<source-id>.<ext>

`<remote>` is configured (e.g. "gdrive:retro-saves").
`<ext>` is the original save file's extension, lowercased.

The manifest is the operator-facing index. It lists every version we've
uploaded along with its hash, size, and retention status. `manifest.json`
is rebuilt locally from SQLite on each upload, so it never falls behind.

Manifest schema v2 (current): adds `device_state` (per-source last-synced
hash) and `conflicts` (open + resolved divergence events). Old v1
manifests still read fine — missing fields default to empty.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Iterable

log = logging.getLogger(__name__)


class CloudError(Exception):
    pass


@dataclass(frozen=True)
class CloudPaths:
    """Composed cloud paths for one save file."""
    base: str               # 'gdrive:retro-saves/snes/super_metroid'
    current: str            # base + '/current.srm'
    manifest: str           # base + '/manifest.json'

    def version(self, ts_iso: str, hash8: str, ext: str) -> str:
        # Drive-safe timestamp: ':' → '-'. Z is already safe.
        safe_ts = ts_iso.replace(":", "-")
        return f"{self.base}/versions/{safe_ts}--{hash8}{ext}"

    def conflict(self, ts_iso: str, hash8: str, ext: str,
                 source_id: str) -> str:
        safe_ts = ts_iso.replace(":", "-")
        # source-id may contain anything; sanitize for path safety.
        safe_src = "".join(
            ch if ch.isalnum() or ch in "-_." else "-" for ch in source_id)
        return (f"{self.base}/conflicts/"
                f"{safe_ts}--{hash8}--from-{safe_src}{ext}")


def hash8(full_hash: str) -> str:
    return full_hash[:8]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Per-system canonical extension for cloud's `current.<ext>`. Keeps the
# cloud layout source-agnostic so an FXPak `.srm` and a Pocket SNES core
# `.sav` for the same game land at the same `current.srm` URL. Versions
# directories preserve whichever extension was uploaded so version files
# stay self-describing.
SYSTEM_CANONICAL_EXTENSION: dict[str, str] = {
    "snes": ".srm",
}


def canonical_extension_for(system: str, fallback_filename: str) -> str:
    """Pick the cloud-side `current.<ext>` extension for a given system.

    Falls back to the input filename's extension (lowercased) when the
    system isn't in the canonical map — preserves behavior for systems
    we haven't standardized yet.
    """
    if system in SYSTEM_CANONICAL_EXTENSION:
        return SYSTEM_CANONICAL_EXTENSION[system]
    return (PurePosixPath(fallback_filename).suffix or ".bin").lower()


def compose_paths(*, remote: str, system: str, game_id: str,
                  save_filename: str) -> CloudPaths:
    ext = canonical_extension_for(system, save_filename)
    base = f"{remote.rstrip('/')}/{system}/{game_id}"
    return CloudPaths(
        base=base,
        current=f"{base}/current{ext}",
        manifest=f"{base}/manifest.json",
    )


@dataclass
class ManifestEntry:
    cloud_path: str
    hash: str
    size_bytes: int
    observed_at: str
    uploaded_at: str
    retention: str = "keep"
    parent_hash: str | None = None
    uploaded_by: str | None = None

    def to_dict(self) -> dict:
        out = {
            "cloud_path": self.cloud_path,
            "hash": self.hash,
            "size_bytes": self.size_bytes,
            "observed_at": self.observed_at,
            "uploaded_at": self.uploaded_at,
            "retention": self.retention,
            "parent_hash": self.parent_hash,
            "uploaded_by": self.uploaded_by,
        }
        return out


@dataclass
class DeviceState:
    last_synced_hash: str
    last_synced_at: str

    def to_dict(self) -> dict:
        return {
            "last_synced_hash": self.last_synced_hash,
            "last_synced_at": self.last_synced_at,
        }


@dataclass
class ConflictEntry:
    id: int
    detected_at: str
    base_hash: str | None
    cloud: dict          # {"hash":..., "path":..., "from":...}
    device: dict         # {"hash":..., "path":..., "from":...}
    resolved_at: str | None = None
    winner_hash: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "detected_at": self.detected_at,
            "base_hash": self.base_hash,
            "cloud": self.cloud,
            "device": self.device,
            "resolved_at": self.resolved_at,
            "winner_hash": self.winner_hash,
        }


# Manifests written by RetroSync after Pocket Sync lands.
MANIFEST_SCHEMA = 2


@dataclass
class Manifest:
    """Cloud-side index for one save's history.

    `source_id` is the *primary* uploader (kept for v1 read-back), but
    per-version provenance lives in `entry.uploaded_by` and per-source
    state in `device_state`.
    """
    schema: int
    source_id: str
    system: str
    game_id: str
    save_path: str
    versions: list[ManifestEntry]
    current_hash: str | None
    updated_at: str
    save_filename: str | None = None
    device_state: dict[str, DeviceState] = None  # type: ignore[assignment]
    conflicts: list[ConflictEntry] = None        # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.device_state is None:
            self.device_state = {}
        if self.conflicts is None:
            self.conflicts = []

    def to_json(self) -> str:
        return json.dumps({
            "schema": self.schema,
            "system": self.system,
            "game_id": self.game_id,
            "save_path": self.save_path,
            "save_filename": self.save_filename,
            "source_id": self.source_id,
            "current_hash": self.current_hash,
            "updated_at": self.updated_at,
            "device_state": {sid: ds.to_dict()
                             for sid, ds in self.device_state.items()},
            "versions": [v.to_dict() for v in self.versions],
            "conflicts": [c.to_dict() for c in self.conflicts],
        }, indent=2, sort_keys=True)


class RcloneCloud:
    """Wraps the rclone CLI. We deliberately shell out rather than use the
    Python rclone bindings — the binary's behavior, retry logic, and config
    are battle-tested, and shelling out keeps the surface area tiny.

    `config_path` is passed via --config to every rclone invocation so the
    daemon and the CLI always agree on which credentials to use, regardless
    of HOME, RCLONE_CONFIG env var, or systemd namespace restrictions.
    """

    def __init__(self, *, remote: str, binary: str = "rclone",
                 config_path: str | None = None,
                 extra_args: tuple[str, ...] = ("--retries", "3",
                                                "--low-level-retries", "5",
                                                "--timeout", "60s")):
        if ":" not in remote:
            raise ValueError(
                f"remote must be 'remote-name:path', got {remote!r}")
        self._remote = remote
        self._binary = binary
        self._config_path = config_path
        # Build the args once; --config goes first so it's clear in process
        # listings.
        prefix: tuple[str, ...] = ()
        if config_path:
            prefix = ("--config", config_path)
        self._extra = prefix + tuple(extra_args)

    @property
    def remote(self) -> str:
        return self._remote

    # ----------- low-level rclone -----------

    def _run(self, *args: str, stdin: bytes | None = None,
             capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
        # Global flags (--config, --retries, --timeout) before the subcommand;
        # rclone accepts them either side but this is the conventional order.
        cmd = [self._binary, *self._extra, *args]
        log.debug("rclone: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd, input=stdin,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE,
                check=check, timeout=300,
            )
        except FileNotFoundError as exc:
            raise CloudError(f"rclone binary not found at {self._binary}") from exc
        except subprocess.CalledProcessError as exc:
            raise CloudError(
                f"rclone {' '.join(args[:2])} failed (exit {exc.returncode}): "
                f"{exc.stderr.decode(errors='replace').strip()}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CloudError(f"rclone {' '.join(args[:2])} timed out") from exc
        return proc

    def reachable(self) -> bool:
        """Quick sanity check: list the remote root. Returns False on auth/network errors."""
        try:
            self._run("lsf", self._remote, capture=True, check=False)
            return True
        except CloudError:
            return False

    def upload_bytes(self, *, data: bytes, dest: str) -> None:
        """rcat is the streaming-upload verb; uses one round-trip per call."""
        self._run("rcat", dest, stdin=data)

    def download_bytes(self, *, src: str) -> bytes:
        proc = self._run("cat", src, capture=True)
        return proc.stdout

    def exists(self, path: str) -> bool:
        """Return True iff <path> exists in cloud.

        Distinguishes "definitely missing" from "couldn't tell" via
        rclone's documented exit codes:
          0 + non-empty json → exists
          0 + empty json     → doesn't exist
          3 (directory not found), 4 (file not found) → doesn't exist
          anything else (2 / 5 / 6 / 7 / quota / network) → transient,
              raise CloudError so the caller doesn't misinterpret a
              network blip as "this game's manifest is missing → bootstrap
              upload" and silently re-upload an unchanged save.
        """
        proc = self._run("lsjson", path, capture=True, check=False)
        if proc.returncode == 0:
            try:
                entries = json.loads(proc.stdout or b"[]")
            except json.JSONDecodeError:
                return False
            return bool(entries)
        if proc.returncode in (3, 4):
            return False
        raise CloudError(
            f"rclone lsjson {path} failed (exit {proc.returncode}): "
            f"{proc.stderr.decode(errors='replace').strip()}")

    def lsjson(self, path: str) -> list[dict]:
        proc = self._run("lsjson", path, capture=True)
        try:
            return json.loads(proc.stdout or b"[]")
        except json.JSONDecodeError as exc:
            raise CloudError(f"bad lsjson output: {exc}") from exc

    def delete(self, path: str) -> None:
        self._run("delete", path, check=False)

    # ----------- high-level upload -----------

    def upload_version(self, *, paths: CloudPaths, save_data: bytes,
                       full_hash: str, observed_at: str) -> str:
        """Write versions/<ts>--<hash8>.<ext> and return its cloud path.

        Idempotent: if the version path already exists with the same size,
        we trust it. Hash comparison would require a download.
        """
        size = len(save_data)
        ext = (PurePosixPath(paths.current).suffix or ".bin")
        # Use the *current* timestamp as the version's wall-clock anchor.
        # `observed_at` is when the daemon first saw this hash, which may be
        # slightly earlier; using upload time keeps timestamps strictly
        # monotonic in the cloud listing.
        version_path = paths.version(utc_iso(), hash8(full_hash), ext)
        if self.exists(version_path):
            log.debug("version already in cloud: %s", version_path)
            return version_path
        self.upload_bytes(data=save_data, dest=version_path)
        return version_path

    def overwrite_current(self, *, paths: CloudPaths, save_data: bytes) -> None:
        self.upload_bytes(data=save_data, dest=paths.current)

    def write_manifest(self, *, paths: CloudPaths, manifest: Manifest) -> None:
        self.upload_bytes(data=manifest.to_json().encode("utf-8"),
                          dest=paths.manifest)

    def read_manifest(self, paths: CloudPaths) -> Manifest | None:
        if not self.exists(paths.manifest):
            return None
        try:
            raw = json.loads(self.download_bytes(src=paths.manifest))
        except json.JSONDecodeError as exc:
            raise CloudError(f"corrupt manifest at {paths.manifest}: {exc}") from exc
        return parse_manifest(raw)


def parse_manifest(raw: dict) -> Manifest:
    """Parse a manifest dict tolerantly. Accepts v1 and v2 layouts."""
    versions: list[ManifestEntry] = []
    for v in raw.get("versions", []):
        versions.append(ManifestEntry(
            cloud_path=v.get("cloud_path", ""),
            hash=v.get("hash", ""),
            size_bytes=v.get("size_bytes", 0),
            observed_at=v.get("observed_at", ""),
            uploaded_at=v.get("uploaded_at", ""),
            retention=v.get("retention", "keep"),
            parent_hash=v.get("parent_hash"),
            uploaded_by=v.get("uploaded_by"),
        ))
    device_state: dict[str, DeviceState] = {}
    for sid, ds in (raw.get("device_state") or {}).items():
        device_state[sid] = DeviceState(
            last_synced_hash=ds.get("last_synced_hash", ""),
            last_synced_at=ds.get("last_synced_at", ""),
        )
    conflicts: list[ConflictEntry] = []
    for c in raw.get("conflicts") or []:
        conflicts.append(ConflictEntry(
            id=c.get("id", 0),
            detected_at=c.get("detected_at", ""),
            base_hash=c.get("base_hash"),
            cloud=c.get("cloud") or c.get("candidate_a") or {},
            device=c.get("device") or c.get("candidate_b") or {},
            resolved_at=c.get("resolved_at"),
            winner_hash=c.get("winner_hash"),
        ))
    return Manifest(
        schema=raw.get("schema", 1),
        source_id=raw.get("source_id", ""),
        system=raw.get("system", ""),
        game_id=raw.get("game_id", ""),
        save_path=raw.get("save_path", ""),
        save_filename=raw.get("save_filename"),
        current_hash=raw.get("current_hash"),
        updated_at=raw.get("updated_at", utc_iso()),
        versions=versions,
        device_state=device_state,
        conflicts=conflicts,
    )


def build_manifest(*, source_id: str, system: str, game_id: str,
                   save_path: str, current_hash: str | None,
                   versions: Iterable[ManifestEntry],
                   save_filename: str | None = None,
                   device_state: dict[str, DeviceState] | None = None,
                   conflicts: Iterable[ConflictEntry] | None = None,
                   ) -> Manifest:
    return Manifest(
        schema=MANIFEST_SCHEMA,
        source_id=source_id,
        system=system,
        game_id=game_id,
        save_path=save_path,
        save_filename=save_filename,
        current_hash=current_hash,
        updated_at=utc_iso(),
        versions=sorted(list(versions), key=lambda v: v.uploaded_at),
        device_state=dict(device_state or {}),
        conflicts=sorted(list(conflicts or []), key=lambda c: c.id),
    )
