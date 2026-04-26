#!/usr/bin/env bash
# RetroSync installer.
#
# Usage on the Pi (after SSHing in):
#
#   curl -fsSL https://raw.githubusercontent.com/indiefan/RetroSync/main/install/setup.sh | sudo bash
#
# Or, if you've cloned the repo yourself:
#
#   sudo bash install/setup.sh
#
# What it does:
#   1. Apt-installs the small set of system deps.
#   2. Creates a dedicated 'retrosync' system user.
#   3. Fetches the latest SNI release for the Pi's architecture.
#   4. Installs rclone via the official installer.
#   5. Clones (or updates) the RetroSync repo to /opt/retrosync and
#      creates a Python venv with the daemon installed.
#   6. Lays down /etc/retrosync/config.yaml from the example.
#   7. Installs the systemd units, enables them, and starts SNI.
#   8. Walks you through `rclone config` for Google Drive (interactive).
#   9. Starts the daemon.
#
# Re-running the script is safe: it's idempotent. If you change config,
# just `sudo systemctl restart retrosync`.

set -euo pipefail

# -------- knobs ----------------------------------------------------------
RETROSYNC_REPO="${RETROSYNC_REPO:-https://github.com/indiefan/RetroSync.git}"
RETROSYNC_REF="${RETROSYNC_REF:-main}"
RETROSYNC_DIR="${RETROSYNC_DIR:-/opt/retrosync}"
RETROSYNC_USER="${RETROSYNC_USER:-retrosync}"
RETROSYNC_HOME="/home/${RETROSYNC_USER}"
RETROSYNC_DATA="/var/lib/retrosync"
RETROSYNC_ETC="/etc/retrosync"
RETROSYNC_LOCAL_SOURCE="${RETROSYNC_LOCAL_SOURCE:-}"   # optional: path to local checkout

SNI_VERSION="${SNI_VERSION:-latest}"
SNI_BINARY="/usr/local/bin/sni"

# -------- helpers --------------------------------------------------------
log()  { printf '\033[1;36m[retrosync]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[retrosync]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[retrosync]\033[0m %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ ${EUID} -eq 0 ]] || die "must run as root (use sudo)"
}

detect_arch() {
  local m
  m="$(uname -m)"
  case "${m}" in
    aarch64|arm64) echo "linux-arm64" ;;
    armv7l|armv6l) echo "linux-arm32v7" ;;
    x86_64)        echo "linux-amd64" ;;
    *) die "unsupported architecture: ${m}" ;;
  esac
}

# -------- step 1: apt deps -----------------------------------------------
install_apt_deps() {
  log "installing system dependencies (apt)..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # dbus-x11 provides /usr/bin/dbus-run-session, used to wrap SNI so its
  # systray code has a session bus to attach to on a headless box (without
  # this, SNI segfaults at startup).
  apt-get install -yq \
    ca-certificates curl jq git \
    python3 python3-venv python3-pip \
    sqlite3 \
    dbus-x11 \
    unzip
}

# -------- step 2: user, dirs ---------------------------------------------
ensure_user_and_dirs() {
  if ! id -u "${RETROSYNC_USER}" >/dev/null 2>&1; then
    log "creating system user ${RETROSYNC_USER}"
    useradd --system --create-home --home-dir "${RETROSYNC_HOME}" \
            --shell /usr/sbin/nologin "${RETROSYNC_USER}"
  fi
  # The cart will appear as /dev/ttyACM*; dialout group grants access on
  # Raspberry Pi OS / Debian.
  usermod -aG dialout "${RETROSYNC_USER}" || true

  install -d -o "${RETROSYNC_USER}" -g "${RETROSYNC_USER}" -m 0755 \
    "${RETROSYNC_DATA}" \
    "${RETROSYNC_DATA}/fxpak-cache" \
    "${RETROSYNC_DATA}/sni-home" \
    "${RETROSYNC_HOME}/.config" \
    "${RETROSYNC_HOME}/.config/rclone"
  install -d -m 0755 "${RETROSYNC_ETC}"

  # Defensive: rclone.conf and state.db must be readable by the retrosync
  # user (the daemon and CLI both run as them). On some systems an earlier
  # `sudo` operation re-owned these to root, which makes the daemon and
  # CLI fail with "permission denied". Re-chown if they exist; harmless
  # if they don't.
  for f in "${RETROSYNC_DATA}/rclone.conf" \
           "${RETROSYNC_DATA}/state.db" \
           "${RETROSYNC_DATA}/state.db-shm" \
           "${RETROSYNC_DATA}/state.db-wal"; do
    if [[ -e "${f}" ]]; then
      chown "${RETROSYNC_USER}:${RETROSYNC_USER}" "${f}"
    fi
  done
  if [[ -e "${RETROSYNC_DATA}/rclone.conf" ]]; then
    chmod 0600 "${RETROSYNC_DATA}/rclone.conf"
  fi
}

