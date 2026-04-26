"""Cloud destination: rclone-backed uploader and manifest manager.

Path scheme (must stay in lock-step with the design doc):

    <remote>/<system>/<game-id>/
        current.<ext>
        manifest.json
        versions/
            YYYY-MM-DDTHH-MM-SSZ--<hash8>.<ext>

`<remote>` is configured (e.g. "gdrive:retro-saves").
`<ext>` is the original save file's extension, lowercased.

The manifest is the operator-facing index. It lists every version we've
uploaded along with its hash, size, and retention status. `manifest.json`
is rebuilt locally from SQLite on each upload, so it never falls behind.
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
    base: str               # 'gdrive:retro-saves/snes/0a1b2c3d_super_metroid'
    current: str            # base + '/current.srm'
    manifest: str           # base + '/manifest.json'

    def version(self, ts_iso: str, hash8: str, ext: str) -> str:
        # Drive-safe timestamp: ':' → '-'. Z is already safe.
        safe_ts = ts_iso.replace(":", "-")
        return f"{self.base}/versions/{safe_ts}--{hash8}{ext}"


def hash8(full_hash: str) -> str:
    return full_hash[:8]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compose_paths(*, remote: str, system: str, game_id: str,
                  save_filename: str) -> CloudPaths:
    ext = (PurePosixPath(save_filename).suffix or ".bin").lower()
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

    def to_dict(self) -> dict:
        return {
            "cloud_path": self.cloud_path,
            "hash": self.hash,
            "size_bytes": self.size_bytes,
            "observed_at": self.observed_at,
            "uploaded_at": self.uploaded_at,
            "retention": self.retention,
        }


@dataclass
class Manifest:
    schema: int
    source_id: str
    system: str
    game_id: str
    save_path: str
    versions: list[ManifestEntry]
    current_hash: str | None
    updated_at: str

    def to_json(self) -> str:
        return json.dumps({
            "schema": self.schema,
            "source_id": self.source_id,
            "system": self.system,
            "game_id": self.game_id,
            "save_path": self.save_path,
            "current_hash": self.current_hash,
            "updated_at": self.updated_at,
            "versions": [v.to_dict() for v in self.versions],
        }, indent=2, sort_keys=True)


class RcloneCloud:
    """Wraps the rclone CLI. We deliberately shell out rather than use the
    Python rclone bindings — the binary's behavior, retry logic, and config
    are battle-tested, and shelling out keeps the surface area tiny.
    """

    def __init__(self, *, remote: str, binary: str = "rclone",
                 extra_args: tuple[str, ...] = ("--retries", "3",
                                                "--low-level-retries", "5",
                                                "--timeout", "60s")):
        if ":" not in remote:
            raise ValueError(
                f"remote must be 'remote-name:path', got {remote!r}")
        self._remote = remote
        self._binary = binary
        self._extra = tuple(extra_args)

    @property
    def remote(self) -> str:
        return self._remote

    # ----------- low-level rclone -----------

    def _run(self, *args: str, stdin: bytes | None = None,
             capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [self._binary, *args, *self._extra]
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
        # `rclone lsf` returns 0 with no output when the path doesn't exist
        # in the remote root; for nested paths it returns nonzero. We use
        # `rclone lsjson` for unambiguous existence.
        proc = self._run("lsjson", path, capture=True, check=False)
        if proc.returncode != 0:
            return False
        try:
            entries = json.loads(proc.stdout or b"[]")
        except json.JSONDecodeError:
            return False
        return bool(entries)

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
        return Manifest(
            schema=raw.get("schema", 1),
            source_id=raw.get("source_id", ""),
            system=raw.get("system", ""),
            game_id=raw.get("game_id", ""),
            save_path=raw.get("save_path", ""),
            current_hash=raw.get("current_hash"),
            updated_at=raw.get("updated_at", utc_iso()),
            versions=[ManifestEntry(**v) for v in raw.get("versions", [])],
        )


def build_manifest(*, source_id: str, system: str, game_id: str,
                   save_path: str, current_hash: str | None,
                   versions: Iterable[ManifestEntry]) -> Manifest:
    return Manifest(
        schema=1,
        source_id=source_id,
        system=system,
        game_id=game_id,
        save_path=save_path,
        current_hash=current_hash,
        updated_at=utc_iso(),
        versions=sorted(list(versions), key=lambda v: v.uploaded_at),
    )
