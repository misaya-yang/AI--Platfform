use codex_app_server_protocol::ThreadResumeParams;
use codex_protocol::ThreadId;
use pretty_assertions::assert_eq;

use super::ResumeThreadRequest;
use super::resume_params;

#[test]
fn empty_resume_keeps_the_upstream_persisted_metadata_path() {
    let thread_id = ThreadId::new();

    let actual = resume_params(thread_id, ResumeThreadRequest::default())
        .unwrap_or_else(|_| panic!("valid resume"));

    assert_eq!(
        actual,
        ThreadResumeParams {
            thread_id: thread_id.to_string(),
            ..Default::default()
        }
    );
}

#[test]
fn platform_resume_reinstates_the_private_responses_provider() {
    let thread_id = ThreadId::new();

    let actual = resume_params(
        thread_id,
        ResumeThreadRequest {
            model: Some("qwen3.7-plus".to_string()),
            model_plane_base_url: Some(
                "http://gateway:8080/internal/v1/agent-model-plane".to_string(),
            ),
        },
    )
    .unwrap_or_else(|_| panic!("valid platform resume"));

    assert_eq!(actual.thread_id, thread_id.to_string());
    assert_eq!(actual.model.as_deref(), Some("qwen3.7-plus"));
    assert_eq!(
        actual.model_provider.as_deref(),
        Some("ai-platform-gateway")
    );
    let config = actual.config.expect("platform config");
    assert_eq!(config["model"], "qwen3.7-plus");
    assert_eq!(config["model_provider"], "ai-platform-gateway");
    assert_eq!(
        config["model_providers"]["ai-platform-gateway"]["base_url"],
        "http://gateway:8080/internal/v1/agent-model-plane"
    );
    assert_eq!(
        config["model_providers"]["ai-platform-gateway"]["env_key"],
        "AI_PLATFORM_AGENT_RUNTIME_MODEL_PLANE_INTERNAL_TOKEN"
    );
    assert_eq!(
        config["model_providers"]["ai-platform-gateway"]["supports_websockets"],
        false
    );
}

#[test]
fn incomplete_or_untrusted_platform_resume_config_is_rejected() {
    let thread_id = ThreadId::new();
    for request in [
        ResumeThreadRequest {
            model: Some("qwen3.7-plus".to_string()),
            model_plane_base_url: None,
        },
        ResumeThreadRequest {
            model: Some("qwen3.7-plus".to_string()),
            model_plane_base_url: Some(
                "https://user@example.test/internal/v1/agent-model-plane".to_string(),
            ),
        },
        ResumeThreadRequest {
            model: Some("qwen3.7-plus".to_string()),
            model_plane_base_url: Some("https://example.test/v1/responses".to_string()),
        },
        ResumeThreadRequest {
            model: Some("qwen3.7-plus".to_string()),
            model_plane_base_url: Some("http:///internal/v1/agent-model-plane".to_string()),
        },
    ] {
        assert!(resume_params(thread_id, request).is_err());
    }
}
