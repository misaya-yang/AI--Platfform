//! Durable platform lifecycle hooks installed into the Agent kernel.

use std::sync::Arc;

use codex_core::config::Config;
use codex_extension_api::ExtensionFuture;
use codex_extension_api::ThreadLifecycleContributor;
use codex_extension_api::ThreadResumeInput;
use codex_extension_api::ToolCallOutcome;
use codex_extension_api::ToolDispatchError;
use codex_extension_api::ToolEffect;
use codex_extension_api::ToolFinishInput;
use codex_extension_api::ToolLifecycleContributor;
use codex_extension_api::ToolLifecycleFuture;
use codex_extension_api::ToolStartInput;
use codex_extension_api::TurnAbortInput;
use codex_extension_api::TurnLifecycleContributor;
use codex_extension_api::TurnStopInput;
use codex_protocol::ThreadId;
use codex_tools::ToolPayload;
use serde_json::Value;
use serde_json::json;
use sha2::Digest;
use sha2::Sha256;

use crate::PostgresThreadStore;
use crate::postgres_store::PlatformLifecycleEvent;

#[derive(Clone)]
pub(crate) struct PlatformLifecycleContributor {
    store: Arc<PostgresThreadStore>,
}

impl PlatformLifecycleContributor {
    pub(crate) fn new(store: Arc<PostgresThreadStore>) -> Self {
        Self { store }
    }

    async fn recover_unclosed(&self, thread_id: ThreadId, turn_id: &str) {
        let Ok(events) = self
            .store
            .read_platform_lifecycle_events(thread_id, turn_id)
            .await
        else {
            return;
        };
        for call_id in unclosed_tool_call_ids(&events) {
            let payload = json!({
                "schema_version": "agent-runtime-tool-lifecycle/v1",
                "turn_id": turn_id,
                "tool_call_id": call_id,
                "lifecycle": "terminal",
                "result_status": "side_effect_unknown",
                "recovery": "runtime_restart_or_interrupt",
            });
            let _ = self
                .store
                .append_platform_lifecycle_event(PlatformLifecycleEvent {
                    kernel_thread_id: thread_id,
                    turn_id: turn_id.to_string(),
                    item_id: Some(call_id.clone()),
                    event_key: format!("tool-result/{turn_id}/{call_id}"),
                    item_type: "tool_result".to_string(),
                    status: "side_effect_unknown".to_string(),
                    payload,
                })
                .await;
        }
    }

    async fn append(&self, event: PlatformLifecycleEvent) -> Result<(), ToolDispatchError> {
        self.store
            .append_platform_lifecycle_event(event)
            .await
            .map(|_| ())
            .map_err(|_| ToolDispatchError {
                code: "AI_PLATFORM_AGENT_RUNTIME_TOOL_RECEIPT_UNAVAILABLE".to_string(),
                message: "tool dispatch blocked because its durable receipt could not be written"
                    .to_string(),
            })
    }
}

/// Reconstructs the calls that have a durable dispatch receipt but no durable
/// terminal result. This is intentionally pure so a Runtime restart and a
/// live interrupt use the same recovery decision.
pub(crate) fn unclosed_tool_call_ids(events: &[Value]) -> Vec<String> {
    let mut started = std::collections::BTreeSet::new();
    let mut finished = std::collections::BTreeSet::new();
    for event in events {
        let Some(call_id) = event.get("tool_call_id").and_then(Value::as_str) else {
            continue;
        };
        match event.get("lifecycle").and_then(Value::as_str) {
            Some("dispatched") => {
                started.insert(call_id.to_string());
            }
            Some("terminal") => {
                finished.insert(call_id.to_string());
            }
            _ => {}
        }
    }
    started.difference(&finished).cloned().collect()
}

fn command_argument(payload: &ToolPayload) -> Option<String> {
    let raw = payload.log_payload();
    serde_json::from_str::<Value>(raw.as_ref())
        .ok()?
        .get("cmd")
        .and_then(Value::as_str)
        .map(str::to_string)
}

fn is_internal_agent_control_tool(tool_name: &str) -> bool {
    let unnamespaced = matches!(
        tool_name,
        "spawn_agent"
            | "send_input"
            | "wait"
            | "close_agent"
            | "resume_agent"
            | "send_message"
            | "followup_task"
            | "wait_agent"
            | "list_agents"
            | "interrupt_agent"
            | "update_plan"
            | "skillsread"
    );
    let collaboration_v2 = matches!(
        tool_name,
        "collaborationspawn_agent"
            | "collaborationsend_message"
            | "collaborationfollowup_task"
            | "collaborationwait_agent"
            | "collaborationlist_agents"
            | "collaborationinterrupt_agent"
    );
    let collaboration_v1 = matches!(
        tool_name,
        "multi_agent_v1spawn_agent"
            | "multi_agent_v1send_input"
            | "multi_agent_v1wait"
            | "multi_agent_v1close_agent"
            | "multi_agent_v1resume_agent"
    );
    unnamespaced || collaboration_v2 || collaboration_v1
}

