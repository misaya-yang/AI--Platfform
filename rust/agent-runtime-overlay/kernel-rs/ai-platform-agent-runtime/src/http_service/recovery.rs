//! Original-turn recovery: claim, reconcile original calls, then cold Core resume.

use axum::{
    Json,
    extract::{Path, State},
    http::HeaderMap,
};
use codex_app_server_protocol::{ClientRequest, DynamicToolCallParams, RequestId};
use codex_protocol::{ThreadId, models::ResponseItem, protocol::EventMsg};
use codex_rollout::RolloutItem;
use serde_json::{Value, json};
use sqlx::Row;
use std::collections::{BTreeMap, HashSet};
use std::time::Duration;
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

use super::security::{RuntimeError, SESSION_HEADER, TENANT_HEADER, USER_HEADER};
use super::thread_lifecycle::{ResumeThreadRequest, authorize_thread_scope, resume_params};
use super::turns::{StartTurnRequest, start_turn_mode};
use super::{RuntimeBroadcastEvent, RuntimeHttpState};
use crate::postgres_store::execution::ExecutionClaim;

#[derive(Default)]
struct RecoveryTail {
    started: bool,
    terminal: Option<&'static str>,
    superseded: bool,
    calls: BTreeMap<String, DynamicToolCallParams>,
    outputs: HashSet<String>,
}

fn recovery_tail(
    items: Vec<RolloutItem>,
    thread_id: ThreadId,
    run_id: &str,
) -> Result<RecoveryTail, String> {
    let mut tail = RecoveryTail::default();
    let mut active = false;
    for item in items {
        match item {
            RolloutItem::EventMsg(EventMsg::TurnStarted(event)) => {
                active = event.turn_id == run_id;
                if active {
                    tail.started = true;
                    tail.superseded = false;
                } else if tail.started {
                    tail.superseded = true;
                }
            }
            RolloutItem::EventMsg(EventMsg::TurnComplete(event)) if event.turn_id == run_id => {
                tail.terminal = Some("completed")
            }
            RolloutItem::EventMsg(EventMsg::TurnAborted(event))
                if event.turn_id.as_deref() == Some(run_id) =>
            {
                tail.terminal = Some("cancelled")
            }
            RolloutItem::ResponseItem(envelope) if active => match envelope.item {
                ResponseItem::FunctionCall {
                    name,
                    namespace,
                    arguments,
                    call_id,
                    ..
                } => {
                    let arguments = serde_json::from_str(&arguments)
                        .map_err(|_| "recovery_arguments_invalid")?;
                    let params = DynamicToolCallParams {
                        thread_id: thread_id.to_string(),
                        turn_id: run_id.to_string(),
                        call_id,
                        namespace,
                        tool: name,
                        arguments,
                    };
                    if let Some(previous) =
                        tail.calls.insert(params.call_id.clone(), params.clone())
                        && previous != params
                    {
                        return Err("recovery_call_identity_changed".to_string());
                    }
                }
                ResponseItem::FunctionCallOutput {
                    call_id: Some(call_id),
                    ..
                } => {
                    tail.outputs.insert(call_id);
                }
                ResponseItem::CustomToolCall { .. } | ResponseItem::LocalShellCall { .. } => {
                    return Err("native_tool_recovery_unsupported".to_string());
                }
                _ => {}
            },
            _ => {}
        }
    }
    Ok(tail)
}

pub(super) async fn recover_turn(
    State(state): State<RuntimeHttpState>,
    Path((thread_id, run_id)): Path<(String, String)>,
    headers: HeaderMap,
) -> Result<Json<Value>, RuntimeError> {
    let thread_id = authorize_thread_scope(&state, &headers, &thread_id).await?;
    let run_id =
        Uuid::parse_str(&run_id).map_err(|_| RuntimeError::bad_request("invalid_turn_id"))?;
    let owns:bool=sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM assistant_runtime_execution_owners WHERE run_id=$1 AND runtime_thread_id=$2)")
        .bind(run_id).bind(Uuid::parse_str(&thread_id.to_string()).map_err(|_|RuntimeError::bad_request("invalid_thread_id"))?)
        .fetch_one(&state.store.pool).await.map_err(|_|RuntimeError::unavailable("runtime_recovery_store_unavailable"))?;
    if !owns {
        return Err(RuntimeError::bad_request("runtime_turn_not_recoverable"));
    }
    // A request wakes the same durable scan; it does not start a new turn.
    state.recovery_notify.notify_one();
    Ok(Json(
        json!({"runId":run_id,"turnId":run_id,"status":"recovery_requested"}),
    ))
}

