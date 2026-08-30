use std::collections::HashMap;
use std::collections::HashSet;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;
use std::time::Duration;
use std::time::Instant;

use axum::Json;
use axum::Router;
use axum::extract::State;
use axum::routing::get;
use axum::routing::post;
use codex_app_server_client::InProcessAppServerRequestHandle;
use codex_protocol::ThreadId;
use serde::Serialize;
use serde_json::json;
use tokio::sync::broadcast;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

use crate::AgentKernel;
use crate::PostgresThreadStore;
use crate::SequencedAssistantTurnEventV1;
use crate::V1ProjectionContext;
use crate::approval_control::ApprovalBroker;

mod approvals;
mod capability_dispatch;
mod events;
mod projection;
mod security;
mod thread_lifecycle;
mod threads;
mod turns;

use self::approvals::decide_approval;
use self::approvals::get_approval;
use self::capability_dispatch::route_kernel_events;
use self::events::events;
use self::security::RuntimeError;
use self::thread_lifecycle::archive_thread;
use self::thread_lifecycle::interrupt_turn;
use self::thread_lifecycle::resume_thread;
use self::thread_lifecycle::unarchive_thread;
use self::threads::cleanup_session;
use self::threads::create_thread;
use self::turns::start_turn;

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
pub(super) struct ReadonlyTurnBinding {
    pub(super) snapshot_id: String,
    pub(super) capability_revision: i64,
    pub(super) payload: serde_json::Value,
    pub(super) created_at: Instant,
}

const READONLY_TURN_TTL: Duration = Duration::from_secs(900);
const READONLY_TURN_MAX_ENTRIES: usize = 1_024;

#[derive(Clone, Debug)]
pub(super) struct RuntimeBroadcastEvent {
    pub(super) root_thread_id: ThreadId,
    pub(super) event: SequencedAssistantTurnEventV1,
}

#[derive(Default)]
pub(super) struct TextProjectionState {
    turns: HashMap<String, TurnTextProjectionState>,
}

#[derive(Default)]
struct TurnTextProjectionState {
    delta_items: HashSet<String>,
    completed_items: HashSet<String>,
    child_agents: HashSet<String>,
}

impl TextProjectionState {
    pub(super) fn normalize_subagent_events(
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

    pub(super) fn fallback_event(
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
