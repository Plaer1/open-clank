//! Inactive v3 adapters. The v2 wire, repository and schema remain unchanged.
use crate::ontology::{ScopeRef, TypedValue};
use crate::repository::{KnowledgeRevision, RepositoryError, V2Repository};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashSet;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct KnowledgeRef {
    pub block_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revision: Option<i64>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum V3Value {
    Existing(TypedValue),
    KnowledgeRef(KnowledgeRef),
}

/// Keep all existing variants in their original serde/validator authority.
pub fn validate_value(value: Value) -> Result<V3Value, RepositoryError> {
    if value.get("type").and_then(Value::as_str) != Some("knowledge_ref") {
        let existing: TypedValue = serde_json::from_value(value)
            .map_err(|_| RepositoryError::Validation("invalid typed value".into()))?;
        existing.validate().map_err(RepositoryError::Validation)?;
        return Ok(V3Value::Existing(existing));
    }
    let object = value.as_object().ok_or_else(|| RepositoryError::Validation("invalid knowledge reference".into()))?;
    if object.len() != 2 || !object.contains_key("value") {
        return Err(RepositoryError::Validation("invalid knowledge reference".into()));
    }
    let raw = object.get("value").unwrap();
    if raw.get("revision").is_some_and(Value::is_null) {
        return Err(RepositoryError::Validation("invalid knowledge reference".into()));
    }
    let reference: KnowledgeRef = serde_json::from_value(raw.clone())
        .map_err(|_| RepositoryError::Validation("invalid knowledge reference".into()))?;
    if reference.block_id.trim().is_empty() || reference.block_id != reference.block_id.trim() ||
        reference.revision.is_some_and(|revision| revision < 1) {
        return Err(RepositoryError::Validation("invalid knowledge reference".into()));
    }
    Ok(V3Value::KnowledgeRef(reference))
}

/// Existing repository owns visibility and exact-revision lookup. Scope owner
/// is only a selector until it matches the independently authenticated owner.
pub fn resolve_knowledge_reference(
    repository: &V2Repository<'_>, authenticated_owner: &str, scope: &ScopeRef, reference: &KnowledgeRef,
) -> Result<KnowledgeRevision, RepositoryError> {
    if authenticated_owner.trim().is_empty() || authenticated_owner.trim() != scope.owner_id ||
        reference.block_id.trim().is_empty() || reference.block_id != reference.block_id.trim() ||
        reference.revision.is_some_and(|revision| revision < 1) {
        return Err(RepositoryError::NotFound);
    }
    repository.get_knowledge(scope, &reference.block_id, reference.revision)
}

/// Validate only declarative metadata. Field-authority claims belong to S03.
pub fn validate_revision_metadata(value: &Value) -> Result<(), RepositoryError> {
    let object = value.as_object().ok_or_else(|| RepositoryError::Validation("invalid revision metadata".into()))?;
    for (key, item) in object {
        match key.as_str() {
            "name" | "processing_role" => {
                let text = item.as_str().ok_or_else(|| RepositoryError::Validation("invalid revision metadata".into()))?;
                if text.trim().is_empty() || text != text.trim() || text.len()>512 { return Err(RepositoryError::Validation("invalid revision metadata".into())); }
            }
            "aliases" | "types" => {
                let values = item.as_array().ok_or_else(|| RepositoryError::Validation("invalid revision metadata".into()))?;
                if values.len()>64 { return Err(RepositoryError::Validation("invalid revision metadata".into())); }
                let mut seen = HashSet::new();
                for value in values {
                    let text = value.as_str().ok_or_else(|| RepositoryError::Validation("invalid revision metadata".into()))?;
                    if text.trim().is_empty() || text != text.trim() || text.len()>512 || !seen.insert(text) {
                        return Err(RepositoryError::Validation("invalid revision metadata".into()));
                    }
                }
            }
            "policy_overrides" => {
                let values=item.as_object().ok_or_else(||RepositoryError::Validation("invalid policy overrides".into()))?;
                if values.len()>3 {return Err(RepositoryError::Validation("invalid policy overrides".into()))}
                for (axis,value) in values {
                    let valid=match axis.as_str() {
                        "importance"=>value.is_null() || value.as_f64().is_some_and(|v|v.is_finite() && (0.0..=1.0).contains(&v)),
                        "connections"|"consolidation"=>value.is_null() || value.is_boolean(),
                        _=>false,
                    };
                    if !valid {return Err(RepositoryError::Validation("invalid policy overrides".into()))}
                }
            }
            "importance_score" => {
                let m=item.as_object().ok_or_else(||RepositoryError::Validation("invalid importance metadata".into()))?;
                let valid_id=|key:&str| m.get(key).and_then(Value::as_str).is_some_and(|v|!v.is_empty() && v.trim()==v && v.len()<=512 && !v.chars().any(char::is_control));
                let valid_hash=|key:&str| m.get(key).and_then(Value::as_str).is_some_and(|v|v.len()==64 && v.bytes().all(|c|c.is_ascii_digit() || (b'a'..=b'f').contains(&c)));
                let origin=m.get("origin").and_then(Value::as_str);
                let approval=matches!(origin,Some("inferred"|"manual")) && valid_id("approved_by");
                if m.len()!=11 || m.get("version").and_then(Value::as_u64)!=Some(1) || m.get("value").and_then(Value::as_f64).is_none_or(|v|!v.is_finite() || !(0.0..=1.0).contains(&v)) || !approval || !["job_id","candidate_id","result_receipt_id"].iter().all(|k|valid_id(k)) || !["result_receipt_sha256","outcome_sha256","policy_snapshot_sha256","source_commitment_sha256"].iter().all(|k|valid_hash(k)) {return Err(RepositoryError::Validation("invalid importance metadata".into()));}
            }
            "connection" => {
                let c=item.as_object().ok_or_else(||RepositoryError::Validation("invalid connection metadata".into()))?;
                let subject=c.get("subject").and_then(Value::as_object).ok_or_else(||RepositoryError::Validation("invalid connection metadata".into()))?;
                let block=subject.get("block_id").and_then(Value::as_str).unwrap_or("");
                let hash=c.get("predicate_sha256").and_then(Value::as_str).unwrap_or("");
                if c.len()!=4 || c.get("version").and_then(Value::as_u64)!=Some(1) || subject.len()!=2 || block.is_empty() || block!=block.trim() || block.len()>512 || subject.get("revision").and_then(Value::as_i64).is_none_or(|v|v<1) || c.get("predicate_version").and_then(Value::as_i64).is_none_or(|v|v<1) || hash.len()!=64 || !hash.bytes().all(|v|v.is_ascii_digit() || (b'a'..=b'f').contains(&v)) {return Err(RepositoryError::Validation("invalid connection metadata".into()));}
            }
            "domain_refs" => {
                let values = item.as_array().ok_or_else(|| RepositoryError::Validation("invalid revision metadata".into()))?;
                if values.len()>64 { return Err(RepositoryError::Validation("invalid revision metadata".into())); }
                let mut seen = HashSet::new();
                for value in values {
                    let reference = match validate_value(serde_json::json!({"type":"knowledge_ref", "value":value}))? {
                        V3Value::KnowledgeRef(reference) if reference.revision.is_some() && reference.block_id.len()<=512 => reference,
                        _ => return Err(RepositoryError::Validation("domain reference requires an exact revision".into())),
                    };
                    if !seen.insert((reference.block_id, reference.revision)) {
                        return Err(RepositoryError::Validation("duplicate domain reference".into()));
                    }
                }
            }
            _ => return Err(RepositoryError::Validation("unsupported revision metadata".into())),
        }
    }
    Ok(())
}

/// Resolve each exact domain reference using the source block's authenticated
/// scope; the caller must obtain that scope from the V2 block, not model input.
pub fn resolve_domain_references(
    repository: &V2Repository<'_>, authenticated_owner: &str, source_scope: &ScopeRef, metadata: &Value,
) -> Result<(), RepositoryError> {
    validate_revision_metadata(metadata)?;
    if let Some(refs) = metadata.get("domain_refs").and_then(Value::as_array) {
        for value in refs {
            let reference = match validate_value(serde_json::json!({"type":"knowledge_ref", "value":value}))? {
                V3Value::KnowledgeRef(reference) => reference,
                _ => return Err(RepositoryError::Validation("invalid domain reference".into())),
            };
            resolve_knowledge_reference(repository, authenticated_owner, source_scope, &reference)?;
        }
    }
    Ok(())
}
