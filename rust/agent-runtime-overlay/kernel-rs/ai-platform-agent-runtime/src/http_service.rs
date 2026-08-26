use std::collections::HashMap;
use std::collections::HashSet;
use std::convert::Infallible;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;
use std::time::Duration;
use std::time::Instant;

use axum::Json;
use axum::Router;
use axum::extract::Path;
use axum::extract::Query;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::http::HeaderValue;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::response::sse::Event;
use axum::response::sse::KeepAlive;
use axum::response::sse::Sse;
use axum::routing::get;
use axum::routing::post;
use codex_app_server::in_process::InProcessServerEvent;
use codex_app_server_client::InProcessAppServerRequestHandle;
use codex_app_server_protocol::ClientRequest;
use codex_app_server_protocol::JSONRPCErrorError;
use codex_app_server_protocol::RequestId;
use codex_app_server_protocol::ThreadMemoryMode;
use codex_app_server_protocol::ThreadMemoryModeSetParams;
use codex_app_server_protocol::AdditionalContextEntry;
use codex_app_server_protocol::AdditionalContextKind;
use codex_app_server_protocol::ThreadStartParams;
use codex_app_server_protocol::ThreadStartResponse;
use codex_app_server_protocol::TurnStartParams;
use codex_app_server_protocol::TurnStartResponse;
use codex_app_server_protocol::UserInput;
use codex_protocol::ThreadId;
use codex_protocol::openai_models::ReasoningEffort;
use serde::Deserialize;
use serde::Serialize;
use serde_json::json;
use sha2::Digest;
use sha2::Sha256;
use sqlx::Row;
use tokio::sync::Semaphore;
use tokio::sync::broadcast;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;
use tracing::warn;
use uuid::Uuid;

use crate::AgentKernel;
use crate::PlatformThreadIdentity;
use crate::PostgresThreadStore;
use crate::SequencedAssistantTurnEventV1;
use crate::V1ProjectionContext;
use crate::approval_control::ApprovalBroker;
use crate::approval_control::ApprovalDecisionRequest;
use crate::capability_execution::{
    CapabilityExecutionOutcome, ReadonlyCapabilityBinding, execute_capability,
};
use crate::capability_worker::CapabilityWorkerClient;
use crate::postgres_store::PlatformLifecycleEvent;
use crate::project_server_notification;
use crate::readonly_capabilities::RuntimeCapabilityScope;
use crate::readonly_capabilities::render_turn_input;
use crate::readonly_capabilities::validate_platform_config;
use crate::server_notification_thread_id;
use ai_platform_capability_contract::CapabilityEffect;
use ai_platform_capability_contract::CapabilityExecutionStatus;

mod security;
mod thread_lifecycle;

use self::security::RuntimeError;
use self::security::SESSION_HEADER;
use self::security::TENANT_HEADER;
use self::security::USER_HEADER;
use self::security::authorize;
use self::security::required_header;
use self::thread_lifecycle::archive_thread;
use self::thread_lifecycle::authorize_thread_scope;
use self::thread_lifecycle::interrupt_turn;
use self::thread_lifecycle::resume_thread;
use self::thread_lifecycle::unarchive_thread;
const EVENT_CHANNEL_CAPACITY: usize = 256;

#[derive(Clone)]
pub struct RuntimeHttpState {
    store: Arc<PostgresThreadStore>,
    requests: InProcessAppServerRequestHandle,
    approvals: ApprovalBroker,
    events: broadcast::Sender<RuntimeBroadcastEvent>,
    internal_token: Arc<str>,
    kernel_ready: Arc<AtomicBool>,
    readonly_by_turn: Arc<Mutex<HashMap<String, ReadonlyTurnBinding>>>,
    turn_cancellations: Arc<Mutex<HashMap<String, CancellationToken>>>,
}

#[derive(Clone, Debug)]
struct ReadonlyTurnBinding {
    snapshot_id: String,
    capability_revision: i64,
    payload: serde_json::Value,
    created_at: Instant,
}

const READONLY_TURN_TTL: Duration = Duration::from_secs(900);
const READONLY_TURN_MAX_ENTRIES: usize = 1_024;

#[derive(Clone, Debug)]
struct RuntimeBroadcastEvent {
    root_thread_id: ThreadId,
    event: SequencedAssistantTurnEventV1,
}

#[derive(Default)]
struct TextProjectionState {
    turns: HashMap<String, TurnTextProjectionState>,
}

#[derive(Default)]
struct TurnTextProjectionState {
    delta_items: HashSet<String>,
    completed_items: HashSet<String>,
    child_agents: HashSet<String>,
}

impl TextProjectionState {
    fn normalize_subagent_events(
        &mut self,
        projected: Vec<crate::AssistantTurnEventV1>,
    ) -> Vec<crate::AssistantTurnEventV1> {
        let mut normalized = Vec::with_capacity(projected.len());
        for event in projected {
            let run_id = event
                .data
                .get("run_id")
                .and_then(serde_json::Value::as_str)
                .unwrap_or_default();
            let agent_id = event
                .data
                .get("agent_id")
                .and_then(serde_json::Value::as_str)
                .unwrap_or_default();
            if run_id.is_empty() || agent_id.is_empty() {
                normalized.push(event);
                continue;
            }
            if event.event_type == "subagent_started" {
                if self.turn(run_id).child_agents.insert(agent_id.to_string()) {
                    normalized.push(event);
                }
                continue;
            }
            if event.event_type == "subagent_finished"
                && self.turn(run_id).child_agents.insert(agent_id.to_string())
            {
                let mut started = event.data.clone();
                started["status"] = "running".into();
                normalized.push(crate::AssistantTurnEventV1::new(
                    "subagent_started",
                    started,
                ));
            }
            normalized.push(event);
        }
        normalized
    }

    fn fallback_event(
        &mut self,
        notification: &codex_app_server_protocol::ServerNotification,
        context: &V1ProjectionContext,
    ) -> Option<crate::AssistantTurnEventV1> {
        match notification {
            codex_app_server_protocol::ServerNotification::AgentMessageDelta(delta) => {
                self.turn(&delta.turn_id)
                    .delta_items
                    .insert(delta.item_id.clone());
                None
            }
            codex_app_server_protocol::ServerNotification::ItemCompleted(completed) => {
                let codex_app_server_protocol::ThreadItem::AgentMessage { id, text, .. } =
                    &completed.item
                else {
                    return None;
                };
                let turn = self.turn(&completed.turn_id);
                if !turn.completed_items.insert(id.clone()) || turn.delta_items.contains(id) {
                    return None;
                }
                Some(crate::AssistantTurnEventV1::new(
                    "text_delta",
                    json!({
                        "run_id": completed.turn_id,
                        "session_id": context.session_id,
                        "thread_id": completed.thread_id,
                        "item_id": id,
                        "content": text,
                        "projection": "completed_item_fallback",
                    }),
                ))
            }
            codex_app_server_protocol::ServerNotification::TurnCompleted(completed) => {
                self.turns.remove(&completed.turn.id);
                None
            }
            _ => None,
        }
    }

    fn turn(&mut self, turn_id: &str) -> &mut TurnTextProjectionState {
        const MAX_ACTIVE_TURNS: usize = 1_024;
        if !self.turns.contains_key(turn_id)
            && self.turns.len() >= MAX_ACTIVE_TURNS
            && let Some(stale) = self.turns.keys().next().cloned()
        {
            self.turns.remove(&stale);
        }
        self.turns.entry(turn_id.to_string()).or_default()
    }
}

pub struct RuntimeHttpService {
    state: RuntimeHttpState,
    shutdown_tx: Option<oneshot::Sender<()>>,
    event_task: JoinHandle<()>,
}

impl RuntimeHttpState {
    pub(super) fn cancel_turn(&self, turn_id: &str) {
        if let Some(token) = self
            .turn_cancellations
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(turn_id)
            .cloned()
        {
            token.cancel();
        }
    }
}

