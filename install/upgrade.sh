#!/usr/bin/env bash
# RetroSync upgrade: pulls the latest source from GitHub and re-runs the
# installer. Idempotent — safe to run any time, even on a freshly-installed
# Pi. Re-execs itself under sudo if not already root.
#
# Usage:
#   retrosync upgrade            # via the CLI wrapper (preferred)
#   /usr/local/bin/retrosync-upgrade   # direct
#   sudo bash /opt/retrosync/install/upgrade.sh
#
# Honors RETROSYNC_DIR and RETROSYNC_USER if set; otherwise uses the
# install defaults.
set -euo pipefail

RETROSYNC_DIR="${RETROSYNC_DIR:-/opt/retrosync}"
RETROSYNC_USER="${RETROSYNC_USER:-retrosync}"

log()  { printf '\033[1;36m[upgrade]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[upgrade]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[upgrade]\033[0m %s\n' "$*" >&2; exit 1; }

if [[ ${EUID} -ne 0 ]]; then
  log "elevating via sudo for the installer step..."
  # Plain `sudo` (no -E) — some sudoers policies reject env preservation
  # with "not allowed to preserve the environment". Operators who need
  # to override RETROSYNC_DIR / RETROSYNC_USER should invoke directly:
  #   sudo RETROSYNC_DIR=... bash /opt/retrosync/install/upgrade.sh
  exec sudo "$0" "$@"
fi

[[ -d "${RETROSYNC_DIR}/.git" ]] || die \
  "${RETROSYNC_DIR} is not a git checkout; can't upgrade. "\
"Re-run setup.sh from a fresh clone instead."

log "fetching latest from origin..."
# Run git as the checkout's owner — git refuses to operate on a repo it
# doesn't own (the "dubious ownership" check) when invoked as root.
before="$(sudo -u "${RETROSYNC_USER}" git -C "${RETROSYNC_DIR}" rev-parse HEAD)"
sudo -u "${RETROSYNC_USER}" git -C "${RETROSYNC_DIR}" fetch --quiet origin
sudo -u "${RETROSYNC_USER}" git -C "${RETROSYNC_DIR}" pull --quiet --ff-only
after="$(sudo -u "${RETROSYNC_USER}" git -C "${RETROSYNC_DIR}" rev-parse HEAD)"

if [[ "${before}" == "${after}" ]]; then
  log "already at $(echo "${after}" | cut -c1-7); nothing to pull"
else
  log "updated $(echo "${before}" | cut -c1-7) -> $(echo "${after}" | cut -c1-7)"
  log "$(sudo -u "${RETROSYNC_USER}" git -C "${RETROSYNC_DIR}" log \
            --oneline "${before}..${after}" | head -10)"
fi

log "re-running installer..."
# SKIP_RCLONE_CONFIG: the OAuth flow already ran on first install. We don't
# want to re-prompt for the unchanged remote on every upgrade. setup.sh's
# ensure_rclone_remote() also detects an existing remote and exits early,
# but bypassing the function entirely keeps stdin requirements simple.
SKIP_RCLONE_CONFIG=1 exec bash "${RETROSYNC_DIR}/install/setup.sh" "$@"
