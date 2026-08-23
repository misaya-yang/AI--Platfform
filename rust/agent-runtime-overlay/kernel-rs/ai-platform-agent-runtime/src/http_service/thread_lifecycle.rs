use std::collections::HashMap;

use axum::Json;
use axum::extract::Path;
use axum::extract::State;
use axum::http::HeaderMap;
use codex_app_server_protocol::ClientRequest;
use codex_app_server_protocol::RequestId;
use codex_app_server_protocol::ThreadArchiveParams;
use codex_app_server_protocol::ThreadArchiveResponse;
use codex_app_server_protocol::ThreadResumeParams;
use codex_app_server_protocol::ThreadResumeResponse;
use codex_app_server_protocol::ThreadUnarchiveParams;
use codex_app_server_protocol::ThreadUnarchiveResponse;
use codex_app_server_protocol::TurnInterruptParams;
use codex_app_server_protocol::TurnInterruptResponse;
use codex_protocol::ThreadId;
use serde::Deserialize;
use serde_json::Value;
use serde_json::json;
use tracing::warn;
use uuid::Uuid;

use super::RuntimeHttpState;
use super::security::RuntimeError;
use super::security::SESSION_HEADER;
use super::security::TENANT_HEADER;
use super::security::USER_HEADER;
use super::security::authorize;
use super::security::required_header;
use crate::PlatformThreadIdentity;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct EmptyLifecycleRequest {}

#[derive(Default, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(super) struct ResumeThreadRequest {
    model: Option<String>,
    model_plane_base_url: Option<String>,
}

pub(super) async fn resume_thread(
    State(state): State<RuntimeHttpState>,
    Path(thread_id): Path<String>,
    headers: HeaderMap,
    Json(body): Json<ResumeThreadRequest>,
) -> Result<Json<ThreadResumeResponse>, RuntimeError> {
    let thread_id = authorize_thread_scope(&state, &headers, &thread_id).await?;
    let params = resume_params(thread_id, body)?;
    request_typed(
        &state,
        ClientRequest::ThreadResume {
            request_id: lifecycle_request_id("thread-resume", thread_id),
            params,
        },
        "invalid_agent_thread_resume_response",
    )
    .await
}

/// Interrupts one active turn after checking the platform-owned thread
/// identity. Agent remains responsible for stopping its loop and emitting the
/// terminal/interrupted item; this endpoint only forwards the typed request so
/// a client disconnect cannot bypass the kernel's tool-result closure rules.
pub(super) async fn interrupt_turn(
    State(state): State<RuntimeHttpState>,
    Path((thread_id, turn_id)): Path<(String, String)>,
    headers: HeaderMap,
    Json(_body): Json<EmptyLifecycleRequest>,
) -> Result<Json<TurnInterruptResponse>, RuntimeError> {
    let thread_id = authorize_thread_scope(&state, &headers, &thread_id).await?;
    if turn_id.is_empty() || turn_id.len() > 255 {
        return Err(RuntimeError::bad_request("invalid_turn_id"));
    }
    request_typed(
        &state,
        ClientRequest::TurnInterrupt {
            request_id: RequestId::String(format!("turn-interrupt-{thread_id}-{turn_id}")),
            params: TurnInterruptParams {
                thread_id: thread_id.to_string(),
                turn_id,
            },
        },
        "invalid_agent_turn_interrupt_response",
    )
    .await
}

fn resume_params(
    thread_id: ThreadId,
    body: ResumeThreadRequest,
) -> Result<ThreadResumeParams, RuntimeError> {
    let (model, model_plane_base_url) = match (body.model, body.model_plane_base_url) {
        (None, None) => {
            return Ok(ThreadResumeParams {
                thread_id: thread_id.to_string(),
                ..Default::default()
            });
        }
        (Some(model), Some(model_plane_base_url)) => (model, model_plane_base_url),
        _ => return Err(RuntimeError::bad_request("invalid_thread_resume_config")),
    };
    if model.is_empty()
        || model.len() > 255
        || model_plane_base_url.len() > 2048
        || !valid_model_plane_base_url(&model_plane_base_url)
    {
        return Err(RuntimeError::bad_request("invalid_thread_resume_config"));
    }
    let provider_id = "ai-platform-gateway";
    let mut config = HashMap::<String, Value>::new();
    config.insert("model_provider".to_string(), provider_id.into());
    config.insert("model".to_string(), model.clone().into());
    config.insert(
        "model_providers".to_string(),
        json!({
            (provider_id): {
                "name": "AI Platform Gateway Model Plane",
                "base_url": model_plane_base_url,
                "env_key": "AI_PLATFORM_AGENT_RUNTIME_MODEL_PLANE_INTERNAL_TOKEN",
                "wire_api": "responses",
                "requires_openai_auth": false,
                "supports_websockets": false,
                "request_max_retries": 0,
                "stream_max_retries": 0,
            }
        }),
    );
    Ok(ThreadResumeParams {
        thread_id: thread_id.to_string(),
        model: Some(model),
        model_provider: Some(provider_id.to_string()),
        config: Some(config),
        ..Default::default()
    })
}

