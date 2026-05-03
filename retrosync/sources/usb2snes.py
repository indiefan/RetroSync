"""Minimal usb2snes WebSocket client.

The usb2snes protocol is JSON-over-WebSocket plus binary frames for file I/O.
It is implemented by SNI (https://github.com/alttpo/sni), QUsb2snes, and a
couple of other servers. We target SNI by default.

Protocol summary (v1, the one tooling has standardized on):

    Client → server : {"Opcode": "<op>", "Space": "SNES",
                       "Operands": ["<arg1>", ...], "Flags": [...]}
    Server → client : {"Results": ["<r1>", "<r2>", ...]}      # text
                      <binary frame>                          # for GetFile
                      ...

Operations we use:

    DeviceList                 → ["/dev/cu.usbmodem...", ...]
    Attach                     → no response, but subsequent ops target device
    Info                       → ["<firmware>", "<version>", "<rom name>"]
    List <path>                → ["<flag>", "<name>", "<flag>", "<name>", ...]
                                 flag "0" = directory, "1" = file
    GetFile <path>             → ["<size as decimal>"], then binary chunks
    PutFile <path> <size>      → no response; client streams binary

This module deliberately wraps just the verbs we need. It is async-first so
the orchestrator can keep the connection open while doing other work.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import websockets
from websockets.client import WebSocketClientProtocol

log = logging.getLogger(__name__)


class Usb2SnesError(Exception):
    pass


@dataclass(frozen=True)
class DirEntry:
    name: str
    is_dir: bool

    @property
    def is_file(self) -> bool:
        return not self.is_dir


class Usb2SnesClient:
    """Async client for a usb2snes server (SNI by default).

    Typical use:

        async with Usb2SnesClient("ws://127.0.0.1:23074") as cart:
            await cart.attach()                 # picks first device
            info = await cart.info()
            for entry in await cart.list("/"):
                ...
            data = await cart.get_file("/Mario.srm")

    The client owns one device attachment per session. To target multiple
    devices, open multiple clients.
    """

    def __init__(self, url: str = "ws://127.0.0.1:23074", *,
                 connect_timeout: float = 5.0,
                 read_timeout: float = 10.0):
        self._url = url
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._ws: WebSocketClientProtocol | None = None
        self._device: str | None = None

    # ----------- connection lifecycle -----------

    async def __aenter__(self) -> "Usb2SnesClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self._url, max_size=2**24, ping_interval=20),
                timeout=self._connect_timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise Usb2SnesError(f"connect failed: {exc}") from exc

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None

    # ----------- low-level send/receive -----------

    async def _send_cmd(self, opcode: str, *operands: str,
                        space: str = "SNES",
                        flags: list[str] | None = None) -> None:
        if self._ws is None:
            raise Usb2SnesError("not connected")
        msg = {"Opcode": opcode, "Space": space,
               "Operands": [str(o) for o in operands]}
        if flags:
            msg["Flags"] = flags
        await self._ws.send(json.dumps(msg))

    async def _recv_text(self) -> list[str]:
        if self._ws is None:
            raise Usb2SnesError("not connected")
        try:
            raw = await asyncio.wait_for(self._ws.recv(),
                                         timeout=self._read_timeout)
        except asyncio.TimeoutError as exc:
            raise Usb2SnesError("read timeout") from exc
        if isinstance(raw, bytes):
            raise Usb2SnesError("expected text frame, got binary")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Usb2SnesError(f"bad JSON: {exc}: {raw!r}") from exc
        results = payload.get("Results")
        if not isinstance(results, list):
            raise Usb2SnesError(f"missing Results: {payload!r}")
        return [str(r) for r in results]

    async def _recv_binary(self, total_size: int) -> bytes:
        if self._ws is None:
            raise Usb2SnesError("not connected")
        chunks: list[bytes] = []
        got = 0
        while got < total_size:
            try:
                frame = await asyncio.wait_for(self._ws.recv(),
                                               timeout=self._read_timeout)
            except asyncio.TimeoutError as exc:
                raise Usb2SnesError("binary read timeout") from exc
            if isinstance(frame, str):
                raise Usb2SnesError(f"expected binary, got text: {frame!r}")
            chunks.append(frame)
            got += len(frame)
        if got != total_size:
            raise Usb2SnesError(
                f"binary length mismatch: expected {total_size}, got {got}")
        return b"".join(chunks)

    # ----------- high-level ops -----------

    async def device_list(self) -> list[str]:
        await self._send_cmd("DeviceList")
        return await self._recv_text()

    async def attach(self, device: str | None = None) -> str:
        """Attach to `device`, or to the first one if None.

        Returns the device path attached to.
        """
        if device is None:
            devices = await self.device_list()
            if not devices:
                raise Usb2SnesError("no usb2snes devices found")
            device = devices[0]
        await self._send_cmd("Attach", device)
        # Attach has no response on success. Server name + app name are also
        # courtesies that some servers expect; SNI tolerates them missing.
        await self._send_cmd("Name", "RetroSync")
        self._device = device
        return device

    async def info(self) -> dict:
        await self._send_cmd("Info")
        results = await self._recv_text()
        # Conventional layout: [firmware-version, version-string, rom-name, ...]
        out: dict = {"raw": results}
        if len(results) >= 1:
            out["firmware"] = results[0]
        if len(results) >= 2:
            out["version"] = results[1]
        if len(results) >= 3:
            out["rom_name"] = results[2]
        return out

    async def list(self, path: str) -> list[DirEntry]:
        await self._send_cmd("List", path)
        results = await self._recv_text()
        # Pairs of [flag, name, flag, name, ...]; flag '0'=dir, '1'=file.
        # Some servers prefix a special entry pair (".", "..") — we skip those.
        if len(results) % 2 != 0:
            raise Usb2SnesError(f"odd-length List response: {results!r}")
        entries: list[DirEntry] = []
        for i in range(0, len(results), 2):
            flag, name = results[i], results[i + 1]
            if name in (".", ".."):
                continue
            entries.append(DirEntry(name=name, is_dir=(flag == "0")))
        return entries

    async def list_recursive(self, root: str = "/", *,
                             max_depth: int = 8,
                             exclude_dirs: tuple[str, ...] = ()) -> list[str]:
        """List all *file* paths under root. Slow on large SDs; cache results."""
        out: list[str] = []
        stack: list[tuple[str, int]] = [(root, 0)]
        while stack:
            path, depth = stack.pop()
            if depth > max_depth:
                continue
            try:
                entries = await self.list(path)
            except Usb2SnesError as exc:
                log.debug("list failed for %s: %s", path, exc)
                continue
            for e in entries:
                child = path.rstrip("/") + "/" + e.name
                if any(child.startswith(excl) for excl in exclude_dirs):
                    continue
                if e.is_dir:
                    stack.append((child, depth + 1))
                else:
                    out.append(child)
        return out

    async def get_file(self, path: str) -> bytes:
        await self._send_cmd("GetFile", path)
        results = await self._recv_text()
        if not results:
            raise Usb2SnesError(f"GetFile {path}: empty response")
        try:
            size = int(results[0], 16) if results[0].lower().startswith("0x") \
                                       else int(results[0], 16)
        except ValueError:
            # Some servers return decimal, others hex. Try both.
            try:
                size = int(results[0])
            except ValueError as exc:
                raise Usb2SnesError(
                    f"GetFile {path}: bad size {results[0]!r}") from exc
        return await self._recv_binary(size)

    async def put_file(self, path: str, data: bytes) -> None:
        await self._send_cmd("PutFile", path, f"{len(data):X}")
        if self._ws is None:
            raise Usb2SnesError("not connected")
        # Send as one binary frame; SNI accepts up to 2 MB easily.
        await self._ws.send(data)
