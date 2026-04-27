"""EverDrive 64 X7 SaveSource adapter.

The EverDrive's SD card stores N64 saves as one file per format
(`.eep`, `.srm`/`.sra`, `.fla`, `.mp1`–`.mp4`) under
`/ED64/gamedata/` (older firmware: `/ED64/SAVES/`). The adapter:

  - Lists every per-format file as a separate `SaveRef`.
  - `group_refs` groups refs by canonical game-id (so all of
    `Foo.eep` + `Foo.mp1` for one game land in one group).
  - `read_canonical_bytes` aggregates a group's per-format files
    into an `N64SaveSet`, then `n64.combine()`s into a 296,960-byte
    cloud-form blob.
  - `write_canonical_bytes` does the inverse — `n64.split()`, then
    writes each populated region to its per-format file (and deletes
    files for regions that have become empty).
  - `target_save_paths_for(game_id)` walks `/ED64/ROMS/` to find a
    matching ROM stem, returns one entry per save extension.

The wire-level USB transport lives in
`retrosync.transport.krikzz_ftdi`. The adapter consumes it as a
service — no protocol bytes appear in this file.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ...formats import n64
from ...game_id import resolve_game_id
from ...transport.krikzz_ftdi import (
    KrikzzFtdiTransport, MockKrikzzTransport, build_transport,
)
from ..base import HealthStatus, SaveRef, SourceError
from ..registry import register

log = logging.getLogger(__name__)


@dataclass
class EverDrive64Config:
    id: str
    # Default `serial` matches the FT232 / kernel-bound case verified
    # on the operator's EverDrive 64 X7. Use `pyftdi` only for
    # FT245R-equipped carts where the kernel hasn't claimed the device.
    transport: str = "serial"  # | pyftdi | unfloader | mock
    serial_path: str = "/dev/ttyUSB0"
    serial_baud: int = 9600
    ftdi_url: str = "ftdi://ftdi:0x6001/1"
    # Real EverDrive 64 X7 OS firmware writes saves to /ED64/gamedata/,
    # NOT the /ED64/SAVES/ that some open-source references mention.
    # Operator can override if their firmware variant differs.
    sd_saves_root: str = "/ED64/gamedata"
    sd_roms_root: str = "/ED64/ROMS"
    rom_extensions: tuple[str, ...] = (".z64", ".n64", ".v64")
    # SRAM extension to use when writing fresh SRAM saves. EverDrive
    # OS firmware writes `.srm`; some older / open-source variants
    # write `.sra`. Reads accept both regardless of this setting.
    sram_write_extension: str = n64.EXT_SRAM
    region_preference: tuple[str, ...] = (
        "usa", "world", "europe", "japan")
    game_aliases: dict[str, list[str]] = field(default_factory=dict)
    # Workaround for the missing dir-list operation in Krikzz's USB
    # tool source. Two ways to feed the adapter the ROM names it
    # needs to derive save filenames:
    #
    #   local_rom_dir: a directory ON THE PI (or wherever the daemon
    #     runs) containing a copy of the operator's N64 ROMs. Most
    #     setups have this anyway. The adapter `os.listdir`s it once
    #     per pass and uses the discovered filenames the same way it
    #     would if dir_list were available. Cheap, zero ongoing
    #     maintenance.
    #
    #   rom_filenames: explicit list. Tedious for big libraries; only
    #     useful when local_rom_dir isn't available (e.g. headless
    #     setup with ROMs only on the cart's SD).
    #
    # Both can be combined — the adapter merges them. Once an OS64
    # dir-list cmd byte is reverse-engineered, both become optional
    # and the cart self-enumerates.
    local_rom_dir: str | None = None
    rom_filenames: tuple[str, ...] = ()
    # Path to a plain-text file listing the cart's gamedata save
    # filenames (one per line, basename only — no directory prefix).
    # The operator generates this once via:
    #
    #   ls /path/to/sd/ED64/gamedata > /etc/retrosync/everdrive64-saves.txt
    #
    # The adapter trusts the listing and uses it as the source of
    # save filenames (same role dir_list would play if the firmware
    # supported it). This handles the case where the cart's saves
    # use a naming convention that differs from the operator's local
    # ROMs (e.g. cart has GoodTools `Foo (U) [!].srm`, ROM library
    # has No-Intro `Foo (USA).z64`) — canonical-slug matching alone
    # can't bridge the gap because we don't know what filenames to
    # try.
    #
    # Falls back to local_rom_dir / rom_filenames probing if this
    # path isn't set or the file doesn't exist. The listing should
    # be re-generated whenever the operator starts a new game on
    # the cart.
    saves_listing_path: str | None = None
    # Maximum seconds the rom_filenames-fallback enumeration can spend
    # probing file_exists across the full ROM × per-format-extension
    # space. For 200+ ROMs × 7 extensions, an unbounded scan can take
    # many minutes; this caps it at a reasonable value and returns
    # whatever was found. Bump if you have a large library AND a
    # responsive cart; lower if you want test-cart to fail fast.
    enumerate_budget_sec: int = 60
    system: str = "n64"
    # When transport=mock, callers can pass an already-constructed
    # MockKrikzzTransport via dependency injection rather than via
    # the registry. Used by tests.
    transport_instance: KrikzzFtdiTransport | None = None
    unfloader_path: str = "/usr/local/bin/UNFLoader"


class EverDrive64Source:
    """SaveSource for the EverDrive 64 X7 over its USB port.

    Public attributes per the SaveSource protocol. `device_kind =
    "n64-everdrive"` so cloud versions land under
    `versions/n64-everdrive/...` and stay distinguishable from
    Deck-authored N64 saves (`versions/deck/...`).
    """

    device_kind = "n64-everdrive"

    def __init__(self, config: EverDrive64Config):
        self._cfg = config
        self.id = config.id
        self.system = config.system
        self._transport: KrikzzFtdiTransport | None = config.transport_instance
        self._opened = False
        # Per-pass cache of resolved game_ids per save filename.
        # Cleared at health-check time; a new pass repopulates.
        self._game_id_cache: dict[str, str] = {}
        # Per-pass cache of ROM stems for bootstrap-pull file naming.
        # game_id → ROM stem (without extension).
        self._rom_stem_cache: dict[str, str] = {}

    # ----------- internals -----------

    def _ensure_transport(self) -> KrikzzFtdiTransport:
        if self._transport is None:
            opts: dict = {}
            if self._cfg.transport == "serial":
                opts["serial_path"] = self._cfg.serial_path
                opts["baud"] = self._cfg.serial_baud
            elif self._cfg.transport == "pyftdi":
                opts["ftdi_url"] = self._cfg.ftdi_url
            elif self._cfg.transport == "unfloader":
                opts["unfloader_path"] = self._cfg.unfloader_path
            self._transport = build_transport(
                kind=self._cfg.transport, **opts)
        return self._transport

    async def _open(self) -> KrikzzFtdiTransport:
        t = self._ensure_transport()
        if not self._opened:
            try:
                await t.open()
                self._opened = True
            except Exception as exc:  # noqa: BLE001
                raise SourceError(f"opening EverDrive 64 USB: {exc}") from exc
        return t

    # ----------- SaveSource methods -----------

    async def health(self) -> HealthStatus:
        try:
            t = await self._open()
            ok, detail = await t.health()
        except SourceError as exc:
            return HealthStatus(False, str(exc))
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(False, f"transport error: {exc}")
        # New pass; flush the per-pass caches so a re-scan picks up
        # any new ROMs / saves the operator added.
        self._game_id_cache.clear()
        self._rom_stem_cache.clear()
        return HealthStatus(ok, detail)

    async def list_saves(self) -> list[SaveRef]:
        t = await self._open()
        # Strategy chain (most authoritative first):
        #   1. dir_list  — only Mock and future firmware-supported
        #      backends can do this; gives us the cart's directory
        #      directly.
        #   2. saves_listing_path sidecar — operator-supplied list
        #      of cart-side save filenames. Trusted as-is. Useful
        #      when the cart's naming convention doesn't match the
        #      operator's local ROMs (e.g. GoodTools vs No-Intro).
        #   3. ROM-stem probing — derive expected save filenames
        #      from local_rom_dir / rom_filenames and probe each
        #      with file_exists. Only works if cart's save names
        #      match ROM stems byte-for-byte.
        try:
            entries = await t.dir_list(self._cfg.sd_saves_root)
        except NotImplementedError:
            sidecar = self._read_sidecar_listing()
            if sidecar:
                log.info("EverDrive 64 %s: using sidecar listing (%d "
                         "entries from %s)",
                         self.id, len(sidecar),
                         self._cfg.saves_listing_path)
                return sidecar
            return await self._list_saves_via_rom_filenames()
        except Exception as exc:  # noqa: BLE001
            raise SourceError(
                f"listing {self._cfg.sd_saves_root}: {exc}") from exc
        out: list[SaveRef] = []
        for e in entries:
            if e.is_dir:
                continue
            ext = ("." + e.name.rsplit(".", 1)[-1].lower()
                   if "." in e.name else "")
            if ext not in n64.ALL_N64_SAVE_EXTENSIONS:
                continue
            out.append(SaveRef(
                path=f"{self._cfg.sd_saves_root}/{e.name}",
                size_bytes=e.size,
            ))
        return out

    def _read_sidecar_listing(self) -> list[SaveRef]:
        """Read the operator-supplied sidecar listing. Each non-blank,
        non-`#` line is a basename like `Mario Golf (U) [!].srm`.
        Returns [] if the path isn't configured or the file is
        missing — caller falls back to ROM-stem probing.
        """
        if not self._cfg.saves_listing_path:
            return []
        from pathlib import Path
        path = Path(self._cfg.saves_listing_path)
        if not path.exists():
            log.debug("EverDrive 64 %s: saves_listing_path %s not found",
                      self.id, path)
            return []
        try:
            lines = path.read_text().splitlines()
        except OSError as exc:
            log.warning("EverDrive 64 %s: reading sidecar %s failed: %s",
                        self.id, path, exc)
            return []
        out: list[SaveRef] = []
        for line in lines:
            name = line.strip()
            if not name or name.startswith("#"):
                continue
            ext = ("." + name.rsplit(".", 1)[-1].lower()
                   if "." in name else "")
            if ext not in n64.ALL_N64_SAVE_EXTENSIONS:
                continue
            out.append(SaveRef(
                path=f"{self._cfg.sd_saves_root}/{name}"))
        return out

    def _resolved_rom_names(self) -> list[str]:
        """Combine local_rom_dir scan + explicit rom_filenames into
        a deduplicated list of ROM filenames whose extensions match
        rom_extensions. Used as the de-facto ROM listing whenever the
        cart's transport doesn't support dir_list.
        """
        names: list[str] = []
        seen: set[str] = set()
        rom_exts = tuple(e.lower() for e in self._cfg.rom_extensions)
        if self._cfg.local_rom_dir:
            from pathlib import Path
            try:
                for entry in Path(self._cfg.local_rom_dir).iterdir():
                    if not entry.is_file():
                        continue
                    if not entry.name.lower().endswith(rom_exts):
                        continue
                    if entry.name not in seen:
                        names.append(entry.name)
                        seen.add(entry.name)
            except OSError as exc:
                log.warning(
                    "%s: scanning local_rom_dir %s failed: %s",
                    self.id, self._cfg.local_rom_dir, exc)
        for name in self._cfg.rom_filenames:
            if name not in seen:
                names.append(name)
                seen.add(name)
        return names

    async def _list_saves_via_rom_filenames(self) -> list[SaveRef]:
        """Fallback enumeration when dir_list isn't available.

        Source of ROM names: `_resolved_rom_names()`. For each ROM,
        derive per-format save filenames and probe each with
        file_exists. Emit SaveRef for the ones that exist.

        Resilient to "USB Timeout" cart wedges: if N consecutive
        probes error out, close + reopen + handshake the transport
        and continue. Bounds total enumeration time at
        `enumerate_budget_sec` (default 60s). For large libraries
        (200+ ROMs × 7 extensions), the budget cuts an open-ended
        full scan short and returns whatever it found.
        """
        import time as _time

        rom_names = self._resolved_rom_names()
        if not rom_names:
            log.warning(
                "EverDrive 64 %s: no ROMs visible. Set "
                "options.local_rom_dir to a directory containing "
                "your N64 ROMs, OR list filenames under "
                "options.rom_filenames.",
                self.id)
            return []
        t = await self._open()
        out: list[SaveRef] = []
        consecutive_errors = 0
        max_consecutive = 5
        deadline = _time.monotonic() + self._cfg.enumerate_budget_sec
        probed = 0
        for rom_name in rom_names:
            if _time.monotonic() > deadline:
                log.warning(
                    "EverDrive 64 %s: enumeration budget (%ds) "
                    "exhausted after %d probes; returning %d "
                    "save(s) discovered so far. Bump "
                    "options.enumerate_budget_sec to scan more.",
                    self.id, self._cfg.enumerate_budget_sec,
                    probed, len(out))
                break
            stem = PurePosixPath(rom_name).stem
            for ext in (n64.EXT_EEPROM, n64.EXT_SRAM, n64.EXT_SRAM_LEGACY,
                        n64.EXT_FLASHRAM, *n64.EXT_CPAK_PER_PORT):
                if _time.monotonic() > deadline:
                    break
                path = f"{self._cfg.sd_saves_root}/{stem}{ext}"
                probed += 1
                try:
                    exists = await t.file_exists(path)
                    consecutive_errors = 0
                except Exception as exc:  # noqa: BLE001
                    consecutive_errors += 1
                    log.debug("file_exists(%s) failed (%d in a row): %s",
                              path, consecutive_errors, exc)
                    if consecutive_errors >= max_consecutive:
                        log.warning(
                            "%s: %d consecutive file_exists errors — "
                            "cart likely in USB Timeout state; "
                            "attempting recovery", self.id,
                            consecutive_errors)
                        if not await self._reopen_transport():
                            log.warning(
                                "%s: recovery failed; returning %d "
                                "save(s) discovered so far",
                                self.id, len(out))
                            return out
                        t = self._transport  # type: ignore[assignment]
                        consecutive_errors = 0
                    continue
                if exists:
                    out.append(SaveRef(path=path))
        log.info("EverDrive 64 %s: enumerated %d save(s) from %d probes "
                 "across %d ROM(s)", self.id, len(out), probed,
                 len(rom_names))
        return out

    async def _reopen_transport(self) -> bool:
        """Close and reopen the transport, run a handshake. Used to
        recover from cart "USB Timeout" wedges. Returns True iff the
        cart is back. Resets `_opened` so the next operation
        re-acquires cleanly."""
        import asyncio as _asyncio
        try:
            if self._transport is not None:
                await self._transport.close()
        except Exception:  # noqa: BLE001
            pass
        self._transport = None
        self._opened = False
        await _asyncio.sleep(0.5)
        try:
            t = await self._open()
            ok, _ = await t.health()
            return ok
        except Exception as exc:  # noqa: BLE001
            log.warning("EverDrive 64 reopen failed: %s", exc)
            return False

    async def read_save(self, ref: SaveRef) -> bytes:
        """Read a single per-format file. The engine never calls this
        directly for EverDrive groups (it calls read_canonical_bytes
        instead), but `retrosync push`/`pull` and other CLI tools
        operate on individual files."""
        t = await self._open()
        try:
            return await t.file_read(ref.path)
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"reading {ref.path}: {exc}") from exc

    async def write_save(self, ref: SaveRef, data: bytes) -> None:
        """Write a single per-format file. Same caveat as `read_save`
        — engine uses `write_canonical_bytes` for grouped saves."""
        t = await self._open()
        try:
            await t.file_write(ref.path, data)
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"writing {ref.path}: {exc}") from exc

    def resolve_game_id(self, ref: SaveRef) -> str:
        cached = self._game_id_cache.get(ref.path)
        if cached is not None:
            return cached
        slug = resolve_game_id(PurePosixPath(ref.path).stem,
                               aliases=self._cfg.game_aliases)
        self._game_id_cache[ref.path] = slug
        return slug

    # ----------- multi-file group hooks -----------

    def group_refs(self, refs: list[SaveRef]) -> dict[str, list[SaveRef]]:
        """Group per-format files by canonical game-id.

        For SuperMario64.eep + SuperMario64.mp1, both refs share
        canonical_slug `super_mario_64` and end up in the same group.
        """
        out: dict[str, list[SaveRef]] = defaultdict(list)
        for ref in refs:
            out[self.resolve_game_id(ref)].append(ref)
        # Sort inside each group for stable state.db tracking
        # (orchestrator uses the first ref's path as the conventional
        # state.db key).
        return {k: sorted(v, key=lambda r: r.path) for k, v in out.items()}

    async def read_canonical_bytes(self, refs: list[SaveRef]) -> bytes:
        """Read every per-format file in `refs`, pack into a saveset,
        return the combined 296,960-byte mupen64plus-format blob."""
        if not refs:
            return n64.combine(n64.empty_set())
        ss = await self._read_saveset(refs)
        return n64.combine(ss)

    async def write_canonical_bytes(self, refs: list[SaveRef],
                                    data: bytes) -> None:
        """Split the combined blob into a saveset, write each populated
        region to its per-format file. Existing files for now-empty
        regions are deleted (cleaner than writing zero-length files —
        the EverDrive firmware prefers the absent-file representation).

        `refs` is the current group; we use its first ref to derive
        the ROM stem for filename construction. New per-format files
        are written under that stem.
        """
        ss = n64.split(data)
        if not refs:
            return  # no anchor for filename; bootstrap path uses a
                    # different code path that supplies target paths
                    # explicitly.
        anchor = refs[0]
        stem = PurePosixPath(anchor.path).stem
        await self._write_saveset(stem, ss, current_refs=refs)

    # ----------- adapter-specific (used by orchestrator + tests) -----------

    async def _read_saveset(self, refs: list[SaveRef]) -> n64.N64SaveSet:
        eeprom = sram = flashram = None
        cpak: list[bytes | None] = [None, None, None, None]
        t = await self._open()
        for ref in refs:
            ext = ("." + ref.path.rsplit(".", 1)[-1].lower()
                   if "." in ref.path else "")
            try:
                data = await t.file_read(ref.path)
            except Exception as exc:  # noqa: BLE001
                raise SourceError(
                    f"reading {ref.path}: {exc}") from exc
            if ext == n64.EXT_EEPROM:
                eeprom = data
            elif ext in n64.ALL_SRAM_EXTENSIONS:
                sram = data
            elif ext == n64.EXT_FLASHRAM:
                flashram = data
            elif ext in n64.EXT_CPAK_PER_PORT:
                idx = n64.EXT_CPAK_PER_PORT.index(ext)
                cpak[idx] = data
            elif ext == n64.EXT_CPAK_GENERIC:
                # Older firmware uses a single .mpk for port 1.
                cpak[0] = data
            else:
                log.debug("EverDrive 64: unknown save extension on %s",
                          ref.path)
        return n64.N64SaveSet(
            eeprom=eeprom, sram=sram, flashram=flashram,
            cpak=(cpak[0], cpak[1], cpak[2], cpak[3]),
        )

    async def _write_saveset(self, stem: str, ss: n64.N64SaveSet,
                              *, current_refs: list[SaveRef]) -> None:
        """Write each populated region; delete files for empty regions
        that currently exist on the SD."""
        t = await self._open()
        present = {ref.path for ref in current_refs}

        async def delete_if_present(path: str, reason: str) -> None:
            if path not in present:
                return
            try:
                await t.file_delete(path)
                log.info("EverDrive 64: %s %s", reason, path)
            except Exception as exc:  # noqa: BLE001
                log.warning("delete %s failed: %s", path, exc)

        async def write_or_delete(ext: str, payload: bytes | None,
                                  also_delete_exts: tuple[str, ...] = ()
                                  ) -> None:
            path = f"{self._cfg.sd_saves_root}/{stem}{ext}"
            alt_paths = tuple(
                f"{self._cfg.sd_saves_root}/{stem}{alt}"
                for alt in also_delete_exts if alt != ext)
            if payload is None:
                await delete_if_present(path, "deleted empty")
                for alt_path in alt_paths:
                    await delete_if_present(alt_path, "deleted empty alt")
                return
            try:
                await t.file_write(path, payload)
            except Exception as exc:  # noqa: BLE001
                raise SourceError(f"writing {path}: {exc}") from exc
            for alt_path in alt_paths:
                await delete_if_present(
                    alt_path, f"removed stale alt (superseded by {ext})")

        await write_or_delete(n64.EXT_EEPROM, ss.eeprom)
        # Pick whichever SRAM extension already exists on the cart so
        # we don't accidentally write `.srm` next to an existing `.sra`
        # (or vice versa). Falls back to configured `sram_write_extension`
        # (default `.srm` to match real EverDrive firmware).
        sram_ext = self._cfg.sram_write_extension
        for ref in current_refs:
            ref_ext = ("." + ref.path.rsplit(".", 1)[-1].lower()
                       if "." in ref.path else "")
            if ref_ext in n64.ALL_SRAM_EXTENSIONS:
                sram_ext = ref_ext
                break
        # The "other" SRAM extension we want to clean up if we wrote one.
        sram_alts = tuple(e for e in n64.ALL_SRAM_EXTENSIONS if e != sram_ext)
        await write_or_delete(sram_ext, ss.sram,
                              also_delete_exts=sram_alts)
        await write_or_delete(n64.EXT_FLASHRAM, ss.flashram)
        for idx, cpak_bytes in enumerate(ss.cpak):
            await write_or_delete(n64.EXT_CPAK_PER_PORT[idx], cpak_bytes)

    async def target_save_paths_for(self,
                                    game_id: str) -> dict[str, str | list[str]]:
        """Per the design's generalized API. Returns the per-format
        save paths the cart uses (or would use) for `game_id`.

        Strategy chain:
          1. Sidecar listing — if any entry's basename resolves to
             `game_id`, use its actual stem (cart's existing filename).
             This is the only way to hit the right path when the cart
             uses GoodTools naming and the local ROMs use No-Intro.
          2. ROM-stem derivation — find a ROM in /ED64/ROMS or
             local_rom_dir whose canonical slug matches `game_id`,
             use its stem. Used for fresh games with no save yet.

        Returns an empty dict when no match is found — caller skips
        the bootstrap-pull and logs a warning.
        """
        sidecar = self._read_sidecar_listing()
        for ref in sidecar:
            name = PurePosixPath(ref.path).name
            stem = PurePosixPath(name).stem
            if resolve_game_id(stem,
                               aliases=self._cfg.game_aliases) == game_id:
                self._rom_stem_cache[game_id] = stem
                return self._target_paths_from_stem(stem)
        t = await self._open()
        try:
            entries = await t.dir_list(self._cfg.sd_roms_root)
            rom_names_iter: list[str] = [
                e.name for e in entries if not e.is_dir]
        except NotImplementedError:
            rom_names_iter = self._resolved_rom_names()
            if not rom_names_iter:
                log.warning(
                    "%s: dir_list unsupported and no local_rom_dir / "
                    "rom_filenames configured; can't derive save paths "
                    "for %s", self.id, game_id)
                return {}
        except Exception as exc:  # noqa: BLE001
            log.warning("listing %s for game-id lookup failed: %s",
                        self._cfg.sd_roms_root, exc)
            return {}
        matches: list[str] = []
        rom_exts = tuple(e.lower() for e in self._cfg.rom_extensions)
        for name in rom_names_iter:
            if not name.lower().endswith(rom_exts):
                continue
            slug = resolve_game_id(PurePosixPath(name).stem,
                                   aliases=self._cfg.game_aliases)
            if slug == game_id:
                matches.append(name)
        if not matches:
            return {}
        # Pick by region preference — same logic as filename_map / Pocket.
        chosen = _pick_by_region(matches, self._cfg.region_preference)
        stem = PurePosixPath(chosen).stem
        self._rom_stem_cache[game_id] = stem
        return self._target_paths_from_stem(stem)

    def _target_paths_from_stem(
            self, stem: str) -> dict[str, str | list[str]]:
        """Build the per-format save-paths dict for a given filename
        stem. Covers every possible N64 save format — write_saveset
        will only actually write the ones the saveset has bytes for.
        """
        sram_ext = self._cfg.sram_write_extension
        return {
            "eep":  f"{self._cfg.sd_saves_root}/{stem}{n64.EXT_EEPROM}",
            "sra":  f"{self._cfg.sd_saves_root}/{stem}{sram_ext}",
            "fla":  f"{self._cfg.sd_saves_root}/{stem}{n64.EXT_FLASHRAM}",
            "mpk": [f"{self._cfg.sd_saves_root}/{stem}{ext}"
                    for ext in n64.EXT_CPAK_PER_PORT],
        }


_SINGLE_LETTER_REGIONS = {"u": "usa", "e": "europe", "j": "japan",
                          "w": "world"}


def _pick_by_region(names: list[str],
                    preference: tuple[str, ...]) -> str:
    """Same shape as the Pocket / filename_map region preference logic."""
    def rank(name: str) -> int:
        lname = name.lower()
        for i, want in enumerate(preference):
            if want in lname:
                return i
            for letter, full in _SINGLE_LETTER_REGIONS.items():
                if full == want and (
                        f"({letter})" in lname or f"({letter}," in lname):
                    return i
        return len(preference)
    return sorted(names, key=lambda n: (rank(n), n))[0]


def _build(*, id: str, transport: str = "serial",
           serial_path: str = "/dev/ttyUSB0",
           serial_baud: int = 9600,
           ftdi_url: str = "ftdi://ftdi:0x6001/1",
           sd_saves_root: str = "/ED64/gamedata",
           sd_roms_root: str = "/ED64/ROMS",
           sram_write_extension: str = n64.EXT_SRAM,
           rom_extensions: list[str] | None = None,
           region_preference: list[str] | None = None,
           game_aliases: dict[str, list[str]] | None = None,
           rom_filenames: list[str] | None = None,
           local_rom_dir: str | None = None,
           saves_listing_path: str | None = None,
           enumerate_budget_sec: int = 60,
           system: str = "n64",
           unfloader_path: str = "/usr/local/bin/UNFLoader",
           ) -> EverDrive64Source:
    cfg = EverDrive64Config(
        id=id, transport=transport,
        serial_path=serial_path, serial_baud=serial_baud,
        ftdi_url=ftdi_url,
        sd_saves_root=sd_saves_root, sd_roms_root=sd_roms_root,
        sram_write_extension=sram_write_extension,
        game_aliases=dict(game_aliases or {}),
        rom_filenames=tuple(rom_filenames or ()),
        local_rom_dir=local_rom_dir,
        saves_listing_path=saves_listing_path,
        enumerate_budget_sec=enumerate_budget_sec,
        system=system, unfloader_path=unfloader_path,
    )
    if rom_extensions:
        cfg.rom_extensions = tuple(rom_extensions)
    if region_preference:
        cfg.region_preference = tuple(region_preference)
    return EverDrive64Source(cfg)


register("everdrive64", _build)
