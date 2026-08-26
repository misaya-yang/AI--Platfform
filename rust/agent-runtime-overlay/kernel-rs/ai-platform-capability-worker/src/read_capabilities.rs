//! Tenant-bound read-only platform capability adapters.

use std::collections::{BTreeMap, BTreeSet};
use std::io::Read;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

#[cfg(unix)]
use std::ffi::{CStr, OsString};
#[cfg(unix)]
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
#[cfg(unix)]
use std::os::unix::ffi::{OsStrExt, OsStringExt};

use ai_platform_capability_contract::canonical_json_hash;
use async_trait::async_trait;
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use globset::{Glob, GlobMatcher};
use hmac::{Hmac, Mac};
use reqwest::{Client, Response, Url};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::RuntimeConnectorBinding;

const MAX_INTERNAL_RESPONSE_BYTES: usize = 2 * 1024 * 1024;
const MAX_MCP_RESPONSE_BYTES: usize = 1024 * 1024;
const MAX_WEB_RESPONSE_BYTES: usize = 2 * 1024 * 1024;
const MAX_FILE_BYTES: usize = 2 * 1024 * 1024;
const MAX_REDIRECTS: usize = 3;
const MAX_GLOB_RESULTS: usize = 1_000;
const MAX_GREP_RESULTS: usize = 500;
const MAX_WALKED_FILES: usize = 20_000;
const CAPABILITY_PROOF_SCHEMA_VERSION: &str = "ai-platform-capability-proof/v1";
const CAPABILITY_PROOF_TTL_SECONDS: u64 = 30;
const MIN_PROOF_SECRET_BYTES: usize = 32;
const WORKING_MEMORY_SCHEMA_VERSION: &str = "assistant-working-memory/v2";
const MAX_WORKING_MEMORY_BYTES: usize = 100_000;
const MAX_WORKING_MEMORY_TASKS: usize = 100;
const MAX_WORKING_MEMORY_TASK_ID_BYTES: usize = 128;
const MAX_WORKING_MEMORY_DESCRIPTION_BYTES: usize = 1_000;
const MAX_WORKING_MEMORY_RESULT_BYTES: usize = 500;
const MAX_WORKING_MEMORY_GOAL_BYTES: usize = 2_000;
const MAX_WORKING_MEMORY_INFO_KEY_BYTES: usize = 128;
const MAX_WORKING_MEMORY_INFO_VALUE_BYTES: usize = 2_000;
const MAX_WORKING_MEMORY_INFO_SOURCE_BYTES: usize = 128;

type HmacSha256 = Hmac<Sha256>;

#[derive(Clone, Debug)]
pub struct ReadCapabilityConfig {
    pub knowledge_base_url: String,
    pub gateway_url: String,
    pub workspace_root: PathBuf,
    pub internal_token: String,
    pub proof_secret: String,
}