# -------- step 3: SNI ----------------------------------------------------
install_sni() {
  if [[ -x "${SNI_BINARY}" && "${SNI_FORCE:-0}" != "1" ]]; then
    log "SNI already at ${SNI_BINARY}; skipping (set SNI_FORCE=1 to reinstall)"
    return
  fi
  local arch tag asset_url tmpdir
  arch="$(detect_arch)"
  log "fetching SNI release info (${SNI_VERSION}, ${arch})..."

  if [[ "${SNI_VERSION}" == "latest" ]]; then
    tag="$(curl -fsSL https://api.github.com/repos/alttpo/sni/releases/latest \
            | jq -r '.tag_name')"
  else
    tag="${SNI_VERSION}"
  fi
  log "  -> SNI tag = ${tag}"

  # SNI release assets are named like: sni-v0.0.96-linux-arm64.tar.xz
  asset_url="$(curl -fsSL "https://api.github.com/repos/alttpo/sni/releases/tags/${tag}" \
    | jq -r --arg arch "${arch}" '.assets[] | select(.name | endswith("-" + $arch + ".tar.xz")) | .browser_download_url' \
    | head -n1)"

  if [[ -z "${asset_url}" || "${asset_url}" == "null" ]]; then
    die "no SNI asset for ${arch} at tag ${tag}; see https://github.com/alttpo/sni/releases"
  fi

  log "  -> downloading ${asset_url}"
  tmpdir="$(mktemp -d)"
  trap 'rm -rf "${tmpdir}"' RETURN
  curl -fsSL "${asset_url}" -o "${tmpdir}/sni.tar.xz"
  tar -xJf "${tmpdir}/sni.tar.xz" -C "${tmpdir}"
  local extracted
  extracted="$(find "${tmpdir}" -maxdepth 2 -type f -name sni | head -n1)"
  [[ -n "${extracted}" ]] || die "SNI binary not found inside archive"
  install -m 0755 "${extracted}" "${SNI_BINARY}"
  log "installed SNI -> ${SNI_BINARY}"
}

# -------- step 4: rclone -------------------------------------------------
install_rclone() {
  if command -v rclone >/dev/null 2>&1; then
    log "rclone already installed: $(rclone --version | head -n1)"
    return
  fi
  log "installing rclone via official installer..."
  curl -fsSL https://rclone.org/install.sh | bash
}

