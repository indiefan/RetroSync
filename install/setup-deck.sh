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
  # rclone publishes "current" at the root (no version prefix) and
  # versioned releases under /v1.x.y/. Pick the right URL shape.
  local url
  if [[ "${RCLONE_VERSION}" == "current" ]]; then
    url="https://downloads.rclone.org/rclone-current-${arch}.zip"
  else
    url="https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-${arch}.zip"
  fi
  curl -fSL "${url}" -o "${tmp}/rclone.zip" \
    || die "rclone download failed: ${url}"
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
  # The trampolines also export RETROSYNC_CONFIG so interactive shell
  # invocations don't trip over the Pi-side default path
  # (/etc/retrosync/config.yaml). The systemd unit sets this too —
  # this just covers the bare-shell case.
  mkdir -p "${USER_BIN}"
  for entry in retrosync retrosyncd; do
    target="${RETROSYNC_DIR}/.venv/bin/${entry}"
    case "${entry}" in
      retrosync)  module="retrosync.cli" ;;
      retrosyncd) module="retrosync.daemon" ;;
    esac
    cat > "${target}" <<EOF
#!/usr/bin/env bash
# Default the config path to the Deck's user-space install when the
# operator hasn't overridden via env or --config. Generated by
# setup-deck.sh; do not edit by hand.
: "\${RETROSYNC_CONFIG:=${ETC_DIR}/config.yaml}"
export RETROSYNC_CONFIG
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
#
# Two-phase: write the cloud/state/lease scaffold once (skipped if config
# already exists), then call `retrosync deck add-source --system <sys>`
# for every system the Deck has ROMs for. The CLI handles per-system
# detection (saves_root from RetroArch's savefile_directory, roms_root
# from internal -> SD-card fallback) and is idempotent — re-running the
# installer to pick up a newly-installed system is safe.

# Per-system list mirrors retrosync/deck/systems.py. Add an entry here
# when extending the catalog.
SUPPORTED_SYSTEMS=(snes n64 gba genesis)

write_config_scaffold() {
  mkdir -p "${ETC_DIR}"
  local cfg="${ETC_DIR}/config.yaml"
  if [[ -f "${cfg}" ]]; then
    log "config exists at ${cfg}; leaving the scaffold alone"
    return
  fi
  log "writing config scaffold to ${cfg}"
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

# Safer default for a fresh Deck joining an existing cloud-synced fleet:
# when this device shows up with bytes that differ from cloud's current
# AND don't match any known historical version (case 4 — no prior
# agreement), preserve the device's bytes as a versions/* entry but
# let cloud's current win. With cloud_to_device=true above, cloud's
# bytes are also written back to the Deck so the next launch resumes
# from the cloud-side save state. Without this, the first sync of a
# game on a new Deck will overwrite cloud with whatever the Deck had
# at boot (which is "nothing" for a never-played game → empty save).
cloud_wins_on_unknown_device: true

# Active-device lease — soft mode is the default. Switch to hard once
# every other device in the fleet is also lease-aware (i.e. running
# v0.3+).
lease:
  mode: soft
  ttl_minutes: 15
  heartbeat_minutes: 5

sources: []
EOF
  chmod 0644 "${cfg}"
}

# Look up a per-system roms dir using the same chain the Python helper
# uses (internal -> SD card -> caller-provided env). Echoes the path
# if found, nothing otherwise. Used purely as a "has this system been
# installed" probe; the Python `add-source` does the real work.
roms_dir_for_system() {
  local system="$1"
  local root="${EMUDECK_ROOT:-${HOME}/Emulation}"
  local cand="${root}/roms/${system}"
  if [[ -d "${cand}" ]]; then echo "${cand}"; return; fi
  for sd_root in /run/media/mmcblk0p1/Emulation \
                 /run/media/deck/mmcblk0p1/Emulation \
                 /run/media/*/Emulation \
                 /run/media/*/*/Emulation; do
    if [[ -d "${sd_root}/roms/${system}" ]]; then
      echo "${sd_root}/roms/${system}"; return
    fi
  done
}

# Hand a system off to `retrosync deck add-source`, which is idempotent
# and handles its own path detection. No-op (with a log line) if the
# system already has a source configured. Honors ROMS_ROOT_<SYS>
# env-var overrides per system, e.g. ROMS_ROOT_SNES=/foo.
add_source_for_system() {
  local system="$1"
  local cfg="${ETC_DIR}/config.yaml"
  local upper="$(echo "${system}" | tr '[:lower:]' '[:upper:]')"
  local override_var="ROMS_ROOT_${upper}"
  local args=(--config "${cfg}" deck add-source --system "${system}"
              --config-path "${cfg}")
  if [[ -n "${EMUDECK_ROOT:-}" ]]; then
    args+=(--emudeck-root "${EMUDECK_ROOT}")
  fi
  if [[ -n "${!override_var:-}" ]]; then
    args+=(--roms-root "${!override_var}")
  elif [[ "${system}" == "snes" && -n "${ROMS_ROOT:-}" ]]; then
    # Backwards compat with the v1 single-system installer's env var.
    args+=(--roms-root "${ROMS_ROOT}")
  fi
  if "${USER_BIN}/retrosync" "${args[@]}" 2>&1 | tee /tmp/.retrosync-add-source.$$; then
    rm -f /tmp/.retrosync-add-source.$$
  else
    rm -f /tmp/.retrosync-add-source.$$
    return 1
  fi
}

