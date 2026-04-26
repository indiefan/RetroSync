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


def build_command_frame(cmd: int, address: int = 0, size_bytes: int = 0,
                        arg: int = 0) -> bytes:
    """Construct a 16-byte command frame matching UNFLoader's layout.

    `size_bytes` is the actual byte size of any payload that follows
    the frame; we encode it in 512-byte blocks per the firmware
    convention (UNFLoader's `device_sendcmd_everdrive` does
    `size /= 512` before encoding).
    """
    if not 0 <= cmd <= 0xff:
        raise ValueError(f"cmd byte out of range: {cmd}")
    blocks = size_bytes // 512
    frame = bytearray(COMMAND_FRAME_SIZE)
    frame[0:3] = b"cmd"
    frame[3] = cmd
    frame[4:8] = address.to_bytes(4, "big", signed=False)
    frame[8:12] = blocks.to_bytes(4, "big", signed=False)
    frame[12:16] = arg.to_bytes(4, "big", signed=False)
    return bytes(frame)


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
            # Drain any junk left from prior sessions.
            self._port.reset_input_buffer()
            self._port.reset_output_buffer()
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
        # CMD_TEST: send 'cmdt' + 12 zero-bytes, expect 16 bytes back.
        # Per UNFLoader: recv[3] == 'r' identifies an EverDrive.
        try:
            resp = await self._send_recv(ord("t"))
        except KrikzzFtdiError as exc:
            return False, str(exc)
        if len(resp) < 4 or resp[3:4] != b"r":
            return False, (
                f"unexpected handshake response: {resp[:8]!r} "
                "(expected b'cmdr...')")
        # The trailing 12 bytes encode firmware version + status. We
        # surface them in the detail string so `retrosync test-cart`
        # output is informative; format may evolve.
        status = resp[4:].hex()
        return True, f"EverDrive 64 (handshake ok, status={status})"

    # ----- 16-byte framing helper -----

    async def _send_recv(self, cmd: int, *, address: int = 0,
                         size_bytes: int = 0, arg: int = 0,
                         response_size: int = RESPONSE_FRAME_SIZE,
                         ) -> bytes:
        """Send a 16-byte command frame and read the response."""
        if self._port is None:
            raise KrikzzFtdiError("transport not open")
        frame = build_command_frame(cmd, address=address,
                                    size_bytes=size_bytes, arg=arg)
        try:
            self._port.reset_input_buffer()
            written = self._port.write(frame)
            self._port.flush()
        except Exception as exc:  # noqa: BLE001
            raise KrikzzFtdiError(f"write failed: {exc}") from exc
        if written != COMMAND_FRAME_SIZE:
            raise KrikzzFtdiError(
                f"short write: {written}/{COMMAND_FRAME_SIZE} bytes")
        try:
            resp = self._port.read(response_size)
        except Exception as exc:  # noqa: BLE001
            raise KrikzzFtdiError(f"read failed: {exc}") from exc
        if len(resp) < response_size:
            raise KrikzzFtdiError(
                f"short read: {len(resp)}/{response_size} bytes "
                f"(timeout? cart in wrong state?)")
        return resp

    # ----- SD operations: stubbed pending Krikzz USB tool review ------

    async def dir_list(self, path: str) -> list[FileEntry]:
        raise NotImplementedError(
            "SerialKrikzzTransport.dir_list: SD-file ops are not in "
            "UNFLoader's source. Need to derive from Krikzz's USB tool "
            "(krikzz.com/pub/support/everdrive-64/x-series/dev/) or a "
            "ROM-side debug print of the OS64 protocol. Handshake "
            "works; this is the next byte-level work.")

    async def file_read(self, path: str) -> bytes:
        raise NotImplementedError("see SerialKrikzzTransport.dir_list")

    async def file_write(self, path: str, data: bytes) -> None:
        raise NotImplementedError("see SerialKrikzzTransport.dir_list")

    async def file_delete(self, path: str) -> None:
        raise NotImplementedError("see SerialKrikzzTransport.dir_list")

    async def file_exists(self, path: str) -> bool:
        raise NotImplementedError("see SerialKrikzzTransport.dir_list")


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
