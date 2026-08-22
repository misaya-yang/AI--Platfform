#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
runtime_image="${CODEX_RUNTIME_IMAGE:-}"
if [[ -z "$runtime_image" ]]; then
    echo "ERROR: CODEX_RUNTIME_IMAGE must name the locally built Runtime image" >&2
    exit 2
fi
if ! docker image inspect "$runtime_image" >/dev/null 2>&1; then
    echo "ERROR: CODEX_RUNTIME_IMAGE is not present locally" >&2
    exit 2
fi
image_artifact="$(docker image inspect "$runtime_image" --format '{{index .Config.Labels "com.misaya.ai-platform.codex.artifact"}}')"
image_binary="$(docker image inspect "$runtime_image" --format '{{index .Config.Labels "com.misaya.ai-platform.codex.binary"}}')"
if [[ "$image_artifact" != "agent_runtime" \
    || "$image_binary" != "ai-platform-agent-runtime" ]]; then
    echo "ERROR: CODEX_RUNTIME_IMAGE is not an AI Platform Agent Runtime artifact" >&2
    exit 2
fi

suffix="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
network_name="codex-runtime-smoke-${suffix}"
postgres_name="codex-runtime-smoke-pg-${suffix}"
runtime_name="codex-runtime-smoke-app-${suffix}"
postgres_password="$(openssl rand -hex 24)"
internal_token="$(openssl rand -hex 32)"
tenant_id="tenant-smoke-${suffix}"
user_id="user-smoke-${suffix}"
session_id="session-smoke-${suffix}"
run_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
snapshot_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
lease_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
lease_signature="v1:$(openssl rand -hex 32)"
nonce_sha256="$(openssl rand -hex 32)"
snapshot_sha256="$(printf '{}' | shasum -a 256 | awk '{print $1}')"

