use std::collections::BTreeMap;
use std::sync::Arc;

use ai_platform_capability_contract::{
    CAPABILITY_CATALOG_SCHEMA_VERSION, CAPABILITY_EXECUTION_SCHEMA_VERSION,
    CapabilityCatalogRequestV2, CapabilityCatalogV2, CapabilityDescriptorV2, CapabilityEffect,
    CapabilityExecutionStatus, CapabilityExecutionV2, CapabilityScopeV2,
    CreateCapabilityExecutionRequestV2, RuntimeCapabilityLeaseV1, validate_json_value,
};
use axum::extract::{DefaultBodyLimit, Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::{Mutex, Notify, Semaphore};
use uuid::Uuid;

use crate::{
    CapabilityIdentity, DynStore, ExecutionRecord, MAX_EVENT_PAYLOAD_BYTES, MAX_REQUEST_BYTES,
    NewExecution, RuntimeCapabilityBinding, StoreError,
    attachment_capabilities::AttachmentCapabilityBroker,
    durable_recovery_result,
    external_write_capabilities::{ExternalWriteContext, ExternalWriteExecutor},
    fixture_result, is_recoverable_execution,
    local_node_broker::LocalNodeBroker,
    local_node_broker::{LocalNodeActionRequest, LocalNodeScope},
    needs_side_effect_unknown_recovery, now_epoch_ms,
    office_capabilities::{OfficeCapabilityExecutor, OfficeExecutionContext},
    platform_catalog::worker_capability_catalog_with_writes,
    python_code_execution::LocalPythonSandboxBroker,
    python_code_execution::{
        CodeInputAttachment, PythonCodeExecutionRequest, PythonSandboxBroker, PythonSandboxLimits,
    },
    quiz_capabilities::QuizPersistenceAdapter,
    read_capabilities::{ReadCapabilityContext, ReadCapabilityExecutor},
    write_capabilities::{WriteCapabilityContext, WriteCapabilityExecutor},
};

const INTERNAL_TOKEN_HEADER: &str = "x-ai-platform-internal-token";
const TENANT_HEADER: &str = "x-ai-tenant-id";
const USER_HEADER: &str = "x-ai-user-id";
const SESSION_HEADER: &str = "x-ai-session-id";

enum OperationError {
    Failed(String),
    SideEffectUnknown,
}

#[derive(Clone)]
pub struct WorkerState {
    pub store: DynStore,
    pub internal_token: Arc<str>,
    pub lease_secret: Arc<[u8]>,
    pub execution_slots: Arc<Semaphore>,
    pub read_executor: Option<Arc<ReadCapabilityExecutor>>,
    pub write_executor: Option<Arc<WriteCapabilityExecutor>>,
    pub quiz_executor: Option<Arc<QuizPersistenceAdapter>>,
    pub external_write_executor: Option<Arc<ExternalWriteExecutor>>,
    pub office_executor: Option<Arc<OfficeCapabilityExecutor>>,
    pub python_executor: Option<Arc<LocalPythonSandboxBroker>>,
    pub attachment_executor: Option<Arc<AttachmentCapabilityBroker>>,
    pub local_node_broker: Option<Arc<LocalNodeBroker>>,
    /// Immutable descriptor registry.  It is constructed once at startup and
    /// shared by catalog, create, descriptor policy, and timeout paths.
    pub capability_registry:
        Arc<BTreeMap<String, ai_platform_capability_contract::CapabilityDescriptorV2>>,
    cancellation: Arc<Mutex<BTreeMap<String, Arc<Notify>>>>,
}

impl WorkerState {
    pub fn try_new(
        store: DynStore,
        internal_token: String,
        lease_secret: Vec<u8>,
        fixtures_enabled: bool,
    ) -> Result<Self, crate::platform_catalog::PlatformCatalogError> {
        Self::try_new_with_writes(store, internal_token, lease_secret, fixtures_enabled, false)
    }

    pub fn try_new_with_writes(
        store: DynStore,
        internal_token: String,
        lease_secret: Vec<u8>,
        fixtures_enabled: bool,
        writes_enabled: bool,
    ) -> Result<Self, crate::platform_catalog::PlatformCatalogError> {
        Ok(Self {
            store,
            internal_token: Arc::from(internal_token),
            lease_secret: Arc::from(lease_secret),
            execution_slots: Arc::new(Semaphore::new(1)),
            read_executor: None,
            write_executor: None,
            quiz_executor: None,
            external_write_executor: None,
            office_executor: None,
            python_executor: None,
            attachment_executor: None,
            local_node_broker: None,
            capability_registry: Arc::new({
                let mut registry = worker_capability_catalog_with_writes(writes_enabled)?;
                if fixtures_enabled {
                    for descriptor in crate::fixture_catalog() {
                        if registry.insert(descriptor.id.clone(), descriptor).is_some() {
                            return Err(crate::platform_catalog::PlatformCatalogError::Duplicate(
                                "projected id",
                                "fixture collision".to_string(),
                            ));
                        }
                    }
                }
                registry
            }),
            cancellation: Arc::new(Mutex::new(BTreeMap::new())),
        })
    }

    pub fn with_read_executor(mut self, executor: Arc<ReadCapabilityExecutor>) -> Self {
        self.read_executor = Some(executor);
        self
    }

    pub fn with_write_executor(mut self, executor: Arc<WriteCapabilityExecutor>) -> Self {
        self.write_executor = Some(executor);
        self
    }

    pub fn with_quiz_executor(mut self, executor: Arc<QuizPersistenceAdapter>) -> Self {
        self.quiz_executor = Some(executor);
        self
    }

    pub fn with_external_write_executor(mut self, executor: Arc<ExternalWriteExecutor>) -> Self {
        self.external_write_executor = Some(executor);
        self
    }

    pub fn with_office_executor(mut self, executor: Arc<OfficeCapabilityExecutor>) -> Self {
        self.office_executor = Some(executor);
        self
    }

    pub fn with_python_executor(mut self, executor: Arc<LocalPythonSandboxBroker>) -> Self {
        self.python_executor = Some(executor);
        self
    }

    pub fn with_attachment_executor(mut self, executor: Arc<AttachmentCapabilityBroker>) -> Self {
        self.attachment_executor = Some(executor);
        self
    }

    pub fn with_local_node_broker(mut self, broker: Arc<LocalNodeBroker>) -> Self {
        self.local_node_broker = Some(broker);
        self
    }

    async fn register_cancellation(&self, execution_id: &str) -> (Arc<Notify>, bool) {
        let mut cancellation = self.cancellation.lock().await;
        if let Some(token) = cancellation.get(execution_id) {
            return (token.clone(), false);
        }
        let token = Arc::new(Notify::new());
        cancellation.insert(execution_id.to_string(), token.clone());
        (token, true)
    }

    async fn request_cancel(&self, execution_id: &str) {
        let cancellation = self.cancellation.lock().await.get(execution_id).cloned();
        if let Some(cancellation) = cancellation {
            cancellation.notify_waiters();
        }
    }

    async fn forget_cancellation(&self, execution_id: &str) {
        self.cancellation.lock().await.remove(execution_id);
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EventsQuery {
    #[serde(default)]
    after_sequence: u64,
}

#[derive(Debug, Serialize)]
struct ErrorBody {
    error: &'static str,
    message: &'static str,
}

type HttpError = (StatusCode, Json<ErrorBody>);

fn error(status: StatusCode, code: &'static str, message: &'static str) -> HttpError {
    (
        status,
        Json(ErrorBody {
            error: code,
            message,
        }),
    )
}

fn store_error(value: StoreError) -> HttpError {
    match value {
        StoreError::NotFound => error(StatusCode::NOT_FOUND, "not_found", "execution not found"),
        StoreError::ScopeMismatch => {
            error(StatusCode::NOT_FOUND, "not_found", "execution not found")
        }
        StoreError::IdempotencyConflict => error(
            StatusCode::CONFLICT,
            "idempotency_conflict",
            "execution identity conflicts with an existing request",
        ),
        StoreError::DispatchFenceMismatch => error(
            StatusCode::CONFLICT,
            "dispatch_fence_conflict",
            "execution already has another dispatch fence",
        ),
        StoreError::ApprovalRequired => error(
            StatusCode::FORBIDDEN,
            "approval_required",
            "a bound one-time approval is required",
        ),
        StoreError::TerminalImmutable => error(
            StatusCode::CONFLICT,
            "terminal_immutable",
            "terminal execution cannot be mutated",
        ),
        StoreError::InvalidEvent => error(
            StatusCode::BAD_REQUEST,
            "event_invalid",
            "execution event is invalid",
        ),
        StoreError::RuntimeBindingUnauthorized => error(
            StatusCode::FORBIDDEN,
            "runtime_binding_unauthorized",
            "runtime capability binding is not active",
        ),
        StoreError::Internal => error(
            StatusCode::SERVICE_UNAVAILABLE,
            "store_unavailable",
            "capability execution store is unavailable",
        ),
    }
}

fn header(headers: &HeaderMap, name: &'static str) -> Result<String, HttpError> {
    let value = headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| error(StatusCode::FORBIDDEN, "scope_invalid", "scope is missing"))?;
    if value.len() > 255 || value.bytes().any(|byte| byte.is_ascii_control()) {
        return Err(error(
            StatusCode::FORBIDDEN,
            "scope_invalid",
            "scope is invalid",
        ));
    }
    Ok(value.to_string())
}

fn authorize(headers: &HeaderMap, state: &WorkerState) -> Result<CapabilityScopeV2, HttpError> {
    let provided = headers
        .get(INTERNAL_TOKEN_HEADER)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default();
    if provided.len() != state.internal_token.len()
        || !constant_time_eq::constant_time_eq(provided.as_bytes(), state.internal_token.as_bytes())
    {
        return Err(error(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "internal authorization failed",
        ));
    }
    Ok(CapabilityScopeV2 {
        tenant_id: header(headers, TENANT_HEADER)?,
        user_id: header(headers, USER_HEADER)?,
        session_id: header(headers, SESSION_HEADER)?,
    })
}

fn scope_matches(scope: &CapabilityScopeV2, expected: &CapabilityScopeV2) -> bool {
    scope == expected
}

fn resource_binding_value(
    binding: &RuntimeCapabilityBinding,
    scope: &CapabilityScopeV2,
    run_id: &str,
    descriptor: &CapabilityDescriptorV2,
) -> Value {
    serde_json::json!({
        "tenant_id": scope.tenant_id,
        "user_id": scope.user_id,
        "session_id": scope.session_id,
        "run_id": run_id,
        "snapshot_id": binding.snapshot_id,
        "capability_revision": binding.capability_revision,
        "type": binding.capability_type,
        "name": binding.name,
        "capability_id": binding.capability_id,
        "capability_version": binding.capability_version,
        "schema_hash": binding.schema_hash,
        "bound_dataset_ids": binding.bound_dataset_ids.iter().cloned().collect::<Vec<_>>(),
        "connector_binding": binding.connector_binding.clone(),
        "device_id": binding.connector_binding.as_ref().and_then(|value| value.connection_id.clone()),
        "memory_policy": binding.memory_policy.clone(),
        "descriptor": descriptor,
    })
}

fn descriptor_type(descriptor: &CapabilityDescriptorV2) -> Result<&'static str, HttpError> {
    if descriptor
        .tags
        .iter()
        .any(|tag| tag == "kind:connector" || tag == "binding-type:connector")
    {
        Ok("connector")
    } else if descriptor.tags.iter().any(|tag| tag == "kind:knowledge") {
        Ok("knowledge")
    } else if descriptor.tags.iter().any(|tag| tag == "kind:mcp") {
        Ok("mcp")
    } else if descriptor.tags.iter().any(|tag| tag == "kind:tool")
        || descriptor.tags.iter().any(|tag| tag == "fixture")
    {
        Ok("tool")
    } else {
        Err(error(
            StatusCode::SERVICE_UNAVAILABLE,
            "capability_descriptor_invalid",
            "capability descriptor kind is invalid",
        ))
    }
}

fn verify_descriptor_against_runtime_binding(
    descriptor: &CapabilityDescriptorV2,
    binding: &RuntimeCapabilityBinding,
    requested_connector_binding: Option<&Value>,
) -> Result<(), StoreError> {
    if binding.capability_id != descriptor.id
        || binding.capability_version != descriptor.version
        || binding.schema_hash != descriptor.schema_hash
        || binding.effect != descriptor.effect
        || binding.approval_policy != descriptor.approval_policy
    {
        return Err(StoreError::RuntimeBindingUnauthorized);
    }
    let stored_connector_binding = binding
        .connector_binding
        .as_ref()
        .map(|value| serde_json::to_value(value).unwrap_or(Value::Null));
    if stored_connector_binding.as_ref() != requested_connector_binding
        || descriptor.connector_binding.as_ref() != requested_connector_binding
    {
        return Err(StoreError::RuntimeBindingUnauthorized);
    }
    Ok(())
}

pub fn router(state: WorkerState) -> Router {
    tokio::spawn(recover_executions_loop(state.clone()));
    Router::new()
        .route("/health/ready", get(health))
        .route("/internal/v2/capabilities/catalog", post(catalog))
        .route(
            "/internal/v2/capabilities/executions",
            post(create_execution),
        )
        .route(
            "/internal/v2/capabilities/executions/{execution_id}",
            get(get_execution).post(cancel_execution),
        )
        .route(
            "/internal/v2/capabilities/executions/{execution_id}/events",
            get(get_events),
        )
        .layer(DefaultBodyLimit::max(MAX_REQUEST_BYTES))
        .with_state(state)
}

async fn health() -> impl IntoResponse {
    (StatusCode::OK, Json(serde_json::json!({"status": "ready"})))
}

async fn catalog(
    State(state): State<WorkerState>,
    headers: HeaderMap,
    Json(request): Json<CapabilityCatalogRequestV2>,
) -> Result<impl IntoResponse, HttpError> {
    let scope = authorize(&headers, &state)?;
    request.validate().map_err(|_| {
        error(
            StatusCode::BAD_REQUEST,
            "catalog_request_invalid",
            "catalog request is invalid",
        )
    })?;
    if !scope_matches(
        &scope,
        &CapabilityScopeV2 {
            tenant_id: request.tenant_id,
            user_id: request.user_id,
            session_id: request.session_id,
        },
    ) {
        return Err(error(
            StatusCode::FORBIDDEN,
            "scope_mismatch",
            "catalog scope does not match forwarded identity",
        ));
    }
    Ok(Json(CapabilityCatalogV2 {
        schema_version: CAPABILITY_CATALOG_SCHEMA_VERSION.to_string(),
        capability_revision: request.capability_revision,
        capabilities: state.capability_registry.values().cloned().collect(),
    }))
}

async fn create_execution(
    State(state): State<WorkerState>,
    headers: HeaderMap,
    Json(request): Json<CreateCapabilityExecutionRequestV2>,
) -> Result<impl IntoResponse, HttpError> {
    let scope = authorize(&headers, &state)?;
    request.validate(now_epoch_ms()).map_err(|_| {
        error(
            StatusCode::BAD_REQUEST,
            "execution_request_invalid",
            "execution request is invalid",
        )
    })?;
    request
        .lease
        .verify_signature(&state.lease_secret)
        .map_err(|_| {
            error(
                StatusCode::FORBIDDEN,
                "lease_invalid",
                "runtime capability lease is invalid",
            )
        })?;
    if !scope_matches(&scope, &request.lease.scope()) {
        return Err(error(
            StatusCode::FORBIDDEN,
            "scope_mismatch",
            "lease scope does not match forwarded identity",
        ));
    }
    let requested_descriptor = request.descriptor.clone();
    requested_descriptor.validate().map_err(|_| {
        error(
            StatusCode::FORBIDDEN,
            "capability_descriptor_invalid",
            "capability descriptor is invalid",
        )
    })?;
    if requested_descriptor.id != request.lease.capability_id
        || requested_descriptor.effect != request.lease.effect
    {
        return Err(store_error(StoreError::RuntimeBindingUnauthorized));
    }
    // Built-ins remain registry-authoritative. Dynamic descriptors are not
    // required to appear in this catalog; they are checked against PG below.
    let descriptor = if let Some(static_descriptor) =
        state.capability_registry.get(&request.lease.capability_id)
    {
        if static_descriptor != &requested_descriptor {
            return Err(store_error(StoreError::RuntimeBindingUnauthorized));
        }
        static_descriptor.clone()
    } else {
        requested_descriptor
    };
    let declared_type = descriptor_type(&descriptor)?;
    let runtime_binding = state
        .store
        .authorize_runtime_binding(
            &scope,
            &request.lease.run_id,
            request.lease.capability_revision,
            &CapabilityIdentity {
                capability_type: declared_type.to_string(),
                name: descriptor.name.clone(),
                capability_id: descriptor.id.clone(),
                version: descriptor.version.clone(),
                schema_hash: descriptor.schema_hash.clone(),
                effect: descriptor.effect,
                approval_policy: descriptor.approval_policy,
                connector_binding: request.connector_binding.clone(),
            },
        )
        .await
        .map_err(store_error)?;
    if runtime_binding.capability_revision != request.lease.capability_revision {
        return Err(store_error(StoreError::RuntimeBindingUnauthorized));
    }
    if runtime_binding.capability_type != declared_type
        && !(runtime_binding.capability_type == "connector"
            && descriptor
                .tags
                .iter()
                .any(|tag| tag == "binding-type:connector"))
    {
        return Err(store_error(StoreError::RuntimeBindingUnauthorized));
    }
    verify_descriptor_against_runtime_binding(
        &descriptor,
        &runtime_binding,
        request.connector_binding.as_ref(),
    )
    .map_err(store_error)?;
    let arguments = request.arguments;
    if descriptor.id == "search_knowledge_base"
        && let Some(requested_datasets) = arguments.get("dataset_ids")
    {
        let requested_datasets = requested_datasets.as_array().ok_or_else(|| {
            error(
                StatusCode::BAD_REQUEST,
                "capability_arguments_invalid",
                "dataset_ids must be an array",
            )
        })?;
        if requested_datasets.iter().any(|dataset| {
            dataset
                .as_str()
                .is_none_or(|dataset| !runtime_binding.bound_dataset_ids.contains(dataset))
        }) {
            return Err(error(
                StatusCode::FORBIDDEN,
                "runtime_resource_scope_mismatch",
                "requested datasets are outside the Runtime snapshot",
            ));
        }
    }
    validate_json_value(&descriptor.input_schema, &arguments).map_err(|_| {
        error(
            StatusCode::BAD_REQUEST,
            "capability_arguments_invalid",
            "capability arguments do not match the bound schema",
        )
    })?;
    if request.lease.effect != descriptor.effect {
        return Err(error(
            StatusCode::CONFLICT,
            "capability_binding_changed",
            "capability binding changed",
        ));
    }
    if descriptor.id == "update_user_memory" && runtime_binding.memory_policy.is_none() {
        return Err(store_error(StoreError::RuntimeBindingUnauthorized));
    }
    if let Some(policy) = &runtime_binding.memory_policy {
        policy
            .validate()
            .map_err(|_| store_error(StoreError::RuntimeBindingUnauthorized))?;
    }

    let execution_id = Uuid::now_v7().to_string();
    let events_url = format!("/internal/v2/capabilities/executions/{execution_id}/events");
    let execution = CapabilityExecutionV2 {
        schema_version: CAPABILITY_EXECUTION_SCHEMA_VERSION.to_string(),
        execution_id: execution_id.clone(),
        lease_id: request.lease.lease_id.clone(),
        tenant_id: request.lease.tenant_id.clone(),
        user_id: request.lease.user_id.clone(),
        session_id: request.lease.session_id.clone(),
        run_id: request.lease.run_id.clone(),
        tool_call_id: request.lease.tool_call_id.clone(),
        attempt_id: request.lease.attempt_id.clone(),
        capability_id: request.lease.capability_id.clone(),
        capability_revision: request.lease.capability_revision,
        arguments_hash: request.lease.arguments_hash.clone(),
        idempotency_key: request.idempotency_key,
        effect: request.lease.effect,
        status: CapabilityExecutionStatus::Published,
        events_url,
        result: None,
        error: None,
    };
    execution.validate().map_err(|_| {
        error(
            StatusCode::BAD_REQUEST,
            "execution_request_invalid",
            "execution request is invalid",
        )
    })?;
    let outcome = state
        .store
        .reserve(NewExecution {
            execution,
            arguments,
            resource_binding: resource_binding_value(
                &runtime_binding,
                &scope,
                &request.lease.run_id,
                &descriptor,
            ),
            approval_policy: descriptor.approval_policy,
            approval_id: request.lease.approval_id,
            approval_status: if matches!(descriptor.effect, CapabilityEffect::Read) {
                "not_required".to_string()
            } else {
                "approved".to_string()
            },
        })
        .await
        .map_err(store_error)?;
    let actual_id = outcome.record.execution.execution_id.clone();
    if outcome.created {
        let published_event_id = event_id(&actual_id, 1)?;
        state
            .store
            .append_event(
                &scope,
                &actual_id,
                &published_event_id,
                "published",
                CapabilityExecutionStatus::Published,
                BTreeMap::new(),
                None,
            )
            .await
            .map_err(store_error)?;
    }

    if outcome.created && !outcome.record.execution.status.is_terminal() {
        state.register_cancellation(&actual_id).await;
        tokio::spawn(run_execution(
            state.clone(),
            scope,
            actual_id.clone(),
            outcome.record.execution.capability_id.clone(),
            outcome.record.arguments.clone(),
            descriptor,
        ));
    }
    let current = state
        .store
        .get(&outcome.record.execution.scope(), &actual_id)
        .await;
    let response = current.unwrap_or(outcome.record).execution;
    Ok((
        if outcome.created {
            StatusCode::ACCEPTED
        } else {
            StatusCode::OK
        },
        Json(response),
    ))
}

fn lease_for_record(execution: &CapabilityExecutionV2) -> RuntimeCapabilityLeaseV1 {
    let issued = now_epoch_ms();
    RuntimeCapabilityLeaseV1 {
        schema_version: ai_platform_capability_contract::RUNTIME_CAPABILITY_LEASE_SCHEMA_VERSION
            .into(),
        lease_id: execution.lease_id.clone(),
        tenant_id: execution.tenant_id.clone(),
        user_id: execution.user_id.clone(),
        session_id: execution.session_id.clone(),
        run_id: execution.run_id.clone(),
        tool_call_id: execution.tool_call_id.clone(),
        attempt_id: execution.attempt_id.clone(),
        capability_id: execution.capability_id.clone(),
        capability_revision: execution.capability_revision,
        arguments_hash: execution.arguments_hash.clone(),
        effect: execution.effect,
        approval_id: None,
        issued_at_epoch_ms: issued,
        expires_at_epoch_ms: issued
            .saturating_add(ai_platform_capability_contract::MAX_CAPABILITY_LEASE_TTL_MS),
        nonce: execution.execution_id.clone(),
        signature: String::new(),
    }
}

async fn run_execution(
    state: WorkerState,
    scope: CapabilityScopeV2,
    execution_id: String,
    capability_id: String,
    arguments: Value,
    descriptor: CapabilityDescriptorV2,
) {
    let cancellation = state.register_cancellation(&execution_id).await.0;
    let _slot = tokio::select! {
        _ = cancellation.notified() => {
            state.forget_cancellation(&execution_id).await;
            return;
        }
        slot = state.execution_slots.acquire() => match slot {
            Ok(slot) => slot,
            Err(_) => {
                state.forget_cancellation(&execution_id).await;
                return;
            }
        }
    };
    if let Ok(current) = state.store.get(&scope, &execution_id).await
        && current.execution.status.is_terminal()
    {
        state.forget_cancellation(&execution_id).await;
        return;
    }
    let requested_fence = Uuid::now_v7().to_string();
    let dispatch = match state
        .store
        .dispatch(&scope, &execution_id, &requested_fence)
        .await
    {
        Ok(dispatch) => dispatch,
        Err(error) => {
            tracing::error!(%execution_id, %error, "failed to claim capability execution");
            state.forget_cancellation(&execution_id).await;
            return;
        }
    };
    if !dispatch.claimed {
        state.forget_cancellation(&execution_id).await;
        return;
    }
    let fence = dispatch
        .record
        .dispatch_fence
        .clone()
        .unwrap_or(requested_fence);
    let lease_lost = Arc::new(Notify::new());
    let heartbeat_state = state.clone();
    let heartbeat_scope = scope.clone();
    let heartbeat_execution_id = execution_id.clone();
    let heartbeat_fence = fence.clone();
    let heartbeat_lease_lost = lease_lost.clone();
    let heartbeat = tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(5));
        interval.tick().await;
        loop {
            interval.tick().await;
            match heartbeat_state
                .store
                .renew(
                    &heartbeat_scope,
                    &heartbeat_execution_id,
                    &heartbeat_fence,
                    30_000,
                )
                .await
            {
                Ok(true) => {}
                Ok(false) => {
                    tracing::warn!(%heartbeat_execution_id, "capability worker lease was lost");
                    heartbeat_lease_lost.notify_waiters();
                    return;
                }
                Err(error) => {
                    tracing::error!(%heartbeat_execution_id, %error, "failed to renew capability worker lease");
                    heartbeat_lease_lost.notify_waiters();
                    return;
                }
            }
        }
    });
    if let Err(error) = state
        .store
        .append_event(
            &scope,
            &execution_id,
            &event_id_unchecked(&execution_id, 2),
            "dispatched",
            CapabilityExecutionStatus::Dispatched,
            BTreeMap::new(),
            Some(&fence),
        )
        .await
    {
        tracing::error!(%execution_id, %error, "failed to persist capability dispatch event");
        heartbeat.abort();
        state.forget_cancellation(&execution_id).await;
        return;
    }
    if let Err(error) = state
        .store
        .append_event(
            &scope,
            &execution_id,
            &event_id_unchecked(&execution_id, 3),
            "progress",
            CapabilityExecutionStatus::Running,
            BTreeMap::new(),
            Some(&fence),
        )
        .await
    {
        tracing::error!(%execution_id, %error, "failed to persist capability running event");
        heartbeat.abort();
        state.forget_cancellation(&execution_id).await;
        return;
    }
    let timeout_ms = descriptor.timeout_ms.clamp(1, 120_000);
    let effect = descriptor.effect;
    let operation = async {
        match effect {
            CapabilityEffect::Read => {
                if capability_id == "read_attachment" {
                    let Some(executor) = &state.attachment_executor else {
                        return Err(OperationError::Failed(
                            "attachment_executor_unavailable".into(),
                        ));
                    };
                    let read_arguments =
                        serde_json::from_value(arguments.clone()).map_err(|_| {
                            OperationError::Failed("attachment_arguments_invalid".into())
                        })?;
                    let lease = lease_for_record(&dispatch.record.execution);
                    return executor
                        .read_scoped(lease, execution_id.clone(), read_arguments)
                        .await
                        .map(|value| serde_json::to_value(value).unwrap_or(Value::Null))
                        .map_err(|error| OperationError::Failed(error.to_string()));
                }
                if capability_id == "local_node_catalog" || capability_id == "local_node_describe" {
                    let Some(broker) = &state.local_node_broker else {
                        return Err(OperationError::Failed(
                            "local_node_broker_unavailable".into(),
                        ));
                    };
                    let device_id = dispatch
                        .record
                        .resource_binding
                        .get("device_id")
                        .and_then(Value::as_str)
                        .or_else(|| {
                            dispatch
                                .record
                                .resource_binding
                                .get("connector_binding")
                                .and_then(|value| value.get("device_id"))
                                .and_then(Value::as_str)
                        })
                        .or_else(|| arguments.get("device_id").and_then(Value::as_str))
                        .ok_or_else(|| {
                            OperationError::Failed("local_node_device_required".into())
                        })?;
                    let descriptor = broker
                        .describe(&LocalNodeScope {
                            tenant_id: scope.tenant_id.clone(),
                            user_id: scope.user_id.clone(),
                            session_id: scope.session_id.clone(),
                            device_id: device_id.into(),
                        })
                        .await
                        .map_err(|error| OperationError::Failed(error.to_string()))?;
                    return Ok(serde_json::to_value(descriptor).unwrap_or(Value::Null));
                }
                if capability_id == "mcp_docgen__preview_document" {
                    let Some(executor) = &state.office_executor else {
                        return Err(OperationError::Failed(
                            "office_capability_executor_unavailable".into(),
                        ));
                    };
                    let format: ai_platform_office::DocumentFormat =
                        serde_json::from_value(arguments.get("format").cloned().ok_or_else(
                            || OperationError::Failed("office_format_required".into()),
                        )?)
                        .map_err(|_| OperationError::Failed("office_format_invalid".into()))?;
                    let encoded = arguments
                        .get("source_base64")
                        .and_then(Value::as_str)
                        .ok_or_else(|| OperationError::Failed("office_source_required".into()))?;
                    let source = base64::engine::general_purpose::STANDARD
                        .decode(encoded)
                        .map_err(|_| OperationError::Failed("office_source_invalid".into()))?;
                    return executor
                        .preview_existing_document(format, &source)
                        .map(|value| serde_json::to_value(value).unwrap_or(Value::Null))
                        .map_err(|error| OperationError::Failed(error.to_string()));
                }
                if capability_id == "platform.read_fixture"
                    && let Some(delay_ms) = arguments.get("delay_ms").and_then(Value::as_u64)
                {
                    tokio::time::sleep(std::time::Duration::from_millis(delay_ms.min(5_000))).await;
                }
                if let Some(result) = fixture_result(&capability_id, &arguments) {
                    Ok(result)
                } else if let Some(executor) = &state.read_executor {
                    let bound_dataset_ids = dispatch
                        .record
                        .resource_binding
                        .get("bound_dataset_ids")
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect();
                    executor
                        .execute(
                            &capability_id,
                            &ReadCapabilityContext {
                                tenant_id: scope.tenant_id.clone(),
                                user_id: scope.user_id.clone(),
                                session_id: scope.session_id.clone(),
                                execution_id: execution_id.clone(),
                                tool_call_id: dispatch.record.execution.tool_call_id.clone(),
                                run_id: dispatch.record.execution.run_id.clone(),
                                capability_revision: dispatch.record.execution.capability_revision,
                                bound_dataset_ids,
                                connector_binding: dispatch
                                    .record
                                    .resource_binding
                                    .get("connector_binding")
                                    .filter(|value| !value.is_null())
                                    .cloned()
                                    .map(serde_json::from_value)
                                    .transpose()
                                    .map_err(|_| {
                                        OperationError::Failed(
                                            "runtime_connector_binding_invalid".to_string(),
                                        )
                                    })?,
                            },
                            arguments,
                        )
                        .await
                        .map_err(|error| OperationError::Failed(error.to_string()))
                } else {
                    Err(OperationError::Failed(
                        "read_capability_executor_unavailable".to_string(),
                    ))
                }
            }
            CapabilityEffect::Write => {
                if capability_id == "execute_python_code" {
                    let Some(executor) = &state.python_executor else {
                        return Err(OperationError::Failed("python_executor_unavailable".into()));
                    };
                    let lease = lease_for_record(&dispatch.record.execution);
                    let code = arguments
                        .get("code")
                        .and_then(Value::as_str)
                        .ok_or_else(|| OperationError::Failed("python_code_required".into()))?
                        .to_owned();
                    let inputs: Vec<CodeInputAttachment> = serde_json::from_value(
                        arguments
                            .get("inputs")
                            .cloned()
                            .unwrap_or_else(|| serde_json::json!([])),
                    )
                    .map_err(|_| OperationError::Failed("python_inputs_invalid".into()))?;
                    let limits: PythonSandboxLimits = serde_json::from_value(
                        arguments.get("limits").cloned().unwrap_or_else(|| {
                            serde_json::to_value(PythonSandboxLimits::default())
                                .unwrap_or(Value::Null)
                        }),
                    )
                    .map_err(|_| OperationError::Failed("python_limits_invalid".into()))?;
                    let request = PythonCodeExecutionRequest {
                        lease,
                        arguments_hash: dispatch.record.execution.arguments_hash.clone(),
                        code,
                        inputs,
                        limits,
                    };
                    return executor
                        .execute(request)
                        .await
                        .map(|value| serde_json::to_value(value).unwrap_or(Value::Null))
                        .map_err(|error| match error {
                            crate::python_code_execution::CodeExecutionError::TimedOut => {
                                OperationError::Failed("capability_timeout".into())
                            }
                            crate::python_code_execution::CodeExecutionError::Cancelled => {
                                OperationError::Failed("cancelled".into())
                            }
                            crate::python_code_execution::CodeExecutionError::SideEffectUnknown => {
                                OperationError::SideEffectUnknown
                            }
                            _ => OperationError::Failed(error.to_string()),
                        });
                }
                if capability_id == "local_node_action" {
                    let Some(broker) = &state.local_node_broker else {
                        return Err(OperationError::Failed(
                            "local_node_broker_unavailable".into(),
                        ));
                    };
                    let device_id = dispatch
                        .record
                        .resource_binding
                        .get("device_id")
                        .and_then(Value::as_str)
                        .or_else(|| {
                            dispatch
                                .record
                                .resource_binding
                                .get("connector_binding")
                                .and_then(|value| value.get("device_id"))
                                .and_then(Value::as_str)
                        })
                        .or_else(|| arguments.get("device_id").and_then(Value::as_str))
                        .ok_or_else(|| {
                            OperationError::Failed("local_node_device_required".into())
                        })?;
                    let operation_name = arguments
                        .get("operation")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            OperationError::Failed("local_node_operation_required".into())
                        })?;
                    let nested = arguments.get("arguments").cloned().unwrap_or(Value::Null);
                    let request = LocalNodeActionRequest {
                        scope: LocalNodeScope {
                            tenant_id: scope.tenant_id.clone(),
                            user_id: scope.user_id.clone(),
                            session_id: scope.session_id.clone(),
                            device_id: device_id.into(),
                        },
                        execution_id: execution_id.clone(),
                        run_id: dispatch.record.execution.run_id.clone(),
                        tool_call_id: dispatch.record.execution.tool_call_id.clone(),
                        attempt_id: dispatch.record.execution.attempt_id.clone(),
                        capability_revision: dispatch.record.execution.capability_revision,
                        effect,
                        operation: operation_name.into(),
                        arguments_hash: ai_platform_capability_contract::canonical_json_hash(
                            &nested,
                        )
                        .map_err(|_| {
                            OperationError::Failed("local_node_arguments_invalid".into())
                        })?,
                        arguments: nested,
                        idempotency_key: dispatch.record.execution.idempotency_key.clone(),
                        approval_id: dispatch.record.approval_id.clone(),
                        timeout: std::time::Duration::from_millis(
                            descriptor.timeout_ms.min(120_000),
                        ),
                    };
                    return broker
                        .execute(request, 0)
                        .await
                        .map(|value| serde_json::to_value(value).unwrap_or(Value::Null))
                        .map_err(|error| match error {
                            crate::local_node_broker::LocalNodeBrokerError::SideEffectUnknown
                            | crate::local_node_broker::LocalNodeBrokerError::Timeout => {
                                OperationError::SideEffectUnknown
                            }
                            _ => OperationError::Failed(error.to_string()),
                        });
                }
                if capability_id == "mcp_docgen__modify_document" {
                    let Some(executor) = &state.office_executor else {
                        return Err(OperationError::Failed(
                            "office_capability_executor_unavailable".into(),
                        ));
                    };
                    let format: ai_platform_office::DocumentFormat =
                        serde_json::from_value(arguments.get("format").cloned().ok_or_else(
                            || OperationError::Failed("office_format_required".into()),
                        )?)
                        .map_err(|_| OperationError::Failed("office_format_invalid".into()))?;
                    let encoded = arguments
                        .get("source_base64")
                        .and_then(Value::as_str)
                        .ok_or_else(|| OperationError::Failed("office_source_required".into()))?;
                    let source = base64::engine::general_purpose::STANDARD
                        .decode(encoded)
                        .map_err(|_| OperationError::Failed("office_source_invalid".into()))?;
                    return executor
                        .modify_existing_document(
                            &OfficeExecutionContext {
                                tenant_id: scope.tenant_id.clone(),
                                user_id: scope.user_id.clone(),
                                session_id: scope.session_id.clone(),
                                run_id: dispatch.record.execution.run_id.clone(),
                                execution_id: execution_id.clone(),
                                tool_call_id: dispatch.record.execution.tool_call_id.clone(),
                                arguments_hash: dispatch.record.execution.arguments_hash.clone(),
                            },
                            format,
                            &source,
                            arguments,
                        )
                        .await
                        .map(|value| serde_json::to_value(value).unwrap_or(Value::Null))
                        .map_err(|error| {
                            if error.is_side_effect_unknown() {
                                OperationError::SideEffectUnknown
                            } else {
                                OperationError::Failed(error.to_string())
                            }
                        });
                }
                let bound_dataset_ids = dispatch
                    .record
                    .resource_binding
                    .get("bound_dataset_ids")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect();
                let memory_policy = dispatch
                    .record
                    .resource_binding
                    .get("memory_policy")
                    .filter(|value| !value.is_null())
                    .cloned()
                    .map(serde_json::from_value)
                    .transpose()
                    .map_err(|_| {
                        OperationError::Failed("runtime_memory_policy_invalid".to_string())
                    })?;
                let write_context = WriteCapabilityContext {
                    tenant_id: scope.tenant_id.clone(),
                    user_id: scope.user_id.clone(),
                    session_id: scope.session_id.clone(),
                    execution_id: execution_id.clone(),
                    run_id: dispatch.record.execution.run_id.clone(),
                    capability_revision: dispatch.record.execution.capability_revision,
                    bound_dataset_ids,
                    memory_policy,
                };
                if matches!(
                    capability_id.as_str(),
                    "confluence_write" | "generate_image"
                ) {
                    let Some(executor) = &state.external_write_executor else {
                        return Err(OperationError::Failed(
                            "external_write_executor_unavailable".to_string(),
                        ));
                    };
                    let connector_binding = dispatch
                        .record
                        .resource_binding
                        .get("connector_binding")
                        .filter(|value| !value.is_null())
                        .cloned()
                        .map(serde_json::from_value)
                        .transpose()
                        .map_err(|_| {
                            OperationError::Failed("runtime_connector_binding_invalid".to_string())
                        })?;
                    executor
                        .execute(
                            &capability_id,
                            &ExternalWriteContext {
                                write: write_context,
                                tool_call_id: dispatch.record.execution.tool_call_id.clone(),
                                arguments_hash: dispatch
                                    .record
                                    .execution
                                    .arguments_hash
                                    .clone(),
                                connector_binding,
                            },
                            arguments,
                        )
                        .await
                        .map_err(|error| {
                            if matches!(
                                error,
                                crate::external_write_capabilities::ExternalWriteError::SideEffectUnknown
                            ) {
                                OperationError::SideEffectUnknown
                            } else {
                                OperationError::Failed(error.to_string())
                            }
                        })
                        .and_then(|result| {
                            serde_json::to_value(result).map_err(|_| {
                                OperationError::Failed(
                                    "external_write_result_invalid".to_string(),
                                )
                            })
                        })
                } else if capability_id == "generate_quiz" {
                    let Some(executor) = &state.quiz_executor else {
                        return Err(OperationError::Failed(
                            "quiz_capability_executor_unavailable".to_string(),
                        ));
                    };
                    executor
                        .persist(
                            &write_context,
                            &dispatch.record.execution.tool_call_id,
                            &dispatch.record.execution.arguments_hash,
                            arguments,
                        )
                        .await
                        .map_err(|error| {
                            if error.is_side_effect_unknown() {
                                OperationError::SideEffectUnknown
                            } else {
                                OperationError::Failed(error.to_string())
                            }
                        })
                        .and_then(|result| {
                            serde_json::to_value(result).map_err(|_| {
                                OperationError::Failed("quiz_capability_result_invalid".to_string())
                            })
                        })
                } else if capability_id == "mcp_docgen__generate_document" {
                    let Some(executor) = &state.office_executor else {
                        return Err(OperationError::Failed(
                            "office_capability_executor_unavailable".to_string(),
                        ));
                    };
                    executor
                        .generate_document(
                            &OfficeExecutionContext {
                                tenant_id: scope.tenant_id.clone(),
                                user_id: scope.user_id.clone(),
                                session_id: scope.session_id.clone(),
                                run_id: dispatch.record.execution.run_id.clone(),
                                execution_id: execution_id.clone(),
                                tool_call_id: dispatch.record.execution.tool_call_id.clone(),
                                arguments_hash: dispatch.record.execution.arguments_hash.clone(),
                            },
                            arguments,
                        )
                        .await
                        .map_err(|error| {
                            if error.is_side_effect_unknown() {
                                OperationError::SideEffectUnknown
                            } else {
                                OperationError::Failed(error.to_string())
                            }
                        })
                        .and_then(|result| {
                            serde_json::to_value(result).map_err(|_| {
                                OperationError::Failed(
                                    "office_capability_result_invalid".to_string(),
                                )
                            })
                        })
                } else {
                    let Some(executor) = &state.write_executor else {
                        return Err(OperationError::Failed(
                            "write_capability_executor_unavailable".to_string(),
                        ));
                    };
                    executor
                        .execute(&capability_id, &write_context, arguments)
                        .await
                        .map_err(|error| {
                            if error.outcome_unknown() {
                                OperationError::SideEffectUnknown
                            } else {
                                OperationError::Failed(error.to_string())
                            }
                        })
                }
            }
            CapabilityEffect::Unknown => Err(OperationError::Failed(
                "capability_effect_unknown".to_string(),
            )),
        }
    };
    let result = tokio::select! {
        _ = cancellation.notified() => {
            if matches!(effect, CapabilityEffect::Read) {
                Err(OperationError::Failed("cancelled".to_string()))
            } else {
                Err(OperationError::SideEffectUnknown)
            }
        },
        _ = lease_lost.notified() => {
            if matches!(effect, CapabilityEffect::Read) {
                Err(OperationError::Failed("capability_lease_lost".to_string()))
            } else {
                Err(OperationError::SideEffectUnknown)
            }
        },
        result = tokio::time::timeout(std::time::Duration::from_millis(timeout_ms), operation) =>
            result.unwrap_or_else(|_| {
                if matches!(effect, CapabilityEffect::Read) {
                    Err(OperationError::Failed("capability_timeout".to_string()))
                } else {
                    Err(OperationError::SideEffectUnknown)
                }
            }),
    };
    heartbeat.abort();
    let mut payload = BTreeMap::new();
    let mut status = match result {
        Ok(result) => {
            payload.insert("result".to_string(), result);
            CapabilityExecutionStatus::Succeeded
        }
        Err(OperationError::SideEffectUnknown) => {
            payload.insert(
                "error_code".to_string(),
                Value::String("side_effect_unknown".to_string()),
            );
            CapabilityExecutionStatus::SideEffectUnknown
        }
        Err(OperationError::Failed(error_code)) => {
            payload.insert("error_code".to_string(), Value::String(error_code.clone()));
            match error_code.as_str() {
                "cancelled" => CapabilityExecutionStatus::Cancelled,
                "capability_timeout" => CapabilityExecutionStatus::Timeout,
                _ => CapabilityExecutionStatus::Failed,
            }
        }
    };
    if !payload_within_limit(&payload) {
        payload.clear();
        payload.insert(
            "error_code".to_string(),
            Value::String("capability_result_too_large".to_string()),
        );
        status = CapabilityExecutionStatus::Failed;
    }
    if let Err(error) = state
        .store
        .append_event(
            &scope,
            &execution_id,
            &event_id_unchecked(&execution_id, 4),
            "terminal",
            status,
            payload,
            Some(&fence),
        )
        .await
    {
        tracing::error!(%execution_id, %error, "failed to persist capability terminal event");
    }
    state.forget_cancellation(&execution_id).await;
}

