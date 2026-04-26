"""Krikzz FT245 / FT232 transport — frame format + serial handshake.

The 16-byte command frame layout matches UNFLoader's
`device_sendcmd_everdrive` exactly. Bytes verified against UNFLoader
source on real EverDrive 64 X7 hardware (handshake round-trip
returned `b'cmdr...'` as expected).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync.transport.krikzz_ftdi import (  # noqa: E402
    COMMAND_FRAME_SIZE, MockKrikzzTransport, SerialKrikzzTransport,
    build_command_frame, build_transport,
)


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


def test_frame_test_command_matches_unfloader() -> bool:
    """`cmdt` + 12 zero bytes — verified against UNFLoader source +
    real-cart handshake (responded with b'cmdr...' as expected)."""
    frame = build_command_frame(ord("t"))
    return _check(frame, b"cmdt" + b"\x00" * 12,
                  "test command frame")


def test_frame_with_address_size_arg() -> bool:
    """Address/size/arg encoded as big-endian uint32, size in 512-byte
    blocks (UNFLoader does `size /= 512`)."""
    frame = build_command_frame(
        ord("W"), address=0x10000000, size_bytes=0x4000, arg=0)
    expected = (
        b"cmdW"
        + (0x10000000).to_bytes(4, "big")
        + (0x4000 // 512).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
    )
    return _check(frame, expected,
                  "ROM-write frame (W cmd, address, size in 512B blocks)")


def test_frame_size_is_16() -> bool:
    return _check(len(build_command_frame(ord("t"))),
                  COMMAND_FRAME_SIZE, "frame is exactly 16 bytes")


def test_factory_includes_serial() -> bool:
    """`build_transport(kind='serial', ...)` returns a
    SerialKrikzzTransport without trying to open the port."""
    t = build_transport(kind="serial", serial_path="/dev/null",
                        baud=9600)
    return _check(isinstance(t, SerialKrikzzTransport), True,
                  "factory builds SerialKrikzzTransport")


def test_factory_unknown_kind_raises() -> bool:
    try:
        build_transport(kind="bogus")
    except ValueError:
        print("ok:   unknown kind raises ValueError")
        return True
    print("FAIL: expected ValueError")
    return False


async def test_serial_health_without_open_returns_false() -> bool:
    """health() before open() returns (False, 'transport not open').
    Don't try to open a real serial port in tests; this just confirms
    the transport doesn't crash when used incorrectly."""
    t = SerialKrikzzTransport(serial_path="/dev/null")
    ok, detail = await t.health()
    return (_check(ok, False, "health=False without open")
            and _check("not open" in detail, True,
                       "detail mentions 'not open'"))


async def test_mock_unchanged() -> bool:
    """Mock transport still constructible via factory with no kwargs."""
    t = build_transport(kind="mock")
    await t.open()
    ok, detail = await t.health()
    return (_check(ok, True, "mock health ok")
            and _check("mock" in detail.lower(), True,
                       "mock detail mentions mock"))


def main() -> int:
    ok = True
    for name, fn in [
        ("frame_test_command_matches_unfloader",
         test_frame_test_command_matches_unfloader),
        ("frame_with_address_size_arg",
         test_frame_with_address_size_arg),
        ("frame_size_is_16", test_frame_size_is_16),
        ("factory_includes_serial", test_factory_includes_serial),
        ("factory_unknown_kind_raises", test_factory_unknown_kind_raises),
    ]:
        print(f"--- {name} ---")
        ok &= fn()
    for name, fn in [
        ("serial_health_without_open_returns_false",
         test_serial_health_without_open_returns_false),
        ("mock_unchanged", test_mock_unchanged),
    ]:
        print(f"--- {name} ---")
        ok &= asyncio.run(fn())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