impl ReadCapabilityConfig {
    pub fn validate(&self) -> Result<(), ReadCapabilityError> {
        internal_base_url(&self.knowledge_base_url)?;
        internal_base_url(&self.gateway_url)?;
        if !self.workspace_root.is_absolute()
            || self.internal_token.len() < 32
            || self.proof_secret.len() < MIN_PROOF_SECRET_BYTES
        {
            return Err(ReadCapabilityError::Configuration);
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReadCapabilityContext {
    pub tenant_id: String,
    pub user_id: String,
    pub session_id: String,
    pub execution_id: String,
    pub tool_call_id: String,
    pub run_id: String,
    pub capability_revision: u64,
    pub bound_dataset_ids: BTreeSet<String>,
    pub connector_binding: Option<RuntimeConnectorBinding>,
}

impl ReadCapabilityContext {
    fn validate(&self) -> Result<(), ReadCapabilityError> {
        if self.capability_revision == 0
            || [
                self.tenant_id.as_str(),
                self.user_id.as_str(),
                self.session_id.as_str(),
                self.execution_id.as_str(),
                self.tool_call_id.as_str(),
                self.run_id.as_str(),
            ]
            .into_iter()
            .any(|value| {
                value.is_empty()
                    || value.len() > 255
                    || value.bytes().any(|byte| byte.is_ascii_control())
            })
        {
            return Err(ReadCapabilityError::Scope);
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq, thiserror::Error)]
pub enum ReadCapabilityError {
    #[error("read_capability_configuration_invalid")]
    Configuration,
    #[error("read_capability_scope_invalid")]
    Scope,
    #[error("read_capability_arguments_invalid")]
    Arguments,
    #[error("read_capability_not_found")]
    NotFound,
    #[error("read_capability_path_escape")]
    PathEscape,
    #[error("read_capability_ssrf_blocked")]
    SsrfBlocked,
    #[error("read_capability_dns_failed")]
    DnsFailed,
    #[error("read_capability_response_too_large")]
    ResponseTooLarge,
    #[error("read_capability_downstream_rejected")]
    DownstreamRejected,
    #[error("read_capability_downstream_unavailable")]
    DownstreamUnavailable,
    #[error("read_capability_worker_failed")]
    WorkerFailed,
    #[error("read_capability_working_memory_invalid")]
    WorkingMemoryInvalid,
}

#[derive(Clone, Debug)]
pub struct PublicHttpResponse {
    pub status: u16,
    pub content_type: String,
    pub location: Option<String>,
    pub remote_ip: Option<IpAddr>,
    pub bytes: Vec<u8>,
}

#[async_trait]
pub trait ReadHttpAdapter: Send + Sync {
    async fn post_internal_json(
        &self,
        url: Url,
        headers: &[(String, String)],
        body: Value,
    ) -> Result<Value, ReadCapabilityError>;

    async fn get_public(&self, url: Url) -> Result<PublicHttpResponse, ReadCapabilityError>;
}

/// Read-only access to session memory. The caller supplies the owner-bound
/// v2 key; implementations must not fall back to the legacy unscoped key.
#[async_trait]
pub trait SessionMemoryReadAdapter: Send + Sync {
    async fn get_session_memory(
        &self,
        tenant_id: &str,
        session_id: &str,
        key: &str,
    ) -> Result<Option<Value>, ReadCapabilityError>;
}

#[derive(Clone)]
pub struct PostgresSessionMemoryReadAdapter {
    pool: sqlx::PgPool,
}

impl PostgresSessionMemoryReadAdapter {
    pub fn new(pool: sqlx::PgPool) -> Self {
        Self { pool }
    }

    pub fn pool(&self) -> sqlx::PgPool {
        self.pool.clone()
    }
}

#[async_trait]
impl SessionMemoryReadAdapter for PostgresSessionMemoryReadAdapter {
    async fn get_session_memory(
        &self,
        tenant_id: &str,
        session_id: &str,
        key: &str,
    ) -> Result<Option<Value>, ReadCapabilityError> {
        sqlx::query_scalar::<_, Value>(
            "SELECT value FROM session_memory \
             WHERE tenant_id = $1 AND session_id = $2 AND key = $3 \
               AND (expires_at IS NULL OR expires_at > NOW())",
        )
        .bind(tenant_id)
        .bind(session_id)
        .bind(key)
        .fetch_optional(&self.pool)
        .await
        .map_err(|_| ReadCapabilityError::WorkerFailed)
    }
}

#[derive(Clone)]
pub struct ReqwestReadHttpAdapter {
    client: Client,
}

impl ReqwestReadHttpAdapter {
    pub fn new(client: Client) -> Self {
        Self { client }
    }
}

#[async_trait]
impl ReadHttpAdapter for ReqwestReadHttpAdapter {
    async fn post_internal_json(
        &self,
        url: Url,
        headers: &[(String, String)],
        body: Value,
    ) -> Result<Value, ReadCapabilityError> {
        for attempt in 0..2 {
            let mut request = self.client.post(url.clone()).json(&body);
            for (name, value) in headers {
                request = request.header(name, value);
            }
            let response = match request.send().await {
                Ok(response) => response,
                Err(error) if attempt == 0 && error.is_timeout() => continue,
                Err(_) => return Err(ReadCapabilityError::DownstreamUnavailable),
            };
            if response.status().is_server_error() && attempt == 0 {
                continue;
            }
            if !response.status().is_success() {
                return Err(ReadCapabilityError::DownstreamRejected);
            }
            let bytes = bounded_response(response, MAX_INTERNAL_RESPONSE_BYTES).await?;
            return serde_json::from_slice(&bytes)
                .map_err(|_| ReadCapabilityError::DownstreamRejected);
        }
        Err(ReadCapabilityError::DownstreamUnavailable)
    }

    async fn get_public(&self, url: Url) -> Result<PublicHttpResponse, ReadCapabilityError> {
        let host = url.host_str().ok_or(ReadCapabilityError::SsrfBlocked)?;
        let port = url
            .port_or_known_default()
            .ok_or(ReadCapabilityError::SsrfBlocked)?;
        let addresses = tokio::net::lookup_host((host, port))
            .await
            .map_err(|_| ReadCapabilityError::DnsFailed)?
            .collect::<Vec<_>>();
        if addresses.is_empty() || addresses.iter().any(|address| !is_public_ip(address.ip())) {
            return Err(ReadCapabilityError::SsrfBlocked);
        }
        // Disable environment proxies and pin this request to the validated
        // addresses. Otherwise a proxy can turn a public peer connection into
        // an internal fetch, or DNS can change between validation and connect.
        let client = Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(30))
            .resolve_to_addrs(host, &addresses)
            .build()
            .map_err(|_| ReadCapabilityError::Configuration)?;
        let response = client
            .get(url)
            .header(
                reqwest::header::USER_AGENT,
                "Mozilla/5.0 (compatible; AI-Gateway-web_fetch/2.0)",
            )
            .header(
                reqwest::header::ACCEPT,
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            )
            .send()
            .await
            .map_err(|_| ReadCapabilityError::DownstreamUnavailable)?;
        let status = response.status().as_u16();
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("application/octet-stream")
            .chars()
            .take(128)
            .collect();
        let location = response
            .headers()
            .get(reqwest::header::LOCATION)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        let remote_ip = resolved_peer_ip(
            response.remote_addr().map(|address| address.ip()),
            &addresses,
        );
        let bytes = bounded_response(response, MAX_WEB_RESPONSE_BYTES).await?;
        Ok(PublicHttpResponse {
            status,
            content_type,
            location,
            remote_ip,
            bytes,
        })
    }
}

async fn bounded_response(
    mut response: Response,
    limit: usize,
) -> Result<Vec<u8>, ReadCapabilityError> {
    if response
        .content_length()
        .is_some_and(|length| length > limit as u64)
    {
        return Err(ReadCapabilityError::ResponseTooLarge);
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| ReadCapabilityError::DownstreamUnavailable)?
    {
        if bytes.len().saturating_add(chunk.len()) > limit {
            return Err(ReadCapabilityError::ResponseTooLarge);
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}

#[derive(Clone)]
pub struct ReadCapabilityExecutor {
    config: ReadCapabilityConfig,
    http: Arc<dyn ReadHttpAdapter>,
    session_memory: Option<Arc<dyn SessionMemoryReadAdapter>>,
}

impl ReadCapabilityExecutor {
    pub fn new(
        config: ReadCapabilityConfig,
        http: Arc<dyn ReadHttpAdapter>,
    ) -> Result<Self, ReadCapabilityError> {
        config.validate()?;
        Ok(Self {
            config,
            http,
            session_memory: None,
        })
    }

    pub fn with_session_memory(
        mut self,
        session_memory: Arc<dyn SessionMemoryReadAdapter>,
    ) -> Self {
        self.session_memory = Some(session_memory);
        self
    }

    pub async fn execute(
        &self,
        capability_id: &str,
        context: &ReadCapabilityContext,
        arguments: Value,
    ) -> Result<Value, ReadCapabilityError> {
        context.validate()?;
        if !arguments.is_object() {
            return Err(ReadCapabilityError::Arguments);
        }
        if context
            .connector_binding
            .as_ref()
            .is_some_and(|binding| binding.provider == "mcp" && binding.tool_name == capability_id)
        {
            return self.mcp_read(context, capability_id, &arguments).await;
        }
        match capability_id {
            "search_knowledge_base" => self.search_knowledge_base(context, &arguments).await,
            "web_fetch" => self.web_fetch(&arguments).await,
            "search_web" => self.web_search(context, &arguments).await,
            "fs_read" => self.fs_read(context, &arguments).await,
            "fs_glob" => self.fs_glob(context, &arguments).await,
            "fs_grep" => self.fs_grep(context, &arguments).await,
            "read_tool_artifact" => self.read_tool_artifact(context, &arguments).await,
            "confluence_read" => self.confluence_read(context, &arguments).await,
            "todo_read" => self.todo_read(context).await,
            _ => Err(ReadCapabilityError::NotFound),
        }
    }

    async fn todo_read(
        &self,
        context: &ReadCapabilityContext,
    ) -> Result<Value, ReadCapabilityError> {
        let memory = self
            .session_memory
            .as_ref()
            .ok_or(ReadCapabilityError::WorkerFailed)?;
        let key = working_memory_key(&context.tenant_id, &context.user_id, &context.session_id);
        let envelope = memory
            .get_session_memory(&context.tenant_id, &context.session_id, &key)
            .await?;
        let Some(envelope) = envelope else {
            return Ok(todo_read_empty());
        };
        render_working_memory(&envelope, context)
    }

    async fn search_knowledge_base(
        &self,
        context: &ReadCapabilityContext,
        arguments: &Value,
    ) -> Result<Value, ReadCapabilityError> {
        let query = string_argument(arguments, "query", 1, 4_096)?;
        let intent = arguments
            .get("intent")
            .and_then(Value::as_str)
            .unwrap_or("general");
        if !matches!(intent, "general" | "find_document") {
            return Err(ReadCapabilityError::Arguments);
        }
        let dataset_ids = match arguments.get("dataset_ids") {
            Some(_) => string_array_argument(arguments, "dataset_ids", 8, 128)?,
            None => context.bound_dataset_ids.iter().cloned().collect(),
        };
        if dataset_ids.is_empty()
            || dataset_ids.iter().any(|dataset_id| {
                !valid_dataset_id(dataset_id) || !context.bound_dataset_ids.contains(dataset_id)
            })
        {
            return Err(ReadCapabilityError::Scope);
        }
        let top_k = integer_argument(arguments, "top_k", 5, 1, 20)?;
        let score_threshold = number_argument(arguments, "score_threshold", 0.0, 0.0, 1.0)?;
        let mut results = Vec::new();
        let mut metadata = BTreeMap::new();
        for dataset_id in &dataset_ids {
            let url = internal_endpoint(
                &self.config.knowledge_base_url,
                &format!("internal/v2/capabilities/knowledge/{dataset_id}/retrieve"),
            )?;
            let body = json!({
                "query": query,
                "top_k": top_k,
                "threshold": score_threshold
            });
            let headers = self.internal_headers(context, &url, &body)?;
            let value = self.http.post_internal_json(url, &headers, body).await?;
            results.extend(
                value
                    .get("results")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default(),
            );
            metadata.insert(
                dataset_id.clone(),
                value.get("metadata").cloned().unwrap_or_else(|| json!({})),
            );
        }
        Ok(json!({
            "query": query,
            "intent": intent,
            "dataset_ids": dataset_ids,
            "results": results,
            "metadata": metadata
        }))
    }

    async fn web_fetch(&self, arguments: &Value) -> Result<Value, ReadCapabilityError> {
        let raw_url = string_argument(arguments, "url", 1, 4_096)?;
        let max_chars = integer_argument(arguments, "max_chars", 8_000, 1, 32_000)? as usize;
        let extract = arguments
            .get("extract")
            .and_then(Value::as_str)
            .unwrap_or("markdown");
        if !matches!(extract, "markdown" | "text" | "raw") {
            return Err(ReadCapabilityError::Arguments);
        }
        let mut current = validated_public_url(raw_url).await?;
        for redirect in 0..=MAX_REDIRECTS {
            let response = self.http.get_public(current.clone()).await?;
            match response.remote_ip {
                Some(remote_ip) if is_public_ip(remote_ip) => {}
                _ => return Err(ReadCapabilityError::SsrfBlocked),
            }
            if (300..400).contains(&response.status) {
                if redirect == MAX_REDIRECTS {
                    return Err(ReadCapabilityError::DownstreamRejected);
                }
                let location = response
                    .location
                    .ok_or(ReadCapabilityError::DownstreamRejected)?;
                current = validated_public_url(
                    current
                        .join(&location)
                        .map_err(|_| ReadCapabilityError::SsrfBlocked)?
                        .as_str(),
                )
                .await?;
                continue;
            }
            if !(200..300).contains(&response.status) {
                return Err(ReadCapabilityError::DownstreamRejected);
            }
            let raw = String::from_utf8_lossy(&response.bytes);
            let extracted = if extract == "raw" {
                raw.into_owned()
            } else {
                strip_html(&raw)
            };
            let content: String = extracted.chars().take(max_chars).collect();
            return Ok(json!({
                "url": current.as_str(),
                "status": response.status,
                "content_type": response.content_type,
                "content": content,
                "truncated": extracted.chars().count() > content.chars().count()
            }));
        }
        Err(ReadCapabilityError::DownstreamRejected)
    }

    async fn web_search(
        &self,
        context: &ReadCapabilityContext,
        arguments: &Value,
    ) -> Result<Value, ReadCapabilityError> {
        let queries = string_array_argument(arguments, "queries", 5, 500)?;
        if queries.is_empty() {
            return Err(ReadCapabilityError::Arguments);
        }
        let max_results = integer_argument(arguments, "max_results", 5, 1, 10)?;
        let url = internal_endpoint(
            &self.config.gateway_url,
            "internal/v2/agent-capabilities/web-search",
        )?;
        let body = json!({"queries": queries, "max_results": max_results});
        let headers = self.internal_headers(context, &url, &body)?;
        self.http.post_internal_json(url, &headers, body).await
    }

    async fn fs_read(
        &self,
        context: &ReadCapabilityContext,
        arguments: &Value,
    ) -> Result<Value, ReadCapabilityError> {
        let root = self.scoped_workspace_root(context);
        let path = string_argument(arguments, "path", 1, 1_024)?.to_string();
        let offset = integer_argument(arguments, "offset", 0, 0, 1_000_000)? as usize;
        let limit = integer_argument(arguments, "limit", 2_000, 1, 20_000)? as usize;
        tokio::task::spawn_blocking(move || read_file(&root, &path, offset, limit))
            .await
            .map_err(|_| ReadCapabilityError::WorkerFailed)?
    }

    async fn fs_glob(
        &self,
        context: &ReadCapabilityContext,
        arguments: &Value,
    ) -> Result<Value, ReadCapabilityError> {
        let root = self.scoped_workspace_root(context);
        let pattern = string_argument(arguments, "pattern", 1, 1_024)?.to_string();
        tokio::task::spawn_blocking(move || glob_files(&root, &pattern))
            .await
            .map_err(|_| ReadCapabilityError::WorkerFailed)?
    }

    async fn fs_grep(
        &self,
        context: &ReadCapabilityContext,
        arguments: &Value,
    ) -> Result<Value, ReadCapabilityError> {
        let root = self.scoped_workspace_root(context);
        let pattern = string_argument(arguments, "pattern", 1, 1_024)?.to_string();
        let glob = arguments
            .get("glob")
            .and_then(Value::as_str)
            .unwrap_or("**/*")
            .to_string();
        let case_sensitive = arguments
            .get("case_sensitive")
            .and_then(Value::as_bool)
            .unwrap_or(true);
        tokio::task::spawn_blocking(move || grep_files(&root, &pattern, &glob, case_sensitive))
            .await
            .map_err(|_| ReadCapabilityError::WorkerFailed)?
    }

    async fn read_tool_artifact(
        &self,
        context: &ReadCapabilityContext,
        arguments: &Value,
    ) -> Result<Value, ReadCapabilityError> {
        let artifact_id = string_argument(arguments, "artifact_id", 12, 68)?;
        if !artifact_id.starts_with("art_")
            || !artifact_id[4..]
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric())
        {
            return Err(ReadCapabilityError::Arguments);
        }
        let offset = integer_argument(arguments, "offset", 0, 0, 2_000_000)?;
        let limit = integer_argument(arguments, "limit", 4_000, 1, 4_000)?;
        let url = internal_endpoint(
            &self.config.gateway_url,
            &format!("internal/v2/agent-capabilities/artifacts/{artifact_id}/read"),
        )?;
        let body = json!({"offset": offset, "limit": limit});
        let headers = self.internal_headers(context, &url, &body)?;
        self.http.post_internal_json(url, &headers, body).await
    }

    async fn confluence_read(
        &self,
        context: &ReadCapabilityContext,
        arguments: &Value,
    ) -> Result<Value, ReadCapabilityError> {
        let action = arguments
            .get("action")
            .and_then(Value::as_str)
            .ok_or(ReadCapabilityError::Arguments)?;
        if !matches!(
            action,
            "search" | "read_page" | "list_spaces" | "get_space" | "list_children"
        ) {
            return Err(ReadCapabilityError::Arguments);
        }
        let binding = context
            .connector_binding
            .as_ref()
            .filter(|binding| {
                binding.provider == "confluence"
                    && binding.tool_name == "confluence_read"
                    && !binding.channel.is_empty()
            })
            .ok_or(ReadCapabilityError::Scope)?;
        let url = internal_endpoint(
            &self.config.gateway_url,
            "internal/v2/agent-capabilities/confluence/read",
        )?;
        let envelope = json!({
            "arguments": arguments,
            "binding": binding,
        });
        let headers = self.internal_headers(context, &url, &envelope)?;
        self.http.post_internal_json(url, &headers, envelope).await
    }

    async fn mcp_read(
        &self,
        context: &ReadCapabilityContext,
        capability_id: &str,
        arguments: &Value,
    ) -> Result<Value, ReadCapabilityError> {
        let binding = context
            .connector_binding
            .as_ref()
            .filter(|binding| binding.provider == "mcp" && binding.tool_name == capability_id)
            .ok_or(ReadCapabilityError::Scope)?;
        let connection_id = binding
            .connection_id
            .as_deref()
            .filter(|value| !value.is_empty())
            .ok_or(ReadCapabilityError::Scope)?;
        let schema_hash = binding
            .schema_hash
            .as_deref()
            .filter(|value| value.starts_with("sha256:"))
            .ok_or(ReadCapabilityError::Scope)?;
        let principal_type = binding
            .principal_type
            .as_deref()
            .filter(|value| matches!(*value, "service_account" | "user_delegated"))
            .ok_or(ReadCapabilityError::Scope)?;
        let url = internal_endpoint(
            &self.config.gateway_url,
            "internal/v2/agent-capabilities/mcp/read",
        )?;
        let envelope = json!({
            "connection_id": connection_id,
            "principal_type": principal_type,
            "channel": binding.channel,
            "runtime_name": capability_id,
            "schema_hash": schema_hash,
            "risk_level": binding.risk_level.as_deref().unwrap_or("medium"),
            "arguments": arguments,
            "arguments_hash": canonical_json_hash(arguments)
                .map_err(|_| ReadCapabilityError::Arguments)?,
        });
        let headers = self.internal_headers(context, &url, &envelope)?;
        let result = self
            .http
            .post_internal_json(url, &headers, envelope)
            .await?;
        if serde_json::to_vec(&result)
            .map_err(|_| ReadCapabilityError::DownstreamRejected)?
            .len()
            > MAX_MCP_RESPONSE_BYTES
        {
            return Err(ReadCapabilityError::ResponseTooLarge);
        }
        Ok(result)
    }

    fn internal_headers(
        &self,
        context: &ReadCapabilityContext,
        url: &Url,
        body: &Value,
    ) -> Result<Vec<(String, String)>, ReadCapabilityError> {
        let proof = sign_capability_proof(
            &self.config.proof_secret,
            CapabilityProofInput {
                method: "POST",
                path: url.path(),
                body,
                context,
                now: current_epoch_seconds()?,
                nonce: Uuid::now_v7().simple().to_string(),
            },
        )?;
        Ok(vec![
            (
                "x-ai-platform-internal-token".to_string(),
                self.config.internal_token.clone(),
            ),
            ("x-ai-tenant-id".to_string(), context.tenant_id.clone()),
            ("x-ai-user-id".to_string(), context.user_id.clone()),
            ("x-ai-session-id".to_string(), context.session_id.clone()),
            ("x-ai-capability-proof".to_string(), proof),
            (
                "x-ai-execution-id".to_string(),
                context.execution_id.clone(),
            ),
            ("x-ai-run-id".to_string(), context.run_id.clone()),
            (
                "x-ai-tool-call-id".to_string(),
                context.tool_call_id.clone(),
            ),
        ])
    }

    fn scoped_workspace_root(&self, context: &ReadCapabilityContext) -> PathBuf {
        let mut digest = Sha256::new();
        digest.update(context.tenant_id.as_bytes());
        digest.update(b"\0");
        digest.update(context.user_id.as_bytes());
        digest.update(b"\0");
        digest.update(context.session_id.as_bytes());
        self.config
            .workspace_root
            .join(format!("{:x}", digest.finalize()))
    }
}

/// The v2 key is intentionally byte-for-byte compatible with Python's
/// `_scope_digest`: each UTF-8 component is length-prefixed as an unsigned
/// big-endian u64 before being hashed.
pub fn working_memory_key(tenant_id: &str, user_id: &str, session_id: &str) -> String {
    let mut digest = Sha256::new();
    for value in [tenant_id, user_id, session_id] {
        let bytes = value.as_bytes();
        digest.update((bytes.len() as u64).to_be_bytes());
        digest.update(bytes);
    }
    format!("working_memory:{:x}", digest.finalize())
}

fn todo_read_empty() -> Value {
    json!({
        "markdown": "(no tasks)",
        "task_count": 0,
        "progress": {
            "total": 0,
            "completed": 0,
            "failed": 0,
            "percentage": 0
        }
    })
}

pub(crate) fn render_working_memory(
    envelope: &Value,
    context: &ReadCapabilityContext,
) -> Result<Value, ReadCapabilityError> {
    if serde_json::to_string(envelope)
        .map_err(|_| ReadCapabilityError::WorkingMemoryInvalid)?
        .chars()
        .count()
        > MAX_WORKING_MEMORY_BYTES
    {
        return Err(ReadCapabilityError::WorkingMemoryInvalid);
    }
    let envelope = envelope
        .as_object()
        .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
    require_exact_keys(
        envelope,
        &["schema_version", "owner_scope", "working_memory"],
    )?;
    if envelope.get("schema_version").and_then(Value::as_str) != Some(WORKING_MEMORY_SCHEMA_VERSION)
    {
        return Err(ReadCapabilityError::WorkingMemoryInvalid);
    }
    let expected_scope =
        working_memory_scope(&context.tenant_id, &context.user_id, &context.session_id);
    if envelope.get("owner_scope").and_then(Value::as_str) != Some(expected_scope.as_str()) {
        return Err(ReadCapabilityError::WorkingMemoryInvalid);
    }
    let memory = envelope
        .get("working_memory")
        .and_then(Value::as_object)
        .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
    validate_working_memory(memory, &context.session_id)?;
    let tasks = memory
        .get("tasks")
        .and_then(Value::as_array)
        .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
    if tasks.is_empty() {
        return Ok(todo_read_empty());
    }

    let mut completed = 0usize;
    let mut failed = 0usize;
    let mut markdown = String::from("# Current Task State\n\n");
    if let Some(goal) = memory.get("goal").and_then(Value::as_str)
        && !goal.is_empty()
    {
        markdown.push_str("**Goal:** ");
        markdown.push_str(&markdown_inline(goal));
        markdown.push_str("\n\n");
    }
    markdown.push_str("## Tasks\n");
    for task in tasks {
        let task = task
            .as_object()
            .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
        let status = task
            .get("status")
            .and_then(Value::as_str)
            .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
        let description = task
            .get("description")
            .and_then(Value::as_str)
            .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
        let indicator = match status {
            "pending" => "[ ]",
            "in_progress" => "[~]",
            "completed" => {
                completed += 1;
                "[x]"
            }
            "failed" => {
                failed += 1;
                "[!]"
            }
            "blocked" => "[B]",
            _ => return Err(ReadCapabilityError::WorkingMemoryInvalid),
        };
        markdown.push_str("- ");
        markdown.push_str(indicator);
        markdown.push(' ');
        markdown.push_str(&markdown_inline(description));
        if status == "in_progress" {
            markdown.push_str(" <- current");
        }
        if let Some(error) = task.get("error").and_then(Value::as_str)
            && !error.is_empty()
        {
            markdown.push_str(" (error: ");
            markdown.push_str(&markdown_inline(error));
            markdown.push(')');
        }
        markdown.push('\n');
    }
    markdown.push('\n');

    if let Some(information) = memory.get("collected_info").and_then(Value::as_array)
        && !information.is_empty()
    {
        markdown.push_str("## Collected Information\n");
        for item in information {
            let item = item
                .as_object()
                .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
            let key = item
                .get("key")
                .and_then(Value::as_str)
                .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
            let value = item
                .get("value")
                .and_then(Value::as_str)
                .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
            let display = if value.chars().count() > 100 {
                format!("{}...", value.chars().take(100).collect::<String>())
            } else {
                value.to_string()
            };
            markdown.push_str("- **");
            markdown.push_str(&markdown_inline(key));
            markdown.push_str("**: ");
            markdown.push_str(&markdown_inline(&display));
            markdown.push('\n');
        }
        markdown.push('\n');
    }
    if let Some(notes) = memory.get("notes").and_then(Value::as_array)
        && !notes.is_empty()
    {
        markdown.push_str("## Notes\n");
        for note in notes {
            markdown.push_str("- ");
            markdown.push_str(&markdown_inline(
                note.as_str()
                    .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?,
            ));
            markdown.push('\n');
        }
        markdown.push('\n');
    }
    let total = tasks.len();
    Ok(json!({
        "markdown": markdown.trim_end_matches('\n'),
        "task_count": total,
        "progress": {
            "total": total,
            "completed": completed,
            "failed": failed,
            "percentage": (completed as f64 / total as f64) * 100.0
        }
    }))
}

pub(crate) fn validate_working_memory(
    memory: &serde_json::Map<String, Value>,
    session_id: &str,
) -> Result<(), ReadCapabilityError> {
    const KEYS: &[&str] = &[
        "session_id",
        "goal",
        "goal_set_at",
        "turns_since_goal",
        "tasks",
        "collected_info",
        "notes",
        "archived",
    ];
    if memory.keys().any(|key| !KEYS.contains(&key.as_str()))
        || memory.get("session_id").and_then(Value::as_str) != Some(session_id)
    {
        return Err(ReadCapabilityError::WorkingMemoryInvalid);
    }
    if let Some(goal) = memory.get("goal")
        && !goal.is_null()
        && !goal
            .as_str()
            .is_some_and(|value| valid_memory_text(value, 1, MAX_WORKING_MEMORY_GOAL_BYTES))
    {
        return Err(ReadCapabilityError::WorkingMemoryInvalid);
    }
    for field in ["goal_set_at"] {
        if let Some(value) = memory.get(field)
            && !value.is_null()
            && value.as_str().is_none_or(|text| !valid_text(text, 1, 128))
        {
            return Err(ReadCapabilityError::WorkingMemoryInvalid);
        }
    }
    if let Some(turns) = memory.get("turns_since_goal")
        && turns.as_u64().is_none_or(|value| value > 1_000_000)
    {
        return Err(ReadCapabilityError::WorkingMemoryInvalid);
    }
    let tasks = memory
        .get("tasks")
        .and_then(Value::as_array)
        .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
    if tasks.len() > MAX_WORKING_MEMORY_TASKS {
        return Err(ReadCapabilityError::WorkingMemoryInvalid);
    }
    let mut ids = BTreeSet::new();
    for task in tasks {
        let task = task
            .as_object()
            .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
        if task.keys().any(|key| {
            ![
                "id",
                "description",
                "status",
                "result",
                "error",
                "created_at",
                "completed_at",
            ]
            .contains(&key.as_str())
        }) {
            return Err(ReadCapabilityError::WorkingMemoryInvalid);
        }
        let id = task
            .get("id")
            .and_then(Value::as_str)
            .filter(|value| valid_text(value, 1, MAX_WORKING_MEMORY_TASK_ID_BYTES))
            .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
        if !ids.insert(id.to_string()) {
            return Err(ReadCapabilityError::WorkingMemoryInvalid);
        }
        if task
            .get("description")
            .and_then(Value::as_str)
            .is_none_or(|value| !valid_memory_text(value, 1, MAX_WORKING_MEMORY_DESCRIPTION_BYTES))
        {
            return Err(ReadCapabilityError::WorkingMemoryInvalid);
        }
        if task
            .get("status")
            .and_then(Value::as_str)
            .is_none_or(|value| {
                !matches!(
                    value,
                    "pending" | "in_progress" | "completed" | "failed" | "blocked"
                )
            })
        {
            return Err(ReadCapabilityError::WorkingMemoryInvalid);
        }
        for field in ["result", "error"] {
            if let Some(value) = task.get(field)
                && !value.is_null()
                && value
                    .as_str()
                    .is_none_or(|text| !valid_memory_text(text, 1, MAX_WORKING_MEMORY_RESULT_BYTES))
            {
                return Err(ReadCapabilityError::WorkingMemoryInvalid);
            }
        }
        for field in ["created_at", "completed_at"] {
            if let Some(value) = task.get(field)
                && !value.is_null()
                && value.as_str().is_none_or(|text| !valid_text(text, 1, 128))
            {
                return Err(ReadCapabilityError::WorkingMemoryInvalid);
            }
        }
    }
    if let Some(information) = memory.get("collected_info") {
        let information = information
            .as_array()
            .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
        if information.len() > MAX_WORKING_MEMORY_TASKS {
            return Err(ReadCapabilityError::WorkingMemoryInvalid);
        }
        for item in information {
            let item = item
                .as_object()
                .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
            if item
                .keys()
                .any(|key| !["key", "value", "source", "timestamp"].contains(&key.as_str()))
            {
                return Err(ReadCapabilityError::WorkingMemoryInvalid);
            }
            for (field, max) in [
                ("key", MAX_WORKING_MEMORY_INFO_KEY_BYTES),
                ("value", MAX_WORKING_MEMORY_INFO_VALUE_BYTES),
                ("source", MAX_WORKING_MEMORY_INFO_SOURCE_BYTES),
                ("timestamp", 128),
            ] {
                let validator: fn(&str, usize, usize) -> bool = if field == "value" {
                    valid_memory_text
                } else {
                    valid_text
                };
                if item
                    .get(field)
                    .and_then(Value::as_str)
                    .is_none_or(|text| !validator(text, 1, max))
                {
                    return Err(ReadCapabilityError::WorkingMemoryInvalid);
                }
            }
        }
    }
    if let Some(notes) = memory.get("notes") {
        let notes = notes
            .as_array()
            .ok_or(ReadCapabilityError::WorkingMemoryInvalid)?;
        if notes.len() > MAX_WORKING_MEMORY_TASKS
            || notes.iter().any(|note| {
                note.as_str().is_none_or(|text| {
                    !valid_memory_text(text, 1, MAX_WORKING_MEMORY_DESCRIPTION_BYTES)
                })
            })
        {
            return Err(ReadCapabilityError::WorkingMemoryInvalid);
        }
    }
    if let Some(archived) = memory.get("archived")
        && !archived.is_null()
        && !archived.is_object()
    {
        return Err(ReadCapabilityError::WorkingMemoryInvalid);
    }
    Ok(())
}

fn require_exact_keys(
    object: &serde_json::Map<String, Value>,
    required: &[&str],
) -> Result<(), ReadCapabilityError> {
    if object.len() != required.len() || required.iter().any(|key| !object.contains_key(*key)) {
        return Err(ReadCapabilityError::WorkingMemoryInvalid);
    }
    Ok(())
}

fn valid_text(value: &str, min: usize, max: usize) -> bool {
    (min..=max).contains(&value.chars().count())
        && !value.bytes().any(|byte| byte.is_ascii_control())
}

fn valid_memory_text(value: &str, min: usize, max: usize) -> bool {
    (min..=max).contains(&value.chars().count())
        && !value
            .bytes()
            .any(|byte| byte.is_ascii_control() && byte != b'\n')
}

fn markdown_inline(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn working_memory_scope(tenant_id: &str, user_id: &str, session_id: &str) -> String {
    working_memory_key(tenant_id, user_id, session_id)
        .strip_prefix("working_memory:")
        .unwrap_or_default()
        .to_string()
}

fn current_epoch_seconds() -> Result<u64, ReadCapabilityError> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .map_err(|_| ReadCapabilityError::Configuration)
}

/// Sign the exact downstream request that will be sent to a trusted platform
/// service. The envelope and its body hash intentionally match the Python
/// `capability_proof.py` implementation byte-for-byte.
struct CapabilityProofInput<'a> {
    method: &'a str,
    path: &'a str,
    body: &'a Value,
    context: &'a ReadCapabilityContext,
    now: u64,
    nonce: String,
}

fn sign_capability_proof(
    secret: &str,
    input: CapabilityProofInput<'_>,
) -> Result<String, ReadCapabilityError> {
    if secret.len() < MIN_PROOF_SECRET_BYTES || input.now == 0 || input.nonce.is_empty() {
        return Err(ReadCapabilityError::Configuration);
    }
    let expires_at = input
        .now
        .checked_add(CAPABILITY_PROOF_TTL_SECONDS)
        .ok_or(ReadCapabilityError::Configuration)?;
    let body_sha256 = canonical_json_hash(input.body)
        .map_err(|_| ReadCapabilityError::Configuration)?
        .strip_prefix("sha256:")
        .map(str::to_owned)
        .ok_or(ReadCapabilityError::Configuration)?;

    let mut unsigned = BTreeMap::<String, Value>::new();
    unsigned.insert("body_sha256".to_string(), Value::String(body_sha256));
    unsigned.insert(
        "execution_id".to_string(),
        Value::String(input.context.execution_id.clone()),
    );
    unsigned.insert("expires_at".to_string(), json!(expires_at));
    unsigned.insert(
        "method".to_string(),
        Value::String(input.method.trim().to_ascii_uppercase()),
    );
    unsigned.insert("nonce".to_string(), Value::String(input.nonce));
    unsigned.insert(
        "path".to_string(),
        Value::String(input.path.trim().to_string()),
    );
    unsigned.insert(
        "run_id".to_string(),
        Value::String(input.context.run_id.clone()),
    );
    unsigned.insert(
        "schema_version".to_string(),
        Value::String(CAPABILITY_PROOF_SCHEMA_VERSION.to_string()),
    );
    unsigned.insert(
        "session_id".to_string(),
        Value::String(input.context.session_id.clone()),
    );
    unsigned.insert(
        "tenant_id".to_string(),
        Value::String(input.context.tenant_id.clone()),
    );
    unsigned.insert(
        "user_id".to_string(),
        Value::String(input.context.user_id.clone()),
    );

    let unsigned_bytes =
        serde_json::to_vec(&unsigned).map_err(|_| ReadCapabilityError::Configuration)?;
    let mut mac = HmacSha256::new_from_slice(secret.as_bytes())
        .map_err(|_| ReadCapabilityError::Configuration)?;
    mac.update(&unsigned_bytes);
    let signature = hex::encode(mac.finalize().into_bytes());

    let mut envelope = unsigned;
    envelope.insert("signature".to_string(), Value::String(signature));
    let envelope_bytes =
        serde_json::to_vec(&envelope).map_err(|_| ReadCapabilityError::Configuration)?;
    Ok(format!("v1.{}", URL_SAFE_NO_PAD.encode(envelope_bytes)))
}

fn internal_base_url(value: &str) -> Result<Url, ReadCapabilityError> {
    let url = Url::parse(value).map_err(|_| ReadCapabilityError::Configuration)?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(ReadCapabilityError::Configuration);
    }
    Ok(url)
}

fn internal_endpoint(base: &str, path: &str) -> Result<Url, ReadCapabilityError> {
    let mut base = internal_base_url(base)?;
    if !base.path().ends_with('/') {
        base.set_path(&format!("{}/", base.path()));
    }
    base.join(path)
        .map_err(|_| ReadCapabilityError::Configuration)
}

async fn validated_public_url(value: &str) -> Result<Url, ReadCapabilityError> {
    let url = Url::parse(value).map_err(|_| ReadCapabilityError::SsrfBlocked)?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err(ReadCapabilityError::SsrfBlocked);
    }
    let host = url.host_str().ok_or(ReadCapabilityError::SsrfBlocked)?;
    if host.eq_ignore_ascii_case("localhost") {
        return Err(ReadCapabilityError::SsrfBlocked);
    }
    if let Ok(ip) = host.parse::<IpAddr>() {
        if !is_public_ip(ip) {
            return Err(ReadCapabilityError::SsrfBlocked);
        }
        return Ok(url);
    }
    let port = url
        .port_or_known_default()
        .ok_or(ReadCapabilityError::SsrfBlocked)?;
    let addresses: Vec<_> = tokio::net::lookup_host((host, port))
        .await
        .map_err(|_| ReadCapabilityError::DnsFailed)?
        .collect();
    if addresses.is_empty() || addresses.iter().any(|address| !is_public_ip(address.ip())) {
        return Err(ReadCapabilityError::SsrfBlocked);
    }
    Ok(url)
}

fn resolved_peer_ip(remote: Option<IpAddr>, pinned: &[std::net::SocketAddr]) -> Option<IpAddr> {
    // `resolve_to_addrs` pins the TCP peer. Some HTTP stacks then leave
    // `remote_addr()` empty; falling back to the already-validated pin keeps
    // SSRF closed without rejecting a successful public fetch.
    remote
        .filter(|ip| is_public_ip(*ip))
        .or_else(|| pinned.iter().map(std::net::SocketAddr::ip).find(|ip| is_public_ip(*ip)))
}

fn is_public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => is_public_ipv4(ip),
        IpAddr::V6(ip) => is_public_ipv6(ip),
    }
}

