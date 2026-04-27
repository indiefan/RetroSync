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


def test_goodtools_revision_tag_does_not_leak_into_slug() -> bool:
    """Regression: PurePosixPath.stem mis-fired on filenames like
    `Foo (V1.2) [!]` (no explicit extension) — it'd treat `.2) [!]`
    as the extension, chop the name to `Foo (V1`, and produce slugs
    ending in a stray `_v1`. This is the failure mode that produced
    duplicate cloud folders like `star_wars_shadows_of_the_empire_v1`
    next to `star_wars_shadows_of_the_empire`.

    Both forms of input must collapse to the canonical slug:
      - filename with extension: `Foo (V1.2) [!].srm`
      - bare stem (some adapters pre-strip): `Foo (V1.2) [!]`
    """
    cases = [
        ("Star Wars - Shadows of the Empire (U) (V1.2) [!].srm",
         "star_wars_shadows_of_the_empire"),
        ("Star Wars - Shadows of the Empire (U) (V1.2) [!]",
         "star_wars_shadows_of_the_empire"),
        ("Cruis_n USA (U) (V1.2) [!].eep",      "cruis_n_usa"),
        ("Cruis_n USA (U) (V1.2) [!]",          "cruis_n_usa"),
        ("Star Fox 64 (U) (V1.1) [!].eep",      "star_fox_64"),
        ("Star Fox 64 (U) (V1.1) [!]",          "star_fox_64"),
        ("Wave Race 64 (U) (V1.1) [!].eep",     "wave_race_64"),
    ]
    ok = True
    for name, want in cases:
        ok &= _check(canonical_slug(name), want, f"slug({name!r})")
    return ok


def test_cart_and_no_intro_collapse_to_same_slug() -> bool:
    """Real-world cross-source case: cart writes GoodTools-named
    saves, Deck has No-Intro ROMs. They MUST resolve to the same
    canonical slug or cloud sync routes them to different folders."""
    cart_save = "Star Wars - Shadows of the Empire (U) (V1.2) [!].srm"
    deck_rom  = "Star Wars - Shadows of the Empire (USA).z64"
    return _check(canonical_slug(cart_save), canonical_slug(deck_rom),
                  "cart save and Deck ROM resolve to same slug")


def test_unknown_extension_treated_as_part_of_name() -> bool:
    """Conservative behavior: only known save/ROM extensions are
    stripped. A `.bak` or other unknown suffix stays in the name to
    avoid false-positive stripping of dotted tags."""
    cases = [
        ("Star Wars (USA).z64.bak",  "star_wars_z64_bak"),
        ("Some Game",                "some_game"),
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
    print("--- goodtools_revision_tag_does_not_leak_into_slug ---")
    ok &= test_goodtools_revision_tag_does_not_leak_into_slug()
    print("--- cart_and_no_intro_collapse_to_same_slug ---")
    ok &= test_cart_and_no_intro_collapse_to_same_slug()
    print("--- unknown_extension_treated_as_part_of_name ---")
    ok &= test_unknown_extension_treated_as_part_of_name()
    print("--- aliases ---")
    ok &= test_aliases()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
