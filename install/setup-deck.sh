#!/usr/bin/env bash
# RetroSync — Steam Deck (EmuDeck) installer. Run from a Desktop Mode
# terminal as the `deck` user (NOT as root — this is a user-space
# install since SteamOS keeps /usr read-only).
#
#   bash install/setup-deck.sh
#
# What it does (each step is idempotent so re-running is safe):
#
#   1. Sanity-check: SteamOS / EmuDeck present, RetroArch's saves dir
#      detected.
#   2. Stage rclone in ~/.local/bin (download static binary; no
#      pacman / steamos-readonly disable needed).
#   3. Clone or update the RetroSync repo at
#      ~/.local/share/retrosync.
#   4. Build a Python venv inside the repo, pip install retrosync.
#   5. Install the wrap dispatcher to ~/.local/bin/retrosync-wrap.
#   6. Write a default user config to ~/.config/retrosync/config.yaml
#      with a `deck-1` source pre-filled from auto-detection.
#   7. Lay down user-systemd units (retrosyncd-deck, suspend hook,
#      reconnect helper). Enable + start the daemon.
#   8. enable-linger so the daemon survives Game Mode and reboots.
#   9. Walk through `rclone config` for Google Drive (interactive).
#  10. Patch Steam ROM Manager parser configurations to call our
#      wrapper.
#  11. Print a "next step" prompt to re-run SRM.

set -euo pipefail

# -------- knobs --------------------------------------------------------------
RETROSYNC_REPO="${RETROSYNC_REPO:-https://github.com/indiefan/RetroSync.git}"
RETROSYNC_REF="${RETROSYNC_REF:-main}"
RETROSYNC_DIR="${RETROSYNC_DIR:-${HOME}/.local/share/retrosync}"
RETROSYNC_LOCAL_SOURCE="${RETROSYNC_LOCAL_SOURCE:-}"

ETC_DIR="${HOME}/.config/retrosync"
USER_BIN="${HOME}/.local/bin"
USER_SYSTEMD="${HOME}/.config/systemd/user"

RCLONE_VERSION="${RCLONE_VERSION:-current}"   # "current" or v1.xx.x

# -------- helpers ------------------------------------------------------------
log()  { printf '\033[1;36m[setup-deck]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup-deck]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[setup-deck]\033[0m %s\n' "$*" >&2; exit 1; }

require_user() {
  [[ "$(id -u)" -ne 0 ]] || die "do not run as root; this is a user-space install"
}

detect_arch() {
  local m="$(uname -m)"
  case "${m}" in
    aarch64|arm64) echo "linux-arm64" ;;
    x86_64)        echo "linux-amd64" ;;
    *) die "unsupported architecture: ${m}" ;;
  esac
}

# -------- step 1: sanity checks ----------------------------------------------
sanity_check() {
  log "checking environment"
  # Probe the same set of locations as deck/emudeck_paths.py, plus an
  # `EMUDECK_ROOT` env override for non-standard installs (BTRFS subvol,
  # custom mount, etc.).
  local candidates=()
  if [[ -n "${EMUDECK_ROOT:-}" ]]; then
    candidates+=("${EMUDECK_ROOT}")
  fi
  candidates+=(
    "${HOME}/Emulation"
    "/run/media/mmcblk0p1/Emulation"
    "/run/media/deck/mmcblk0p1/Emulation"
  )
  # Plus anything that looks like an SD-card mount on this system.
  for sd in /run/media/*/Emulation /run/media/*/*/Emulation; do
    [[ -d "${sd}" ]] && candidates+=("${sd}")
  done
  local found=""
  for c in "${candidates[@]}"; do
    if [[ -d "${c}" ]]; then
      found="${c}"
      break
    fi
  done
  if [[ -z "${found}" ]]; then
    warn "EmuDeck not detected. Looked in:"
    for c in "${candidates[@]}"; do
      warn "  ${c}"
    done
    warn ""
    warn "If your Emulation/ directory is elsewhere, re-run with:"
    warn "  EMUDECK_ROOT=/path/to/Emulation bash install/setup-deck.sh"
    die "Install EmuDeck first (https://www.emudeck.com), or set EMUDECK_ROOT."
  fi
  log "  EmuDeck root detected at ${found}"
  export EMUDECK_ROOT="${found}"
  # Check for EmuDeck Cloud Save markers (paid feature). They can't
  # coexist with RetroSync — both want to write the same save files.
  local cloud_marker="${HOME}/Emulation/storage/emudeck/cloud_sync_enabled"
  if [[ -e "${cloud_marker}" && "${FORCE:-0}" != "1" ]]; then
    die "EmuDeck Cloud Save appears to be enabled. Disable it first or
    re-run with FORCE=1 to override (NOT recommended; both will fight)."
  fi
}