cleanup() {
    docker rm -f "$runtime_name" "$postgres_name" >/dev/null 2>&1 || true
    docker network rm "$network_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create --internal "$network_name" >/dev/null
docker run --detach --rm \
    --name "$postgres_name" \
    --network "$network_name" \
    --env POSTGRES_USER=runtime_smoke \
    --env "POSTGRES_PASSWORD=$postgres_password" \
    --env POSTGRES_DB=runtime_smoke \
    postgres:16-alpine >/dev/null

postgres_ready=false
for _ in $(seq 1 60); do
    if docker exec "$postgres_name" pg_isready -U runtime_smoke -d runtime_smoke >/dev/null 2>&1; then
        postgres_ready=true
        break
    fi
    sleep 1
done
if [[ "$postgres_ready" != true ]]; then
    echo "ERROR: isolated PostgreSQL did not become ready" >&2
    exit 1
fi

docker exec -i "$postgres_name" psql -v ON_ERROR_STOP=1 -U runtime_smoke -d runtime_smoke \
    < "$repo_root/deploy/codex-harness/runtime-smoke-base.sql" >/dev/null
docker exec -i "$postgres_name" psql -v ON_ERROR_STOP=1 -U runtime_smoke -d runtime_smoke \
    < "$repo_root/database/migrations/089_codex_runtime_thread_store.sql" >/dev/null
docker exec -i "$postgres_name" psql -v ON_ERROR_STOP=1 -U runtime_smoke -d runtime_smoke \
    < "$repo_root/database/migrations/090_codex_runtime_model_leases.sql" >/dev/null
docker exec "$postgres_name" psql -v ON_ERROR_STOP=1 -U runtime_smoke -d runtime_smoke -c \
    "INSERT INTO sessions (session_id, service_id, user_id, tenant_id) VALUES ('$session_id', '__builtin_assistant__', '$user_id', '$tenant_id')" \
    >/dev/null

start_runtime() {
    docker run --detach \
        --name "$runtime_name" \
        --network "$network_name" \
        --tmpfs /var/lib/ai-platform-codex/runtime-home:rw,uid=10001,gid=10001,mode=0700 \
        --env "AI_PLATFORM_INTERNAL_TOKEN=$internal_token" \
        --env AI_PLATFORM_RUNTIME_BIND=0.0.0.0:8094 \
        --env AI_PLATFORM_RUNTIME_WORKDIR=/workspace \
        --env AI_PLATFORM_CODEX_HOME=/var/lib/ai-platform-codex/runtime-home \
        --env "POSTGRES_HOST=$postgres_name" \
        --env POSTGRES_PORT=5432 \
        --env POSTGRES_USER=runtime_smoke \
        --env "POSTGRES_PASSWORD=$postgres_password" \
        --env POSTGRES_DB=runtime_smoke \
        "$runtime_image" >/dev/null

    local runtime_ready=false
    for _ in $(seq 1 60); do
        if docker exec "$runtime_name" curl -fsS http://127.0.0.1:8094/health/ready \
            >/dev/null 2>&1; then
            runtime_ready=true
            break
        fi
        if [[ "$(docker inspect --format '{{.State.Running}}' "$runtime_name" 2>/dev/null || true)" != "true" ]]; then
            echo "ERROR: Runtime container exited before readiness" >&2
            docker logs "$runtime_name" >&2 || true
            return 1
        fi
        sleep 1
    done
    if [[ "$runtime_ready" != true ]]; then
        echo "ERROR: Runtime container did not become ready" >&2
        docker logs "$runtime_name" >&2 || true
        return 1
    fi
    docker exec "$runtime_name" sh -c \
        'test -r /sys/fs/cgroup/memory.current && printf "CODEX_RUNTIME_MEMORY_CURRENT_BYTES=%s\n" "$(cat /sys/fs/cgroup/memory.current)"' \
        || true
}

start_runtime
create_response="$(docker exec "$runtime_name" curl -fsS \
    -H "x-ai-platform-internal-token: $internal_token" \
    -H 'content-type: application/json' \
    -d "{\"tenantId\":\"$tenant_id\",\"userId\":\"$user_id\",\"sessionId\":\"$session_id\",\"start\":{}}" \
    http://127.0.0.1:8094/internal/v1/threads)"
thread_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["thread"]["id"])' <<<"$create_response")"
if [[ ! "$thread_id" =~ ^[0-9a-f-]{36}$ ]]; then
    echo "ERROR: Runtime returned an invalid Thread ID" >&2
    exit 1
fi

docker exec "$postgres_name" psql -v ON_ERROR_STOP=1 -U runtime_smoke -d runtime_smoke -c \
    "INSERT INTO assistant_session_runtime_assignments (tenant_id,user_id,session_id,runtime_owner,kernel_revision,assignment_reason) VALUES ('$tenant_id','$user_id','$session_id','codex_candidate','smoke-kernel','explicit'); SELECT issue_assistant_runtime_turn('$snapshot_id','$lease_id','$run_id','$thread_id','$tenant_id','$user_id','$session_id','smoke-kernel','codex-runtime-snapshot/v1','{}'::jsonb,'$snapshot_sha256',1,'minimal','codex-runtime-model-lease/v1','dashscope','qwen3.7-plus','smoke-provider','$nonce_sha256',1,1000000,1024,1000000,NOW() + INTERVAL '15 minutes','runtime smoke');" \
    >/dev/null

docker rm -f "$runtime_name" >/dev/null
start_runtime
resume_response="$(docker exec "$runtime_name" curl -fsS \
    -H "x-ai-platform-internal-token: $internal_token" \
    -H "x-ai-tenant-id: $tenant_id" \
    -H "x-ai-user-id: $user_id" \
    -H "x-ai-session-id: $session_id" \
    -H 'content-type: application/json' \
    -d '{"model":"qwen3.7-plus","modelPlaneBaseUrl":"http://gateway.invalid/internal/v1/codex-model-plane"}' \
    "http://127.0.0.1:8094/internal/v1/threads/$thread_id/resume")"
resumed_thread_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["thread"]["id"])' <<<"$resume_response")"
if [[ "$resumed_thread_id" != "$thread_id" ]]; then
    echo "ERROR: second Runtime process resumed a different Thread" >&2
    exit 1
fi


turn_response="$(docker exec "$runtime_name" curl -fsS \
    -H "x-ai-platform-internal-token: $internal_token" \
    -H "x-ai-tenant-id: $tenant_id" \
    -H "x-ai-user-id: $user_id" \
    -H "x-ai-session-id: $session_id" \
    -H 'content-type: application/json' \
    -d "{\"runId\":\"$run_id\",\"snapshotId\":\"$snapshot_id\",\"leaseId\":\"$lease_id\",\"leaseSignature\":\"$lease_signature\",\"message\":\"hello\",\"model\":\"qwen3.7-plus\",\"effort\":\"minimal\"}" \
    "http://127.0.0.1:8094/internal/v1/threads/$thread_id/turns")"
returned_turn_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["turn"]["id"])' <<<"$turn_response")"
if [[ "$returned_turn_id" != "$run_id" ]]; then
    echo "ERROR: Runtime did not preserve the pre-authorized Turn ID" >&2
    exit 1
fi

member_count="$(docker exec "$postgres_name" psql -At -U runtime_smoke -d runtime_smoke -c \
    "SELECT COUNT(*) FROM assistant_runtime_thread_members WHERE runtime_thread_id = '$thread_id'")"
if [[ "$member_count" != "1" ]]; then
    echo "ERROR: Runtime root membership is missing or duplicated" >&2
    exit 1
fi

echo "codex-runtime-smoke: ok"