fn is_public_ipv4(ip: Ipv4Addr) -> bool {
    let [a, b, _, _] = ip.octets();
    !(ip.is_private()
        || ip.is_loopback()
        || ip.is_link_local()
        || ip.is_unspecified()
        || ip.is_multicast()
        || ip.is_broadcast()
        || ip.is_documentation()
        || a == 0
        || (a == 100 && (64..=127).contains(&b))
        || (a == 192 && b == 0)
        || (a == 198 && matches!(b, 18 | 19))
        || a >= 240)
}

fn is_public_ipv6(ip: Ipv6Addr) -> bool {
    let first = ip.segments()[0];
    !(ip.is_loopback()
        || ip.is_unspecified()
        || ip.is_multicast()
        || (first & 0xfe00) == 0xfc00
        || (first & 0xffc0) == 0xfe80
        || (ip.segments()[0] == 0x2001 && ip.segments()[1] == 0x0db8))
}

#[cfg(test)]
mod capability_proof_tests {
    use std::collections::BTreeSet;

    use super::{CapabilityProofInput, ReadCapabilityContext, sign_capability_proof};
    use serde_json::json;

    #[test]
    fn pinned_public_peer_survives_missing_remote_addr() {
        let pinned = "1.1.1.1:443".parse::<std::net::SocketAddr>().unwrap();
        assert_eq!(
            super::resolved_peer_ip(None, &[pinned]),
            Some(std::net::IpAddr::V4(std::net::Ipv4Addr::new(1, 1, 1, 1)))
        );
        assert_eq!(
            super::resolved_peer_ip(Some(std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)), &[pinned]),
            Some(std::net::IpAddr::V4(std::net::Ipv4Addr::new(1, 1, 1, 1)))
        );
    }

    #[test]
    fn matches_python_capability_proof_fixture() {
        let body = json!({"query": "退款", "top_k": 5, "threshold": 0.0});
        let context = ReadCapabilityContext {
            tenant_id: "tenant-a".to_string(),
            user_id: "user-a".to_string(),
            session_id: "session-a".to_string(),
            execution_id: "exec_01".to_string(),
            tool_call_id: "tool-call-01".to_string(),
            run_id: "run_01".to_string(),
            capability_revision: 1,
            bound_dataset_ids: BTreeSet::new(),
            connector_binding: None,
        };
        let header = sign_capability_proof(
            "capability-proof-secret-012345678901234567890123",
            CapabilityProofInput {
                method: "post",
                path: " /internal/v2/capabilities/knowledge/docs/retrieve ",
                body: &body,
                context: &context,
                now: 1_700_000_000,
                nonce: "nonce-fixed-0123456789".to_string(),
            },
        )
        .expect("fixture proof should sign");
        assert_eq!(
            header,
            "v1.eyJib2R5X3NoYTI1NiI6ImU2NTk3MGI3YzM2NWE4NmI2YzZkZGVjZDNhODllOGRlMzZkMzlhNzE5YTlhYjFmNGMwZGQ0YWI0MmMwNjYxMmEiLCJleGVjdXRpb25faWQiOiJleGVjXzAxIiwiZXhwaXJlc19hdCI6MTcwMDAwMDAzMCwibWV0aG9kIjoiUE9TVCIsIm5vbmNlIjoibm9uY2UtZml4ZWQtMDEyMzQ1Njc4OSIsInBhdGgiOiIvaW50ZXJuYWwvdjIvY2FwYWJpbGl0aWVzL2tub3dsZWRnZS9kb2NzL3JldHJpZXZlIiwicnVuX2lkIjoicnVuXzAxIiwic2NoZW1hX3ZlcnNpb24iOiJhaS1wbGF0Zm9ybS1jYXBhYmlsaXR5LXByb29mL3YxIiwic2Vzc2lvbl9pZCI6InNlc3Npb24tYSIsInNpZ25hdHVyZSI6IjUyNjBhNjExOTQyNTAxMzg1ODY2YmI0MTc1OGRlNjQ5OGY4NjE1MjdlYjNiNTExZTdkNWRiYmE5M2I4NjlhZmEiLCJ0ZW5hbnRfaWQiOiJ0ZW5hbnQtYSIsInVzZXJfaWQiOiJ1c2VyLWEifQ"
        );
    }
}

