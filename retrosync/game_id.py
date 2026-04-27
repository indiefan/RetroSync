"""Canonical game-ID derivation and alias resolution.

The same game on different hardware (FXPak Pro cart, Analogue Pocket SD card,
EverDrive 64, Steam Deck RetroArch) needs to resolve to the same `<game_id>`
so its saves share cloud history. We do that by normalizing the save's
filename into a slug and consulting an operator-maintained alias table for
the corner cases where two valid filenames don't collapse on their own.

Slug normalization rules (in order):
  1. Take the basename of the path.
  2. Strip a final `.<ext>` ONLY if `<ext>` is in `KNOWN_EXTENSIONS`.
     This avoids `PurePosixPath.stem` mis-firing on filenames like
     `Foo (V1.2) [!]` (it'd treat `.2) [!]` as the extension).
  3. Strip parenthesized / bracketed tags like `(USA)`, `(En,Ja)`,
     `[!]`, `(V1.1)`.
  4. Lowercase.
  5. Replace any run of non-alphanumeric characters with a single `_`.
  6. Strip leading/trailing `_`.

Examples:
  "Super Metroid (USA, Europe).srm"          -> "super_metroid"
  "Chrono Trigger (U) [!].srm"               -> "chrono_trigger"
  "Star Wars (U) (V1.2) [!].srm"             -> "star_wars"
  "A Link to the Past.srm"                   -> "a_link_to_the_past"
  "Super Metroid.sav"                        -> "super_metroid"

Alias resolution:
  After computing the raw slug, look it up in `aliases`. The aliases table
  maps a canonical id to the list of raw slugs that should resolve to it.
  If the raw slug appears in any list, the canonical id is the key. If it
  doesn't appear anywhere, the raw slug itself is the canonical id.
"""
from __future__ import annotations

import re


# Parenthesized or bracketed group, with surrounding whitespace. Non-greedy
# so consecutive groups collapse cleanly.
_TAG_RE = re.compile(r"\s*[\(\[].*?[\)\]]\s*")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Save + ROM extensions we'll strip from the end of a filename. Anything
# else after a final `.` is treated as part of the name (so a filename
# like `Foo (V1.2) [!]` doesn't get butchered into `Foo (V1` by a naive
# `PurePosixPath.stem`).
KNOWN_EXTENSIONS = frozenset((
    # Save extensions
    "srm", "sav", "sra", "fla", "eep",
    "mpk", "mp1", "mp2", "mp3", "mp4",
    "state", "st0", "st1", "st2", "st3",
    # ROM extensions
    "sfc", "smc", "swc", "fig",
    "z64", "n64", "v64",
    "gb", "gbc", "gba",
    "nes", "unf", "unif",
    "md", "gen", "smd", "bin",
    "pce", "tg16",
    "iso", "cue", "chd",
))


def _strip_known_extension(name: str) -> str:
    """Return `name` minus its final `.<ext>` if `<ext>` is in
    KNOWN_EXTENSIONS; otherwise return `name` unchanged.

    Conservative replacement for `PurePosixPath.stem`, which strips
    ANY suffix after the last `.` — wrong for filenames that contain
    dots inside parenthesized tags (e.g. `(V1.2)`).
    """
    if "." not in name:
        return name
    head, ext = name.rsplit(".", 1)
    if ext.lower() in KNOWN_EXTENSIONS:
        return head
    return name


def canonical_slug(name: str) -> str:
    """Derive the canonical game-id slug from a save filename or stem.

    `name` may be a full path, a filename, or a bare stem.
    """
    base = name.rsplit("/", 1)[-1]
    stem = _strip_known_extension(base)
    stripped = _TAG_RE.sub(" ", stem)
    slug = _NON_ALNUM_RE.sub("_", stripped.lower()).strip("_")
    return slug or "unnamed"


def resolve_game_id(name: str, *,
                    aliases: dict[str, list[str]] | None = None) -> str:
    """Compute the canonical game id for a save filename.

    Looks up the raw slug in the alias table; if it appears in any group
    the group's key wins. Otherwise the raw slug is the id.
    """
    raw = canonical_slug(name)
    if not aliases:
        return raw
    for canonical, members in aliases.items():
        if raw == canonical:
            return canonical
        if raw in members:
            return canonical
    return raw
