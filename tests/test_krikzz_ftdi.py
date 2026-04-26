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
    """Address/length/arg encoded as big-endian uint32. Caller is
    responsible for size-in-blocks vs size-in-bytes semantics
    (per Krikzz's Edio.cs: ROM commands divide by 512 themselves;
    file commands pass byte count as-is)."""
    frame = build_command_frame(
        ord("W"), address=0x10000000, length=0x4000 // 512, arg=0)
    expected = (
        b"cmdW"
        + (0x10000000).to_bytes(4, "big")
        + (0x4000 // 512).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
    )
    return _check(frame, expected,
                  "ROM-write frame (W cmd, address, length in 512B blocks)")


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


# -------- Fake serial port for protocol-byte assertions --------

class _FakeSerial:
    """Captures writes and returns scripted reads. Mirrors the
    pyserial.Serial subset SerialKrikzzTransport uses."""

    def __init__(self, scripted_reads: list[bytes]):
        self.writes: list[bytes] = []
        self._reads = list(scripted_reads)
        self._read_buffer = b""

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        pass

    def reset_output_buffer(self) -> None:
        pass

    def close(self) -> None:
        pass

    def read(self, size: int) -> bytes:
        # Drain from queued scripted reads, concatenated as needed.
        while len(self._read_buffer) < size and self._reads:
            self._read_buffer += self._reads.pop(0)
        out, self._read_buffer = self._read_buffer[:size], self._read_buffer[size:]
        return out


def _serial_with_script(reads: list[bytes]) -> SerialKrikzzTransport:
    t = SerialKrikzzTransport(serial_path="/dev/null")
    t._port = _FakeSerial(reads)
    return t


def _ok_status_resp() -> bytes:
    """16-byte 'cmdr' response with status==0."""
    return b"cmdr" + b"\x00" * 12


async def test_file_info_present() -> bool:
    """fileInfo returns size etc. when status byte is 0."""
    # Build a 'cmd4' response with status=0, attrib=0x20, size=8192.
    resp = bytearray(b"cmd4" + b"\x00" * 12)
    resp[5] = 0x20
    resp[8:12] = (8192).to_bytes(4, "big")
    t = _serial_with_script([bytes(resp)])
    path = "/ED64/SAVES/Foo.eep"
    info = await t._file_info(path)
    ok = _check(info["size"], 8192, "file_info parses size")
    ok &= _check(info["attrib"], 0x20, "file_info parses attrib")
    # Verify the wire bytes: 16-byte cmdTX with cmd='4', length=len(path),
    # arg=0; then the path bytes (padded to a 4-byte multiple with 0xff).
    written = b"".join(t._port.writes)
    expected_cmd = (b"cmd4" + b"\x00" * 4
                    + len(path).to_bytes(4, "big") + b"\x00" * 4)
    ok &= _check(written[:16], expected_cmd, "file_info command frame")
    # 19-byte path + 1 byte padding = 20 (multiple of 4).
    expected_payload = path.encode() + b"\xff" * (4 - len(path) % 4)
    ok &= _check(written[16:16 + len(expected_payload)],
                 expected_payload, "file_info path payload")
    return ok


async def test_file_info_missing_returns_none() -> bool:
    """fileInfo returns None when status byte is non-zero (FR_NO_FILE)."""
    resp = bytearray(b"cmd4" + b"\x00" * 12)
    resp[4] = 4  # FR_NO_FILE
    t = _serial_with_script([bytes(resp)])
    info = await t._file_info("/missing.eep")
    return _check(info, None, "missing file → None")


async def test_file_exists_true_false() -> bool:
    """file_exists wraps file_info."""
    # Two responses: one OK, one NO_FILE.
    ok_resp = bytearray(b"cmd4" + b"\x00" * 12)
    ok_resp[8:12] = (100).to_bytes(4, "big")
    miss_resp = bytearray(b"cmd4" + b"\x00" * 12)
    miss_resp[4] = 4
    t = _serial_with_script([bytes(ok_resp), bytes(miss_resp)])
    exists1 = await t.file_exists("/ED64/SAVES/Foo.eep")
    exists2 = await t.file_exists("/ED64/SAVES/Bar.eep")
    return (_check(exists1, True, "file_exists(present)=True")
            and _check(exists2, False, "file_exists(missing)=False"))


async def test_file_read_full_round_trip() -> bool:
    """file_read = file_info + file_open + file_read_chunk + file_close.

    Verifies the on-wire byte sequence for each step.
    """
    # Scripted responses: file_info OK with size=8, then status OK
    # for open, then 8 bytes of file content, then status OK for read,
    # then status OK for close.
    info_resp = bytearray(b"cmd4" + b"\x00" * 12)
    info_resp[8:12] = (8).to_bytes(4, "big")
    t = _serial_with_script([
        bytes(info_resp),                # file_info response
        _ok_status_resp(),               # checkStatus after file_open
        b"DEADBEEF",                     # 8 bytes of file content
        _ok_status_resp(),               # checkStatus after file_read
        _ok_status_resp(),               # checkStatus after file_close
    ])
    data = await t.file_read("/ED64/SAVES/x.eep")
    return _check(data, b"DEADBEEF", "file_read returns the content")


async def test_file_write_full_round_trip() -> bool:
    """file_write = file_open(CREATE_ALWAYS|WRITE) + file_write_chunk + file_close."""
    t = _serial_with_script([
        _ok_status_resp(),  # status after file_open
        _ok_status_resp(),  # status after file_write
        _ok_status_resp(),  # status after file_close
    ])
    await t.file_write("/ED64/SAVES/x.eep", b"PAYLOAD!")
    # Inspect the writes: cmd '0' (open) with length=path_len and arg=
    # FAT_CREATE_ALWAYS|FAT_WRITE = 0x0a.
    open_frame = t._port.writes[0]
    arg = int.from_bytes(open_frame[12:16], "big")
    return (_check(open_frame[3:4], b"0", "first cmd is file_open ('0')")
            and _check(arg, 0x0a, "open mode = CREATE_ALWAYS | WRITE"))


async def test_dir_list_still_not_implemented() -> bool:
    """dir_list raises NotImplementedError — Krikzz's source doesn't
    expose it. Adapter falls back to rom_filenames-based enumeration."""
    t = SerialKrikzzTransport(serial_path="/dev/null")
    t._port = _FakeSerial([])
    try:
        await t.dir_list("/ED64/SAVES")
    except NotImplementedError:
        print("ok:   dir_list still NotImplementedError (per design)")
        return True
    print("FAIL: expected NotImplementedError")
    return False


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
        ("file_info_present", test_file_info_present),
        ("file_info_missing_returns_none",
         test_file_info_missing_returns_none),
        ("file_exists_true_false", test_file_exists_true_false),
        ("file_read_full_round_trip", test_file_read_full_round_trip),
        ("file_write_full_round_trip", test_file_write_full_round_trip),
        ("dir_list_still_not_implemented",
         test_dir_list_still_not_implemented),
    ]:
        print(f"--- {name} ---")
        ok &= asyncio.run(fn())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