write_config() {
  write_config_scaffold
  local added=0
  local skipped=0
  for system in "${SUPPORTED_SYSTEMS[@]}"; do
    local roms_dir
    roms_dir="$(roms_dir_for_system "${system}")"
    if [[ -z "${roms_dir}" ]]; then
      log "no roms/${system}/ found; skipping ${system} source"
      skipped=$((skipped + 1))
      continue
    fi
    log "configuring ${system} source (ROMs: ${roms_dir})"
    if add_source_for_system "${system}"; then
      added=$((added + 1))
    else
      warn "  add-source for ${system} failed; continue"
    fi
  done
  if (( added == 0 )); then
    warn "no sources configured. Drop ROMs under <emudeck>/roms/<system>/"
    warn "and re-run setup-deck.sh, or run:"
    warn "  retrosync deck add-source --system <sys>"
  fi
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
  # Skip cleanly when SRM hasn't been used (low-integration setups
  # using EmulationStation / ES-DE etc.). The patcher itself errors
  # if userConfigurations.json is missing; check first to keep the
  # log quiet for those operators.
  local srm_cfg="${HOME}/.config/steam-rom-manager/userData/userConfigurations.json"
  if [[ ! -f "${srm_cfg}" ]]; then
    log "Steam ROM Manager not configured (${srm_cfg} missing); skipping."
    log "  This is fine if you launch games via EmulationStation / ES-DE"
    log "  — the ES-DE hooks (next step) cover the same pre-launch path."
    return 0
  fi
  log "patching Steam ROM Manager parser configs"
  if "${USER_BIN}/retrosync" --config "${ETC_DIR}/config.yaml" \
        deck patch-srm; then
    log "SRM parsers patched."
  else
    warn "SRM patch failed unexpectedly; re-run with: retrosync deck patch-srm"
  fi
}

# -------- step 8b: ES-DE pre/post hooks --------------------------------------
install_es_de_hooks() {
  # ES-DE custom event scripts let us run arbitrary commands before
  # game launch and after game exit — same role as the SRM wrapper
  # but for low-integration setups. Idempotent: scripts are owned by
  # us (`00-retrosync-*.sh`), safe to overwrite.
  #
  # Detect across the known ES-DE layouts:
  #   - ES-DE 3.0+ native:     ~/ES-DE/
  #   - Legacy ES / older ES-DE: ~/.emulationstation/
  #   - Flatpak (modern):      ~/.var/app/org.es_de.frontend/config/ES-DE/
  #   - Flatpak (older slug):  ~/.var/app/com.gitlab.es-de.EmulationStation-DE/.emulationstation/
  #   - ES_DE_HOME env override for everything else.
  local candidates=()
  [[ -n "${ES_DE_HOME:-}" ]] && candidates+=("${ES_DE_HOME}")
  candidates+=(
    "${HOME}/ES-DE"
    "${HOME}/.emulationstation"
    "${HOME}/.var/app/org.es_de.frontend/config/ES-DE"
    "${HOME}/.var/app/com.gitlab.es-de.EmulationStation-DE/.emulationstation"
  )
  local es_root=""
  for c in "${candidates[@]}"; do
    if [[ -d "${c}" ]]; then
      es_root="${c}"
      break
    fi
  done
  if [[ -z "${es_root}" ]]; then
    log "EmulationStation/ES-DE not detected. Looked in:"
    for c in "${candidates[@]}"; do
      log "  ${c}"
    done
    log "  Skipping ES-DE hooks. If your install is elsewhere, re-run with"
    log "  ES_DE_HOME=/path/to/es-de bash install/setup-deck.sh"
    return 0
  fi
  log "installing ES-DE pre/post hooks under ${es_root}"
  local start_dir="${es_root}/scripts/game-start"
  local end_dir="${es_root}/scripts/game-end"
  mkdir -p "${start_dir}" "${end_dir}"
  install -m 0755 "${RETROSYNC_DIR}/install/scripts/es-de-game-start.sh" \
                  "${start_dir}/00-retrosync-pre.sh"
  install -m 0755 "${RETROSYNC_DIR}/install/scripts/es-de-game-end.sh" \
                  "${end_dir}/00-retrosync-post.sh"
  log "  -> ${start_dir}/00-retrosync-pre.sh"
  log "  -> ${end_dir}/00-retrosync-post.sh"
  # Detect whether custom-event-scripts is enabled in es_settings.xml.
  # 3.0+ stores settings under <root>/settings/es_settings.xml; older
  # installs put it at <root>/es_settings.xml. Check both.
  local settings=""
  for s in "${es_root}/settings/es_settings.xml" \
           "${es_root}/es_settings.xml"; do
    if [[ -f "${s}" ]]; then
      settings="${s}"
      break
    fi
  done
  if [[ -n "${settings}" ]] \
        && grep -q 'CustomEventScripts.*"true"' "${settings}"; then
    log "  (custom event scripts already enabled in ${settings})"
  else
    cat <<EOF

  ----------------------------------------------------------------
  NEXT STEP for ES-DE: enable custom event scripts so the hooks
  actually fire. In ES-DE:

      Main Menu  →  Other Settings  →  Enable custom event scripts
      → ON

  After that, every game you launch via ES-DE will sync via
  RetroSync automatically.
  ----------------------------------------------------------------

EOF
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
  install_es_de_hooks
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