fn string_argument<'a>(
    value: &'a Value,
    name: &str,
    minimum: usize,
    maximum: usize,
) -> Result<&'a str, ReadCapabilityError> {
    value
        .get(name)
        .and_then(Value::as_str)
        .filter(|item| {
            (minimum..=maximum).contains(&item.len())
                && !item.bytes().any(|byte| byte.is_ascii_control())
        })
        .ok_or(ReadCapabilityError::Arguments)
}

fn string_array_argument(
    value: &Value,
    name: &str,
    maximum_items: usize,
    maximum_length: usize,
) -> Result<Vec<String>, ReadCapabilityError> {
    let values = value
        .get(name)
        .and_then(Value::as_array)
        .ok_or(ReadCapabilityError::Arguments)?;
    if values.len() > maximum_items {
        return Err(ReadCapabilityError::Arguments);
    }
    let output: Vec<_> = values
        .iter()
        .map(|value| {
            value
                .as_str()
                .filter(|value| !value.is_empty() && value.len() <= maximum_length)
                .map(str::to_string)
                .ok_or(ReadCapabilityError::Arguments)
        })
        .collect::<Result<_, _>>()?;
    if output.iter().collect::<BTreeSet<_>>().len() != output.len() {
        return Err(ReadCapabilityError::Arguments);
    }
    Ok(output)
}