# -------- step 5: app code + venv ----------------------------------------
install_retrosync_app() {
  if [[ -n "${RETROSYNC_LOCAL_SOURCE}" ]]; then
    log "copying local source from ${RETROSYNC_LOCAL_SOURCE} -> ${RETROSYNC_DIR}"
    rm -rf "${RETROSYNC_DIR}"
    cp -a "${RETROSYNC_LOCAL_SOURCE}" "${RETROSYNC_DIR}"
  elif [[ -d "${RETROSYNC_DIR}/.git" ]]; then
    log "updating existing checkout at ${RETROSYNC_DIR}"
    # The checkout is owned by retrosync; running git as root against it
    # trips git's "dubious ownership" check. Run as the owning user.
    sudo -u "${RETROSYNC_USER}" git -C "${RETROSYNC_DIR}" \
       fetch --quiet origin
    sudo -u "${RETROSYNC_USER}" git -C "${RETROSYNC_DIR}" \
       checkout --quiet "${RETROSYNC_REF}"
    sudo -u "${RETROSYNC_USER}" git -C "${RETROSYNC_DIR}" \
       pull --quiet --ff-only origin "${RETROSYNC_REF}"
  else
    log "cloning ${RETROSYNC_REPO} (${RETROSYNC_REF}) -> ${RETROSYNC_DIR}"
    git clone --quiet --branch "${RETROSYNC_REF}" \
              "${RETROSYNC_REPO}" "${RETROSYNC_DIR}"
  fi
  chown -R "${RETROSYNC_USER}:${RETROSYNC_USER}" "${RETROSYNC_DIR}"

  log "building venv at ${RETROSYNC_DIR}/.venv"
  sudo -u "${RETROSYNC_USER}" python3 -m venv "${RETROSYNC_DIR}/.venv"
  sudo -u "${RETROSYNC_USER}" "${RETROSYNC_DIR}/.venv/bin/pip" \
       install --quiet --upgrade pip
  sudo -u "${RETROSYNC_USER}" "${RETROSYNC_DIR}/.venv/bin/pip" \
       install --quiet "${RETROSYNC_DIR}"

  # The setuptools-generated console_script entry points spent multiple
  # seconds hanging at startup on Python 3.13 / Pi OS aarch64 — likely
  # legacy pkg_resources discovery. We never actually need that lookup
  # since we know the entry function. Replace the generated scripts with
  # tiny bash trampolines that use `python -m` directly. Path stays
  # identical so the systemd unit and sudoers entry don't have to change.
  for entry in retrosync retrosyncd; do
    target="${RETROSYNC_DIR}/.venv/bin/${entry}"
    case "${entry}" in
      retrosync)  module="retrosync.cli" ;;
      retrosyncd) module="retrosync.daemon" ;;
    esac
    cat > "${target}" <<EOF
#!/usr/bin/env bash
# RetroSync ${entry} entry point. Bypasses setuptools' console_script shim
# which was hanging at startup on this platform. Regenerated by setup.sh
# every install; do not edit by hand.
exec ${RETROSYNC_DIR}/.venv/bin/python -m ${module} "\$@"
EOF
    chown "${RETROSYNC_USER}:${RETROSYNC_USER}" "${target}"
    chmod 0755 "${target}"
  done

  # The CLI needs to read both /etc/retrosync/config.yaml and the rclone
  # OAuth config in /var/lib/retrosync/. The rclone config holds a refresh
  # token (mode 0600, retrosync-only), so rather than open it up, install
  # a wrapper that auto-elevates to the retrosync user. Operators who are
  # in the `sudo` group (pi is by default on Pi OS) can run `retrosync ...`
  # without typing a password thanks to the sudoers entry below.
  #
  # The `upgrade` subcommand is special-cased: it needs root for the
  # installer step, so it routes to retrosync-upgrade BEFORE we sudo down
  # to the unprivileged retrosync user.
  #
  # Older installs left /usr/local/bin/retrosync as a symlink pointing at
  # the venv-side trampoline. `cat >` follows symlinks, so writing the
  # wrapper here would have clobbered the trampoline (causing the wrapper
  # to recursively exec itself). `rm -f` first to break any such symlink
  # — `cat >` then creates a fresh regular file at /usr/local/bin.
  rm -f /usr/local/bin/retrosync
  cat > /usr/local/bin/retrosync <<'WRAPPER'
#!/usr/bin/env bash
# RetroSync CLI wrapper. Generated by setup.sh; do not edit by hand
# (will be overwritten on upgrade).
set -e
TARGET=/opt/retrosync/.venv/bin/retrosync

if [[ "${1:-}" == "upgrade" ]]; then
  shift
  exec /usr/local/bin/retrosync-upgrade "$@"
fi

# pocket-sync needs root for mount/umount/udisksctl. Don't drop to the
# retrosync user when the caller is already root (typical: systemd unit
# or `sudo retrosync pocket-sync ...`).
if [[ "${1:-}" == "pocket-sync" && "$(id -u)" == "0" ]]; then
  exec "$TARGET" "$@"
