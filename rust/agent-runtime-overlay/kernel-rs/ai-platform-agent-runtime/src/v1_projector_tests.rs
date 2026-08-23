use codex_app_server_protocol::AgentMessageDeltaNotification;
use codex_app_server_protocol::DynamicToolCallStatus;
use codex_app_server_protocol::ItemCompletedNotification;
use codex_app_server_protocol::ItemStartedNotification;
use codex_app_server_protocol::ReasoningSummaryTextDeltaNotification;
use codex_app_server_protocol::ReasoningTextDeltaNotification;
use codex_app_server_protocol::ServerNotification;
use codex_app_server_protocol::ThreadItem;
use codex_app_server_protocol::Turn;
use codex_app_server_protocol::TurnCompletedNotification;
use codex_app_server_protocol::TurnItemsView;
use codex_app_server_protocol::TurnStatus;
use serde_json::json;

use super::v1_projector::V1ProjectionContext;
use super::v1_projector::project_server_notification;

fn context() -> V1ProjectionContext {
    V1ProjectionContext {
        tenant_id: "tenant-a".to_string(),
        user_id: "user-a".to_string(),
        session_id: "session-a".to_string(),
    }
}

#[test]
fn visible_text_and_reasoning_summary_map_without_raw_reasoning() {
    let text = project_server_notification(
        &ServerNotification::AgentMessageDelta(AgentMessageDeltaNotification {
            thread_id: "thread-a".to_string(),
            turn_id: "turn-a".to_string(),
            item_id: "message-a".to_string(),
            delta: "hello".to_string(),
        }),
        &context(),
    );
    let summary = project_server_notification(
        &ServerNotification::ReasoningSummaryTextDelta(ReasoningSummaryTextDeltaNotification {
            thread_id: "thread-a".to_string(),
            turn_id: "turn-a".to_string(),
            item_id: "reasoning-a".to_string(),
            delta: "checking".to_string(),
            summary_index: 0,
        }),
        &context(),
    );
    let raw = project_server_notification(
        &ServerNotification::ReasoningTextDelta(ReasoningTextDeltaNotification {
            thread_id: "thread-a".to_string(),
            turn_id: "turn-a".to_string(),
            item_id: "reasoning-a".to_string(),
            delta: "hidden chain".to_string(),
            content_index: 0,
        }),
        &context(),
    );

    assert_eq!(text[0].event_type, "text_delta");
    assert_eq!(text[0].data["content"], "hello");
    assert_eq!(summary[0].event_type, "thinking_delta");
    assert_eq!(summary[0].data["content"], "checking");
    assert!(raw.is_empty());
}

#[test]
fn dynamic_tool_item_projects_one_start_and_one_terminal_pair() {
    let item = ThreadItem::DynamicToolCall {
        id: "call-a".to_string(),
        namespace: Some("platform".to_string()),
        tool: "search".to_string(),
        arguments: json!({"query": "rust"}),
        status: DynamicToolCallStatus::Completed,
        content_items: None,
        success: Some(true),
        duration_ms: Some(25),
    };
    let started = project_server_notification(
        &ServerNotification::ItemStarted(ItemStartedNotification {
            item: item.clone(),
            thread_id: "thread-a".to_string(),
            turn_id: "turn-a".to_string(),
            started_at_ms: 1,
        }),
        &context(),
    );
    let completed = project_server_notification(
        &ServerNotification::ItemCompleted(ItemCompletedNotification {
            item,
            thread_id: "thread-a".to_string(),
            turn_id: "turn-a".to_string(),
            completed_at_ms: 2,
        }),
        &context(),
    );

    assert_eq!(started.len(), 1);
    assert_eq!(started[0].event_type, "tool_call_start");
    assert_eq!(started[0].data["tool_call_id"], "call-a");
    assert_eq!(completed.len(), 2);
    assert_eq!(completed[0].event_type, "tool_call_result");
    assert_eq!(completed[1].event_type, "tool_call_end");
    assert_eq!(
        completed[0].data["tool_call_id"],
        completed[1].data["tool_call_id"]
    );
    assert_eq!(completed[0].data["success"], true);
}

#[test]
fn turn_terminal_status_maps_to_one_v1_terminal_envelope() {
    let completed = project_server_notification(
        &ServerNotification::TurnCompleted(TurnCompletedNotification {
            thread_id: "thread-a".to_string(),
            turn: Turn {
                id: "turn-a".to_string(),
                items: Vec::new(),
                items_view: TurnItemsView::NotLoaded,
                status: TurnStatus::Completed,
                error: None,
                started_at: Some(1),
                completed_at: Some(2),
                duration_ms: Some(1_000),
            },
        }),
        &context(),
    );

    assert_eq!(completed.len(), 1);
    assert_eq!(completed[0].event_type, "run_finished");
    assert_eq!(completed[0].data["status"], "succeeded");
    assert_eq!(
        completed[0].data["terminal_envelope"]["schema_version"],
        "assistant-turn-contract/v1"
    );
    assert_eq!(completed[0].data["duration_ms"], 1_000);
}
