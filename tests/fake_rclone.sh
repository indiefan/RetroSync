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

# Skip leading global flags (RetroSync now invokes rclone with --config
# and the retry/timeout flags before the subcommand).
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config|--retries|--low-level-retries|--timeout|--transfers)
      shift; shift || true ;;
    --*)
      shift ;;
    *)
      break ;;
  esac
done

cmd="${1:-}"
shift || true

# Drop any remaining flags scattered after the subcommand.
declare -a positional=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config|--retries|--low-level-retries|--timeout|--transfers)
      shift; shift || true ;;
    --*)
      shift ;;
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
    # Helper: emit ISO-8601 mtime in the format real rclone uses
    # ("2026-04-29T12:34:56.000000000Z"). Tests that want to drive the
    # manifest-drift check `touch -d ... <file>` to set this.
    mtime_for() {
      stat -c '%y' "$1" 2>/dev/null \
        || stat -f '%Sm' -t '%Y-%m-%dT%H:%M:%SZ' "$1"
    }
    if [[ -f "${path}" ]]; then
      size=$(stat -c %s "${path}" 2>/dev/null || stat -f %z "${path}")
      mtime=$(mtime_for "${path}")
      printf '[{"Name":"%s","Size":%s,"IsDir":false,"ModTime":"%s"}]\n' \
             "$(basename "${rel}")" "${size}" "${mtime}"
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
          mtime=$(mtime_for "${entry}")
          printf '{"Name":"%s","IsDir":false,"Size":%s,"ModTime":"%s"}' \
                 "${name}" "${size}" "${mtime}"
        fi
      done
      printf ']\n'
    else
      # Real rclone returns 3 for "directory not found" and 4 for "file
      # not found". cloud.exists() relies on these specific codes to
      # distinguish "really missing" from "transient error".
      printf '[]\n'
      if [[ "${rel}" == */ || -d "$(dirname "${path}")" ]]; then
        exit 4  # file not found (parent exists)
      fi
      exit 3  # directory not found
    fi
    ;;
  delete)
    rel="$(strip_remote "$1")"
    rm -rf "${FAKE_RCLONE_ROOT}/${rel}" || true
    ;;
  rmdir)
    rel="$(strip_remote "$1")"
    rmdir "${FAKE_RCLONE_ROOT}/${rel}" 2>/dev/null || true
    ;;
  move)
    rel_src="$(strip_remote "$1")"
    rel_dst="$(strip_remote "$2")"
    src="${FAKE_RCLONE_ROOT}/${rel_src}"
    dst="${FAKE_RCLONE_ROOT}/${rel_dst}"
    mkdir -p "${dst}"
    if [[ -d "${src}" ]]; then
      shopt -s dotglob nullglob
      for entry in "${src}"/*; do
        bn=$(basename "${entry}")
        if [[ -d "${entry}" && -d "${dst}/${bn}" ]]; then
          # Recurse: copy contents into existing dest dir.
          mkdir -p "${dst}/${bn}"
          mv "${entry}"/* "${dst}/${bn}/" 2>/dev/null || true
          rmdir "${entry}" 2>/dev/null || true
        else
          mv "${entry}" "${dst}/" 2>/dev/null || true
        fi
      done
    fi
    ;;
  moveto)
    rel_src="$(strip_remote "$1")"
    rel_dst="$(strip_remote "$2")"
    src="${FAKE_RCLONE_ROOT}/${rel_src}"
    dst="${FAKE_RCLONE_ROOT}/${rel_dst}"
    mkdir -p "$(dirname "${dst}")"
    mv "${src}" "${dst}"
    ;;
  *)
    echo "fake_rclone: unsupported subcommand '${cmd}'" >&2
    exit 2
    ;;
esac
