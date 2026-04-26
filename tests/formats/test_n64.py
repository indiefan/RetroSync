"""N64 combine/split round-trip + boundary tests.

The translator is the highest-risk new code in v0.4 — every other
piece of N64 sync depends on these bytes being arranged correctly.
Heavy on round-trip property tests + a few real-shape fixtures
(EEPROM-only, SRAM-only, FlashRAM-only, multi-format).
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from retrosync.formats import n64  # noqa: E402


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


def test_combine_emits_exact_size() -> bool:
    """combine() always emits exactly 296,960 bytes regardless of input."""
    blob = n64.combine(n64.empty_set())
    return _check(len(blob), n64.COMBINED_SIZE, "empty saveset → 296,960 bytes")


def test_split_zero_filled_returns_empty() -> bool:
    """A fully-zero combined srm splits to an all-None saveset."""
    blob = b"\x00" * n64.COMBINED_SIZE
    ss = n64.split(blob)
    return _check(ss.is_empty(), True, "zero srm → empty saveset")


def test_combine_split_eeprom_only_4kbit() -> bool:
    """4-Kbit EEPROM round-trip preserves the natural 512-byte size."""
    eep = bytes(random.randrange(256) for _ in range(n64.EEPROM_4KBIT_BYTES))
    ss_in = n64.N64SaveSet(eeprom=eep)
    blob = n64.combine(ss_in)
    ss_out = n64.split(blob)
    ok = _check(ss_out.eeprom, eep, "EEPROM 4-Kbit round-trip preserved")
    ok &= _check(ss_out.sram, None, "SRAM still None")
    ok &= _check(ss_out.flashram, None, "FlashRAM still None")
    ok &= _check(all(c is None for c in ss_out.cpak), True,
                 "All cpak ports still None")
    return ok


def test_combine_split_eeprom_only_16kbit() -> bool:
    """16-Kbit EEPROM round-trip — natural 2 KB size preserved."""
    eep = bytes(random.randrange(1, 256) for _ in range(n64.EEPROM_16KBIT_BYTES))
    ss = n64.split(n64.combine(n64.N64SaveSet(eeprom=eep)))
    return _check(ss.eeprom, eep, "EEPROM 16-Kbit round-trip preserved")


def test_combine_split_sram_only() -> bool:
    sram = bytes(random.randrange(1, 256) for _ in range(n64.SRAM_SIZE))
    ss = n64.split(n64.combine(n64.N64SaveSet(sram=sram)))
    ok = _check(ss.sram, sram, "SRAM round-trip preserved")
    ok &= _check(ss.eeprom, None, "EEPROM None when game uses only SRAM")
    return ok


def test_combine_split_flashram_only() -> bool:
    fla = bytes(random.randrange(1, 256) for _ in range(n64.FLASHRAM_SIZE))
    ss = n64.split(n64.combine(n64.N64SaveSet(flashram=fla)))
    return _check(ss.flashram, fla, "FlashRAM round-trip preserved")


def test_combine_split_cpak_per_port() -> bool:
    """Each Controller Pak port roundtrips independently."""
    cpak0 = bytes(random.randrange(1, 256) for _ in range(n64.CPAK_SIZE))
    cpak2 = bytes(random.randrange(1, 256) for _ in range(n64.CPAK_SIZE))
    ss = n64.split(n64.combine(n64.N64SaveSet(
        cpak=(cpak0, None, cpak2, None))))
    ok = _check(ss.cpak[0], cpak0, "cpak port 1 preserved")
    ok &= _check(ss.cpak[1], None, "cpak port 2 None")
    ok &= _check(ss.cpak[2], cpak2, "cpak port 3 preserved")
    ok &= _check(ss.cpak[3], None, "cpak port 4 None")
    return ok


def test_combine_split_multi_format_paper_mario() -> bool:
    """Paper Mario uses FlashRAM + Controller Pak. Both round-trip."""
    fla = bytes(random.randrange(1, 256) for _ in range(n64.FLASHRAM_SIZE))
    cpak = bytes(random.randrange(1, 256) for _ in range(n64.CPAK_SIZE))
    ss = n64.split(n64.combine(n64.N64SaveSet(
        flashram=fla, cpak=(cpak, None, None, None))))
    ok = _check(ss.flashram, fla, "FlashRAM preserved")
    ok &= _check(ss.cpak[0], cpak, "cpak port 1 preserved")
    return ok


def test_split_tolerates_short_input() -> bool:
    """Some emulators truncate `.srm` to the bytes the game actually uses.
    split() pads with zeros."""
    short = b"\xab" * n64.EEPROM_4KBIT_BYTES   # only 512 bytes
    ss = n64.split(short)
    return _check(ss.eeprom, short, "short srm: EEPROM populated, rest None")


def test_split_rejects_oversized_input() -> bool:
    """Inputs > 296,960 are not a real .srm; refuse rather than silently truncate."""
    oversized = b"\x00" * (n64.COMBINED_SIZE + 1)
    try:
        n64.split(oversized)
    except ValueError:
        print("ok:   oversized input → ValueError")
        return True
    print("FAIL: expected ValueError on oversized input")
    return False


def test_split_zero_pad_4kbit_eeprom() -> bool:
    """A combined srm with only the first 512 bytes of EEPROM populated
    (and 0x200–0x7FF zeros) should split to a 512-byte EEPROM."""
    eep_4kbit = bytes(random.randrange(1, 256) for _ in range(n64.EEPROM_4KBIT_BYTES))
    blob = bytearray(n64.COMBINED_SIZE)
    blob[:len(eep_4kbit)] = eep_4kbit
    ss = n64.split(bytes(blob))
    return _check(len(ss.eeprom), n64.EEPROM_4KBIT_BYTES,
                  "4-Kbit EEPROM trimmed to natural 512 bytes")


def test_combine_rejects_oversized_eeprom() -> bool:
    too_big = b"\x00" * (n64.EEPROM_SIZE + 1)
    try:
        n64.combine(n64.N64SaveSet(eeprom=too_big))
    except ValueError:
        print("ok:   oversized EEPROM → ValueError")
        return True
    print("FAIL: expected ValueError")
    return False


def test_combine_rejects_wrong_size_sram() -> bool:
    try:
        n64.combine(n64.N64SaveSet(sram=b"\x00" * 100))
    except ValueError:
        print("ok:   short SRAM → ValueError")
        return True
    print("FAIL: expected ValueError")
    return False


def test_property_random_round_trips() -> bool:
    """100 randomly-shaped savesets round-trip without data loss."""
    rng = random.Random(0xc0ffee)
    failures: list[str] = []
    for i in range(100):
        eep = None
        if rng.random() < 0.4:
            sz = rng.choice([n64.EEPROM_4KBIT_BYTES, n64.EEPROM_16KBIT_BYTES])
            eep = bytes(rng.randrange(1, 256) for _ in range(sz))
        sram = None
        if rng.random() < 0.4:
            sram = bytes(rng.randrange(1, 256) for _ in range(n64.SRAM_SIZE))
        fla = None
        if rng.random() < 0.4 and sram is None:
            fla = bytes(rng.randrange(1, 256) for _ in range(n64.FLASHRAM_SIZE))
        cpak = []
        for _ in range(4):
            if rng.random() < 0.3:
                cpak.append(bytes(rng.randrange(1, 256) for _ in range(n64.CPAK_SIZE)))
            else:
                cpak.append(None)
        ss_in = n64.N64SaveSet(
            eeprom=eep, sram=sram, flashram=fla,
            cpak=(cpak[0], cpak[1], cpak[2], cpak[3]))
        ss_out = n64.split(n64.combine(ss_in))
        if ss_out != ss_in:
            failures.append(f"iter {i}: {ss_in!r} → {ss_out!r}")
    if failures:
        print(f"FAIL: {len(failures)} round-trip failures")
        for f in failures[:3]:
            print(f"  {f}")
        return False
    print("ok:   100 random round-trips preserved")
    return True


def test_cpak_port_extension() -> bool:
    ok = _check(n64.cpak_port_extension(1), ".mp1", "port 1 → .mp1")
    ok &= _check(n64.cpak_port_extension(4), ".mp4", "port 4 → .mp4")
    try:
        n64.cpak_port_extension(0)
        print("FAIL: port 0 accepted")
        return False
    except ValueError:
        print("ok:   port 0 rejected")
    return ok


def main() -> int:
    ok = True
    for name, fn in [
        ("combine_emits_exact_size", test_combine_emits_exact_size),
        ("split_zero_filled_returns_empty", test_split_zero_filled_returns_empty),
        ("combine_split_eeprom_only_4kbit", test_combine_split_eeprom_only_4kbit),
        ("combine_split_eeprom_only_16kbit", test_combine_split_eeprom_only_16kbit),
        ("combine_split_sram_only", test_combine_split_sram_only),
        ("combine_split_flashram_only", test_combine_split_flashram_only),
        ("combine_split_cpak_per_port", test_combine_split_cpak_per_port),
        ("combine_split_multi_format_paper_mario",
         test_combine_split_multi_format_paper_mario),
        ("split_tolerates_short_input", test_split_tolerates_short_input),
        ("split_rejects_oversized_input", test_split_rejects_oversized_input),
        ("split_zero_pad_4kbit_eeprom", test_split_zero_pad_4kbit_eeprom),
        ("combine_rejects_oversized_eeprom", test_combine_rejects_oversized_eeprom),
        ("combine_rejects_wrong_size_sram", test_combine_rejects_wrong_size_sram),
        ("property_random_round_trips", test_property_random_round_trips),
        ("cpak_port_extension", test_cpak_port_extension),
    ]:
        print(f"--- {name} ---")
        ok &= fn()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
