"""EmuDeck system catalog.

Single source of truth for the per-system conventions setup-deck.sh
and `retrosync deck add-source` use to write `emudeck` source blocks.
Adding a new system means adding one entry here — no installer code
changes.

`save_extension` is what RetroArch writes for the system. For multi-
format systems like N64, this is the combined cloud-canonical
extension (cloud always stores `.srm`, the cart-side adapter splits
into per-format files at the device).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmuDeckSystem:
    name: str                       # YAML `system:` value
    rom_extensions: tuple[str, ...]
    save_extension: str
    description: str                # human-readable, for log lines


SYSTEMS: tuple[EmuDeckSystem, ...] = (
    EmuDeckSystem(
        name="snes",
        rom_extensions=(".sfc", ".smc", ".swc", ".fig"),
        save_extension=".srm",
        description="Super Nintendo (snes9x / bsnes via RetroArch)",
    ),
    EmuDeckSystem(
        name="n64",
        rom_extensions=(".z64", ".n64", ".v64"),
        save_extension=".srm",
        description="Nintendo 64 (mupen64plus-next via RetroArch)",
    ),
    EmuDeckSystem(
        name="gba",
        rom_extensions=(".gba",),
        save_extension=".srm",
        description="Game Boy Advance (mGBA via RetroArch)",
    ),
    EmuDeckSystem(
        name="genesis",
        rom_extensions=(".md", ".gen", ".smd", ".bin"),
        save_extension=".srm",
        description="Sega Genesis / Mega Drive (Genesis Plus GX)",
    ),
)


SYSTEMS_BY_NAME: dict[str, EmuDeckSystem] = {s.name: s for s in SYSTEMS}


def get(name: str) -> EmuDeckSystem:
    """Lookup by name, raise ValueError with the supported names listed."""
    s = SYSTEMS_BY_NAME.get(name)
    if s is None:
        raise ValueError(
            f"unknown system {name!r}; supported: "
            f"{', '.join(s.name for s in SYSTEMS)}")
    return s
