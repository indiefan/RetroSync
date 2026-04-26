"""Inotify watcher debounce semantics — exercised against the
FakeInotifyEventQueue so the test runs on macOS / non-Linux too.

The fake mirrors the real watcher's per-key debounce: events for the
same key inside the window collapse to one handler call.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync.inotify_watch import FakeInotifyEventQueue  # noqa: E402


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


async def test_burst_collapses_to_one_call() -> bool:
    """Three events for the same game in quick succession → one call."""
    calls: list[tuple[str, list[Path]]] = []
    q = FakeInotifyEventQueue(debounce_seconds=0.1)
    q.set_handler(lambda key, paths: calls.append((key, paths)))
    base = Path("/tmp/Foo.srm")
    q.inject(base)
    q.inject(base)
    q.inject(base)
    await asyncio.sleep(0.2)
    return _check(len(calls), 1, "three events collapse to one call")


async def test_distinct_keys_fire_separately() -> bool:
    """Different stems → different debounce timers → separate calls."""
    calls: list[str] = []
    q = FakeInotifyEventQueue(debounce_seconds=0.1)
    q.set_handler(lambda key, _paths: calls.append(key))
    q.inject(Path("/tmp/Foo.srm"))
    q.inject(Path("/tmp/Bar.srm"))
    await asyncio.sleep(0.2)
    return _check(sorted(calls), ["Bar", "Foo"],
                  "two distinct keys → two calls")


async def test_event_during_window_extends_timer() -> bool:
    """An event inside the debounce window restarts the timer."""
    calls: list[float] = []
    q = FakeInotifyEventQueue(debounce_seconds=0.2)
    loop = asyncio.get_event_loop()
    q.set_handler(lambda key, _paths: calls.append(loop.time()))
    t0 = loop.time()
    q.inject(Path("/tmp/Foo.srm"))
    await asyncio.sleep(0.1)
    q.inject(Path("/tmp/Foo.srm"))
    await asyncio.sleep(0.5)
    if not _check(len(calls), 1, "still one call after extended burst"):
        return False
    elapsed = calls[0] - t0
    return _check(elapsed >= 0.3, True,
                  "fire happened ≥0.3s after first event "
                  "(reset by second)")


def main() -> int:
    ok = True
    for name, fn in [
        ("burst_collapses_to_one_call", test_burst_collapses_to_one_call),
        ("distinct_keys_fire_separately", test_distinct_keys_fire_separately),
        ("event_during_window_extends_timer",
         test_event_during_window_extends_timer),
    ]:
        print(f"--- {name} ---")
        ok &= asyncio.run(fn())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
