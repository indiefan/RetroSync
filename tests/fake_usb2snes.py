"""A minimal stand-in for SNI/QUsb2snes for testing.

Listens on ws://127.0.0.1:23074 (configurable). Speaks just enough of the
usb2snes protocol for RetroSync to drive it: DeviceList, Attach, Name,
Info, List, GetFile, PutFile.

Backing store is an in-memory dict {path: bytes}. Pre-populate it via
the `files` constructor arg.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict

import websockets

log = logging.getLogger("fake_usb2snes")


class FakeCart:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 23074,
                 files: dict[str, bytes] | None = None):
        self._host = host
        self._port = port
        self.files: dict[str, bytes] = dict(files or {})
        self._device = "/dev/fake-fxpak"
        self._server: websockets.server.WebSocketServer | None = None

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handle, self._host, self._port, max_size=2**24)
        log.info("fake usb2snes listening on ws://%s:%d",
                 self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, ws):
        log.debug("client connected")
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    # Should only happen during PutFile, handled there.
                    log.warning("unexpected binary at top-level")
                    continue
                msg = json.loads(raw)
                op = msg.get("Opcode")
                operands = msg.get("Operands") or []
                handler = self._dispatch.get(op)
                if handler is None:
                    log.info("ignoring unknown opcode %r", op)
                    continue
                await handler(self, ws, operands)
        except websockets.ConnectionClosed:
            log.debug("client disconnected")

    # ----- handlers -----

    async def _h_device_list(self, ws, _):
        await ws.send(json.dumps({"Results": [self._device]}))

    async def _h_attach(self, ws, _):
        # No response. SNI does the same.
        return

    async def _h_name(self, ws, _):
        return  # No response.

    async def _h_info(self, ws, _):
        await ws.send(json.dumps({"Results": [
            "fake-firmware-1.10.3", "fake-version-12", "FAKEROM"]}))

    async def _h_list(self, ws, operands):
        path = (operands[0] if operands else "/")
        path = self._norm(path)
        log.debug("List %r", path)
        # Build a directory listing: every file/dir whose parent dir is path.
        entries: list[tuple[str, bool]] = []
        seen_dirs: set[str] = set()
        for f in self.files:
            parent = self._dirname(f)
            if parent == path:
                entries.append((self._basename(f), False))
            elif parent.startswith(path.rstrip("/") + "/") or (
                  path == "/" and parent != "/"):
                # f is in a subdirectory of `path`; report the immediate child dir.
                rest = parent[len(path):].lstrip("/")
                first = rest.split("/", 1)[0]
                if first not in seen_dirs:
                    seen_dirs.add(first)
                    entries.append((first, True))
        # Encode as flag/name pairs ('0'=dir, '1'=file).
        results: list[str] = []
        for name, is_dir in entries:
            results.append("0" if is_dir else "1")
            results.append(name)
        await ws.send(json.dumps({"Results": results}))

    async def _h_get_file(self, ws, operands):
        path = self._norm(operands[0])
        if path not in self.files:
            # Real SNI returns an error; for our purposes, returning size 0
            # is enough — daemon will treat as a transient.
            await ws.send(json.dumps({"Results": ["0"]}))
            return
        data = self.files[path]
        await ws.send(json.dumps({"Results": [f"{len(data):X}"]}))
        # Send as one binary frame (chunking would also be valid).
        await ws.send(data)

    async def _h_put_file(self, ws, operands):
        path = self._norm(operands[0])
        size = int(operands[1], 16)
        # Read one binary frame of `size` bytes.
        data = b""
        while len(data) < size:
            chunk = await ws.recv()
            if isinstance(chunk, str):
                raise RuntimeError("expected binary in PutFile")
            data += chunk
        self.files[path] = data[:size]
        log.info("PutFile %s (%d bytes)", path, size)

    @staticmethod
    def _norm(p: str) -> str:
        if not p.startswith("/"):
            p = "/" + p
        return p

    @staticmethod
    def _dirname(p: str) -> str:
        i = p.rfind("/")
        if i <= 0:
            return "/"
        return p[:i]

    @staticmethod
    def _basename(p: str) -> str:
        return p.rsplit("/", 1)[-1]

    _dispatch = {
        "DeviceList": _h_device_list,
        "Attach":     _h_attach,
        "Name":       _h_name,
        "Info":       _h_info,
        "List":       _h_list,
        "GetFile":    _h_get_file,
        "PutFile":    _h_put_file,
    }


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Sample contents.
    cart = FakeCart(files={
        "/Super Metroid.smc": b"\x00ROM\x00" * 1024,
        "/Super Metroid.srm": b"SAVE-A" + b"\x00" * (32*1024 - 6),
        "/A Link to the Past.smc": b"\xffROM\xff" * 1024,
        "/A Link to the Past.srm": b"ZELDA-A" + b"\x00" * (8*1024 - 7),
    })
    await cart.start()
    try:
        await asyncio.Future()
    except KeyboardInterrupt:
        await cart.stop()


if __name__ == "__main__":
    asyncio.run(main())
