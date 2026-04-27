"""`find_config_path()` — probe order across env / per-user / system."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrosync import config as config_mod  # noqa: E402


def _check(actual, expected, label) -> bool:
    if actual != expected:
        print(f"FAIL: {label}: got {actual!r}, expected {expected!r}")
        return False
    print(f"ok:   {label}")
    return True


def test_env_var_wins() -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="rs-cfg-")) / "alt.yaml"
    tmp.write_text("cloud: {rclone_remote: gdrive:foo}\n")
    with mock.patch.dict(os.environ,
                         {"RETROSYNC_CONFIG": str(tmp)}, clear=False):
        return _check(config_mod.find_config_path(), str(tmp),
                      "RETROSYNC_CONFIG env wins")


def test_user_path_when_present() -> bool:
    """When the per-user path exists and the env var is unset, prefer
    the per-user path over the system path."""
    tmp = Path(tempfile.mkdtemp(prefix="rs-cfg-"))
    user_cfg = tmp / "user.yaml"
    user_cfg.write_text("cloud: {rclone_remote: gdrive:foo}\n")
    with mock.patch.dict(os.environ, {}, clear=False) as env, \
            mock.patch.object(config_mod, "USER_CONFIG_PATH", str(user_cfg)):
        env.pop("RETROSYNC_CONFIG", None)
        return _check(config_mod.find_config_path(), str(user_cfg),
                      "user path wins when no env var")


def test_falls_back_to_system_path() -> bool:
    """Per-user missing → falls back to system path (which may also
    be missing — caller will hit FileNotFoundError on actual load)."""
    tmp = Path(tempfile.mkdtemp(prefix="rs-cfg-"))
    user_missing = tmp / "missing.yaml"
    sys_path = tmp / "sys.yaml"
    sys_path.write_text("cloud: {rclone_remote: gdrive:foo}\n")
    with mock.patch.dict(os.environ, {}, clear=False) as env, \
            mock.patch.object(config_mod, "USER_CONFIG_PATH",
                              str(user_missing)), \
            mock.patch.object(config_mod, "DEFAULT_CONFIG_PATH",
                              str(sys_path)):
        env.pop("RETROSYNC_CONFIG", None)
        return _check(config_mod.find_config_path(), str(sys_path),
                      "system path used when user path missing")


def main() -> int:
    ok = True
    for name, fn in [
        ("env_var_wins", test_env_var_wins),
        ("user_path_when_present", test_user_path_when_present),
        ("falls_back_to_system_path", test_falls_back_to_system_path),
    ]:
        print(f"--- {name} ---")
        ok &= fn()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