pub(super) async fn recovery_loop(state: RuntimeHttpState) {
    loop {
        if state.recovery_shutdown.is_cancelled() {
            break;
        }
        let claims: Vec<_> = state
            .store
            .execution_claims
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .values()
            .cloned()
            .collect();
        for claim in claims {
            if !state
                .store
                .heartbeat_execution(&claim)
                .await
                .unwrap_or(false)
            {
                // Detach stale work. Worker/model/store independently reject its fence.
                state.cancel_turn(&claim.run_id.to_string());
                let terminal = sqlx::query_scalar::<_, String>(
                    "SELECT status FROM assistant_runs WHERE run_id=$1",
                )
                .bind(claim.run_id)
                .fetch_optional(&state.store.pool)
                .await
                .ok()
                .flatten()
                .is_some_and(|status| !matches!(status.as_str(), "running" | "awaiting_approval"));
                if !terminal {
                    state.execution_shutdown.cancel();
                    return;
                }
            }
        }
        close_expired_authority(&state).await;
        let rows=sqlx::query("SELECT o.run_id,o.runtime_thread_id FROM assistant_runtime_execution_owners o JOIN assistant_runs r ON r.run_id=o.run_id WHERE o.owner_id<>$1 AND o.lease_until<=NOW() AND r.status IN ('running','awaiting_approval') ORDER BY o.created_at LIMIT 32")
            .bind(state.store.instance_id).fetch_all(&state.store.pool).await.unwrap_or_default();
        for row in rows {
            let Ok(run_id) = row.try_get::<Uuid, _>("run_id") else {
                continue;
            };
            let Ok(thread_id) = row.try_get::<Uuid, _>("runtime_thread_id") else {
                continue;
            };
            let thread_id = ThreadId::from_u128(thread_id.as_u128());
            let Ok(thread_guard) = state.thread_gate(thread_id).try_lock_owned() else {
                continue;
            };
            let Ok(claim) = state.store.claim_execution(run_id, None).await else {
                continue;
            };
            state
                .recovering_threads
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .insert(claim.thread_id);
            let state = state.clone();
            tokio::spawn(async move {
                let _thread_guard = thread_guard;
                let result = tokio::select! {
                    result = recover_claim(&state,claim.clone()) => Some(result),
                    () = state.recovery_shutdown.cancelled() => None,
                };
                if let Some(Err(code)) = result {
                    tracing::warn!(%run_id,code,"original turn recovery failed closed");
                    fail_recovery(&state, &claim, &code).await;
                }
                state
                    .recovering_threads
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .remove(&claim.thread_id);
            });
        }
        tokio::select! {
            ()=tokio::time::sleep(Duration::from_secs(5))=>{},
            ()=state.recovery_notify.notified()=>{},
            ()=state.recovery_shutdown.cancelled()=>break,
        }
    }
}