impl RuntimeHttpService {
    pub fn start(
        kernel: AgentKernel,
        store: Arc<PostgresThreadStore>,
        internal_token: String,
    ) -> Result<Self, std::io::Error> {
        if internal_token.is_empty() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "AI_PLATFORM_INTERNAL_TOKEN must be non-empty",
            ));
        }
        let requests = kernel.request_handle();
        let (events, _) = broadcast::channel(EVENT_CHANNEL_CAPACITY);
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        let kernel_ready = Arc::new(AtomicBool::new(true));
        let approvals = ApprovalBroker::new();
        let startup_cutoff = chrono::Utc::now();
        {
            let approvals = approvals.clone();
            let store = Arc::clone(&store);
            tokio::spawn(async move {
                approvals
                    .reconcile_after_restart(&store, startup_cutoff)
                    .await;
            });
        }
        let readonly_by_turn = Arc::new(Mutex::new(HashMap::new()));
        let turn_cancellations = Arc::new(Mutex::new(HashMap::new()));
        let capability_client = reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(std::time::Duration::from_secs(2))
            .timeout(std::time::Duration::from_secs(30))
            .build()
            .map_err(|error| std::io::Error::other(error.to_string()))?;
        let event_task = tokio::spawn(route_kernel_events(
            kernel,
            Arc::clone(&store),
            approvals.clone(),
            events.clone(),
            Arc::clone(&kernel_ready),
            Arc::clone(&readonly_by_turn),
            Arc::clone(&turn_cancellations),
            capability_client,
            shutdown_rx,
        ));
        Ok(Self {
            state: RuntimeHttpState {
                store,
                requests,
                approvals,
                events,
                internal_token: internal_token.into(),
                kernel_ready,
                readonly_by_turn,
                turn_cancellations,
            },
            shutdown_tx: Some(shutdown_tx),
            event_task,
        })
    }

    pub fn router(&self) -> Router {
        Router::new()
            .route("/health/live", get(live))
            .route("/health/ready", get(ready))
            .route("/internal/v1/threads", post(create_thread))
            .route(
                "/internal/v1/sessions/{session_id}/cleanup",
                post(cleanup_session),
            )
            .route("/internal/v1/approvals/{approval_id}", get(get_approval))
            .route(
                "/internal/v1/approvals/{approval_id}/decision",
                post(decide_approval),
            )
            .route("/internal/v1/threads/{thread_id}/turns", post(start_turn))
            .route(
                "/internal/v1/threads/{thread_id}/turns/{turn_id}/interrupt",
                post(interrupt_turn),
            )
            .route(
                "/internal/v1/threads/{thread_id}/resume",
                post(resume_thread),
            )
            .route(
                "/internal/v1/threads/{thread_id}/archive",
                post(archive_thread),
            )
            .route(
                "/internal/v1/threads/{thread_id}/unarchive",
                post(unarchive_thread),
            )
            .route("/internal/v1/threads/{thread_id}/events", get(events))
            .with_state(self.state.clone())
    }

    pub async fn shutdown(mut self) {
        if let Some(shutdown_tx) = self.shutdown_tx.take() {
            let _ = shutdown_tx.send(());
        }
        let _ = self.event_task.await;
    }
}

#[derive(Serialize)]
struct HealthResponse {
    status: &'static str,
    kernel: &'static str,
}

async fn live() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        kernel: "agent-runtime",
    })
}