async fn terminalize_recovered_side_effect(state: &WorkerState, record: &ExecutionRecord) {
    let Some(fence) = record.dispatch_fence.as_deref() else {
        return;
    };
    let execution_id = &record.execution.execution_id;
    let mut payload = BTreeMap::new();
    let (status, event_message) = if let Some(result) = durable_recovery_result(record) {
        payload.insert("result".to_string(), result);
        (
            CapabilityExecutionStatus::Succeeded,
            "recovered durable capability receipt",
        )
    } else {
        payload.insert(
            "error_code".to_string(),
            Value::String("side_effect_unknown".to_string()),
        );
        (
            CapabilityExecutionStatus::SideEffectUnknown,
            "failed to reconcile recovered side effect",
        )
    };
    payload.insert(
        "recovery".to_string(),
        Value::String("worker_restart".to_string()),
    );
    if let Err(error) = state
        .store
        .append_event(
            &record.execution.scope(),
            execution_id,
            &event_id_unchecked(execution_id, 4),
            "terminal",
            status,
            payload,
            Some(fence),
        )
        .await
    {
        tracing::error!(
            %execution_id,
            %error,
            recovery = event_message,
            "failed to persist recovered capability terminal"
        );
    }
}

async fn recover_executions_loop(state: WorkerState) {
    loop {
        let records = match state.store.recoverable().await {
            Ok(records) => records,
            Err(error) => {
                tracing::error!(%error, "failed to load recoverable capability executions");
                Vec::new()
            }
        };
        for record in records {
            if !is_recoverable_execution(&record) {
                continue;
            }
            if needs_side_effect_unknown_recovery(&record) {
                terminalize_recovered_side_effect(&state, &record).await;
                continue;
            }
            let descriptor = state
                .capability_registry
                .get(&record.execution.capability_id)
                .cloned()
                .or_else(|| {
                    record
                        .resource_binding
                        .get("descriptor")
                        .cloned()
                        .and_then(|value| serde_json::from_value(value).ok())
                });
            let Some(descriptor) = descriptor else {
                continue;
            };
            let execution_id = record.execution.execution_id.clone();
            let (_, registered) = state.register_cancellation(&execution_id).await;
            if !registered {
                continue;
            }
            tokio::spawn(run_execution(
                state.clone(),
                record.execution.scope(),
                execution_id,
                record.execution.capability_id,
                record.arguments,
                descriptor,
            ));
        }
        tokio::time::sleep(std::time::Duration::from_secs(5)).await;
    }
}