async fn recover_claim(state: &RuntimeHttpState, claim: ExecutionClaim) -> Result<(), String> {
    let body: StartTurnRequest =
        serde_json::from_value(claim.context.clone()).map_err(|_| "recovery_context_invalid")?;
    if body.run_id != claim.run_id {
        return Err("recovery_identity_mismatch".to_string());
    }
    let identity = state
        .store
        .identity_for_kernel_thread(claim.thread_id)
        .await
        .map_err(|_| "recovery_scope_unavailable")?;
    let rows=sqlx::query("SELECT payload FROM assistant_runtime_items WHERE kernel_thread_id=$1 AND event_type='rollout/item' ORDER BY sequence")
        .bind(Uuid::parse_str(&claim.thread_id.to_string()).map_err(|_|"recovery_identity_invalid")?)
        .fetch_all(&state.store.pool).await.map_err(|_|"recovery_history_unavailable")?;
    let items = rows
        .into_iter()
        .map(|row| {
            row.try_get::<Value, _>("payload")
                .map_err(|_| "recovery_history_invalid")
                .and_then(|value| {
                    serde_json::from_value(value).map_err(|_| "recovery_history_invalid")
                })
        })
        .collect::<Result<Vec<RolloutItem>, _>>()?;
    let mut tail = recovery_tail(items, claim.thread_id, &claim.run_id.to_string())?;
    if tail.superseded {
        return Err("recovery_turn_superseded".to_string());
    }
    if let Some(status) = tail.terminal {
        terminal_projection(state, &claim, status, None).await?;
        return Ok(());
    }
    let cancel = CancellationToken::new();
    state
        .turn_cancellations
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .insert(claim.run_id.to_string(), cancel.clone());
    let journal=sqlx::query("SELECT params FROM assistant_runtime_invocations WHERE run_id=$1 ORDER BY created_at,kernel_thread_id,call_id")
        .bind(claim.run_id).fetch_all(&state.store.pool).await.map_err(|_|"recovery_journal_unavailable")?;
    for row in journal {
        let params: DynamicToolCallParams = serde_json::from_value(
            row.try_get("params")
                .map_err(|_| "recovery_journal_invalid")?,
        )
        .map_err(|_| "recovery_journal_invalid")?;
        if params.thread_id != claim.thread_id.to_string() {
            return Err("descendant_recovery_unsupported".to_string());
        }
        tail.calls.insert(params.call_id.clone(), params);
    }
    for (call_id, params) in tail.calls {
        if tail.outputs.contains(&call_id) {
            continue;
        }
        let response = super::capability_dispatch::handle_dynamic_tool_call(
            &params,
            &claim.run_id.to_string(),
            &state.store,
            &state.readonly_by_turn,
            &state.capability_client,
            &state.approvals,
            &state.events,
            &cancel,
        )
        .await?;
        state
            .store
            .inject_invocation_output(&params, response)
            .await
            .map_err(|_| "recovery_output_persistence_failed")?;
    }
    state
        .store
        .assert_execution(claim.run_id)
        .await
        .map_err(|_| "recovery_fence_lost")?;
    let snapshot: Value =
        sqlx::query_scalar("SELECT snapshot FROM assistant_runtime_snapshots WHERE run_id=$1")
            .bind(claim.run_id)
            .fetch_one(&state.store.pool)
            .await
            .map_err(|_| "recovery_snapshot_unavailable")?;
    let policy = state
        .store
        .resolve_tool_policy(claim.thread_id, None)
        .await
        .map_err(|_| "recovery_tool_policy_invalid")?;
    let mut resume_config = snapshot
        .get("runtime_resume_config")
        .cloned()
        .ok_or("recovery_resume_config_missing")?;
    resume_config["autoCompactTokenLimit"] = snapshot
        .pointer("/limits/auto_compact_token_limit")
        .cloned()
        .unwrap_or(Value::Null);
    let resume: ResumeThreadRequest =
        serde_json::from_value(resume_config).map_err(|_| "recovery_resume_config_invalid")?;
    let params =
        resume_params(claim.thread_id, resume).map_err(|_| "recovery_resume_config_invalid")?;
    state
        .requests
        .request_thread_resume(
            ClientRequest::ThreadResume {
                request_id: RequestId::String(format!(
                    "recovery-load-{}-{}",
                    claim.run_id, claim.fence
                )),
                params,
            },
            codex_app_server::host_runtime::AppServerThreadResumeOptions::new(
                policy.kernel_policy(),
            ),
        )
        .await
        .map_err(|_| "recovery_kernel_unavailable")?
        .map_err(|_| "recovery_thread_rejected")?;
    let mut headers = HeaderMap::new();
    for (name, value) in [
        (
            "x-ai-platform-internal-token",
            state.internal_token.to_string(),
        ),
        (TENANT_HEADER, identity.tenant_id),
        (USER_HEADER, identity.user_id),
        (SESSION_HEADER, identity.session_id),
    ] {
        headers.insert(name, value.parse().map_err(|_| "recovery_scope_invalid")?);
    }
    // A crash before Core admission has no user input in history. Admit the
    // already-reserved original run once; an accepted turn uses recovery mode.
    let _ = start_turn_mode(
        State(state.clone()),
        Path(claim.thread_id.to_string()),
        headers,
        Json(body),
        tail.started,
    )
    .await
    .map_err(|_| "recovery_turn_rejected")?;
    Ok(())
}

async fn terminal_projection(
    state: &RuntimeHttpState,
    claim: &ExecutionClaim,
    status: &str,
    error: Option<&str>,
) -> Result<(), String> {
    state
        .store
        .assert_execution(claim.run_id)
        .await
        .map_err(|_| "recovery_fence_lost")?;
    state
        .store
        .admit_turn_terminal(claim.thread_id, &claim.run_id.to_string())
        .await
        .map_err(|_| "recovery_tool_terminal_unavailable")?;
    sqlx::query("UPDATE assistant_runs r SET status=$2,error=$3,finished_at=COALESCE(finished_at,NOW()),updated_at=NOW() WHERE r.run_id=$1 AND r.status IN ('running','awaiting_approval') AND EXISTS (SELECT 1 FROM assistant_runtime_execution_owners o WHERE o.run_id=r.run_id AND o.owner_id=$4 AND o.fence=$5 AND o.lease_until>NOW())")
        .bind(claim.run_id).bind(status).bind(error).bind(claim.owner_id).bind(claim.fence).execute(&state.store.pool).await.map_err(|_|"recovery_terminal_store_unavailable")?;
    let identity = state
        .store
        .identity_for_kernel_thread(claim.thread_id)
        .await
        .map_err(|_| "recovery_scope_unavailable")?;
    let event = crate::AssistantTurnEventV1::new(
        if status == "completed" {
            "run_finished"
        } else {
            "run_error"
        },
        json!({
            "run_id":claim.run_id,"session_id":identity.session_id,"thread_id":claim.thread_id,"status":status,
            "error_code":error,"recovery":"original_turn_reconciled",
        }),
    );
    let sequence = state
        .store
        .append_v1_event(
            claim.thread_id,
            claim.run_id,
            &format!("compat/recovery/{}/terminal", claim.run_id),
            &event,
        )
        .await
        .map_err(|_| "recovery_terminal_projection_failed")?;
    let _ = state.events.send(RuntimeBroadcastEvent {
        root_thread_id: claim.thread_id,
        event: crate::SequencedAssistantTurnEventV1 { sequence, event },
    });
    Ok(())
}

