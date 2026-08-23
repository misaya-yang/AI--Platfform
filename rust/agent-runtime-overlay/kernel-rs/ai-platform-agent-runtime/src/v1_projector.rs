use codex_app_server_protocol::ServerNotification;
use codex_app_server_protocol::ThreadItem;
use codex_app_server_protocol::TurnStatus;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use serde_json::json;

pub const ASSISTANT_TURN_CONTRACT_V1: &str = "assistant-turn-contract/v1";

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct V1ProjectionContext {
    pub tenant_id: String,
    pub user_id: String,
    pub session_id: String,
}

#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
pub struct AssistantTurnEventV1 {
    pub schema_version: String,
    pub event_type: String,
    pub data: Value,
    pub timestamp: f64,
}

#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
pub struct SequencedAssistantTurnEventV1 {
    pub sequence: i64,
    #[serde(flatten)]
    pub event: AssistantTurnEventV1,
}

impl AssistantTurnEventV1 {
    pub(crate) fn new(event_type: &str, data: Value) -> Self {
        Self {
            schema_version: ASSISTANT_TURN_CONTRACT_V1.to_string(),
            event_type: event_type.to_string(),
            data,
            timestamp: chrono::Utc::now().timestamp_millis() as f64 / 1_000.0,
        }
    }
}

pub fn project_server_notification(
    notification: &ServerNotification,
    context: &V1ProjectionContext,
) -> Vec<AssistantTurnEventV1> {
    match notification {
        ServerNotification::TurnStarted(started) => vec![AssistantTurnEventV1::new(
            "run_started",
            lifecycle_data(
                context,
                &started.thread_id,
                &started.turn.id,
                "running",
                None,
            ),
        )],
        ServerNotification::AgentMessageDelta(delta) => vec![AssistantTurnEventV1::new(
            "text_delta",
            json!({
                "run_id": delta.turn_id,
                "session_id": context.session_id,
                "thread_id": delta.thread_id,
                "content": delta.delta,
            }),
        )],
        ServerNotification::ReasoningSummaryTextDelta(delta) => {
            vec![AssistantTurnEventV1::new(
                "thinking_delta",
                json!({
                    "run_id": delta.turn_id,
                    "session_id": context.session_id,
                    "thread_id": delta.thread_id,
                    "content": delta.delta,
                    "item_id": delta.item_id,
                    "summary_index": delta.summary_index,
                }),
            )]
        }
        // Raw reasoning is intentionally not a product event. Only provider-approved
        // reasoning summaries may reach the V1 compatibility stream.
        ServerNotification::ReasoningTextDelta(_) => Vec::new(),
        ServerNotification::ItemStarted(started) => project_tool_item(
            &started.item,
            &started.thread_id,
            &started.turn_id,
            context,
            ToolProjectionPhase::Started,
        ),
        ServerNotification::ItemCompleted(completed) => project_tool_item(
            &completed.item,
            &completed.thread_id,
            &completed.turn_id,
            context,
            ToolProjectionPhase::Completed,
        ),
        ServerNotification::TurnCompleted(completed) => {
            let (event_type, status) = match completed.turn.status {
                TurnStatus::Completed => ("run_finished", "succeeded"),
                TurnStatus::Interrupted => ("run_error", "cancelled"),
                TurnStatus::Failed => ("run_error", "failed"),
                TurnStatus::InProgress => return Vec::new(),
            };
            vec![AssistantTurnEventV1::new(
                event_type,
                lifecycle_data(
                    context,
                    &completed.thread_id,
                    &completed.turn.id,
                    status,
                    Some(completed.turn.duration_ms),
                ),
            )]
        }
        ServerNotification::Error(error) if !error.will_retry => vec![AssistantTurnEventV1::new(
            "error",
            json!({
                "run_id": error.turn_id,
                "session_id": context.session_id,
                "thread_id": error.thread_id,
                "message": "Agent runtime could not complete this request. Please try again.",
            }),
        )],
        _ => Vec::new(),
    }
}

pub fn server_notification_thread_id(notification: &ServerNotification) -> Option<&str> {
    match notification {
        ServerNotification::TurnStarted(value) => Some(&value.thread_id),
        ServerNotification::AgentMessageDelta(value) => Some(&value.thread_id),
        ServerNotification::ReasoningSummaryTextDelta(value) => Some(&value.thread_id),
        ServerNotification::ReasoningTextDelta(value) => Some(&value.thread_id),
        ServerNotification::ItemStarted(value) => Some(&value.thread_id),
        ServerNotification::ItemCompleted(value) => Some(&value.thread_id),
        ServerNotification::TurnCompleted(value) => Some(&value.thread_id),
        ServerNotification::Error(value) => Some(&value.thread_id),
        _ => None,
    }
}

