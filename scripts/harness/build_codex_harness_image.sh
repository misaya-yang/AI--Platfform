#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
fork_root="${CODEX_HARNESS_FORK:-}"
build_memory="${CODEX_HARNESS_BUILD_MEMORY:-4g}"
build_cpu_quota="${CODEX_HARNESS_BUILD_CPU_QUOTA:-200000}"
cargo_jobs="${CODEX_HARNESS_CARGO_JOBS:-2}"
minimum_host_free_percent="${CODEX_HARNESS_MIN_HOST_FREE_PERCENT:-20}"

if [[ -z "$fork_root" || ! -d "$fork_root/.git" ]]; then
    echo "ERROR: CODEX_HARNESS_FORK must point to the independent Codex fork" >&2
    exit 1
fi

if [[ -n "$(git -C "$fork_root" status --porcelain)" ]]; then
    echo "ERROR: Codex Harness fork must be clean before an image build" >&2
    exit 1
fi

upstream_sha="$(git -C "$fork_root" rev-parse HEAD)"
receipt_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["fork_sha"])' "$repo_root/deploy/codex-harness/source-receipt.json")"
schema_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["schema_bundle"]["sha256"])' "$repo_root/deploy/codex-harness/source-receipt.json")"

if [[ "$upstream_sha" != "$receipt_sha" ]]; then
    echo "ERROR: fork HEAD does not match the committed source receipt" >&2
    exit 1
fi

image_tag="ai-gateway-codex-harness:local-${upstream_sha:0:12}"
build_context="$(mktemp -d /tmp/ai-platform-codex-build.XXXXXX)"

cleanup() {
    rm -rf -- "$build_context"
}
trap cleanup EXIT

git -C "$fork_root" archive HEAD | tar -x -C "$build_context"

monitor_build_memory() {
    local build_pid="$1"
    if ! command -v memory_pressure >/dev/null 2>&1; then
        wait "$build_pid"
        return
    fi
    while kill -0 "$build_pid" >/dev/null 2>&1; do
        local free_percent
        free_percent="$(memory_pressure | awk '/System-wide memory free percentage:/ {gsub(/%/, "", $5); print $5}')"
        if [[ "$free_percent" =~ ^[0-9]+$ ]] && (( free_percent < minimum_host_free_percent )); then
            echo "ERROR: host memory free percentage fell to ${free_percent}%; cancelling Docker build" >&2
            kill "$build_pid" >/dev/null 2>&1 || true
            wait "$build_pid" || true
            return 1
        fi
        sleep 10
    done
    wait "$build_pid"
}

docker build \
    --resource "memory=$build_memory" \
    --resource "cpu-quota=$build_cpu_quota" \
    --file "$repo_root/deploy/codex-harness/Dockerfile" \
    --build-arg "CODEX_UPSTREAM_SHA=$upstream_sha" \
    --build-arg "CODEX_SCHEMA_SHA256=$schema_sha" \
    --build-arg "CARGO_BUILD_JOBS=$cargo_jobs" \
    --tag "$image_tag" \
    "$build_context" &
build_pid="$!"
monitor_build_memory "$build_pid"

image_revision="$(docker image inspect "$image_tag" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
image_schema_sha="$(docker image inspect "$image_tag" --format '{{index .Config.Labels "com.misaya.ai-platform.codex.schema-sha256"}}')"
image_artifact="$(docker image inspect "$image_tag" --format '{{index .Config.Labels "com.misaya.ai-platform.codex.artifact"}}')"
image_binary="$(docker image inspect "$image_tag" --format '{{index .Config.Labels "com.misaya.ai-platform.codex.binary"}}')"
if [[ "$image_revision" != "$upstream_sha" \
    || "$image_schema_sha" != "$schema_sha" \
    || "$image_artifact" != "app_server" \
    || "$image_binary" != "codex-app-server" ]]; then
    echo "ERROR: image source/schema/artifact labels do not match the source receipt" >&2
    exit 1
fi

python3 - "$image_tag" <<'PY'
import json
import select
import subprocess
import sys
import time

image = sys.argv[1]
request = {
    "method": "initialize",
    "id": 0,
    "params": {"clientInfo": {"name": "ai_platform_probe", "title": "AI Platform Probe", "version": "0.1.0"}},
}
process = subprocess.Popen(
    ["docker", "run", "--rm", "-i", "--network", "none", image],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
assert process.stdin is not None
assert process.stdout is not None
assert process.stderr is not None
process.stdin.write(json.dumps(request) + "\n")
process.stdin.flush()

deadline = time.monotonic() + 30
initialize_seen = False
stdout_lines = []
while time.monotonic() < deadline:
    readable, _, _ = select.select([process.stdout], [], [], 1)
    if not readable:
        if process.poll() is not None:
            break
        continue
    line = process.stdout.readline()
    if not line:
        break
    stdout_lines.append(line)
    if not line.strip().startswith("{"):
        continue
    item = json.loads(line)
    if item.get("id") == 0 and isinstance(item.get("result"), dict):
        initialize_seen = True
        break

process.stdin.close()
try:
    process.wait(timeout=10)
except subprocess.TimeoutExpired:
    process.terminate()
    process.wait(timeout=5)
stderr = process.stderr.read()
if not initialize_seen:
    raise SystemExit(
        "image protocol probe did not return initialize result; "
        f"stdout={''.join(stdout_lines)[-1000:]!r}; stderr={stderr[-1000:]!r}"
    )
PY

image_id="$(docker image inspect "$image_tag" --format '{{.Id}}')"
python3 "$repo_root/scripts/harness/codex_harness_supply_chain.py" record-local-image \
    --repo-root "$repo_root" \
    --lock "$repo_root/deploy/codex-harness/lock.json" \
    --artifact app_server \
    --image "$image_tag"
printf 'CODEX_HARNESS_IMAGE_TAG=%s\n' "$image_tag"
printf 'CODEX_HARNESS_IMAGE_DIGEST=%s\n' "$image_id"
printf 'CODEX_HARNESS_SCHEMA_SHA256=%s\n' "$image_schema_sha"
