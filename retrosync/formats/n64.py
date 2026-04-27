"""N64 multi-format save translator.

The N64 had five distinct save formats (4-Kbit EEPROM, 16-Kbit EEPROM,
SRAM, FlashRAM, Controller Pak). The EverDrive 64 X7 stores them as
separate per-format files (`.eep`, `.srm`/`.sra`, `.fla`,
`.mpk`/`.mp1`–`.mp4`) under `/ED64/gamedata/` (older firmware:
`/ED64/SAVES/`). Mupen64Plus (the libretro core RetroArch on the
Deck uses) stores them packed into a single 296,960-byte combined
`.srm` at fixed offsets.

Note the SRAM extension collision: real EverDrive X7 firmware writes
SRAM as `.srm` (NOT the `.sra` documented in some open-source
references). On the cart side, a `.srm` is a raw 32 KB SRAM dump for
one game; on the cloud side, `.srm` is the combined 296,960-byte
mupen64plus blob. They live in different namespaces (per-game cart
file vs. canonical cloud format) so the overload doesn't actually
collide in code.

This module translates between the two layouts. Cloud's canonical
storage is the combined form (matches `SYSTEM_CANONICAL_EXTENSION`'s
`{"snes": ".srm"}` pattern), so EverDrive uploads `combine()` first
and downloads `split()` first.

Layout (verified against libretro `mupen64plus-libretro-nx` 2.5.x —
see `mupen64plus-core/src/main/savestates.c`):

    Offset      Size        Format
    0x00000     0x00800     EEPROM (16 Kbit max; 4 Kbit games use first 0x200)
    0x00800     0x08000     SRAM (32 KB)
    0x08800     0x20000     FlashRAM (128 KB)
    0x28800     0x08000     Controller Pak port 1
    0x30800     0x08000     Controller Pak port 2
    0x38800     0x08000     Controller Pak port 3
    0x40800     0x08000     Controller Pak port 4
    [end at 0x48800 = 296,960]

Empty regions are zero-filled. A game that uses only EEPROM has
0x00000–0x007FF populated and zeros for the remaining 296 KB.

Real-world `.srm` files in the wild may be **truncated** to exactly
the bytes a game uses (some emulator builds do this). `split()` is
tolerant — it pads short inputs with zeros for missing regions and
returns `None` for any region whose bytes are entirely zero, so
callers don't write empty per-format files to the EverDrive.
`combine()` always emits exactly 296,960 bytes.
"""
from __future__ import annotations

from dataclasses import dataclass

# Region offsets and sizes per the libretro layout above.
EEPROM_OFFSET   = 0x00000
EEPROM_SIZE     = 0x00800   # 2 KB (covers 4-Kbit and 16-Kbit)

SRAM_OFFSET     = 0x00800
SRAM_SIZE       = 0x08000   # 32 KB

FLASHRAM_OFFSET = 0x08800
FLASHRAM_SIZE   = 0x20000   # 128 KB

CPAK_BASE_OFFSET = 0x28800
CPAK_SIZE        = 0x08000  # 32 KB per port
CPAK_PORTS       = 4

# Per-port Controller Pak offsets, derived from the base + index.
CPAK_OFFSETS = tuple(
    CPAK_BASE_OFFSET + i * CPAK_SIZE for i in range(CPAK_PORTS))

# Total combined-srm size. Sanity-asserted at module load.
COMBINED_SIZE = 0x48800
assert COMBINED_SIZE == CPAK_BASE_OFFSET + CPAK_PORTS * CPAK_SIZE
assert COMBINED_SIZE == 296_960

# Native sizes the EverDrive's per-format files use. EEPROM is special:
# the file's actual size depends on which variant the game uses
# (4 Kbit = 512 B, 16 Kbit = 2 KB), and the EverDrive firmware writes
# the natural size — there's no padding.
EEPROM_4KBIT_BYTES  = 0x200      # 512 bytes
EEPROM_16KBIT_BYTES = 0x800      # 2 KB

# Per-format file extensions on the EverDrive's SD card.
EXT_EEPROM    = ".eep"
# Real EverDrive 64 X7 firmware writes SRAM as `.srm`. Some older
# firmware (and a number of open-source references) use `.sra`. We
# accept both on read and default to `.srm` on write — see
# `EverDrive64Config.sram_extension` for the override.
EXT_SRAM      = ".srm"
EXT_SRAM_LEGACY = ".sra"
ALL_SRAM_EXTENSIONS = (EXT_SRAM, EXT_SRAM_LEGACY)
EXT_FLASHRAM  = ".fla"
EXT_CPAK_GENERIC = ".mpk"        # ports collapsed; older firmware
EXT_CPAK_PER_PORT = (".mp1", ".mp2", ".mp3", ".mp4")  # newer firmware

ALL_N64_SAVE_EXTENSIONS = (
    EXT_EEPROM, EXT_SRAM, EXT_SRAM_LEGACY, EXT_FLASHRAM,
    EXT_CPAK_GENERIC, *EXT_CPAK_PER_PORT,
)


@dataclass(frozen=True)
class N64SaveSet:
    """Logical bundle of save data for one game across all formats.

    `eeprom` holds whatever native size the game uses (512 B or 2 KB).
    `sram` is exactly 32 KB or None. `flashram` is exactly 128 KB or
    None. `cpak` is a 4-element list, each entry exactly 32 KB or None.

    A region of None means "the game doesn't use this format" — the
    EverDrive shouldn't have a file for it, and the combined srm fills
    the corresponding region with zeros.
    """
    eeprom:    bytes | None = None
    sram:      bytes | None = None
    flashram:  bytes | None = None
    cpak:      tuple[bytes | None, bytes | None, bytes | None,
                     bytes | None] = (None, None, None, None)

    def is_empty(self) -> bool:
        return (self.eeprom is None and self.sram is None
                and self.flashram is None
                and all(c is None for c in self.cpak))