# -------- step 2: rclone -----------------------------------------------------
install_rclone() {
  if command -v rclone >/dev/null 2>&1; then
    log "rclone already on PATH: $(rclone --version | head -n1)"
    return
  fi
  if [[ -x "${USER_BIN}/rclone" ]]; then
    log "rclone already at ${USER_BIN}/rclone"
    return
  fi
  log "downloading rclone (${RCLONE_VERSION}, $(detect_arch))..."
  mkdir -p "${USER_BIN}"
  local tmp="$(mktemp -d)"
  trap "rm -rf ${tmp}" RETURN
  local arch="$(detect_arch)"
  local url="https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-${arch}.zip"
  curl -fsSL "${url}" -o "${tmp}/rclone.zip"
  unzip -q "${tmp}/rclone.zip" -d "${tmp}"
  install -m 0755 "${tmp}"/rclone-*/rclone "${USER_BIN}/rclone"
  log "installed rclone -> ${USER_BIN}/rclone"
}

# -------- step 3: app code + venv --------------------------------------------
install_retrosync_app() {
  if [[ -n "${RETROSYNC_LOCAL_SOURCE}" ]]; then
    log "copying local source from ${RETROSYNC_LOCAL_SOURCE} -> ${RETROSYNC_DIR}"
    rm -rf "${RETROSYNC_DIR}"
    mkdir -p "$(dirname "${RETROSYNC_DIR}")"
    cp -a "${RETROSYNC_LOCAL_SOURCE}" "${RETROSYNC_DIR}"
  elif [[ -d "${RETROSYNC_DIR}/.git" ]]; then
    log "updating existing checkout at ${RETROSYNC_DIR}"
    git -C "${RETROSYNC_DIR}" fetch --quiet origin
    git -C "${RETROSYNC_DIR}" checkout --quiet "${RETROSYNC_REF}"
    git -C "${RETROSYNC_DIR}" pull --quiet --ff-only origin "${RETROSYNC_REF}"
  else
    log "cloning ${RETROSYNC_REPO} (${RETROSYNC_REF}) -> ${RETROSYNC_DIR}"
    mkdir -p "$(dirname "${RETROSYNC_DIR}")"
    git clone --quiet --branch "${RETROSYNC_REF}" "${RETROSYNC_REPO}" "${RETROSYNC_DIR}"
  fi

  log "building venv at ${RETROSYNC_DIR}/.venv"
  python3 -m venv "${RETROSYNC_DIR}/.venv"
  "${RETROSYNC_DIR}/.venv/bin/pip" install --quiet --upgrade pip
  "${RETROSYNC_DIR}/.venv/bin/pip" install --quiet "${RETROSYNC_DIR}"

  # Console_script entry points have been slow on some Python builds.
  # Replace with tiny bash trampolines that use `python -m` directly.
  mkdir -p "${USER_BIN}"
  for entry in retrosync retrosyncd; do
    target="${RETROSYNC_DIR}/.venv/bin/${entry}"
    case "${entry}" in
      retrosync)  module="retrosync.cli" ;;
      retrosyncd) module="retrosync.daemon" ;;
    esac
    cat > "${target}" <<EOF
#!/usr/bin/env bash
exec "${RETROSYNC_DIR}/.venv/bin/python" -m ${module} "\$@"
EOF
    chmod 0755 "${target}"
  done
  ln -sf "${RETROSYNC_DIR}/.venv/bin/retrosync"  "${USER_BIN}/retrosync"
  ln -sf "${RETROSYNC_DIR}/.venv/bin/retrosyncd" "${USER_BIN}/retrosyncd"
}

