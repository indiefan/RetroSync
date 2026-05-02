"""Local cache manager for cloud saves.

Maintains a local mirror of the cloud state to eliminate network latency
during the blocking boot-up sequence. The `CloudMirror` acts as a fast
proxy:
  - Manifests are checked against Drive, but cached locally.
  - Save file payloads (current.<ext>) are only downloaded if the local
    cache's hash doesn't match the expected `current_hash`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from .cloud import CloudPaths, Manifest, RcloneCloud, compose_paths

log = logging.getLogger(__name__)


class CloudMirror:
    def __init__(self, cache_root: str | os.PathLike):
        self.root = Path(cache_root)
        # Maps cloud rel_path (e.g. "snes/super_metroid/manifest.json") to Drive ModTime
        self._cloud_modtimes: dict[str, str] = {}

    def _local_manifest_path(self, paths: CloudPaths) -> Path:
        system, game_id = paths.base.rsplit("/", 2)[-2:]
        return self.root / system / game_id / "manifest.json"

    def _local_current_path(self, paths: CloudPaths) -> Path:
        system, game_id = paths.base.rsplit("/", 2)[-2:]
        ext = Path(paths.current).suffix
        return self.root / system / game_id / f"current{ext}"

    def _atomic_write(self, dest: Path, data: bytes) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Use a temporary file in the same directory to ensure atomic os.replace
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
        except Exception:
            os.unlink(tmp)
            raise

    async def refresh_manifest(self, paths: CloudPaths, cloud: RcloneCloud) -> Manifest | None:
        """Fetch manifest from Drive and cache it locally.
        
        This is the "fast-path check". `rclone cat` on a small manifest is
        sub-second, confirming our Drive state is current.
        """
        manifest = await asyncio.to_thread(cloud.read_manifest, paths)
        if manifest:
            self._atomic_write(self._local_manifest_path(paths),
                               manifest.to_json().encode("utf-8"))
        return manifest

    def get_manifest(self, paths: CloudPaths) -> Manifest | None:
        """Read manifest directly from the local cache without network calls."""
        local_path = self._local_manifest_path(paths)
        if local_path.exists():
            try:
                return Manifest.from_json(local_path.read_bytes().decode("utf-8"))
            except Exception:
                pass
        return None

    async def get_current_bytes(self, paths: CloudPaths, expected_hash: str, cloud: RcloneCloud) -> bytes:
        """Return save bytes, preferring the local cache if the hash matches."""
        local_path = self._local_current_path(paths)
        if local_path.exists():
            data = local_path.read_bytes()
            # If the file hasn't been corrupted/modified, use it.
            # We use md5 because sync_one_game uses md5 hashes (or whatever hash algorithm).
            # Wait, `expected_hash` is generated via sha256 or md5? 
            # In RetroSync, the hash is `sha256_bytes(data)`. I'll use sha256.
            # Let's check `cloud.py` sha256_bytes function. Yes, it's sha256.
            from .cloud import sha256_bytes
            if sha256_bytes(data) == expected_hash:
                log.debug("cache hit for %s (hash: %s)", paths.current, expected_hash[:8])
                return data
        
        # Cache miss or hash mismatch: download from Drive
        log.info("cache miss for %s, downloading from cloud", paths.current)
        data = await asyncio.to_thread(cloud.download_bytes, src=paths.current)
        self._atomic_write(local_path, data)
        return data

    def update_local(self, paths: CloudPaths, manifest: Manifest, data: bytes) -> None:
        """Synchronously update the cache after the Pi uploads a new save."""
        self._atomic_write(self._local_manifest_path(paths),
                           manifest.to_json().encode("utf-8"))
        self._atomic_write(self._local_current_path(paths), data)

    async def background_poll(self, cloud: RcloneCloud) -> None:
        """Find newer manifests in Drive and pre-download them."""
        try:
            log.debug("starting background cloud poll")
            # lsjson requires the path. If we append the filter flags to the remote arg,
            # rclone CLI parses it correctly if passed as separate arguments, but
            # cloud._run accepts extra args before the positional.
            # wait, `cloud.lsjson` takes a single path arg.
            # Let's use `cloud._run` directly to pass the filter safely.
            proc = await asyncio.to_thread(
                cloud._run, 
                "lsjson", cloud.remote, "-R", "--include", "/*/*/manifest.json",
                capture=True
            )
            entries = json.loads(proc.stdout or b"[]")
        except Exception as e:
            log.warning("background cloud poll failed: %s", e)
            return

        for entry in entries:
            rel_path = entry.get("Path")
            cloud_modtime = entry.get("ModTime")
            if not rel_path or not cloud_modtime:
                continue
            
            if self._cloud_modtimes.get(rel_path) == cloud_modtime:
                continue
                
            parts = rel_path.split("/")
            if len(parts) != 3:
                continue
                
            system, game_id, _ = parts
            paths = compose_paths(remote=cloud.remote, system=system, 
                                  game_id=game_id, save_filename=f"{game_id}.bin")
            
            # Read local manifest to see current_hash before refresh
            local_man_path = self._local_manifest_path(paths)
            local_hash = None
            if local_man_path.exists():
                try:
                    with open(local_man_path, "r") as f:
                        local_man = json.load(f)
                        local_hash = local_man.get("current_hash")
                except Exception:
                    pass

            # Fetch the new manifest
            try:
                manifest = await self.refresh_manifest(paths, cloud)
                if manifest and manifest.current_hash and manifest.current_hash != local_hash:
                    log.info("background poll: caching newer save manifest for %s/%s", system, game_id)
                
                # Mark as seen
                self._cloud_modtimes[rel_path] = cloud_modtime
            except Exception as e:
                log.warning("background poll failed to cache %s/%s: %s", system, game_id, e)