fn lifecycle_data(
    context: &V1ProjectionContext,
    thread_id: &str,
    run_id: &str,
    status: &str,
    duration_ms: Option<Option<i64>>,
) -> Value {
    let mut data = json!({
        "run_id": run_id,
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
        "session_id": context.session_id,
        "thread_id": thread_id,
        "status": status,
    });
    if let Some(Some(duration_ms)) = duration_ms
        && let Some(object) = data.as_object_mut()
    {
        object.insert("duration_ms".to_string(), duration_ms.into());
    }
    if matches!(status, "succeeded" | "failed" | "cancelled")
        && let Some(object) = data.as_object_mut()
    {
        object.insert("exit".to_string(), status.into());
        object.insert(
            "terminal_envelope".to_string(),
            json!({
                "schema_version": ASSISTANT_TURN_CONTRACT_V1,
                "run_id": run_id,
                "tenant_id": context.tenant_id,
                "session_id": context.session_id,
                "thread_id": thread_id,
                "status": status,
                "exit_reason": status,
            }),
        );
    }
    data
}

#[derive(Clone, Copy)]
enum ToolProjectionPhase {
    Started,
    Completed,
}

fn tool_terminal_failed(status: Option<&str>) -> bool {
    !matches!(
        status.map(str::to_ascii_lowercase).as_deref(),
        Some("completed" | "succeeded")
    )
}

fn project_tool_item(
    item: &ThreadItem,
    thread_id: &str,
    turn_id: &str,
    context: &V1ProjectionContext,
    phase: ToolProjectionPhase,
) -> Vec<AssistantTurnEventV1> {
    let Some(mut tool) = tool_data(item) else {
        return Vec::new();
    };
    tool.insert("run_id".to_string(), turn_id.into());
    tool.insert("session_id".to_string(), context.session_id.clone().into());
    tool.insert("thread_id".to_string(), thread_id.into());
    match phase {
        ToolProjectionPhase::Started => {
            tool.insert("status".to_string(), "started".into());
            vec![AssistantTurnEventV1::new(
                "tool_call_start",
                Value::Object(tool),
            )]
        }
        ToolProjectionPhase::Completed => {
            let failed = tool_terminal_failed(tool.get("status").and_then(Value::as_str));
            tool.insert(
                "status".to_string(),
                if failed { "failed" } else { "completed" }.into(),
            );
            tool.insert("success".to_string(), (!failed).into());
            let data = Value::Object(tool);
            vec![
                AssistantTurnEventV1::new("tool_call_result", data.clone()),
                AssistantTurnEventV1::new("tool_call_end", data),
            ]
        }
    }
}

fn tool_data(item: &ThreadItem) -> Option<serde_json::Map<String, Value>> {
    let value = serde_json::to_value(item).ok()?;
    let object = value.as_object()?;
    let item_type = object.get("type")?.as_str()?;
    let tool_name = match item_type {
        "mcpToolCall" => format!(
            "{}.{}",
            object.get("server")?.as_str()?,
            object.get("tool")?.as_str()?
        ),
        "dynamicToolCall" => object.get("tool")?.as_str()?.to_string(),
        "commandExecution" => "shell".to_string(),
        "fileChange" => "apply_patch".to_string(),
        "collabAgentToolCall" => "collaboration".to_string(),
        "webSearch" => "web_search".to_string(),
        _ => return None,
    };
    let mut projected = serde_json::Map::new();
    let item_id = object.get("id")?.as_str()?;
    projected.insert("tool_call_id".to_string(), item_id.into());
    projected.insert("tool_name".to_string(), tool_name.into());
    if let Some(arguments) = object.get("arguments") {
        projected.insert("arguments".to_string(), arguments.clone());
    }
    if let Some(status) = object.get("status") {
        projected.insert("status".to_string(), status.clone());
    }
    if let Some(duration_ms) = object.get("durationMs") {
        projected.insert("duration_ms".to_string(), duration_ms.clone());
    }
    Some(projected)
}

#[cfg(test)]
mod tests {
    use super::tool_terminal_failed;

    #[test]
    fn non_successful_tool_statuses_are_projected_as_failures() {
        assert!(!tool_terminal_failed(Some("completed")));
        assert!(!tool_terminal_failed(Some("succeeded")));
        assert!(tool_terminal_failed(Some("failed")));
        assert!(tool_terminal_failed(Some("declined")));
        assert!(tool_terminal_failed(Some("cancelled")));
        assert!(tool_terminal_failed(None));
    }
}
