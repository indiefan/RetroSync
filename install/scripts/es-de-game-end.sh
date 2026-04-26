#!/usr/bin/env bash
# RetroSync — EmulationStation-DE post-exit hook.
#
# Installed by setup-deck.sh to:
#   ~/.emulationstation/scripts/game-end/00-retrosync-post.sh
#
# Same arg shape as the game-start hook. We forward $1 to wrap-post,
# which flushes any in-flight uploads for this game and releases the
# lease so other devices can pick up.

set -u

SOURCE_ID="${RETROSYNC_DECK_SOURCE_ID:-deck-1}"
TIMEOUT="${RETROSYNC_WRAP_POST_TIMEOUT:-30}"
RETROSYNC_BIN="${RETROSYNC_BIN:-retrosync}"

ROM_PATH="${1:-}"
[[ -z "${ROM_PATH}" ]] && exit 0

SYSTEM_GAME="$("${RETROSYNC_BIN}" wrap-derive-game-id "${ROM_PATH}" \
                2>/dev/null | head -n1 || true)"
[[ -z "${SYSTEM_GAME}" ]] && exit 0

"${RETROSYNC_BIN}" wrap-post "${SOURCE_ID}" "${SYSTEM_GAME}" \
    --timeout "${TIMEOUT}" || true
exit 0
