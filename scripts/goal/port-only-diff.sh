#!/usr/bin/env bash
# Show git diff/stat for Workstream B (PORT manifest) only — excludes approval/resume files.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="${ROOT}/scripts/goal/sync-port-only.txt"
cd "${ROOT}"

map_paths() {
  grep -v '^#' "${MANIFEST}" | grep -v '^$'
}

echo "== PORT-only changed files (manifest ∩ git diff) =="
comm -12 <(map_paths | sort) <(git diff --name-only HEAD | sort)

echo ""
echo "== PORT-only diff stat =="
# shellcheck disable=SC2046
git diff --stat HEAD -- $(map_paths)