async fn ready(
    State(state): State<RuntimeHttpState>,
) -> Result<Json<HealthResponse>, RuntimeError> {
    if !state.kernel_ready.load(Ordering::Acquire) {
        return Err(RuntimeError::unavailable("agent_kernel_unavailable"));
    }
    sqlx::query_scalar::<_, i32>("SELECT 1")
        .fetch_one(&state.store.pool)
        .await
        .map_err(|_| RuntimeError::unavailable("runtime_store_unavailable"))?;
    Ok(Json(HealthResponse {
        status: "ready",
        kernel: "agent-runtime",
    }))
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct CreateThreadRequest {
    tenant_id: String,
    user_id: String,
    session_id: String,
    #[serde(default)]
    start: ThreadStartParams,
    /// Platform-selected memory behavior for this thread. Applied through
    /// the kernel's native thread/memoryMode/set request after the durable
    /// root identity is reserved.
    memory_mode: Option<ThreadMemoryMode>,
    /// Model capability data from the immutable platform snapshot. These are
    /// converted to the kernel's config keys before thread creation so child
    /// threads inherit the same context/compaction policy.
    model_context_window: Option<i64>,
    auto_compact_token_limit: Option<i64>,
}

async fn create_thread(
    State(state): State<RuntimeHttpState>,
    headers: HeaderMap,
    Json(body): Json<CreateThreadRequest>,
) -> Result<Json<ThreadStartResponse>, RuntimeError> {
    authorize(&headers, &state.internal_token)?;
    if body.start.ephemeral == Some(true) {
        return Err(RuntimeError::bad_request("ephemeral_thread_not_supported"));
    }
    let mut start = body.start;
    apply_model_limits(
        &mut start,
        body.model_context_window,
        body.auto_compact_token_limit,
    )?;
    let memory_mode = body.memory_mode;
    validate_memory_mode(memory_mode)?;
    let root_thread_id = ThreadId::new();
    state
        .store
        .authorize_root(&PlatformThreadIdentity::new(
            root_thread_id,
            body.tenant_id,
            body.user_id,
            body.session_id,
        ))
        .await
        .map_err(RuntimeError::from_store)?;
    let result = state
        .requests
        .request_thread_start(
            ClientRequest::ThreadStart {
                request_id: RequestId::String(format!("thread-start-{root_thread_id}")),
                params: start,
            },
            codex_app_server::host_runtime::AppServerThreadStartOptions::new(root_thread_id),
        )
        .await
        .map_err(|_| RuntimeError::unavailable("agent_kernel_unavailable"))?
        .map_err(|_| RuntimeError::bad_request("agent_thread_start_rejected"))?;
    if let Some(mode) = memory_mode {
        state
            .requests
            .request(ClientRequest::ThreadMemoryModeSet {
                request_id: RequestId::String(format!("thread-memory-mode-{root_thread_id}")),
                params: ThreadMemoryModeSetParams {
                    thread_id: root_thread_id.to_string(),
                    mode,
                },
            })
            .await
            .map_err(|_| RuntimeError::unavailable("agent_kernel_unavailable"))?
            .map_err(|_| RuntimeError::bad_request("agent_thread_memory_mode_rejected"))?;
    }
    serde_json::from_value(result)
        .map(Json)
        .map_err(|_| RuntimeError::internal("invalid_agent_thread_start_response"))
}

#[derive(Serialize)]
struct SessionCleanupResponse {
    session_id: String,
    status: &'static str,
}

/// Tombstone the Runtime projection and root identity for one platform
/// session. Items remain append-only for audit/recovery, and scope is bound
/// both to the authenticated headers and the path session id.
async fn cleanup_session(
    State(state): State<RuntimeHttpState>,
    Path(session_id): Path<String>,
    headers: HeaderMap,
) -> Result<Json<SessionCleanupResponse>, RuntimeError> {
    authorize(&headers, &state.internal_token)?;
    if session_id.is_empty()
        || session_id.len() > 255
        || session_id.bytes().any(|byte| byte.is_ascii_control())
    {
        return Err(RuntimeError::bad_request("invalid_session_id"));
    }
    let tenant_id = required_header(&headers, TENANT_HEADER)?;
    let user_id = required_header(&headers, USER_HEADER)?;
    let header_session_id = required_header(&headers, SESSION_HEADER)?;
    if [tenant_id.as_str(), user_id.as_str()]
        .into_iter()
        .any(|value| value.len() > 255 || value.bytes().any(|byte| byte.is_ascii_control()))
    {
        return Err(RuntimeError::bad_request("invalid_runtime_scope"));
    }
    if header_session_id != session_id {
        return Err(RuntimeError::not_found("session_not_found"));
    }
    let deleted = state
        .store
        .cleanup_session(&tenant_id, &user_id, &session_id)
        .await
        .map_err(RuntimeError::from_store)?;
    Ok(Json(SessionCleanupResponse {
        session_id,
        status: if deleted { "deleted" } else { "not_found" },
    }))
}

fn validate_memory_mode(mode: Option<ThreadMemoryMode>) -> Result<(), RuntimeError> {
    if matches!(mode, Some(ThreadMemoryMode::Enabled)) {
        // The upstream memory generator persists into a process-local SQLite
        // home. This service is multi-tenant, so accepting `enabled` here
        // would create an unscoped cross-tenant memory store. Until the
        // platform memory contributor is wired to tenant-scoped storage,
        // fail closed rather than silently enabling it.
        return Err(RuntimeError::bad_request(
            "agent_memory_backend_unavailable",
        ));
    }
    Ok(())
}

/// Merge model capability limits into the official thread config map. The
/// platform sends camelCase fields while the kernel config loader owns the
/// snake_case keys. Conflicting values are rejected rather than silently
/// choosing one source of truth.
fn apply_model_limits(
    start: &mut ThreadStartParams,
    model_context_window: Option<i64>,
    auto_compact_token_limit: Option<i64>,
) -> Result<(), RuntimeError> {
    if model_context_window.is_none() && auto_compact_token_limit.is_none() {
        return Ok(());
    }
    let context_window = model_context_window.ok_or_else(|| {
        RuntimeError::bad_request("model_context_window_required_for_compaction_policy")
    })?;
    if !(1..=10_000_000).contains(&context_window) {
        return Err(RuntimeError::bad_request("invalid_model_context_window"));
    }
    let compact_limit = auto_compact_token_limit.unwrap_or((context_window * 9) / 10);
    if !(1..=context_window).contains(&compact_limit) {
        return Err(RuntimeError::bad_request(
            "invalid_auto_compact_token_limit",
        ));
    }
    let config = start.config.get_or_insert_with(HashMap::new);
    insert_matching_config(config, "model_context_window", context_window)?;
    insert_matching_config(config, "model_auto_compact_token_limit", compact_limit)?;
    Ok(())
}

fn insert_matching_config(
    config: &mut HashMap<String, serde_json::Value>,
    key: &str,
    value: i64,
) -> Result<(), RuntimeError> {
    let json_value = serde_json::Value::from(value);
    if let Some(existing) = config.get(key) {
        if existing != &json_value {
            return Err(RuntimeError::bad_request(
                "conflicting_model_runtime_config",
            ));
        }
    } else {
        config.insert(key.to_string(), json_value);
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct StartTurnRequest {
    run_id: Uuid,
    snapshot_id: Uuid,
    lease_id: Uuid,
    lease_signature: String,
    message: String,
    model: String,
    effort: Option<String>,
    capability_revision: i64,
    #[serde(default)]
    readonly: Option<serde_json::Value>,
    #[serde(default)]
    platform_config: Option<serde_json::Value>,
}

async fn start_turn(
    State(state): State<RuntimeHttpState>,
    Path(thread_id): Path<String>,
    headers: HeaderMap,
    Json(body): Json<StartTurnRequest>,
) -> Result<Json<TurnStartResponse>, RuntimeError> {
    let thread_id = authorize_thread_scope(&state, &headers, &thread_id).await?;
    validate_start_turn_request(&body)?;
    let tenant_id = required_header(&headers, TENANT_HEADER)?;
    let user_id = required_header(&headers, USER_HEADER)?;
    let session_id = required_header(&headers, SESSION_HEADER)?;
    let readonly_input = body
        .readonly
        .as_ref()
        .map(|payload| {
            render_turn_input(
                &RuntimeCapabilityScope {
                    tenant_id: tenant_id.clone(),
                    user_id: user_id.clone(),
                    session_id: session_id.clone(),
                    capability_revision: body.capability_revision,
                    snapshot_id: body.snapshot_id.to_string(),
                },
                payload,
            )
        })
        .transpose()
        .map_err(|_| RuntimeError::bad_request("invalid_readonly_capability_payload"))?;
    if let Some(platform_config) = body.platform_config.as_ref() {
        validate_platform_config(
            &RuntimeCapabilityScope {
                tenant_id: tenant_id.clone(),
                user_id: user_id.clone(),
                session_id: session_id.clone(),
                capability_revision: body.capability_revision,
                snapshot_id: body.snapshot_id.to_string(),
            },
            platform_config,
        )
        .map_err(|_| RuntimeError::bad_request("invalid_platform_runtime_config"))?;
        if body
            .readonly
            .as_ref()
            .and_then(|value| value.get("platform_config"))
            != Some(platform_config)
        {
            return Err(RuntimeError::bad_request(
                "platform_runtime_config_mismatch",
            ));
        }
    } else {
        return Err(RuntimeError::bad_request("platform_runtime_config_missing"));
    }
    let authorized = sqlx::query_scalar::<_, bool>(
        r#"
        SELECT EXISTS (
            SELECT 1
              FROM assistant_runtime_model_leases AS l
              JOIN assistant_runtime_snapshots AS s
                ON s.snapshot_id = l.snapshot_id
               AND s.run_id = l.run_id
               AND s.tenant_id = l.tenant_id
               AND s.user_id = l.user_id
               AND s.session_id = l.session_id
              JOIN assistant_runs AS r
                ON r.run_id = l.run_id
             WHERE l.lease_id = $1
               AND l.snapshot_id = $2
               AND l.run_id = $3
               AND l.runtime_thread_id = $4
               AND l.tenant_id = $5
               AND l.user_id = $6
               AND l.session_id = $7
               AND l.model_id = $8
               AND s.capability_revision = $9
               AND l.status = 'active'
               AND l.expires_at > NOW()
               AND r.status = 'running'
               AND r.engine = 'agent_runtime'
               AND NOT EXISTS (
                   SELECT 1
                     FROM assistant_runtime_snapshot_revocations AS rev
                    WHERE rev.snapshot_id = l.snapshot_id
               )
        )
        "#,
    )
    .bind(body.lease_id)
    .bind(body.snapshot_id)
    .bind(body.run_id)
    .bind(
        Uuid::parse_str(&thread_id.to_string())
            .map_err(|_| RuntimeError::bad_request("invalid_thread_id"))?,
    )
    .bind(&tenant_id)
    .bind(&user_id)
    .bind(&session_id)
    .bind(&body.model)
    .bind(body.capability_revision)
    .fetch_one(&state.store.pool)
    .await
    .map_err(|_| RuntimeError::unavailable("runtime_lease_store_unavailable"))?;
    if !authorized {
        return Err(RuntimeError::not_found("runtime_turn_lease_not_found"));
    }
    if let Some(payload) = body.readonly.clone() {
        let mut bindings = state
            .readonly_by_turn
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let now = Instant::now();
        bindings.retain(|_, binding| now.duration_since(binding.created_at) < READONLY_TURN_TTL);
        if bindings.len() >= READONLY_TURN_MAX_ENTRIES
            && let Some(oldest) = bindings
                .iter()
                .min_by_key(|(_, binding)| binding.created_at)
                .map(|(key, _)| key.clone())
        {
            bindings.remove(&oldest);
        }
        bindings.insert(
            body.run_id.to_string(),
            ReadonlyTurnBinding {
                snapshot_id: body.snapshot_id.to_string(),
                capability_revision: body.capability_revision,
                payload,
                created_at: now,
            },
        );
    }

    let mut metadata = std::collections::HashMap::new();
    metadata.insert(
        "ai_platform_lease_id".to_string(),
        body.lease_id.to_string(),
    );
    metadata.insert(
        "ai_platform_lease_signature".to_string(),
        body.lease_signature,
    );
    metadata.insert(
        "ai_platform_scope_sha256".to_string(),
        runtime_scope_sha256(&tenant_id, &user_id, &session_id),
    );
    let input = vec![UserInput::Text {
        text: body.message,
        text_elements: Vec::new(),
    }];
    let additional_context = readonly_input.flatten().map(|value| {
        HashMap::from([(
            "ai_platform_readonly".to_string(),
            AdditionalContextEntry {
                value,
                kind: AdditionalContextKind::Untrusted,
            },
        )])
    });
    let params = TurnStartParams {
        thread_id: thread_id.to_string(),
        input,
        additional_context,
        responsesapi_client_metadata: Some(metadata),
        model: Some(body.model),
        effort: body
            .effort
            .as_deref()
            .map(parse_reasoning_effort)
            .transpose()?,
        ..Default::default()
    };
    let result = state
        .requests
        .request_turn_start(
            ClientRequest::TurnStart {
                request_id: RequestId::String(format!("turn-start-{}", body.run_id)),
                params,
            },
            codex_app_server::host_runtime::AppServerTurnStartOptions::new(body.run_id.to_string()),
        )
        .await
        .map_err(|_| RuntimeError::unavailable("agent_kernel_unavailable"))?
        .map_err(|_| RuntimeError::bad_request("agent_turn_start_rejected"))?;
    let response: TurnStartResponse = serde_json::from_value(result)
        .map_err(|_| RuntimeError::internal("invalid_agent_turn_start_response"))?;
    if response.turn.id != body.run_id.to_string() {
        return Err(RuntimeError::internal("agent_turn_id_mismatch"));
    }
    Ok(Json(response))
}

fn validate_start_turn_request(body: &StartTurnRequest) -> Result<(), RuntimeError> {
    if body.message.is_empty()
        || body.message.len() > 1_000_000
        || body.model.is_empty()
        || body.model.len() > 255
        || body.capability_revision < 1
        || body.lease_signature.len() != 67
        || !body.lease_signature.starts_with("v1:")
        || !body.lease_signature[3..]
            .bytes()
            .all(|value| value.is_ascii_hexdigit() && !value.is_ascii_uppercase())
    {
        return Err(RuntimeError::bad_request("invalid_turn_start_request"));
    }
    Ok(())
}

fn parse_reasoning_effort(value: &str) -> Result<ReasoningEffort, RuntimeError> {
    match value {
        "none" => Ok(ReasoningEffort::None),
        "minimal" => Ok(ReasoningEffort::Minimal),
        "low" => Ok(ReasoningEffort::Low),
        "medium" => Ok(ReasoningEffort::Medium),
        "high" => Ok(ReasoningEffort::High),
        "xhigh" => Ok(ReasoningEffort::XHigh),
        "max" => Ok(ReasoningEffort::Max),
        "ultra" => Ok(ReasoningEffort::Ultra),
        _ => Err(RuntimeError::bad_request("invalid_reasoning_effort")),
    }
}

fn runtime_scope_sha256(tenant_id: &str, user_id: &str, session_id: &str) -> String {
    let mut digest = Sha256::new();
    for value in [tenant_id, user_id, session_id] {
        let bytes = value.as_bytes();
        digest.update((bytes.len() as u64).to_be_bytes());
        digest.update(bytes);
    }
    let output = digest.finalize();
    format!("{output:x}")
}

#[derive(Deserialize)]
struct EventsQuery {
    #[serde(default)]
    after_sequence: i64,
    #[serde(default = "default_event_limit")]
    limit: i64,
}

fn default_event_limit() -> i64 {
    200
}

async fn events(
    State(state): State<RuntimeHttpState>,
    Path(thread_id): Path<String>,
    Query(query): Query<EventsQuery>,
    headers: HeaderMap,
) -> Result<Response, RuntimeError> {
    authorize(&headers, &state.internal_token)?;
    let root_thread_id = ThreadId::from_string(&thread_id)
        .map_err(|_| RuntimeError::bad_request("invalid_thread_id"))?;
    let identity = PlatformThreadIdentity::new(
        root_thread_id,
        required_header(&headers, TENANT_HEADER)?,
        required_header(&headers, USER_HEADER)?,
        required_header(&headers, SESSION_HEADER)?,
    );
    state
        .store
        .verify_root_identity(&identity)
        .await
        .map_err(|_| RuntimeError::not_found("thread_not_found"))?;
    let mut receiver = state.events.subscribe();
    let initial = state
        .store
        .read_v1_events_after(root_thread_id, query.after_sequence, query.limit)
        .await
        .map_err(RuntimeError::from_store)?;
    let store = Arc::clone(&state.store);
    let stream = async_stream::stream! {
        let mut cursor = query.after_sequence;
        for event in initial {
            cursor = event.sequence;
            if let Some(sse) = sse_event(&event) {
                yield Ok::<Event, Infallible>(sse);
            }
        }
        loop {
            match receiver.recv().await {
                Ok(message) if message.root_thread_id == root_thread_id => {
                    if message.event.sequence > cursor {
                        cursor = message.event.sequence;
                        if let Some(sse) = sse_event(&message.event) {
                            yield Ok(sse);
                        }
                    }
                }
                Ok(_) => {}
                Err(broadcast::error::RecvError::Lagged(_)) => {
                    match store.read_v1_events_after(root_thread_id, cursor, 1_000).await {
                        Ok(backlog) => {
                            for event in backlog {
                                cursor = event.sequence;
                                if let Some(sse) = sse_event(&event) {
                                    yield Ok(sse);
                                }
                            }
                        }
                        Err(_) => break,
                    }
                }
                Err(broadcast::error::RecvError::Closed) => break,
            }
        }
    };
    let mut response = Sse::new(stream)
        .keep_alive(KeepAlive::default())
        .into_response();
    response
        .headers_mut()
        .insert("cache-control", HeaderValue::from_static("no-cache"));
    response
        .headers_mut()
        .insert("x-accel-buffering", HeaderValue::from_static("no"));
    Ok(response)
}

fn sse_event(event: &SequencedAssistantTurnEventV1) -> Option<Event> {
    let data = serde_json::to_string(event).ok()?;
    Some(
        Event::default()
            .id(event.sequence.to_string())
            .event(&event.event.event_type)
            .data(data),
    )
}

async fn get_approval(
    State(state): State<RuntimeHttpState>,
    Path(approval_id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Json<crate::approval_control::ApprovalSummary>, RuntimeError> {
    authorize(&headers, &state.internal_token)?;
    let tenant_id = required_header(&headers, TENANT_HEADER)?;
    let user_id = required_header(&headers, USER_HEADER)?;
    let session_id = required_header(&headers, SESSION_HEADER)?;
    state
        .approvals
        .summary(approval_id, &tenant_id, &user_id, &session_id, &state.store)
        .await
        .map(Json)
        .map_err(|_| RuntimeError::not_found("approval_not_found"))
}

async fn decide_approval(
    State(state): State<RuntimeHttpState>,
    Path(approval_id): Path<Uuid>,
    headers: HeaderMap,
    Json(body): Json<ApprovalDecisionRequest>,
) -> Result<Json<serde_json::Value>, RuntimeError> {
    authorize(&headers, &state.internal_token)?;
    let tenant_id = required_header(&headers, TENANT_HEADER)?;
    let user_id = required_header(&headers, USER_HEADER)?;
    let session_id = required_header(&headers, SESSION_HEADER)?;
    let projection = state
        .approvals
        .decide(
            approval_id,
            body.decision,
            &tenant_id,
            &user_id,
            &session_id,
            body.reason.as_deref(),
            &state.store,
            &state.requests,
        )
        .await
        .map_err(|_| RuntimeError::bad_request("approval_not_eligible"))?;
    let _ = state.events.send(RuntimeBroadcastEvent {
        root_thread_id: projection.root_thread_id,
        event: projection.event,
    });
    Ok(Json(json!({
        "approval_id": approval_id,
        "status": projection.status,
    })))
}

#[allow(clippy::too_many_arguments)]
async fn route_kernel_events(
    mut kernel: AgentKernel,
    store: Arc<PostgresThreadStore>,
    approvals: ApprovalBroker,
    events: broadcast::Sender<RuntimeBroadcastEvent>,
    kernel_ready: Arc<AtomicBool>,
    readonly_by_turn: Arc<Mutex<HashMap<String, ReadonlyTurnBinding>>>,
    turn_cancellations: Arc<Mutex<HashMap<String, CancellationToken>>>,
    capability_client: reqwest::Client,
    mut shutdown_rx: oneshot::Receiver<()>,
) {
    let request_handle = kernel.request_handle();
    let dynamic_tool_slots = Arc::new(Semaphore::new(16));
    let runtime_cancel = CancellationToken::new();
    let mut text_projection = TextProjectionState::default();
    loop {
        tokio::select! {
            _ = &mut shutdown_rx => {
                runtime_cancel.cancel();
                break;
            },
            event = kernel.next_event() => {
                let Some(event) = event else {
                    runtime_cancel.cancel();
                    break;
                };
                match event {
                    InProcessServerEvent::ServerNotification(notification) => {
                        if let codex_app_server_protocol::ServerNotification::TurnCompleted(
                            completed,
                        ) = notification.as_ref()
                        {
                            let token = turn_cancellations
                                .lock()
                                .unwrap_or_else(std::sync::PoisonError::into_inner)
                                .remove(&completed.turn.id);
                            if let Some(token) = token {
                                token.cancel();
                            }
                        }
                        persist_projected_events(
                            notification.as_ref(),
                            &store,
                            &events,
                            &readonly_by_turn,
                            &mut text_projection,
                        )
                        .await;
                    }
                    InProcessServerEvent::ServerRequest(request) => {
                        if let codex_app_server_protocol::ServerRequest::DynamicToolCall {
                            request_id,
                            params,
                        } = request.as_ref()
                        {
                            let Some(permit) = dynamic_tool_slots.clone().try_acquire_owned().ok()
                            else {
                                let _ = request_handle.reject_server_request(
                                    request_id.clone(),
                                    JSONRPCErrorError {
                                        code: -32001,
                                        message: "read-only capability concurrency limit reached".to_string(),
                                        data: None,
                                    },
                                );
                                continue;
                            };
                            let request_handle = request_handle.clone();
                            let store = Arc::clone(&store);
                            let readonly_by_turn = Arc::clone(&readonly_by_turn);
                            let capability_client = capability_client.clone();
                            let approvals = approvals.clone();
                            let events = events.clone();
                            let request_id = request_id.clone();
                            let params = params.clone();
                            let cancel = turn_cancellations
                                .lock()
                                .unwrap_or_else(std::sync::PoisonError::into_inner)
                                .entry(params.turn_id.clone())
                                .or_insert_with(|| runtime_cancel.child_token())
                                .clone();
                            tokio::spawn(async move {
                                let _permit = permit;
                                let result = handle_dynamic_tool_call(
                                    &params,
                                    &store,
                                    &readonly_by_turn,
                                    &capability_client,
                                    &approvals,
                                    &events,
                                    &cancel,
                                )
                                .await;
                                match result {
                                    Ok(value) => {
                                        let _ = request_handle
                                            .resolve_server_request_async(request_id, value)
                                            .await;
                                    }
                                    Err(_) => {
                                        let _ = request_handle.reject_server_request_async(
                                            request_id,
                                            JSONRPCErrorError {
                                                code: -32001,
                                                message: "read-only capability invocation failed".to_string(),
                                                data: None,
                                            },
                                        )
                                        .await;
                                    }
                                }
                            });
                            continue;
                        }
                        match approvals.capture_server_request(request.as_ref(), &store).await {
                            Ok(Some(approval_id)) => {
                                persist_approval_required(request.as_ref(), approval_id, &store, &events).await;
                            }
                            Ok(None) | Err(_) => {
                                let _ = kernel
                                    .reject_server_request(
                                        request.id().clone(),
                                        JSONRPCErrorError {
                                            code: -32001,
                                            message: "platform approval is unavailable or unsupported".to_string(),
                                            data: None,
                                        },
                                    )
                                    .await;
                            }
                        }
                    }
                    InProcessServerEvent::Lagged { skipped } => {
                        warn!(skipped, "Agent in-process event consumer lagged");
                    }
                }
            }
        }
    }
    kernel_ready.store(false, Ordering::Release);
    let _ = kernel.shutdown().await;
}

async fn handle_dynamic_tool_call(
    params: &codex_app_server_protocol::DynamicToolCallParams,
    store: &PostgresThreadStore,
    readonly_by_turn: &Arc<Mutex<HashMap<String, ReadonlyTurnBinding>>>,
    capability_client: &reqwest::Client,
    approvals: &ApprovalBroker,
    events: &broadcast::Sender<RuntimeBroadcastEvent>,
    cancel: &CancellationToken,
) -> Result<serde_json::Value, String> {
    let thread_id = ThreadId::from_string(&params.thread_id)
        .map_err(|_| "dynamic_tool_thread_invalid".to_string())?;
    let identity = store
        .identity_for_kernel_thread(thread_id)
        .await
        .map_err(|_| "dynamic_tool_scope_unavailable".to_string())?;
    let cached_binding = readonly_by_turn
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .get(&params.turn_id)
        .cloned();
    // Re-check the durable lease on every call. The in-memory entry is only a
    // consistency witness; using it as authority would allow a revoked lease
    // or completed run to invoke the capability plane.
    let binding = load_readonly_turn_binding(store, &identity, &params.turn_id).await?;
    if let Some(cached_binding) = cached_binding
        && (cached_binding.snapshot_id != binding.snapshot_id
            || cached_binding.capability_revision != binding.capability_revision)
    {
        return Err("dynamic_tool_binding_changed".to_string());
    }
    let (capability_allowlist, expected_tool, descriptor, effect) =
        crate::readonly_capabilities::resolve_dynamic_capability_descriptor(
            &binding.payload,
            params.namespace.as_deref(),
            &params.tool,
        )
        .map_err(|error| error.to_string())?;
    let bound_dataset_ids = binding
        .payload
        .get("items")
        .and_then(serde_json::Value::as_array)
        .into_iter()
        .flatten()
        .filter(|item| item.get("kind").and_then(serde_json::Value::as_str) == Some("knowledge"))
        .filter_map(|item| item.get("payload"))
        .filter_map(|payload| payload.get("dataset_id"))
        .filter_map(serde_json::Value::as_str)
        .map(str::to_string)
        .collect::<Vec<_>>();
    let capability_plane_url = std::env::var("AI_PLATFORM_CAPABILITY_PLANE_URL")
        .unwrap_or_else(|_| "http://gateway:8080/internal/v2/agent-capabilities".to_string());
    let mut digest = Sha256::new();
    digest.update(
        serde_json::to_vec(&params.arguments).map_err(|_| "dynamic_tool_arguments_invalid")?,
    );
    let arguments_sha256 = format!("{:x}", digest.finalize());
    store
        .append_platform_lifecycle_event(PlatformLifecycleEvent {
            kernel_thread_id: thread_id,
            turn_id: params.turn_id.clone(),
            item_id: Some(params.call_id.clone()),
            event_key: format!("tool-use/{}/{}", params.turn_id, params.call_id),
            item_type: "tool_use".to_string(),
            status: if effect == CapabilityEffect::Read {
                "dispatched".to_string()
            } else {
                "awaiting_approval".to_string()
            },
            payload: serde_json::json!({
                "schema_version": "agent-runtime-tool-lifecycle/v1",
                "turn_id": params.turn_id,
                "tool_call_id": params.call_id,
                "tool_name": params.tool,
                "arguments_sha256": arguments_sha256,
                "lifecycle": if effect == CapabilityEffect::Read {
                    "dispatched"
                } else {
                    "awaiting_approval"
                },
                "effect": match effect {
                    CapabilityEffect::Read => "read",
                    CapabilityEffect::Write => "write",
                    CapabilityEffect::Unknown => "unknown",
                },
                "dispatch_state": if effect == CapabilityEffect::Read {
                    "dispatched"
                } else {
                    "awaiting_approval"
                },
            }),
        })
        .await
        .map_err(|_| "dynamic_tool_dispatch_receipt_failed")?;
    let worker_enabled = capability_worker_enabled(
        std::env::var("AI_PLATFORM_CAPABILITY_WORKER_ENABLED")
            .ok()
            .as_deref(),
    );
    let worker_writes_enabled = capability_worker_enabled(
        std::env::var("AI_PLATFORM_CAPABILITY_WORKER_WRITES_ENABLED")
            .ok()
            .as_deref(),
    );
    let worker_url = std::env::var("AI_PLATFORM_CAPABILITY_WORKER_URL").ok();
    let lease_secret = std::env::var("AI_PLATFORM_CAPABILITY_LEASE_SIGNING_SECRET").ok();
    if effect != CapabilityEffect::Read
        && (!worker_enabled
            || !worker_writes_enabled
            || worker_url
                .as_deref()
                .is_none_or(|value| value.trim().is_empty())
            || lease_secret
                .as_deref()
                .is_none_or(|value| value.trim().is_empty()))
    {
        persist_dynamic_terminal_receipt(
            params,
            thread_id,
            store,
            "failed",
            "capability_worker_not_ready",
        )
        .await
        .map_err(|_| "dynamic_tool_terminal_receipt_failed".to_string())?;
        return Ok(structured_capability_response(
            false,
            "capability worker is not ready for this capability",
        ));
    }
    let mut approval_id = None;
    if effect != CapabilityEffect::Read {
        let (id, receiver) = approvals
            .await_dynamic_tool(params, &expected_tool.id, &identity, store)
            .await?;
        approval_id = Some(id);
        if let Err(error) =
            persist_dynamic_approval_required(params, id, effect, &identity, store, events).await
        {
            approvals
                .cancel_dynamic(
                    id,
                    &identity.tenant_id,
                    &identity.user_id,
                    &identity.session_id,
                    "approval_receipt_failed",
                    store,
                )
                .await;
            persist_dynamic_terminal_receipt(
                params,
                thread_id,
                store,
                "failed",
                "approval_receipt_failed",
            )
            .await
            .map_err(|_| "dynamic_tool_terminal_receipt_failed".to_string())?;
            return Ok(structured_capability_response(false, &error));
        }
        let decision = tokio::select! {
            decision = tokio::time::timeout(Duration::from_secs(600), receiver) => {
                decision.ok().and_then(Result::ok)
            }
            () = cancel.cancelled() => None,
        };
        let Some(decision) = decision else {
            approvals
                .cancel_dynamic(
                    id,
                    &identity.tenant_id,
                    &identity.user_id,
                    &identity.session_id,
                    if cancel.is_cancelled() {
                        "runtime_cancelled"
                    } else {
                        "approval_expired"
                    },
                    store,
                )
                .await;
            persist_dynamic_terminal_receipt(
                params,
                thread_id,
                store,
                if cancel.is_cancelled() {
                    "cancelled"
                } else {
                    "failed"
                },
                if cancel.is_cancelled() {
                    "capability_execution_cancelled"
                } else {
                    "capability_execution_timeout"
                },
            )
            .await
            .map_err(|_| "dynamic_tool_terminal_receipt_failed".to_string())?;
            return Ok(structured_capability_response(
                false,
                if cancel.is_cancelled() {
                    "capability execution cancelled"
                } else {
                    "capability approval expired"
                },
            ));
        };
        if !decision.approved {
            persist_dynamic_terminal_receipt(
                params,
                thread_id,
                store,
                "failed",
                "capability_execution_rejected",
            )
            .await
            .map_err(|_| "dynamic_tool_terminal_receipt_failed".to_string())?;
            return Ok(structured_capability_response(
                false,
                decision
                    .reason
                    .as_deref()
                    .unwrap_or("capability approval rejected"),
            ));
        }
        store
            .append_platform_lifecycle_event(PlatformLifecycleEvent {
                kernel_thread_id: thread_id,
                turn_id: params.turn_id.clone(),
                item_id: Some(params.call_id.clone()),
                event_key: format!("tool-dispatch/{}/{}", params.turn_id, params.call_id),
                item_type: "tool_use".to_string(),
                status: "dispatched".to_string(),
                payload: serde_json::json!({
                    "schema_version": "agent-runtime-tool-lifecycle/v1",
                    "turn_id": params.turn_id,
                    "tool_call_id": params.call_id,
                    "tool_name": params.tool,
                    "approval_id": id,
                    "lifecycle": "dispatched",
                    "dispatch_state": "dispatched",
                    "effect": "write",
                }),
            })
            .await
            .map_err(|_| "dynamic_tool_dispatch_receipt_failed")?;
    }
    let internal_token = std::env::var("AI_PLATFORM_INTERNAL_TOKEN").unwrap_or_default();
    let approval_id_string = approval_id.map(|id| id.to_string());
    let result: Result<CapabilityExecutionOutcome, String> = if worker_enabled {
        let worker_url = worker_url
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| "capability_worker_url_missing".to_string());
        let lease_secret = lease_secret
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| "capability_worker_lease_secret_missing".to_string());
        match (worker_url, lease_secret) {
            (Ok(worker_url), Ok(lease_secret)) => {
                let worker = CapabilityWorkerClient::new(
                    capability_client.clone(),
                    &worker_url,
                    internal_token.clone(),
                )
                .map_err(|error| error.code().to_string());
                match worker {
                    Ok(worker) => execute_capability(
                        &worker,
                        &identity,
                        &ReadonlyCapabilityBinding {
                            capability_revision: binding.capability_revision as u64,
                            allowlist: capability_allowlist.clone(),
                            expected_tool: expected_tool.clone(),
                            descriptor: descriptor.clone(),
                        },
                        params,
                        lease_secret.as_bytes(),
                        effect,
                        approval_id_string.as_deref(),
                        cancel,
                    )
                    .await
                    .map_err(|error| error.code().to_string()),
                    Err(error) => Err(error),
                }
            }
            (Err(error), _) | (_, Err(error)) => Err(error),
        }
    } else if effect == CapabilityEffect::Read {
        crate::capability_plane::invoke_dynamic_tool(
            capability_client,
            params,
            &identity,
            &capability_plane_url,
            &internal_token,
            binding.capability_revision,
            &binding.snapshot_id,
            &bound_dataset_ids,
            &capability_allowlist,
            &expected_tool,
        )
        .await
        .map(|response| CapabilityExecutionOutcome {
            status: if response.get("success").and_then(serde_json::Value::as_bool) == Some(true) {
                CapabilityExecutionStatus::Succeeded
            } else {
                CapabilityExecutionStatus::Failed
            },
            response,
        })
    } else {
        Err("capability_worker_required_for_write".to_string())
    };
    let (status, detail) = match &result {
        Ok(outcome) => (
            capability_status_name(outcome.status),
            capability_result_detail(outcome),
        ),
        Err(error) => ("failed", error.as_str()),
    };
    let receipt = store
        .append_platform_lifecycle_event(PlatformLifecycleEvent {
            kernel_thread_id: thread_id,
            turn_id: params.turn_id.clone(),
            item_id: Some(params.call_id.clone()),
            event_key: format!("tool-result/{}/{}", params.turn_id, params.call_id),
            item_type: "tool_result".to_string(),
            status: status.to_string(),
            payload: serde_json::json!({
                "schema_version": "agent-runtime-tool-lifecycle/v1",
                "turn_id": params.turn_id,
                "tool_call_id": params.call_id,
                "lifecycle": "terminal",
                "result_status": status,
                "detail": detail,
            }),
        })
        .await;
    if receipt.is_err() {
        return Err("dynamic_tool_result_receipt_failed".to_string());
    }
    result.map(|outcome| outcome.response)
}

fn capability_worker_enabled(value: Option<&str>) -> bool {
    value.is_some_and(|value| value.eq_ignore_ascii_case("true"))
}

fn capability_status_name(status: CapabilityExecutionStatus) -> &'static str {
    match status {
        CapabilityExecutionStatus::Succeeded => "succeeded",
        CapabilityExecutionStatus::Failed => "failed",
        CapabilityExecutionStatus::Cancelled => "cancelled",
        CapabilityExecutionStatus::Timeout => "timeout",
        CapabilityExecutionStatus::SideEffectUnknown => "side_effect_unknown",
        _ => "failed",
    }
}

fn capability_result_detail(outcome: &CapabilityExecutionOutcome) -> &'static str {
    match outcome.status {
        CapabilityExecutionStatus::Succeeded => "completed",
        CapabilityExecutionStatus::Failed => "capability_execution_failed",
        CapabilityExecutionStatus::Cancelled => "capability_execution_cancelled",
        CapabilityExecutionStatus::Timeout => "capability_execution_timeout",
        CapabilityExecutionStatus::SideEffectUnknown => "capability_execution_side_effect_unknown",
        _ => "capability_execution_failed",
    }
}

async fn load_readonly_turn_binding(
    store: &PostgresThreadStore,
    identity: &PlatformThreadIdentity,
    turn_id: &str,
) -> Result<ReadonlyTurnBinding, String> {
    let turn_uuid =
        Uuid::parse_str(turn_id).map_err(|_| "dynamic_tool_turn_invalid".to_string())?;
    let row = sqlx::query(
        r#"
        SELECT snapshot.snapshot_id,
               snapshot.capability_revision,
               snapshot.snapshot
          FROM assistant_runtime_snapshots AS snapshot
          JOIN assistant_runtime_model_leases AS lease
            ON lease.snapshot_id = snapshot.snapshot_id
           AND lease.run_id = snapshot.run_id
           AND lease.tenant_id = snapshot.tenant_id
           AND lease.user_id = snapshot.user_id
           AND lease.session_id = snapshot.session_id
         JOIN assistant_runs AS run ON run.run_id = snapshot.run_id
         WHERE snapshot.run_id = $1
           AND snapshot.runtime_thread_id = $2
           AND snapshot.tenant_id = $3
           AND snapshot.user_id = $4
           AND snapshot.session_id = $5
           AND lease.status = 'active'
           AND lease.expires_at > NOW()
           AND run.status = 'running'
           AND run.engine = 'agent_runtime'
           AND NOT EXISTS (
               SELECT 1 FROM assistant_runtime_snapshot_revocations AS revoked
                WHERE revoked.snapshot_id = snapshot.snapshot_id
           )
         ORDER BY snapshot.created_at DESC
         LIMIT 1
        "#,
    )
    .bind(turn_uuid)
    .bind(
        identity
            .runtime_thread_id
            .to_string()
            .parse::<Uuid>()
            .map_err(|_| "dynamic_tool_thread_invalid")?,
    )
    .bind(&identity.tenant_id)
    .bind(&identity.user_id)
    .bind(&identity.session_id)
    .fetch_optional(&store.pool)
    .await
    .map_err(|_| "dynamic_tool_binding_unavailable".to_string())?
    .ok_or_else(|| "dynamic_tool_binding_missing".to_string())?;
    let snapshot: serde_json::Value = row
        .try_get("snapshot")
        .map_err(|_| "dynamic_tool_snapshot_invalid".to_string())?;
    let payload = snapshot
        .get("readonly_capabilities")
        .cloned()
        .ok_or_else(|| "dynamic_tool_binding_missing".to_string())?;
    Ok(ReadonlyTurnBinding {
        snapshot_id: row
            .try_get::<Uuid, _>("snapshot_id")
            .map_err(|_| "dynamic_tool_snapshot_invalid".to_string())?
            .to_string(),
        capability_revision: row
            .try_get("capability_revision")
            .map_err(|_| "dynamic_tool_snapshot_invalid".to_string())?,
        payload,
        created_at: Instant::now(),
    })
}

async fn persist_approval_required(
    request: &codex_app_server_protocol::ServerRequest,
    approval_id: Uuid,
    store: &PostgresThreadStore,
    events: &broadcast::Sender<RuntimeBroadcastEvent>,
) {
    let Ok(raw) = serde_json::to_value(request) else {
        return;
    };
    let Some(params) = raw.get("params") else {
        return;
    };
    let Some(thread_id) = params
        .get("threadId")
        .or_else(|| params.get("conversationId"))
        .and_then(serde_json::Value::as_str)
        .and_then(|value| ThreadId::from_string(value).ok())
    else {
        return;
    };
    let turn_id = params
        .get("turnId")
        .or_else(|| params.get("turn_id"))
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    let Ok(identity) = store.identity_for_kernel_thread(thread_id).await else {
        return;
    };
    let event = crate::AssistantTurnEventV1::new(
        "approval_required",
        json!({
            "run_id": turn_id,
            "session_id": identity.session_id,
            "thread_id": identity.runtime_thread_id.to_string(),
            "approval_id": approval_id,
            "tool_call_id": params.get("itemId").or_else(|| params.get("callId")),
            "status": "approval_required",
            "approval_required": true,
        }),
    );
    let event_key = format!("compat/approval/{approval_id}/required");
    match store
        .append_v1_event(thread_id, approval_id, &event_key, &event)
        .await
    {
        Ok(sequence) => {
            let _ = events.send(RuntimeBroadcastEvent {
                root_thread_id: identity.runtime_thread_id,
                event: SequencedAssistantTurnEventV1 { sequence, event },
            });
        }
        Err(error) => warn!(%error, "failed to project approval_required"),
    }
}

async fn persist_dynamic_approval_required(
    params: &codex_app_server_protocol::DynamicToolCallParams,
    approval_id: Uuid,
    effect: CapabilityEffect,
    identity: &PlatformThreadIdentity,
    store: &PostgresThreadStore,
    events: &broadcast::Sender<RuntimeBroadcastEvent>,
) -> Result<(), String> {
    let arguments_hash = ai_platform_capability_contract::canonical_json_hash(&params.arguments)
        .unwrap_or_else(|_| "sha256:invalid".to_string());
    let effect = match effect {
        CapabilityEffect::Read => "read",
        CapabilityEffect::Write => "write",
        CapabilityEffect::Unknown => "unknown",
    };
    let event = crate::AssistantTurnEventV1::new(
        "approval_required",
        json!({
            "run_id": params.turn_id,
            "session_id": identity.session_id,
            "thread_id": identity.runtime_thread_id.to_string(),
            "approval_id": approval_id,
            "tool_id": params.call_id,
            "tool_call_id": params.call_id,
            "tool_name": params.tool,
            "arguments_hash": arguments_hash,
            "effect": effect,
            "status": "approval_required",
            "approval_required": true,
        }),
    );
    let event_key = format!("compat/approval/{approval_id}/required");
    match store
        .append_v1_event(identity.runtime_thread_id, approval_id, &event_key, &event)
        .await
    {
        Ok(sequence) => {
            let _ = events.send(RuntimeBroadcastEvent {
                root_thread_id: identity.runtime_thread_id,
                event: SequencedAssistantTurnEventV1 { sequence, event },
            });
            Ok(())
        }
        Err(error) => {
            warn!(%error, "failed to project dynamic approval_required");
            Err("approval_receipt_failed".to_string())
        }
    }
}

fn structured_capability_response(success: bool, message: &str) -> serde_json::Value {
    crate::capability_execution::dynamic_tool_text_response(success, message)
}

async fn persist_dynamic_terminal_receipt(
    params: &codex_app_server_protocol::DynamicToolCallParams,
    thread_id: ThreadId,
    store: &PostgresThreadStore,
    status: &str,
    detail: &str,
) -> Result<(), ()> {
    store
        .append_platform_lifecycle_event(PlatformLifecycleEvent {
            kernel_thread_id: thread_id,
            turn_id: params.turn_id.clone(),
            item_id: Some(params.call_id.clone()),
            event_key: format!("tool-result/{}/{}", params.turn_id, params.call_id),
            item_type: "tool_result".to_string(),
            status: status.to_string(),
            payload: serde_json::json!({
                "schema_version": "agent-runtime-tool-lifecycle/v1",
                "turn_id": params.turn_id,
                "tool_call_id": params.call_id,
                "lifecycle": "terminal",
                "result_status": status,
                "detail": detail,
            }),
        })
        .await
        .map(|_| ())
        .map_err(|_| ())
}

async fn persist_projected_events(
    notification: &codex_app_server_protocol::ServerNotification,
    store: &PostgresThreadStore,
    events: &broadcast::Sender<RuntimeBroadcastEvent>,
    readonly_by_turn: &Arc<Mutex<HashMap<String, ReadonlyTurnBinding>>>,
    text_projection: &mut TextProjectionState,
) {
    let Some(kernel_thread_id) = server_notification_thread_id(notification)
        .and_then(|value| ThreadId::from_string(value).ok())
    else {
        return;
    };
    let Ok(identity) = store.identity_for_kernel_thread(kernel_thread_id).await else {
        return;
    };
    let context = V1ProjectionContext {
        tenant_id: identity.tenant_id.clone(),
        user_id: identity.user_id.clone(),
        session_id: identity.session_id.clone(),
    };
    let fallback = text_projection.fallback_event(notification, &context);
    if let codex_app_server_protocol::ServerNotification::TurnCompleted(completed) = notification {
        readonly_by_turn
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .remove(&completed.turn.id);
        let recovered = match store
            .admit_turn_terminal(kernel_thread_id, &completed.turn.id)
            .await
        {
            Ok(recovered) => recovered,
            Err(error) => {
                warn!(%error, turn_id = %completed.turn.id, "terminal admission failed");
                // Persistence is the authority for normal event sequencing,
                // but a failed admission must not leave the connected client
                // waiting forever. Broadcast one explicitly non-durable,
                // failed terminal at the maximum sequence. Eval rejects any
                // missing tool receipts; the Gateway also marks the run failed.
                let event = terminal_admission_failure_event(completed, &context);
                let _ = events.send(RuntimeBroadcastEvent {
                    root_thread_id: identity.runtime_thread_id,
                    event: SequencedAssistantTurnEventV1 {
                        sequence: i64::MAX,
                        event,
                    },
                });
                return;
            }
        };
        for call in recovered {
            let data = json!({
                "run_id": completed.turn.id,
                "session_id": context.session_id,
                "thread_id": completed.thread_id,
                "tool_call_id": call.call_id.clone(),
                "status": call.result_status,
                "success": false,
                "detail": call.detail,
                "recovery": "terminal_admission",
            });
            for event_type in ["tool_call_result", "tool_call_end"] {
                let event = crate::AssistantTurnEventV1::new(event_type, data.clone());
                let event_key = format!(
                    "compat/recovery/{}/{}/{}",
                    completed.turn.id, call.call_id, event_type
                );
                let event_id = stable_event_id(&event_key);
                match store
                    .append_v1_event(kernel_thread_id, event_id, &event_key, &event)
                    .await
                {
                    Ok(sequence) => {
                        let _ = events.send(RuntimeBroadcastEvent {
                            root_thread_id: identity.runtime_thread_id,
                            event: SequencedAssistantTurnEventV1 { sequence, event },
                        });
                    }
                    Err(error) => warn!(%error, "failed to persist recovered tool result"),
                }
            }
        }
    }
    let projected = fallback
        .into_iter()
        .chain(project_server_notification(notification, &context))
        .collect();
    for event in text_projection.normalize_subagent_events(projected) {
        let event_id = Uuid::now_v7();
        let event_key = format!("compat/v1/{event_id}");
        match store
            .append_v1_event(kernel_thread_id, event_id, &event_key, &event)
            .await
        {
            Ok(sequence) => {
                let _ = events.send(RuntimeBroadcastEvent {
                    root_thread_id: identity.runtime_thread_id,
                    event: SequencedAssistantTurnEventV1 { sequence, event },
                });
            }
            Err(error) => warn!(%error, "failed to persist projected Agent event"),
        }
    }
}

fn stable_event_id(event_key: &str) -> Uuid {
    let mut digest = Sha256::new();
    digest.update(event_key.as_bytes());
    let digest = digest.finalize();
    let mut bytes = [0u8; 16];
    bytes.copy_from_slice(&digest[..16]);
    Uuid::from_bytes(bytes)
}

fn terminal_admission_failure_event(
    completed: &codex_app_server_protocol::TurnCompletedNotification,
    context: &V1ProjectionContext,
) -> crate::AssistantTurnEventV1 {
    crate::AssistantTurnEventV1::new(
        "run_error",
        json!({
            "run_id": completed.turn.id,
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "session_id": context.session_id,
            "thread_id": completed.thread_id,
            "status": "failed",
            "exit": "failed",
            "durable": false,
            "terminal_envelope": {
                "schema_version": crate::ASSISTANT_TURN_CONTRACT_V1,
                "run_id": completed.turn.id,
                "tenant_id": context.tenant_id,
                "user_id": context.user_id,
                "session_id": context.session_id,
                "thread_id": completed.thread_id,
                "status": "failed",
                "exit_reason": "terminal_admission_failed",
            },
        }),
    )
}

#[cfg(test)]
mod turn_request_tests {
    use super::*;
    use codex_app_server_protocol::AgentMessageDeltaNotification;
    use codex_app_server_protocol::ItemCompletedNotification;
    use codex_app_server_protocol::ServerNotification;
    use codex_app_server_protocol::ThreadItem;
    use codex_app_server_protocol::Turn;
    use codex_app_server_protocol::TurnCompletedNotification;
    use codex_app_server_protocol::TurnItemsView;
    use codex_app_server_protocol::TurnStatus;

    fn request(signature: String) -> StartTurnRequest {
        StartTurnRequest {
            run_id: Uuid::nil(),
            snapshot_id: Uuid::nil(),
            lease_id: Uuid::nil(),
            lease_signature: signature,
            message: "hello".to_string(),
            model: "qwen3.7-plus".to_string(),
            effort: Some("minimal".to_string()),
            capability_revision: 1,
            readonly: None,
            platform_config: None,
        }
    }

    #[test]
    fn signed_turn_request_contract_is_bounded() {
        let valid = request(format!("v1:{}", "a".repeat(64)));
        assert!(validate_start_turn_request(&valid).is_ok());
        assert!(parse_reasoning_effort("minimal").is_ok());
        assert!(parse_reasoning_effort("invented").is_err());
        assert_eq!(runtime_scope_sha256("tenant", "user", "session").len(), 64);
        assert_ne!(
            runtime_scope_sha256("tenant", "user", "session"),
            runtime_scope_sha256("tenant", "users", "ession")
        );

        let uppercase = request(format!("v1:{}", "A".repeat(64)));
        assert!(validate_start_turn_request(&uppercase).is_err());
        let oversized = request(format!("v1:{}", "a".repeat(65)));
        assert!(validate_start_turn_request(&oversized).is_err());
    }

    #[test]
    fn capability_worker_requires_explicit_true_flag() {
        assert!(capability_worker_enabled(Some("true")));
        assert!(capability_worker_enabled(Some("TRUE")));
        assert!(!capability_worker_enabled(None));
        assert!(!capability_worker_enabled(Some("false")));
        assert!(!capability_worker_enabled(Some("1")));
        assert!(!capability_worker_enabled(Some(" true ")));
    }

    #[test]
    fn capability_result_status_does_not_treat_failed_response_as_success() {
        let failed = CapabilityExecutionOutcome {
            response: serde_json::json!({
                "contentItems": [{"type": "inputText", "text": "failed"}],
                "success": false
            }),
            status: CapabilityExecutionStatus::Failed,
        };
        assert_eq!(capability_status_name(failed.status), "failed");
        assert_eq!(
            capability_result_detail(&failed),
            "capability_execution_failed"
        );
        assert_ne!(capability_status_name(failed.status), "succeeded");
    }

    #[test]
    fn model_limits_are_profile_driven_and_conflicts_fail_closed() {
        let mut start = ThreadStartParams::default();
        assert!(
            apply_model_limits(&mut start, Some(1_000_000), Some(900_000)).is_ok(),
            "valid model limits"
        );
        let config = start.config.expect("limits should be placed in config");
        assert_eq!(config["model_context_window"], 1_000_000);
        assert_eq!(config["model_auto_compact_token_limit"], 900_000);

        let mut conflicting = ThreadStartParams {
            config: Some(HashMap::from([(
                "model_context_window".to_string(),
                serde_json::json!(272_000),
            )])),
            ..Default::default()
        };
        assert!(apply_model_limits(&mut conflicting, Some(1_000_000), None).is_err());
        assert!(apply_model_limits(&mut ThreadStartParams::default(), None, Some(10)).is_err());
        assert!(
            apply_model_limits(&mut ThreadStartParams::default(), Some(1_000), Some(1_001))
                .is_err()
        );
    }

    #[test]
    fn official_thread_instructions_use_stable_camel_case_fields() {
        let start = ThreadStartParams {
            base_instructions: Some("platform system contract".to_string()),
            developer_instructions: Some("platform developer contract".to_string()),
            ..Default::default()
        };
        let value = serde_json::to_value(start).expect("thread params should serialize");
        assert_eq!(value["baseInstructions"], "platform system contract");
        assert_eq!(
            value["developerInstructions"],
            "platform developer contract"
        );
        assert!(!value.as_object().unwrap().contains_key("base_instructions"));
    }

    #[test]
    fn memory_mode_does_not_enable_unscoped_local_storage() {
        assert!(validate_memory_mode(None).is_ok());
        assert!(validate_memory_mode(Some(ThreadMemoryMode::Disabled)).is_ok());
        assert!(validate_memory_mode(Some(ThreadMemoryMode::Enabled)).is_err());
    }

    #[test]
    fn subagent_projection_deduplicates_starts_and_repairs_late_receiver_identity() {
        let data = json!({
            "run_id": "turn-a",
            "agent_id": "child-a",
            "status": "running",
        });
        let mut state = TextProjectionState::default();
        let first = state.normalize_subagent_events(vec![crate::AssistantTurnEventV1::new(
            "subagent_started",
            data.clone(),
        )]);
        assert_eq!(first.len(), 1);
        let duplicate = state.normalize_subagent_events(vec![crate::AssistantTurnEventV1::new(
            "subagent_started",
            data,
        )]);
        assert!(duplicate.is_empty());
        let finish = state.normalize_subagent_events(vec![crate::AssistantTurnEventV1::new(
            "subagent_finished",
            json!({
                "run_id": "turn-a",
                "agent_id": "child-late",
                "status": "completed",
            }),
        )]);
        assert_eq!(finish.len(), 2);
        assert_eq!(finish[0].event_type, "subagent_started");
        assert_eq!(finish[1].event_type, "subagent_finished");
    }

    #[test]
    fn terminal_admission_failure_is_an_explicit_non_durable_failure() {
        let event = terminal_admission_failure_event(
            &TurnCompletedNotification {
                thread_id: "thread-a".to_string(),
                turn: Turn {
                    id: "turn-a".to_string(),
                    items: Vec::new(),
                    items_view: TurnItemsView::NotLoaded,
                    status: TurnStatus::Interrupted,
                    error: None,
                    started_at: Some(1),
                    completed_at: Some(2),
                    duration_ms: Some(1_000),
                },
            },
            &V1ProjectionContext {
                tenant_id: "tenant-a".to_string(),
                user_id: "user-a".to_string(),
                session_id: "session-a".to_string(),
            },
        );
        assert_eq!(event.event_type, "run_error");
        assert_eq!(event.data["status"], "failed");
        assert_eq!(event.data["durable"], false);
        assert_eq!(
            event.data["terminal_envelope"]["exit_reason"],
            "terminal_admission_failed"
        );
    }

    fn completed_message(item_id: &str, text: &str) -> ServerNotification {
        ServerNotification::ItemCompleted(ItemCompletedNotification {
            item: ThreadItem::AgentMessage {
                id: item_id.to_string(),
                text: text.to_string(),
                phase: None,
                memory_citation: None,
                delivery: None,
            },
            thread_id: "thread-1".to_string(),
            turn_id: "turn-1".to_string(),
            completed_at_ms: 1,
        })
    }

    #[test]
    fn completed_agent_message_falls_back_only_when_no_delta_was_seen() {
        let context = V1ProjectionContext {
            tenant_id: "tenant".to_string(),
            user_id: "user".to_string(),
            session_id: "session".to_string(),
        };
        let mut state = TextProjectionState::default();
        let fallback = state
            .fallback_event(&completed_message("message-1", "hello"), &context)
            .expect("completed item fallback");
        assert_eq!(fallback.event_type, "text_delta");
        assert_eq!(fallback.data["content"], "hello");
        assert!(
            state
                .fallback_event(&completed_message("message-1", "hello"), &context)
                .is_none()
        );

        let delta = ServerNotification::AgentMessageDelta(AgentMessageDeltaNotification {
            thread_id: "thread-1".to_string(),
            turn_id: "turn-1".to_string(),
            item_id: "message-2".to_string(),
            delta: "streamed".to_string(),
        });
        assert!(state.fallback_event(&delta, &context).is_none());
        assert!(
            state
                .fallback_event(&completed_message("message-2", "streamed"), &context)
                .is_none()
        );
    }
}
