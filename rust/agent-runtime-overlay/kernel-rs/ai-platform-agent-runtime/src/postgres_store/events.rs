use codex_protocol::ThreadId;
use codex_thread_store::ThreadStoreError;
use codex_thread_store::ThreadStoreResult;
use serde_json::Value;
use sha2::Digest;
use sha2::Sha256;
use sqlx::Row;
use uuid::Uuid;

use crate::AssistantTurnEventV1;
use crate::SequencedAssistantTurnEventV1;

use super::PostgresThreadStore;
use super::json_error;
use super::payload_hash;
use super::store_error;
use super::thread_uuid;

pub(crate) struct PlatformLifecycleEvent {
    pub(crate) kernel_thread_id: ThreadId,
    pub(crate) turn_id: String,
    pub(crate) item_id: Option<String>,
    pub(crate) event_key: String,
    pub(crate) item_type: String,
    pub(crate) status: String,
    pub(crate) payload: Value,
}

impl PostgresThreadStore {
    /// Appends a platform lifecycle receipt to the immutable Item Store.
    /// Event ids are derived from the idempotency key so a retry of the same
    /// callback is a no-op rather than a second tool result.
    pub(crate) async fn append_platform_lifecycle_event(
        &self,
        event: PlatformLifecycleEvent,
    ) -> ThreadStoreResult<i64> {
        if event.event_key.is_empty() || event.event_key.len() > 255 || event.turn_id.is_empty() {
            return Err(ThreadStoreError::InvalidRequest {
                message: "platform lifecycle identifiers are invalid".to_string(),
            });
        }
        let kernel_thread_uuid = thread_uuid(event.kernel_thread_id)?;
        let scope = self.member_scope(kernel_thread_uuid).await?;
        let payload_hash = payload_hash(&event.payload)?;
        let mut digest = Sha256::new();
        digest.update(event.event_key.as_bytes());
        let digest = digest.finalize();
        let mut event_id_bytes = [0u8; 16];
        event_id_bytes.copy_from_slice(&digest[..16]);
        let event_id = Uuid::from_bytes(event_id_bytes);
        sqlx::query_scalar::<_, i64>(
            r#"
            SELECT append_assistant_runtime_item(
                $1, $2, $3, $4, $5, $6, $7, $8, $9,
                'agent-runtime/tool-lifecycle', $10, $11, $12, $13
            )
            "#,
        )
        .bind(scope.root_thread_id)
        .bind(kernel_thread_uuid)
        .bind(&scope.tenant_id)
        .bind(&scope.user_id)
        .bind(&scope.session_id)
        .bind(event_id)
        .bind(event.event_key)
        .bind(event.turn_id)
        .bind(event.item_id)
        .bind(event.item_type)
        .bind(event.status)
        .bind(event.payload)
        .bind(payload_hash)
        .fetch_one(&self.pool)
        .await
        .map_err(store_error)
    }

    /// Reads the durable lifecycle receipts needed to reconstruct a turn
    /// after a Runtime process restart.
    pub async fn read_platform_lifecycle_events(
        &self,
        kernel_thread_id: ThreadId,
        turn_id: &str,
    ) -> ThreadStoreResult<Vec<Value>> {
        let kernel_thread_uuid = thread_uuid(kernel_thread_id)?;
        let _scope = self.member_scope(kernel_thread_uuid).await?;
        sqlx::query(
            r#"
            SELECT payload
              FROM assistant_runtime_items
             WHERE kernel_thread_id = $1
               AND turn_id = $2
               AND event_type = 'agent-runtime/tool-lifecycle'
             ORDER BY sequence
            "#,
        )
        .bind(kernel_thread_uuid)
        .bind(turn_id)
        .fetch_all(&self.pool)
        .await
        .map_err(store_error)?
        .into_iter()
        .map(|row| row.try_get("payload").map_err(store_error))
        .collect()
    }

    pub(crate) async fn read_platform_lifecycle_turn_ids(
        &self,
        kernel_thread_id: ThreadId,
    ) -> ThreadStoreResult<Vec<String>> {
        let kernel_thread_uuid = thread_uuid(kernel_thread_id)?;
        let _scope = self.member_scope(kernel_thread_uuid).await?;
        sqlx::query(
            r#"
            SELECT DISTINCT turn_id
              FROM assistant_runtime_items
             WHERE kernel_thread_id = $1
               AND event_type = 'agent-runtime/tool-lifecycle'
               AND turn_id IS NOT NULL
             ORDER BY turn_id
            "#,
        )
        .bind(kernel_thread_uuid)
        .fetch_all(&self.pool)
        .await
        .map_err(store_error)?
        .into_iter()
        .map(|row| row.try_get("turn_id").map_err(store_error))
        .collect()
    }