async fn get_execution(
    State(state): State<WorkerState>,
    headers: HeaderMap,
    Path(execution_id): Path<String>,
) -> Result<impl IntoResponse, HttpError> {
    let scope = authorize(&headers, &state)?;
    validate_execution_id(&execution_id)?;
    let execution = state
        .store
        .get(&scope, &execution_id)
        .await
        .map_err(store_error)?;
    Ok(Json(execution.execution))
}

async fn get_events(
    State(state): State<WorkerState>,
    headers: HeaderMap,
    Path(execution_id): Path<String>,
    Query(query): Query<EventsQuery>,
) -> Result<impl IntoResponse, HttpError> {
    let scope = authorize(&headers, &state)?;
    validate_execution_id(&execution_id)?;
    Ok(Json(
        state
            .store
            .events(&scope, &execution_id, query.after_sequence)
            .await
            .map_err(store_error)?,
    ))
}

async fn cancel_execution(
    State(state): State<WorkerState>,
    headers: HeaderMap,
    Path(execution_path): Path<String>,
) -> Result<impl IntoResponse, HttpError> {
    let scope = authorize(&headers, &state)?;
    let execution_id = execution_path.strip_suffix(":cancel").ok_or_else(|| {
        error(
            StatusCode::BAD_REQUEST,
            "execution_id_invalid",
            "cancel path is invalid",
        )
    })?;
    validate_execution_id(execution_id)?;
    state.request_cancel(execution_id).await;
    Ok(Json(
        state
            .store
            .cancel(&scope, execution_id)
            .await
            .map_err(store_error)?
            .execution,
    ))
}