def empty_set() -> N64SaveSet:
    """All-None saveset for newly-bootstrapped games."""
    return N64SaveSet()


def combine(save_set: N64SaveSet) -> bytes:
    """Pack a saveset into a 296,960-byte mupen64plus-format `.srm`.

    Empty regions are zero-filled. EEPROM is placed at offset 0 and
    zero-padded to its 2 KB region (so a 4-Kbit save fills 0x000–0x1FF
    with EEPROM data, 0x200–0x7FF with zeros).
    """
    out = bytearray(COMBINED_SIZE)
    if save_set.eeprom is not None:
        if len(save_set.eeprom) > EEPROM_SIZE:
            raise ValueError(
                f"eeprom region overflow: got {len(save_set.eeprom)} bytes, "
                f"max {EEPROM_SIZE}")
        out[EEPROM_OFFSET:EEPROM_OFFSET + len(save_set.eeprom)] = save_set.eeprom
    if save_set.sram is not None:
        if len(save_set.sram) != SRAM_SIZE:
            raise ValueError(
                f"sram must be exactly {SRAM_SIZE} bytes, "
                f"got {len(save_set.sram)}")
        out[SRAM_OFFSET:SRAM_OFFSET + SRAM_SIZE] = save_set.sram
    if save_set.flashram is not None:
        if len(save_set.flashram) != FLASHRAM_SIZE:
            raise ValueError(
                f"flashram must be exactly {FLASHRAM_SIZE} bytes, "
                f"got {len(save_set.flashram)}")
        out[FLASHRAM_OFFSET:FLASHRAM_OFFSET + FLASHRAM_SIZE] = save_set.flashram
    for i, cpak_bytes in enumerate(save_set.cpak):
        if cpak_bytes is None:
            continue
        if len(cpak_bytes) != CPAK_SIZE:
            raise ValueError(
                f"cpak port {i + 1} must be exactly {CPAK_SIZE} bytes, "
                f"got {len(cpak_bytes)}")
        offset = CPAK_OFFSETS[i]
        out[offset:offset + CPAK_SIZE] = cpak_bytes
    return bytes(out)


def split(srm: bytes) -> N64SaveSet:
    """Unpack a `.srm` into a saveset.

    Tolerates short inputs by zero-padding to COMBINED_SIZE. Returns
    None for any region whose bytes are entirely zero, so the caller
    doesn't write empty per-format files to the EverDrive. Refuses
    inputs longer than COMBINED_SIZE — that's almost certainly a
    different format altogether.
    """
    if len(srm) > COMBINED_SIZE:
        raise ValueError(
            f"srm too large: got {len(srm)} bytes, max {COMBINED_SIZE}")
    if len(srm) < COMBINED_SIZE:
        srm = srm + b"\x00" * (COMBINED_SIZE - len(srm))

    def slice_or_none(off: int, size: int) -> bytes | None:
        chunk = srm[off:off + size]
        if all(b == 0 for b in chunk):
            return None
        return chunk

    eeprom = slice_or_none(EEPROM_OFFSET, EEPROM_SIZE)
    # If the EEPROM region is mostly zero with content only in the
    # first 0x200 bytes, the game uses 4-Kbit EEPROM. Trim trailing
    # zeros so the per-format file matches the game's natural size.
    # (combine() re-pads on the way back, so the round-trip is exact.)
    if eeprom is not None:
        eeprom = _trim_trailing_zeros_to_natural_eeprom(eeprom)

    cpak = tuple(
        slice_or_none(off, CPAK_SIZE) for off in CPAK_OFFSETS)
    # mypy/typing: cast the 4-tuple shape explicitly
    cpak4 = (cpak[0], cpak[1], cpak[2], cpak[3])

    return N64SaveSet(
        eeprom=eeprom,
        sram=slice_or_none(SRAM_OFFSET, SRAM_SIZE),
        flashram=slice_or_none(FLASHRAM_OFFSET, FLASHRAM_SIZE),
        cpak=cpak4,
    )


def _trim_trailing_zeros_to_natural_eeprom(eeprom: bytes) -> bytes:
    """Trim a 2 KB EEPROM region to its natural size (512 B or 2 KB).

    A 4-Kbit EEPROM game has data only in the first 512 bytes; the
    rest of the 2 KB region is zero. Detect that and return just the
    first 512 bytes so the per-format `.eep` file matches what the
    EverDrive's firmware natively writes.

    A 16-Kbit EEPROM game uses the full 2 KB. Return as-is. We err on
    the side of "16 Kbit" if any byte past offset 0x1FF is non-zero.
    """
    assert len(eeprom) == EEPROM_SIZE
    if all(b == 0 for b in eeprom[EEPROM_4KBIT_BYTES:]):
        return eeprom[:EEPROM_4KBIT_BYTES]
    return eeprom


def cpak_port_extension(port: int) -> str:
    """Return the per-port Controller Pak file extension for `port` (1–4).

    The EverDrive firmware writes per-port files as `.mp1` … `.mp4`.
    Older firmware may use `.mpk` for port 1 only. We always emit the
    per-port form on writes; reads accept either.
    """
    if not 1 <= port <= CPAK_PORTS:
        raise ValueError(f"port must be 1..4, got {port}")
    return EXT_CPAK_PER_PORT[port - 1]