# -------- step 4: wrap dispatcher --------------------------------------------
install_wrap_script() {
  log "installing wrap dispatcher -> ${USER_BIN}/retrosync-wrap"
  install -m 0755 "${RETROSYNC_DIR}/install/bin/retrosync-wrap" \
                  "${USER_BIN}/retrosync-wrap"
}

# -------- step 5: config -----------------------------------------------------
write_config() {
  mkdir -p "${ETC_DIR}"
  local cfg="${ETC_DIR}/config.yaml"
  if [[ -f "${cfg}" ]]; then
    log "config exists at ${cfg}; leaving it alone"
    return
  fi
  log "writing default config to ${cfg}"
  # Detect saves_root + roms_root via our helper so the operator
  # doesn't have to guess. EMUDECK_ROOT (set by sanity_check) is
  # passed through so a non-standard install resolves the same way.
  local detect_args=(deck detect-paths --system snes)
  if [[ -n "${EMUDECK_ROOT:-}" ]]; then
    detect_args+=(--emudeck-root "${EMUDECK_ROOT}")
  fi
  local detect="$(${USER_BIN}/retrosync --config /dev/null \
                  "${detect_args[@]}" 2>/dev/null || true)"
  local root="${EMUDECK_ROOT:-${HOME}/Emulation}"
  local saves_root="${root}/saves/retroarch/saves"
  local roms_root="${root}/roms/snes"
  if echo "${detect}" | grep -q '^saves_root'; then
    saves_root="$(echo "${detect}" | awk -F': *' '/^saves_root/ {print $2; exit}')"
    roms_root="$(echo "${detect}" | awk -F': *' '/^roms_root/  {print $2; exit}')"
  fi
  # Split-storage case: EmuDeck lets you put ROMs on the SD while
  # everything else stays on internal storage. If `roms_root` doesn't
  # exist where we picked it, look on the SD card paths and use the
  # first one that does. Operators with a non-standard layout can
  # override via ROMS_ROOT env var.
  if [[ -n "${ROMS_ROOT:-}" ]]; then
    roms_root="${ROMS_ROOT}"
    log "  using ROMS_ROOT override: ${roms_root}"
  elif [[ ! -d "${roms_root}" ]]; then
    for sd_root in /run/media/mmcblk0p1/Emulation \
                   /run/media/deck/mmcblk0p1/Emulation \
                   /run/media/*/Emulation \
                   /run/media/*/*/Emulation; do
      if [[ -d "${sd_root}/roms/snes" ]]; then
        roms_root="${sd_root}/roms/snes"
        log "  ROMs found on SD card: ${roms_root}"
        break
      fi
    done
    if [[ ! -d "${roms_root}" ]]; then
      warn "no roms/snes/ directory found at ${roms_root}."
      warn "If your ROMs are elsewhere, set ROMS_ROOT before re-running:"
      warn "  ROMS_ROOT=/path/to/roms/snes bash install/setup-deck.sh"
      warn "Continuing — you can edit ${cfg} later."
    fi
  fi
  cat > "${cfg}" <<EOF
# RetroSync — Steam Deck (EmuDeck) configuration. Generated by
# setup-deck.sh; safe to edit by hand. Restart the daemon after
# changes:  systemctl --user restart retrosyncd-deck

cloud:
  rclone_remote: "gdrive:retro-saves"
  rclone_binary: "${USER_BIN}/rclone"
  rclone_config_path: "${ETC_DIR}/rclone.conf"

state:
  db_path: ${HOME}/.local/share/retrosync/state.db

# Bidirectional sync: cloud-newer saves get pulled to the Deck.
# Verify save-format compatibility (cf. pocket-sync-design §10) before
# flipping on if you also sync from a cart.
cloud_to_device: true

# Auto-resolve divergences in favor of the device's bytes; loser stays
# in versions/ for recovery.
conflict_winner: device

# Active-device lease — soft mode is the default. Switch to hard once
# every other device in the fleet is also lease-aware (i.e. running
# v0.3+).
lease:
  mode: soft
  ttl_minutes: 15
  heartbeat_minutes: 5

sources:
  - id: deck-1
    adapter: emudeck
    options:
      saves_root: ${saves_root}
      roms_root:  ${roms_root}
      save_extension: .srm
      rom_extensions: [".sfc", ".smc", ".swc", ".fig"]
      system: snes
EOF
  chmod 0644 "${cfg}"
}

