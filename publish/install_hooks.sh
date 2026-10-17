#!/usr/bin/env bash
# @title Install the privacy pre-push hook into the malaviR working copy
# @purpose Copy publish/hooks/malaviR-pre-push to <malaviR>/.git/hooks/pre-push, so every
#   push of the public R package is scanned first. Git hooks are not tracked, so this
#   has to be run once per clone (RUNBOOK section 9d).
# @input publish/hooks/malaviR-pre-push
# @output <malaviR>/.git/hooks/pre-push
# @program git
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MALAVIR="${1:-${MALAVIR_DIR:-/mnt/biostore-all/Vellis/malaviR}}"
if [[ ! -d "${MALAVIR}/.git" ]]; then
  echo "no git repository at ${MALAVIR}; give the malaviR path as the first argument" >&2
  exit 1
fi
install -m 755 "${ROOT}/publish/hooks/malaviR-pre-push" "${MALAVIR}/.git/hooks/pre-push"
echo "installed ${MALAVIR}/.git/hooks/pre-push"
echo "test it with: cd ${MALAVIR} && .git/hooks/pre-push"