fn valid_dataset_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn integer_argument(
    value: &Value,
    name: &str,
    default: i64,
    minimum: i64,
    maximum: i64,
) -> Result<i64, ReadCapabilityError> {
    let value = match value.get(name) {
        None => default,
        Some(value) => value.as_i64().ok_or(ReadCapabilityError::Arguments)?,
    };
    if !(minimum..=maximum).contains(&value) {
        return Err(ReadCapabilityError::Arguments);
    }
    Ok(value)
}

fn number_argument(
    value: &Value,
    name: &str,
    default: f64,
    minimum: f64,
    maximum: f64,
) -> Result<f64, ReadCapabilityError> {
    let value = match value.get(name) {
        None => default,
        Some(value) => value.as_f64().ok_or(ReadCapabilityError::Arguments)?,
    };
    if !value.is_finite() || value < minimum || value > maximum {
        return Err(ReadCapabilityError::Arguments);
    }
    Ok(value)
}

fn read_file(
    root: &Path,
    relative: &str,
    offset: usize,
    limit: usize,
) -> Result<Value, ReadCapabilityError> {
    let bytes = read_bounded_file(root, relative)?;
    let byte_truncated = bytes.len() > MAX_FILE_BYTES;
    let text = String::from_utf8_lossy(&bytes[..bytes.len().min(MAX_FILE_BYTES)]);
    let lines: Vec<_> = text.lines().collect();
    let selected = lines
        .iter()
        .skip(offset)
        .take(limit)
        .copied()
        .collect::<Vec<_>>();
    Ok(json!({
        "path": relative,
        "content": selected.join("\n"),
        "total_lines": lines.len(),
        "returned_lines": selected.len(),
        "byte_truncated": byte_truncated
    }))
}

