//! Durable approval broker for Agent server requests.

use std::collections::HashMap;
use std::sync::Arc;

use chrono::DateTime;
use chrono::Utc;
use codex_app_server_client::InProcessAppServerRequestHandle;
use codex_app_server_protocol::DynamicToolCallParams;
use codex_app_server_protocol::RequestId;
use codex_app_server_protocol::ServerRequest;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use serde_json::json;
use sha2::Digest;
use sha2::Sha256;
use sqlx::Row;
use tokio::sync::Mutex;
use tokio::sync::oneshot;
use tracing::warn;
use uuid::Uuid;

use crate::AssistantTurnEventV1;
use crate::PostgresThreadStore;
use crate::SequencedAssistantTurnEventV1;
use crate::postgres_store::PlatformLifecycleEvent;

const APPROVAL_TTL_SECONDS: i64 = 600;

#[derive(Clone)]
pub(crate) struct ApprovalBroker {
    pending: Arc<Mutex<HashMap<Uuid, PendingApproval>>>,
}

#[derive(Clone)]
struct PendingApproval {
    destination: ApprovalDestination,
    tenant_id: String,
    user_id: String,
    session_id: String,
    thread_id: Uuid,
    root_thread_id: codex_protocol::ThreadId,
    turn_id: String,
    call_id: String,
}

#[derive(Clone)]
enum ApprovalDestination {
    ServerRequest(RequestId),
    Dynamic(Arc<Mutex<Option<oneshot::Sender<DynamicApprovalDecision>>>>),
}

#[derive(Debug)]
pub(crate) struct DynamicApprovalDecision {
    pub approved: bool,
    pub reason: Option<String>,
}

