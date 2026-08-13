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

untracked_manifest_paths() {
  comm -12 \
    <(map_manifest | sort -u) \
    <(git ls-files --others --exclude-standard | sort -u)
}

assert_no_untracked_manifest_paths() {
  local untracked_paths
  untracked_paths="$(untracked_manifest_paths)"
  if [[ -z "${untracked_paths}" ]]; then
    return 0
  fi

  echo "FATAL: PORT manifest contains untracked files that git diff cannot include:" >&2
  printf '%s\n' "${untracked_paths}" >&2
  echo "Track these files before generating a PORT patch." >&2
  return 1
}

assert_no_untracked_manifest_paths

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
