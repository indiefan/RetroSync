"""Krikzz EverDrive USB protocol.

Common to several Krikzz flash carts that expose a USB Type-B Mini
port. Hardware varies — older carts use FT245R (USB FIFO), newer
revisions / variants use FT232 (USB UART). Both are bulk-USB underneath
and accept the same 16-byte command frame; what changes is how the
host-side OS surfaces the device:

  - FT245R → libusb-direct via `pyftdi` (bypasses kernel driver).
  - FT232  → kernel auto-binds `ftdi_sio` and exposes a serial port
            at `/dev/ttyUSB*`; talk to it with `pyserial`.

This module ships four transport backends behind one common
interface (`KrikzzFtdiTransport`):

  - `SerialKrikzzTransport`  — pyserial-based. Works against the
                               FT232 / kernel-bound case (verified on
                               operator's EverDrive 64 X7 hardware).
                               Handshake confirmed; SD-file ops
                               stubbed pending Krikzz-USB-tool
                               source review.
  - `PyFtdiKrikzzTransport`  — direct USB via libusb / pyftdi for
                               FT245R hardware. Wire bytes are the
                               same 16-byte frame as the serial
                               transport; the framing helper is
                               shared. open() + handshake stubbed
                               pending pyftdi-on-FT245R verification.
  - `UnfloaderKrikzzTransport` — subprocess wrapper around the
                                  UNFLoader binary. UNFLoader source
                                  has only ROM-upload / debug / PIFboot
                                  commands; SD-file ops aren't there,
                                  so this backend is stubbed beyond
                                  health().
  - `MockKrikzzTransport`    — in-memory implementation for unit
                                tests. Fully functional.

The 16-byte command frame layout (verified against UNFLoader's
`device_everdrive.cpp::device_sendcmd_everdrive`):

    byte 0..2  : ASCII "cmd" magic
    byte 3     : command code ('t' = test, 'W' = ROM write, 's' =
                 PIFboot, others undocumented in UNFLoader)
    byte 4..7  : address (uint32 big-endian)
    byte 8..11 : size in 512-byte blocks (uint32 big-endian)
    byte 12..15: arg (uint32 big-endian)

Response is also 16 bytes for most commands; `recv[3] == 'r'` after a
test command is the EverDrive identification check.

SD-file operations (`CMD_DIR_OPEN`, `CMD_FILE_READ` etc. that the
n64-sync-design doc referenced) are NOT in UNFLoader's source. The
v3.x OS64 firmware exposes them but the Python implementation needs
to be derived from Krikzz's separate USB tool (referenced in
UNFLoader as krikzz.com/pub/support/everdrive-64/x-series/dev/).
Until that's done the SD-op methods raise NotImplementedError with
a pointer.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger(__name__)


# Per UNFLoader docs: the EverDrive 3.0+ family uses an FT245-based
# command framing. Each command starts with a 4-byte header magic,
# followed by a 1-byte command code, then optional args / payload.
# The exact magic and command codes vary by firmware version (Cart OS
# v2 differs from OS64 v3.x). v0.4 targets OS64 v3.x, the firmware the
# X7 ships with.
#
# Verify against UNFLoader's `device_everdrive3.c` constants table.
HEADER_MAGIC = b"CMD "  # TBD: confirm against UNFLoader source
CMD_TEST     = ord("t")
CMD_DIR_OPEN  = ord("o")  # TBD
CMD_DIR_READ  = ord("r")  # TBD
CMD_FILE_OPEN  = ord("O")  # TBD
CMD_FILE_READ  = ord("R")  # TBD
CMD_FILE_WRITE = ord("W")  # TBD
CMD_FILE_CLOSE = ord("C")  # TBD


class KrikzzFtdiError(Exception):
    """Raised on protocol-level failures (timeout, bad response,
    SD I/O error reported by the cart)."""


# 16-byte command frame size — fixed by the firmware protocol.
COMMAND_FRAME_SIZE = 16
RESPONSE_FRAME_SIZE = 16

# Minimum block size for serial reads / writes. Krikzz's Usbio.cs
# rounds every transfer up to a multiple of 4 bytes (with 0xff
# padding). We mirror that to avoid the firmware getting stuck mid-
# read on an odd-length payload.
MIN_BLOCK_SIZE = 4

# FatFs file open modes. Lifted directly from Krikzz's Edio.cs.
FAT_READ            = 0x01
FAT_WRITE           = 0x02
FAT_OPEN_EXISTING   = 0x00
FAT_CREATE_NEW      = 0x04
FAT_CREATE_ALWAYS   = 0x08
FAT_OPEN_ALWAYS     = 0x10
FAT_OPEN_APPEND     = 0x30

# Default chunk size for file I/O. Krikzz's Edio uses 4096; we follow
# suit. Larger chunks get split inside the firmware.
DEFAULT_CHUNK = 4096


def build_command_frame(cmd: int, address: int = 0, length: int = 0,
                        arg: int = 0) -> bytes:
    """Construct a 16-byte command frame matching UNFLoader / Krikzz
    `Edio.cmdTX`.

    `length` semantics differ by command:
      - ROM commands ('c', 'W', 'R', 'f'): the firmware expects the
        size field in 512-byte blocks. Callers divide before passing.
      - File commands ('0', '1', '2', '4'): the firmware expects the
        actual byte count.
    The helper does NOT auto-divide; passing the right value is the
    caller's job (per Krikzz's `cmdTX`, where `len /= 512;` is
    explicitly commented out).
    """
    if not 0 <= cmd <= 0xff:
        raise ValueError(f"cmd byte out of range: {cmd}")
    frame = bytearray(COMMAND_FRAME_SIZE)
    frame[0:3] = b"cmd"
    frame[3] = cmd
    frame[4:8] = address.to_bytes(4, "big", signed=False)
    frame[8:12] = length.to_bytes(4, "big", signed=False)
    frame[12:16] = arg.to_bytes(4, "big", signed=False)
    return bytes(frame)


def pad_to_min_block(data: bytes) -> bytes:
    """Round payload up to a multiple of MIN_BLOCK_SIZE with 0xff
    padding. Mirrors Krikzz's `Usbio.fixDataSize`."""
    if len(data) % MIN_BLOCK_SIZE == 0:
        return data
    pad = MIN_BLOCK_SIZE - (len(data) % MIN_BLOCK_SIZE)
    return data + b"\xff" * pad


