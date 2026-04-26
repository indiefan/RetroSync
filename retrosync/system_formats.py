"""Per-system save-format registry.

Most systems are single-file: the device's save bytes are the cloud's
bytes, no translation. They live in this registry with `combine=None`
and `split=None` — the engine treats them as opaque blobs.

Multi-format systems (N64 today, Saturn/Dreamcast in the future)
register a `combine`/`split` pair from `retrosync.formats.<system>`.
The engine pre-processes uploads through `combine` and post-processes
downloads through `split` so the cloud-side bytes are always the
canonical combined form.

Adding a new single-file system:
  1. Pick a system string.
  2. Add to `SYSTEM_CANONICAL_EXTENSION` in cloud.py.
  3. Add a `SystemFormat(canonical_extension=...)` entry below.

That's the entire change for SNES, GB, GBA, Genesis, etc.

Adding a new multi-format system:
  4. Implement combine/split in `retrosync/formats/<system>.py`.
  5. Pass them to the SystemFormat entry above.
  6. Adapters that store per-file saves override `group_refs` to
     group by game_id and expose a `read_saveset / write_saveset`
     pair — see `EverDrive64Source` for the reference shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import cloud as _cloud
from .formats import n64 as _n64


@dataclass(frozen=True)
class SystemFormat:
    """Per-system canonical-format metadata.

    `canonical_extension`: the cloud-side `current.<ext>` extension
        (kept in lock-step with `cloud.SYSTEM_CANONICAL_EXTENSION`).
    `combine`: device-saveset → cloud-bytes. None for single-file
        systems where device bytes ARE the cloud bytes.
    `split`: cloud-bytes → device-saveset. None for single-file systems.
    """
    canonical_extension: str
    combine: Callable[[Any], bytes] | None = None
    split: Callable[[bytes], Any] | None = None

    @property
    def is_multi_format(self) -> bool:
        return self.combine is not None and self.split is not None


SYSTEM_FORMATS: dict[str, SystemFormat] = {
    "snes": SystemFormat(canonical_extension=".srm"),
    "n64": SystemFormat(
        canonical_extension=".srm",
        combine=_n64.combine,
        split=_n64.split,
    ),
    # Future single-file systems are one line each:
    # "genesis": SystemFormat(canonical_extension=".srm"),
    # "gba":     SystemFormat(canonical_extension=".srm"),
    # "gb":      SystemFormat(canonical_extension=".sav"),
    # "saturn":  SystemFormat(canonical_extension=".bin",
    #                         combine=_saturn.combine, split=_saturn.split),
}


def for_system(system: str) -> SystemFormat:
    """Look up the SystemFormat for `system`. Raises KeyError if
    unknown — adapters should refuse to instantiate against an
    unregistered system rather than silently using an unconfigured
    default."""
    if system not in SYSTEM_FORMATS:
        raise KeyError(
            f"system {system!r} not registered in SYSTEM_FORMATS — "
            f"add an entry to retrosync/system_formats.py "
            f"(see module docstring for the recipe).")
    return SYSTEM_FORMATS[system]


def is_multi_format(system: str) -> bool:
    """Convenience predicate. Adapters/engine use this to decide
    whether to round-trip device bytes through combine/split."""
    return for_system(system).is_multi_format


# Sanity-check that the SystemFormat extensions match the
# cloud-canonical mapping at import time. They must be in lock-step
# so a typo in either one fails fast rather than producing bogus
# cloud paths.
for _sys, _fmt in SYSTEM_FORMATS.items():
    if _cloud.SYSTEM_CANONICAL_EXTENSION.get(_sys) != _fmt.canonical_extension:
        raise RuntimeError(
            f"SystemFormat / SYSTEM_CANONICAL_EXTENSION mismatch for "
            f"{_sys}: registry={_fmt.canonical_extension!r}, "
            f"cloud={_cloud.SYSTEM_CANONICAL_EXTENSION.get(_sys)!r}")