fn glob_files(root: &Path, pattern: &str) -> Result<Value, ReadCapabilityError> {
    let matcher = Glob::new(pattern)
        .map_err(|_| ReadCapabilityError::Arguments)?
        .compile_matcher();
    let (paths, walked_truncated) = walk_files(root, &matcher, MAX_GLOB_RESULTS)?;
    Ok(json!({
        "paths": paths,
        "truncated": walked_truncated || paths.len() >= MAX_GLOB_RESULTS
    }))
}

fn grep_files(
    root: &Path,
    pattern: &str,
    glob: &str,
    case_sensitive: bool,
) -> Result<Value, ReadCapabilityError> {
    let matcher = Glob::new(glob)
        .map_err(|_| ReadCapabilityError::Arguments)?
        .compile_matcher();
    let (files, walked_truncated) = walk_files(root, &matcher, MAX_WALKED_FILES)?;
    let needle = if case_sensitive {
        pattern.to_string()
    } else {
        pattern.to_lowercase()
    };
    let mut matches = Vec::new();
    for relative in files {
        if matches.len() >= MAX_GREP_RESULTS {
            break;
        }
        let Ok(bytes) = read_bounded_file(root, &relative) else {
            continue;
        };
        if bytes.len() > MAX_FILE_BYTES {
            continue;
        }
        let content = String::from_utf8_lossy(&bytes);
        for (index, line) in content.lines().enumerate() {
            let haystack = if case_sensitive {
                line.to_string()
            } else {
                line.to_lowercase()
            };
            if haystack.contains(&needle) {
                matches.push(json!({
                    "path": relative,
                    "line": index + 1,
                    "text": line.chars().take(500).collect::<String>()
                }));
                if matches.len() >= MAX_GREP_RESULTS {
                    break;
                }
            }
        }
    }
    Ok(json!({
        "matches": matches,
        "truncated": walked_truncated || matches.len() >= MAX_GREP_RESULTS
    }))
}