@dataclass(frozen=True)
class FileEntry:
    """A directory entry returned by `dir_list`."""
    name: str
    size: int
    is_dir: bool


class KrikzzFtdiTransport(ABC):
    """Abstract interface for SD-file operations on a Krikzz cart.

    Adapter classes (EverDrive64Source, hypothetical
    MegaEverDriveSource) consume this; the concrete backend
    (pyftdi / UNFLoader / mock) is selected by config.
    """

    @abstractmethod
    async def open(self) -> None:
        """Acquire the USB device, run the handshake (CMD_TEST)."""

    @abstractmethod
    async def close(self) -> None:
        """Release the USB device cleanly."""

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """Probe the cart with CMD_TEST. Returns (ok, detail).
        Detail is human-readable for logging."""

    @abstractmethod
    async def dir_list(self, path: str) -> list[FileEntry]:
        """List entries under `path` (e.g. `/ED64/SAVES`)."""

    @abstractmethod
    async def file_read(self, path: str) -> bytes:
        """Read an entire file from the SD."""

    @abstractmethod
    async def file_write(self, path: str, data: bytes) -> None:
        """Write `data` to `path`. Overwrites if exists."""

    @abstractmethod
    async def file_delete(self, path: str) -> None:
        """Delete `path`. No-op if it doesn't exist."""

    @abstractmethod
    async def file_exists(self, path: str) -> bool:
        """Cheap existence check (CMD_DIR_OPEN + lookup)."""


# -------------------------------------------------------------------- #
# pyserial backend — kernel-bound FT232 / ftdi_sio at /dev/ttyUSB*.    #
# -------------------------------------------------------------------- #