fi

if [[ "$(id -un)" == "retrosync" ]]; then
  exec "$TARGET" "$@"
fi
exec sudo -u retrosync "$TARGET" "$@"
WRAPPER
  chmod 0755 /usr/local/bin/retrosync
  # The daemon binary is invoked by systemd directly; symlink is fine.
  ln -sf "${RETROSYNC_DIR}/.venv/bin/retrosyncd" /usr/local/bin/retrosyncd
  # Standalone upgrade entry point. Runs as root, pulls + re-runs setup.sh.
  ln -sf "${RETROSYNC_DIR}/install/upgrade.sh" /usr/local/bin/retrosync-upgrade
  chmod +x "${RETROSYNC_DIR}/install/upgrade.sh"

  # Sudoers: allow members of the sudo group to run the wrapper's target as
  # the retrosync user without a password. visudo -cf validates first so a
  # broken file can't lock anyone out.
  local sudoers="/etc/sudoers.d/retrosync"
  cat > "${sudoers}.tmp" <<EOF
# Installed by RetroSync setup.sh. Allows operators (members of the sudo
# group) to run the retrosync CLI as the retrosync system user without a
# password. The CLI is the only thing they can run this way.
%sudo ALL=(retrosync) NOPASSWD: ${RETROSYNC_DIR}/.venv/bin/retrosync
EOF
  if visudo -cf "${sudoers}.tmp" >/dev/null; then
    mv "${sudoers}.tmp" "${sudoers}"
    chmod 0440 "${sudoers}"
    log "installed sudoers entry at ${sudoers}"
  else
    rm -f "${sudoers}.tmp"
    warn "sudoers entry failed validation; CLI will require explicit "
    warn "'sudo -u retrosync' until this is resolved."
  fi
}

# -------- step 6: config -------------------------------------------------
write_config() {
  local cfg="${RETROSYNC_ETC}/config.yaml"
  if [[ -f "${cfg}" ]]; then
    log "config exists at ${cfg}; leaving it alone"
    return
  fi
  log "writing default config to ${cfg}"
  "${RETROSYNC_DIR}/.venv/bin/python" -c "
from retrosync.config import Config
print(Config.example_yaml())
" > "${cfg}"
  # config.yaml has no secrets (rclone OAuth lives elsewhere in retrosync's
  # home dir). Leave it world-readable so the CLI works no matter who runs
  # it. The wrapper at /usr/local/bin/retrosync handles the rclone-config
  # access via sudo to the retrosync user.
  chown root:"${RETROSYNC_USER}" "${cfg}"
  chmod 0644 "${cfg}"
}

# -------- step 7: systemd ------------------------------------------------
install_systemd_units() {
  log "installing systemd units"
  install -m 0644 "${RETROSYNC_DIR}/install/systemd/sni.service" \
                  /etc/systemd/system/sni.service
  install -m 0644 "${RETROSYNC_DIR}/install/systemd/retrosync.service" \
                  /etc/systemd/system/retrosync.service
  install -m 0644 "${RETROSYNC_DIR}/install/systemd/retrosync-pocket-sync@.service" \
                  /etc/systemd/system/retrosync-pocket-sync@.service
  systemctl daemon-reload
  systemctl enable sni.service retrosync.service
  systemctl restart sni.service
  # Templated pocket-sync units don't need to be enabled — they're
  # triggered via udev's SYSTEMD_WANTS.
}

install_udev_rules() {
  local src="${RETROSYNC_DIR}/install/udev/99-retrosync-pocket.rules"
  local dst="/etc/udev/rules.d/99-retrosync-pocket.rules"
  if grep -q "XXXX" "${src}"; then
    warn "udev rule still has XXXX:YYYY placeholder vendor/product IDs."
    warn "Plug in the Pocket (Tools → USB → Mount as USB Drive) and run:"
    warn "    lsusb | grep -i analogue"
    warn "Then edit ${dst} to replace XXXX:YYYY with the printed IDs."
  fi
  install -m 0644 "${src}" "${dst}"
  udevadm control --reload || true
  udevadm trigger || true
  log "installed udev rule -> ${dst}"
}

