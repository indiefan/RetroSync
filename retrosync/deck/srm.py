"""Steam ROM Manager parser-config patcher.

EmuDeck installs Steam ROM Manager and ships parser configurations at
`~/.config/steam-rom-manager/userData/userConfigurations.json`. SRM
generates non-Steam-game shortcuts from these configs; each shortcut's
launch command is the parser's `executable.path` + `executableArgs`.

We patch each parser to invoke our wrapper (`~/.local/bin/retrosync-wrap`)
in front of the original executable. After patching, the operator
re-runs SRM's "Save to Steam" once and every Steam shortcut now
calls retrosync-wrap before the emulator.

Idempotent: re-patching is a no-op (we detect already-wrapped parsers
by looking for our wrapper in the executable path).
`unpatch=True` reverses the change.

Layout we read/write:

  {
    "parserType": "Manual",
    "configTitle": "EmuDeck - SNES",
    "executable": {"path": "${RETROARCH}", "appendArgsToExecutable": true},
    "executableArgs": "-L ${LIBRETRO_CORE} \\"${ROM_DIR}/${TITLE}.${EXTENSION}\\"",
    ...
  }

After patching:

  {
    "executable": {"path": "/home/deck/.local/bin/retrosync-wrap",
                   "appendArgsToExecutable": true},
    "executableArgs": "-- \\"${RETROARCH}\\" -L ${LIBRETRO_CORE} \\"${ROM_DIR}/${TITLE}.${EXTENSION}\\"",
    "_retrosync_original": {
        "executable": {...},
        "executableArgs": "...",
    },
    ...
  }

We stash the originals under `_retrosync_original` so unpatch can
restore them precisely without depending on knowing what they were.
"""
from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_SRM_CONFIG_PATH = (
    Path.home() / ".config/steam-rom-manager/userData/userConfigurations.json")
DEFAULT_WRAPPER_PATH = (
    Path.home() / ".local/bin/retrosync-wrap")
ORIG_KEY = "_retrosync_original"


@dataclass
class PatchSummary:
    parsers_total: int = 0
    patched: int = 0
    already_patched: int = 0
    skipped: int = 0
    unpatched: int = 0


def patch_srm_config(*, config_path: Path = DEFAULT_SRM_CONFIG_PATH,
                     wrapper_path: Path = DEFAULT_WRAPPER_PATH,
                     unpatch: bool = False,
                     write: bool = True) -> tuple[PatchSummary, list[dict]]:
    """Patch (or unpatch) every parser in the SRM config.

    `write=False` is the dry-run path: returns the would-be config
    without touching disk. The caller can diff before committing.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"SRM config not found at {config_path}")
    raw = config_path.read_text()
    parsers = json.loads(raw)
    summary = PatchSummary()
    if not isinstance(parsers, list):
        raise ValueError(
            f"SRM config at {config_path} is not a JSON array of parsers")
    out = []
    for p in parsers:
        summary.parsers_total += 1
        if not isinstance(p, dict):
            summary.skipped += 1
            out.append(p)
            continue
        if unpatch:
            out.append(_unpatch_parser(p, summary=summary))
        else:
            out.append(_patch_parser(p, wrapper_path=wrapper_path,
                                     summary=summary))
    if write:
        config_path.write_text(json.dumps(out, indent=4))
    return summary, out


def _patch_parser(parser: dict, *, wrapper_path: Path,
                  summary: PatchSummary) -> dict:
    """Patch one parser in-place and return it.

    Detection rule: if the parser has `_retrosync_original` OR its
    `executable.path` is already our wrapper, treat as already
    patched.
    """
    executable = parser.get("executable") or {}
    exec_path = (executable.get("path") if isinstance(executable, dict)
                 else None)
    args = parser.get("executableArgs") or ""
    if (ORIG_KEY in parser
            or (isinstance(exec_path, str)
                and exec_path == str(wrapper_path))):
        summary.already_patched += 1
        return parser
    if not exec_path or not isinstance(args, str):
        summary.skipped += 1
        return parser
    out = dict(parser)
    out[ORIG_KEY] = {
        "executable": dict(executable),
        "executableArgs": args,
    }
    out["executable"] = {
        "path": str(wrapper_path),
        "appendArgsToExecutable": True,
    }
    # Rewrite args to: `-- "<original-exec>" <original-args>`. Quoting
    # the exec path keeps SRM's variable expansion happy when
    # `${RETROARCH}` resolves to a path with spaces.
    out["executableArgs"] = (
        f'-- "{exec_path}" {args}' if args.strip()
        else f'-- "{exec_path}"')
    summary.patched += 1
    return out


def _unpatch_parser(parser: dict, *, summary: PatchSummary) -> dict:
    """Restore the pre-patch executable / executableArgs."""
    orig = parser.get(ORIG_KEY)
    if not isinstance(orig, dict):
        summary.skipped += 1
        return parser
    out = {k: v for k, v in parser.items() if k != ORIG_KEY}
    out["executable"] = orig.get("executable", out.get("executable"))
    out["executableArgs"] = orig.get(
        "executableArgs", out.get("executableArgs", ""))
    summary.unpatched += 1
    return out
