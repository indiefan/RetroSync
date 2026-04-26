"""FXPak Pro source adapter (SNES, via usb2snes over USB).

Save files on the FXPak Pro live as <ROM-stem>.srm in the same directory as
the ROM file. We discover them by listing the cart's SD recursively and
filtering on extension.

Game ID strategy: <crc32>_<slug>, where the CRC32 comes from the ROM bytes
the first time we encounter a save, and is cached in a small JSON file
beside the state DB. The slug is the ROM filename minus extension,
lowercased and made FS-safe. CRC alone is unfriendly to humans; slug alone
is ambiguous across regions/revisions.
"""
from __future__ import annotations

import json
import logging
import os
import re
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath

from .base import HealthStatus, SaveRef, SourceError
from .registry import register
from .usb2snes import Usb2SnesClient, Usb2SnesError

log = logging.getLogger(__name__)

SRM_SUFFIX = ".srm"
# A few common ROM extensions; we use these to find the partner ROM for CRC.
ROM_EXTS = (".sfc", ".smc", ".swc", ".fig")


@dataclass
class FXPakConfig:
    id: str
    sni_url: str = "ws://127.0.0.1:23074"
    sd_root: str = "/"
    save_extensions: tuple[str, ...] = (SRM_SUFFIX,)
    cache_dir: str = "/var/lib/retrosync/fxpak-cache"


class FXPakSource:
    """SaveSource implementation for the FXPak Pro flash cart.

    `id` and `system` are public attributes per the SaveSource protocol.
    """

    system = "snes"

    def __init__(self, config: FXPakConfig):
        self._cfg = config
        self.id = config.id
        self._game_id_cache: dict[str, str] = {}
        os.makedirs(self._cfg.cache_dir, exist_ok=True)
        self._cache_file = os.path.join(self._cfg.cache_dir, "game_ids.json")
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            with open(self._cache_file) as fp:
                self._game_id_cache = json.load(fp)
        except (OSError, json.JSONDecodeError):
            self._game_id_cache = {}

    def _save_cache(self) -> None:
        tmp = self._cache_file + ".tmp"
        with open(tmp, "w") as fp:
            json.dump(self._game_id_cache, fp)
        os.replace(tmp, self._cache_file)

    # ----------- SaveSource methods -----------

    async def health(self) -> HealthStatus:
        try:
            async with Usb2SnesClient(self._cfg.sni_url) as cart:
                devs = await cart.device_list()
                if not devs:
                    return HealthStatus(False, "no usb2snes devices attached")
                await cart.attach(devs[0])
                info = await cart.info()
                return HealthStatus(True,
                    f"device={devs[0]} firmware={info.get('firmware','?')}")
        except Usb2SnesError as exc:
            return HealthStatus(False, f"sni unreachable: {exc}")

    async def list_saves(self) -> list[SaveRef]:
        try:
            async with Usb2SnesClient(self._cfg.sni_url) as cart:
                await cart.attach()
                paths = await cart.list_recursive(self._cfg.sd_root)
        except Usb2SnesError as exc:
            raise SourceError(str(exc)) from exc

        saves: list[SaveRef] = []
        suffixes = tuple(self._cfg.save_extensions)
        for p in paths:
            if p.lower().endswith(suffixes):
                saves.append(SaveRef(path=p))
        log.debug("FXPak %s: found %d save files", self.id, len(saves))
        return saves

    async def read_save(self, ref: SaveRef) -> bytes:
        try:
            async with Usb2SnesClient(self._cfg.sni_url) as cart:
                await cart.attach()
                return await cart.get_file(ref.path)
        except Usb2SnesError as exc:
            raise SourceError(str(exc)) from exc

    async def write_save(self, ref: SaveRef, data: bytes) -> None:
        try:
            async with Usb2SnesClient(self._cfg.sni_url) as cart:
                await cart.attach()
                await cart.put_file(ref.path, data)
        except Usb2SnesError as exc:
            raise SourceError(str(exc)) from exc

    def resolve_game_id(self, ref: SaveRef) -> str:
        """Cached <crc32>_<slug>. Falls back to slug-only if CRC unavailable."""
        cached = self._game_id_cache.get(ref.path)
        if cached:
            return cached
        # Without an actual ROM read we can't compute CRC here synchronously.
        # Defer to async helper below; orchestrator calls async_resolve when
        # populating the manifest. For the synchronous fallback we slug only.
        slug = self._slug_from_save_path(ref.path)
        return f"unknown_{slug}"

    async def async_resolve_game_id(self, ref: SaveRef) -> str:
        """Async variant that reads the partner ROM to compute CRC32."""
        cached = self._game_id_cache.get(ref.path)
        if cached:
            return cached
        slug = self._slug_from_save_path(ref.path)
        crc = await self._fetch_rom_crc(ref.path)
        if crc is None:
            game_id = f"unknown_{slug}"
        else:
            game_id = f"{crc:08x}_{slug}"
        self._game_id_cache[ref.path] = game_id
        self._save_cache()
        return game_id

    # ----------- helpers -----------

    @staticmethod
    def _slug_from_save_path(save_path: str) -> str:
        stem = PurePosixPath(save_path).stem
        slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
        return slug or "unnamed"

    async def _fetch_rom_crc(self, save_path: str) -> int | None:
        """Find the ROM next to the save and return its CRC32, or None."""
        save_dir = str(PurePosixPath(save_path).parent)
        save_stem = PurePosixPath(save_path).stem
        try:
            async with Usb2SnesClient(self._cfg.sni_url) as cart:
                await cart.attach()
                entries = await cart.list(save_dir or "/")
                for ext in ROM_EXTS:
                    candidate = save_stem + ext
                    for e in entries:
                        if e.is_file and e.name.lower() == candidate.lower():
                            rom_path = (save_dir.rstrip("/") + "/" + e.name)
                            data = await cart.get_file(rom_path)
                            return zlib.crc32(data) & 0xFFFFFFFF
        except Usb2SnesError as exc:
            log.warning("CRC lookup failed for %s: %s", save_path, exc)
        return None


def _build(*, id: str, sni_url: str = "ws://127.0.0.1:23074",
           sd_root: str = "/",
           save_extensions: list[str] | None = None,
           cache_dir: str = "/var/lib/retrosync/fxpak-cache") -> FXPakSource:
    return FXPakSource(FXPakConfig(
        id=id, sni_url=sni_url, sd_root=sd_root,
        save_extensions=tuple(save_extensions or [SRM_SUFFIX]),
        cache_dir=cache_dir,
    ))


register("fxpak", _build)