impl ThreadLifecycleContributor<Config> for PlatformLifecycleContributor {
    fn on_thread_resume<'a>(&'a self, input: ThreadResumeInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            let Ok(thread_id) = ThreadId::from_string(input.thread_store.level_id()) else {
                return;
            };
            // A process kill can happen before the previous turn's abort hook
            // runs. Revisit every durable turn receipt on resume and close any
            // dispatched call that lacks a result as side_effect_unknown.
            let Ok(turn_ids) = self.store.read_platform_lifecycle_turn_ids(thread_id).await else {
                return;
            };
            for turn_id in turn_ids {
                self.recover_unclosed(thread_id, &turn_id).await;
            }
        })
    }
}

impl ToolLifecycleContributor for PlatformLifecycleContributor {
    fn before_tool_dispatch<'a>(
        &'a self,
        input: &'a codex_extension_api::ToolStartInput<'a>,
    ) -> codex_extension_api::ToolDispatchFuture<'a> {
        Box::pin(async move {
            let tool_name = input.tool_name.to_string();
            if matches!(input.effect, ToolEffect::Write | ToolEffect::Unknown)
                && !is_internal_agent_control_tool(&tool_name)
            {
                let run_id =
                    uuid::Uuid::parse_str(input.turn_id).map_err(|_| ToolDispatchError {
                        code: "AI_PLATFORM_AGENT_RUNTIME_APPROVAL_REQUIRED".to_string(),
                        message: "write-capable tool requires an approved runtime action"
                            .to_string(),
                    })?;
                let thread_id =
                    ThreadId::from_string(input.thread_store.level_id()).map_err(|_| {
                        ToolDispatchError {
                            code: "AI_PLATFORM_AGENT_RUNTIME_SCOPE_INVALID".to_string(),
                            message: "tool scope is invalid".to_string(),
                        }
                    })?;
                let identity = self
                    .store
                    .identity_for_kernel_thread(thread_id)
                    .await
                    .map_err(|_| ToolDispatchError {
                        code: "AI_PLATFORM_AGENT_RUNTIME_SCOPE_INVALID".to_string(),
                        message: "tool scope is unavailable".to_string(),
                    })?;
                let tool_kind = match tool_name.as_str() {
                    "shell" | "exec_command" | "shell_command" => "command_execution",
                    "apply_patch" => "file_change",
                    other => other,
                };
                let expected_command = if tool_kind == "command_execution" {
                    Some(command_argument(input.payload).ok_or_else(|| {
                        ToolDispatchError {
                            code: "AI_PLATFORM_AGENT_RUNTIME_APPROVAL_REQUIRED".to_string(),
                            message: "write-capable tool arguments could not be bound to approval"
                                .to_string(),
                        }
                    })?)
                } else {
                    None
                };
                let claimed = sqlx::query(
                    "UPDATE assistant_tool_approvals SET status='consumed', approved_at=COALESCE(approved_at, NOW()) WHERE approval_id IN (SELECT approval_id FROM assistant_tool_approvals WHERE run_id=$1 AND tenant_id=$2 AND user_id=$3 AND session_id=$4 AND tool_name=$5 AND arguments->>'itemId'=$6 AND ($7::text IS NULL OR arguments->>'command'=$7) AND status='approved' AND expires_at > NOW() ORDER BY approved_at NULLS LAST, approval_id LIMIT 1) AND status='approved' RETURNING approval_id",
                )
                .bind(run_id)
                .bind(identity.tenant_id)
                .bind(identity.user_id)
                .bind(identity.session_id)
                .bind(tool_kind)
                .bind(input.call_id)
                .bind(expected_command)
                .fetch_optional(&self.store.pool)
                .await
                .map_err(|_| ToolDispatchError {
                    code: "AI_PLATFORM_AGENT_RUNTIME_APPROVAL_UNAVAILABLE".to_string(),
                    message: "tool dispatch approval could not be verified".to_string(),
                })?;
                if claimed.is_none() {
                    return Err(ToolDispatchError {
                        code: "AI_PLATFORM_AGENT_RUNTIME_APPROVAL_REQUIRED".to_string(),
                        message: "write-capable tool requires an approved runtime action"
                            .to_string(),
                    });
                }
            }
            let mut digest = Sha256::new();
            digest.update(input.payload.log_payload().as_bytes());
            let arguments_sha256 = format!("{:x}", digest.finalize());
            let thread_id = ThreadId::from_string(input.thread_store.level_id()).map_err(|_| {
                ToolDispatchError {
                    code: "AI_PLATFORM_AGENT_RUNTIME_SCOPE_INVALID".to_string(),
                    message: "tool scope is invalid".to_string(),
                }
            })?;
            self.append(PlatformLifecycleEvent {
                kernel_thread_id: thread_id,
                turn_id: input.turn_id.to_string(),
                item_id: Some(input.call_id.to_string()),
                event_key: format!("tool-use/{}/{}", input.turn_id, input.call_id),
                item_type: "tool_use".to_string(),
                status: "dispatched".to_string(),
                payload: json!({
                    "schema_version": "agent-runtime-tool-lifecycle/v1",
                    "turn_id": input.turn_id,
                    "tool_call_id": input.call_id,
                    "tool_name": input.tool_name.to_string(),
                    "arguments_sha256": arguments_sha256,
                    "lifecycle": "dispatched",
                    "dispatch_state": "dispatched",
                    "effect": format!("{:?}", input.effect),
                }),
            })
            .await
        })
    }

    fn on_tool_start<'a>(&'a self, _input: ToolStartInput<'a>) -> ToolLifecycleFuture<'a> {
        Box::pin(async {})
    }

    fn on_tool_finish<'a>(&'a self, input: ToolFinishInput<'a>) -> ToolLifecycleFuture<'a> {
        Box::pin(async move {
            let (result_status, detail) = match input.outcome {
                ToolCallOutcome::Completed { success: true } => ("succeeded", "completed"),
                ToolCallOutcome::Completed { success: false } => ("failed", "completed_failed"),
                ToolCallOutcome::Blocked => ("cancelled", "blocked_before_handler"),
                ToolCallOutcome::Failed {
                    handler_executed: true,
                } => ("side_effect_unknown", "handler_failed_after_dispatch"),
                ToolCallOutcome::Failed {
                    handler_executed: false,
                } => ("failed", "handler_not_executed"),
                ToolCallOutcome::Aborted => ("cancelled", "aborted"),
            };
            let payload = json!({
                "schema_version": "agent-runtime-tool-lifecycle/v1",
                "turn_id": input.turn_id,
                "tool_call_id": input.call_id,
                "tool_name": input.tool_name.to_string(),
                "lifecycle": "terminal",
                "result_status": result_status,
                "detail": detail,
            });
            let Ok(thread_id) = ThreadId::from_string(input.thread_store.level_id()) else {
                return;
            };
            let _ = self
                .append(PlatformLifecycleEvent {
                    kernel_thread_id: thread_id,
                    turn_id: input.turn_id.to_string(),
                    item_id: Some(input.call_id.to_string()),
                    event_key: format!("tool-result/{}/{}", input.turn_id, input.call_id),
                    item_type: "tool_result".to_string(),
                    status: result_status.to_string(),
                    payload,
                })
                .await;
        })
    }
}