# -------- step 8: rclone OAuth ------------------------------------------
ensure_rclone_remote() {
  local remote_name="gdrive"
  local conf="${RETROSYNC_DATA}/rclone.conf"
  local legacy_conf="${RETROSYNC_HOME}/.config/rclone/rclone.conf"

  # Migrate from the old location (used by installs from before the
  # /var/lib move). We keep the rclone config under /var/lib/retrosync
  # because the daemon's ProtectHome=true masks /home from its namespace.
  if [[ -f "${legacy_conf}" && ! -f "${conf}" ]]; then
    log "migrating rclone config: ${legacy_conf} -> ${conf}"
    install -o "${RETROSYNC_USER}" -g "${RETROSYNC_USER}" -m 0600 \
      "${legacy_conf}" "${conf}"
    rm -f "${legacy_conf}"
  fi

  if sudo -u "${RETROSYNC_USER}" rclone --config "${conf}" \
       listremotes 2>/dev/null | grep -q "^${remote_name}:$"; then
    log "rclone remote '${remote_name}' already configured at ${conf}"
    return
  fi

  # `rclone config` is interactive. If we were piped in via curl|bash, our
  # stdin is the pipe (now empty) — every prompt would loop forever printing
  # "This value is required and it has no default." Detect that and bail
  # with instructions, rather than spam the operator.
  if [[ ! -t 0 ]]; then
    cat <<EOF >&2

================================================================
  rclone config needs an interactive terminal but stdin is a pipe.

  Re-run the installer from a real shell:

      cd /opt/retrosync || git clone https://github.com/indiefan/RetroSync.git /tmp/RetroSync
      sudo bash /opt/retrosync/install/setup.sh
      # or, if cloned to /tmp:
      sudo bash /tmp/RetroSync/install/setup.sh

  Or finish just the rclone step manually:

      sudo -u ${RETROSYNC_USER} rclone --config ${conf} config
      sudo systemctl restart retrosync
================================================================
EOF
    return 1
  fi

  cat <<EOF

================================================================
  Google Drive needs to be authorized.
  rclone will open a browser-based OAuth flow.

  When prompted:
    Storage:    drive
    client_id:  (leave blank — uses rclone's shared default)
    scope:      drive.file        (option 4 or 5; check the menu)
    Authorize:  on a machine with a browser, run the URL it shows.
                If you SSH'd from your Mac, copy the URL into the Mac's
                browser, complete sign-in, and paste the verification
                token back into the Pi.
================================================================
EOF
  sudo -u "${RETROSYNC_USER}" rclone --config "${conf}" config
  log "rclone configured. listing remotes:"
  sudo -u "${RETROSYNC_USER}" rclone --config "${conf}" listremotes
}

# -------- step 9: start daemon ------------------------------------------
start_daemon() {
  log "starting retrosync.service"
  systemctl restart retrosync.service
  sleep 1
  systemctl --no-pager --full status retrosync.service || true
}

# -------- main -----------------------------------------------------------
main() {
  require_root

  log "RetroSync installer starting on $(uname -srm)"
  install_apt_deps
  ensure_user_and_dirs
  install_sni
  install_rclone
  install_retrosync_app
  write_config
  install_systemd_units
  install_udev_rules
  if [[ "${SKIP_RCLONE_CONFIG:-0}" != "1" ]]; then
    ensure_rclone_remote
  else
    warn "SKIP_RCLONE_CONFIG=1 set — skipping OAuth setup"
  fi
  if [[ "${SKIP_DAEMON_START:-0}" != "1" ]]; then
    start_daemon
  else
    warn "SKIP_DAEMON_START=1 set — daemon not started"
  fi

  cat <<EOF

================================================================
  RetroSync is installed.

  Edit config:    sudo nano ${RETROSYNC_ETC}/config.yaml
  Test cart:      retrosync test-cart fxpak-pro-1
  Test cloud:     retrosync test-cloud
  Daemon logs:    journalctl -u retrosync.service -f
  Restart:        sudo systemctl restart retrosync
  Status:         retrosync status
================================================================
EOF
}

main "$@"
