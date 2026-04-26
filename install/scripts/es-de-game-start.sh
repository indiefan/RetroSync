#!/usr/bin/env bash
# RetroSync — EmulationStation-DE pre-launch hook.
#
# Installed by setup-deck.sh to:
#   ~/.emulationstation/scripts/game-start/00-retrosync-pre.sh
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
# next save flushes via the inotify daemon as usual.

set -u

SOURCE_ID="${RETROSYNC_DECK_SOURCE_ID:-deck-1}"
TIMEOUT="${RETROSYNC_WRAP_PRE_TIMEOUT:-10}"
RETROSYNC_BIN="${RETROSYNC_BIN:-retrosync}"

ROM_PATH="${1:-}"
[[ -z "${ROM_PATH}" ]] && exit 0

SYSTEM_GAME="$("${RETROSYNC_BIN}" wrap-derive-game-id "${ROM_PATH}" \
                2>/dev/null | head -n1 || true)"
[[ -z "${SYSTEM_GAME}" ]] && exit 0

"${RETROSYNC_BIN}" wrap-pre "${SOURCE_ID}" "${SYSTEM_GAME}" \
    --timeout "${TIMEOUT}" || true
exit 0
