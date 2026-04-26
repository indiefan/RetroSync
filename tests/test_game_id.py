"""Unit tests for slug normalization and alias resolution.

Run with:
    PYTHONPATH=. python3 tests/test_game_id.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync.game_id import canonical_slug, resolve_game_id  # noqa: E402


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


def test_canonical_slug() -> bool:
    cases = [
        ("Super Metroid (USA, Europe).srm",          "super_metroid"),
        ("Super Metroid (USA, Europe) (En,Ja).srm",  "super_metroid"),
        ("Chrono Trigger (U) [!].srm",               "chrono_trigger"),
        ("A Link to the Past.srm",                   "a_link_to_the_past"),
        ("Super Metroid.sav",                        "super_metroid"),
        ("/Saves/agg23.SNES/Super Metroid.sav",      "super_metroid"),
        ("/sd2snes/saves/Super Metroid.srm",         "super_metroid"),
        ("Final Fantasy III (V1.1).srm",             "final_fantasy_iii"),
        ("Earthbound.srm",                           "earthbound"),
        ("",                                         "unnamed"),
        ("(USA).srm",                                "unnamed"),
    ]
    ok = True
    for name, want in cases:
        ok &= _check(canonical_slug(name), want, f"slug({name!r})")
    return ok


def test_aliases() -> bool:
    aliases = {
        "super_metroid": [
            "super_metroid_jpn",
            "super_metroid_classic_mini",
        ],
    }
    ok = True
    ok &= _check(
        resolve_game_id("Super Metroid (USA).srm", aliases=aliases),
        "super_metroid",
        "alias passthrough (canonical name matches raw slug)",
    )
    ok &= _check(
        resolve_game_id("super_metroid_jpn.sav", aliases=aliases),
        "super_metroid",
        "alias collapse (raw slug listed under canonical)",
    )
    ok &= _check(
        resolve_game_id("Chrono Trigger.srm", aliases=aliases),
        "chrono_trigger",
        "no alias match -> raw slug",
    )
    return ok


def main() -> int:
    ok = True
    print("--- canonical_slug ---")
    ok &= test_canonical_slug()
    print("--- aliases ---")
    ok &= test_aliases()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
