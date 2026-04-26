"""Canonical game-ID derivation and alias resolution.

The same game on different hardware (FXPak Pro cart, Analogue Pocket SD card)
needs to resolve to the same `<game_id>` so its saves share cloud history.
We do that by normalizing the save's filename stem into a slug and consulting
an operator-maintained alias table for the corner cases where two valid
filenames don't collapse on their own.

Slug normalization rules (in order):
  1. Take the filename stem (no directory, no extension).
  2. Strip parenthesized / bracketed tags like `(USA)`, `(En,Ja)`, `[!]`.
  3. Lowercase.
  4. Replace any run of non-alphanumeric characters with a single `_`.
  5. Strip leading/trailing `_`.

Examples:
  "Super Metroid (USA, Europe).srm"          -> "super_metroid"
  "Chrono Trigger (U) [!].srm"               -> "chrono_trigger"
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
from pathlib import PurePosixPath


# Parenthesized or bracketed group, with surrounding whitespace. Non-greedy
# so consecutive groups collapse cleanly. Same regex as the FXPak adapter
# used pre-refactor; pulled here so the Pocket adapter can share it.
_TAG_RE = re.compile(r"\s*[\(\[].*?[\)\]]\s*")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def canonical_slug(name: str) -> str:
    """Derive the canonical game-id slug from a save filename or stem.

    `name` may be a full path, just a filename, or a bare stem; the function
    strips the directory and extension itself.
    """
    stem = PurePosixPath(name).stem
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