impl TurnLifecycleContributor for PlatformLifecycleContributor {
    fn on_turn_abort<'a>(&'a self, input: TurnAbortInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            let Ok(thread_id) = ThreadId::from_string(input.thread_store.level_id()) else {
                return;
            };
            self.recover_unclosed(thread_id, input.turn_store.level_id())
                .await;
        })
    }

    fn on_turn_stop<'a>(&'a self, input: TurnStopInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            let Ok(thread_id) = ThreadId::from_string(input.thread_store.level_id()) else {
                return;
            };
            self.recover_unclosed(thread_id, input.turn_store.level_id())
                .await;
        })
    }
}

#[cfg(test)]
mod tests {
    use super::command_argument;
    use super::is_internal_agent_control_tool;
    use super::unclosed_tool_call_ids;
    use codex_tools::ToolPayload;
    use serde_json::json;

    #[test]
    fn restart_recovery_detects_only_unpaired_dispatched_calls() {
        let events = vec![
            json!({"tool_call_id":"read-1", "lifecycle":"dispatched"}),
            json!({"tool_call_id":"read-1", "lifecycle":"terminal"}),
            json!({"tool_call_id":"write-1", "lifecycle":"dispatched"}),
            json!({"tool_call_id":"pending", "lifecycle":"published"}),
        ];
        assert_eq!(unclosed_tool_call_ids(&events), vec!["write-1"]);
    }

    #[test]
    fn approval_binding_extracts_the_exact_exec_command_argument() {
        let payload = ToolPayload::Function {
            arguments: r#"{"cmd":"rm -rf /tmp/example"}"#.to_string(),
        };
        assert_eq!(
            command_argument(&payload).as_deref(),
            Some("rm -rf /tmp/example")
        );
        let malformed = ToolPayload::Function {
            arguments: "not-json".to_string(),
        };
        assert!(command_argument(&malformed).is_none());
    }

    #[test]
    fn native_agent_control_tools_do_not_require_external_action_approval() {
        for name in [
            "spawn_agent",
            "collaborationspawn_agent",
            "send_message",
            "wait_agent",
            "interrupt_agent",
            "update_plan",
            "skillsread",
        ] {
            assert!(is_internal_agent_control_tool(name));
        }
        for name in ["exec_command", "apply_patch", "unknown_external_tool"] {
            assert!(!is_internal_agent_control_tool(name));
        }
    }
}
