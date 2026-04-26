"""inotify-driven directory watcher with per-key debouncing.

The Steam Deck daemon uses this to react within seconds to in-game
saves: RetroArch flushes the .srm, the kernel emits an IN_CLOSE_WRITE,
we wait briefly for the burst to settle, then fire a per-game sync.

Implementation note — we don't add an external dependency for this.
Linux's inotify is exposed via three syscalls (`inotify_init1`,
`inotify_add_watch`, `read`); we wire them up via ctypes. That keeps
the dependency footprint at "must be Linux", which the EmuDeck design
already requires.

Public surface:

  watcher = InotifyWatcher()
  watcher.add_path(Path("/home/deck/Emulation/saves/retroarch/saves"))
  await watcher.run(handler=on_event, stop=stop_event,
                    debounce_seconds=5, key_for=lambda p: derived_key)

`handler(key, paths)` is called once per debounce-key after `paths`
have been quiet for `debounce_seconds`. Multiple events for the same
key inside the window collapse to one handler call.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import errno
import logging
import os
import struct
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# inotify event flags. Subset we care about.
IN_MODIFY      = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO    = 0x00000080
IN_CREATE      = 0x00000100
IN_DELETE      = 0x00000200
IN_ONLYDIR     = 0x01000000
IN_ISDIR       = 0x40000000

# Events the daemon cares about: a save was rewritten in-place
# (CLOSE_WRITE) or atomically replaced (MOVED_TO).
DEFAULT_MASK = IN_CLOSE_WRITE | IN_MOVED_TO

# inotify_init1 flags.
IN_NONBLOCK = 0x800
IN_CLOEXEC = 0x80000


# Resolved lazily so importing this module on a non-Linux host (macOS
# unit tests, FreeBSD, etc.) doesn't blow up at import time. The
# Deck-side daemon is the only consumer that actually instantiates an
# InotifyWatcher; everywhere else the symbols are never touched.
_libc = None


def _ensure_libc() -> ctypes.CDLL:
    global _libc
    if _libc is not None:
        return _libc
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6",
                       use_errno=True)
    libc.inotify_init1.argtypes = [ctypes.c_int]
    libc.inotify_init1.restype = ctypes.c_int
    libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p,
                                       ctypes.c_uint32]
    libc.inotify_add_watch.restype = ctypes.c_int
    libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.inotify_rm_watch.restype = ctypes.c_int
    _libc = libc
    return libc


# struct inotify_event: int32 wd, uint32 mask, uint32 cookie, uint32 len
_HEADER_FMT = "iIII"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)


@dataclass
class _PendingFire:
    paths: set[Path] = field(default_factory=set)
    handle: asyncio.TimerHandle | None = None


class InotifyWatcher:
    """Linux inotify wrapper with asyncio-friendly debouncing.

    Lifecycle:
      1. Construct.
      2. add_path(...) for each directory you want to watch.
      3. await run(handler, stop, ...).

    Stop by setting the `stop` Event. `run` cleans up the inotify fd
    and any pending debounce timers.
    """

    def __init__(self):
        libc = _ensure_libc()
        self._libc = libc
        self._fd = libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self._fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), "inotify_init1")
        self._wds: dict[int, Path] = {}    # wd -> root path
        self._closed = False

    def add_path(self, root: Path, *,
                 mask: int = DEFAULT_MASK) -> None:
        if not root.exists():
            raise FileNotFoundError(f"inotify add_path: {root} missing")
        wd = self._libc.inotify_add_watch(self._fd, str(root).encode(),
                                          mask | IN_ONLYDIR)
        if wd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err),
                          f"inotify_add_watch({root})")
        self._wds[wd] = root
        log.info("inotify: watching %s (wd=%d, mask=0x%x)", root, wd, mask)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._fd)
        except OSError:
            pass

    def _read_events(self) -> list[tuple[int, int, str]]:
        """Drain pending events. Returns list of (wd, mask, name)."""
        try:
            buf = os.read(self._fd, 65536)
        except BlockingIOError:
            return []
        events: list[tuple[int, int, str]] = []
        offset = 0
        while offset + _HEADER_SIZE <= len(buf):
            wd, mask, _cookie, name_len = struct.unpack_from(
                _HEADER_FMT, buf, offset)
            offset += _HEADER_SIZE
            name = buf[offset:offset + name_len].rstrip(b"\x00").decode(
                errors="replace") if name_len else ""
            offset += name_len
            events.append((wd, mask, name))
        return events

    async def run(self, *,
                  handler: Callable[[str, list[Path]],
                                    Awaitable[None] | None],
                  stop: asyncio.Event,
                  debounce_seconds: float = 5.0,
                  key_for: Callable[[Path], str] | None = None,
                  filter_path: Callable[[Path], bool] | None = None,
                  ) -> None:
        """Loop forever, dispatching debounced calls to `handler`.

        `key_for(path)` collapses related events under one debounce
        timer. Common pick: the canonical game-id slug, so writes to
        `Foo.srm` plus `Foo.state` plus `Foo.srm.bak` all collapse
        into one sync of game `foo`. Defaults to the file's stem.

        `filter_path(path) -> bool` lets the caller skip events for
        files outside the configured save extensions, etc.

        `handler(key, paths)` may be sync or async.
        """
        if key_for is None:
            key_for = lambda p: p.stem
        loop = asyncio.get_running_loop()
        pending: dict[str, _PendingFire] = {}

        def fire(key: str) -> None:
            entry = pending.pop(key, None)
            if entry is None:
                return
            paths = sorted(entry.paths)
            log.debug("inotify: debounce fired key=%s paths=%s",
                      key, [str(p) for p in paths])
            result = handler(key, paths)
            if asyncio.iscoroutine(result):
                # Schedule and fire-and-forget; handler errors are logged
                # on the orphan task to surface them in the journal.
                async def _run_and_log() -> None:
                    try:
                        await result
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "inotify handler errored for key=%s", key)
                asyncio.create_task(_run_and_log())

        try:
            stop_task = asyncio.create_task(stop.wait())
            while not stop.is_set():
                # Wait for either readability of the inotify fd or stop.
                read_event = asyncio.Event()
                loop.add_reader(self._fd, read_event.set)
                read_task = asyncio.create_task(read_event.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {read_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED)
                finally:
                    loop.remove_reader(self._fd)
                    if not read_task.done():
                        read_task.cancel()
                if stop.is_set():
                    break
                events = self._read_events()
                for wd, _mask, name in events:
                    root = self._wds.get(wd)
                    if root is None:
                        continue
                    full = root / name if name else root
                    if filter_path is not None and not filter_path(full):
                        continue
                    key = key_for(full)
                    entry = pending.get(key)
                    if entry is None:
                        entry = _PendingFire()
                        pending[key] = entry
                    entry.paths.add(full)
                    if entry.handle is not None:
                        entry.handle.cancel()
                    entry.handle = loop.call_later(
                        debounce_seconds, fire, key)
        finally:
            for entry in pending.values():
                if entry.handle is not None:
                    entry.handle.cancel()
            self.close()


# --------------------------------------------------------------------------
# Convenience: synchronous batch debouncer for non-Linux unit tests.
# --------------------------------------------------------------------------

class FakeInotifyEventQueue:
    """In-process replacement for InotifyWatcher used by unit tests.

    Tests inject events with `inject(path)`, then drive the asyncio loop
    until the debounce timers fire. Mirrors `run`'s debounce semantics
    so tests can exercise the same handler logic without inotify."""

    def __init__(self, *, debounce_seconds: float = 0.5,
                 key_for: Callable[[Path], str] | None = None):
        self._debounce = debounce_seconds
        self._key_for = key_for or (lambda p: p.stem)
        self._pending: dict[str, _PendingFire] = {}
        self._handler: Callable | None = None

    def set_handler(self, handler: Callable[[str, list[Path]], None]) -> None:
        self._handler = handler

    def inject(self, path: Path) -> None:
        loop = asyncio.get_event_loop()
        key = self._key_for(path)
        entry = self._pending.get(key)
        if entry is None:
            entry = _PendingFire()
            self._pending[key] = entry
        entry.paths.add(path)
        if entry.handle is not None:
            entry.handle.cancel()
        entry.handle = loop.call_later(self._debounce, self._fire, key)

    def _fire(self, key: str) -> None:
        entry = self._pending.pop(key, None)
        if entry is None or self._handler is None:
            return
        self._handler(key, sorted(entry.paths))
