"""FXPak Pro source adapter (SNES, via usb2snes over USB).

Save files on the FXPak Pro live as <ROM-stem>.srm somewhere on the cart's
SD card. We discover them by listing the cart recursively from `sd_root`
and filtering on extension.

Game ID strategy: a "title slug" derived from the save filename with
parenthetical/bracket tags stripped. So `Chrono Trigger (U) [!].srm`
becomes `chrono_trigger`. The full filename (including the stripped
tags) is preserved in the manifest's `save_path` field, so version
provenance isn't lost.

Collisions — two cart paths resolving to the same title slug — are
flagged with a WARN and the alphabetically-first cart path keeps the
clean slug. The rest fall back to their full filename slug. Subfolder
promotion (`chrono_trigger/japan/...`) is left for if/when an operator
actually has multi-region saves to back up.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .base import HealthStatus, SaveRef, SourceError
from .registry import register
from .usb2snes import Usb2SnesClient, Usb2SnesError

log = logging.getLogger(__name__)

SRM_SUFFIX = ".srm"

# Parenthetical or bracketed run, with surrounding whitespace, e.g.
# " (U)" or " [!]". Non-greedy so consecutive runs collapse cleanly.
_TAG_RE = re.compile(r"\s*[\(\[].*?[\)\]]\s*")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class FXPakConfig:
    id: str
    sni_url: str = "ws://127.0.0.1:23074"
    sd_root: str = "/"
    save_extensions: tuple[str, ...] = (SRM_SUFFIX,)


class FXPakSource:
    """SaveSource implementation for the FXPak Pro flash cart.

    `id` and `system` are public attributes per the SaveSource protocol.
    """

    system = "snes"

    def __init__(self, config: FXPakConfig):
        self._cfg = config
        self.id = config.id
        # Populated by list_saves, consumed by resolve_game_id. Path → slug.
        self._slug_assignments: dict[str, str] = {}

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

        self._slug_assignments = self._compute_slug_assignments(
            [s.path for s in saves])
        log.debug("FXPak %s: found %d save file(s)", self.id, len(saves))
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
        """Return the assigned slug, computing it on the fly if needed.

        list_saves populates `_slug_assignments` for the whole pass. For
        callers that arrive without that priming (e.g. an upload of a
        stuck-version row at startup), fall back to the title slug — it
        won't have collision-aware fallback, but the orchestrator's
        next list_saves will fix it within a poll.
        """
        return (self._slug_assignments.get(ref.path)
                or self._title_slug(ref.path))

    # ----------- helpers -----------

    @staticmethod
    def _title_slug(save_path: str) -> str:
        """`Chrono Trigger (U) [!].srm` → `chrono_trigger`."""
        stem = PurePosixPath(save_path).stem
        stripped = _TAG_RE.sub(" ", stem)
        slug = _NON_ALNUM_RE.sub("_", stripped.lower()).strip("_")
        return slug or "unnamed"

    @staticmethod
    def _full_slug(save_path: str) -> str:
        """`Chrono Trigger (U) [!].srm` → `chrono_trigger_u`. Used as a
        collision fallback when two saves share a title slug."""
        stem = PurePosixPath(save_path).stem
        slug = _NON_ALNUM_RE.sub("_", stem.lower()).strip("_")
        return slug or "unnamed"

    def _compute_slug_assignments(self, paths: list[str]) -> dict[str, str]:
        """Map each cart path to its game-id slug, with deterministic
        collision handling (alphabetically-first cart path wins the clean
        slug; others fall back to their full filename slug).
        """
        by_title: dict[str, list[str]] = {}
        for p in paths:
            by_title.setdefault(self._title_slug(p), []).append(p)

        out: dict[str, str] = {}
        for title, group in by_title.items():
            if len(group) == 1:
                out[group[0]] = title
                continue
            group.sort()
            log.warning(
                "FXPak %s: %d cart paths share game-id %r — first one keeps "
                "the clean slug; the rest fall back to their full filename "
                "slug. Paths: %s",
                self.id, len(group), title, group)
            out[group[0]] = title
            for p in group[1:]:
                out[p] = self._full_slug(p)
        return out


def _build(*, id: str, sni_url: str = "ws://127.0.0.1:23074",
           sd_root: str = "/",
           save_extensions: list[str] | None = None,
           # Accepted but ignored — older config.yaml files may still set
           # these. Kept here so an upgrade doesn't crash on stale options.
           cache_dir: str | None = None,
           rom_root: str | None = None) -> FXPakSource:
    return FXPakSource(FXPakConfig(
        id=id, sni_url=sni_url, sd_root=sd_root,
        save_extensions=tuple(save_extensions or [SRM_SUFFIX]),
    ))


register("fxpak", _build)
