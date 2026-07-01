#!/usr/bin/env bash
# Two-phase verification: Phase B (PORT-only tree) then Phase A (approval/resume restored).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRATCH="${GROK_GOAL_SCRATCH:-/var/folders/py/yshkz8454g98pq1wc0rnw5pm0000gp/T/grok-goal-e94c37b26768/implementer}"
MANIFEST="${ROOT}/scripts/goal/sync-port-only.txt"
WS_A_PATHS="${ROOT}/scripts/goal/workstream-a-paths.txt"
LOG="${SCRATCH}/verify-sync-port.log"
STASH_NOTE="${SCRATCH}/workstream-a.stash"
cd "${ROOT}"
mkdir -p "${SCRATCH}"

exec > >(tee "${LOG}") 2>&1

map_manifest() {
  grep -v '^#' "${MANIFEST}" | grep -v '^$'
}

manifest_py_files() {
  map_manifest | grep '\.py$' || true
}

count_ws_a_overlap() {
  comm -23 <(git diff --name-only HEAD | sort) <(map_manifest | sort) | wc -l | tr -d ' '
}

stash_ws_a_if_needed() {
  local overlap
  overlap="$(count_ws_a_overlap)"
  if [[ "${overlap}" == "0" ]]; then
    echo "worktree already PORT-only (${overlap} workstream-A paths in diff)"
    return 0
  fi
  echo "stashing ${overlap} workstream-A paths before Phase B"
  git stash push -m "goal-ws-A" --pathspec-from-file="${WS_A_PATHS}"
  {
    echo "stash_name=goal-ws-A"
    echo "stashed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "paths_file=${WS_A_PATHS}"
    git stash list | head -1
  } > "${STASH_NOTE}"
  overlap="$(count_ws_a_overlap)"
  if [[ "${overlap}" != "0" ]]; then
    echo "FATAL: worktree still mixed after stash (${overlap} non-manifest paths)" >&2
    exit 1
  fi
}

run_phase_b() {
  echo ""
  echo "======== Phase B: PORT gate (workstream A stashed) ========"
  echo "phase-b: started $(date -u +%Y-%m-%dT%H:%M:%SZ)"

  echo "== docker preflight =="
  if command -v docker >/dev/null 2>&1; then
    { docker ps 2>&1; docker compose ps 2>&1; } | tee "${SCRATCH}/docker-preflight.log" || true
  else
    echo "no docker context" | tee "${SCRATCH}/docker-preflight.log"
  fi

  git diff --name-only HEAD | sort | tee "${SCRATCH}/changed-files.txt"
  cp "${SCRATCH}/changed-files.txt" "${SCRATCH}/changed-files-port-only.txt"
  cp "${SCRATCH}/changed-files.txt" "${SCRATCH}/changed-files-all.txt"

  if [[ "$(count_ws_a_overlap)" != "0" ]]; then
    echo "FATAL: Phase B requires PORT-only git diff; stash workstream A first." >&2
    exit 1
  fi

  bash "${ROOT}/scripts/goal/port-only-diff.sh" | tee "${SCRATCH}/port-only-diff-stat.log"

  echo "== plan step 2a: ruff full dirs (honest capture) =="
  set +e
  ruff check src/ packages/ai-gateway-core/src/ apps/assistant-service/src/ \
    apps/knowledge-service/src/ scripts/new/ --extend-select E,F,W \
    2>&1 | tee "${SCRATCH}/ruff-full-dirs.log"
  RUFF_FULL_EXIT=$?
  set -e
  echo "ruff full dirs exit=${RUFF_FULL_EXIT} (pre-existing; not a Phase B gate)"

  echo "== plan step 2b: ruff manifest python (GATE) =="
  PY_FILES=()
  while IFS= read -r line; do
    [[ -n "${line}" ]] && PY_FILES+=("${line}")
  done < <(manifest_py_files)
  ruff check --extend-select E,F,W "${PY_FILES[@]}" 2>&1 | tee "${SCRATCH}/ruff.log"
  cp "${SCRATCH}/ruff.log" "${SCRATCH}/ruff-manifest-extend.log"

  bash "${ROOT}/scripts/goal/generate-port-patch.sh" | tee "${SCRATCH}/port-patch-gen.log"

  echo "== bash -n migrate/deploy =="
  bash -n scripts/new/migrate.sh
  bash -n scripts/new/deploy.sh

  echo "== gateway_preflight =="
  python scripts/new/gateway_preflight.py | tee "${SCRATCH}/guard-exec.log"

  echo "== plan step 3: security pytest =="
  pytest tests/security/ \
    tests/api/test_gateway_tenant_isolation.py \
    tests/api/test_langgraph_passthrough_security.py \
    tests/api/test_path_traversal.py \
    tests/api/test_image_adversarial.py \
    tests/api/test_presign_security.py \
    tests/api/test_users_security.py \
    tests/api/test_conversation_share_quiz.py \
    tests/api/test_quiz_grading.py \
    tests/knowledge/test_kb_security_regressions.py \
    --no-cov -q --tb=line 2>&1 | tee "${SCRATCH}/security-tests.log"

  echo "== script/release guard pytest =="
  pytest tests/scripts/test_release_guards.py \
    tests/scripts/test_gateway_preflight.py \
    tests/scripts/test_validate_env_quickstart.py \
    tests/scripts/test_script_secret_defaults.py \
    --no-cov -q --tb=line 2>&1 | tee "${SCRATCH}/script-tests.log"

  git diff -U0 --no-color -- $(map_manifest) | head -200 \
    > "${SCRATCH}/diff-head-port-only.txt" || true

  echo "phase-b: passed $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

run_phase_a() {
  echo ""
  echo "======== Phase A: approval/resume (workstream A restored) ========"
  echo "phase-a: started $(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if git stash list | grep -q "goal-ws-A"; then
    git stash pop
    echo "restored workstream A from stash goal-ws-A"
  else
    echo "no goal-ws-A stash; assuming workstream A already in worktree"
  fi

  pytest tests/services/assistant/ \
    -k "agent_loop or approval or resume or streaming_first" \
    --no-cov -q --tb=line 2>&1 | tee "${SCRATCH}/approval-tests.log"

  pytest tests/services/eval/test_trace_feedback.py --no-cov -q --tb=line \
    2>&1 | tee -a "${SCRATCH}/approval-tests.log"

  python "${ROOT}/scripts/goal/exercise-shipped-paths.py" 2>&1 | tee "${SCRATCH}/direct-exercise.log"

  git diff --name-only HEAD | sort | tee "${SCRATCH}/changed-files-after-restore.txt"
  comm -23 "${SCRATCH}/changed-files-after-restore.txt" <(map_manifest | sort) \
    | tee "${SCRATCH}/changed-files-workstream-a.txt"

  echo "phase-a: passed $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

echo "verify-sync-port: started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
stash_ws_a_if_needed
run_phase_b
run_phase_a
echo "verify-sync-port: all phases passed $(date -u +%Y-%m-%dT%H:%M:%SZ)"