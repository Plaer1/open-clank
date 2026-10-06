//! Versioned Frankenmemory ontology and additive v2 schema.
//!
//! This module is deliberately independent of the legacy prose/category
//! tables.  The v2 tables are a canonical, tenant-scoped foundation; legacy
//! callers remain untouched until the migration and parity gates pass.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

pub const CONTRACT_VERSION: &str = "frankenmemory.v2";

#[derive(Debug, Clone, Copy, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
pub enum V2Operation {
    #[serde(rename = "scope.resolve")]
    ScopeResolve,
    #[serde(rename = "entity.create")]
    EntityCreate,
    #[serde(rename = "entity.get")]
    EntityGet,
    #[serde(rename = "entity.update")]
    EntityUpdate,
    #[serde(rename = "entity.history")]
    EntityHistory,
    #[serde(rename = "knowledge.create")]
    KnowledgeCreate,
    #[serde(rename = "knowledge.get")]
    KnowledgeGet,
    #[serde(rename = "knowledge.list")]
    KnowledgeList,
    #[serde(rename = "knowledge.questions")]
    KnowledgeQuestions,
    #[serde(rename = "knowledge.update")]
    KnowledgeUpdate,
    #[serde(rename = "knowledge.history")]
    KnowledgeHistory,
    #[serde(rename = "knowledge.diff")]
    KnowledgeDiff,
    #[serde(rename = "knowledge.blame")]
    KnowledgeBlame,
    #[serde(rename = "knowledge.revert")]
    KnowledgeRevert,
    #[serde(rename = "conflict.record")]
    ConflictRecord,
    #[serde(rename = "conflict.resolve")]
    ConflictResolve,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScopeRef {
    pub owner_id: String,
    pub workspace_id: Option<String>,
    pub project_id: Option<String>,
    pub session_id: Option<String>,
}

impl ScopeRef {
    pub fn validate(&self) -> Result<(), String> {
        if self.owner_id.trim().is_empty() {
            return Err("owner_id is required".into());
        }
        if self
            .workspace_id
            .as_deref()
            .is_some_and(|v| v.trim().is_empty())
            || self
                .project_id
                .as_deref()
                .is_some_and(|v| v.trim().is_empty())
        {
            return Err("scope identifiers cannot be blank".into());
        }
        if self.project_id.is_some() && self.workspace_id.is_none() {
            return Err("project_id requires workspace_id".into());
        }
        Ok(())
    }