fn valid_model_plane_base_url(value: &str) -> bool {
    let remainder = value
        .strip_prefix("http://")
        .or_else(|| value.strip_prefix("https://"));
    let Some((authority, path)) = remainder.and_then(|value| value.split_once('/')) else {
        return false;
    };
    !authority.is_empty()
        && !authority.contains('@')
        && !authority.chars().any(char::is_whitespace)
        && path == "internal/v1/agent-model-plane"
}

#[cfg(test)]
#[path = "thread_lifecycle_tests.rs"]
mod tests;

pub(super) async fn archive_thread(
    State(state): State<RuntimeHttpState>,
    Path(thread_id): Path<String>,
    headers: HeaderMap,
    Json(_body): Json<EmptyLifecycleRequest>,
) -> Result<Json<ThreadArchiveResponse>, RuntimeError> {
    let thread_id = authorize_thread_scope(&state, &headers, &thread_id).await?;
    request_typed(
        &state,
        ClientRequest::ThreadArchive {
            request_id: lifecycle_request_id("thread-archive", thread_id),
            params: ThreadArchiveParams {
                thread_id: thread_id.to_string(),
            },
        },
        "invalid_agent_thread_archive_response",
    )
    .await
}

pub(super) async fn unarchive_thread(
    State(state): State<RuntimeHttpState>,
    Path(thread_id): Path<String>,
    headers: HeaderMap,
    Json(_body): Json<EmptyLifecycleRequest>,
) -> Result<Json<ThreadUnarchiveResponse>, RuntimeError> {
    let thread_id = authorize_thread_scope(&state, &headers, &thread_id).await?;
    request_typed(
        &state,
        ClientRequest::ThreadUnarchive {
            request_id: lifecycle_request_id("thread-unarchive", thread_id),
            params: ThreadUnarchiveParams {
                thread_id: thread_id.to_string(),
            },
        },
        "invalid_agent_thread_unarchive_response",
    )
    .await
}

pub(super) async fn authorize_thread_scope(
    state: &RuntimeHttpState,
    headers: &HeaderMap,
    thread_id: &str,
) -> Result<ThreadId, RuntimeError> {
    authorize(headers, &state.internal_token)?;
    let thread_id = ThreadId::from_string(thread_id)
        .map_err(|_| RuntimeError::bad_request("invalid_thread_id"))?;
    let identity = PlatformThreadIdentity::new(
        thread_id,
        required_header(headers, TENANT_HEADER)?,
        required_header(headers, USER_HEADER)?,
        required_header(headers, SESSION_HEADER)?,
    );
    state
        .store
        .verify_root_identity(&identity)
        .await
        .map_err(|_| RuntimeError::not_found("thread_not_found"))?;
    Ok(thread_id)
}

async fn request_typed<T>(
    state: &RuntimeHttpState,
    request: ClientRequest,
    invalid_response_code: &'static str,
) -> Result<Json<T>, RuntimeError>
where
    T: serde::de::DeserializeOwned,
{
    let result = state
        .requests
        .request(request)
        .await
        .map_err(|_| RuntimeError::unavailable("agent_kernel_unavailable"))?
        .map_err(|error| {
            warn!(
                provider_code = error.code,
                provider_message = %error.message,
                "Agent rejected a platform thread lifecycle request"
            );
            RuntimeError::bad_request("agent_thread_lifecycle_rejected")
        })?;
    serde_json::from_value(result)
        .map(Json)
        .map_err(|_| RuntimeError::internal(invalid_response_code))
}

fn lifecycle_request_id(operation: &str, thread_id: ThreadId) -> RequestId {
    RequestId::String(format!("{operation}-{thread_id}-{}", Uuid::now_v7()))
}
