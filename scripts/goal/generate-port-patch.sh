#!/usr/bin/env bash
# Emit PORT-only patch evidence (stash Workstream A first).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRATCH="${GROK_GOAL_SCRATCH:-/var/folders/py/yshkz8454g98pq1wc0rnw5pm0000gp/T/grok-goal-e94c37b26768/implementer}"
MANIFEST="${ROOT}/scripts/goal/sync-port-only.txt"
WS_A="${ROOT}/scripts/goal/workstream-a-paths.txt"
cd "${ROOT}"
mkdir -p "${SCRATCH}"

map_manifest() {
  grep -v '^#' "${MANIFEST}" | grep -v '^$'
}

STASHED=0
if comm -23 <(git diff --name-only HEAD | sort) <(map_manifest | sort) | grep -q .; then
  git stash push -m "goal-ws-A-patch" --pathspec-from-file="${WS_A}"
  STASHED=1
fi

git diff --name-only HEAD | sort > "${SCRATCH}/changed-files-port-phase-b.txt"
git diff HEAD -- $(map_manifest) > "${SCRATCH}/port-only.patch"
git status -u --short | grep '^??' | awk '{print $2}' > "${SCRATCH}/untracked-port-files.txt" || true

if [[ "${STASHED}" == "1" ]]; then
  git stash pop
fi

echo "port-only.patch lines: $(wc -l < "${SCRATCH}/port-only.patch")"
echo "changed tracked PORT files: $(wc -l < "${SCRATCH}/changed-files-port-phase-b.txt")"