    pub fn storage_keys(&self) -> (String, String, String) {
        (
            self.owner_id.clone(),
            self.workspace_id.clone().unwrap_or_default(),
            self.project_id.clone().unwrap_or_default(),
        )
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq)]
#[serde(
    tag = "type",
    content = "value",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum TypedValue {
    Null,
    String(String),
    Number(f64),
    Boolean(bool),
    Time(String),
    Duration(String),
    Uri(String),
    EntityRef { entity_id: String },
    Object(serde_json::Map<String, serde_json::Value>),
    List(Vec<serde_json::Value>),
}

impl TypedValue {
    pub fn validate(&self) -> Result<(), String> {
        if let Self::EntityRef { entity_id } = self {
            if entity_id.is_empty() {
                return Err("entity_ref.entity_id is required".into());
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ValueType {
    Null,
    String,
    Number,
    Boolean,
    Time,
    Duration,
    Uri,
    EntityRef,
    Object,
    List,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PredicateCardinality {
    Single,
    Set,
    OrderedMany,
    TimeScoped,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeStatus {
    Open,
    Active,
    Superseded,
    Retracted,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum JobState {
    Queued,
    Active,
    AwaitingReview,
    RetryWait,
    Succeeded,
    FailedTerminal,
    Cancelled,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum AttemptState {
    Leased,
    Running,
    Succeeded,
    FailedRetryable,
    FailedTerminal,
    Cancelled,
    Fenced,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CandidateState {
    Proposed,
    Validating,
    AwaitingReview,
    EligibleAuto,
    Duplicate,
    Corroborating,
    Conflicted,
    Quarantined,
    Rejected,
    Cancelled,
    Activated,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
pub struct EffectiveScopeSet {
    pub requested: ScopeRef,
    pub exact: Vec<ScopeRef>,
}

impl EffectiveScopeSet {
    pub fn for_request(requested: ScopeRef, owner_only: bool) -> Result<Self, String> {
        requested.validate()?;
        let mut exact = vec![ScopeRef {
            owner_id: requested.owner_id.clone(),
            workspace_id: None,
            project_id: None,
            session_id: None,
        }];
        if !owner_only {
            if let Some(workspace_id) = requested.workspace_id.clone() {
                exact.push(ScopeRef {
                    owner_id: requested.owner_id.clone(),
                    workspace_id: Some(workspace_id.clone()),
                    project_id: None,
                    session_id: None,
                });
                if let Some(project_id) = requested.project_id.clone() {
                    exact.push(ScopeRef {
                        owner_id: requested.owner_id.clone(),
                        workspace_id: Some(workspace_id),
                        project_id: Some(project_id),
                        session_id: None,
                    });
                }
            }
        }
        Ok(Self { requested, exact })
    }
}

/// Test fixtures initialize the same current schema as the production opener.
#[cfg(test)]
pub fn install_schema(conn: &rusqlite::Connection) -> Result<(), rusqlite::Error> {
    conn.execute_batch(include_str!("store/current-schema.sql"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    #[test]
    fn scope_set_is_specific_and_owner_bound() {
        let set = EffectiveScopeSet::for_request(
            ScopeRef {
                owner_id: "e".into(),
                workspace_id: Some("w".into()),
                project_id: Some("p".into()),
                session_id: Some("session-1".into()),
            },
            false,
        )
        .unwrap();
        assert_eq!(set.exact.len(), 3);
        assert!(set.exact.iter().all(|scope| scope.owner_id == "e"));
        assert!(set.exact.iter().all(|scope| scope.session_id.is_none()));
        assert_eq!(set.requested.session_id.as_deref(), Some("session-1"));
    }

    #[test]
    fn project_requires_workspace() {
        let error = ScopeRef {
            owner_id: "e".into(),
            workspace_id: None,
            project_id: Some("p".into()),
            session_id: None,
        }
        .validate()
        .unwrap_err();
        assert!(error.contains("workspace_id"));
    }

    #[test]
    fn canonical_fixtures_match_rust_scope_and_typed_value_contracts() {
        let fixtures: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../../contracts/frankenmemory/v2/contract-fixtures.json"
        ))
        .unwrap();
        for case in fixtures["operation"].as_array().unwrap() {
            let valid = serde_json::from_value::<V2Operation>(case["value"].clone()).is_ok();
            assert_eq!(valid, case["valid"].as_bool().unwrap(), "{}", case["name"]);
        }
        for case in fixtures["scope"].as_array().unwrap() {
            let valid = serde_json::from_value::<ScopeRef>(case["value"].clone())
                .map_err(|error| error.to_string())
                .and_then(|scope| scope.validate())
                .is_ok();
            assert_eq!(valid, case["valid"].as_bool().unwrap(), "{}", case["name"]);
        }
        for case in fixtures["typed_value"].as_array().unwrap() {
            let valid = serde_json::from_value::<TypedValue>(case["value"].clone())
                .map_err(|error| error.to_string())
                .and_then(|value| value.validate())
                .is_ok();
            assert_eq!(valid, case["valid"].as_bool().unwrap(), "{}", case["name"]);
        }
    }

    #[test]
    fn v2_schema_enables_composite_owner_foreign_keys() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        conn.execute_batch("CREATE TABLE fm_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);")
            .unwrap();
        install_schema(&conn).unwrap();
        let fk: i64 = conn
            .query_row("PRAGMA foreign_keys", [], |r| r.get(0))
            .unwrap();
        assert_eq!(fk, 1);
        let tables: i64 = conn.query_row(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='fm_v2_knowledge_blocks'",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(tables, 1);
        for table in [
            "fm_v2_derived_generations",
            "fm_v2_index_pointers",
            "fm_v2_index_publications",
            "fm_v2_chunk_embeddings",
            "fm_v2_trust_assignments",
            "fm_v2_job_manifests",
        ] {
            let exists: i64 = conn
                .query_row(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?1",
                    [table],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(exists, 1, "{table}");
        }
        for legacy in ["fm_v2_index_generations", "fm_v2_index_heads"] {
            let exists: i64 = conn
                .query_row(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?1",
                    [legacy],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(exists, 0, "new databases must not create {legacy}");
        }
        let trust_columns: Vec<String> = conn
            .prepare("PRAGMA table_info(fm_v2_policy_trust)")
            .unwrap()
            .query_map([], |row| row.get(1))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap();
        for column in [
            "trust_id",
            "revision",
            "created_by",
            "created_reason",
            "revoked_by",
            "revoked_reason",
            "manifest_hash",
            "capability_hash",
        ] {
            assert!(
                trust_columns.iter().any(|value| value == column),
                "{column}"
            );
        }
        let profile_columns: Vec<String> = conn
            .prepare("PRAGMA table_info(fm_v2_policy_profiles)")
            .unwrap()
            .query_map([], |row| row.get(1))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap();
        assert!(profile_columns
            .iter()
            .any(|value| value == "credential_hash"));
        assert!(profile_columns
            .iter()
            .any(|value| value == "activation_revision"));
    }


}