    /// Admission check for a turn terminal. Any dispatched call without a
    /// result is closed durably as unknown before the caller may project the
    /// terminal envelope.
    pub(crate) async fn admit_turn_terminal(
        &self,
        kernel_thread_id: ThreadId,
        turn_id: &str,
    ) -> ThreadStoreResult<Vec<String>> {
        let events = self
            .read_platform_lifecycle_events(kernel_thread_id, turn_id)
            .await?;
        let unclosed = crate::platform_lifecycle::unclosed_tool_call_ids(&events);
        for call_id in &unclosed {
            let payload = serde_json::json!({
                "schema_version": "agent-runtime-tool-lifecycle/v1",
                "turn_id": turn_id,
                "tool_call_id": call_id,
                "lifecycle": "terminal",
                "result_status": "side_effect_unknown",
                "recovery": "terminal_admission",
            });
            self.append_platform_lifecycle_event(PlatformLifecycleEvent {
                kernel_thread_id,
                turn_id: turn_id.to_string(),
                item_id: Some(call_id.clone()),
                event_key: format!("tool-result/{turn_id}/{call_id}"),
                item_type: "tool_result".to_string(),
                status: "side_effect_unknown".to_string(),
                payload,
            })
            .await?;
        }
        Ok(unclosed)
    }

    /// Resolves one Agent member to its platform root ownership.
    pub async fn identity_for_kernel_thread(
        &self,
        kernel_thread_id: ThreadId,
    ) -> ThreadStoreResult<super::PlatformThreadIdentity> {
        let scope = self.member_scope(thread_uuid(kernel_thread_id)?).await?;
        Ok(super::PlatformThreadIdentity::new(
            ThreadId::from_u128(scope.root_thread_id.as_u128()),
            scope.tenant_id,
            scope.user_id,
            scope.session_id,
        ))
    }

    /// Verifies a caller-provided root identity without creating or changing it.
    pub async fn verify_root_identity(
        &self,
        identity: &super::PlatformThreadIdentity,
    ) -> ThreadStoreResult<()> {
        let root_id = thread_uuid(identity.runtime_thread_id)?;
        let scope = self.root_scope(root_id).await?;
        if scope.tenant_id != identity.tenant_id
            || scope.user_id != identity.user_id
            || scope.session_id != identity.session_id
        {
            return Err(ThreadStoreError::ThreadNotFound {
                thread_id: identity.runtime_thread_id,
            });
        }
        Ok(())
    }

    /// Appends one projected V1 event to the root-monotonic durable event log.
    pub async fn append_v1_event(
        &self,
        kernel_thread_id: ThreadId,
        event_id: Uuid,
        event_key: &str,
        event: &AssistantTurnEventV1,
    ) -> ThreadStoreResult<i64> {
        if event_key.is_empty() || event_key.len() > 255 {
            return Err(ThreadStoreError::InvalidRequest {
                message: "runtime event key must contain 1 to 255 characters".to_string(),
            });
        }
        let kernel_thread_uuid = thread_uuid(kernel_thread_id)?;
        let scope = self.member_scope(kernel_thread_uuid).await?;
        let payload = serde_json::to_value(event).map_err(json_error)?;
        let hash = payload_hash(&payload)?;
        let turn_id = event.data.get("run_id").and_then(Value::as_str);
        let item_id = event
            .data
            .get("tool_call_id")
            .or_else(|| event.data.get("item_id"))
            .and_then(Value::as_str);
        let status = event.data.get("status").and_then(Value::as_str);
        sqlx::query_scalar::<_, i64>(
            r#"
            SELECT append_assistant_runtime_item(
                $1, $2, $3, $4, $5, $6, $7, $8, $9,
                $10, 'assistant_turn_event_v1', $11, $12, $13
            )
            "#,
        )
        .bind(scope.root_thread_id)
        .bind(kernel_thread_uuid)
        .bind(&scope.tenant_id)
        .bind(&scope.user_id)
        .bind(&scope.session_id)
        .bind(event_id)
        .bind(event_key)
        .bind(turn_id)
        .bind(item_id)
        .bind(format!("compat/v1/{}", event.event_type))
        .bind(status)
        .bind(payload)
        .bind(hash)
        .fetch_one(&self.pool)
        .await
        .map_err(store_error)
    }

    /// Reads an ordered, bounded V1 cursor page for one platform root Thread.
    pub async fn read_v1_events_after(
        &self,
        root_thread_id: ThreadId,
        after_sequence: i64,
        limit: i64,
    ) -> ThreadStoreResult<Vec<SequencedAssistantTurnEventV1>> {
        if after_sequence < 0 || !(1..=1_000).contains(&limit) {
            return Err(ThreadStoreError::InvalidRequest {
                message: "event cursor must be non-negative and limit must be 1 to 1000"
                    .to_string(),
            });
        }
        let rows = sqlx::query(
            r#"
            SELECT sequence, payload
            FROM assistant_runtime_items
            WHERE runtime_thread_id = $1
              AND sequence > $2
              AND event_type LIKE 'compat/v1/%'
            ORDER BY sequence
            LIMIT $3
            "#,
        )
        .bind(thread_uuid(root_thread_id)?)
        .bind(after_sequence)
        .bind(limit)
        .fetch_all(&self.pool)
        .await
        .map_err(store_error)?;
        rows.into_iter()
            .map(|row| {
                let sequence = row.try_get("sequence").map_err(store_error)?;
                let payload: Value = row.try_get("payload").map_err(store_error)?;
                let event = serde_json::from_value(payload).map_err(json_error)?;
                Ok(SequencedAssistantTurnEventV1 { sequence, event })
            })
            .collect()
    }
}