async fn fail_recovery(state: &RuntimeHttpState, claim: &ExecutionClaim, code: &str) {
    let _ = terminal_projection(state, claim, "failed", Some(code)).await;
}

async fn close_expired_authority(state: &RuntimeHttpState) {
    let rows=sqlx::query("WITH closed AS (UPDATE assistant_runs r SET status='failed',error='RUNTIME_RECOVERY_AUTHORITY_EXPIRED',finished_at=NOW(),updated_at=NOW() FROM assistant_runtime_execution_owners o WHERE o.run_id=r.run_id AND o.lease_until<=NOW() AND r.status IN ('running','awaiting_approval') AND (NOT EXISTS (SELECT 1 FROM assistant_runtime_model_leases l WHERE l.run_id=r.run_id AND l.status='active' AND l.expires_at>NOW()) OR EXISTS (SELECT 1 FROM assistant_runtime_snapshot_revocations x WHERE x.snapshot_id=r.runtime_snapshot_id)) RETURNING r.run_id,r.harness_thread_id) SELECT * FROM closed")
        .fetch_all(&state.store.pool).await.unwrap_or_default();
    for row in rows {
        let (Ok(run_id), Ok(thread_id)) = (
            row.try_get::<Uuid, _>("run_id"),
            row.try_get::<Uuid, _>("harness_thread_id"),
        ) else {
            continue;
        };
        let _=sqlx::query("UPDATE assistant_tool_approvals SET status='cancelled',reason='recovery_authority_expired',approved_at=NOW() WHERE run_id=$1 AND status IN ('pending','approved')")
            .bind(run_id).execute(&state.store.pool).await;
        let thread_id = ThreadId::from_u128(thread_id.as_u128());
        let _ = state
            .store
            .admit_turn_terminal(thread_id, &run_id.to_string())
            .await;
        if let Ok(identity) = state.store.identity_for_kernel_thread(thread_id).await {
            let event = crate::AssistantTurnEventV1::new(
                "run_error",
                json!({"run_id":run_id,"session_id":identity.session_id,"thread_id":thread_id,"status":"failed","error_code":"RUNTIME_RECOVERY_AUTHORITY_EXPIRED"}),
            );
            let _ = state
                .store
                .append_v1_event(
                    thread_id,
                    run_id,
                    &format!("compat/recovery/{run_id}/authority-expired"),
                    &event,
                )
                .await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_protocol::protocol::TurnStartedEvent;
    use codex_rollout::ResponseItemEnvelope;

    fn started(turn: &str) -> RolloutItem {
        RolloutItem::EventMsg(EventMsg::TurnStarted(TurnStartedEvent {
            turn_id: turn.to_string(),
            root_turn_id: None,
            trace_id: None,
            started_at: None,
            model_context_window: None,
            collaboration_mode_kind: Default::default(),
        }))
    }
    fn call(value: i32) -> RolloutItem {
        RolloutItem::ResponseItem(ResponseItemEnvelope::new(ResponseItem::FunctionCall {
            id: None,
            name: "fixture_tool".to_string(),
            namespace: None,
            arguments: format!("{{\"value\":{value}}}"),
            encrypted_function_args: None,
            call_id: "call-a".to_string(),
            internal_chat_message_metadata_passthrough: None,
        }))
    }
    #[test]
    fn recovery_preserves_original_pending_call_and_rejects_superseded_turn() {
        let thread = ThreadId::from_u128(1);
        let tail = recovery_tail(vec![started("original"), call(1)], thread, "original").unwrap();
        assert!(tail.started && !tail.superseded);
        assert_eq!(tail.calls["call-a"].turn_id, "original");
        assert_eq!(tail.calls["call-a"].arguments, json!({"value":1}));
        let tail = recovery_tail(
            vec![started("original"), call(1), started("later")],
            thread,
            "original",
        )
        .unwrap();
        assert!(tail.superseded);
    }
    #[test]
    fn recovery_rejects_mutation_of_original_call_identity() {
        assert!(
            matches!(recovery_tail(vec![started("original"),call(1),call(2)],ThreadId::from_u128(1),"original"),Err(code) if code=="recovery_call_identity_changed")
        );
    }
}
