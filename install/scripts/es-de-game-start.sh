#!/usr/bin/env bash
# RetroSync — EmulationStation-DE pre-launch hook.
#
# Installed by setup-deck.sh to:
#   <ES-DE root>/scripts/game-start/00-retrosync-pre.sh
#
# ES-DE invokes every executable in this directory before launching a
# game. Arguments (positional, per the ES-DE custom-event-scripts spec):
#
#   $1  full path to the ROM file
#   $2  ROM filename (no path)
#   $3  full game name (from gamelist.xml; falls back to filename)
#   $4  system short name (e.g. "snes")
#
# We forward $1 to retrosync's wrap-pre subcommand. wrap-pre runs a
# pre-launch sync (cloud → device if cloud is ahead) and grabs the
# active-device lease so other devices know we're playing.
#
# Failures are non-fatal — we never block ES-DE from launching the
# game. Worst case: emulator launches with stale local bytes; the
# next save flushes via the inotify daemon as usual. Failures are
# logged to ~/.local/share/retrosync/wrap-pre.log so they can be
# inspected after the fact (ES-DE's own log doesn't always capture
# stderr from custom event scripts cleanly).

set -u

SOURCE_ID="${RETROSYNC_DECK_SOURCE_ID:-deck-1}"
TIMEOUT="${RETROSYNC_WRAP_PRE_TIMEOUT:-30}"
# Resolve retrosync via an absolute path first — ES-DE custom event
# scripts inherit whatever environment ES-DE was launched with, which
# often doesn't include ~/.local/bin on Game Mode / Steam-launched
# sessions.
if [[ -n "${RETROSYNC_BIN:-}" ]]; then
  :
elif [[ -x "${HOME}/.local/bin/retrosync" ]]; then
  RETROSYNC_BIN="${HOME}/.local/bin/retrosync"
else
  RETROSYNC_BIN="retrosync"
fi

LOG="${HOME}/.local/share/retrosync/wrap-pre.log"
mkdir -p "$(dirname "${LOG}")"

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

ROM_PATH="${1:-}"
if [[ -z "${ROM_PATH}" ]]; then
  echo "$(ts) [pre] empty ROM_PATH; nothing to do" >> "${LOG}"
  exit 0
fi
echo "$(ts) [pre] launching with ROM=${ROM_PATH}" >> "${LOG}"

# ES-DE on some setups passes the ROM path with shell-style backslash
# escapes baked into the argument value (e.g. "Final\ Fantasy" instead
# of "Final Fantasy"). If the literal path doesn't exist on disk, try
# a stripped version. SNES / console ROM filenames don't legitimately
# contain backslashes, so it's safe to drop them all.
if [[ ! -f "${ROM_PATH}" ]]; then
  STRIPPED="${ROM_PATH//\\/}"
  if [[ -f "${STRIPPED}" ]]; then
    echo "$(ts) [pre] unescaped ROM_PATH → ${STRIPPED}" >> "${LOG}"
    ROM_PATH="${STRIPPED}"
  fi
fi

SYSTEM_GAME="$("${RETROSYNC_BIN}" wrap-derive-game-id "${ROM_PATH}" \
                2>>"${LOG}" | head -n1 || true)"
if [[ -z "${SYSTEM_GAME}" ]]; then
  echo "$(ts) [pre] could not derive system:game_id from ${ROM_PATH}" >> "${LOG}"
  exit 0
fi
echo "$(ts) [pre] system_game=${SYSTEM_GAME} source=${SOURCE_ID} timeout=${TIMEOUT}" >> "${LOG}"

"${RETROSYNC_BIN}" wrap-pre "${SOURCE_ID}" "${SYSTEM_GAME}" \
    --timeout "${TIMEOUT}" >> "${LOG}" 2>&1 || true
echo "$(ts) [pre] done" >> "${LOG}"
exit 0
