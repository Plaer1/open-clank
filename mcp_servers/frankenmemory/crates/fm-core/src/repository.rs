//! Tenant-scoped, append-only repository for the Frankenmemory v2 ontology.
//!
//! Canonical writes and their projection outbox event share one SQLite
//! transaction.  Updates require a block-local expected revision; a stale
//! writer returns the accepted head and never persists its proposal.

use std::collections::{BTreeMap, BTreeSet};

use rusqlite::{params, Connection, OptionalExtension, Transaction, TransactionBehavior};
use schemars::JsonSchema;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use thiserror::Error;

use crate::ontology::{
    AttemptState, CandidateState, JobState, KnowledgeStatus, PredicateCardinality, ScopeRef,
    TypedValue, ValueType, CONTRACT_VERSION,
};

#[derive(Debug, Error)]
pub enum RepositoryError {
    #[error("{0}")]
    Validation(String),
    #[error("record not found")]
    NotFound,
    #[error("revision conflict: expected {expected}, current {current}")]
    RevisionConflict {
        expected: i64,
        current: i64,
        proposal: Value,
    },
    #[error("{0}")]
    Database(#[from] rusqlite::Error),
    #[error("{0}")]
    Serialization(#[from] serde_json::Error),
}

impl RepositoryError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::Validation(_) => "validation",
            Self::NotFound => "not_found",
            Self::RevisionConflict { .. } => "revision_conflict",
            Self::Database(_) | Self::Serialization(_) => "storage_failure",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RevisionActor {
    pub actor_type: String,
    pub actor_id: String,
    pub reason: String,
    pub source_event_id: Option<String>,
}

impl RevisionActor {
    fn validate(&self) -> Result<(), RepositoryError> {
        for (name, value) in [
            ("actor_type", &self.actor_type),
            ("actor_id", &self.actor_id),
            ("reason", &self.reason),
        ] {
            if value.trim().is_empty() {
                return Err(RepositoryError::Validation(format!("{name} is required")));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct EntityPayload {
    pub entity_type: String,
    pub canonical_label: String,
    #[serde(default)]
    pub aliases: Vec<String>,
    #[serde(default)]
    pub roles: Vec<String>,
    #[serde(default)]
    pub tags: Vec<String>,
    pub status: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct KnowledgeIdentity {
    pub block_id: String,
    pub subject_entity_id: String,
    pub predicate: String,
    pub claim_slot: String,
    pub cardinality: PredicateCardinality,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct KnowledgePayload {
    pub value: TypedValue,
    pub expected_value_type: Option<ValueType>,
    pub kind: String,
    #[serde(default)]
    pub tags: Vec<String>,
    pub status: KnowledgeStatus,
    pub confidence: f64,
    pub trust: f64,
    pub activation: String,
    #[serde(default)]
    pub activation_rationale: Value,
    pub valid_from: Option<String>,
    pub valid_to: Option<String>,
    #[serde(default)]
    pub evidence_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct EntityRevision {
    pub entity_id: String,
    pub scope: ScopeRef,
    pub payload: EntityPayload,
    pub revision: i64,
    pub previous_revision: Option<i64>,
    pub actor_type: String,
    pub actor_id: String,
    pub reason: String,
    pub change_set_id: String,
    pub content_hash: String,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct KnowledgeRevision {
    pub identity: KnowledgeIdentity,
    pub scope: ScopeRef,
    pub payload: KnowledgePayload,
    pub revision: i64,
    pub previous_revision: Option<i64>,
    pub actor_type: String,
    pub actor_id: String,
    pub reason: String,
    pub source_event_id: Option<String>,
    pub change_set_id: String,
    pub content_hash: String,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct KnowledgeDiff {
    pub block_id: String,
    pub from_revision: i64,
    pub to_revision: i64,
    pub changes: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BlameEntry {
    pub revision: i64,
    pub actor_type: String,
    pub actor_id: String,
    pub reason: String,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CandidatePayload {
    pub proposal: Value,
    pub state: CandidateState,
    pub confidence: f64,
    pub importance: f64,
    pub reason: String,
    pub source_id: Option<String>,
    pub source_revision: Option<i64>,
    pub accepted_block_id: Option<String>,
    #[serde(default)]
    pub evidence_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CandidateRevision {
    pub candidate_id: String,
    pub idempotency_key: String,
    pub scope: ScopeRef,
    pub payload: CandidatePayload,
    pub revision: i64,
    pub previous_revision: Option<i64>,
    pub actor_type: String,
    pub actor_id: String,
    pub source_event_id: Option<String>,
    pub change_set_id: String,
    pub content_hash: String,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SourcePayload {
    pub source_uri: String,
    pub content_hash: String,
    pub source_type: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SourceRecord {
    pub source_id: String,
    pub source_revision: i64,
    pub scope: ScopeRef,
    pub payload: SourcePayload,
    pub forget_state: String,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EvidencePayload {
    pub source_id: String,
    pub source_revision: i64,
    pub locator_type: String,
    pub locator: Value,
    pub quote: Option<String>,
    pub extraction_contract: String,
    pub parser_version: String,
    pub raw_value: Value,
    pub normalized_value: Value,
    #[serde(default)]
    pub validation: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct EvidenceRecord {
    pub evidence_id: String,
    pub scope: ScopeRef,
    pub payload: EvidencePayload,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct JobRecord {
    pub job_id: String,
    pub kind: String,
    pub scope: ScopeRef,
    pub idempotency_key: String,
    pub state: JobState,
    pub current_attempt_id: Option<String>,
    pub attempt_count: i64,
    pub input_hash: String,
    pub result: Option<Value>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct JobAttempt {
    pub attempt_id: String,
    pub attempt_number: i64,
    pub state: AttemptState,
    pub lease_epoch: i64,
    pub lease_owner: Option<String>,
    pub lease_expires_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct JobEvent {
    pub sequence: i64,
    pub state: JobState,
    pub attempt_id: Option<String>,
    pub lease_epoch: Option<i64>,
    pub result: Option<Value>,
    pub error: Option<Value>,
    pub change_set_id: String,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ConflictRecord {
    pub conflict_id: String,
    pub block_id: String,
    pub scope: ScopeRef,
    pub current_revision: i64,
    pub competing_payload: Value,
    pub state: String,
    pub rationale: String,
    pub created_change_set_id: String,
    pub resolved_change_set_id: Option<String>,
    pub created_at: String,
    pub resolved_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ConflictEvent {
    pub sequence: i64,
    pub state: String,
    pub rationale: String,
    pub change_set_id: String,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PrivacyErasureResult {
    pub erasure_id: String,
    pub source_id: String,
    pub erased_counts: BTreeMap<String, i64>,
}

pub struct V2Repository<'a> {
    conn: &'a mut Connection,
}

impl<'a> V2Repository<'a> {
    pub fn new(conn: &'a mut Connection) -> Result<Self, RepositoryError> {
        conn.execute_batch("PRAGMA foreign_keys=ON;")?;
        let enabled: i64 = conn.query_row("PRAGMA foreign_keys", [], |row| row.get(0))?;
        if enabled != 1 {
            return Err(RepositoryError::Validation(
                "SQLite foreign keys are unavailable".into(),
            ));
        }
        let installed: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='fm_v2_knowledge_revisions')",
            [],
            |row| row.get(0),
        )?;
        if !installed {
            return Err(RepositoryError::Validation(
                "Frankenmemory v2 schema is unavailable".into(),
            ));
        }
        Ok(Self { conn })
    }

    pub fn create_entity(
        &mut self,
        scope: &ScopeRef,
        entity_id: &str,
        payload: &EntityPayload,
        actor: &RevisionActor,
    ) -> Result<EntityRevision, RepositoryError> {
        validate_scope(scope)?;
        validate_entity(entity_id, payload, actor)?;
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        if transaction
            .query_row(
                "SELECT 1 FROM fm_v2_entities WHERE owner_id=? AND entity_id=?",
                params![owner, entity_id],
                |_| Ok(()),
            )
            .optional()?
            .is_some()
        {
            return Err(RepositoryError::Validation(
                "entity_id already exists".into(),
            ));
        }
        let payload_json = serde_json::to_value(payload)?;
        let change_set = insert_change_set(&transaction, &owner, actor, &payload_json, &now)?;
        transaction.execute(
            "INSERT INTO fm_v2_entities(owner_id,entity_id,workspace_key,project_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,0,?)",
            params![owner, entity_id, workspace, project, change_set, now],
        )?;
        insert_entity_revision(
            &transaction,
            &owner,
            entity_id,
            1,
            None,
            payload,
            actor,
            &change_set,
            &now,
        )?;
        transaction.execute(
            "UPDATE fm_v2_entities SET current_revision=1 WHERE owner_id=? AND entity_id=? AND current_revision=0",
            params![owner, entity_id],
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "entity_revision",
            &json!({"entity_id": entity_id, "revision": 1}),
            1,
            &now,
        )?;
        transaction.commit()?;
        self.get_entity(scope, entity_id, None)
    }

    pub fn update_entity(
        &mut self,
        scope: &ScopeRef,
        entity_id: &str,
        expected_revision: i64,
        payload: &EntityPayload,
        actor: &RevisionActor,
    ) -> Result<EntityRevision, RepositoryError> {
        validate_scope(scope)?;
        validate_entity(entity_id, payload, actor)?;
        if expected_revision < 1 {
            return Err(RepositoryError::Validation(
                "expected_revision must be positive".into(),
            ));
        }
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current: Option<(String, String, i64)> = transaction
            .query_row(
                "SELECT workspace_key,project_key,current_revision FROM fm_v2_entities WHERE owner_id=? AND entity_id=?",
                params![owner, entity_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        let Some((stored_workspace, stored_project, current_revision)) = current else {
            return Err(RepositoryError::NotFound);
        };
        if (stored_workspace, stored_project) != (workspace, project) {
            return Err(RepositoryError::NotFound);
        }
        if current_revision != expected_revision {
            return Err(RepositoryError::RevisionConflict {
                expected: expected_revision,
                current: current_revision,
                proposal: serde_json::to_value(payload)?,
            });
        }
        let revision = current_revision + 1;
        let payload_json = serde_json::to_value(payload)?;
        let change_set = insert_change_set(&transaction, &owner, actor, &payload_json, &now)?;
        insert_entity_revision(
            &transaction,
            &owner,
            entity_id,
            revision,
            Some(current_revision),
            payload,
            actor,
            &change_set,
            &now,
        )?;
        if transaction.execute(
            "UPDATE fm_v2_entities SET current_revision=? WHERE owner_id=? AND entity_id=? AND current_revision=?",
            params![revision, owner, entity_id, expected_revision],
        )? != 1
        {
            return Err(RepositoryError::RevisionConflict {
                expected: expected_revision,
                current: current_revision,
                proposal: payload_json,
            });
        }
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "entity_revision",
            &json!({"entity_id": entity_id, "revision": revision}),
            revision,
            &now,
        )?;
        transaction.commit()?;
        self.get_entity(scope, entity_id, None)
    }

    pub fn get_entity(
        &self,
        scope: &ScopeRef,
        entity_id: &str,
        revision: Option<i64>,
    ) -> Result<EntityRevision, RepositoryError> {
        validate_scope(scope)?;
        let (owner, requested_workspace, requested_project) = scope.storage_keys();
        let identity: Option<(String, String, i64)> = self
            .conn
            .query_row(
                "SELECT workspace_key,project_key,current_revision FROM fm_v2_entities WHERE owner_id=? AND entity_id=?",
                params![owner, entity_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        let Some((workspace, project, current_revision)) = identity else {
            return Err(RepositoryError::NotFound);
        };
        if !scope_visible(
            &workspace,
            &project,
            &requested_workspace,
            &requested_project,
        ) {
            return Err(RepositoryError::NotFound);
        }
        read_entity(
            self.conn,
            &owner,
            entity_id,
            revision.unwrap_or(current_revision),
            &workspace,
            &project,
        )
    }

    pub fn entity_history(
        &self,
        scope: &ScopeRef,
        entity_id: &str,
    ) -> Result<Vec<EntityRevision>, RepositoryError> {
        let current = self.get_entity(scope, entity_id, None)?;
        let (owner, workspace, project) = current.scope.storage_keys();
        (1..=current.revision)
            .map(|revision| {
                read_entity(self.conn, &owner, entity_id, revision, &workspace, &project)
            })
            .collect()
    }

    pub fn create_knowledge(
        &mut self,
        scope: &ScopeRef,
        identity: &KnowledgeIdentity,
        payload: &KnowledgePayload,
        actor: &RevisionActor,
    ) -> Result<KnowledgeRevision, RepositoryError> {
        validate_scope(scope)?;
        validate_knowledge(identity, payload, actor)?;
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        validate_knowledge_relations(
            &transaction,
            &owner,
            &workspace,
            &project,
            identity,
            payload,
        )?;
        if transaction
            .query_row(
                "SELECT 1 FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",
                params![owner, identity.block_id],
                |_| Ok(()),
            )
            .optional()?
            .is_some()
        {
            return Err(RepositoryError::Validation(
                "block_id already exists".into(),
            ));
        }
        let payload_json = serde_json::to_value(payload)?;
        let change_set = insert_change_set(&transaction, &owner, actor, &payload_json, &now)?;
        transaction.execute(
            "INSERT INTO fm_v2_knowledge_blocks(owner_id,block_id,workspace_key,project_key,subject_entity_id,predicate,claim_slot,cardinality,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
            params![owner, identity.block_id, workspace, project, identity.subject_entity_id, identity.predicate, identity.claim_slot, enum_text(&identity.cardinality)?, change_set, now],
        )?;
        insert_knowledge_revision(
            &transaction,
            &owner,
            identity,
            1,
            None,
            payload,
            actor,
            &change_set,
            &now,
        )?;
        transaction.execute(
            "UPDATE fm_v2_knowledge_blocks SET current_revision=1 WHERE owner_id=? AND block_id=? AND current_revision=0",
            params![owner, identity.block_id],
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "knowledge_revision",
            &json!({"block_id": identity.block_id, "revision": 1}),
            1,
            &now,
        )?;
        transaction.commit()?;
        self.get_knowledge(scope, &identity.block_id, None)
    }

    pub fn update_knowledge(
        &mut self,
        scope: &ScopeRef,
        block_id: &str,
        expected_revision: i64,
        payload: &KnowledgePayload,
        actor: &RevisionActor,
    ) -> Result<KnowledgeRevision, RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        if expected_revision < 1 {
            return Err(RepositoryError::Validation(
                "expected_revision must be positive".into(),
            ));
        }
        validate_knowledge_payload(payload)?;
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let identity = read_knowledge_identity(&transaction, &owner, block_id)?;
        if (identity.1.clone(), identity.2.clone()) != (workspace.clone(), project.clone()) {
            return Err(RepositoryError::NotFound);
        }
        if identity.3 != expected_revision {
            return Err(RepositoryError::RevisionConflict {
                expected: expected_revision,
                current: identity.3,
                proposal: serde_json::to_value(payload)?,
            });
        }
        validate_knowledge_relations(
            &transaction,
            &owner,
            &workspace,
            &project,
            &identity.0,
            payload,
        )?;
        let revision = expected_revision + 1;
        let payload_json = serde_json::to_value(payload)?;
        let change_set = insert_change_set(&transaction, &owner, actor, &payload_json, &now)?;
        insert_knowledge_revision(
            &transaction,
            &owner,
            &identity.0,
            revision,
            Some(expected_revision),
            payload,
            actor,
            &change_set,
            &now,
        )?;
        if transaction.execute(
            "UPDATE fm_v2_knowledge_blocks SET current_revision=? WHERE owner_id=? AND block_id=? AND current_revision=?",
            params![revision, owner, block_id, expected_revision],
        )? != 1
        {
            return Err(RepositoryError::RevisionConflict {
                expected: expected_revision,
                current: identity.3,
                proposal: payload_json,
            });
        }
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "knowledge_revision",
            &json!({"block_id": block_id, "revision": revision}),
            revision,
            &now,
        )?;
        transaction.commit()?;
        self.get_knowledge(scope, block_id, None)
    }

    pub fn get_knowledge(
        &self,
        scope: &ScopeRef,
        block_id: &str,
        revision: Option<i64>,
    ) -> Result<KnowledgeRevision, RepositoryError> {
        validate_scope(scope)?;
        let (owner, requested_workspace, requested_project) = scope.storage_keys();
        let (identity, workspace, project, current_revision) =
            read_knowledge_identity(self.conn, &owner, block_id)?;
        if !scope_visible(
            &workspace,
            &project,
            &requested_workspace,
            &requested_project,
        ) {
            return Err(RepositoryError::NotFound);
        }
        read_knowledge(
            self.conn,
            &owner,
            &identity,
            revision.unwrap_or(current_revision),
            &workspace,
            &project,
        )
    }

    pub fn knowledge_history(
        &self,
        scope: &ScopeRef,
        block_id: &str,
    ) -> Result<Vec<KnowledgeRevision>, RepositoryError> {
        let current = self.get_knowledge(scope, block_id, None)?;
        let (owner, workspace, project) = current.scope.storage_keys();
        (1..=current.revision)
            .map(|revision| {
                read_knowledge(
                    self.conn,
                    &owner,
                    &current.identity,
                    revision,
                    &workspace,
                    &project,
                )
            })
            .collect()
    }

    pub fn knowledge_diff(
        &self,
        scope: &ScopeRef,
        block_id: &str,
        from_revision: i64,
        to_revision: i64,
    ) -> Result<KnowledgeDiff, RepositoryError> {
        let before = serde_json::to_value(
            self.get_knowledge(scope, block_id, Some(from_revision))?
                .payload,
        )?;
        let after = serde_json::to_value(
            self.get_knowledge(scope, block_id, Some(to_revision))?
                .payload,
        )?;
        let before = before.as_object().cloned().unwrap_or_default();
        let after = after.as_object().cloned().unwrap_or_default();
        let keys: BTreeSet<_> = before.keys().chain(after.keys()).cloned().collect();
        let changes = keys
            .into_iter()
            .filter_map(|key| {
                let old = before.get(&key).cloned().unwrap_or(Value::Null);
                let new = after.get(&key).cloned().unwrap_or(Value::Null);
                (old != new).then(|| (key, json!({"before": old, "after": new})))
            })
            .collect();
        Ok(KnowledgeDiff {
            block_id: block_id.into(),
            from_revision,
            to_revision,
            changes,
        })
    }

    pub fn knowledge_blame(
        &self,
        scope: &ScopeRef,
        block_id: &str,
    ) -> Result<BTreeMap<String, BlameEntry>, RepositoryError> {
        let mut previous = Map::new();
        let mut blamed = BTreeMap::new();
        for revision in self.knowledge_history(scope, block_id)? {
            let payload = serde_json::to_value(&revision.payload)?;
            for (field, value) in payload.as_object().cloned().unwrap_or_default() {
                if previous.get(&field) != Some(&value) {
                    blamed.insert(
                        field.clone(),
                        BlameEntry {
                            revision: revision.revision,
                            actor_type: revision.actor_type.clone(),
                            actor_id: revision.actor_id.clone(),
                            reason: revision.reason.clone(),
                            created_at: revision.created_at.clone(),
                        },
                    );
                }
                previous.insert(field, value);
            }
        }
        Ok(blamed)
    }

    pub fn revert_knowledge(
        &mut self,
        scope: &ScopeRef,
        block_id: &str,
        expected_revision: i64,
        target_revision: i64,
        actor: &RevisionActor,
    ) -> Result<KnowledgeRevision, RepositoryError> {
        let target = self.get_knowledge(scope, block_id, Some(target_revision))?;
        let mut actor = actor.clone();
        actor.reason = format!("{} to revision {target_revision}", actor.reason);
        self.update_knowledge(scope, block_id, expected_revision, &target.payload, &actor)
    }

    pub fn create_source(
        &mut self,
        scope: &ScopeRef,
        source_id: &str,
        source_revision: i64,
        payload: &SourcePayload,
        actor: &RevisionActor,
    ) -> Result<SourceRecord, RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        validate_source(source_id, source_revision, payload)?;
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let payload_value = serde_json::to_value(payload)?;
        let change_set = insert_change_set(&transaction, &owner, actor, &payload_value, &now)?;
        transaction.execute(
            "INSERT INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,source_uri,source_revision,content_hash,source_type,forget_state,created_at) VALUES (?,?,?,?,?,?,?,?, 'active', ?)",
            params![owner, source_id, workspace, project, payload.source_uri, source_revision, payload.content_hash, payload.source_type, now],
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "source_revision",
            &json!({"source_id": source_id, "source_revision": source_revision}),
            source_revision,
            &now,
        )?;
        transaction.commit()?;
        self.get_source(scope, source_id, source_revision)
    }

    pub fn get_source(
        &self,
        scope: &ScopeRef,
        source_id: &str,
        source_revision: i64,
    ) -> Result<SourceRecord, RepositoryError> {
        validate_scope(scope)?;
        let (owner, requested_workspace, requested_project) = scope.storage_keys();
        let record = self
            .conn
            .query_row(
                "SELECT workspace_key,project_key,source_uri,content_hash,source_type,forget_state,created_at FROM fm_v2_sources WHERE owner_id=? AND source_id=? AND source_revision=?",
                params![owner, source_id, source_revision],
                |row| {
                    let workspace: String = row.get(0)?;
                    let project: String = row.get(1)?;
                    Ok((workspace, project, SourceRecord {
                        source_id: source_id.into(),
                        source_revision,
                        scope: ScopeRef { owner_id: owner.clone(), workspace_id: None, project_id: None, session_id: None },
                        payload: SourcePayload { source_uri: row.get(2)?, content_hash: row.get(3)?, source_type: row.get(4)? },
                        forget_state: row.get(5)?,
                        created_at: row.get(6)?,
                    }))
                },
            )
            .optional()?;
        let Some((workspace, project, mut record)) = record else {
            return Err(RepositoryError::NotFound);
        };
        if !scope_visible(
            &workspace,
            &project,
            &requested_workspace,
            &requested_project,
        ) {
            return Err(RepositoryError::NotFound);
        }
        record.scope = scope_from_keys(&owner, &workspace, &project);
        Ok(record)
    }

    pub fn source_history(
        &self,
        scope: &ScopeRef,
        source_id: &str,
    ) -> Result<Vec<SourceRecord>, RepositoryError> {
        validate_scope(scope)?;
        let (owner, requested_workspace, requested_project) = scope.storage_keys();
        let mut statement = self.conn.prepare(
            "SELECT source_revision,workspace_key,project_key,source_uri,content_hash,source_type,forget_state,created_at FROM fm_v2_sources WHERE owner_id=? AND source_id=? ORDER BY source_revision",
        )?;
        let rows = statement.query_map(params![owner, source_id], |row| {
            let workspace: String = row.get(1)?;
            let project: String = row.get(2)?;
            Ok((
                workspace.clone(),
                project.clone(),
                SourceRecord {
                    source_id: source_id.into(),
                    source_revision: row.get(0)?,
                    scope: scope_from_keys(&owner, &workspace, &project),
                    payload: SourcePayload {
                        source_uri: row.get(3)?,
                        content_hash: row.get(4)?,
                        source_type: row.get(5)?,
                    },
                    forget_state: row.get(6)?,
                    created_at: row.get(7)?,
                },
            ))
        })?;
        let mut records = Vec::new();
        for row in rows {
            let (workspace, project, record) = row?;
            if scope_visible(
                &workspace,
                &project,
                &requested_workspace,
                &requested_project,
            ) {
                records.push(record);
            }
        }
        if records.is_empty() {
            return Err(RepositoryError::NotFound);
        }
        Ok(records)
    }

    pub fn create_evidence(
        &mut self,
        scope: &ScopeRef,
        evidence_id: &str,
        payload: &EvidencePayload,
        actor: &RevisionActor,
    ) -> Result<EvidenceRecord, RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        validate_evidence(evidence_id, payload)?;
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        require_source_scope(
            &transaction,
            &owner,
            &workspace,
            &project,
            &payload.source_id,
            payload.source_revision,
        )?;
        let payload_value = serde_json::to_value(payload)?;
        let change_set = insert_change_set(&transaction, &owner, actor, &payload_value, &now)?;
        transaction.execute(
            "INSERT INTO fm_v2_evidence(owner_id,evidence_id,source_id,source_revision,locator_type,locator_json,quote,extraction_contract,parser_version,raw_value_json,normalized_value_json,validation_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            params![owner, evidence_id, payload.source_id, payload.source_revision, payload.locator_type, serde_json::to_string(&payload.locator)?, payload.quote, payload.extraction_contract, payload.parser_version, serde_json::to_string(&payload.raw_value)?, serde_json::to_string(&payload.normalized_value)?, serde_json::to_string(&payload.validation)?, now],
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "evidence_created",
            &json!({"evidence_id": evidence_id, "source_id": payload.source_id, "source_revision": payload.source_revision}),
            payload.source_revision,
            &now,
        )?;
        transaction.commit()?;
        self.get_evidence(scope, evidence_id)
    }

    pub fn get_evidence(
        &self,
        scope: &ScopeRef,
        evidence_id: &str,
    ) -> Result<EvidenceRecord, RepositoryError> {
        validate_scope(scope)?;
        let (owner, requested_workspace, requested_project) = scope.storage_keys();
        let record = self.conn.query_row(
            "SELECT s.workspace_key,s.project_key,e.source_id,e.source_revision,e.locator_type,e.locator_json,e.quote,e.extraction_contract,e.parser_version,e.raw_value_json,e.normalized_value_json,e.validation_json,e.created_at FROM fm_v2_evidence e JOIN fm_v2_sources s ON s.owner_id=e.owner_id AND s.source_id=e.source_id AND s.source_revision=e.source_revision WHERE e.owner_id=? AND e.evidence_id=?",
            params![owner, evidence_id],
            |row| {
                let workspace: String = row.get(0)?;
                let project: String = row.get(1)?;
                Ok((workspace.clone(), project.clone(), EvidenceRecord {
                    evidence_id: evidence_id.into(),
                    scope: scope_from_keys(&owner, &workspace, &project),
                    payload: EvidencePayload {
                        source_id: row.get(2)?,
                        source_revision: row.get(3)?,
                        locator_type: row.get(4)?,
                        locator: decode_sql_json(row.get(5)?)?,
                        quote: row.get(6)?,
                        extraction_contract: row.get(7)?,
                        parser_version: row.get(8)?,
                        raw_value: decode_sql_json(row.get(9)?)?,
                        normalized_value: decode_sql_json(row.get(10)?)?,
                        validation: decode_sql_json(row.get(11)?)?,
                    },
                    created_at: row.get(12)?,
                }))
            },
        ).optional()?;
        let Some((workspace, project, record)) = record else {
            return Err(RepositoryError::NotFound);
        };
        if !scope_visible(
            &workspace,
            &project,
            &requested_workspace,
            &requested_project,
        ) {
            return Err(RepositoryError::NotFound);
        }
        Ok(record)
    }

    pub fn create_candidate(
        &mut self,
        scope: &ScopeRef,
        candidate_id: &str,
        idempotency_key: &str,
        payload: &CandidatePayload,
        actor: &RevisionActor,
    ) -> Result<CandidateRevision, RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        validate_candidate(candidate_id, idempotency_key, payload, true)?;
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        validate_candidate_relations(&transaction, &owner, &workspace, &project, payload)?;
        let payload_value = serde_json::to_value(payload)?;
        let change_set = insert_change_set(&transaction, &owner, actor, &payload_value, &now)?;
        transaction.execute(
            "INSERT INTO fm_v2_candidates(owner_id,candidate_id,workspace_key,project_key,idempotency_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,0,?)",
            params![owner, candidate_id, workspace, project, idempotency_key, change_set, now],
        )?;
        insert_candidate_revision(
            &transaction,
            &owner,
            candidate_id,
            1,
            None,
            payload,
            actor,
            &change_set,
            &now,
        )?;
        transaction.execute(
            "UPDATE fm_v2_candidates SET current_revision=1 WHERE owner_id=? AND candidate_id=? AND current_revision=0",
            params![owner, candidate_id],
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "candidate_revision",
            &json!({"candidate_id": candidate_id, "revision": 1}),
            1,
            &now,
        )?;
        transaction.commit()?;
        self.get_candidate(scope, candidate_id, None)
    }

    pub fn update_candidate(
        &mut self,
        scope: &ScopeRef,
        candidate_id: &str,
        expected_revision: i64,
        payload: &CandidatePayload,
        actor: &RevisionActor,
    ) -> Result<CandidateRevision, RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        validate_candidate(candidate_id, "existing", payload, false)?;
        if expected_revision < 1 {
            return Err(RepositoryError::Validation(
                "expected_revision must be positive".into(),
            ));
        }
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current: Option<(String, String, i64)> = transaction
            .query_row(
                "SELECT workspace_key,project_key,current_revision FROM fm_v2_candidates WHERE owner_id=? AND candidate_id=?",
                params![owner, candidate_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        let Some((stored_workspace, stored_project, current_revision)) = current else {
            return Err(RepositoryError::NotFound);
        };
        if (stored_workspace, stored_project) != (workspace.clone(), project.clone()) {
            return Err(RepositoryError::NotFound);
        }
        if current_revision != expected_revision {
            return Err(RepositoryError::RevisionConflict {
                expected: expected_revision,
                current: current_revision,
                proposal: serde_json::to_value(payload)?,
            });
        }
        validate_candidate_relations(&transaction, &owner, &workspace, &project, payload)?;
        let revision = current_revision + 1;
        let payload_value = serde_json::to_value(payload)?;
        let change_set = insert_change_set(&transaction, &owner, actor, &payload_value, &now)?;
        insert_candidate_revision(
            &transaction,
            &owner,
            candidate_id,
            revision,
            Some(current_revision),
            payload,
            actor,
            &change_set,
            &now,
        )?;
        if transaction.execute(
            "UPDATE fm_v2_candidates SET current_revision=? WHERE owner_id=? AND candidate_id=? AND current_revision=?",
            params![revision, owner, candidate_id, expected_revision],
        )? != 1
        {
            return Err(RepositoryError::RevisionConflict {
                expected: expected_revision,
                current: current_revision,
                proposal: payload_value,
            });
        }
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "candidate_revision",
            &json!({"candidate_id": candidate_id, "revision": revision}),
            revision,
            &now,
        )?;
        transaction.commit()?;
        self.get_candidate(scope, candidate_id, None)
    }

    pub fn get_candidate(
        &self,
        scope: &ScopeRef,
        candidate_id: &str,
        revision: Option<i64>,
    ) -> Result<CandidateRevision, RepositoryError> {
        validate_scope(scope)?;
        let (owner, requested_workspace, requested_project) = scope.storage_keys();
        let identity: Option<(String, String, String, i64)> = self
            .conn
            .query_row(
                "SELECT workspace_key,project_key,idempotency_key,current_revision FROM fm_v2_candidates WHERE owner_id=? AND candidate_id=?",
                params![owner, candidate_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .optional()?;
        let Some((workspace, project, idempotency_key, current_revision)) = identity else {
            return Err(RepositoryError::NotFound);
        };
        if !scope_visible(
            &workspace,
            &project,
            &requested_workspace,
            &requested_project,
        ) {
            return Err(RepositoryError::NotFound);
        }
        read_candidate(
            self.conn,
            &owner,
            candidate_id,
            &idempotency_key,
            revision.unwrap_or(current_revision),
            &workspace,
            &project,
        )
    }

    pub fn candidate_history(
        &self,
        scope: &ScopeRef,
        candidate_id: &str,
    ) -> Result<Vec<CandidateRevision>, RepositoryError> {
        let current = self.get_candidate(scope, candidate_id, None)?;
        let (owner, workspace, project) = current.scope.storage_keys();
        (1..=current.revision)
            .map(|revision| {
                read_candidate(
                    self.conn,
                    &owner,
                    candidate_id,
                    &current.idempotency_key,
                    revision,
                    &workspace,
                    &project,
                )
            })
            .collect()
    }

    pub fn activate_candidate_to_knowledge(
        &mut self,
        scope: &ScopeRef,
        candidate_id: &str,
        expected_candidate_revision: i64,
        identity: &KnowledgeIdentity,
        knowledge: &KnowledgePayload,
        actor: &RevisionActor,
    ) -> Result<(CandidateRevision, KnowledgeRevision), RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        validate_knowledge(identity, knowledge, actor)?;
        if expected_candidate_revision < 1 {
            return Err(RepositoryError::Validation(
                "expected candidate revision must be positive".into(),
            ));
        }
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let candidate_identity: Option<(String, String, String, i64)> = transaction
            .query_row(
                "SELECT workspace_key,project_key,idempotency_key,current_revision FROM fm_v2_candidates WHERE owner_id=? AND candidate_id=?",
                params![owner, candidate_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .optional()?;
        let Some((stored_workspace, stored_project, idempotency_key, current_revision)) =
            candidate_identity
        else {
            return Err(RepositoryError::NotFound);
        };
        if (stored_workspace, stored_project) != (workspace.clone(), project.clone()) {
            return Err(RepositoryError::NotFound);
        }
        let current_candidate = read_candidate(
            &transaction,
            &owner,
            candidate_id,
            &idempotency_key,
            current_revision,
            &workspace,
            &project,
        )?;
        if current_revision != expected_candidate_revision {
            return Err(RepositoryError::RevisionConflict {
                expected: expected_candidate_revision,
                current: current_revision,
                proposal: serde_json::to_value(knowledge)?,
            });
        }
        if !matches!(
            current_candidate.payload.state,
            CandidateState::AwaitingReview | CandidateState::EligibleAuto
        ) {
            return Err(RepositoryError::Validation(
                "only an awaiting-review or eligible-auto candidate can activate".into(),
            ));
        }
        validate_knowledge_relations(
            &transaction,
            &owner,
            &workspace,
            &project,
            identity,
            knowledge,
        )?;
        if transaction
            .query_row(
                "SELECT 1 FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",
                params![owner, identity.block_id],
                |_| Ok(()),
            )
            .optional()?
            .is_some()
        {
            return Err(RepositoryError::Validation(
                "activation target block already exists".into(),
            ));
        }
        let candidate_revision = current_revision + 1;
        let combined_payload = json!({
            "candidate_id": candidate_id,
            "candidate_revision": candidate_revision,
            "block_id": identity.block_id,
            "knowledge_revision": 1,
            "knowledge": knowledge,
        });
        let change_set = insert_change_set(&transaction, &owner, actor, &combined_payload, &now)?;
        transaction.execute(
            "INSERT INTO fm_v2_knowledge_blocks(owner_id,block_id,workspace_key,project_key,subject_entity_id,predicate,claim_slot,cardinality,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
            params![owner, identity.block_id, workspace, project, identity.subject_entity_id, identity.predicate, identity.claim_slot, enum_text(&identity.cardinality)?, change_set, now],
        )?;
        insert_knowledge_revision(
            &transaction,
            &owner,
            identity,
            1,
            None,
            knowledge,
            actor,
            &change_set,
            &now,
        )?;
        transaction.execute(
            "UPDATE fm_v2_knowledge_blocks SET current_revision=1 WHERE owner_id=? AND block_id=? AND current_revision=0",
            params![owner, identity.block_id],
        )?;
        let mut activated_candidate = current_candidate.payload;
        activated_candidate.state = CandidateState::Activated;
        activated_candidate.accepted_block_id = Some(identity.block_id.clone());
        insert_candidate_revision(
            &transaction,
            &owner,
            candidate_id,
            candidate_revision,
            Some(current_revision),
            &activated_candidate,
            actor,
            &change_set,
            &now,
        )?;
        if transaction.execute(
            "UPDATE fm_v2_candidates SET current_revision=? WHERE owner_id=? AND candidate_id=? AND current_revision=?",
            params![candidate_revision, owner, candidate_id, expected_candidate_revision],
        )? != 1
        {
            return Err(RepositoryError::RevisionConflict {
                expected: expected_candidate_revision,
                current: current_revision,
                proposal: combined_payload,
            });
        }
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "knowledge_revision",
            &json!({"block_id": identity.block_id, "revision": 1}),
            1,
            &now,
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "candidate_revision",
            &json!({"candidate_id": candidate_id, "revision": candidate_revision, "accepted_block_id": identity.block_id}),
            candidate_revision,
            &now,
        )?;
        transaction.commit()?;
        Ok((
            self.get_candidate(scope, candidate_id, None)?,
            self.get_knowledge(scope, &identity.block_id, None)?,
        ))
    }

    pub fn create_job(
        &mut self,
        scope: &ScopeRef,
        job_id: &str,
        kind: &str,
        idempotency_key: &str,
        input_hash: &str,
        actor: &RevisionActor,
    ) -> Result<JobRecord, RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        for (name, value) in [
            ("job_id", job_id),
            ("kind", kind),
            ("idempotency_key", idempotency_key),
            ("input_hash", input_hash),
        ] {
            if value.trim().is_empty() {
                return Err(RepositoryError::Validation(format!("{name} is required")));
            }
        }
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let payload = json!({
            "job_id": job_id,
            "kind": kind,
            "idempotency_key": idempotency_key,
            "input_hash": input_hash,
            "state": "queued"
        });
        let change_set = insert_change_set(&transaction, &owner, actor, &payload, &now)?;
        transaction.execute(
            "INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,current_attempt_id,attempt_count,input_hash,result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,'queued',NULL,0,?,NULL,?,?)",
            params![owner, job_id, kind, workspace, project, idempotency_key, input_hash, now, now],
        )?;
        insert_job_event(
            &transaction,
            &owner,
            job_id,
            JobState::Queued,
            None,
            None,
            None,
            None,
            &change_set,
            &now,
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "job_state",
            &json!({"job_id": job_id, "state": "queued"}),
            1,
            &now,
        )?;
        transaction.commit()?;
        self.get_job(scope, job_id)
    }

    pub fn get_job(&self, scope: &ScopeRef, job_id: &str) -> Result<JobRecord, RepositoryError> {
        validate_scope(scope)?;
        let (owner, requested_workspace, requested_project) = scope.storage_keys();
        let record = self.conn.query_row(
            "SELECT kind,workspace_key,project_key,idempotency_key,state,current_attempt_id,attempt_count,input_hash,result_json,created_at,updated_at FROM fm_v2_jobs WHERE owner_id=? AND job_id=?",
            params![owner, job_id],
            |row| {
                let workspace: String = row.get(1)?;
                let project: String = row.get(2)?;
                let state_raw: String = row.get(4)?;
                let state = decode_enum_text(&state_raw)?;
                let result_raw: Option<String> = row.get(8)?;
                Ok((workspace.clone(), project.clone(), JobRecord {
                    job_id: job_id.into(),
                    kind: row.get(0)?,
                    scope: scope_from_keys(&owner, &workspace, &project),
                    idempotency_key: row.get(3)?,
                    state,
                    current_attempt_id: row.get(5)?,
                    attempt_count: row.get(6)?,
                    input_hash: row.get(7)?,
                    result: result_raw.map(decode_sql_json).transpose()?,
                    created_at: row.get(9)?,
                    updated_at: row.get(10)?,
                }))
            },
        ).optional()?;
        let Some((workspace, project, record)) = record else {
            return Err(RepositoryError::NotFound);
        };
        if !scope_visible(
            &workspace,
            &project,
            &requested_workspace,
            &requested_project,
        ) {
            return Err(RepositoryError::NotFound);
        }
        Ok(record)
    }

    pub fn job_history(
        &self,
        scope: &ScopeRef,
        job_id: &str,
    ) -> Result<Vec<JobEvent>, RepositoryError> {
        self.get_job(scope, job_id)?;
        let owner = scope.owner_id.trim();
        let mut statement = self.conn.prepare(
            "SELECT sequence,state,attempt_id,lease_epoch,result_json,error_json,change_set_id,created_at FROM fm_v2_job_events WHERE owner_id=? AND job_id=? ORDER BY sequence",
        )?;
        let events = statement
            .query_map(params![owner, job_id], |row| {
                let state_raw: String = row.get(1)?;
                let result_raw: Option<String> = row.get(4)?;
                let error_raw: Option<String> = row.get(5)?;
                Ok(JobEvent {
                    sequence: row.get(0)?,
                    state: decode_enum_text(&state_raw)?,
                    attempt_id: row.get(2)?,
                    lease_epoch: row.get(3)?,
                    result: result_raw.map(decode_sql_json).transpose()?,
                    error: error_raw.map(decode_sql_json).transpose()?,
                    change_set_id: row.get(6)?,
                    created_at: row.get(7)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()
            .map_err(RepositoryError::from)?;
        Ok(events)
    }

    pub fn claim_job(
        &mut self,
        scope: &ScopeRef,
        job_id: &str,
        worker_id: &str,
        lease_expires_at: &str,
        actor: &RevisionActor,
    ) -> Result<(JobRecord, JobAttempt), RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        if worker_id.trim().is_empty() || lease_expires_at.trim().is_empty() {
            return Err(RepositoryError::Validation(
                "worker and lease expiry are required".into(),
            ));
        }
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current: Option<(String, String, String, i64)> = transaction
            .query_row(
                "SELECT workspace_key,project_key,state,attempt_count FROM fm_v2_jobs WHERE owner_id=? AND job_id=?",
                params![owner, job_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .optional()?;
        let Some((stored_workspace, stored_project, state, attempt_count)) = current else {
            return Err(RepositoryError::NotFound);
        };
        if (stored_workspace, stored_project) != (workspace, project) {
            return Err(RepositoryError::NotFound);
        }
        if !matches!(state.as_str(), "queued" | "retry_wait") {
            return Err(RepositoryError::Validation(
                "only queued or retry-wait jobs can be claimed".into(),
            ));
        }
        let payload =
            json!({"job_id": job_id, "worker_id": worker_id, "lease_expires_at": lease_expires_at});
        let change_set = insert_change_set(&transaction, &owner, actor, &payload, &now)?;
        if state == "retry_wait" {
            transaction.execute(
                "UPDATE fm_v2_jobs SET state='queued',updated_at=? WHERE owner_id=? AND job_id=? AND state='retry_wait'",
                params![now, owner, job_id],
            )?;
            insert_job_event(
                &transaction,
                &owner,
                job_id,
                JobState::Queued,
                None,
                None,
                None,
                None,
                &change_set,
                &now,
            )?;
        }
        let attempt_number = attempt_count + 1;
        let attempt_id = format!("attempt_{}", uuid::Uuid::new_v4().simple());
        let lease_epoch: i64 = transaction.query_row(
            "SELECT COALESCE(MAX(lease_epoch),0)+1 FROM fm_v2_attempts WHERE owner_id=? AND job_id=?",
            params![owner, job_id],
            |row| row.get(0),
        )?;
        transaction.execute(
            "INSERT INTO fm_v2_attempts(owner_id,attempt_id,job_id,attempt_number,state,lease_epoch,lease_owner,lease_expires_at,heartbeat_at,progress_watermark,error_json,created_at,updated_at) VALUES (?,?,?,?,'leased',?,?,?,?,NULL,NULL,?,?)",
            params![owner, attempt_id, job_id, attempt_number, lease_epoch, worker_id, lease_expires_at, now, now, now],
        )?;
        if transaction.execute(
            "UPDATE fm_v2_jobs SET state='active',current_attempt_id=?,attempt_count=?,updated_at=? WHERE owner_id=? AND job_id=? AND state='queued'",
            params![attempt_id, attempt_number, now, owner, job_id],
        )? != 1
        {
            return Err(RepositoryError::Validation(
                "job changed before claim".into(),
            ));
        }
        insert_job_event(
            &transaction,
            &owner,
            job_id,
            JobState::Active,
            Some(&attempt_id),
            Some(lease_epoch),
            None,
            None,
            &change_set,
            &now,
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "job_state",
            &json!({"job_id": job_id, "state": "active", "attempt_id": attempt_id, "lease_epoch": lease_epoch}),
            attempt_number,
            &now,
        )?;
        transaction.commit()?;
        Ok((
            self.get_job(scope, job_id)?,
            JobAttempt {
                attempt_id,
                attempt_number,
                state: AttemptState::Leased,
                lease_epoch,
                lease_owner: Some(worker_id.into()),
                lease_expires_at: Some(lease_expires_at.into()),
            },
        ))
    }

    pub fn start_job_attempt(
        &mut self,
        scope: &ScopeRef,
        job_id: &str,
        attempt_id: &str,
        worker_id: &str,
        lease_epoch: i64,
        actor: &RevisionActor,
    ) -> Result<JobAttempt, RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        require_job_attempt(
            &transaction,
            &owner,
            &workspace,
            &project,
            job_id,
            attempt_id,
            worker_id,
            lease_epoch,
            "leased",
        )?;
        let payload = json!({"job_id": job_id, "attempt_id": attempt_id, "state": "running"});
        let change_set = insert_change_set(&transaction, &owner, actor, &payload, &now)?;
        transaction.execute(
            "UPDATE fm_v2_attempts SET state='running',heartbeat_at=?,updated_at=? WHERE owner_id=? AND attempt_id=? AND lease_epoch=? AND state='leased'",
            params![now, now, owner, attempt_id, lease_epoch],
        )?;
        insert_job_event(
            &transaction,
            &owner,
            job_id,
            JobState::Active,
            Some(attempt_id),
            Some(lease_epoch),
            None,
            None,
            &change_set,
            &now,
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "job_attempt",
            &payload,
            lease_epoch,
            &now,
        )?;
        transaction.commit()?;
        Ok(JobAttempt {
            attempt_id: attempt_id.into(),
            attempt_number: self.conn.query_row(
                "SELECT attempt_number FROM fm_v2_attempts WHERE owner_id=? AND attempt_id=?",
                params![owner, attempt_id],
                |row| row.get(0),
            )?,
            state: AttemptState::Running,
            lease_epoch,
            lease_owner: Some(worker_id.into()),
            lease_expires_at: self.conn.query_row(
                "SELECT lease_expires_at FROM fm_v2_attempts WHERE owner_id=? AND attempt_id=?",
                params![owner, attempt_id],
                |row| row.get(0),
            )?,
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn finish_job(
        &mut self,
        scope: &ScopeRef,
        job_id: &str,
        attempt_id: &str,
        worker_id: &str,
        lease_epoch: i64,
        state: JobState,
        result: Option<&Value>,
        error: Option<&Value>,
        actor: &RevisionActor,
    ) -> Result<JobRecord, RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        let attempt_state = match state {
            JobState::Succeeded | JobState::AwaitingReview => AttemptState::Succeeded,
            JobState::RetryWait => AttemptState::FailedRetryable,
            JobState::FailedTerminal => AttemptState::FailedTerminal,
            JobState::Cancelled => AttemptState::Cancelled,
            JobState::Queued | JobState::Active => {
                return Err(RepositoryError::Validation(
                    "finish state must be terminal, review, or retry-wait".into(),
                ));
            }
        };
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        require_job_attempt(
            &transaction,
            &owner,
            &workspace,
            &project,
            job_id,
            attempt_id,
            worker_id,
            lease_epoch,
            "running",
        )?;
        let payload = json!({"job_id": job_id, "attempt_id": attempt_id, "state": enum_text(&state)?, "result": result, "error": error});
        let change_set = insert_change_set(&transaction, &owner, actor, &payload, &now)?;
        transaction.execute(
            "UPDATE fm_v2_attempts SET state=?,error_json=?,updated_at=? WHERE owner_id=? AND attempt_id=? AND lease_epoch=? AND state='running'",
            params![enum_text(&attempt_state)?, error.map(serde_json::to_string).transpose()?, now, owner, attempt_id, lease_epoch],
        )?;
        if transaction.execute(
            "UPDATE fm_v2_jobs SET state=?,result_json=?,updated_at=? WHERE owner_id=? AND job_id=? AND state='active' AND current_attempt_id=?",
            params![enum_text(&state)?, result.map(serde_json::to_string).transpose()?, now, owner, job_id, attempt_id],
        )? != 1
        {
            return Err(RepositoryError::Validation(
                "job changed before completion".into(),
            ));
        }
        insert_job_event(
            &transaction,
            &owner,
            job_id,
            state,
            Some(attempt_id),
            Some(lease_epoch),
            result,
            error,
            &change_set,
            &now,
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "job_state",
            &payload,
            lease_epoch,
            &now,
        )?;
        transaction.commit()?;
        self.get_job(scope, job_id)
    }

    pub fn record_conflict(
        &mut self,
        scope: &ScopeRef,
        conflict_id: &str,
        block_id: &str,
        expected_revision: i64,
        competing_payload: &Value,
        rationale: &str,
        actor: &RevisionActor,
    ) -> Result<ConflictRecord, RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        if conflict_id.trim().is_empty()
            || block_id.trim().is_empty()
            || rationale.trim().is_empty()
            || !competing_payload.is_object()
        {
            return Err(RepositoryError::Validation(
                "conflict identity, object proposal, and rationale are required".into(),
            ));
        }
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (_, stored_workspace, stored_project, current_revision) =
            read_knowledge_identity(&transaction, &owner, block_id)?;
        if (stored_workspace, stored_project) != (workspace, project) {
            return Err(RepositoryError::NotFound);
        }
        if current_revision != expected_revision {
            return Err(RepositoryError::RevisionConflict {
                expected: expected_revision,
                current: current_revision,
                proposal: competing_payload.clone(),
            });
        }
        let payload = json!({"conflict_id": conflict_id, "block_id": block_id, "current_revision": current_revision, "competing_payload": competing_payload, "rationale": rationale});
        let change_set = insert_change_set(&transaction, &owner, actor, &payload, &now)?;
        transaction.execute(
            "INSERT INTO fm_v2_conflicts(owner_id,conflict_id,block_id,current_revision,competing_payload,state,rationale,created_change_set_id,resolved_change_set_id,created_at,resolved_at) VALUES (?,?,?,?,?,'open',?,?,NULL,?,NULL)",
            params![owner, conflict_id, block_id, current_revision, serde_json::to_string(competing_payload)?, rationale, change_set, now],
        )?;
        insert_conflict_event(
            &transaction,
            &owner,
            conflict_id,
            "open",
            rationale,
            &change_set,
            &now,
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "conflict_recorded",
            &json!({"conflict_id": conflict_id, "block_id": block_id, "current_revision": current_revision}),
            current_revision,
            &now,
        )?;
        transaction.commit()?;
        self.get_conflict(scope, conflict_id)
    }

    pub fn get_conflict(
        &self,
        scope: &ScopeRef,
        conflict_id: &str,
    ) -> Result<ConflictRecord, RepositoryError> {
        validate_scope(scope)?;
        let (owner, requested_workspace, requested_project) = scope.storage_keys();
        let record = self.conn.query_row(
            "SELECT conflict.block_id,block.workspace_key,block.project_key,conflict.current_revision,conflict.competing_payload,conflict.state,conflict.rationale,conflict.created_change_set_id,conflict.resolved_change_set_id,conflict.created_at,conflict.resolved_at FROM fm_v2_conflicts conflict JOIN fm_v2_knowledge_blocks block ON block.owner_id=conflict.owner_id AND block.block_id=conflict.block_id WHERE conflict.owner_id=? AND conflict.conflict_id=?",
            params![owner, conflict_id],
            |row| {
                let workspace: String = row.get(1)?;
                let project: String = row.get(2)?;
                Ok((workspace.clone(), project.clone(), ConflictRecord {
                    conflict_id: conflict_id.into(),
                    block_id: row.get(0)?,
                    scope: scope_from_keys(&owner, &workspace, &project),
                    current_revision: row.get(3)?,
                    competing_payload: decode_sql_json(row.get(4)?)?,
                    state: row.get(5)?,
                    rationale: row.get(6)?,
                    created_change_set_id: row.get(7)?,
                    resolved_change_set_id: row.get(8)?,
                    created_at: row.get(9)?,
                    resolved_at: row.get(10)?,
                }))
            },
        ).optional()?;
        let Some((workspace, project, record)) = record else {
            return Err(RepositoryError::NotFound);
        };
        if !scope_visible(
            &workspace,
            &project,
            &requested_workspace,
            &requested_project,
        ) {
            return Err(RepositoryError::NotFound);
        }
        Ok(record)
    }

    pub fn conflict_history(
        &self,
        scope: &ScopeRef,
        conflict_id: &str,
    ) -> Result<Vec<ConflictEvent>, RepositoryError> {
        self.get_conflict(scope, conflict_id)?;
        let owner = scope.owner_id.trim();
        let mut statement = self.conn.prepare(
            "SELECT sequence,state,rationale,change_set_id,created_at FROM fm_v2_conflict_events WHERE owner_id=? AND conflict_id=? ORDER BY sequence",
        )?;
        let events = statement
            .query_map(params![owner, conflict_id], |row| {
                Ok(ConflictEvent {
                    sequence: row.get(0)?,
                    state: row.get(1)?,
                    rationale: row.get(2)?,
                    change_set_id: row.get(3)?,
                    created_at: row.get(4)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(events)
    }

    pub fn resolve_conflict(
        &mut self,
        scope: &ScopeRef,
        conflict_id: &str,
        expected_revision: i64,
        payload: &KnowledgePayload,
        rationale: &str,
        actor: &RevisionActor,
    ) -> Result<KnowledgeRevision, RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        validate_knowledge_payload(payload)?;
        if rationale.trim().is_empty() {
            return Err(RepositoryError::Validation(
                "resolution rationale is required".into(),
            ));
        }
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let conflict: Option<(String, String)> = transaction
            .query_row(
                "SELECT block_id,state FROM fm_v2_conflicts WHERE owner_id=? AND conflict_id=?",
                params![owner, conflict_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let Some((block_id, state)) = conflict else {
            return Err(RepositoryError::NotFound);
        };
        if state != "open" {
            return Err(RepositoryError::Validation(
                "only an open conflict can be resolved".into(),
            ));
        }
        let (identity, stored_workspace, stored_project, current_revision) =
            read_knowledge_identity(&transaction, &owner, &block_id)?;
        if (stored_workspace, stored_project) != (workspace.clone(), project.clone()) {
            return Err(RepositoryError::NotFound);
        }
        if current_revision != expected_revision {
            return Err(RepositoryError::RevisionConflict {
                expected: expected_revision,
                current: current_revision,
                proposal: serde_json::to_value(payload)?,
            });
        }
        validate_knowledge_relations(
            &transaction,
            &owner,
            &workspace,
            &project,
            &identity,
            payload,
        )?;
        let revision = current_revision + 1;
        let resolution = json!({"conflict_id": conflict_id, "block_id": block_id, "revision": revision, "payload": payload, "rationale": rationale});
        let change_set = insert_change_set(&transaction, &owner, actor, &resolution, &now)?;
        insert_knowledge_revision(
            &transaction,
            &owner,
            &identity,
            revision,
            Some(current_revision),
            payload,
            actor,
            &change_set,
            &now,
        )?;
        if transaction.execute(
            "UPDATE fm_v2_knowledge_blocks SET current_revision=? WHERE owner_id=? AND block_id=? AND current_revision=?",
            params![revision, owner, block_id, expected_revision],
        )? != 1
        {
            return Err(RepositoryError::RevisionConflict {
                expected: expected_revision,
                current: current_revision,
                proposal: serde_json::to_value(payload)?,
            });
        }
        transaction.execute(
            "UPDATE fm_v2_conflicts SET state='resolved',rationale=?,resolved_change_set_id=?,resolved_at=? WHERE owner_id=? AND conflict_id=? AND state='open'",
            params![rationale, change_set, now, owner, conflict_id],
        )?;
        insert_conflict_event(
            &transaction,
            &owner,
            conflict_id,
            "resolved",
            rationale,
            &change_set,
            &now,
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "conflict_resolved",
            &json!({"conflict_id": conflict_id, "block_id": block_id, "revision": revision}),
            revision,
            &now,
        )?;
        transaction.commit()?;
        self.get_knowledge(scope, &block_id, None)
    }

    pub fn dismiss_conflict(
        &mut self,
        scope: &ScopeRef,
        conflict_id: &str,
        rationale: &str,
        actor: &RevisionActor,
    ) -> Result<ConflictRecord, RepositoryError> {
        let conflict = self.get_conflict(scope, conflict_id)?;
        if conflict.state != "open" || rationale.trim().is_empty() {
            return Err(RepositoryError::Validation(
                "an open conflict and rationale are required".into(),
            ));
        }
        actor.validate()?;
        let owner = scope.owner_id.trim().to_owned();
        let now = chrono::Utc::now().to_rfc3339();
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let payload =
            json!({"conflict_id": conflict_id, "state": "dismissed", "rationale": rationale});
        let change_set = insert_change_set(&transaction, &owner, actor, &payload, &now)?;
        if transaction.execute(
            "UPDATE fm_v2_conflicts SET state='dismissed',rationale=?,resolved_change_set_id=?,resolved_at=? WHERE owner_id=? AND conflict_id=? AND state='open'",
            params![rationale, change_set, now, owner, conflict_id],
        )? != 1
        {
            return Err(RepositoryError::Validation(
                "conflict changed before dismissal".into(),
            ));
        }
        insert_conflict_event(
            &transaction,
            &owner,
            conflict_id,
            "dismissed",
            rationale,
            &change_set,
            &now,
        )?;
        insert_outbox(
            &transaction,
            &owner,
            &change_set,
            "conflict_dismissed",
            &json!({"conflict_id": conflict_id}),
            conflict.current_revision,
            &now,
        )?;
        transaction.commit()?;
        self.get_conflict(scope, conflict_id)
    }

    pub fn erase_source(
        &mut self,
        scope: &ScopeRef,
        source_id: &str,
        actor: &RevisionActor,
    ) -> Result<PrivacyErasureResult, RepositoryError> {
        validate_scope(scope)?;
        actor.validate()?;
        if source_id.trim().is_empty() || actor.reason.trim().is_empty() {
            return Err(RepositoryError::Validation(
                "source ID and erasure reason are required".into(),
            ));
        }
        let (owner, workspace, project) = scope.storage_keys();
        let now = chrono::Utc::now().to_rfc3339();
        let erasure_id = format!("erase_{}", uuid::Uuid::new_v4().simple());
        let transaction = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let source_count: i64 = transaction.query_row(
            "SELECT count(*) FROM fm_v2_sources WHERE owner_id=? AND source_id=? AND workspace_key=? AND project_key=?",
            params![owner, source_id, workspace, project],
            |row| row.get(0),
        )?;
        if source_count == 0 {
            return Err(RepositoryError::NotFound);
        }
        let block_ids = query_string_column(
            &transaction,
            "SELECT DISTINCT link.block_id FROM fm_v2_revision_evidence link JOIN fm_v2_evidence evidence ON evidence.owner_id=link.owner_id AND evidence.evidence_id=link.evidence_id WHERE evidence.owner_id=? AND evidence.source_id=? UNION SELECT DISTINCT revision.block_id FROM fm_v2_knowledge_revisions revision JOIN json_each(revision.evidence_json) item JOIN fm_v2_evidence evidence ON evidence.owner_id=revision.owner_id AND evidence.evidence_id=item.value WHERE evidence.owner_id=? AND evidence.source_id=?",
            params![owner, source_id, owner, source_id],
        )?;
        let candidate_ids = query_string_column(
            &transaction,
            "SELECT DISTINCT revision.candidate_id FROM fm_v2_candidate_revisions revision WHERE revision.owner_id=? AND revision.source_id=? UNION SELECT DISTINCT link.candidate_id FROM fm_v2_candidate_evidence link JOIN fm_v2_evidence evidence ON evidence.owner_id=link.owner_id AND evidence.evidence_id=link.evidence_id WHERE evidence.owner_id=? AND evidence.source_id=?",
            params![owner, source_id, owner, source_id],
        )?;
        let mut entity_ids = query_string_column(
            &transaction,
            "SELECT DISTINCT block.subject_entity_id FROM fm_v2_knowledge_blocks block JOIN fm_v2_revision_evidence link ON link.owner_id=block.owner_id AND link.block_id=block.block_id JOIN fm_v2_evidence evidence ON evidence.owner_id=link.owner_id AND evidence.evidence_id=link.evidence_id WHERE evidence.owner_id=? AND evidence.source_id=?",
            params![owner, source_id],
        )?;
        for block_id in &block_ids {
            entity_ids.extend(query_string_column(
                &transaction,
                "SELECT subject_entity_id FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",
                params![owner, block_id],
            )?);
        }
        entity_ids.sort();
        entity_ids.dedup();
        let mut affected_change_sets: BTreeSet<String> = BTreeSet::new();
        affected_change_sets.extend(query_string_column(
            &transaction,
            "SELECT DISTINCT revision.change_set_id FROM fm_v2_knowledge_revisions revision JOIN fm_v2_revision_evidence link ON link.owner_id=revision.owner_id AND link.block_id=revision.block_id AND link.revision=revision.revision JOIN fm_v2_evidence evidence ON evidence.owner_id=link.owner_id AND evidence.evidence_id=link.evidence_id WHERE evidence.owner_id=? AND evidence.source_id=? UNION SELECT DISTINCT revision.change_set_id FROM fm_v2_knowledge_revisions revision JOIN json_each(revision.evidence_json) item JOIN fm_v2_evidence evidence ON evidence.owner_id=revision.owner_id AND evidence.evidence_id=item.value WHERE evidence.owner_id=? AND evidence.source_id=?",
            params![owner, source_id, owner, source_id],
        )?);
        for query in [
            "SELECT DISTINCT revision.change_set_id FROM fm_v2_candidate_revisions revision WHERE revision.owner_id=? AND revision.source_id=?",
            "SELECT DISTINCT revision.change_set_id FROM fm_v2_candidate_revisions revision JOIN fm_v2_candidate_evidence link ON link.owner_id=revision.owner_id AND link.candidate_id=revision.candidate_id AND link.revision=revision.revision JOIN fm_v2_evidence evidence ON evidence.owner_id=link.owner_id AND evidence.evidence_id=link.evidence_id WHERE evidence.owner_id=? AND evidence.source_id=?",
            "SELECT DISTINCT outbox.change_set_id FROM fm_v2_outbox outbox WHERE outbox.owner_id=? AND json_valid(outbox.payload_json) AND json_extract(outbox.payload_json,'$.source_id')=?",
        ] {
            affected_change_sets.extend(query_string_column(
                &transaction,
                query,
                params![owner, source_id],
            )?);
        }
        for block_id in &block_ids {
            affected_change_sets.extend(query_string_column(
                &transaction,
                "SELECT created_change_set_id FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=? UNION SELECT created_change_set_id FROM fm_v2_conflicts WHERE owner_id=? AND block_id=? UNION SELECT resolved_change_set_id FROM fm_v2_conflicts WHERE owner_id=? AND block_id=? AND resolved_change_set_id IS NOT NULL",
                params![owner, block_id, owner, block_id, owner, block_id],
            )?);
        }
        for candidate_id in &candidate_ids {
            affected_change_sets.extend(query_string_column(
                &transaction,
                "SELECT created_change_set_id FROM fm_v2_candidates WHERE owner_id=? AND candidate_id=? UNION SELECT change_set_id FROM fm_v2_candidate_revisions WHERE owner_id=? AND candidate_id=?",
                params![owner, candidate_id, owner, candidate_id],
            )?);
        }
        transaction.execute(
            "INSERT INTO fm_v2_privacy_erasure_context(owner_id,erasure_id,source_id,authorized_by,reason,created_at) VALUES (?,?,?,?,?,?)",
            params![owner, erasure_id, source_id, actor.actor_id, actor.reason, now],
        )?;

        let mut counts = BTreeMap::new();
        let mut removed_candidate_evidence = 0_i64;
        let mut removed_candidate_revisions = 0_i64;
        let mut removed_candidates = 0_i64;
        for candidate_id in &candidate_ids {
            removed_candidate_evidence += transaction.execute(
                "DELETE FROM fm_v2_candidate_evidence WHERE owner_id=? AND candidate_id=?",
                params![owner, candidate_id],
            )? as i64;
            removed_candidate_revisions += transaction.execute(
                "DELETE FROM fm_v2_candidate_revisions WHERE owner_id=? AND candidate_id=?",
                params![owner, candidate_id],
            )? as i64;
            removed_candidates += transaction.execute(
                "DELETE FROM fm_v2_candidates WHERE owner_id=? AND candidate_id=?",
                params![owner, candidate_id],
            )? as i64;
        }
        counts.insert("candidate_evidence".into(), removed_candidate_evidence);
        counts.insert("candidate_revisions".into(), removed_candidate_revisions);
        counts.insert("candidates".into(), removed_candidates);
        let mut removed_conflict_events = 0_i64;
        let mut removed_conflicts = 0_i64;
        let mut removed_revision_evidence = 0_i64;
        let mut removed_revisions = 0_i64;
        let mut removed_blocks = 0_i64;
        for block_id in &block_ids {
            removed_conflict_events += transaction.execute(
                "DELETE FROM fm_v2_conflict_events WHERE owner_id=? AND conflict_id IN (SELECT conflict_id FROM fm_v2_conflicts WHERE owner_id=? AND block_id=?)",
                params![owner, owner, block_id],
            )? as i64;
            removed_conflicts += transaction.execute(
                "DELETE FROM fm_v2_conflicts WHERE owner_id=? AND block_id=?",
                params![owner, block_id],
            )? as i64;
            removed_revision_evidence += transaction.execute(
                "DELETE FROM fm_v2_revision_evidence WHERE owner_id=? AND block_id=?",
                params![owner, block_id],
            )? as i64;
            removed_revisions += transaction.execute(
                "DELETE FROM fm_v2_knowledge_revisions WHERE owner_id=? AND block_id=?",
                params![owner, block_id],
            )? as i64;
            removed_blocks += transaction.execute(
                "DELETE FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",
                params![owner, block_id],
            )? as i64;
        }
        counts.insert("conflict_events".into(), removed_conflict_events);
        counts.insert("conflicts".into(), removed_conflicts);
        counts.insert("revision_evidence".into(), removed_revision_evidence);
        counts.insert("knowledge_revisions".into(), removed_revisions);
        counts.insert("knowledge_blocks".into(), removed_blocks);

        let mut removed_entities = 0_i64;
        let mut removed_entity_revisions = 0_i64;
        for entity_id in &entity_ids {
            let still_referenced: bool = transaction.query_row(
                "SELECT EXISTS(SELECT 1 FROM fm_v2_knowledge_blocks WHERE owner_id=? AND subject_entity_id=?)",
                params![owner, entity_id],
                |row| row.get(0),
            )?;
            if still_referenced {
                continue;
            }
            affected_change_sets.extend(query_string_column(
                &transaction,
                "SELECT created_change_set_id FROM fm_v2_entities WHERE owner_id=? AND entity_id=? UNION SELECT change_set_id FROM fm_v2_entity_revisions WHERE owner_id=? AND entity_id=?",
                params![owner, entity_id, owner, entity_id],
            )?);
            transaction.execute(
                "DELETE FROM fm_v2_entity_aliases WHERE owner_id=? AND entity_id=?",
                params![owner, entity_id],
            )?;
            transaction.execute(
                "DELETE FROM fm_v2_entity_roles WHERE owner_id=? AND entity_id=?",
                params![owner, entity_id],
            )?;
            transaction.execute(
                "DELETE FROM fm_v2_entity_tags WHERE owner_id=? AND entity_id=?",
                params![owner, entity_id],
            )?;
            removed_entity_revisions += transaction.execute(
                "DELETE FROM fm_v2_entity_revisions WHERE owner_id=? AND entity_id=?",
                params![owner, entity_id],
            )? as i64;
            removed_entities += transaction.execute(
                "DELETE FROM fm_v2_entities WHERE owner_id=? AND entity_id=?",
                params![owner, entity_id],
            )? as i64;
        }
        counts.insert("entity_revisions".into(), removed_entity_revisions);
        counts.insert("entities".into(), removed_entities);
        counts.insert(
            "chunk_embeddings".into(),
            transaction.execute(
                "DELETE FROM fm_v2_chunk_embeddings WHERE owner_id=? AND chunk_id IN (SELECT chunk.chunk_id FROM fm_v2_chunks chunk JOIN fm_v2_documents document ON document.owner_id=chunk.owner_id AND document.document_id=chunk.document_id WHERE document.owner_id=? AND document.source_id=?)",
                params![owner, owner, source_id],
            )? as i64,
        );
        counts.insert(
            "chunks".into(),
            transaction.execute(
                "DELETE FROM fm_v2_chunks WHERE owner_id=? AND document_id IN (SELECT document_id FROM fm_v2_documents WHERE owner_id=? AND source_id=?)",
                params![owner, owner, source_id],
            )? as i64,
        );
        counts.insert(
            "documents".into(),
            transaction.execute(
                "DELETE FROM fm_v2_documents WHERE owner_id=? AND source_id=?",
                params![owner, source_id],
            )? as i64,
        );
        counts.insert(
            "episodes".into(),
            transaction.execute(
                "DELETE FROM fm_v2_episodes WHERE owner_id=? AND source_id=?",
                params![owner, source_id],
            )? as i64,
        );
        counts.insert(
            "evidence".into(),
            transaction.execute(
                "DELETE FROM fm_v2_evidence WHERE owner_id=? AND source_id=?",
                params![owner, source_id],
            )? as i64,
        );
        counts.insert(
            "sources".into(),
            transaction.execute(
                "DELETE FROM fm_v2_sources WHERE owner_id=? AND source_id=? AND workspace_key=? AND project_key=?",
                params![owner, source_id, workspace, project],
            )? as i64,
        );
        let mut removed_outbox = 0_i64;
        let mut removed_change_sets = 0_i64;
        for change_set in &affected_change_sets {
            removed_outbox += transaction.execute(
                "DELETE FROM fm_v2_outbox WHERE owner_id=? AND change_set_id=?",
                params![owner, change_set],
            )? as i64;
            removed_change_sets += transaction.execute(
                "DELETE FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND NOT EXISTS (SELECT 1 FROM fm_v2_entities WHERE owner_id=? AND created_change_set_id=?) AND NOT EXISTS (SELECT 1 FROM fm_v2_entity_revisions WHERE owner_id=? AND change_set_id=?) AND NOT EXISTS (SELECT 1 FROM fm_v2_knowledge_blocks WHERE owner_id=? AND created_change_set_id=?) AND NOT EXISTS (SELECT 1 FROM fm_v2_knowledge_revisions WHERE owner_id=? AND change_set_id=?) AND NOT EXISTS (SELECT 1 FROM fm_v2_candidates WHERE owner_id=? AND created_change_set_id=?) AND NOT EXISTS (SELECT 1 FROM fm_v2_candidate_revisions WHERE owner_id=? AND change_set_id=?) AND NOT EXISTS (SELECT 1 FROM fm_v2_conflicts WHERE owner_id=? AND (created_change_set_id=? OR resolved_change_set_id=?)) AND NOT EXISTS (SELECT 1 FROM fm_v2_conflict_events WHERE owner_id=? AND change_set_id=?) AND NOT EXISTS (SELECT 1 FROM fm_v2_job_events WHERE owner_id=? AND change_set_id=?)",
                params![owner, change_set, owner, change_set, owner, change_set, owner, change_set, owner, change_set, owner, change_set, owner, change_set, owner, change_set, change_set, owner, change_set, owner, change_set],
            )? as i64;
        }
        counts.insert("outbox".into(), removed_outbox);
        counts.insert("change_sets".into(), removed_change_sets);

        transaction.execute(
            "DELETE FROM fm_v2_privacy_erasure_context WHERE owner_id=? AND erasure_id=?",
            params![owner, erasure_id],
        )?;
        transaction.execute(
            "INSERT INTO fm_v2_privacy_erasure_tombstones(owner_id,erasure_id,source_id,authorized_by,reason,erased_counts_json,created_at) VALUES (?,?,?,?,?,?,?)",
            params![owner, erasure_id, source_id, actor.actor_id, actor.reason, serde_json::to_string(&counts)?, now],
        )?;
        let audit_payload =
            json!({"erasure_id": erasure_id, "source_id": source_id, "erased_counts": counts});
        let audit_change_set =
            insert_change_set(&transaction, &owner, actor, &audit_payload, &now)?;
        insert_outbox(
            &transaction,
            &owner,
            &audit_change_set,
            "privacy_erasure",
            &audit_payload,
            1,
            &now,
        )?;
        transaction.commit()?;
        Ok(PrivacyErasureResult {
            erasure_id,
            source_id: source_id.into(),
            erased_counts: counts,
        })
    }
}

fn validate_scope(scope: &ScopeRef) -> Result<(), RepositoryError> {
    scope.validate().map_err(RepositoryError::Validation)
}

fn scope_from_keys(owner: &str, workspace: &str, project: &str) -> ScopeRef {
    ScopeRef {
        owner_id: owner.into(),
        workspace_id: (!workspace.is_empty()).then(|| workspace.into()),
        project_id: (!project.is_empty()).then(|| project.into()),
        session_id: None,
    }
}

fn validate_source(
    source_id: &str,
    source_revision: i64,
    payload: &SourcePayload,
) -> Result<(), RepositoryError> {
    if source_id.trim().is_empty()
        || source_revision < 1
        || payload.source_uri.trim().is_empty()
        || payload.content_hash.trim().is_empty()
        || payload.source_type.trim().is_empty()
    {
        return Err(RepositoryError::Validation(
            "source identity, revision, URI, hash, and type are required".into(),
        ));
    }
    Ok(())
}

fn validate_evidence(evidence_id: &str, payload: &EvidencePayload) -> Result<(), RepositoryError> {
    if evidence_id.trim().is_empty()
        || payload.source_id.trim().is_empty()
        || payload.source_revision < 1
        || payload.locator_type.trim().is_empty()
        || payload.extraction_contract.trim().is_empty()
        || payload.parser_version.trim().is_empty()
    {
        return Err(RepositoryError::Validation(
            "evidence identity, source, locator, contract, and parser are required".into(),
        ));
    }
    if !payload.locator.is_object() || !payload.validation.is_object() {
        return Err(RepositoryError::Validation(
            "evidence locator and validation must be objects".into(),
        ));
    }
    Ok(())
}

fn validate_candidate(
    candidate_id: &str,
    idempotency_key: &str,
    payload: &CandidatePayload,
    creating: bool,
) -> Result<(), RepositoryError> {
    if candidate_id.trim().is_empty()
        || idempotency_key.trim().is_empty()
        || payload.reason.trim().is_empty()
        || !payload.proposal.is_object()
    {
        return Err(RepositoryError::Validation(
            "candidate identity, idempotency key, object proposal, and reason are required".into(),
        ));
    }
    if creating && payload.state != CandidateState::Proposed {
        return Err(RepositoryError::Validation(
            "a new candidate must start proposed".into(),
        ));
    }
    if !(0.0..=1.0).contains(&payload.confidence) || !(0.0..=1.0).contains(&payload.importance) {
        return Err(RepositoryError::Validation(
            "candidate confidence and importance must be between 0 and 1".into(),
        ));
    }
    if payload.source_id.is_some() != payload.source_revision.is_some() {
        return Err(RepositoryError::Validation(
            "candidate source ID and revision must be supplied together".into(),
        ));
    }
    if payload.state == CandidateState::Activated && payload.accepted_block_id.is_none() {
        return Err(RepositoryError::Validation(
            "an activated candidate requires an accepted block".into(),
        ));
    }
    validate_strings("evidence_ids", &payload.evidence_ids)
}

fn require_source_scope(
    conn: &Connection,
    owner: &str,
    workspace: &str,
    project: &str,
    source_id: &str,
    source_revision: i64,
) -> Result<(), RepositoryError> {
    let exists: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=? AND source_id=? AND source_revision=? AND workspace_key=? AND project_key=? AND forget_state='active')",
        params![owner, source_id, source_revision, workspace, project],
        |row| row.get(0),
    )?;
    if !exists {
        return Err(RepositoryError::Validation(
            "source is absent from the exact active tenant scope".into(),
        ));
    }
    Ok(())
}

fn validate_candidate_relations(
    conn: &Connection,
    owner: &str,
    workspace: &str,
    project: &str,
    payload: &CandidatePayload,
) -> Result<(), RepositoryError> {
    if let (Some(source_id), Some(source_revision)) = (&payload.source_id, payload.source_revision)
    {
        require_source_scope(conn, owner, workspace, project, source_id, source_revision)?;
    }
    for evidence_id in &payload.evidence_ids {
        let exists: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM fm_v2_evidence evidence JOIN fm_v2_sources source ON source.owner_id=evidence.owner_id AND source.source_id=evidence.source_id AND source.source_revision=evidence.source_revision WHERE evidence.owner_id=? AND evidence.evidence_id=? AND source.workspace_key=? AND source.project_key=? AND source.forget_state='active')",
            params![owner, evidence_id, workspace, project],
            |row| row.get(0),
        )?;
        if !exists {
            return Err(RepositoryError::Validation(format!(
                "evidence is absent from the exact active tenant scope: {evidence_id}"
            )));
        }
    }
    if let Some(block_id) = &payload.accepted_block_id {
        let exists: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=? AND workspace_key=? AND project_key=?)",
            params![owner, block_id, workspace, project],
            |row| row.get(0),
        )?;
        if !exists {
            return Err(RepositoryError::Validation(
                "accepted block is absent from the exact tenant scope".into(),
            ));
        }
    }
    Ok(())
}

fn validate_entity(
    entity_id: &str,
    payload: &EntityPayload,
    actor: &RevisionActor,
) -> Result<(), RepositoryError> {
    actor.validate()?;
    if entity_id.trim().is_empty()
        || payload.entity_type.trim().is_empty()
        || payload.status.trim().is_empty()
    {
        return Err(RepositoryError::Validation(
            "entity_id, entity_type, and status are required".into(),
        ));
    }
    validate_strings("aliases", &payload.aliases)?;
    validate_strings("roles", &payload.roles)?;
    validate_strings("tags", &payload.tags)
}

fn validate_knowledge(
    identity: &KnowledgeIdentity,
    payload: &KnowledgePayload,
    actor: &RevisionActor,
) -> Result<(), RepositoryError> {
    actor.validate()?;
    for (name, value) in [
        ("block_id", &identity.block_id),
        ("subject_entity_id", &identity.subject_entity_id),
        ("predicate", &identity.predicate),
        ("claim_slot", &identity.claim_slot),
    ] {
        if value.trim().is_empty() {
            return Err(RepositoryError::Validation(format!("{name} is required")));
        }
    }
    validate_knowledge_payload(payload)
}

fn validate_knowledge_payload(payload: &KnowledgePayload) -> Result<(), RepositoryError> {
    payload
        .value
        .validate()
        .map_err(RepositoryError::Validation)?;
    if payload.kind.trim().is_empty() || payload.activation.trim().is_empty() {
        return Err(RepositoryError::Validation(
            "kind and activation are required".into(),
        ));
    }
    if !(0.0..=1.0).contains(&payload.confidence) || !(0.0..=1.0).contains(&payload.trust) {
        return Err(RepositoryError::Validation(
            "confidence and trust must be between 0 and 1".into(),
        ));
    }
    if payload.status == KnowledgeStatus::Open {
        if !matches!(payload.value, TypedValue::Null) || payload.expected_value_type.is_none() {
            return Err(RepositoryError::Validation(
                "open knowledge requires a null value and expected_value_type".into(),
            ));
        }
    }
    if payload.valid_from.is_some()
        && payload.valid_to.is_some()
        && payload.valid_to <= payload.valid_from
    {
        return Err(RepositoryError::Validation(
            "valid_to must be after valid_from".into(),
        ));
    }
    validate_strings("tags", &payload.tags)?;
    validate_strings("evidence_ids", &payload.evidence_ids)
}

fn validate_strings(name: &str, values: &[String]) -> Result<(), RepositoryError> {
    if values.iter().any(|value| value.trim().is_empty()) {
        return Err(RepositoryError::Validation(format!(
            "{name} cannot contain blanks"
        )));
    }
    let unique: BTreeSet<_> = values.iter().collect();
    if unique.len() != values.len() {
        return Err(RepositoryError::Validation(format!(
            "{name} cannot contain duplicates"
        )));
    }
    Ok(())
}

fn scope_visible(
    stored_workspace: &str,
    stored_project: &str,
    requested_workspace: &str,
    requested_project: &str,
) -> bool {
    (stored_workspace.is_empty() && stored_project.is_empty())
        || (!requested_workspace.is_empty()
            && stored_workspace == requested_workspace
            && (stored_project.is_empty()
                || (!requested_project.is_empty() && stored_project == requested_project)))
}

fn hash_value(value: &Value) -> Result<String, RepositoryError> {
    let bytes = serde_json::to_vec(value)?;
    Ok(blake3::hash(&bytes).to_hex().to_string())
}

fn enum_text<T: Serialize>(value: &T) -> Result<String, RepositoryError> {
    serde_json::to_value(value)?
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| RepositoryError::Validation("enum did not serialize as text".into()))
}

fn decode_enum_text<T: DeserializeOwned>(raw: &str) -> rusqlite::Result<T> {
    serde_json::from_value(Value::String(raw.into())).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(
            raw.len(),
            rusqlite::types::Type::Text,
            Box::new(error),
        )
    })
}

fn decode_sql_json<T: DeserializeOwned>(raw: String) -> rusqlite::Result<T> {
    serde_json::from_str(&raw).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(
            raw.len(),
            rusqlite::types::Type::Text,
            Box::new(error),
        )
    })
}

fn query_string_column<P: rusqlite::Params>(
    conn: &Connection,
    sql: &str,
    parameters: P,
) -> Result<Vec<String>, RepositoryError> {
    let mut statement = conn.prepare(sql)?;
    let values = statement
        .query_map(parameters, |row| row.get(0))?
        .collect::<Result<Vec<String>, _>>()?;
    Ok(values)
}

fn insert_change_set(
    transaction: &Transaction<'_>,
    owner: &str,
    actor: &RevisionActor,
    payload: &Value,
    now: &str,
) -> Result<String, RepositoryError> {
    let change_set = format!("cs_v2_{}", uuid::Uuid::new_v4().simple());
    transaction.execute(
        "INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        params![owner, change_set, actor.actor_type, actor.actor_id, actor.reason, actor.source_event_id, CONTRACT_VERSION, hash_value(payload)?, now],
    )?;
    Ok(change_set)
}

fn insert_outbox(
    transaction: &Transaction<'_>,
    owner: &str,
    change_set: &str,
    kind: &str,
    payload: &Value,
    revision: i64,
    now: &str,
) -> Result<(), RepositoryError> {
    transaction.execute(
        "INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES (?,?,?,?,?,?,?)",
        params![owner, format!("outbox_{change_set}_{kind}"), change_set, kind, serde_json::to_string(payload)?, revision, now],
    )?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn insert_job_event(
    transaction: &Transaction<'_>,
    owner: &str,
    job_id: &str,
    state: JobState,
    attempt_id: Option<&str>,
    lease_epoch: Option<i64>,
    result: Option<&Value>,
    error: Option<&Value>,
    change_set: &str,
    now: &str,
) -> Result<(), RepositoryError> {
    let sequence: i64 = transaction.query_row(
        "SELECT COALESCE(MAX(sequence),0)+1 FROM fm_v2_job_events WHERE owner_id=? AND job_id=?",
        params![owner, job_id],
        |row| row.get(0),
    )?;
    transaction.execute(
        "INSERT INTO fm_v2_job_events(owner_id,job_id,sequence,state,attempt_id,lease_epoch,result_json,error_json,change_set_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        params![owner, job_id, sequence, enum_text(&state)?, attempt_id, lease_epoch, result.map(serde_json::to_string).transpose()?, error.map(serde_json::to_string).transpose()?, change_set, now],
    )?;
    Ok(())
}

fn insert_conflict_event(
    transaction: &Transaction<'_>,
    owner: &str,
    conflict_id: &str,
    state: &str,
    rationale: &str,
    change_set: &str,
    now: &str,
) -> Result<(), RepositoryError> {
    let sequence: i64 = transaction.query_row(
        "SELECT COALESCE(MAX(sequence),0)+1 FROM fm_v2_conflict_events WHERE owner_id=? AND conflict_id=?",
        params![owner, conflict_id],
        |row| row.get(0),
    )?;
    transaction.execute(
        "INSERT INTO fm_v2_conflict_events(owner_id,conflict_id,sequence,state,rationale,change_set_id,created_at) VALUES (?,?,?,?,?,?,?)",
        params![owner, conflict_id, sequence, state, rationale, change_set, now],
    )?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn require_job_attempt(
    conn: &Connection,
    owner: &str,
    workspace: &str,
    project: &str,
    job_id: &str,
    attempt_id: &str,
    worker_id: &str,
    lease_epoch: i64,
    expected_state: &str,
) -> Result<(), RepositoryError> {
    let exists: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM fm_v2_jobs job JOIN fm_v2_attempts attempt ON attempt.owner_id=job.owner_id AND attempt.job_id=job.job_id WHERE job.owner_id=? AND job.job_id=? AND job.workspace_key=? AND job.project_key=? AND job.state='active' AND job.current_attempt_id=? AND attempt.attempt_id=? AND attempt.lease_owner=? AND attempt.lease_epoch=? AND attempt.state=?)",
        params![owner, job_id, workspace, project, attempt_id, attempt_id, worker_id, lease_epoch, expected_state],
        |row| row.get(0),
    )?;
    if !exists {
        return Err(RepositoryError::Validation(
            "job attempt is stale, fenced, or outside the exact tenant scope".into(),
        ));
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn insert_entity_revision(
    transaction: &Transaction<'_>,
    owner: &str,
    entity_id: &str,
    revision: i64,
    previous_revision: Option<i64>,
    payload: &EntityPayload,
    actor: &RevisionActor,
    change_set: &str,
    now: &str,
) -> Result<(), RepositoryError> {
    let payload_value = serde_json::to_value(payload)?;
    transaction.execute(
        "INSERT INTO fm_v2_entity_revisions(owner_id,entity_id,revision,payload,content_hash,actor_type,actor_id,reason,change_set_id,previous_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        params![owner, entity_id, revision, serde_json::to_string(&payload_value)?, hash_value(&payload_value)?, actor.actor_type, actor.actor_id, actor.reason, change_set, previous_revision, now],
    )?;
    transaction.execute(
        "UPDATE fm_v2_entity_roles SET is_current=0 WHERE owner_id=? AND entity_id=? AND is_current=1",
        params![owner, entity_id],
    )?;
    for alias in &payload.aliases {
        transaction.execute(
            "INSERT INTO fm_v2_entity_aliases(owner_id,entity_id,revision,alias) VALUES (?,?,?,?)",
            params![owner, entity_id, revision, alias],
        )?;
    }
    let (workspace, project): (String, String) = transaction.query_row(
        "SELECT workspace_key,project_key FROM fm_v2_entities WHERE owner_id=? AND entity_id=?",
        params![owner, entity_id],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )?;
    for role in &payload.roles {
        transaction.execute(
            "INSERT INTO fm_v2_entity_roles(owner_id,entity_id,revision,workspace_key,project_key,role,is_current) VALUES (?,?,?,?,?,?,1)",
            params![owner, entity_id, revision, workspace, project, role],
        )?;
    }
    for tag in &payload.tags {
        transaction.execute(
            "INSERT INTO fm_v2_entity_tags(owner_id,entity_id,revision,tag) VALUES (?,?,?,?)",
            params![owner, entity_id, revision, tag],
        )?;
    }
    Ok(())
}

fn read_entity(
    conn: &Connection,
    owner: &str,
    entity_id: &str,
    revision: i64,
    workspace: &str,
    project: &str,
) -> Result<EntityRevision, RepositoryError> {
    conn.query_row(
        "SELECT payload,previous_revision,actor_type,actor_id,reason,change_set_id,content_hash,created_at FROM fm_v2_entity_revisions WHERE owner_id=? AND entity_id=? AND revision=?",
        params![owner, entity_id, revision],
        |row| {
            let payload_raw: String = row.get(0)?;
            let payload = serde_json::from_str(&payload_raw).map_err(|error| {
                rusqlite::Error::FromSqlConversionFailure(
                    payload_raw.len(),
                    rusqlite::types::Type::Text,
                    Box::new(error),
                )
            })?;
            Ok(EntityRevision {
                entity_id: entity_id.into(),
                scope: ScopeRef { owner_id: owner.into(), workspace_id: (!workspace.is_empty()).then(|| workspace.into()), project_id: (!project.is_empty()).then(|| project.into()), session_id: None },
                payload,
                revision,
                previous_revision: row.get(1)?,
                actor_type: row.get(2)?,
                actor_id: row.get(3)?,
                reason: row.get(4)?,
                change_set_id: row.get(5)?,
                content_hash: row.get(6)?,
                created_at: row.get(7)?,
            })
        },
    ).optional()?.ok_or(RepositoryError::NotFound)
}

fn validate_knowledge_relations(
    transaction: &Transaction<'_>,
    owner: &str,
    workspace: &str,
    project: &str,
    identity: &KnowledgeIdentity,
    payload: &KnowledgePayload,
) -> Result<(), RepositoryError> {
    let subject_exists: bool = transaction.query_row(
        "SELECT EXISTS(SELECT 1 FROM fm_v2_entities WHERE owner_id=? AND entity_id=? AND workspace_key=? AND project_key=?)",
        params![owner, identity.subject_entity_id, workspace, project],
        |row| row.get(0),
    )?;
    if !subject_exists {
        return Err(RepositoryError::Validation(
            "subject entity is absent from the exact scope".into(),
        ));
    }
    let registry: Option<(String, String)> = transaction
        .query_row(
            "SELECT value_type,cardinality FROM fm_v2_predicates WHERE predicate=?",
            params![identity.predicate],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    if let Some((expected_type, cardinality)) = registry {
        let actual_type = match payload.value {
            TypedValue::Null => payload
                .expected_value_type
                .as_ref()
                .map(enum_text)
                .transpose()?
                .unwrap_or_default(),
            _ => typed_value_name(&payload.value).into(),
        };
        if expected_type != actual_type || cardinality != enum_text(&identity.cardinality)? {
            return Err(RepositoryError::Validation(
                "predicate value type or cardinality does not match its registry".into(),
            ));
        }
    }
    for evidence_id in &payload.evidence_ids {
        let exists: bool = transaction.query_row(
            "SELECT EXISTS(SELECT 1 FROM fm_v2_evidence WHERE owner_id=? AND evidence_id=?)",
            params![owner, evidence_id],
            |row| row.get(0),
        )?;
        if !exists {
            return Err(RepositoryError::Validation(format!(
                "evidence is absent from tenant scope: {evidence_id}"
            )));
        }
    }
    Ok(())
}

fn typed_value_name(value: &TypedValue) -> &'static str {
    match value {
        TypedValue::Null => "null",
        TypedValue::String(_) => "string",
        TypedValue::Number(_) => "number",
        TypedValue::Boolean(_) => "boolean",
        TypedValue::Time(_) => "time",
        TypedValue::Duration(_) => "duration",
        TypedValue::Uri(_) => "uri",
        TypedValue::EntityRef { .. } => "entity_ref",
        TypedValue::Object(_) => "object",
        TypedValue::List(_) => "list",
    }
}

#[allow(clippy::too_many_arguments)]
fn insert_knowledge_revision(
    transaction: &Transaction<'_>,
    owner: &str,
    identity: &KnowledgeIdentity,
    revision: i64,
    previous_revision: Option<i64>,
    payload: &KnowledgePayload,
    actor: &RevisionActor,
    change_set: &str,
    now: &str,
) -> Result<(), RepositoryError> {
    let payload_value = serde_json::to_value(payload)?;
    let value_json = serde_json::to_string(&payload.value)?;
    transaction.execute(
        "INSERT INTO fm_v2_knowledge_revisions(owner_id,block_id,revision,value_type,value_json,expected_value_type,kind,tags_json,status,confidence,trust,activation,activation_rationale,valid_from,valid_to,evidence_json,content_hash,actor_type,actor_id,reason,source_event_id,change_set_id,previous_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        params![owner, identity.block_id, revision, typed_value_name(&payload.value), value_json, payload.expected_value_type.as_ref().map(enum_text).transpose()?, payload.kind, serde_json::to_string(&payload.tags)?, enum_text(&payload.status)?, payload.confidence, payload.trust, payload.activation, serde_json::to_string(&payload.activation_rationale)?, payload.valid_from, payload.valid_to, serde_json::to_string(&payload.evidence_ids)?, hash_value(&payload_value)?, actor.actor_type, actor.actor_id, actor.reason, actor.source_event_id, change_set, previous_revision, now],
    )?;
    for evidence_id in &payload.evidence_ids {
        transaction.execute(
            "INSERT INTO fm_v2_revision_evidence(owner_id,block_id,revision,evidence_id) VALUES (?,?,?,?)",
            params![owner, identity.block_id, revision, evidence_id],
        )?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn insert_candidate_revision(
    transaction: &Transaction<'_>,
    owner: &str,
    candidate_id: &str,
    revision: i64,
    previous_revision: Option<i64>,
    payload: &CandidatePayload,
    actor: &RevisionActor,
    change_set: &str,
    now: &str,
) -> Result<(), RepositoryError> {
    let payload_value = serde_json::to_value(payload)?;
    transaction.execute(
        "INSERT INTO fm_v2_candidate_revisions(owner_id,candidate_id,revision,proposal_json,state,confidence,importance,reason,source_id,source_revision,accepted_block_id,content_hash,actor_type,actor_id,source_event_id,change_set_id,previous_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        params![owner, candidate_id, revision, serde_json::to_string(&payload.proposal)?, enum_text(&payload.state)?, payload.confidence, payload.importance, payload.reason, payload.source_id, payload.source_revision, payload.accepted_block_id, hash_value(&payload_value)?, actor.actor_type, actor.actor_id, actor.source_event_id, change_set, previous_revision, now],
    )?;
    for evidence_id in &payload.evidence_ids {
        transaction.execute(
            "INSERT INTO fm_v2_candidate_evidence(owner_id,candidate_id,revision,evidence_id) VALUES (?,?,?,?)",
            params![owner, candidate_id, revision, evidence_id],
        )?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn read_candidate(
    conn: &Connection,
    owner: &str,
    candidate_id: &str,
    idempotency_key: &str,
    revision: i64,
    workspace: &str,
    project: &str,
) -> Result<CandidateRevision, RepositoryError> {
    let mut candidate = conn.query_row(
        "SELECT proposal_json,state,confidence,importance,reason,source_id,source_revision,accepted_block_id,previous_revision,actor_type,actor_id,source_event_id,change_set_id,content_hash,created_at FROM fm_v2_candidate_revisions WHERE owner_id=? AND candidate_id=? AND revision=?",
        params![owner, candidate_id, revision],
        |row| {
            let state_raw: String = row.get(1)?;
            let state = serde_json::from_value(Value::String(state_raw.clone())).map_err(|error| {
                rusqlite::Error::FromSqlConversionFailure(
                    state_raw.len(),
                    rusqlite::types::Type::Text,
                    Box::new(error),
                )
            })?;
            Ok(CandidateRevision {
                candidate_id: candidate_id.into(),
                idempotency_key: idempotency_key.into(),
                scope: scope_from_keys(owner, workspace, project),
                payload: CandidatePayload {
                    proposal: decode_sql_json(row.get(0)?)?,
                    state,
                    confidence: row.get(2)?,
                    importance: row.get(3)?,
                    reason: row.get(4)?,
                    source_id: row.get(5)?,
                    source_revision: row.get(6)?,
                    accepted_block_id: row.get(7)?,
                    evidence_ids: Vec::new(),
                },
                revision,
                previous_revision: row.get(8)?,
                actor_type: row.get(9)?,
                actor_id: row.get(10)?,
                source_event_id: row.get(11)?,
                change_set_id: row.get(12)?,
                content_hash: row.get(13)?,
                created_at: row.get(14)?,
            })
        },
    ).optional()?.ok_or(RepositoryError::NotFound)?;
    let mut statement = conn.prepare(
        "SELECT evidence_id FROM fm_v2_candidate_evidence WHERE owner_id=? AND candidate_id=? AND revision=? ORDER BY evidence_id",
    )?;
    candidate.payload.evidence_ids = statement
        .query_map(params![owner, candidate_id, revision], |row| row.get(0))?
        .collect::<Result<Vec<String>, _>>()?;
    Ok(candidate)
}

fn read_knowledge_identity(
    conn: &Connection,
    owner: &str,
    block_id: &str,
) -> Result<(KnowledgeIdentity, String, String, i64), RepositoryError> {
    conn.query_row(
        "SELECT subject_entity_id,predicate,claim_slot,cardinality,workspace_key,project_key,current_revision FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?",
        params![owner, block_id],
        |row| {
            let cardinality_text: String = row.get(3)?;
            let cardinality = serde_json::from_value(Value::String(cardinality_text.clone())).map_err(|error| {
                rusqlite::Error::FromSqlConversionFailure(cardinality_text.len(), rusqlite::types::Type::Text, Box::new(error))
            })?;
            Ok((KnowledgeIdentity { block_id: block_id.into(), subject_entity_id: row.get(0)?, predicate: row.get(1)?, claim_slot: row.get(2)?, cardinality }, row.get(4)?, row.get(5)?, row.get(6)?))
        },
    ).optional()?.ok_or(RepositoryError::NotFound)
}

fn read_knowledge(
    conn: &Connection,
    owner: &str,
    identity: &KnowledgeIdentity,
    revision: i64,
    workspace: &str,
    project: &str,
) -> Result<KnowledgeRevision, RepositoryError> {
    conn.query_row(
        "SELECT value_json,expected_value_type,kind,tags_json,status,confidence,trust,activation,activation_rationale,valid_from,valid_to,evidence_json,previous_revision,actor_type,actor_id,reason,source_event_id,change_set_id,content_hash,created_at FROM fm_v2_knowledge_revisions WHERE owner_id=? AND block_id=? AND revision=?",
        params![owner, identity.block_id, revision],
        |row| {
            let value_raw: String = row.get(0)?;
            let tags_raw: String = row.get(3)?;
            let status_raw: String = row.get(4)?;
            let rationale_raw: String = row.get(8)?;
            let evidence_raw: String = row.get(11)?;
            let expected_raw: Option<String> = row.get(1)?;
            let expected_value_type = expected_raw.map(|raw| serde_json::from_value(Value::String(raw.clone())).map_err(|error| rusqlite::Error::FromSqlConversionFailure(raw.len(), rusqlite::types::Type::Text, Box::new(error)))).transpose()?;
            let status = serde_json::from_value(Value::String(status_raw.clone())).map_err(|error| rusqlite::Error::FromSqlConversionFailure(status_raw.len(), rusqlite::types::Type::Text, Box::new(error)))?;
            Ok(KnowledgeRevision {
                identity: identity.clone(),
                scope: ScopeRef { owner_id: owner.into(), workspace_id: (!workspace.is_empty()).then(|| workspace.into()), project_id: (!project.is_empty()).then(|| project.into()), session_id: None },
                payload: KnowledgePayload {
                    value: decode_sql_json(value_raw)?,
                    expected_value_type,
                    kind: row.get(2)?,
                    tags: decode_sql_json(tags_raw)?,
                    status,
                    confidence: row.get(5)?,
                    trust: row.get(6)?,
                    activation: row.get(7)?,
                    activation_rationale: serde_json::from_str(&rationale_raw).unwrap_or(Value::String(rationale_raw)),
                    valid_from: row.get(9)?,
                    valid_to: row.get(10)?,
                    evidence_ids: decode_sql_json(evidence_raw)?,
                },
                revision,
                previous_revision: row.get(12)?,
                actor_type: row.get(13)?,
                actor_id: row.get(14)?,
                reason: row.get(15)?,
                source_event_id: row.get(16)?,
                change_set_id: row.get(17)?,
                content_hash: row.get(18)?,
                created_at: row.get(19)?,
            })
        },
    ).optional()?.ok_or(RepositoryError::NotFound)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ontology;

    fn setup() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "PRAGMA foreign_keys=ON; CREATE TABLE fm_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);",
        )
        .unwrap();
        ontology::install_schema(&conn).unwrap();
        conn
    }

    fn scope(owner: &str) -> ScopeRef {
        ScopeRef {
            owner_id: owner.into(),
            workspace_id: Some("ws".into()),
            project_id: None,
            session_id: None,
        }
    }

    fn actor(reason: &str) -> RevisionActor {
        RevisionActor {
            actor_type: "user".into(),
            actor_id: "alice".into(),
            reason: reason.into(),
            source_event_id: None,
        }
    }

    fn entity() -> EntityPayload {
        EntityPayload {
            entity_type: "concept".into(),
            canonical_label: "ChromaDB".into(),
            aliases: vec![],
            roles: vec![],
            tags: vec!["database".into()],
            status: "active".into(),
        }
    }

    fn knowledge(text: &str) -> KnowledgePayload {
        KnowledgePayload {
            value: TypedValue::String(text.into()),
            expected_value_type: Some(ValueType::String),
            kind: "fact".into(),
            tags: vec!["database".into()],
            status: KnowledgeStatus::Active,
            confidence: 1.0,
            trust: 1.0,
            activation: "manual".into(),
            activation_rationale: json!({"reason": "test"}),
            valid_from: None,
            valid_to: None,
            evidence_ids: vec![],
        }
    }

    fn source() -> SourcePayload {
        SourcePayload {
            source_uri: "memory://source-1".into(),
            content_hash: "source-content-hash".into(),
            source_type: "human".into(),
        }
    }

    fn evidence() -> EvidencePayload {
        EvidencePayload {
            source_id: "source-1".into(),
            source_revision: 1,
            locator_type: "chat".into(),
            locator: json!({"message_id": "message-1", "content_revision": 1, "start_byte": 0, "end_byte": 5}),
            quote: Some("first".into()),
            extraction_contract: "test-v1".into(),
            parser_version: "test".into(),
            raw_value: json!("first"),
            normalized_value: json!({"type": "string", "value": "first"}),
            validation: json!({"valid": true}),
        }
    }

    fn candidate(state: CandidateState) -> CandidatePayload {
        CandidatePayload {
            proposal: json!({"kind": "fact", "value": {"type": "string", "value": "first"}}),
            state,
            confidence: 0.9,
            importance: 0.6,
            reason: "test candidate".into(),
            source_id: Some("source-1".into()),
            source_revision: Some(1),
            accepted_block_id: None,
            evidence_ids: vec!["evidence-1".into()],
        }
    }

    #[test]
    fn stale_update_does_not_publish_and_revert_appends() {
        let mut conn = setup();
        let mut repository = V2Repository::new(&mut conn).unwrap();
        repository
            .create_entity(&scope("alice"), "entity-db", &entity(), &actor("create"))
            .unwrap();
        let identity = KnowledgeIdentity {
            block_id: "block-db".into(),
            subject_entity_id: "entity-db".into(),
            predicate: "definition".into(),
            claim_slot: "definition".into(),
            cardinality: PredicateCardinality::Single,
        };
        repository
            .create_knowledge(
                &scope("alice"),
                &identity,
                &knowledge("first"),
                &actor("create"),
            )
            .unwrap();
        repository
            .update_knowledge(
                &scope("alice"),
                "block-db",
                1,
                &knowledge("second"),
                &actor("edit"),
            )
            .unwrap();
        let error = repository
            .update_knowledge(
                &scope("alice"),
                "block-db",
                1,
                &knowledge("loser"),
                &actor("stale"),
            )
            .unwrap_err();
        assert!(matches!(
            error,
            RepositoryError::RevisionConflict { current: 2, .. }
        ));
        assert_eq!(
            repository
                .knowledge_history(&scope("alice"), "block-db")
                .unwrap()
                .len(),
            2
        );
        assert!(repository
            .knowledge_diff(&scope("alice"), "block-db", 1, 2)
            .unwrap()
            .changes
            .contains_key("value"));
        assert_eq!(
            repository
                .knowledge_blame(&scope("alice"), "block-db")
                .unwrap()["value"]
                .revision,
            2
        );
        let reverted = repository
            .revert_knowledge(&scope("alice"), "block-db", 2, 1, &actor("revert"))
            .unwrap();
        assert_eq!(reverted.revision, 3);
        assert_eq!(reverted.payload.value, TypedValue::String("first".into()));
    }

    #[test]
    fn outbox_failure_rolls_back_canonical_rows() {
        let mut conn = setup();
        conn.execute_batch(
            "CREATE TRIGGER fail_v2_outbox BEFORE INSERT ON fm_v2_outbox BEGIN SELECT RAISE(ABORT,'injected outbox failure'); END;",
        )
        .unwrap();
        let mut repository = V2Repository::new(&mut conn).unwrap();
        assert!(repository
            .create_entity(&scope("alice"), "entity-db", &entity(), &actor("create"))
            .is_err());
        assert_eq!(
            repository
                .conn
                .query_row("SELECT count(*) FROM fm_v2_entities", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
    }

    #[test]
    fn reads_do_not_cross_owner_scope() {
        let mut conn = setup();
        let mut repository = V2Repository::new(&mut conn).unwrap();
        repository
            .create_entity(&scope("alice"), "entity-db", &entity(), &actor("create"))
            .unwrap();
        assert!(matches!(
            repository.get_entity(&scope("bob"), "entity-db", None),
            Err(RepositoryError::NotFound)
        ));
    }

    #[test]
    fn source_evidence_candidate_lifecycle_is_scoped_append_only_and_cas() {
        let mut conn = setup();
        let mut repository = V2Repository::new(&mut conn).unwrap();
        repository
            .create_source(&scope("alice"), "source-1", 1, &source(), &actor("source"))
            .unwrap();
        repository
            .create_evidence(
                &scope("alice"),
                "evidence-1",
                &evidence(),
                &actor("evidence"),
            )
            .unwrap();
        repository
            .create_candidate(
                &scope("alice"),
                "candidate-1",
                "candidate-key-1",
                &candidate(CandidateState::Proposed),
                &actor("candidate"),
            )
            .unwrap();
        repository
            .update_candidate(
                &scope("alice"),
                "candidate-1",
                1,
                &candidate(CandidateState::Validating),
                &actor("validate"),
            )
            .unwrap();
        repository
            .update_candidate(
                &scope("alice"),
                "candidate-1",
                2,
                &candidate(CandidateState::AwaitingReview),
                &actor("review"),
            )
            .unwrap();
        let mut edited = candidate(CandidateState::Proposed);
        edited.proposal["value"]["value"] = json!("edited");
        repository
            .update_candidate(&scope("alice"), "candidate-1", 3, &edited, &actor("edit"))
            .unwrap();
        let stale = repository
            .update_candidate(
                &scope("alice"),
                "candidate-1",
                3,
                &candidate(CandidateState::Validating),
                &actor("stale"),
            )
            .unwrap_err();
        assert!(matches!(
            stale,
            RepositoryError::RevisionConflict { current: 4, .. }
        ));
        assert_eq!(
            repository
                .candidate_history(&scope("alice"), "candidate-1")
                .unwrap()
                .len(),
            4
        );
        assert!(matches!(
            repository.get_candidate(&scope("bob"), "candidate-1", None),
            Err(RepositoryError::NotFound)
        ));
        assert!(repository
            .create_evidence(
                &scope("bob"),
                "evidence-2",
                &evidence(),
                &actor("cross owner"),
            )
            .is_err());
    }

    #[test]
    fn candidate_activation_publishes_candidate_and_knowledge_atomically() {
        let mut conn = setup();
        let mut repository = V2Repository::new(&mut conn).unwrap();
        repository
            .create_source(&scope("alice"), "source-1", 1, &source(), &actor("source"))
            .unwrap();
        repository
            .create_evidence(
                &scope("alice"),
                "evidence-1",
                &evidence(),
                &actor("evidence"),
            )
            .unwrap();
        repository
            .create_entity(&scope("alice"), "entity-db", &entity(), &actor("entity"))
            .unwrap();
        repository
            .create_candidate(
                &scope("alice"),
                "candidate-1",
                "candidate-key-1",
                &candidate(CandidateState::Proposed),
                &actor("propose"),
            )
            .unwrap();
        repository
            .update_candidate(
                &scope("alice"),
                "candidate-1",
                1,
                &candidate(CandidateState::Validating),
                &actor("validate"),
            )
            .unwrap();
        repository
            .update_candidate(
                &scope("alice"),
                "candidate-1",
                2,
                &candidate(CandidateState::AwaitingReview),
                &actor("review"),
            )
            .unwrap();
        let identity = KnowledgeIdentity {
            block_id: "block-db".into(),
            subject_entity_id: "entity-db".into(),
            predicate: "definition".into(),
            claim_slot: "definition".into(),
            cardinality: PredicateCardinality::Single,
        };
        let mut accepted = knowledge("accepted");
        accepted.evidence_ids = vec!["evidence-1".into()];
        let (activated, knowledge) = repository
            .activate_candidate_to_knowledge(
                &scope("alice"),
                "candidate-1",
                3,
                &identity,
                &accepted,
                &actor("approve"),
            )
            .unwrap();
        assert_eq!(activated.payload.state, CandidateState::Activated);
        assert_eq!(
            activated.payload.accepted_block_id.as_deref(),
            Some("block-db")
        );
        assert_eq!(knowledge.revision, 1);
        assert_eq!(activated.change_set_id, knowledge.change_set_id);
        assert_eq!(
            repository
                .conn
                .query_row(
                    "SELECT count(*) FROM fm_v2_outbox WHERE owner_id='alice' AND change_set_id=?",
                    [&knowledge.change_set_id],
                    |row| row.get::<_, i64>(0),
                )
                .unwrap(),
            2
        );
    }

    #[test]
    fn candidate_activation_outbox_failure_leaves_no_block_or_candidate_head() {
        let mut conn = setup();
        let mut repository = V2Repository::new(&mut conn).unwrap();
        repository
            .create_source(&scope("alice"), "source-1", 1, &source(), &actor("source"))
            .unwrap();
        repository
            .create_evidence(
                &scope("alice"),
                "evidence-1",
                &evidence(),
                &actor("evidence"),
            )
            .unwrap();
        repository
            .create_entity(&scope("alice"), "entity-db", &entity(), &actor("entity"))
            .unwrap();
        repository
            .create_candidate(
                &scope("alice"),
                "candidate-1",
                "candidate-key-1",
                &candidate(CandidateState::Proposed),
                &actor("propose"),
            )
            .unwrap();
        repository
            .update_candidate(
                &scope("alice"),
                "candidate-1",
                1,
                &candidate(CandidateState::Validating),
                &actor("validate"),
            )
            .unwrap();
        repository
            .update_candidate(
                &scope("alice"),
                "candidate-1",
                2,
                &candidate(CandidateState::AwaitingReview),
                &actor("review"),
            )
            .unwrap();
        repository
            .conn
            .execute_batch(
                "CREATE TRIGGER fail_candidate_activation_outbox BEFORE INSERT ON fm_v2_outbox WHEN NEW.kind='candidate_revision' AND json_extract(NEW.payload_json,'$.accepted_block_id') IS NOT NULL BEGIN SELECT RAISE(ABORT,'injected activation outbox failure'); END;",
            )
            .unwrap();
        let identity = KnowledgeIdentity {
            block_id: "block-db".into(),
            subject_entity_id: "entity-db".into(),
            predicate: "definition".into(),
            claim_slot: "definition".into(),
            cardinality: PredicateCardinality::Single,
        };
        assert!(repository
            .activate_candidate_to_knowledge(
                &scope("alice"),
                "candidate-1",
                3,
                &identity,
                &knowledge("accepted"),
                &actor("approve"),
            )
            .is_err());
        assert_eq!(
            repository
                .get_candidate(&scope("alice"), "candidate-1", None)
                .unwrap()
                .revision,
            3
        );
        assert!(matches!(
            repository.get_knowledge(&scope("alice"), "block-db", None),
            Err(RepositoryError::NotFound)
        ));
    }

    #[test]
    fn job_attempts_are_fenced_and_history_is_append_only() {
        let mut conn = setup();
        let mut repository = V2Repository::new(&mut conn).unwrap();
        repository
            .create_job(
                &scope("alice"),
                "job-1",
                "extract",
                "job-key-1",
                "input-hash",
                &actor("queue"),
            )
            .unwrap();
        let (_, leased) = repository
            .claim_job(
                &scope("alice"),
                "job-1",
                "worker-1",
                "2999-01-01T00:00:00Z",
                &actor("claim"),
            )
            .unwrap();
        assert!(repository
            .start_job_attempt(
                &scope("alice"),
                "job-1",
                &leased.attempt_id,
                "worker-2",
                leased.lease_epoch,
                &actor("stale worker"),
            )
            .is_err());
        repository
            .start_job_attempt(
                &scope("alice"),
                "job-1",
                &leased.attempt_id,
                "worker-1",
                leased.lease_epoch,
                &actor("start"),
            )
            .unwrap();
        let completed = repository
            .finish_job(
                &scope("alice"),
                "job-1",
                &leased.attempt_id,
                "worker-1",
                leased.lease_epoch,
                JobState::Succeeded,
                Some(&json!({"count": 1})),
                None,
                &actor("finish"),
            )
            .unwrap();
        assert_eq!(completed.state, JobState::Succeeded);
        assert_eq!(
            repository
                .job_history(&scope("alice"), "job-1")
                .unwrap()
                .iter()
                .map(|event| event.state)
                .collect::<Vec<_>>(),
            vec![
                JobState::Queued,
                JobState::Active,
                JobState::Active,
                JobState::Succeeded
            ]
        );
        assert!(repository
            .finish_job(
                &scope("alice"),
                "job-1",
                &leased.attempt_id,
                "worker-1",
                leased.lease_epoch,
                JobState::Succeeded,
                None,
                None,
                &actor("retry stale"),
            )
            .is_err());
    }

    #[test]
    fn conflicts_preserve_the_head_until_atomic_resolution() {
        let mut conn = setup();
        let mut repository = V2Repository::new(&mut conn).unwrap();
        repository
            .create_entity(&scope("alice"), "entity-db", &entity(), &actor("create"))
            .unwrap();
        let identity = KnowledgeIdentity {
            block_id: "block-db".into(),
            subject_entity_id: "entity-db".into(),
            predicate: "definition".into(),
            claim_slot: "definition".into(),
            cardinality: PredicateCardinality::Single,
        };
        repository
            .create_knowledge(
                &scope("alice"),
                &identity,
                &knowledge("accepted"),
                &actor("create"),
            )
            .unwrap();
        repository
            .record_conflict(
                &scope("alice"),
                "conflict-1",
                "block-db",
                1,
                &json!({"value": {"type": "string", "value": "competing"}}),
                "sources disagree",
                &actor("record conflict"),
            )
            .unwrap();
        assert_eq!(
            repository
                .get_knowledge(&scope("alice"), "block-db", None)
                .unwrap()
                .revision,
            1
        );
        let resolved = repository
            .resolve_conflict(
                &scope("alice"),
                "conflict-1",
                1,
                &knowledge("resolved"),
                "human chose the supported value",
                &actor("resolve"),
            )
            .unwrap();
        assert_eq!(resolved.revision, 2);
        assert_eq!(
            repository
                .conflict_history(&scope("alice"), "conflict-1")
                .unwrap()
                .iter()
                .map(|event| event.state.as_str())
                .collect::<Vec<_>>(),
            vec!["open", "resolved"]
        );
    }

    #[test]
    fn authorized_source_erasure_is_atomic_and_owner_scoped() {
        let mut conn = setup();
        let mut repository = V2Repository::new(&mut conn).unwrap();
        for owner in ["alice", "bob"] {
            repository
                .create_source(&scope(owner), "source-1", 1, &source(), &actor("source"))
                .unwrap();
            repository
                .create_evidence(&scope(owner), "evidence-1", &evidence(), &actor("evidence"))
                .unwrap();
            repository
                .create_entity(&scope(owner), "entity-db", &entity(), &actor("entity"))
                .unwrap();
            let identity = KnowledgeIdentity {
                block_id: "block-db".into(),
                subject_entity_id: "entity-db".into(),
                predicate: "definition".into(),
                claim_slot: "definition".into(),
                cardinality: PredicateCardinality::Single,
            };
            let mut payload = knowledge("private value");
            payload.evidence_ids = vec!["evidence-1".into()];
            repository
                .create_knowledge(&scope(owner), &identity, &payload, &actor("knowledge"))
                .unwrap();
            repository
                .create_candidate(
                    &scope(owner),
                    "candidate-1",
                    "candidate-key-1",
                    &candidate(CandidateState::Proposed),
                    &actor("candidate"),
                )
                .unwrap();
        }
        let result = repository
            .erase_source(
                &scope("alice"),
                "source-1",
                &actor("authorized source forget"),
            )
            .unwrap();
        assert_eq!(result.erased_counts["sources"], 1);
        assert_eq!(result.erased_counts["knowledge_blocks"], 1);
        assert_eq!(result.erased_counts["candidates"], 1);
        assert!(matches!(
            repository.get_source(&scope("alice"), "source-1", 1),
            Err(RepositoryError::NotFound)
        ));
        assert_eq!(
            repository
                .get_source(&scope("bob"), "source-1", 1)
                .unwrap()
                .payload
                .content_hash,
            "source-content-hash"
        );
        assert_eq!(
            repository
                .conn
                .query_row(
                    "SELECT count(*) FROM fm_v2_privacy_erasure_context",
                    [],
                    |row| row.get::<_, i64>(0),
                )
                .unwrap(),
            0
        );
        assert!(repository
            .conn
            .execute(
                "DELETE FROM fm_v2_knowledge_revisions WHERE owner_id='bob'",
                [],
            )
            .unwrap_err()
            .to_string()
            .contains("append-only"));
    }

    #[test]
    fn privacy_erasure_failure_rolls_back_payload_and_authorization() {
        let mut conn = setup();
        let mut repository = V2Repository::new(&mut conn).unwrap();
        repository
            .create_source(&scope("alice"), "source-1", 1, &source(), &actor("source"))
            .unwrap();
        repository
            .conn
            .execute_batch(
                "CREATE TRIGGER fail_erasure_audit BEFORE INSERT ON fm_v2_privacy_erasure_tombstones BEGIN SELECT RAISE(ABORT,'injected erasure audit failure'); END;",
            )
            .unwrap();
        assert!(repository
            .erase_source(
                &scope("alice"),
                "source-1",
                &actor("authorized source forget"),
            )
            .is_err());
        assert!(repository
            .get_source(&scope("alice"), "source-1", 1)
            .is_ok());
        assert_eq!(
            repository
                .conn
                .query_row(
                    "SELECT count(*) FROM fm_v2_privacy_erasure_context",
                    [],
                    |row| row.get::<_, i64>(0),
                )
                .unwrap(),
            0
        );
    }

    #[test]
    fn concurrent_expected_revision_writers_publish_one_head() {
        use std::sync::{Arc, Barrier};

        let path = std::env::temp_dir().join(format!(
            "fm-v2-cas-{}-{}.db",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        {
            let mut conn = Connection::open(&path).unwrap();
            conn.execute_batch(
                "PRAGMA foreign_keys=ON; CREATE TABLE fm_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);",
            )
            .unwrap();
            ontology::install_schema(&conn).unwrap();
            let mut repository = V2Repository::new(&mut conn).unwrap();
            repository
                .create_entity(&scope("alice"), "entity-db", &entity(), &actor("create"))
                .unwrap();
            repository
                .create_knowledge(
                    &scope("alice"),
                    &KnowledgeIdentity {
                        block_id: "block-db".into(),
                        subject_entity_id: "entity-db".into(),
                        predicate: "definition".into(),
                        claim_slot: "definition".into(),
                        cardinality: PredicateCardinality::Single,
                    },
                    &knowledge("first"),
                    &actor("create"),
                )
                .unwrap();
        }
        let barrier = Arc::new(Barrier::new(2));
        let handles = ["second-a", "second-b"].map(|text| {
            let path = path.clone();
            let barrier = Arc::clone(&barrier);
            std::thread::spawn(move || {
                let mut conn = Connection::open(path).unwrap();
                conn.busy_timeout(std::time::Duration::from_secs(5))
                    .unwrap();
                let mut repository = V2Repository::new(&mut conn).unwrap();
                barrier.wait();
                repository.update_knowledge(
                    &scope("alice"),
                    "block-db",
                    1,
                    &knowledge(text),
                    &actor(text),
                )
            })
        });
        let outcomes = handles.map(|handle| handle.join().unwrap());
        assert_eq!(outcomes.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(
            outcomes
                .iter()
                .filter(|result| matches!(
                    result,
                    Err(RepositoryError::RevisionConflict { current: 2, .. })
                ))
                .count(),
            1
        );
        let mut conn = Connection::open(&path).unwrap();
        let repository = V2Repository::new(&mut conn).unwrap();
        assert_eq!(
            repository
                .knowledge_history(&scope("alice"), "block-db")
                .unwrap()
                .len(),
            2
        );
        drop(repository);
        drop(conn);
        let _ = std::fs::remove_file(path);
    }
}
