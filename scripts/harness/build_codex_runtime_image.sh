#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
fork_root="${CODEX_HARNESS_FORK:-}"
build_memory="${CODEX_HARNESS_BUILD_MEMORY:-4g}"
build_cpu_quota="${CODEX_HARNESS_BUILD_CPU_QUOTA:-200000}"
cargo_jobs="${CODEX_HARNESS_CARGO_JOBS:-2}"
minimum_host_free_percent="${CODEX_HARNESS_MIN_HOST_FREE_PERCENT:-20}"

if [[ -z "$fork_root" || ! -e "$fork_root/.git" ]]; then
    echo "ERROR: CODEX_HARNESS_FORK must point to the controlled Codex fork" >&2
    exit 1
fi
if [[ -n "$(git -C "$fork_root" status --porcelain)" ]]; then
    echo "ERROR: Codex Harness fork must be clean before a Runtime image build" >&2
    exit 1
fi
if ! git -C "$fork_root" cat-file -e HEAD:codex-rs/ai-platform-agent-runtime/Cargo.toml; then
    echo "ERROR: pinned fork revision does not contain ai-platform-agent-runtime" >&2
    exit 1
fi

fork_sha="$(git -C "$fork_root" rev-parse HEAD)"
receipt_path="$repo_root/deploy/codex-harness/source-receipt.json"
receipt_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["fork_sha"])' "$receipt_path")"
schema_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["schema_bundle"]["sha256"])' "$receipt_path")"
if [[ "$fork_sha" != "$receipt_sha" ]]; then
    echo "ERROR: fork HEAD does not match the source receipt" >&2
    exit 1
fi

image_tag="ai-gateway-codex-agent-runtime:local-${fork_sha:0:12}"
build_context="$(mktemp -d /tmp/ai-platform-codex-runtime-build.XXXXXX)"

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
    --file "$repo_root/deploy/codex-harness/Dockerfile.runtime" \
    --build-arg "CODEX_FORK_SHA=$fork_sha" \
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
if [[ "$image_revision" != "$fork_sha" \
    || "$image_schema_sha" != "$schema_sha" \
    || "$image_artifact" != "agent_runtime" \
    || "$image_binary" != "ai-platform-agent-runtime" ]]; then
    echo "ERROR: Runtime image labels do not match the source receipt and artifact identity" >&2
    exit 1
fi

image_id="$(docker image inspect "$image_tag" --format '{{.Id}}')"
python3 "$repo_root/scripts/harness/codex_harness_supply_chain.py" record-local-image \
    --repo-root "$repo_root" \
    --lock "$repo_root/deploy/codex-harness/lock.json" \
    --artifact agent_runtime \
    --image "$image_tag"
printf 'CODEX_RUNTIME_IMAGE_TAG=%s\n' "$image_tag"
printf 'CODEX_RUNTIME_IMAGE_DIGEST=%s\n' "$image_id"
printf 'CODEX_RUNTIME_SCHEMA_SHA256=%s\n' "$image_schema_sha"
