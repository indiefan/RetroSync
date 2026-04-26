"""SRM parser-config patcher: idempotency, unpatch, dry-run."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync.deck import srm  # noqa: E402


SAMPLE = [
    {
        "parserType": "Manual",
        "configTitle": "EmuDeck - SNES",
        "executable": {
            "path": "/usr/bin/retroarch",
            "appendArgsToExecutable": True,
        },
        "executableArgs": '-L /libretro/snes9x.so "${ROM_DIR}/${TITLE}.${EXTENSION}"',
    },
    {
        "parserType": "Manual",
        "configTitle": "EmuDeck - GBA",
        "executable": {
            "path": "/usr/bin/retroarch",
            "appendArgsToExecutable": True,
        },
        "executableArgs": '-L /libretro/mgba.so "${ROM_DIR}/${TITLE}.${EXTENSION}"',
    },
]


def _write_sample(path: Path) -> None:
    path.write_text(json.dumps(SAMPLE, indent=4))


def _check(actual, expected, label):
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


def test_patch_inserts_wrapper() -> bool:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-srm-"))
    cfg = workdir / "userConfigurations.json"
    wrapper = Path("/home/deck/.local/bin/retrosync-wrap")
    _write_sample(cfg)
    summary, parsers = srm.patch_srm_config(
        config_path=cfg, wrapper_path=wrapper)
    ok = _check(summary.patched, 2, "both parsers patched")
    ok &= _check(parsers[0]["executable"]["path"], str(wrapper),
                 "executable.path is now wrapper")
    args = parsers[0]["executableArgs"]
    ok &= _check(args.startswith('-- "/usr/bin/retroarch"'), True,
                 "executableArgs prefixed with -- and original exec")
    ok &= _check(srm.ORIG_KEY in parsers[0], True,
                 "_retrosync_original key stashed")
    return ok


def test_patch_idempotent() -> bool:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-srm-"))
    cfg = workdir / "userConfigurations.json"
    wrapper = Path("/home/deck/.local/bin/retrosync-wrap")
    _write_sample(cfg)
    srm.patch_srm_config(config_path=cfg, wrapper_path=wrapper)
    # Re-patch — should be no-op.
    summary2, _ = srm.patch_srm_config(config_path=cfg, wrapper_path=wrapper)
    return (_check(summary2.already_patched, 2, "already_patched=2")
            and _check(summary2.patched, 0, "no new patches"))


def test_unpatch_restores() -> bool:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-srm-"))
    cfg = workdir / "userConfigurations.json"
    wrapper = Path("/home/deck/.local/bin/retrosync-wrap")
    _write_sample(cfg)
    srm.patch_srm_config(config_path=cfg, wrapper_path=wrapper)
    summary, parsers = srm.patch_srm_config(
        config_path=cfg, wrapper_path=wrapper, unpatch=True)
    ok = _check(summary.unpatched, 2, "both parsers unpatched")
    ok &= _check(parsers[0]["executable"]["path"], "/usr/bin/retroarch",
                 "executable.path restored")
    ok &= _check(srm.ORIG_KEY in parsers[0], False,
                 "_retrosync_original removed after unpatch")
    return ok


def test_dry_run_does_not_write() -> bool:
    workdir = Path(tempfile.mkdtemp(prefix="retrosync-srm-"))
    cfg = workdir / "userConfigurations.json"
    wrapper = Path("/home/deck/.local/bin/retrosync-wrap")
    _write_sample(cfg)
    before = cfg.read_text()
    srm.patch_srm_config(config_path=cfg, wrapper_path=wrapper,
                         write=False)
    after = cfg.read_text()
    return _check(before, after, "dry-run leaves file untouched")


def main() -> int:
    ok = True
    for name, fn in [
        ("patch_inserts_wrapper", test_patch_inserts_wrapper),
        ("patch_idempotent", test_patch_idempotent),
        ("unpatch_restores", test_unpatch_restores),
        ("dry_run_does_not_write", test_dry_run_does_not_write),
    ]:
        print(f"--- {name} ---")
        ok &= fn()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