class SerialKrikzzTransport(KrikzzFtdiTransport):
    """pyserial-based transport for FT232-equipped Krikzz carts.

    On Linux, the FT232 is auto-bound by the kernel `ftdi_sio` driver
    and exposed as `/dev/ttyUSB*`. We talk to it with `pyserial` —
    no libusb / detach-kernel-driver dance needed.

    Verified against the operator's EverDrive 64 X7: the test command
    handshake (`cmdt` + 12 zeros → `cmdr` + status bytes) round-trips
    cleanly. The 16-byte command framing matches UNFLoader's
    `device_sendcmd_everdrive`.

    SD-file operations are stubbed — see module docstring.
    """

    def __init__(self, *, serial_path: str = "/dev/ttyUSB0",
                 baud: int = 9600, timeout_sec: float = 2.0):
        # FT232 over USB does bulk transfers regardless of the "baud
        # rate" the host sets, so 9600 / 115200 / 921600 are all
        # functionally equivalent. 9600 is the kernel default; leave
        # it unless the operator has a reason to change it.
        self._path = serial_path
        self._baud = baud
        self._timeout = timeout_sec
        self._port = None  # serial.Serial handle

    async def open(self) -> None:
        try:
            import serial  # noqa: F401
        except ImportError as exc:
            raise KrikzzFtdiError(
                "pyserial not installed; "
                "`sudo apt install python3-serial` "
                "or `pip install pyserial`") from exc
        try:
            import serial as _serial
            self._port = _serial.Serial(
                self._path, self._baud, timeout=self._timeout,
                write_timeout=self._timeout)
            # Aggressive drain of any junk left from prior sessions.
            # An aborted previous command (e.g. operator Ctrl+C'd
            # mid-file_info) leaves a 16-byte 'cmd<X>' response in
            # the FT232's RX buffer that the next health check would
            # mis-read as its own response. Drain by:
            #   1. reset_input_buffer (kernel/userland buffer)
            #   2. brief read with short timeout to consume any bytes
            #      the FT232 chip is still draining out
            self._port.reset_input_buffer()
            self._port.reset_output_buffer()
            saved_to = self._port.timeout
            self._port.timeout = 0.1
            try:
                stale = self._port.read(256)
                if stale:
                    log.debug("EverDrive 64 open: drained %d stale bytes",
                              len(stale))
            finally:
                self._port.timeout = saved_to
        except Exception as exc:  # noqa: BLE001
            raise KrikzzFtdiError(
                f"opening {self._path}: {exc}") from exc

    async def close(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception:  # noqa: BLE001
                pass
            self._port = None

    async def health(self) -> tuple[bool, str]:
        if self._port is None:
            return False, "transport not open"
        # CMD_TEST: send 'cmdt' + 12 zero-bytes, expect 16 bytes back
        # with header 'cmdr'. Krikzz's Edio.getStatus reads the byte at
        # offset 4 as the cart's current status (0 = idle/OK).
        # Self-healing: if we get an unexpected response (typically a
        # stale cmd<X> queued from a prior aborted run), drain and
        # retry once.
        for attempt in range(2):
            try:
                resp = await self._cmd_rx_with_send(
                    ord("t"), expect=ord("r"))
                status_byte = resp[4]
                rest = resp[5:].hex()
                return (True,
                        f"EverDrive 64 "
                        f"(status=0x{status_byte:02x}, meta={rest})")
            except KrikzzFtdiError as exc:
                if attempt == 0 and "unexpected response" in str(exc):
                    # Drain and retry — buffer was holding stale bytes.
                    self._port.reset_input_buffer()
                    saved = self._port.timeout
                    self._port.timeout = 0.2
                    try:
                        self._port.read(256)
                    finally:
                        self._port.timeout = saved
                    continue
                return False, str(exc)
        return False, "health retry exhausted"

    # ----- low-level framing primitives -----

    async def _cmd_tx(self, cmd: int, *, address: int = 0,
                      length: int = 0, arg: int = 0) -> None:
        """Send a 16-byte command frame; do NOT read any response."""
        if self._port is None:
            raise KrikzzFtdiError("transport not open")
        frame = build_command_frame(cmd, address=address,
                                    length=length, arg=arg)
        try:
            self._port.reset_input_buffer()
            written = self._port.write(frame)
            self._port.flush()
        except Exception as exc:  # noqa: BLE001
            raise KrikzzFtdiError(f"write failed: {exc}") from exc
        if written != COMMAND_FRAME_SIZE:
            raise KrikzzFtdiError(
                f"short write: {written}/{COMMAND_FRAME_SIZE} bytes")

    async def _cmd_rx(self, expected_cmd: int) -> bytes:
        """Read a 16-byte response and validate the 'cmd<X>' header.

        Mirrors Krikzz's `Edio.cmdRX`. Raises on header mismatch
        (corrupted response) or on cmd-byte mismatch (unexpected
        response — usually a sign of a previous command not draining
        cleanly, or the cart in a bad state).
        """
        if self._port is None:
            raise KrikzzFtdiError("transport not open")
        try:
            resp = self._port.read(RESPONSE_FRAME_SIZE)
        except Exception as exc:  # noqa: BLE001
            raise KrikzzFtdiError(f"read failed: {exc}") from exc
        if len(resp) < RESPONSE_FRAME_SIZE:
            raise KrikzzFtdiError(
                f"short read: {len(resp)}/{RESPONSE_FRAME_SIZE} bytes "
                f"(timeout? cart in wrong state?)")
        if resp[:3] != b"cmd":
            raise KrikzzFtdiError(
                f"corrupted response header: {resp[:4]!r}")
        if resp[3] != expected_cmd:
            raise KrikzzFtdiError(
                f"unexpected response cmd 0x{resp[3]:02x} "
                f"(expected 0x{expected_cmd:02x})")
        return resp

    async def _cmd_rx_with_send(self, cmd: int, *,
                                expect: int | None = None,
                                **kwargs) -> bytes:
        """Send a command frame and read its response in one go."""
        await self._cmd_tx(cmd, **kwargs)
        return await self._cmd_rx(expect if expect is not None else cmd)

    async def _check_status(self) -> None:
        """Send the 't' command, read the 'r' response, raise if the
        status byte at offset 4 is non-zero. Mirrors `Edio.checkStatus`.
        """
        resp = await self._cmd_rx_with_send(ord("t"), expect=ord("r"))
        if resp[4] != 0:
            raise KrikzzFtdiError(
                f"cart reported error status 0x{resp[4]:02x}")

    async def _write_payload(self, data: bytes) -> None:
        """Write data after a cmdTX. Krikzz pads to a 4-byte minimum
        block size, then chunks at 32 KB. We mirror that."""
        if self._port is None:
            raise KrikzzFtdiError("transport not open")
        padded = pad_to_min_block(data)
        try:
            n = self._port.write(padded)
            self._port.flush()
        except Exception as exc:  # noqa: BLE001
            raise KrikzzFtdiError(f"payload write failed: {exc}") from exc
        if n != len(padded):
            raise KrikzzFtdiError(
                f"short payload write: {n}/{len(padded)} bytes")

    async def _read_payload(self, length: int) -> bytes:
        """Read `length` bytes (post-cmdTX). Pads up to 4-byte minimum
        block then trims to the requested length, matching Krikzz's
        `Usbio.read_block`."""
        if self._port is None:
            raise KrikzzFtdiError("transport not open")
        block_len = length
        if block_len % MIN_BLOCK_SIZE != 0:
            block_len = (
                block_len // MIN_BLOCK_SIZE * MIN_BLOCK_SIZE
                + MIN_BLOCK_SIZE)
        try:
            data = self._port.read(block_len)
        except Exception as exc:  # noqa: BLE001
            raise KrikzzFtdiError(f"payload read failed: {exc}") from exc
        if len(data) < block_len:
            raise KrikzzFtdiError(
                f"short payload read: {len(data)}/{block_len} bytes")
        return bytes(data[:length])

    # ----- SD file operations (Krikzz Edio.cs) -----

    async def _file_open(self, path: str, mode: int) -> None:
        """File opens send the 16-byte command followed by the path
        bytes (NOT zero-terminated; length encoded in the cmd frame).
        Status checked after."""
        path_bytes = path.encode("ascii")
        await self._cmd_tx(ord("0"), length=len(path_bytes), arg=mode)
        await self._write_payload(path_bytes)
        await self._check_status()

    async def _file_close(self) -> None:
        await self._cmd_tx(ord("3"))
        await self._check_status()

    async def _file_read_chunk(self, length: int) -> bytes:
        await self._cmd_tx(ord("1"), length=length)
        out = bytearray()
        remaining = length
        while remaining > 0:
            block = min(DEFAULT_CHUNK, remaining)
            out.extend(await self._read_payload(block))
            remaining -= block
        await self._check_status()
        return bytes(out)

    async def _file_write_chunk(self, data: bytes) -> None:
        await self._cmd_tx(ord("2"), length=len(data))
        offset = 0
        remaining = len(data)
        while remaining > 0:
            block = min(DEFAULT_CHUNK, remaining)
            await self._write_payload(data[offset:offset + block])
            offset += block
            remaining -= block
        await self._check_status()

    async def _file_info(self, path: str) -> dict | None:
        """Return file metadata or None if the file doesn't exist.

        cmdTX('4', length=path.length); write path; read 16-byte
        response with header 'cmd4'. resp[4] is a FatFs error code
        — 0 = OK, anything else = error (typically 4 = FR_NO_FILE).
        """
        path_bytes = path.encode("ascii")
        await self._cmd_tx(ord("4"), length=len(path_bytes))
        await self._write_payload(path_bytes)
        resp = await self._cmd_rx(ord("4"))
        status = resp[4]
        if status != 0:
            # Not necessarily an error — FR_NO_FILE just means the
            # file's missing.
            return None
        return {
            "attrib": resp[5],
            "size":   int.from_bytes(resp[8:12], "big"),
            "date":   int.from_bytes(resp[12:14], "big"),
            "time":   int.from_bytes(resp[14:16], "big"),
        }

    # ----- KrikzzFtdiTransport interface -----

    async def dir_list(self, path: str) -> list[FileEntry]:
        # Krikzz's tool doesn't expose a directory listing. The OS64
        # firmware presumably has one (the on-cart menu shows files)
        # but the protocol byte for it isn't documented in the source
        # we have. Until that's reverse-engineered, the EverDrive 64
        # adapter has to enumerate by guessing names (e.g. derive
        # save filenames from a configured ROM list).
        raise NotImplementedError(
            "SerialKrikzzTransport.dir_list: not in Krikzz's USB tool "
            "source (only file_open/read/write/close/info are exposed). "
            "Either probe for an undocumented OS64 dir-list command, "
            "or use file_exists/file_info against expected save names "
            "derived from a known ROM list.")

    async def file_read(self, path: str) -> bytes:
        info = await self._file_info(path)
        if info is None:
            raise KrikzzFtdiError(f"file not found: {path}")
        size = info["size"]
        await self._file_open(path, FAT_READ)
        try:
            return await self._file_read_chunk(size)
        finally:
            try:
                await self._file_close()
            except KrikzzFtdiError:
                pass  # close-after-error best-effort

    async def file_write(self, path: str, data: bytes) -> None:
        await self._file_open(path, FAT_CREATE_ALWAYS | FAT_WRITE)
        try:
            await self._file_write_chunk(data)
        finally:
            try:
                await self._file_close()
            except KrikzzFtdiError:
                pass

    async def file_delete(self, path: str) -> None:
        # Not in Krikzz's source. The firmware likely has a delete
        # command (FAT-level), but the byte isn't documented. Mark
        # NotImplementedError; EverDrive64Source's empty-region delete
        # logic should fall back to overwriting with zero bytes or
        # logging a warning.
        raise NotImplementedError(
            "SerialKrikzzTransport.file_delete: not exposed by "
            "Krikzz's USB tool. Probe for an undocumented OS64 cmd "
            "byte, or fall back to overwrite-with-zero on the caller "
            "side.")

    async def file_exists(self, path: str) -> bool:
        return (await self._file_info(path)) is not None


# -------------------------------------------------------------------- #
# pyftdi backend — direct USB.                                         #
# -------------------------------------------------------------------- #

class PyFtdiKrikzzTransport(KrikzzFtdiTransport):
    """libusb / pyftdi backend.

    **Status:** structurally complete; wire-byte details for each
    command marked TBD. The implementing agent reads UNFLoader's
    `device_everdrive3.c` to fill in the exact byte sequences the
    cart firmware expects, then verifies against a real cart.

    pyftdi works without a kernel driver — useful on SteamOS where
    we don't have root for `modprobe`. Uses libusb under the hood.
    """

    def __init__(self, *, ftdi_url: str = "ftdi://ftdi:0x6001/1",
                 timeout_ms: int = 5000):
        self._ftdi_url = ftdi_url
        self._timeout_ms = timeout_ms
        self._dev = None  # pyftdi handle, set in open()

    async def open(self) -> None:
        # pyftdi is an optional dep; import lazily so non-N64 setups
        # don't pay for the import.
        try:
            from pyftdi.ftdi import Ftdi  # noqa: F401
        except ImportError as exc:
            raise KrikzzFtdiError(
                "pyftdi not installed; "
                "`pip install pyftdi` or use transport=unfloader") from exc
        # TBD: open the FTDI device, configure as bulk endpoint pair,
        # run handshake. See UNFLoader's `device_everdrive3.c::open`.
        raise NotImplementedError(
            "PyFtdiKrikzzTransport.open: wire-format implementation "
            "pending verification against UNFLoader source. Use "
            "transport=mock for tests or transport=unfloader on "
            "real hardware until this is filled in.")

    async def close(self) -> None:
        if self._dev is None:
            return
        # TBD: close FTDI handle.

    async def health(self) -> tuple[bool, str]:
        # TBD: send CMD_TEST, parse response, return (True, "firmware=v3.05")
        raise NotImplementedError("see PyFtdiKrikzzTransport.open")

    async def dir_list(self, path: str) -> list[FileEntry]:
        # TBD: CMD_DIR_OPEN <path>; loop CMD_DIR_READ until end-of-list
        # marker; collect entries.
        raise NotImplementedError("see PyFtdiKrikzzTransport.open")

    async def file_read(self, path: str) -> bytes:
        # TBD: CMD_FILE_OPEN <path> with read mode; loop CMD_FILE_READ
        # until EOF; CMD_FILE_CLOSE.
        raise NotImplementedError("see PyFtdiKrikzzTransport.open")

    async def file_write(self, path: str, data: bytes) -> None:
        # TBD: CMD_FILE_OPEN <path> with write mode; chunked
        # CMD_FILE_WRITE; CMD_FILE_CLOSE.
        raise NotImplementedError("see PyFtdiKrikzzTransport.open")

    async def file_delete(self, path: str) -> None:
        # TBD: CMD_FILE_OPEN with delete mode, or a dedicated
        # CMD_FILE_DELETE if firmware exposes one.
        raise NotImplementedError("see PyFtdiKrikzzTransport.open")

    async def file_exists(self, path: str) -> bool:
        # TBD: parent-dir list + name lookup is the safest implementation;
        # avoids relying on a CMD_FILE_EXISTS variant that may not be
        # in every firmware.
        raise NotImplementedError("see PyFtdiKrikzzTransport.open")


# -------------------------------------------------------------------- #
# UNFLoader subprocess backend — fallback if pyftdi has issues.         #
# -------------------------------------------------------------------- #

class UnfloaderKrikzzTransport(KrikzzFtdiTransport):
    """Wraps the UNFLoader binary as a subprocess.

    **Status:** Stub. UNFLoader's stock CLI doesn't natively expose
    the SD-file operations we need (it focuses on ROM upload + debug
    print). To use this backend in production, we either:

      a) Patch UNFLoader to add `--sd-list`, `--sd-cat`, `--sd-put`
         CLI flags and ship the patched binary, OR
      b) Write a small companion C utility that links UNFLoader's
         protocol layer and exposes these as a CLI.

    The stub is here so the registry has a third option and so a
    future contributor knows where to plug things in.
    """

    def __init__(self, *, unfloader_path: str = "/usr/local/bin/UNFLoader",
                 timeout_sec: float = 10.0):
        self._bin = unfloader_path
        self._timeout = timeout_sec

    async def open(self) -> None:
        if not _is_executable(self._bin):
            raise KrikzzFtdiError(
                f"UNFLoader binary not found at {self._bin}; install it "
                "or use transport=pyftdi")

    async def close(self) -> None:
        pass  # subprocess-per-op model; nothing to release

    async def health(self) -> tuple[bool, str]:
        try:
            out = await _run_unfloader([self._bin, "-d", "-t", "1"],
                                       timeout=self._timeout)
        except KrikzzFtdiError as exc:
            return False, str(exc)
        return True, out.strip().splitlines()[0] if out else "ok"

    async def dir_list(self, path: str) -> list[FileEntry]:
        raise NotImplementedError(
            "UnfloaderKrikzzTransport.dir_list: stock UNFLoader has no "
            "CLI for SD listing; either patch it or use transport=pyftdi.")

    async def file_read(self, path: str) -> bytes:
        raise NotImplementedError("see dir_list")

    async def file_write(self, path: str, data: bytes) -> None:
        raise NotImplementedError("see dir_list")

    async def file_delete(self, path: str) -> None:
        raise NotImplementedError("see dir_list")

    async def file_exists(self, path: str) -> bool:
        raise NotImplementedError("see dir_list")


def _is_executable(path: str) -> bool:
    import os
    return os.path.isfile(path) and os.access(path, os.X_OK)


async def _run_unfloader(cmd: list[str], *, timeout: float,
                         stdin: bytes | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE if stdin else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin),
                                          timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise KrikzzFtdiError(
            f"UNFLoader timed out after {timeout}s")
    if proc.returncode != 0:
        raise KrikzzFtdiError(
            f"UNFLoader exit {proc.returncode}: {err.decode(errors='replace')}")
    return out.decode(errors="replace")


# -------------------------------------------------------------------- #
# Mock backend — in-memory virtual SD filesystem.                       #
# -------------------------------------------------------------------- #

class MockKrikzzTransport(KrikzzFtdiTransport):
    """In-memory SD card. Used by the EverDrive64Source dry-run tests.

    Behaves like a flat directory tree backed by a dict; supports
    nested paths via `/`-separated keys. Not a substitute for
    real-hardware testing, but it lets the adapter logic be
    exercised end-to-end without USB.
    """

    def __init__(self, *, files: dict[str, bytes] | None = None,
                 firmware: str = "v3.05-mock"):
        # `files` keys are absolute paths; values are the file bytes.
        # Directories are implied by the path structure.
        self._files: dict[str, bytes] = dict(files or {})
        self._firmware = firmware
        self._open = False

    async def open(self) -> None:
        self._open = True

    async def close(self) -> None:
        self._open = False

    async def health(self) -> tuple[bool, str]:
        return True, f"mock firmware={self._firmware}"

    async def dir_list(self, path: str) -> list[FileEntry]:
        path = path.rstrip("/") + "/"
        seen: dict[str, FileEntry] = {}
        for full in self._files:
            if not full.startswith(path):
                continue
            rel = full[len(path):]
            if "/" in rel:
                # Subdirectory entry.
                name = rel.split("/", 1)[0]
                if name not in seen:
                    seen[name] = FileEntry(name=name, size=0, is_dir=True)
            else:
                seen[rel] = FileEntry(name=rel,
                                      size=len(self._files[full]),
                                      is_dir=False)
        return sorted(seen.values(), key=lambda e: (not e.is_dir, e.name))

    async def file_read(self, path: str) -> bytes:
        if path not in self._files:
            raise KrikzzFtdiError(f"file not found: {path}")
        return self._files[path]

    async def file_write(self, path: str, data: bytes) -> None:
        self._files[path] = bytes(data)

    async def file_delete(self, path: str) -> None:
        self._files.pop(path, None)

    async def file_exists(self, path: str) -> bool:
        return path in self._files


# -------------------------------------------------------------------- #
# Factory                                                              #
# -------------------------------------------------------------------- #

def build_transport(*, kind: str, **opts) -> KrikzzFtdiTransport:
    """Construct a transport from a config string.

    `kind` is one of `serial`, `pyftdi`, `unfloader`, `mock`. Extra
    kwargs are passed through to the chosen backend's constructor.

    Picking a backend:
      - `serial`    — FT232 carts where the kernel's `ftdi_sio`
                      auto-bound and exposed `/dev/ttyUSB*`. Most
                      EverDrive 64 X7s ship like this. Default.
      - `pyftdi`    — FT245R carts via libusb-direct. Requires
                      `pip install pyftdi` and that the kernel
                      driver isn't bound to the device.
      - `unfloader` — UNFLoader subprocess. Stubbed.
      - `mock`      — in-memory; tests only.
    """
    if kind == "serial":
        return SerialKrikzzTransport(**opts)
    if kind == "pyftdi":
        return PyFtdiKrikzzTransport(**opts)
    if kind == "unfloader":
        return UnfloaderKrikzzTransport(**opts)
    if kind == "mock":
        return MockKrikzzTransport(**opts)
    raise ValueError(
        f"unknown transport kind {kind!r}; "
        "use one of: serial, pyftdi, unfloader, mock")