fn walk_files(
    root: &Path,
    matcher: &GlobMatcher,
    maximum_results: usize,
) -> Result<(Vec<String>, bool), ReadCapabilityError> {
    #[cfg(unix)]
    {
        walk_files_unix(root, matcher, maximum_results)
    }
    #[cfg(not(unix))]
    {
        walk_files_non_unix(root, matcher, maximum_results)
    }
}

#[cfg(unix)]
fn read_bounded_file(root: &Path, relative: &str) -> Result<Vec<u8>, ReadCapabilityError> {
    let file = open_relative_file(root, relative)?;
    let metadata = file.metadata().map_err(|_| ReadCapabilityError::NotFound)?;
    if !metadata.is_file() {
        return Err(ReadCapabilityError::NotFound);
    }
    let mut bytes = Vec::new();
    file.take((MAX_FILE_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| ReadCapabilityError::WorkerFailed)?;
    Ok(bytes)
}

#[cfg(unix)]
fn open_relative_file(root: &Path, relative: &str) -> Result<std::fs::File, ReadCapabilityError> {
    let components = relative_components(relative)?;
    let mut directory = open_absolute_directory(root)?;
    let (file_name, parents) = components
        .split_last()
        .ok_or(ReadCapabilityError::NotFound)?;
    for component in parents {
        directory = openat_directory(&directory, component)
            .map_err(|error| map_open_error(error, ReadCapabilityError::NotFound))?;
    }
    let file = openat_file(&directory, file_name)
        .map_err(|error| map_open_error(error, ReadCapabilityError::NotFound))?;
    Ok(std::fs::File::from(file))
}

#[cfg(unix)]
fn relative_components(relative: &str) -> Result<Vec<OsString>, ReadCapabilityError> {
    let path = Path::new(relative);
    if path.is_absolute() {
        return Err(ReadCapabilityError::PathEscape);
    }
    let mut components = Vec::new();
    for component in path.components() {
        match component {
            std::path::Component::Normal(value) => components.push(value.to_os_string()),
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir
            | std::path::Component::RootDir
            | std::path::Component::Prefix(_) => return Err(ReadCapabilityError::PathEscape),
        }
    }
    Ok(components)
}

#[cfg(unix)]
fn open_absolute_directory(path: &Path) -> Result<OwnedFd, ReadCapabilityError> {
    if !path.is_absolute() {
        return Err(ReadCapabilityError::Configuration);
    }
    // The workspace root is trusted operator configuration. Resolve it once
    // so platform-owned aliases such as macOS `/var -> /private/var` do not
    // make the worker unusable. Every user-controlled component below this
    // opened root is still resolved fd-relatively with O_NOFOLLOW.
    let path = path
        .canonicalize()
        .map_err(|_| ReadCapabilityError::Configuration)?;
    let mut directory = open_path_directory(Path::new("/"))
        .map_err(|error| map_open_error(error, ReadCapabilityError::Configuration))?;
    for component in path.components() {
        let std::path::Component::Normal(component) = component else {
            if matches!(component, std::path::Component::RootDir) {
                continue;
            }
            return Err(ReadCapabilityError::Configuration);
        };
        directory = openat_directory(&directory, component)
            .map_err(|error| map_open_error(error, ReadCapabilityError::Configuration))?;
    }
    Ok(directory)
}

#[cfg(unix)]
fn open_path_directory(path: &Path) -> std::io::Result<OwnedFd> {
    let component = path.as_os_str();
    let name = std::ffi::CString::new(component.as_bytes())
        .map_err(|_| std::io::Error::from_raw_os_error(libc::EINVAL))?;
    open_fd(
        libc::AT_FDCWD,
        &name,
        libc::O_RDONLY | libc::O_CLOEXEC | libc::O_DIRECTORY | libc::O_NOFOLLOW,
    )
}

#[cfg(unix)]
fn openat_directory(directory: &OwnedFd, component: &std::ffi::OsStr) -> std::io::Result<OwnedFd> {
    let name = std::ffi::CString::new(component.as_bytes())
        .map_err(|_| std::io::Error::from_raw_os_error(libc::EINVAL))?;
    open_fd(
        directory.as_raw_fd(),
        &name,
        libc::O_RDONLY | libc::O_CLOEXEC | libc::O_DIRECTORY | libc::O_NOFOLLOW,
    )
}

#[cfg(unix)]
fn openat_file(directory: &OwnedFd, component: &std::ffi::OsStr) -> std::io::Result<OwnedFd> {
    let name = std::ffi::CString::new(component.as_bytes())
        .map_err(|_| std::io::Error::from_raw_os_error(libc::EINVAL))?;
    open_fd(
        directory.as_raw_fd(),
        &name,
        libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK,
    )
}

#[cfg(unix)]
fn open_fd(
    directory: libc::c_int,
    name: &std::ffi::CStr,
    flags: libc::c_int,
) -> std::io::Result<OwnedFd> {
    // Every component is opened relative to an already-open directory and
    // O_NOFOLLOW is applied to each component. This makes replacement of a
    // path component between validation and use unable to escape the root.
    let fd = unsafe { libc::openat(directory, name.as_ptr(), flags, 0) };
    if fd < 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(unsafe { OwnedFd::from_raw_fd(fd) })
    }
}

#[cfg(unix)]
fn map_open_error(error: std::io::Error, ordinary: ReadCapabilityError) -> ReadCapabilityError {
    // macOS reports ENOTDIR for `O_DIRECTORY | O_NOFOLLOW` on a symlinked
    // parent while Linux normally reports ELOOP. Both must fail closed with
    // the same stable contract error.
    if error
        .raw_os_error()
        .is_some_and(|code| code == libc::ELOOP || code == libc::ENOTDIR)
    {
        ReadCapabilityError::PathEscape
    } else {
        ordinary
    }
}

