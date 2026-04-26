"""Krikzz EverDrive USB protocol — FT245-based.

Shared by every Krikzz EverDrive product that exposes a USB Type-B
Mini port backed by the FTDI FT245R chip: EverDrive 64 X7, Mega
EverDrive Pro / X7 / X3 (Genesis), and others. The wire framing and
SD-file commands are common across the OS64 v3.x firmware family.

This module ships three transport backends behind one common
interface (`KrikzzFtdiTransport`):

  - `PyFtdiKrikzzTransport`  — direct USB via libusb / pyftdi. The
                               primary path. **Wire-byte specifics
                               are marked TBD pending verification
                               against UNFLoader source.**
  - `UnfloaderKrikzzTransport` — subprocess wrapper around the
                                  UNFLoader binary. Fallback for when
                                  pyftdi has driver issues. **Also
                                  TBD: UNFLoader's CLI doesn't
                                  natively expose SD ops; needs a
                                  small wrapper or patch.**
  - `MockKrikzzTransport`    — in-memory implementation for unit
                                tests. Fully functional.

Per the n64-sync-design doc §3.3, the live-hardware backends'
wire format is best-effort here and needs to be confirmed against
UNFLoader's `device_everdrive3.c` (e.g. `cmd_test`, `cmd_dir_open`,
`cmd_file_read`, etc.) on real hardware before shipping. The
adapter built on top of this transport is fully complete and
tested via the Mock backend.
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

    `kind` is one of `pyftdi`, `unfloader`, `mock`. Extra kwargs are
    passed through to the chosen backend's constructor.
    """
    if kind == "pyftdi":
        return PyFtdiKrikzzTransport(**opts)
    if kind == "unfloader":
        return UnfloaderKrikzzTransport(**opts)
    if kind == "mock":
        return MockKrikzzTransport(**opts)
    raise ValueError(
        f"unknown transport kind {kind!r}; "
        "use one of: pyftdi, unfloader, mock")