#[derive(Clone, Copy)]
enum ApprovalKind {
    Command,
    FileChange,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub(crate) struct ApprovalDecisionRequest {
    pub decision: ApprovalDecision,
    pub reason: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ApprovalDecision {
    Approve,
    Reject,
}

#[derive(Debug, Serialize)]
pub(crate) struct ApprovalSummary {
    pub approval_id: Uuid,
    pub status: String,
    pub run_id: Option<Uuid>,
    pub tool_name: String,
    pub arguments_hash: String,
    pub expires_in_seconds: i64,
}

pub(crate) struct ApprovalProjection {
    pub root_thread_id: codex_protocol::ThreadId,
    pub event: SequencedAssistantTurnEventV1,
    pub status: &'static str,
}

impl ApprovalBroker {
    pub(crate) fn new() -> Self {
        Self {
            pending: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Persist one dynamic write/unknown approval before waiting on its
    /// decision. The waiter is process-local; the durable row is the
    /// authority used by the capability worker when it consumes a lease.
    pub(crate) async fn await_dynamic_tool(
        &self,
        params: &DynamicToolCallParams,
        capability_id: &str,
        identity: &crate::PlatformThreadIdentity,
        store: &PostgresThreadStore,
    ) -> Result<(Uuid, oneshot::Receiver<DynamicApprovalDecision>), String> {
        let run_id =
            Uuid::parse_str(&params.turn_id).map_err(|_| "approval_run_invalid".to_string())?;
        if params.call_id.is_empty() || params.tool.is_empty() || !params.arguments.is_object() {
            return Err("approval_call_invalid".to_string());
        }
        let thread_id = Uuid::parse_str(&params.thread_id)
            .map_err(|_| "approval_thread_invalid".to_string())?;
        let approval_id = Uuid::now_v7();
        let arguments = params.arguments.clone();
        sqlx::query(
            "INSERT INTO assistant_tool_approvals (approval_id, tenant_id, user_id, session_id, run_id, tool_call_id, tool_name, arguments, status, expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending',NOW() + ($9 * INTERVAL '1 second'))",
        )
        .bind(approval_id)
        .bind(&identity.tenant_id)
        .bind(&identity.user_id)
        .bind(&identity.session_id)
        .bind(run_id)
        .bind(&params.call_id)
        .bind(capability_id)
        .bind(&arguments)
        .bind(APPROVAL_TTL_SECONDS)
        .execute(&store.pool)
        .await
        .map_err(|_| "approval_persistence_failed".to_string())?;

        let (sender, receiver) = oneshot::channel();
        self.pending.lock().await.insert(
            approval_id,
            PendingApproval {
                destination: ApprovalDestination::Dynamic(Arc::new(Mutex::new(Some(sender)))),
                tenant_id: identity.tenant_id.clone(),
                user_id: identity.user_id.clone(),
                session_id: identity.session_id.clone(),
                thread_id,
                root_thread_id: identity.runtime_thread_id,
                turn_id: params.turn_id.clone(),
                call_id: params.call_id.clone(),
            },
        );
        Ok((approval_id, receiver))
    }

    pub(crate) async fn cancel_dynamic(
        &self,
        approval_id: Uuid,
        tenant_id: &str,
        user_id: &str,
        session_id: &str,
        reason: &str,
        store: &PostgresThreadStore,
    ) {
        let result = sqlx::query(
            "UPDATE assistant_tool_approvals SET status='cancelled', reason=$2, approved_at=NOW() WHERE approval_id=$1 AND tenant_id=$3 AND user_id=$4 AND session_id=$5 AND status='pending'",
        )
        .bind(approval_id)
        .bind(reason)
        .bind(tenant_id)
        .bind(user_id)
        .bind(session_id)
        .execute(&store.pool)
        .await;
        if result.is_ok_and(|result| result.rows_affected() == 1)
            && let Some(pending) = self.pending.lock().await.remove(&approval_id)
            && let ApprovalDestination::Dynamic(waiter) = pending.destination
            && let Some(sender) = waiter.lock().await.take()
        {
            let _ = sender.send(DynamicApprovalDecision {
                approved: false,
                reason: Some(reason.to_string()),
            });
        }
    }

    /// A ServerRequest is held in the App Server process, not in Postgres.
    /// Therefore a Runtime restart cannot safely resume an approval that was
    /// pending in the previous process.  Close those rows at startup instead
    /// of exposing a permanently pending request that can never be consumed.
    pub(crate) async fn reconcile_after_restart(
        &self,
        store: &PostgresThreadStore,
        startup_cutoff: DateTime<Utc>,
    ) {
        let mut transaction = match store.pool.begin().await {
            Ok(transaction) => transaction,
            Err(error) => {
                warn!(%error, "failed to begin Runtime approval reconciliation");
                return;
            }
        };
        let rows = match sqlx::query(
            "WITH eligible AS (SELECT a.approval_id, a.tool_call_id, r.harness_thread_id, r.harness_turn_id FROM assistant_tool_approvals AS a JOIN assistant_runs AS r ON r.run_id=a.run_id WHERE r.engine='agent_runtime' AND r.status IN ('running','awaiting_approval') AND a.status='pending' AND a.created_at < $1), cancelled AS (UPDATE assistant_tool_approvals AS a SET status='cancelled', reason='runtime_restarted', approved_at=NOW() FROM eligible AS e WHERE a.approval_id=e.approval_id AND a.status='pending' RETURNING a.approval_id, a.run_id, a.tenant_id, a.user_id, a.session_id, a.tool_name) SELECT c.approval_id, c.run_id, c.tenant_id, c.user_id, c.session_id, c.tool_name, e.tool_call_id, e.harness_thread_id, e.harness_turn_id FROM cancelled AS c JOIN eligible AS e ON e.approval_id=c.approval_id",
        )
        .bind(startup_cutoff)
        .fetch_all(&mut *transaction)
        .await
        {
            Ok(rows) => rows,
            Err(error) => {
                warn!(%error, "failed to reconcile orphaned Runtime approvals");
                return;
            }
        };
        for row in &rows {
            let run_id: Option<Uuid> = row.try_get("run_id").ok();
            if let Some(run_id) = run_id
                && let Err(error) = sqlx::query(
                    "UPDATE assistant_runs SET status='cancelled', error='AI_PLATFORM_AGENT_RUNTIME_APPROVAL_ORPHANED', finished_at=NOW(), updated_at=NOW() WHERE run_id=$1 AND engine='agent_runtime' AND status IN ('running','awaiting_approval')",
                )
                .bind(run_id)
                .execute(&mut *transaction)
                .await
            {
                warn!(%error, %run_id, "failed to close orphaned Agent run");
                let _ = transaction.rollback().await;
                return;
            }
        }
        if let Err(error) = transaction.commit().await {
            warn!(%error, "failed to commit Runtime approval reconciliation");
            return;
        }
        if !rows.is_empty() {
            warn!(
                count = rows.len(),
                "cancelled approvals orphaned by Runtime restart"
            );
        }
        for row in rows {
            let Some(thread_id) = row
                .try_get::<Option<Uuid>, _>("harness_thread_id")
                .ok()
                .flatten()
            else {
                continue;
            };
            let approval_id: Uuid = match row.try_get("approval_id") {
                Ok(value) => value,
                Err(error) => {
                    warn!(%error, "orphaned approval has invalid id");
                    continue;
                }
            };
            let run_id: Option<Uuid> = row.try_get("run_id").ok();
            let turn_id: String = row
                .try_get::<Option<String>, _>("harness_turn_id")
                .ok()
                .flatten()
                .unwrap_or_else(|| run_id.map(|id| id.to_string()).unwrap_or_default());
            let identity = match store
                .identity_for_kernel_thread(codex_protocol::ThreadId::from_u128(
                    thread_id.as_u128(),
                ))
                .await
            {
                Ok(identity) => identity,
                Err(error) => {
                    warn!(%error, %approval_id, "orphaned approval thread is unavailable");
                    continue;
                }
            };
            let tool_name: String = row
                .try_get("tool_name")
                .unwrap_or_else(|_| "unknown".to_string());
            let tool_call_id: Option<String> = row.try_get("tool_call_id").ok();
            let root_thread_id = identity.runtime_thread_id;
            if let Some(tool_call_id) = tool_call_id
                .clone()
                .filter(|value| !value.is_empty())
                .filter(|_| !matches!(tool_name.as_str(), "command_execution" | "file_change"))
            {
                let _ = store
                    .append_platform_lifecycle_event(PlatformLifecycleEvent {
                        kernel_thread_id: root_thread_id,
                        turn_id: turn_id.clone(),
                        item_id: Some(tool_call_id.clone()),
                        event_key: format!("tool-result/{turn_id}/{tool_call_id}"),
                        item_type: "tool_result".to_string(),
                        status: "cancelled".to_string(),
                        payload: json!({
                            "schema_version": "agent-runtime-tool-lifecycle/v1",
                            "turn_id": turn_id.clone(),
                            "tool_call_id": tool_call_id.clone(),
                            "lifecycle": "terminal",
                            "result_status": "cancelled",
                            "detail": "runtime_restarted",
                        }),
                    })
                    .await;
                let terminal_data = json!({
                    "run_id": turn_id.clone(),
                    "session_id": identity.session_id,
                    "thread_id": identity.runtime_thread_id,
                    "tool_call_id": tool_call_id.clone(),
                    "status": "cancelled",
                    "success": false,
                    "detail": "runtime_restarted",
                });
                for event_type in ["tool_call_result", "tool_call_end"] {
                    let event = AssistantTurnEventV1::new(event_type, terminal_data.clone());
                    let event_key = format!(
                        "compat/recovery/{}/{}/{}",
                        turn_id, tool_call_id, event_type
                    );
                    if let Err(error) = store
                        .append_v1_event(root_thread_id, Uuid::now_v7(), &event_key, &event)
                        .await
                    {
                        warn!(%error, %approval_id, "failed to project orphaned tool result");
                    }
                }
            }
            let approval_event = AssistantTurnEventV1::new(
                "approval_result",
                json!({
                    "run_id": turn_id,
                    "session_id": identity.session_id,
                    "thread_id": identity.runtime_thread_id,
                    "approval_id": approval_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "status": "cancelled",
                    "approved": false,
                    "reason": "runtime_restarted",
                }),
            );
            if let Err(error) = store
                .append_v1_event(
                    root_thread_id,
                    approval_id,
                    &format!("compat/approval/{approval_id}/result"),
                    &approval_event,
                )
                .await
            {
                warn!(%error, %approval_id, "failed to project orphaned approval result");
            }
            if let Some(run_id) = run_id {
                let run_event = AssistantTurnEventV1::new(
                    "run_error",
                    json!({
                        "run_id": turn_id,
                        "session_id": identity.session_id,
                        "thread_id": identity.runtime_thread_id,
                        "status": "cancelled",
                        "error_code": "AI_PLATFORM_AGENT_RUNTIME_APPROVAL_ORPHANED",
                        "reason": "runtime_restarted",
                    }),
                );
                if let Err(error) = store
                    .append_v1_event(
                        root_thread_id,
                        run_id,
                        &format!("compat/run/{run_id}/error"),
                        &run_event,
                    )
                    .await
                {
                    warn!(%error, %run_id, "failed to project orphaned run error");
                }
            }
        }
    }

    pub(crate) async fn capture_server_request(
        &self,
        request: &ServerRequest,
        store: &PostgresThreadStore,
    ) -> Result<Option<Uuid>, String> {
        let raw =
            serde_json::to_value(request).map_err(|_| "approval_request_invalid".to_string())?;
        let method = raw
            .get("method")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let kind = match method {
            "item/commandExecution/requestApproval" => ApprovalKind::Command,
            "item/fileChange/requestApproval" => ApprovalKind::FileChange,
            _ => return Ok(None),
        };
        let params = raw
            .get("params")
            .ok_or_else(|| "approval_params_missing".to_string())?;
        let thread_id = params
            .get("threadId")
            .or_else(|| params.get("conversationId"))
            .and_then(Value::as_str)
            .and_then(|value| Uuid::parse_str(value).ok())
            .ok_or_else(|| "approval_thread_missing".to_string())?;
        let identity = store
            .identity_for_kernel_thread(codex_protocol::ThreadId::from_u128(thread_id.as_u128()))
            .await
            .map_err(|_| "approval_thread_not_found".to_string())?;
        let turn_id = params
            .get("turnId")
            .or_else(|| params.get("turn_id"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let call_id = params
            .get("itemId")
            .or_else(|| params.get("callId"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if turn_id.is_empty() || call_id.is_empty() {
            return Err("approval_call_identity_missing".to_string());
        }
        let tool_name = match kind {
            ApprovalKind::Command => "command_execution",
            ApprovalKind::FileChange => "file_change",
        }
        .to_string();
        let mut digest = Sha256::new();
        digest.update(serde_json::to_vec(params).map_err(|_| "approval_params_invalid")?);
        let arguments_hash = format!("{:x}", digest.finalize());
        let approval_id = params
            .get("approvalId")
            .and_then(Value::as_str)
            .and_then(|value| Uuid::parse_str(value).ok())
            .unwrap_or_else(Uuid::now_v7);
        let run_id = Uuid::parse_str(&turn_id).map_err(|_| "approval_run_invalid".to_string())?;
        let insert = sqlx::query(
            "INSERT INTO assistant_tool_approvals (approval_id, tenant_id, user_id, session_id, run_id, tool_call_id, tool_name, arguments, status, expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending',NOW() + ($9 * INTERVAL '1 second')) ON CONFLICT (approval_id) DO NOTHING",
        )
        .bind(approval_id)
        .bind(&identity.tenant_id)
        .bind(&identity.user_id)
        .bind(&identity.session_id)
        .bind(run_id)
        .bind(&call_id)
        .bind(&tool_name)
        .bind(params)
        .bind(APPROVAL_TTL_SECONDS)
        .execute(&store.pool)
        .await
        .map_err(|_| "approval_persistence_failed".to_string())?;
        if insert.rows_affected() == 0 {
            let existing = sqlx::query(
                "SELECT tenant_id, user_id, session_id, run_id, tool_call_id, tool_name, arguments, status FROM assistant_tool_approvals WHERE approval_id=$1",
            )
            .bind(approval_id)
            .fetch_optional(&store.pool)
            .await
            .map_err(|_| "approval_persistence_failed".to_string())?
            .ok_or_else(|| "approval_conflict".to_string())?;
            let existing_arguments: Value = existing
                .try_get("arguments")
                .map_err(|_| "approval_conflict".to_string())?;
            let existing_run_id: Option<Uuid> = existing.try_get("run_id").ok();
            let existing_status: String = existing
                .try_get("status")
                .map_err(|_| "approval_conflict".to_string())?;
            if existing.try_get::<String, _>("tenant_id").ok().as_deref()
                != Some(identity.tenant_id.as_str())
                || existing.try_get::<String, _>("user_id").ok().as_deref()
                    != Some(identity.user_id.as_str())
                || existing.try_get::<String, _>("session_id").ok().as_deref()
                    != Some(identity.session_id.as_str())
                || existing_run_id != Some(run_id)
                || existing
                    .try_get::<String, _>("tool_call_id")
                    .ok()
                    .as_deref()
                    != Some(call_id.as_str())
                || existing.try_get::<String, _>("tool_name").ok().as_deref()
                    != Some(tool_name.as_str())
                || existing_arguments != params.clone()
            {
                return Err("approval_conflict".to_string());
            }
            if existing_status != "pending" {
                return Err("approval_already_consumed".to_string());
            }
            if self.pending.lock().await.contains_key(&approval_id) {
                return Ok(Some(approval_id));
            }
        }
        if store
            .append_platform_lifecycle_event(PlatformLifecycleEvent {
                kernel_thread_id: identity.runtime_thread_id,
                turn_id: turn_id.clone(),
                item_id: Some(call_id.clone()),
                event_key: format!("approval/{approval_id}"),
                item_type: "approval_request".to_string(),
                status: "pending".to_string(),
                payload: json!({
                    "schema_version": "agent-runtime-approval/v1",
                    "approval_id": approval_id,
                    "run_id": run_id,
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "arguments_hash": arguments_hash,
                    "status": "pending",
                }),
            })
            .await
            .is_err()
        {
            let _ = sqlx::query(
                "UPDATE assistant_tool_approvals SET status='cancelled', reason='approval_receipt_failed', approved_at=NOW() WHERE approval_id=$1 AND status='pending'",
            )
            .bind(approval_id)
            .execute(&store.pool)
            .await;
            let event = AssistantTurnEventV1::new(
                "approval_result",
                json!({
                    "run_id": turn_id,
                    "session_id": identity.session_id,
                    "thread_id": identity.runtime_thread_id,
                    "approval_id": approval_id,
                    "tool_call_id": call_id,
                    "status": "cancelled",
                    "approved": false,
                    "reason": "approval_receipt_failed",
                }),
            );
            let _ = store
                .append_v1_event(
                    identity.runtime_thread_id,
                    approval_id,
                    &format!("compat/approval/{approval_id}/result"),
                    &event,
                )
                .await;
            return Err("approval_receipt_failed".to_string());
        }
        self.pending
            .lock()
            .await
            .entry(approval_id)
            .or_insert(PendingApproval {
                destination: ApprovalDestination::ServerRequest(request.id().clone()),
                tenant_id: identity.tenant_id,
                user_id: identity.user_id,
                session_id: identity.session_id,
                thread_id,
                root_thread_id: identity.runtime_thread_id,
                turn_id,
                call_id,
            });
        Ok(Some(approval_id))
    }

    pub(crate) async fn summary(
        &self,
        approval_id: Uuid,
        tenant_id: &str,
        user_id: &str,
        session_id: &str,
        store: &PostgresThreadStore,
    ) -> Result<ApprovalSummary, String> {
        let row = sqlx::query(
            "SELECT status, run_id, tool_name, arguments, EXTRACT(EPOCH FROM (expires_at - NOW()))::bigint AS expires_in_seconds FROM assistant_tool_approvals WHERE approval_id=$1 AND tenant_id=$2 AND user_id=$3 AND session_id=$4",
        )
        .bind(approval_id)
        .bind(tenant_id)
        .bind(user_id)
        .bind(session_id)
        .fetch_optional(&store.pool)
        .await
        .map_err(|_| "approval_lookup_failed".to_string())?
        .ok_or_else(|| "approval_not_found".to_string())?;
        let arguments: Value =
            sqlx::Row::try_get(&row, "arguments").map_err(|_| "approval_invalid")?;
        let mut digest = Sha256::new();
        digest.update(serde_json::to_vec(&arguments).map_err(|_| "approval_invalid")?);
        Ok(ApprovalSummary {
            approval_id,
            status: sqlx::Row::try_get(&row, "status").map_err(|_| "approval_invalid")?,
            run_id: sqlx::Row::try_get(&row, "run_id").map_err(|_| "approval_invalid")?,
            tool_name: sqlx::Row::try_get(&row, "tool_name").map_err(|_| "approval_invalid")?,
            arguments_hash: format!("{:x}", digest.finalize()),
            expires_in_seconds: sqlx::Row::try_get(&row, "expires_in_seconds").unwrap_or(0),
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn decide(
        &self,
        approval_id: Uuid,
        decision: ApprovalDecision,
        tenant_id: &str,
        user_id: &str,
        session_id: &str,
        reason: Option<&str>,
        store: &PostgresThreadStore,
        requests: &InProcessAppServerRequestHandle,
    ) -> Result<ApprovalProjection, String> {
        let pending = self
            .pending
            .lock()
            .await
            .get(&approval_id)
            .cloned()
            .ok_or_else(|| "approval_not_found".to_string())?;
        if pending.tenant_id != tenant_id
            || pending.user_id != user_id
            || pending.session_id != session_id
        {
            return Err("approval_scope_mismatch".to_string());
        }
        let accepted = matches!(decision, ApprovalDecision::Approve);
        let next_status = if accepted { "approved" } else { "rejected" };
        let result = sqlx::query("UPDATE assistant_tool_approvals SET status=$2, reason=$3, approved_by=$4, approved_at=NOW() WHERE approval_id=$1 AND tenant_id=$5 AND user_id=$6 AND session_id=$7 AND status='pending' AND (expires_at IS NULL OR expires_at > NOW())")
            .bind(approval_id).bind(next_status).bind(reason).bind(user_id).bind(tenant_id).bind(user_id).bind(session_id)
            .execute(&store.pool).await.map_err(|_| "approval_update_failed".to_string())?;
        if result.rows_affected() != 1 {
            return Err("approval_expired_or_consumed".to_string());
        }
        let destination_failed = match &pending.destination {
            ApprovalDestination::ServerRequest(request_id) => {
                let decision_value = if accepted { "accept" } else { "decline" };
                requests
                    .resolve_server_request_async(
                        request_id.clone(),
                        json!({"decision": decision_value}),
                    )
                    .await
                    .is_err()
            }
            ApprovalDestination::Dynamic(waiter) => {
                waiter.lock().await.take().is_none_or(|sender| {
                    sender
                        .send(DynamicApprovalDecision {
                            approved: accepted,
                            reason: reason.map(str::to_string),
                        })
                        .is_err()
                })
            }
        };
        let projection_status = if matches!(pending.destination, ApprovalDestination::Dynamic(_)) {
            if accepted { "approved" } else { "rejected" }
        } else {
            "consumed"
        };
        if destination_failed {
            let _ = sqlx::query(
                "UPDATE assistant_tool_approvals SET status='cancelled', reason='runtime_resume_failed', approved_at=NOW() WHERE approval_id=$1 AND status='approved'",
            )
            .bind(approval_id)
            .execute(&store.pool)
            .await;
            if let Ok(run_id) = Uuid::parse_str(&pending.turn_id) {
                let _ = sqlx::query(
                    "UPDATE assistant_runs SET status='cancelled', error='AI_PLATFORM_AGENT_RUNTIME_APPROVAL_RESUME_FAILED', finished_at=NOW(), updated_at=NOW() WHERE run_id=$1 AND engine='agent_runtime' AND status IN ('running','awaiting_approval')",
                )
                .bind(run_id)
                .execute(&store.pool)
                .await;
            }
            let event = AssistantTurnEventV1::new(
                "approval_result",
                json!({
                    "run_id": pending.turn_id,
                    "session_id": pending.session_id,
                    "thread_id": pending.root_thread_id,
                    "approval_id": approval_id,
                    "tool_call_id": pending.call_id,
                    "status": "cancelled",
                    "approved": false,
                    "reason": "runtime_resume_failed",
                }),
            );
            let thread_id = codex_protocol::ThreadId::from_u128(pending.thread_id.as_u128());
            let _ = store
                .append_v1_event(
                    thread_id,
                    approval_id,
                    &format!("compat/approval/{approval_id}/result"),
                    &event,
                )
                .await;
            self.pending.lock().await.remove(&approval_id);
            return Err("approval_resume_failed".to_string());
        }
        let thread_id = codex_protocol::ThreadId::from_u128(pending.thread_id.as_u128());
        let event = AssistantTurnEventV1::new(
            "approval_result",
            json!({
                "run_id": pending.turn_id,
                "session_id": pending.session_id,
                "thread_id": pending.root_thread_id,
                "approval_id": approval_id,
                "tool_call_id": pending.call_id,
                "status": if accepted { "approved" } else { "rejected" },
                "approved": accepted,
                "reason": reason,
            }),
        );
        let sequence = store
            .append_v1_event(
                thread_id,
                Uuid::now_v7(),
                &format!("compat/approval/{approval_id}/result"),
                &event,
            )
            .await
            .map_err(|_| "approval_result_persistence_failed".to_string())?;
        self.pending.lock().await.remove(&approval_id);
        Ok(ApprovalProjection {
            root_thread_id: pending.root_thread_id,
            event: SequencedAssistantTurnEventV1 { sequence, event },
            status: projection_status,
        })
    }
}