#[cfg(unix)]
fn walk_files_unix(
    root: &Path,
    matcher: &GlobMatcher,
    maximum_results: usize,
) -> Result<(Vec<String>, bool), ReadCapabilityError> {
    let root_directory =
        open_absolute_directory(root).map_err(|_| ReadCapabilityError::Configuration)?;
    let mut directories = vec![(PathBuf::new(), root_directory)];
    let mut output = Vec::new();
    let mut walked = 0_usize;
    while let Some((relative_directory, directory)) = directories.pop() {
        let mut entries = read_directory_entries(&directory)?;
        entries.sort();
        for name in entries {
            walked += 1;
            if walked > MAX_WALKED_FILES {
                output.sort();
                return Ok((output, true));
            }
            let relative = if relative_directory.as_os_str().is_empty() {
                PathBuf::from(&name)
            } else {
                relative_directory.join(&name)
            };
            if let Ok(child_directory) = openat_directory(&directory, &name) {
                directories.push((relative, child_directory));
                continue;
            }
            let Ok(file) = openat_file(&directory, &name) else {
                continue;
            };
            let Ok(metadata) = std::fs::File::from(file).metadata() else {
                continue;
            };
            if metadata.is_file() && matcher.is_match(&relative) {
                output.push(relative.to_string_lossy().replace('\\', "/"));
                if output.len() >= maximum_results {
                    output.sort();
                    return Ok((output, true));
                }
            }
        }
    }
    output.sort();
    Ok((output, false))
}

#[cfg(unix)]
fn read_directory_entries(directory: &OwnedFd) -> Result<Vec<OsString>, ReadCapabilityError> {
    let duplicate = unsafe { libc::dup(directory.as_raw_fd()) };
    if duplicate < 0 {
        return Err(ReadCapabilityError::WorkerFailed);
    }
    let stream = unsafe { libc::fdopendir(duplicate) };
    if stream.is_null() {
        unsafe { libc::close(duplicate) };
        return Err(ReadCapabilityError::WorkerFailed);
    }
    let mut entries = Vec::new();
    loop {
        clear_errno();
        let entry = unsafe { libc::readdir(stream) };
        if entry.is_null() {
            let error = std::io::Error::last_os_error();
            unsafe { libc::closedir(stream) };
            if error.raw_os_error().is_some_and(|code| code != 0) {
                return Err(ReadCapabilityError::WorkerFailed);
            }
            return Ok(entries);
        }
        let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) };
        if name.to_bytes() != b"." && name.to_bytes() != b".." {
            entries.push(OsString::from_vec(name.to_bytes().to_vec()));
        }
    }
}

#[cfg(target_os = "linux")]
fn clear_errno() {
    unsafe { *libc::__errno_location() = 0 };
}

#[cfg(any(target_os = "macos", target_os = "ios", target_os = "freebsd"))]
fn clear_errno() {
    unsafe { *libc::__error() = 0 };
}

#[cfg(all(
    unix,
    not(any(
        target_os = "linux",
        target_os = "macos",
        target_os = "ios",
        target_os = "freebsd"
    ))
))]
fn clear_errno() {}

#[cfg(not(unix))]
fn rooted_existing_path(root: &Path, relative: &str) -> Result<PathBuf, ReadCapabilityError> {
    let root = root
        .canonicalize()
        .map_err(|_| ReadCapabilityError::Configuration)?;
    let relative = Path::new(relative);
    if relative.is_absolute()
        || relative
            .components()
            .any(|component| matches!(component, std::path::Component::ParentDir))
    {
        return Err(ReadCapabilityError::PathEscape);
    }
    let resolved = root
        .join(relative)
        .canonicalize()
        .map_err(|_| ReadCapabilityError::NotFound)?;
    if !resolved.starts_with(&root) {
        return Err(ReadCapabilityError::PathEscape);
    }
    Ok(resolved)
}

#[cfg(not(unix))]
fn read_bounded_file(root: &Path, relative: &str) -> Result<Vec<u8>, ReadCapabilityError> {
    let path = rooted_existing_path(root, relative)?;
    let metadata = path
        .symlink_metadata()
        .map_err(|_| ReadCapabilityError::NotFound)?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err(ReadCapabilityError::NotFound);
    }
    let mut bytes = Vec::new();
    std::fs::File::open(path)
        .map_err(|_| ReadCapabilityError::NotFound)?
        .take((MAX_FILE_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| ReadCapabilityError::WorkerFailed)?;
    Ok(bytes)
}

#[cfg(not(unix))]
fn walk_files_non_unix(
    root: &Path,
    matcher: &GlobMatcher,
    maximum_results: usize,
) -> Result<(Vec<String>, bool), ReadCapabilityError> {
    let root = root
        .canonicalize()
        .map_err(|_| ReadCapabilityError::Configuration)?;
    let mut directories = vec![root.clone()];
    let mut output = Vec::new();
    let mut walked = 0_usize;
    while let Some(directory) = directories.pop() {
        let entries =
            std::fs::read_dir(directory).map_err(|_| ReadCapabilityError::WorkerFailed)?;
        for entry in entries.flatten() {
            walked += 1;
            if walked > MAX_WALKED_FILES {
                return Ok((output, true));
            }
            let path = entry.path();
            let Ok(metadata) = path.symlink_metadata() else {
                continue;
            };
            if metadata.file_type().is_symlink() {
                continue;
            }
            if metadata.is_dir() {
                directories.push(path);
                continue;
            }
            if metadata.is_file() {
                let Ok(relative) = path.strip_prefix(&root) else {
                    continue;
                };
                if matcher.is_match(relative) {
                    output.push(relative.to_string_lossy().replace('\\', "/"));
                    if output.len() >= maximum_results {
                        return Ok((output, true));
                    }
                }
            }
        }
    }
    output.sort();
    Ok((output, false))
}

fn strip_html(input: &str) -> String {
    let mut output = String::with_capacity(input.len());
    let mut in_tag = false;
    for character in input.chars() {
        match character {
            '<' => in_tag = true,
            '>' => {
                in_tag = false;
                output.push(' ');
            }
            _ if !in_tag => output.push(character),
            _ => {}
        }
    }
    output.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn private_addresses_are_rejected() {
        assert!(!is_public_ip("127.0.0.1".parse().unwrap()));
        assert!(!is_public_ip("169.254.169.254".parse().unwrap()));
        assert!(!is_public_ip("::1".parse().unwrap()));
        assert!(is_public_ip("1.1.1.1".parse().unwrap()));
    }

    #[test]
    fn html_extraction_does_not_preserve_tags() {
        assert_eq!(strip_html("<p>Hello <b>world</b></p>"), "Hello world");
    }

    #[cfg(unix)]
    #[test]
    fn root_relative_open_rejects_replaced_file_symlink() {
        let base = std::env::temp_dir().join(format!("ai-platform-read-{}", Uuid::now_v7()));
        let root = base.join("root");
        let outside = base.join("outside.txt");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("document.txt"), "safe").unwrap();
        std::fs::write(&outside, "outside").unwrap();
        std::fs::remove_file(root.join("document.txt")).unwrap();
        std::os::unix::fs::symlink(&outside, root.join("document.txt")).unwrap();

        let error = read_bounded_file(&root, "document.txt").unwrap_err();
        assert_eq!(error, ReadCapabilityError::PathEscape);
        let _ = std::fs::remove_dir_all(base);
    }

    #[cfg(unix)]
    #[test]
    fn root_relative_open_rejects_symlinked_parent_escape() {
        let base = std::env::temp_dir().join(format!("ai-platform-read-{}", Uuid::now_v7()));
        let root = base.join("root");
        let outside = base.join("outside");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::write(outside.join("document.txt"), "outside").unwrap();
        std::os::unix::fs::symlink(&outside, root.join("nested")).unwrap();

        let error = read_bounded_file(&root, "nested/document.txt").unwrap_err();
        assert_eq!(error, ReadCapabilityError::PathEscape);
        let _ = std::fs::remove_dir_all(base);
    }
}