# -------- step 6: systemd units + linger -------------------------------------
install_systemd_units() {
  log "installing user-systemd units -> ${USER_SYSTEMD}"
  mkdir -p "${USER_SYSTEMD}"
  install -m 0644 "${RETROSYNC_DIR}/install/systemd-user/retrosyncd-deck.service" \
                  "${USER_SYSTEMD}/retrosyncd-deck.service"
  install -m 0644 "${RETROSYNC_DIR}/install/systemd-user/retrosyncd-suspend.service" \
                  "${USER_SYSTEMD}/retrosyncd-suspend.service"
  install -m 0644 "${RETROSYNC_DIR}/install/systemd-user/retrosync-reconnect.service" \
                  "${USER_SYSTEMD}/retrosync-reconnect.service"
  systemctl --user daemon-reload
  systemctl --user enable retrosyncd-deck.service retrosyncd-suspend.service
  systemctl --user restart retrosyncd-deck.service

  # enable-linger requires root. Use sudo if available; otherwise warn.
  if command -v sudo >/dev/null 2>&1; then
    log "enabling linger for ${USER} so services run in Game Mode"
    sudo loginctl enable-linger "${USER}" || warn "loginctl enable-linger failed"
  else
    warn "loginctl not invoked (no sudo). Run manually as root:"
    warn "    sudo loginctl enable-linger ${USER}"
  fi
}

# -------- step 7: rclone OAuth -----------------------------------------------
ensure_rclone_remote() {
  local conf="${ETC_DIR}/rclone.conf"
  mkdir -p "${ETC_DIR}"
  if "${USER_BIN}/rclone" --config "${conf}" listremotes 2>/dev/null \
       | grep -q "^gdrive:$"; then
    log "rclone remote 'gdrive' already configured"
    return
  fi
  cat <<EOF
================================================================
  Google Drive needs to be authorized.
  rclone will open a browser-based OAuth flow.

  When prompted:
    Storage:    drive
    client_id:  (leave blank — uses rclone's shared default)
    scope:      drive.file        (option 4 or 5; check the menu)
================================================================
EOF
  if [[ ! -t 0 ]]; then
    cat <<EOF
================================================================
  rclone config needs an interactive terminal but stdin is a pipe.
  Re-run from a real shell:

      bash ${RETROSYNC_DIR}/install/setup-deck.sh

  Or finish just the rclone step:

      ${USER_BIN}/rclone --config ${conf} config
      systemctl --user restart retrosyncd-deck
================================================================
EOF
    return 1
  fi
  "${USER_BIN}/rclone" --config "${conf}" config
}

# -------- step 8: SRM patch --------------------------------------------------
patch_srm() {
  log "patching Steam ROM Manager parser configs"
  if "${USER_BIN}/retrosync" --config "${ETC_DIR}/config.yaml" \
        deck patch-srm; then
    log "SRM parsers patched."
  else
    warn "SRM patch failed (config not found?). Open Steam ROM Manager"
    warn "via EmuDeck → Tools at least once so the config is created,"
    warn "then re-run: retrosync deck patch-srm"
  fi
}

# -------- main ---------------------------------------------------------------
main() {
  require_user
  log "RetroSync EmuDeck installer starting on $(uname -srm)"
  sanity_check
  install_rclone
  install_retrosync_app
  install_wrap_script
  write_config
  install_systemd_units
  if [[ "${SKIP_RCLONE_CONFIG:-0}" != "1" ]]; then
    ensure_rclone_remote
  else
    warn "SKIP_RCLONE_CONFIG=1 — skipping OAuth setup"
  fi
  patch_srm
  cat <<EOF

================================================================
  RetroSync (EmuDeck) is installed.

  NEXT STEP: open Steam ROM Manager (EmuDeck → Tools → Steam ROM
  Manager), click "Add Games" → "Parse" → "Save to Steam". This
  re-generates your shortcuts with the RetroSync wrapper baked in.
  After that, every game you launch from Steam syncs automatically.

  Daemon logs:    journalctl --user -u retrosyncd-deck -f
  Restart:        systemctl --user restart retrosyncd-deck
  Test cloud:     retrosync test-cloud
  Detect paths:   retrosync deck detect-paths
  Status:         retrosync status
================================================================
EOF
}

main "$@"