fn validate_execution_id(value: &str) -> Result<(), HttpError> {
    Uuid::parse_str(value).map(|_| ()).map_err(|_| {
        error(
            StatusCode::BAD_REQUEST,
            "execution_id_invalid",
            "execution id is invalid",
        )
    })
}

fn event_id(execution_id: &str, ordinal: u8) -> Result<String, HttpError> {
    validate_execution_id(execution_id)?;
    Ok(event_id_unchecked(execution_id, ordinal))
}

fn event_id_unchecked(execution_id: &str, ordinal: u8) -> String {
    let mut bytes = *Uuid::parse_str(execution_id)
        .expect("validated execution id")
        .as_bytes();
    bytes[15] ^= ordinal;
    Uuid::from_bytes(bytes).to_string()
}

pub fn payload_within_limit(payload: &BTreeMap<String, Value>) -> bool {
    serde_json::to_vec(payload).is_ok_and(|encoded| encoded.len() <= MAX_EVENT_PAYLOAD_BYTES)
}

trait ExecutionScope {
    fn scope(&self) -> CapabilityScopeV2;
}

impl ExecutionScope for CapabilityExecutionV2 {
    fn scope(&self) -> CapabilityScopeV2 {
        CapabilityScopeV2 {
            tenant_id: self.tenant_id.clone(),
            user_id: self.user_id.clone(),
            session_id: self.session_id.clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::MemoryStore;
    use ai_platform_capability_contract::canonical_json_hash;

    #[test]
    fn deterministic_event_ids_are_distinct() {
        let execution_id = "00000000-0000-0000-0000-000000000001";
        assert_ne!(
            event_id_unchecked(execution_id, 1),
            event_id_unchecked(execution_id, 2)
        );
    }

    #[test]
    fn event_payload_limit_is_bounded() {
        let mut payload = BTreeMap::new();
        payload.insert("value".to_string(), Value::String("x".repeat(1024)));
        assert!(payload_within_limit(&payload));
        payload.insert(
            "large".to_string(),
            Value::String("x".repeat(MAX_EVENT_PAYLOAD_BYTES)),
        );
        assert!(!payload_within_limit(&payload));
    }

    fn state(fixtures_enabled: bool) -> WorkerState {
        WorkerState::try_new(
            Arc::new(MemoryStore::default()),
            "internal-token".to_string(),
            b"lease-signing-secret-that-is-long-enough".to_vec(),
            fixtures_enabled,
        )
        .expect("checked-in catalog must load")
    }

    #[test]
    fn default_registry_hides_fixtures_and_unimplemented_capabilities() {
        let state = state(false);
        for capability_id in [
            "confluence_read",
            "read_tool_artifact",
            "search_knowledge_base",
            "todo_read",
            "web_fetch",
            "read_attachment",
        ] {
            assert!(
                state.capability_registry.contains_key(capability_id),
                "{capability_id}"
            );
        }
        assert!(
            !state
                .capability_registry
                .contains_key("execute_python_code")
        );
        assert!(!state.capability_registry.contains_key("local_node_action"));
    }

    #[test]
    fn fixture_flag_adds_only_the_two_contract_fixtures() {
        let state = state(true);
        assert!(state.capability_registry.contains_key("platform.echo"));
        assert!(
            state
                .capability_registry
                .contains_key("platform.read_fixture")
        );
        assert!(state.capability_registry.len() >= 10);
    }

    #[test]
    fn write_flag_exposes_only_implemented_writers() {
        let state = WorkerState::try_new_with_writes(
            Arc::new(MemoryStore::default()),
            "internal-token".to_string(),
            b"lease-signing-secret-that-is-long-enough".to_vec(),
            false,
            true,
        )
        .expect("checked-in catalog must load");
        assert!(state.capability_registry.contains_key("todo_write"));
        assert!(state.capability_registry.contains_key("update_user_memory"));
        assert!(state.capability_registry.contains_key("confluence_write"));
        assert!(state.capability_registry.contains_key("generate_image"));
        assert!(state.capability_registry.contains_key("generate_quiz"));
        assert!(
            state
                .capability_registry
                .contains_key("mcp_docgen__generate_document")
        );
        assert!(
            state
                .capability_registry
                .contains_key("mcp_docgen__modify_document")
        );
        assert!(
            state
                .capability_registry
                .contains_key("mcp_docgen__preview_document")
        );
        assert!(
            state
                .capability_registry
                .contains_key("execute_python_code")
        );
        assert!(state.capability_registry.contains_key("local_node_action"));
        assert!(state.capability_registry.contains_key("search_web"));
        assert_eq!(state.capability_registry.len(), 19);
    }

    #[test]
    fn cloned_state_shares_the_same_immutable_registry() {
        let state = state(false);
        let clone = state.clone();
        assert!(Arc::ptr_eq(
            &state.capability_registry,
            &clone.capability_registry
        ));
    }

    #[test]
    fn declarative_kind_tag_binds_snapshot_capability_type() {
        let state = state(false);
        assert_eq!(
            descriptor_type(&state.capability_registry["search_knowledge_base"])
                .expect("knowledge kind"),
            "knowledge"
        );
        assert_eq!(
            descriptor_type(&state.capability_registry["web_fetch"]).expect("tool kind"),
            "tool"
        );
    }

    fn dynamic_mcp_descriptor() -> CapabilityDescriptorV2 {
        let input_schema = serde_json::json!({
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": false
        });
        CapabilityDescriptorV2 {
            schema_version: ai_platform_capability_contract::CAPABILITY_DESCRIPTOR_SCHEMA_VERSION
                .to_string(),
            id: "mcp.docs.search".to_string(),
            name: "search_docs".to_string(),
            version: "2026-08-24".to_string(),
            description: "Search the bound docs MCP server".to_string(),
            schema_hash: canonical_json_hash(&input_schema).expect("schema hash"),
            input_schema,
            output_schema: serde_json::json!({"type": "object"}),
            effect: CapabilityEffect::Read,
            approval_policy: ai_platform_capability_contract::ApprovalPolicy::Never,
            execution_mode: ai_platform_capability_contract::ExecutionMode::Inline,
            timeout_ms: 30_000,
            tags: vec!["kind:mcp".to_string()],
            protocol: "mcp".to_string(),
            connector_binding: None,
        }
    }

    fn dynamic_binding(descriptor: &CapabilityDescriptorV2) -> RuntimeCapabilityBinding {
        RuntimeCapabilityBinding {
            snapshot_id: "snapshot-a".to_string(),
            capability_revision: 7,
            capability_type: "mcp".to_string(),
            name: descriptor.name.clone(),
            capability_id: descriptor.id.clone(),
            capability_version: descriptor.version.clone(),
            schema_hash: descriptor.schema_hash.clone(),
            effect: descriptor.effect,
            approval_policy: descriptor.approval_policy,
            bound_dataset_ids: Default::default(),
            connector_binding: None,
            memory_policy: None,
        }
    }

    #[test]
    fn dynamic_mcp_descriptor_is_accepted_without_static_catalog_entry() {
        let descriptor = dynamic_mcp_descriptor();
        let binding = dynamic_binding(&descriptor);
        assert_eq!(descriptor_type(&descriptor).expect("mcp kind"), "mcp");
        verify_descriptor_against_runtime_binding(&descriptor, &binding, None)
            .expect("snapshot-bound dynamic descriptor should dispatch");
    }

    #[test]
    fn dynamic_descriptor_schema_and_connector_tampering_is_rejected() {
        let descriptor = dynamic_mcp_descriptor();
        let binding = dynamic_binding(&descriptor);
        let mut schema_tamper = descriptor.clone();
        schema_tamper.schema_hash =
            "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff".to_string();
        assert_eq!(
            verify_descriptor_against_runtime_binding(&schema_tamper, &binding, None),
            Err(StoreError::RuntimeBindingUnauthorized)
        );
        assert_eq!(
            verify_descriptor_against_runtime_binding(
                &descriptor,
                &binding,
                Some(&serde_json::json!({"binding_type": "grant"})),
            ),
            Err(StoreError::RuntimeBindingUnauthorized)
        );
    }
}
