"""High-level `retrosync load` flow: pull a game's current cloud save and
write it onto a target device by friendly name.

Friendly target names:
  - "pocket"  : the Analogue Pocket. Auto-detects /dev/sd* via the
                vendor:product ID; mounts, writes, unmounts.
  - "<system>": a console name like "snes". Resolves to the configured
                cart adapter for that system (FXPak Pro for snes today).
                Looks up the cart-side save path from state.db / live
                listing, then writes via the adapter.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .cloud import (CloudError, RcloneCloud, compose_paths, sha256_bytes)
from .config import Config, SourceConfig
from .pocket.sync_runner import (build_pocket_source, mount_pocket,
                                 unmount_pocket)
from .sources.base import SaveRef, SaveSource
from .sources.registry import build as build_source
from .state import StateStore

log = logging.getLogger(__name__)


TARGET_POCKET = "pocket"
DEFAULT_POCKET_MOUNT = "/run/retrosync/load-mount"


@dataclass
class LoadResult:
    target: str
    game_id: str
    source_id: str
    cloud_path: str
    written_path: str
    bytes_written: int
    sha256: str


def find_pocket_device() -> str | None:
    """Return /dev/sdX1 for an attached Analogue Pocket, or None.

    Linux's udev creates symlinks under /dev/disk/by-id/ that include the
    USB device's manufacturer + product strings. The Pocket reports as
    "Microchip Technology Inc." / "Analogue Pocket", so we glob for any
    by-id entry containing "Pocket" and ending in "-part1".
    """
    candidates = sorted(glob.glob("/dev/disk/by-id/usb-*Pocket*-part1"))
    if not candidates:
        return None
    return os.path.realpath(candidates[0])


def resolve_cart_source(cfg: Config, system: str) -> tuple[SourceConfig, SaveSource]:
    """Pick the configured cart adapter for the given system. The cart
    distinction is "not the pocket adapter" — the Pocket is its own target
    even though it covers the snes system."""
    for s in cfg.sources:
        if s.adapter == "pocket":
            continue
        try:
            built = build_source(s.adapter, id=s.id, **s.options)
        except Exception as exc:  # noqa: BLE001
            log.debug("skipping source %s: %s", s.id, exc)
            continue
        if built.system == system:
            return s, built
    raise ValueError(
        f"no non-pocket source configured for system {system!r}; "
        f"check /etc/retrosync/config.yaml")


def cart_path_for_game(state: StateStore, source: SaveSource,
                       game_id: str) -> str | None:
    """Find the cart-side save path for `game_id`. Tries state.db first,
    falls back to live `list_saves` so a recent slug rename or freshly
    plugged cart still resolves correctly.
    """
    row = state._conn.execute(
        "SELECT path FROM files WHERE source_id=? AND game_id=?",
        (source.id, game_id)).fetchone()
    if row is not None:
        return row["path"]
    # Live fallback: ask the cart what it has.
    try:
        refs = asyncio.run(source.list_saves())
    except Exception as exc:  # noqa: BLE001
        log.warning("live list_saves failed for %s: %s", source.id, exc)
        return None
    for ref in refs:
        if source.resolve_game_id(ref) == game_id:
            return ref.path
    return None


async def _load_to_cart(source: SaveSource, cart_path: str,
                        data: bytes) -> str:
    await source.write_save(SaveRef(path=cart_path), data)
    return cart_path


def _ensure_mounted(*, device: str | None,
                    mount_path: str) -> tuple[str, bool]:
    """Mount the Pocket at `mount_path` if not already mounted.

    Returns (mount_path, did_mount). did_mount tells the caller whether
    they own the unmount.
    """
    # If something is already mounted at mount_path, reuse it.
    if Path(mount_path).is_mount():
        return mount_path, False
    if device is None:
        device = find_pocket_device()
    if device is None:
        raise FileNotFoundError(
            "no Pocket attached. Plug it in via 'Tools → USB → Mount as USB "
            "Drive', or pass --device /dev/sdX1 explicitly.")
    mount_pocket(device=device, mount_path=mount_path)
    return mount_path, True


def load(*, cfg: Config, game_id: str, target: str,
         device: str | None = None,
         mount_path: str = DEFAULT_POCKET_MOUNT,
         system: str | None = None) -> LoadResult:
    """Load cloud's current save for game_id onto target.

    `target` is "pocket" or a system name ("snes", etc.). When loading to
    the Pocket and `device` is None, we auto-detect via /dev/disk/by-id.
    """
    state = StateStore(cfg.state.db_path)
    cloud = RcloneCloud(remote=cfg.cloud.rclone_remote,
                        binary=cfg.cloud.rclone_binary,
                        config_path=cfg.cloud.rclone_config_path)

    # Cart targets infer system from their adapter; pocket defaults to its
    # configured system (snes for v0.2). `--system` lets the operator
    # override for cross-system loads or when the cloud namespace differs
    # from the source's natural system.
    if target == TARGET_POCKET:
        target_system = system or _pocket_system(cfg)
    else:
        target_system = system or target

    paths = compose_paths(remote=cloud.remote, system=target_system,
                          game_id=game_id, save_filename=f"{game_id}.bin")
    if not cloud.exists(paths.current):
        state.close()
        raise FileNotFoundError(
            f"no current save in cloud for {target_system}/{game_id}; "
            f"checked {paths.current}")

    data = cloud.download_bytes(src=paths.current)
    h = sha256_bytes(data)
    log.info("loaded %d bytes from %s (sha256=%s)",
             len(data), paths.current, h[:8])

    if target == TARGET_POCKET:
        result = _load_pocket(cfg=cfg, game_id=game_id, data=data, h=h,
                              cloud_path=paths.current,
                              device=device, mount_path=mount_path)
    else:
        result = _load_cart(cfg=cfg, system=target_system, game_id=game_id,
                            data=data, h=h, cloud_path=paths.current,
                            state=state)
    state.close()
    return result


def _load_pocket(*, cfg: Config, game_id: str, data: bytes, h: str,
                 cloud_path: str, device: str | None,
                 mount_path: str) -> LoadResult:
    # Mounting is a privileged operation. We need to be root by the time
    # we call _ensure_mounted; the wrapper at /usr/local/bin/retrosync
    # only stays root for `load <game> pocket` if the caller already has
    # EUID 0 (i.e. they ran `sudo retrosync ...`).
    if os.geteuid() != 0:
        raise PermissionError(
            "loading to the Pocket needs root for the mount step. "
            "Re-run as: sudo retrosync load " + game_id + " pocket")
    actual_mount, owned = _ensure_mounted(device=device,
                                          mount_path=mount_path)
    try:
        source = build_pocket_source(
            source_id=_pocket_source_id(cfg),
            mount_path=actual_mount, config=cfg)
        target_path = source.canonical_save_path(game_id)
        log.info("writing pocket save to %s", target_path)
        asyncio.run(source.write_save(SaveRef(path=str(target_path)), data))
    finally:
        if owned:
            unmount_pocket(mount_path=actual_mount, device=device)
    return LoadResult(
        target=TARGET_POCKET, game_id=game_id,
        source_id=_pocket_source_id(cfg),
        cloud_path=cloud_path, written_path=str(target_path),
        bytes_written=len(data), sha256=h,
    )


def _load_cart(*, cfg: Config, system: str, game_id: str,
               data: bytes, h: str, cloud_path: str,
               state: StateStore) -> LoadResult:
    src_cfg, source = resolve_cart_source(cfg, system)
    cart_path = cart_path_for_game(state, source, game_id)
    if cart_path is None:
        raise FileNotFoundError(
            f"no cart-side path known for {source.id} / {game_id}; "
            f"play the game on the cart at least once so the daemon "
            f"records the path, or push manually with `retrosync push`.")
    written = asyncio.run(_load_to_cart(source, cart_path, data))
    return LoadResult(
        target=system, game_id=game_id, source_id=source.id,
        cloud_path=cloud_path, written_path=written,
        bytes_written=len(data), sha256=h,
    )


def _pocket_source_id(cfg: Config) -> str:
    src = next((s for s in cfg.sources if s.adapter == "pocket"), None)
    return src.id if src is not None else "pocket-1"


def _pocket_system(cfg: Config) -> str:
    src = next((s for s in cfg.sources if s.adapter == "pocket"), None)
    if src is not None:
        return str(src.options.get("system", "snes"))
    return "snes"
