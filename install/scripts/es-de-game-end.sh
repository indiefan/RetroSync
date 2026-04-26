#!/usr/bin/env bash
# RetroSync — EmulationStation-DE post-exit hook.
#
# Installed by setup-deck.sh to:
#   <ES-DE root>/scripts/game-end/00-retrosync-post.sh
#
# Same arg shape as the game-start hook. We forward $1 to wrap-post,
# which flushes any in-flight uploads for this game and releases the
# lease so other devices can pick up.
#
# Logs to ~/.local/share/retrosync/wrap-post.log so failures can be
# inspected after the fact (ES-DE's log doesn't always capture stderr
# from custom event scripts cleanly).

set -u

SOURCE_ID="${RETROSYNC_DECK_SOURCE_ID:-deck-1}"
TIMEOUT="${RETROSYNC_WRAP_POST_TIMEOUT:-30}"
if [[ -n "${RETROSYNC_BIN:-}" ]]; then
  :
elif [[ -x "${HOME}/.local/bin/retrosync" ]]; then
  RETROSYNC_BIN="${HOME}/.local/bin/retrosync"
else
  RETROSYNC_BIN="retrosync"
fi

LOG="${HOME}/.local/share/retrosync/wrap-post.log"
mkdir -p "$(dirname "${LOG}")"

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

ROM_PATH="${1:-}"
if [[ -z "${ROM_PATH}" ]]; then
  echo "$(ts) [post] empty ROM_PATH; nothing to do" >> "${LOG}"
  exit 0
fi
echo "$(ts) [post] exit with ROM=${ROM_PATH}" >> "${LOG}"

SYSTEM_GAME="$("${RETROSYNC_BIN}" wrap-derive-game-id "${ROM_PATH}" \
                2>>"${LOG}" | head -n1 || true)"
if [[ -z "${SYSTEM_GAME}" ]]; then
  echo "$(ts) [post] could not derive system:game_id from ${ROM_PATH}" >> "${LOG}"
  exit 0
fi

"${RETROSYNC_BIN}" wrap-post "${SOURCE_ID}" "${SYSTEM_GAME}" \
    --timeout "${TIMEOUT}" >> "${LOG}" 2>&1 || true
echo "$(ts) [post] done" >> "${LOG}"
exit 0
