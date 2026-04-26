"""FXPak Pro source adapter (SNES, via usb2snes over USB).

Save files on the FXPak Pro live as <ROM-stem>.srm. With the firmware's
default settings they sit alongside the ROM, but the "Saves Directory"
option redirects them to a single folder (e.g. /sd2snes/saves). We discover
saves by walking the SD recursively from `sd_root`, and we discover the
partner ROM by first looking next to the save and then, if absent, by
walking `rom_root` once per process.

Game ID strategy: <crc32>_<slug>, where the CRC32 comes from the ROM bytes
the first time we encounter a save, and is cached in a small JSON file
beside the state DB. The slug is the ROM filename minus extension,
lowercased and made FS-safe. CRC alone is unfriendly to humans; slug alone
is ambiguous across regions/revisions. When the CRC cannot be computed
(e.g. the cart is mid-detach) we return `unknown_<slug>` for the current
pass but DO NOT cache it — the next pass will retry.
"""
from __future__ import annotations

import asyncio
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
    # Where to look for ROMs when the partner ROM isn't sitting in the same
    # directory as the save. Matches the FXPak firmware's "Saves Directory"
    # feature, which puts every .srm under one folder regardless of where
    # the ROM lives. Walked once per process and memoized.
    rom_root: str = "/"


class FXPakSource:
    """SaveSource implementation for the FXPak Pro flash cart.

    `id` and `system` are public attributes per the SaveSource protocol.
    """

    system = "snes"

    def __init__(self, config: FXPakConfig):
        self._cfg = config
        self.id = config.id
        self._game_id_cache: dict[str, str] = {}
        # Lazy index of {rom-stem-lowercase: full-rom-path} under rom_root,
        # built on the first miss in a save's parent directory and reused
        # for every subsequent miss within this process.
        self._rom_index: dict[str, str] | None = None
        self._rom_index_lock = asyncio.Lock()
        os.makedirs(self._cfg.cache_dir, exist_ok=True)
        self._cache_file = os.path.join(self._cfg.cache_dir, "game_ids.json")
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            with open(self._cache_file) as fp:
                raw = json.load(fp)
        except (OSError, json.JSONDecodeError):
            self._game_id_cache = {}
            return
        # Self-heal: drop any `unknown_*` entries left by older daemons that
        # cached the CRC-miss fallback. Modern code only caches successful
        # lookups, so on next poll these will be retried and (typically)
        # resolve to a real <crc32>_<slug>.
        purged = {k: v for k, v in raw.items() if not v.startswith("unknown_")}
        if len(purged) != len(raw):
            log.info("FXPak %s: dropped %d poisoned 'unknown_*' cache entries",
                     self.id, len(raw) - len(purged))
        self._game_id_cache = purged

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
        """Async variant that reads the partner ROM to compute CRC32.

        On a CRC miss we return `unknown_<slug>` for this call but do NOT
        write it to the cache — caching the fallback would make the bad ID
        sticky across restarts. Successful lookups are persisted.
        """
        cached = self._game_id_cache.get(ref.path)
        if cached:
            return cached
        slug = self._slug_from_save_path(ref.path)
        crc = await self._fetch_rom_crc(ref.path)
        if crc is None:
            return f"unknown_{slug}"
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
        """Find the partner ROM and return its CRC32, or None.

        Search order:
          1. Same directory as the save (typical when saves live next to ROMs).
          2. Recursive walk of `rom_root` (typical when the FXPak's "Saves
             Directory" feature redirects every .srm into one folder).
        The walk is memoized so it only runs once per process.
        """
        save_dir = str(PurePosixPath(save_path).parent) or "/"
        save_stem = PurePosixPath(save_path).stem
        try:
            async with Usb2SnesClient(self._cfg.sni_url) as cart:
                await cart.attach()

                entries = await cart.list(save_dir)
                rom_path = self._match_rom_in_entries(
                    entries, save_dir, save_stem)

                if rom_path is None and self._cfg.rom_root:
                    index = await self._ensure_rom_index(cart)
                    rom_path = index.get(save_stem.lower())

                if rom_path is None:
                    seen = sorted(e.name for e in entries if e.is_file)
                    expected = [save_stem + ext for ext in ROM_EXTS]
                    log.info(
                        "no ROM match for %s — looked in %s (saw %d file(s): "
                        "%s) and rom_root=%s; expected one of %s",
                        save_path, save_dir, len(seen), seen[:20],
                        self._cfg.rom_root, expected,
                    )
                    return None

                data = await cart.get_file(rom_path)
                return zlib.crc32(data) & 0xFFFFFFFF
        except Usb2SnesError as exc:
            log.warning("CRC lookup failed for %s: %s", save_path, exc)
        return None

    @staticmethod
    def _match_rom_in_entries(entries, save_dir: str, save_stem: str) -> str | None:
        for ext in ROM_EXTS:
            candidate = (save_stem + ext).lower()
            for e in entries:
                if e.is_file and e.name.lower() == candidate:
                    return save_dir.rstrip("/") + "/" + e.name
        return None

    async def _ensure_rom_index(self, cart: Usb2SnesClient) -> dict[str, str]:
        if self._rom_index is not None:
            return self._rom_index
        async with self._rom_index_lock:
            if self._rom_index is not None:
                return self._rom_index
            log.info("FXPak %s: indexing ROMs under %s (one-time scan)",
                     self.id, self._cfg.rom_root)
            paths = await cart.list_recursive(self._cfg.rom_root)
            index: dict[str, str] = {}
            for p in paths:
                if p.lower().endswith(ROM_EXTS):
                    stem = PurePosixPath(p).stem.lower()
                    # First match wins; collisions across regions/revisions
                    # are unavoidable without filename normalization, but
                    # the slug differentiates in the final game-id anyway.
                    index.setdefault(stem, p)
            log.info("FXPak %s: indexed %d ROM(s) from %s",
                     self.id, len(index), self._cfg.rom_root)
            self._rom_index = index
            return index


def _build(*, id: str, sni_url: str = "ws://127.0.0.1:23074",
           sd_root: str = "/",
           save_extensions: list[str] | None = None,
           cache_dir: str = "/var/lib/retrosync/fxpak-cache",
           rom_root: str = "/") -> FXPakSource:
    return FXPakSource(FXPakConfig(
        id=id, sni_url=sni_url, sd_root=sd_root,
        save_extensions=tuple(save_extensions or [SRM_SUFFIX]),
        cache_dir=cache_dir, rom_root=rom_root,
    ))


register("fxpak", _build)
