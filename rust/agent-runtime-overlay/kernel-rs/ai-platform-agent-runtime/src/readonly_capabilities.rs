//! Tenant/revision-bound read-only capability projection for the platform host.
//!
//! This is intentionally a data bridge. It does not select a tool from user
//! text, execute a tool, or introduce another Agent loop. The host resolves
//! authorized contributors and supplies their immutable snapshot metadata;
//! this module validates and projects that data for one Agent turn.

use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::fmt::Display;
use std::fmt::Formatter;

use serde::Deserialize;
use serde::Serialize;
use serde_json::Map;
use serde_json::Value;

pub const READONLY_CAPABILITY_SCHEMA_VERSION: &str = "agent-readonly-capability/v1";

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct RuntimeCapabilityScope {
    pub tenant_id: String,
    pub user_id: String,
    pub session_id: String,
    pub capability_revision: i64,
    #[serde(default)]
    pub snapshot_id: String,
}

impl RuntimeCapabilityScope {
    pub fn validate(&self) -> Result<(), ReadonlyCapabilityError> {
        if self.tenant_id.is_empty() || self.user_id.is_empty() || self.session_id.is_empty() {
            return Err(ReadonlyCapabilityError::new(
                "runtime_capability_scope_invalid",
            ));
        }
        if self.capability_revision < 1 {
            return Err(ReadonlyCapabilityError::new(
                "runtime_capability_revision_invalid",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReadonlyItemKind {
    Context,
    TurnInput,
    Knowledge,
    Attachment,
    Citation,
    Artifact,
    OfficeRead,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct CapabilityItem {
    pub item_id: String,
    pub kind: ReadonlyItemKind,
    pub tenant_id: String,
    pub capability_revision: i64,
    pub source: String,
    pub payload: Value,
    #[serde(default = "default_untrusted")]
    pub untrusted: bool,
    #[serde(default = "default_authority")]
    pub authority: String,
}

fn default_untrusted() -> bool {
    true
}

fn default_authority() -> String {
    "non_authoritative".to_string()
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct CapabilityDescriptor {
    pub name: String,
    pub description: String,
    pub schema: Value,
    pub tenant_id: String,
    pub capability_revision: i64,
    pub source: String,
    #[serde(default = "default_tool_kind")]
    pub kind: String,
    #[serde(default = "default_read_only")]
    pub read_only: bool,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default = "default_internal_protocol")]
    pub protocol: String,
    #[serde(default = "default_general_category")]
    pub category: String,
    /// Stable AgentSpec capability identity. These fields are optional for
    /// legacy catalog entries, but dynamic invocation rejects a descriptor
    /// that cannot be bound to the signed allowlist.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub schema_hash: Option<String>,
    #[serde(default)]
    pub metadata: Map<String, Value>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct CapabilityAllowlistEntry {
    #[serde(rename = "type")]
    pub capability_type: String,
    pub name: String,
    pub id: String,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub schema_hash: Option<String>,
}

impl CapabilityAllowlistEntry {
    fn validate(&self) -> Result<(), ReadonlyCapabilityError> {
        if self.capability_type.is_empty()
            || self.name.is_empty()
            || self.id.is_empty()
            || self.version.as_deref().is_some_and(str::is_empty)
            || !valid_schema_hash(self.schema_hash.as_deref())
        {
            return Err(ReadonlyCapabilityError::new(
                "runtime_capability_allowlist_entry_invalid",
            ));
        }
        if matches!(self.capability_type.as_str(), "mcp" | "skill")
            && (self.version.is_none() || self.schema_hash.is_none())
        {
            return Err(ReadonlyCapabilityError::new(
                "runtime_capability_allowlist_binding_invalid",
            ));
        }
        Ok(())
    }
}

fn valid_schema_hash(value: Option<&str>) -> bool {
    value
        .and_then(|value| value.strip_prefix("sha256:"))
        .is_some_and(|digest| {
            digest.len() == 64
                && digest
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
}

fn default_tool_kind() -> String {
    "tool".to_string()
}

fn default_read_only() -> bool {
    // Missing effect metadata must not silently become executable authority.
    false
}

fn default_internal_protocol() -> String {
    "internal".to_string()
}

fn default_general_category() -> String {
    "general".to_string()
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct MetadataFilter {
    pub kind: Option<String>,
    pub source: Option<String>,
    pub tags: BTreeSet<String>,
    pub protocol: Option<String>,
    pub category: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReadonlyCapabilityError(String);

impl ReadonlyCapabilityError {
    fn new(code: &str) -> Self {
        Self(code.to_string())
    }
}

impl Display for ReadonlyCapabilityError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for ReadonlyCapabilityError {}

fn validate_binding(
    scope: &RuntimeCapabilityScope,
    tenant_id: &str,
    capability_revision: i64,
) -> Result<(), ReadonlyCapabilityError> {
    scope.validate()?;
    if tenant_id != scope.tenant_id || capability_revision != scope.capability_revision {
        return Err(ReadonlyCapabilityError::new(
            "runtime_capability_revision_mismatch",
        ));
    }
    Ok(())
}

fn descriptor_is_valid(
    scope: &RuntimeCapabilityScope,
    descriptor: &CapabilityDescriptor,
) -> Result<(), ReadonlyCapabilityError> {
    validate_binding(scope, &descriptor.tenant_id, descriptor.capability_revision)?;
    if descriptor.name.is_empty()
        || descriptor.id.is_empty()
        || descriptor.source.is_empty()
        || descriptor.description.is_empty()
        || !valid_schema_hash(descriptor.schema_hash.as_deref())
        || matches!(descriptor.kind.as_str(), "mcp" | "skill") && descriptor.version.is_none()
    {
        return Err(ReadonlyCapabilityError::new(
            "runtime_capability_descriptor_invalid",
        ));
    }
    if !descriptor.read_only {
        return Err(ReadonlyCapabilityError::new(
            "runtime_readonly_capability_required",
        ));
    }
    if !descriptor.schema.is_object() {
        return Err(ReadonlyCapabilityError::new(
            "runtime_capability_schema_invalid",
        ));
    }
    Ok(())
}

fn capability_allowlist(
    payload: &Value,
) -> Result<Vec<CapabilityAllowlistEntry>, ReadonlyCapabilityError> {
    let entries = payload
        .get("capability_allowlist")
        .and_then(Value::as_array)
        .ok_or_else(|| ReadonlyCapabilityError::new("runtime_capability_allowlist_missing"))?;
    let mut result = Vec::with_capacity(entries.len());
    let mut identities = BTreeSet::new();
    for value in entries {
        let entry: CapabilityAllowlistEntry =
            serde_json::from_value(value.clone()).map_err(|_| {
                ReadonlyCapabilityError::new("runtime_capability_allowlist_entry_invalid")
            })?;
        entry.validate()?;
        let identity = (
            entry.capability_type.clone(),
            entry.name.clone(),
            entry.id.clone(),
            entry.version.clone(),
            entry.schema_hash.clone(),
        );
        if !identities.insert(identity) {
            return Err(ReadonlyCapabilityError::new(
                "runtime_capability_allowlist_collision",
            ));
        }
        result.push(entry);
    }
    Ok(result)
}

fn descriptor_allowlist_entry(
    descriptor: &CapabilityDescriptor,
    allowlist: &[CapabilityAllowlistEntry],
) -> Result<CapabilityAllowlistEntry, ReadonlyCapabilityError> {
    let candidates = allowlist
        .iter()
        .filter(|entry| {
            entry.capability_type.as_str() == descriptor.kind.as_str()
                && entry.name.as_str() == descriptor.name.as_str()
                && entry.id.as_str() == descriptor.id.as_str()
                && entry.version.as_ref() == descriptor.version.as_ref()
                && entry.schema_hash.as_ref() == descriptor.schema_hash.as_ref()
        })
        .cloned()
        .collect::<Vec<_>>();
    if candidates.len() != 1 {
        return Err(ReadonlyCapabilityError::new(
            "runtime_capability_allowlist_scope_mismatch",
        ));
    }
    Ok(candidates.into_iter().next().expect("one candidate"))
}

/// Resolve a dynamic tool against the immutable snapshot descriptor and its
/// signed AgentSpec allowlist. The model supplies only the tool name and
/// arguments; all identity/version/schema fields come from this payload.
pub fn resolve_dynamic_tool(
    payload: &Value,
    namespace: Option<&str>,
    tool_name: &str,
) -> Result<(Vec<CapabilityAllowlistEntry>, CapabilityAllowlistEntry), ReadonlyCapabilityError> {
    if tool_name.is_empty() {
        return Err(ReadonlyCapabilityError::new("runtime_dynamic_tool_invalid"));
    }
    let mut candidates = Vec::new();
    for key in ["tools", "mcp"] {
        for value in payload
            .get(key)
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let descriptor: CapabilityDescriptor =
                serde_json::from_value(value.clone()).map_err(|_| {
                    ReadonlyCapabilityError::new("runtime_capability_descriptor_invalid")
                })?;
            if descriptor.read_only
                && descriptor.name == tool_name
                && namespace.is_none_or(|requested| {
                    descriptor
                        .metadata
                        .get("namespace")
                        .and_then(Value::as_str)
                        .is_some_and(|value| value == requested)
                        || descriptor.source == requested
                })
            {
                candidates.push(descriptor);
            }
        }
    }
    if candidates.len() != 1 {
        return Err(ReadonlyCapabilityError::new(
            "runtime_dynamic_tool_not_authorized",
        ));
    }
    let allowlist = capability_allowlist(payload)?;
    let expected = descriptor_allowlist_entry(&candidates[0], &allowlist)?;
    Ok((allowlist, expected))
}

fn matches_filter(descriptor: &CapabilityDescriptor, filter: &MetadataFilter) -> bool {
    filter
        .kind
        .as_deref()
        .is_none_or(|kind| kind == descriptor.kind)
        && filter
            .source
            .as_deref()
            .is_none_or(|source| source == descriptor.source)
        && filter
            .protocol
            .as_deref()
            .is_none_or(|protocol| protocol == descriptor.protocol)
        && filter
            .category
            .as_deref()
            .is_none_or(|category| category == descriptor.category)
        && filter
            .tags
            .iter()
            .all(|tag| descriptor.tags.iter().any(|value| value == tag))
}

/// Validate, metadata-filter, deduplicate, and stably order read-only schemas.
///
/// The typed [`MetadataFilter`] deliberately has no prompt/query field. The
/// caller cannot route capabilities through user text at this boundary.
pub fn discover_readonly(
    scope: &RuntimeCapabilityScope,
    descriptors: impl IntoIterator<Item = CapabilityDescriptor>,
    filter: &MetadataFilter,
) -> Result<Vec<CapabilityDescriptor>, ReadonlyCapabilityError> {
    let mut selected = BTreeMap::<(String, String), CapabilityDescriptor>::new();
    for mut descriptor in descriptors {
        descriptor_is_valid(scope, &descriptor)?;
        descriptor.tags.sort();
        descriptor.tags.dedup();
        if !matches_filter(&descriptor, filter) {
            continue;
        }
        let key = (descriptor.source.clone(), descriptor.name.clone());
        if selected.insert(key, descriptor).is_some() {
            return Err(ReadonlyCapabilityError::new(
                "runtime_capability_name_collision",
            ));
        }
    }
    Ok(selected.into_values().collect())
}

/// Validate a gateway payload and render it as one clearly marked, untrusted
/// Agent input item. The Runtime never turns this payload into executable
/// tools; write-capable descriptors are rejected before the kernel sees them.
pub fn render_turn_input(
    scope: &RuntimeCapabilityScope,
    payload: &Value,
) -> Result<Option<String>, ReadonlyCapabilityError> {
    let object = payload
        .as_object()
        .ok_or_else(|| ReadonlyCapabilityError::new("runtime_readonly_payload_invalid"))?;
    if object.get("schema_version").and_then(Value::as_str)
        != Some(READONLY_CAPABILITY_SCHEMA_VERSION)
        || object.get("tenant_id").and_then(Value::as_str) != Some(scope.tenant_id.as_str())
        || object.get("capability_revision").and_then(Value::as_i64)
            != Some(scope.capability_revision)
    {
        return Err(ReadonlyCapabilityError::new(
            "runtime_capability_revision_mismatch",
        ));
    }
    for key in ["items", "tools", "mcp"] {
        if !object.get(key).is_none_or(Value::is_array) {
            return Err(ReadonlyCapabilityError::new(
                "runtime_readonly_payload_invalid",
            ));
        }
    }
    if object.get("capability_allowlist").is_some() {
        capability_allowlist(payload)?;
    }
    if let Some(items) = object.get("items").and_then(Value::as_array) {
        for item in items {
            let item_object = item
                .as_object()
                .ok_or_else(|| ReadonlyCapabilityError::new("runtime_capability_item_invalid"))?;
            validate_binding(
                scope,
                item_object
                    .get("tenant_id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        ReadonlyCapabilityError::new("runtime_capability_item_invalid")
                    })?,
                item_object
                    .get("capability_revision")
                    .and_then(Value::as_i64)
                    .ok_or_else(|| {
                        ReadonlyCapabilityError::new("runtime_capability_item_invalid")
                    })?,
            )?;
            if item_object
                .get("payload")
                .is_none_or(|value| !value.is_object())
            {
                return Err(ReadonlyCapabilityError::new(
                    "runtime_capability_item_invalid",
                ));
            }
        }
    }
    for key in ["tools", "mcp"] {
        if let Some(descriptors) = object.get(key).and_then(Value::as_array) {
            for descriptor in descriptors {
                let descriptor: CapabilityDescriptor = serde_json::from_value(descriptor.clone())
                    .map_err(|_| {
                    ReadonlyCapabilityError::new("runtime_capability_descriptor_invalid")
                })?;
                descriptor_is_valid(scope, &descriptor)?;
            }
        }
    }
    if object
        .get("items")
        .and_then(Value::as_array)
        .is_none_or(Vec::is_empty)
        && object
            .get("tools")
            .and_then(Value::as_array)
            .is_none_or(Vec::is_empty)
        && object
            .get("mcp")
            .and_then(Value::as_array)
            .is_none_or(Vec::is_empty)
    {
        return Ok(None);
    }
    let encoded = serde_json::to_string(payload)
        .map_err(|_| ReadonlyCapabilityError::new("runtime_readonly_payload_invalid"))?;
    Ok(Some(format!(
        "[AI_PLATFORM_READONLY_CONTEXT_V1]\n{encoded}\n[/AI_PLATFORM_READONLY_CONTEXT_V1]"
    )))
}

/// Returns whether a dynamic tool name is explicitly present in the
/// immutable, revision-bound read-only descriptor set. An absent descriptor
/// is never treated as authorized. Namespaces are accepted from descriptor
/// metadata or the descriptor source so the wire payload remains backwards
/// compatible with both internal and MCP projections.
pub fn allows_dynamic_tool(payload: &Value, namespace: Option<&str>, tool_name: &str) -> bool {
    if tool_name.is_empty() {
        return false;
    }
    ["tools", "mcp"]
        .iter()
        .filter_map(|key| payload.get(*key).and_then(Value::as_array))
        .flatten()
        .filter(|value| {
            let Ok(descriptor) = serde_json::from_value::<CapabilityDescriptor>((*value).clone())
            else {
                return false;
            };
            if !descriptor.read_only || descriptor.name != tool_name {
                return false;
            }
            namespace.is_none_or(|requested| {
                descriptor
                    .metadata
                    .get("namespace")
                    .and_then(Value::as_str)
                    .is_some_and(|value| value == requested)
                    || descriptor.source == requested
            })
        })
        .count()
        == 1
}

/// Copy one item into the immutable runtime scope and mark it non-authoritative.
pub fn project_item(
    scope: &RuntimeCapabilityScope,
    item_id: impl Into<String>,
    kind: ReadonlyItemKind,
    source: impl Into<String>,
    payload: Value,
) -> Result<CapabilityItem, ReadonlyCapabilityError> {
    validate_binding(scope, &scope.tenant_id, scope.capability_revision)?;
    let item_id = item_id.into();
    let source = source.into();
    if item_id.is_empty() || source.is_empty() || !payload.is_object() {
        return Err(ReadonlyCapabilityError::new(
            "runtime_capability_item_invalid",
        ));
    }
    Ok(CapabilityItem {
        item_id,
        kind,
        tenant_id: scope.tenant_id.clone(),
        capability_revision: scope.capability_revision,
        source,
        payload,
        untrusted: true,
        authority: default_authority(),
    })
}

pub fn project_knowledge(
    scope: &RuntimeCapabilityScope,
    source_id: impl Into<String>,
    content: impl Into<String>,
) -> Result<CapabilityItem, ReadonlyCapabilityError> {
    let source_id = source_id.into();
    project_item(
        scope,
        format!("knowledge:{source_id}"),
        ReadonlyItemKind::Knowledge,
        source_id,
        serde_json::json!({"content": content.into()}),
    )
}

pub fn project_attachment(
    scope: &RuntimeCapabilityScope,
    attachment_id: impl Into<String>,
    content_ref: impl Into<String>,
) -> Result<CapabilityItem, ReadonlyCapabilityError> {
    let attachment_id = attachment_id.into();
    project_item(
        scope,
        format!("attachment:{attachment_id}"),
        ReadonlyItemKind::Attachment,
        attachment_id,
        serde_json::json!({"content_ref": content_ref.into()}),
    )
}

pub fn project_citation(
    scope: &RuntimeCapabilityScope,
    citation_id: impl Into<String>,
    source_id: impl Into<String>,
    locator: impl Into<String>,
) -> Result<CapabilityItem, ReadonlyCapabilityError> {
    let citation_id = citation_id.into();
    project_item(
        scope,
        format!("citation:{citation_id}"),
        ReadonlyItemKind::Citation,
        source_id,
        serde_json::json!({"locator": locator.into()}),
    )
}

pub fn project_artifact(
    scope: &RuntimeCapabilityScope,
    artifact_id: impl Into<String>,
    content_ref: impl Into<String>,
    media_type: impl Into<String>,
) -> Result<CapabilityItem, ReadonlyCapabilityError> {
    let artifact_id = artifact_id.into();
    project_item(
        scope,
        format!("artifact:{artifact_id}"),
        ReadonlyItemKind::Artifact,
        artifact_id,
        serde_json::json!({"content_ref": content_ref.into(), "media_type": media_type.into()}),
    )
}

pub fn project_office_read(
    scope: &RuntimeCapabilityScope,
    artifact_id: impl Into<String>,
    format: impl Into<String>,
    extracted: Value,
) -> Result<CapabilityItem, ReadonlyCapabilityError> {
    let artifact_id = artifact_id.into();
    project_item(
        scope,
        format!("office-read:{artifact_id}"),
        ReadonlyItemKind::OfficeRead,
        artifact_id,
        serde_json::json!({"format": format.into(), "extracted": extracted}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scope() -> RuntimeCapabilityScope {
        RuntimeCapabilityScope {
            tenant_id: "tenant-a".to_string(),
            user_id: "user-a".to_string(),
            session_id: "session-a".to_string(),
            capability_revision: 7,
            snapshot_id: "snapshot-a".to_string(),
        }
    }

    fn descriptor(name: &str, source: &str) -> CapabilityDescriptor {
        CapabilityDescriptor {
            name: name.to_string(),
            description: "read-only capability".to_string(),
            schema: serde_json::json!({"type": "object"}),
            tenant_id: "tenant-a".to_string(),
            capability_revision: 7,
            source: source.to_string(),
            kind: "knowledge".to_string(),
            read_only: true,
            tags: vec!["retrieval".to_string(), "read".to_string()],
            protocol: "internal".to_string(),
            category: "retrieval".to_string(),
            id: format!("{source}:{name}"),
            version: Some("v1".to_string()),
            schema_hash: Some(format!("sha256:{}", "a".repeat(64))),
            metadata: Map::new(),
        }
    }

    #[test]
    fn discovery_is_metadata_bound_and_stably_sorted() {
        let filter = MetadataFilter {
            tags: BTreeSet::from(["read".to_string()]),
            ..Default::default()
        };
        let values = discover_readonly(
            &scope(),
            [
                descriptor("z.read", "knowledge"),
                descriptor("a.read", "mcp:docs"),
            ],
            &filter,
        )
        .expect("read-only descriptors should validate");
        assert_eq!(values[0].source, "knowledge");
        assert_eq!(values[1].source, "mcp:docs");
        assert_eq!(values[0].tags, vec!["read", "retrieval"]);
    }

    #[test]
    fn tenant_and_revision_mismatch_fail_closed() {
        let mut foreign = descriptor("foreign.read", "foreign");
        foreign.tenant_id = "tenant-b".to_string();
        let error = discover_readonly(&scope(), [foreign], &MetadataFilter::default())
            .expect_err("foreign tenant must be rejected");
        assert_eq!(error.to_string(), "runtime_capability_revision_mismatch");
    }

    #[test]
    fn unified_projection_keeps_external_data_non_authoritative() {
        let values = [
            project_knowledge(&scope(), "source-a", "retrieved"),
            project_attachment(&scope(), "attachment-a", "blob:a"),
            project_citation(&scope(), "citation-a", "source-a", "p.1"),
            project_artifact(&scope(), "artifact-a", "blob:b", "text/plain"),
            project_office_read(&scope(), "artifact-a", "docx", serde_json::json!({"p": 1})),
        ];
        assert!(values.iter().all(|value| {
            value.as_ref().is_ok_and(|item| {
                item.tenant_id == "tenant-a"
                    && item.capability_revision == 7
                    && item.untrusted
                    && item.authority == "non_authoritative"
            })
        }));
    }

    #[test]
    fn write_descriptor_is_not_visible() {
        let mut value = descriptor("write.danger", "danger");
        value.read_only = false;
        let error = discover_readonly(&scope(), [value], &MetadataFilter::default())
            .expect_err("write capability must fail closed");
        assert_eq!(error.to_string(), "runtime_readonly_capability_required");
    }

    #[test]
    fn missing_effect_metadata_is_not_visible() {
        let mut value = serde_json::to_value(descriptor("unknown", "external")).unwrap();
        value.as_object_mut().unwrap().remove("read_only");
        let value: CapabilityDescriptor = serde_json::from_value(value).unwrap();
        assert!(!value.read_only);
        let error = discover_readonly(&scope(), [value], &MetadataFilter::default())
            .expect_err("missing effect metadata must fail closed");
        assert_eq!(error.to_string(), "runtime_readonly_capability_required");
    }

    #[test]
    fn render_turn_input_rejects_foreign_payload_and_marks_context() {
        let payload = serde_json::json!({
            "schema_version": READONLY_CAPABILITY_SCHEMA_VERSION,
            "tenant_id": "tenant-a",
            "capability_revision": 7,
            "items": [{
                "item_id": "knowledge:source-a",
                "kind": "knowledge",
                "tenant_id": "tenant-a",
                "capability_revision": 7,
                "source": "knowledge",
                "payload": {"dataset_id": "source-a"}
            }],
            "tools": [],
            "mcp": []
        });
        let rendered = render_turn_input(&scope(), &payload)
            .expect("readonly input should validate")
            .expect("non-empty readonly input should render");
        assert!(rendered.starts_with("[AI_PLATFORM_READONLY_CONTEXT_V1]"));

        let mut foreign = payload;
        foreign["tenant_id"] = Value::String("tenant-b".to_string());
        let error = render_turn_input(&scope(), &foreign).expect_err("foreign input must fail");
        assert_eq!(error.to_string(), "runtime_capability_revision_mismatch");
    }

    #[test]
    fn dynamic_tools_require_an_explicit_readonly_descriptor() {
        let mut payload = serde_json::json!({
            "tools": [serde_json::to_value(descriptor("search", "knowledge")).unwrap()]
        });
        assert!(allows_dynamic_tool(&payload, None, "search"));
        assert!(allows_dynamic_tool(&payload, Some("knowledge"), "search"));
        assert!(!allows_dynamic_tool(&payload, None, "write"));
        payload["tools"][0]["read_only"] = Value::Bool(false);
        assert!(!allows_dynamic_tool(&payload, None, "search"));
        payload["tools"][0]["read_only"] = Value::Bool(true);
        payload["mcp"] =
            serde_json::json!([serde_json::to_value(descriptor("search", "mcp:docs")).unwrap()]);
        assert!(!allows_dynamic_tool(&payload, None, "search"));
    }

    #[test]
    fn dynamic_tool_resolution_requires_snapshot_allowlist_identity() {
        let descriptor = descriptor("search", "knowledge");
        let mut payload = serde_json::json!({
            "tools": [serde_json::to_value(&descriptor).unwrap()],
            "capability_allowlist": [{
                "type": "knowledge",
                "name": "search",
                "id": "knowledge:search",
                "version": "v1",
                "schema_hash": format!("sha256:{}", "a".repeat(64))
            }]
        });
        let (allowlist, expected) =
            resolve_dynamic_tool(&payload, None, "search").expect("allowlisted tool");
        assert_eq!(allowlist.len(), 1);
        assert_eq!(expected.id, "knowledge:search");
        assert_eq!(expected.schema_hash, descriptor.schema_hash);

        payload["capability_allowlist"][0]["schema_hash"] =
            Value::String(format!("sha256:{}", "b".repeat(64)));
        assert_eq!(
            resolve_dynamic_tool(&payload, None, "search")
                .expect_err("schema drift must fail closed")
                .to_string(),
            "runtime_capability_allowlist_scope_mismatch"
        );
        payload
            .as_object_mut()
            .unwrap()
            .remove("capability_allowlist");
        assert_eq!(
            resolve_dynamic_tool(&payload, None, "search")
                .expect_err("missing allowlist must fail closed")
                .to_string(),
            "runtime_capability_allowlist_missing"
        );
    }
}
