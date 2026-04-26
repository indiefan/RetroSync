#!/usr/bin/env bash
# Minimal rclone stand-in for dry-run validation.
#
# Implements only the subcommands RetroSync uses:
#   rcat      <dest>           reads stdin, writes to FAKE_RCLONE_ROOT/<dest-rel>
#   cat       <src>            writes FAKE_RCLONE_ROOT/<src-rel> to stdout
#   lsf       <path>           lists basenames in path (one per line)
#   lsjson    <path>           emits an array of {Name,Size,IsDir,...}
#   delete    <path>           removes file
#
# The "remote" is recognized as <name>:<rest>; we drop <name>: and write
# under FAKE_RCLONE_ROOT.
set -euo pipefail

: "${FAKE_RCLONE_ROOT:=/tmp/retrosync-fake-cloud}"
mkdir -p "${FAKE_RCLONE_ROOT}"

cmd="${1:-}"
shift || true

# Drop trailing rclone flags we don't care about (--retries, --timeout, etc).
declare -a positional=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --*)
      # Skip flag and its value if it's the kind that takes one.
      case "$1" in
        --retries|--low-level-retries|--timeout|--config|--transfers)
          shift; shift || true ;;
        *) shift ;;
      esac ;;
    *) positional+=("$1"); shift ;;
  esac
done
set -- "${positional[@]}"

# Strip "remote:" prefix off path arguments.
strip_remote() {
  local p="$1"
  if [[ "${p}" == *:* ]]; then
    echo "${p#*:}"
  else
    echo "${p}"
  fi
}

case "${cmd}" in
  --version)
    echo "rclone v1.99.fake — RetroSync test harness"
    ;;
  rcat)
    rel="$(strip_remote "$1")"
    dest="${FAKE_RCLONE_ROOT}/${rel}"
    mkdir -p "$(dirname "${dest}")"
    cat > "${dest}"
    ;;
  cat)
    rel="$(strip_remote "$1")"
    src="${FAKE_RCLONE_ROOT}/${rel}"
    if [[ ! -f "${src}" ]]; then exit 1; fi
    cat "${src}"
    ;;
  lsf)
    rel="$(strip_remote "$1")"
    path="${FAKE_RCLONE_ROOT}/${rel}"
    if [[ -d "${path}" ]]; then
      ls -1 "${path}" | sed 's:$:/:' | sed 's:/\(.*\)/$:\1:'
    fi
    ;;
  lsjson)
    rel="$(strip_remote "$1")"
    path="${FAKE_RCLONE_ROOT}/${rel}"
    if [[ -f "${path}" ]]; then
      size=$(stat -c %s "${path}" 2>/dev/null || stat -f %z "${path}")
      printf '[{"Name":"%s","Size":%s,"IsDir":false}]\n' "$(basename "${rel}")" "${size}"
    elif [[ -d "${path}" ]]; then
      first=1
      printf '['
      shopt -s nullglob
      for entry in "${path}"/*; do
        [[ $first -eq 1 ]] || printf ','
        first=0
        name=$(basename "${entry}")
        if [[ -d "${entry}" ]]; then
          printf '{"Name":"%s","IsDir":true,"Size":0}' "${name}"
        else
          size=$(stat -c %s "${entry}" 2>/dev/null || stat -f %z "${entry}")
          printf '{"Name":"%s","IsDir":false,"Size":%s}' "${name}" "${size}"
        fi
      done
      printf ']\n'
    else
      printf '[]\n'
      exit 1
    fi
    ;;
  delete)
    rel="$(strip_remote "$1")"
    rm -f "${FAKE_RCLONE_ROOT}/${rel}" || true
    ;;
  *)
    echo "fake_rclone: unsupported subcommand '${cmd}'" >&2
    exit 2
    ;;
esac
