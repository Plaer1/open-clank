//! Versioned Frankenmemory ontology and additive v2 schema.
//!
//! This module is deliberately independent of the legacy prose/category
//! tables.  The v2 tables are a canonical, tenant-scoped foundation; legacy
//! callers remain untouched until the migration and parity gates pass.

use chrono::Utc;
use rusqlite::{Connection, Error as SqliteError};
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

/// Install only additive v2 tables.  This is called inside the numbered
/// SqliteStore migration transaction; no legacy row is read or rewritten.
pub fn install_schema(conn: &Connection) -> Result<(), SqliteError> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS fm_v2_predicates (
            predicate TEXT PRIMARY KEY,
            value_type TEXT NOT NULL,
            cardinality TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            allow_unknown INTEGER NOT NULL DEFAULT 0 CHECK (allow_unknown IN (0,1))
        );

        CREATE TABLE IF NOT EXISTS fm_v2_change_sets (
            owner_id TEXT NOT NULL,
            change_set_id TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_event_id TEXT,
            contract_version TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, change_set_id)
        );

        CREATE TABLE IF NOT EXISTS fm_v2_entities (
            owner_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            created_change_set_id TEXT NOT NULL,
            current_revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, entity_id),
            FOREIGN KEY (owner_id, created_change_set_id)
                REFERENCES fm_v2_change_sets(owner_id, change_set_id),
            CHECK (project_key = '' OR workspace_key <> '')
        );

        CREATE TABLE IF NOT EXISTS fm_v2_entity_revisions (
            owner_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            payload TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            change_set_id TEXT NOT NULL,
            previous_revision INTEGER,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, entity_id, revision),
            FOREIGN KEY (owner_id, entity_id)
                REFERENCES fm_v2_entities(owner_id, entity_id),
            FOREIGN KEY (owner_id, change_set_id)
                REFERENCES fm_v2_change_sets(owner_id, change_set_id),
            CHECK (revision > 0)
        );

        CREATE TABLE IF NOT EXISTS fm_v2_knowledge_blocks (
            owner_id TEXT NOT NULL,
            block_id TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            subject_entity_id TEXT NOT NULL,
            predicate TEXT NOT NULL,
            claim_slot TEXT NOT NULL,
            cardinality TEXT NOT NULL,
            created_change_set_id TEXT NOT NULL,
            current_revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, block_id),
            UNIQUE (owner_id, workspace_key, project_key, subject_entity_id, predicate, claim_slot),
            FOREIGN KEY (owner_id, subject_entity_id)
                REFERENCES fm_v2_entities(owner_id, entity_id),
            FOREIGN KEY (owner_id, created_change_set_id)
                REFERENCES fm_v2_change_sets(owner_id, change_set_id),
            CHECK (project_key = '' OR workspace_key <> '')
        );

        CREATE TABLE IF NOT EXISTS fm_v2_knowledge_revisions (
            owner_id TEXT NOT NULL,
            block_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            value_type TEXT NOT NULL,
            value_json TEXT NOT NULL,
            expected_value_type TEXT,
            kind TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            confidence REAL NOT NULL,
            trust REAL NOT NULL,
            activation TEXT NOT NULL,
            activation_rationale TEXT NOT NULL DEFAULT '',
            valid_from TEXT,
            valid_to TEXT,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            content_hash TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_event_id TEXT,
            change_set_id TEXT NOT NULL,
            previous_revision INTEGER,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, block_id, revision),
            FOREIGN KEY (owner_id, block_id)
                REFERENCES fm_v2_knowledge_blocks(owner_id, block_id),
            FOREIGN KEY (owner_id, change_set_id)
                REFERENCES fm_v2_change_sets(owner_id, change_set_id),
            CHECK (revision > 0),
            CHECK (confidence >= 0 AND confidence <= 1),
            CHECK (trust >= 0 AND trust <= 1),
            CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from)
        );

        CREATE TABLE IF NOT EXISTS fm_v2_conflicts (
            owner_id TEXT NOT NULL,
            conflict_id TEXT NOT NULL,
            block_id TEXT NOT NULL,
            current_revision INTEGER NOT NULL,
            competing_payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'open',
            rationale TEXT NOT NULL DEFAULT '',
            created_change_set_id TEXT NOT NULL,
            resolved_change_set_id TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            PRIMARY KEY (owner_id, conflict_id),
            FOREIGN KEY (owner_id, block_id)
                REFERENCES fm_v2_knowledge_blocks(owner_id, block_id),
            FOREIGN KEY (owner_id, created_change_set_id)
                REFERENCES fm_v2_change_sets(owner_id, change_set_id),
            FOREIGN KEY (owner_id, resolved_change_set_id)
                REFERENCES fm_v2_change_sets(owner_id, change_set_id),
            CHECK (state IN ('open','resolved','dismissed'))
        );

        CREATE TABLE IF NOT EXISTS fm_v2_sources (
            owner_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            source_uri TEXT NOT NULL,
            source_revision INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            source_type TEXT NOT NULL,
            forget_state TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, source_id, source_revision),
            CHECK (source_revision > 0),
            CHECK (project_key = '' OR workspace_key <> ''),
            CHECK (forget_state IN ('active','forget_requested','forgotten'))
        );

        CREATE TABLE IF NOT EXISTS fm_v2_evidence (
            owner_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_revision INTEGER NOT NULL,
            locator_type TEXT NOT NULL,
            locator_json TEXT NOT NULL,
            quote TEXT,
            extraction_contract TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            raw_value_json TEXT NOT NULL,
            normalized_value_json TEXT NOT NULL,
            validation_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, evidence_id),
            FOREIGN KEY (owner_id, source_id, source_revision)
                REFERENCES fm_v2_sources(owner_id, source_id, source_revision)
        );

        CREATE TABLE IF NOT EXISTS fm_v2_documents (
            owner_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_revision INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            parser_version TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, document_id),
            FOREIGN KEY (owner_id, source_id, source_revision)
                REFERENCES fm_v2_sources(owner_id, source_id, source_revision)
        );

        CREATE TABLE IF NOT EXISTS fm_v2_chunks (
            owner_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            locator_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, chunk_id),
            UNIQUE (owner_id, document_id, ordinal),
            FOREIGN KEY (owner_id, document_id)
                REFERENCES fm_v2_documents(owner_id, document_id)
        );

        CREATE TABLE IF NOT EXISTS fm_v2_jobs (
            owner_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL,
            state TEXT NOT NULL,
            current_attempt_id TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            input_hash TEXT NOT NULL,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, job_id),
            UNIQUE (owner_id, idempotency_key),
            CHECK (state IN ('queued','active','awaiting_review','retry_wait','succeeded','failed_terminal','cancelled')),
            CHECK (project_key = '' OR workspace_key <> '')
        );

        CREATE TABLE IF NOT EXISTS fm_v2_attempts (
            owner_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            state TEXT NOT NULL,
            lease_epoch INTEGER NOT NULL,
            lease_owner TEXT,
            lease_expires_at TEXT,
            heartbeat_at TEXT,
            progress_watermark TEXT,
            error_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, attempt_id),
            UNIQUE (owner_id, job_id, attempt_number),
            FOREIGN KEY (owner_id, job_id)
                REFERENCES fm_v2_jobs(owner_id, job_id),
            CHECK (state IN ('leased','running','succeeded','failed_retryable','failed_terminal','cancelled','fenced')),
            CHECK (attempt_number > 0)
        );

        CREATE TABLE IF NOT EXISTS fm_v2_projects (
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, project_id)
        );

        CREATE TABLE IF NOT EXISTS fm_v2_project_locators (
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            locator_revision INTEGER NOT NULL,
            canonical_root TEXT NOT NULL,
            root_identity TEXT NOT NULL,
            git_identity TEXT NOT NULL,
            worktree_identity TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            authorized_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, project_id, locator_revision),
            FOREIGN KEY (owner_id, project_id)
                REFERENCES fm_v2_projects(owner_id, project_id),
            CHECK (locator_revision > 0),
            CHECK (active IN (0,1))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_fm_v2_project_active_root
            ON fm_v2_project_locators(owner_id, canonical_root)
            WHERE active=1;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fm_v2_project_active_locator
            ON fm_v2_project_locators(owner_id, project_id)
            WHERE active=1;

        CREATE TABLE IF NOT EXISTS fm_v2_policy_projections (
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            contract_path TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            source_revision INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'never_activated',
            updated_at TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT 'global',
            canonical_root TEXT NOT NULL DEFAULT '',
            root_identity TEXT NOT NULL DEFAULT '',
            git_identity TEXT NOT NULL DEFAULT '',
            worktree_identity TEXT NOT NULL DEFAULT '',
            activation_revision INTEGER NOT NULL DEFAULT 0,
            activated_by TEXT,
            activated_at TEXT,
            PRIMARY KEY (owner_id, project_id),
            CHECK (state IN ('never_activated','active','drifted','deactivated'))
        );

        CREATE TABLE IF NOT EXISTS fm_v2_spells (
            owner_id TEXT NOT NULL,
            spell_id TEXT NOT NULL,
            project_id TEXT,
            title TEXT NOT NULL,
            suggestion_json TEXT NOT NULL,
            source_evidence_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'proposed',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT 'global',
            path_scope TEXT NOT NULL DEFAULT '**/*',
            revision INTEGER NOT NULL DEFAULT 1,
            review_state TEXT NOT NULL DEFAULT 'proposed',
            lifecycle TEXT NOT NULL DEFAULT 'proposed',
            reviewed_by TEXT,
            reviewed_at TEXT,
            expires_at TEXT,
            rationale TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.5,
            promoted_contract_hash TEXT,
            promoted_transition_id TEXT,
            PRIMARY KEY (owner_id, spell_id),
            CHECK (status IN ('proposed','dismissed','expired','edited','promoted'))
        );

        CREATE TABLE IF NOT EXISTS fm_v2_policy_transitions (
            owner_id TEXT NOT NULL,
            transition_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            contract_path TEXT,
            old_contract_hash TEXT,
            new_contract_hash TEXT,
            old_bytes BLOB,
            new_bytes BLOB,
            spell_id TEXT,
            spell_revision INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT,
            PRIMARY KEY (owner_id, transition_id),
            CHECK (phase IN ('prepared','blessing_consumed','file_published','activation_advanced','validated','projection_enqueued','spell_linked','committed','rollback_required','rolled_back'))
        );

        CREATE TABLE IF NOT EXISTS fm_v2_outbox (
            owner_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            change_set_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            source_revision INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            published_at TEXT,
            PRIMARY KEY (owner_id, event_id),
            FOREIGN KEY (owner_id, change_set_id)
                REFERENCES fm_v2_change_sets(owner_id, change_set_id),
            CHECK (state IN ('pending','leased','published','failed'))
        );

        CREATE TABLE IF NOT EXISTS fm_v2_migration_runs (
            migration_run_id TEXT PRIMARY KEY,
            source_database_id TEXT NOT NULL,
            source_schema_version INTEGER NOT NULL,
            source_high_water_mark TEXT NOT NULL,
            conversion_version TEXT NOT NULL,
            manifest_hash TEXT NOT NULL,
            outbox_high_water_mark INTEGER NOT NULL DEFAULT 0,
            backup_path TEXT,
            backup_hash TEXT,
            state TEXT NOT NULL DEFAULT 'planned',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (state IN ('planned','applying','paused','applied','failed','rolled_back'))
        );

        CREATE TABLE IF NOT EXISTS fm_v2_migration_plan (
            migration_run_id TEXT NOT NULL,
            plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_table TEXT NOT NULL,
            source_key TEXT NOT NULL,
            owner_id TEXT,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            source_fingerprint TEXT NOT NULL,
            planned_target_ids TEXT NOT NULL,
            conversion_rule TEXT NOT NULL,
            classification TEXT NOT NULL,
            activation_treatment TEXT NOT NULL,
            exception_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (migration_run_id, source_table, source_key),
            FOREIGN KEY (migration_run_id) REFERENCES fm_v2_migration_runs(migration_run_id),
            CHECK (project_key = '' OR workspace_key <> '')
        );

        CREATE TABLE IF NOT EXISTS fm_v2_migration_events (
            migration_run_id TEXT NOT NULL,
            event_id TEXT PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            phase TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            checkpoint TEXT NOT NULL,
            target_ids TEXT NOT NULL,
            apply_result TEXT NOT NULL,
            validation_result TEXT NOT NULL,
            exception_id TEXT,
            rollback_marker TEXT,
            actor TEXT NOT NULL,
            previous_event_hash TEXT,
            event_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (migration_run_id, sequence),
            FOREIGN KEY (migration_run_id) REFERENCES fm_v2_migration_runs(migration_run_id),
            FOREIGN KEY (plan_id) REFERENCES fm_v2_migration_plan(plan_id)
        );

        CREATE INDEX IF NOT EXISTS idx_fm_v2_blocks_scope
            ON fm_v2_knowledge_blocks(owner_id, workspace_key, project_key, subject_entity_id);
        CREATE INDEX IF NOT EXISTS idx_fm_v2_revisions_current
            ON fm_v2_knowledge_revisions(owner_id, block_id, revision);
        CREATE INDEX IF NOT EXISTS idx_fm_v2_sources_hash
            ON fm_v2_sources(owner_id, content_hash);
        CREATE INDEX IF NOT EXISTS idx_fm_v2_jobs_state
            ON fm_v2_jobs(owner_id, state, updated_at);
        CREATE INDEX IF NOT EXISTS idx_fm_v2_outbox_state
            ON fm_v2_outbox(owner_id, state, created_at);
        CREATE INDEX IF NOT EXISTS idx_fm_v2_migration_plan_run
            ON fm_v2_migration_plan(migration_run_id, plan_id);
        CREATE INDEX IF NOT EXISTS idx_fm_v2_migration_events_run
            ON fm_v2_migration_events(migration_run_id, sequence);

        INSERT OR IGNORE INTO fm_v2_predicates(predicate, value_type, cardinality, version)
            VALUES ('name','string','single',1), ('definition','string','single',1),
                   ('prefers','string','set',1), ('located_at','string','single',1);

        CREATE VIEW IF NOT EXISTS fm_v2_current_knowledge AS
            SELECT b.owner_id, b.block_id, b.workspace_key, b.project_key,
                   b.subject_entity_id, b.predicate, b.claim_slot,
                   b.current_revision, r.value_type, r.value_json,
                   r.expected_value_type, r.kind, r.tags_json, r.status,
                   r.confidence, r.trust, r.activation, r.activation_rationale,
                   r.valid_from, r.valid_to, r.evidence_json, r.content_hash,
                   r.created_at AS revision_created_at
              FROM fm_v2_knowledge_blocks b
              JOIN fm_v2_knowledge_revisions r
                ON r.owner_id=b.owner_id AND r.block_id=b.block_id
               AND r.revision=b.current_revision;
        "#,
    )?;
    // Existing v2.0 databases were stamped before source scope columns were
    // added. Keep the migration additive and idempotent for those databases.
    ensure_schema_extensions(conn)?;
    conn.execute(
        "INSERT OR IGNORE INTO fm_meta(key,value) VALUES (?1,?2)",
        rusqlite::params!["canonical_contract_version", CONTRACT_VERSION],
    )?;
    conn.execute(
        "INSERT OR IGNORE INTO fm_meta(key,value) VALUES (?1,?2)",
        rusqlite::params!["v2_schema_installed_at", Utc::now().to_rfc3339()],
    )?;
    Ok(())
}

/// Apply additive columns introduced after the initial v2 schema stamp. This
/// is safe to call on every open, including databases already at version 11.
pub fn ensure_schema_extensions(conn: &Connection) -> Result<(), SqliteError> {
    ensure_column(
        conn,
        "fm_v2_sources",
        "workspace_key",
        "TEXT NOT NULL DEFAULT ''",
    )?;
    ensure_column(
        conn,
        "fm_v2_sources",
        "project_key",
        "TEXT NOT NULL DEFAULT ''",
    )?;
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS fm_v2_trust_assignments (
            owner_id TEXT NOT NULL,
            assignment_id TEXT NOT NULL,
            subject_kind TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            subject_revision INTEGER NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            assignment_revision INTEGER NOT NULL,
            state TEXT NOT NULL,
            trust REAL,
            actor_type TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            supersedes_assignment_id TEXT,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(owner_id, assignment_id),
            UNIQUE(owner_id, subject_kind, subject_id, subject_revision, assignment_revision),
            CHECK(subject_kind IN ('knowledge_revision','candidate_revision','source_revision','document_revision','code_claim_revision','derived_artifact_revision')),
            CHECK(subject_revision > 0),
            CHECK(project_key = '' OR workspace_key <> ''),
            CHECK(state IN ('unreviewed','assigned','revoked')),
            CHECK((state = 'assigned' AND trust IS NOT NULL AND trust >= 0 AND trust <= 1) OR (state <> 'assigned' AND trust IS NULL)),
            CHECK(actor_type = 'owner'),
            CHECK(reason_code IN ('owner_review','owner_pin','owner_correction','owner_retraction','import_review','migration_review'))
        );
        CREATE INDEX IF NOT EXISTS idx_fm_v2_trust_latest
            ON fm_v2_trust_assignments(owner_id, subject_kind, subject_id, subject_revision, assignment_revision DESC);

        CREATE TABLE IF NOT EXISTS fm_v2_job_manifests (
            owner_id TEXT NOT NULL,
            manifest_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            selected_json TEXT NOT NULL,
            completed_json TEXT NOT NULL DEFAULT '[]',
            reused_json TEXT NOT NULL DEFAULT '[]',
            failed_json TEXT NOT NULL DEFAULT '[]',
            waived_json TEXT NOT NULL DEFAULT '[]',
            config_hash TEXT NOT NULL,
            model_hash TEXT NOT NULL,
            tool_hash TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            parent_manifest_id TEXT,
            content_hash TEXT NOT NULL,
            sealed_at TEXT NOT NULL,
            PRIMARY KEY(owner_id, manifest_id),
            UNIQUE(owner_id, job_id, phase),
            CHECK(phase IN ('selection','outcome'))
        );

        CREATE TABLE IF NOT EXISTS fm_v2_projects (
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, project_id)
        );
        CREATE TABLE IF NOT EXISTS fm_v2_project_locators (
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            locator_revision INTEGER NOT NULL,
            canonical_root TEXT NOT NULL,
            root_identity TEXT NOT NULL,
            git_identity TEXT NOT NULL,
            worktree_identity TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            authorized_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, project_id, locator_revision),
            FOREIGN KEY (owner_id, project_id)
                REFERENCES fm_v2_projects(owner_id, project_id),
            CHECK (locator_revision > 0),
            CHECK (active IN (0,1))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fm_v2_project_active_root
            ON fm_v2_project_locators(owner_id, canonical_root)
            WHERE active=1;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fm_v2_project_active_locator
            ON fm_v2_project_locators(owner_id, project_id)
            WHERE active=1;

        CREATE TABLE IF NOT EXISTS fm_v2_entity_aliases (
            owner_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            alias TEXT NOT NULL,
            PRIMARY KEY (owner_id, entity_id, revision, alias),
            FOREIGN KEY (owner_id, entity_id, revision)
                REFERENCES fm_v2_entity_revisions(owner_id, entity_id, revision),
            CHECK (trim(alias) <> '')
        );
        CREATE TABLE IF NOT EXISTS fm_v2_entity_roles (
            owner_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (owner_id, entity_id, revision, role),
            FOREIGN KEY (owner_id, entity_id, revision)
                REFERENCES fm_v2_entity_revisions(owner_id, entity_id, revision),
            CHECK (project_key = '' OR workspace_key <> ''),
            CHECK (trim(role) <> ''),
            CHECK (is_current IN (0,1))
        );
        CREATE TABLE IF NOT EXISTS fm_v2_entity_tags (
            owner_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (owner_id, entity_id, revision, tag),
            FOREIGN KEY (owner_id, entity_id, revision)
                REFERENCES fm_v2_entity_revisions(owner_id, entity_id, revision),
            CHECK (trim(tag) <> '')
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fm_v2_current_self_scope
            ON fm_v2_entity_roles(owner_id, workspace_key, project_key)
            WHERE role='self' AND is_current=1;

        CREATE TABLE IF NOT EXISTS fm_v2_revision_evidence (
            owner_id TEXT NOT NULL,
            block_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            evidence_id TEXT NOT NULL,
            PRIMARY KEY (owner_id, block_id, revision, evidence_id),
            FOREIGN KEY (owner_id, block_id, revision)
                REFERENCES fm_v2_knowledge_revisions(owner_id, block_id, revision),
            FOREIGN KEY (owner_id, evidence_id)
                REFERENCES fm_v2_evidence(owner_id, evidence_id)
        );

        CREATE TABLE IF NOT EXISTS fm_v2_candidates (
            owner_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL,
            created_change_set_id TEXT NOT NULL,
            current_revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, candidate_id),
            UNIQUE (owner_id, workspace_key, project_key, idempotency_key),
            FOREIGN KEY (owner_id, created_change_set_id)
                REFERENCES fm_v2_change_sets(owner_id, change_set_id),
            CHECK (project_key = '' OR workspace_key <> '')
        );
        CREATE TABLE IF NOT EXISTS fm_v2_candidate_revisions (
            owner_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            proposal_json TEXT NOT NULL,
            state TEXT NOT NULL,
            confidence REAL NOT NULL,
            importance REAL NOT NULL,
            reason TEXT NOT NULL,
            source_id TEXT,
            source_revision INTEGER,
            accepted_block_id TEXT,
            content_hash TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            source_event_id TEXT,
            change_set_id TEXT NOT NULL,
            previous_revision INTEGER,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, candidate_id, revision),
            FOREIGN KEY (owner_id, candidate_id)
                REFERENCES fm_v2_candidates(owner_id, candidate_id),
            FOREIGN KEY (owner_id, change_set_id)
                REFERENCES fm_v2_change_sets(owner_id, change_set_id),
            FOREIGN KEY (owner_id, source_id, source_revision)
                REFERENCES fm_v2_sources(owner_id, source_id, source_revision),
            FOREIGN KEY (owner_id, accepted_block_id)
                REFERENCES fm_v2_knowledge_blocks(owner_id, block_id),
            CHECK (revision > 0),
            CHECK (confidence >= 0 AND confidence <= 1),
            CHECK (importance >= 0 AND importance <= 1),
            CHECK ((source_id IS NULL) = (source_revision IS NULL)),
            CHECK (state IN (
                'proposed','validating','awaiting_review','eligible_auto',
                'duplicate','corroborating','conflicted','quarantined',
                'rejected','cancelled','activated'
            ))
        );
        CREATE TABLE IF NOT EXISTS fm_v2_candidate_evidence (
            owner_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            evidence_id TEXT NOT NULL,
            PRIMARY KEY (owner_id, candidate_id, revision, evidence_id),
            FOREIGN KEY (owner_id, candidate_id, revision)
                REFERENCES fm_v2_candidate_revisions(owner_id, candidate_id, revision),
            FOREIGN KEY (owner_id, evidence_id)
                REFERENCES fm_v2_evidence(owner_id, evidence_id)
        );

        CREATE TABLE IF NOT EXISTS fm_v2_conflict_events (
            owner_id TEXT NOT NULL,
            conflict_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            state TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            change_set_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, conflict_id, sequence),
            FOREIGN KEY (owner_id, conflict_id)
                REFERENCES fm_v2_conflicts(owner_id, conflict_id),
            FOREIGN KEY (owner_id, change_set_id)
                REFERENCES fm_v2_change_sets(owner_id, change_set_id),
            CHECK (state IN ('open','resolved','dismissed'))
        );

        CREATE TABLE IF NOT EXISTS fm_v2_job_events (
            owner_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            state TEXT NOT NULL,
            attempt_id TEXT,
            lease_epoch INTEGER,
            result_json TEXT,
            error_json TEXT,
            change_set_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, job_id, sequence),
            FOREIGN KEY (owner_id, job_id)
                REFERENCES fm_v2_jobs(owner_id, job_id),
            FOREIGN KEY (owner_id, change_set_id)
                REFERENCES fm_v2_change_sets(owner_id, change_set_id),
            CHECK (state IN (
                'queued','active','awaiting_review','retry_wait',
                'succeeded','failed_terminal','cancelled'
            ))
        );

        CREATE TABLE IF NOT EXISTS fm_v2_privacy_erasure_context (
            owner_id TEXT PRIMARY KEY,
            erasure_id TEXT NOT NULL UNIQUE,
            source_id TEXT,
            authorized_by TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fm_v2_privacy_erasure_tombstones (
            owner_id TEXT NOT NULL,
            erasure_id TEXT NOT NULL,
            source_id TEXT,
            authorized_by TEXT NOT NULL,
            reason TEXT NOT NULL,
            erased_counts_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, erasure_id)
        );

        CREATE TABLE IF NOT EXISTS fm_v2_episodes (
            owner_id TEXT NOT NULL,
            episode_id TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL,
            source_revision INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_change_set_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, episode_id),
            FOREIGN KEY (owner_id, source_id, source_revision)
                REFERENCES fm_v2_sources(owner_id, source_id, source_revision),
            FOREIGN KEY (owner_id, created_change_set_id)
                REFERENCES fm_v2_change_sets(owner_id, change_set_id),
            CHECK (project_key = '' OR workspace_key <> '')
        );

        CREATE TABLE IF NOT EXISTS fm_v2_derived_generations (
            owner_id TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            logical_space TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            provider_ref TEXT NOT NULL,
            model TEXT NOT NULL,
            endpoint_class TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            normalization TEXT NOT NULL,
            metric TEXT NOT NULL,
            chunker_version TEXT NOT NULL,
            config_fingerprint TEXT NOT NULL,
            source_watermark TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            retention_state TEXT NOT NULL DEFAULT 'retained',
            failure_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            validated_at TEXT,
            PRIMARY KEY(owner_id, generation_id),
            CHECK (project_key = '' OR workspace_key <> ''),
            CHECK (dimension >= 0),
            CHECK (state IN ('planned','building','validating','ready','failed')),
            CHECK (retention_state IN ('retained','gc_eligible','deleted'))
        );
        CREATE INDEX IF NOT EXISTS idx_fm_v2_derived_generations_space
            ON fm_v2_derived_generations(
                owner_id,logical_space,workspace_key,project_key,created_at
            );
        CREATE TABLE IF NOT EXISTS fm_v2_index_pointers (
            owner_id TEXT NOT NULL,
            logical_space TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            generation_id TEXT NOT NULL,
            publication_tx TEXT NOT NULL DEFAULT '',
            publication_watermark TEXT NOT NULL,
            published_at TEXT NOT NULL,
            PRIMARY KEY(owner_id, logical_space, workspace_key, project_key),
            FOREIGN KEY(owner_id, generation_id)
                REFERENCES fm_v2_derived_generations(owner_id, generation_id),
            CHECK (project_key = '' OR workspace_key <> '')
        );
        CREATE TABLE IF NOT EXISTS fm_v2_chunk_embeddings (
            owner_id TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(owner_id, generation_id, chunk_id),
            FOREIGN KEY(owner_id, generation_id)
                REFERENCES fm_v2_derived_generations(owner_id, generation_id),
            FOREIGN KEY(owner_id, chunk_id)
                REFERENCES fm_v2_chunks(owner_id, chunk_id) ON DELETE CASCADE,
            CHECK (dimension > 0)
        );
        CREATE TABLE IF NOT EXISTS fm_v2_index_publications (
            owner_id TEXT NOT NULL,
            publication_id TEXT NOT NULL,
            logical_space TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            previous_generation_id TEXT,
            generation_id TEXT NOT NULL,
            action TEXT NOT NULL,
            publication_watermark TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(owner_id, publication_id),
            CHECK (project_key = '' OR workspace_key <> ''),
            CHECK (action IN ('publish','rollback'))
        );
        CREATE TRIGGER IF NOT EXISTS fm_v2_generation_state_immutable
        BEFORE UPDATE OF state ON fm_v2_derived_generations
        WHEN NOT (
            old.state = new.state OR
            (old.state = 'planned' AND new.state = 'building') OR
            (old.state = 'building' AND new.state IN ('validating','failed')) OR
            (old.state = 'validating' AND new.state IN ('ready','failed'))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid derived generation state transition');
        END;
        CREATE TRIGGER IF NOT EXISTS fm_v2_active_generation_retained
        BEFORE UPDATE OF retention_state ON fm_v2_derived_generations
        WHEN new.retention_state <> 'retained' AND EXISTS (
            SELECT 1 FROM fm_v2_index_pointers pointer
             WHERE pointer.owner_id=old.owner_id
               AND pointer.generation_id=old.generation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'active generation must remain retained');
        END;

        CREATE TABLE IF NOT EXISTS fm_v2_legacy_outbox (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            source_table TEXT NOT NULL,
            source_key TEXT NOT NULL,
            operation TEXT NOT NULL,
            owner_id TEXT,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            source_revision TEXT NOT NULL,
            pre_fingerprint TEXT,
            post_fingerprint TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload_version TEXT NOT NULL,
            payload_json TEXT,
            state TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            applied_at TEXT,
            CHECK (operation IN ('insert','update','delete','retract','forget')),
            CHECK (state IN ('pending','applied','superseded','erased','quarantined')),
            CHECK (project_key = '' OR workspace_key <> '')
        );
        CREATE INDEX IF NOT EXISTS idx_fm_v2_legacy_outbox_pending
            ON fm_v2_legacy_outbox(state, sequence);

        CREATE TABLE IF NOT EXISTS fm_v2_migration_outbox_events (
            migration_run_id TEXT NOT NULL,
            outbox_sequence INTEGER NOT NULL,
            disposition TEXT NOT NULL,
            target_ids TEXT NOT NULL DEFAULT '[]',
            event_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (migration_run_id, outbox_sequence),
            FOREIGN KEY (migration_run_id)
                REFERENCES fm_v2_migration_runs(migration_run_id),
            FOREIGN KEY (outbox_sequence)
                REFERENCES fm_v2_legacy_outbox(sequence),
            CHECK (disposition IN ('applied','superseded','erased','quarantined'))
        );

        CREATE TABLE IF NOT EXISTS fm_v2_migration_review (
            migration_run_id TEXT NOT NULL,
            review_id TEXT NOT NULL,
            plan_id INTEGER NOT NULL,
            owner_id TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'awaiting_review',
            candidate_revision INTEGER NOT NULL DEFAULT 1,
            proposal_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (migration_run_id, review_id),
            FOREIGN KEY (migration_run_id)
                REFERENCES fm_v2_migration_runs(migration_run_id),
            FOREIGN KEY (plan_id)
                REFERENCES fm_v2_migration_plan(plan_id),
            CHECK (state IN ('awaiting_review','edited','rejected','cancelled')),
            CHECK (project_key = '' OR workspace_key <> '')
        );

        CREATE TABLE IF NOT EXISTS fm_v2_policy_trust (
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            trust_id TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1,
            canonical_root TEXT NOT NULL,
            worktree_identity TEXT NOT NULL,
            branch TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            manifest_hash TEXT NOT NULL,
            capability_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            created_by TEXT NOT NULL DEFAULT '',
            created_reason TEXT NOT NULL DEFAULT '',
            revoked_by TEXT,
            revoked_reason TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (
                owner_id, project_id, canonical_root, worktree_identity, branch,
                contract_hash, engine_version, manifest_hash, capability_hash
            ),
            FOREIGN KEY (owner_id, project_id)
                REFERENCES fm_v2_projects(owner_id, project_id)
        );

        CREATE TABLE IF NOT EXISTS fm_v2_policy_profiles (
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            os_uid INTEGER NOT NULL,
            credential_hash TEXT NOT NULL DEFAULT '',
            activation_revision INTEGER NOT NULL DEFAULT 0,
            allowed_actions_json TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            created_by TEXT NOT NULL DEFAULT '',
            created_reason TEXT NOT NULL DEFAULT '',
            revoked_by TEXT,
            revoked_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, project_id, profile_id),
            FOREIGN KEY (owner_id, project_id)
                REFERENCES fm_v2_projects(owner_id, project_id),
            CHECK (os_uid >= 0)
        );
        "#,
    )?;
    if table_exists(conn, "fm_v2_index_generations")? {
        conn.execute_batch(
            r#"
            INSERT OR IGNORE INTO fm_v2_derived_generations(
                owner_id,generation_id,logical_space,workspace_key,project_key,
                provider_ref,model,endpoint_class,dimension,normalization,metric,
                chunker_version,config_fingerprint,source_watermark,row_count,
                state,retention_state,failure_json,created_at,updated_at,validated_at
            )
            SELECT owner_id,generation_id,logical_space,'','',
                   'legacy','','legacy',0,'none','none','legacy',
                   config_fingerprint,source_watermark,0,
                   CASE state
                       WHEN 'building' THEN 'building'
                       WHEN 'validated' THEN 'validating'
                       WHEN 'published' THEN 'ready'
                       WHEN 'retired' THEN 'ready'
                       ELSE 'failed'
                   END,
                   CASE state WHEN 'retired' THEN 'gc_eligible' ELSE 'retained' END,
                   CASE WHEN state='failed' THEN '{"reason":"legacy_generation_failed"}' END,
                   created_at,COALESCE(published_at,created_at),
                   CASE WHEN state IN ('validated','published','retired')
                        THEN COALESCE(published_at,created_at) END
              FROM fm_v2_index_generations;
            CREATE TRIGGER IF NOT EXISTS fm_v2_legacy_generation_no_insert
            BEFORE INSERT ON fm_v2_index_generations BEGIN
                SELECT RAISE(ABORT, 'fm_v2_index_generations is read-only legacy state');
            END;
            CREATE TRIGGER IF NOT EXISTS fm_v2_legacy_generation_no_update
            BEFORE UPDATE ON fm_v2_index_generations BEGIN
                SELECT RAISE(ABORT, 'fm_v2_index_generations is read-only legacy state');
            END;
            CREATE TRIGGER IF NOT EXISTS fm_v2_legacy_generation_no_delete
            BEFORE DELETE ON fm_v2_index_generations BEGIN
                SELECT RAISE(ABORT, 'fm_v2_index_generations is read-only legacy state');
            END;
            "#,
        )?;
    }
    if table_exists(conn, "fm_v2_index_heads")? {
        conn.execute_batch(
            r#"
            INSERT OR IGNORE INTO fm_v2_index_pointers(
                owner_id,logical_space,workspace_key,project_key,generation_id,
                publication_tx,publication_watermark,published_at
            )
            SELECT head.owner_id,head.logical_space,'','',head.generation_id,
                   'compat_' || head.generation_id,generation.source_watermark,
                   head.published_at
              FROM fm_v2_index_heads head
              JOIN fm_v2_derived_generations generation
                ON generation.owner_id=head.owner_id
               AND generation.generation_id=head.generation_id;
            INSERT OR IGNORE INTO fm_v2_index_publications(
                owner_id,publication_id,logical_space,workspace_key,project_key,
                previous_generation_id,generation_id,action,
                publication_watermark,created_at
            )
            SELECT pointer.owner_id,pointer.publication_tx,pointer.logical_space,
                   pointer.workspace_key,pointer.project_key,NULL,
                   pointer.generation_id,'publish',pointer.publication_watermark,
                   pointer.published_at
              FROM fm_v2_index_pointers pointer
             WHERE pointer.publication_tx LIKE 'compat_%';
            CREATE TRIGGER IF NOT EXISTS fm_v2_legacy_head_no_insert
            BEFORE INSERT ON fm_v2_index_heads BEGIN
                SELECT RAISE(ABORT, 'fm_v2_index_heads is read-only legacy state');
            END;
            CREATE TRIGGER IF NOT EXISTS fm_v2_legacy_head_no_update
            BEFORE UPDATE ON fm_v2_index_heads BEGIN
                SELECT RAISE(ABORT, 'fm_v2_index_heads is read-only legacy state');
            END;
            CREATE TRIGGER IF NOT EXISTS fm_v2_legacy_head_no_delete
            BEFORE DELETE ON fm_v2_index_heads BEGIN
                SELECT RAISE(ABORT, 'fm_v2_index_heads is read-only legacy state');
            END;
            "#,
        )?;
    }
    conn.execute_batch(
        r#"
        CREATE VIEW IF NOT EXISTS fm_v2_index_generations_compat AS
            SELECT owner_id,generation_id,logical_space,config_fingerprint,
                   source_watermark,
                   CASE state
                       WHEN 'planned' THEN 'building'
                       WHEN 'building' THEN 'building'
                       WHEN 'validating' THEN 'validated'
                       WHEN 'ready' THEN 'published'
                       ELSE 'failed'
                   END AS state,
                   CASE state WHEN 'failed' THEN 'failed' ELSE 'healthy' END AS health,
                   created_at,
                   CASE state WHEN 'ready' THEN validated_at END AS published_at
              FROM fm_v2_derived_generations;
        CREATE VIEW IF NOT EXISTS fm_v2_index_heads_compat AS
            SELECT owner_id,logical_space,generation_id,published_at
              FROM fm_v2_index_pointers
             WHERE workspace_key='' AND project_key='';
        "#,
    )?;
    for (table, column, definition) in [
        ("fm_v2_projects", "current_root", "TEXT NOT NULL DEFAULT ''"),
        (
            "fm_v2_projects",
            "current_locator_revision",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "fm_v2_project_locators",
            "git_repository",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "fm_v2_project_locators",
            "worktree",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "fm_v2_project_locators",
            "branch",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "fm_v2_project_locators",
            "locator_hash",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "fm_v2_policy_projections",
            "workspace_id",
            "TEXT NOT NULL DEFAULT 'global'",
        ),
        (
            "fm_v2_policy_projections",
            "canonical_root",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "fm_v2_policy_projections",
            "root_identity",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "fm_v2_policy_projections",
            "git_identity",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "fm_v2_policy_projections",
            "worktree_identity",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "fm_v2_policy_projections",
            "activation_revision",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        ("fm_v2_policy_projections", "activated_by", "TEXT"),
        ("fm_v2_policy_projections", "activated_at", "TEXT"),
        (
            "fm_v2_spells",
            "workspace_id",
            "TEXT NOT NULL DEFAULT 'global'",
        ),
        ("fm_v2_spells", "path_scope", "TEXT NOT NULL DEFAULT '**/*'"),
        ("fm_v2_spells", "revision", "INTEGER NOT NULL DEFAULT 1"),
        (
            "fm_v2_spells",
            "review_state",
            "TEXT NOT NULL DEFAULT 'proposed'",
        ),
        (
            "fm_v2_spells",
            "lifecycle",
            "TEXT NOT NULL DEFAULT 'proposed'",
        ),
        ("fm_v2_spells", "reviewed_by", "TEXT"),
        ("fm_v2_spells", "reviewed_at", "TEXT"),
        ("fm_v2_spells", "expires_at", "TEXT"),
        ("fm_v2_spells", "rationale", "TEXT NOT NULL DEFAULT ''"),
        ("fm_v2_spells", "confidence", "REAL NOT NULL DEFAULT 0.5"),
        ("fm_v2_spells", "promoted_contract_hash", "TEXT"),
        ("fm_v2_spells", "promoted_transition_id", "TEXT"),
        ("fm_v2_policy_transitions", "contract_path", "TEXT"),
        ("fm_v2_policy_transitions", "old_contract_hash", "TEXT"),
        ("fm_v2_policy_transitions", "new_contract_hash", "TEXT"),
        ("fm_v2_policy_transitions", "old_bytes", "BLOB"),
        ("fm_v2_policy_transitions", "new_bytes", "BLOB"),
        ("fm_v2_policy_transitions", "spell_id", "TEXT"),
        ("fm_v2_policy_transitions", "spell_revision", "INTEGER"),
        (
            "fm_v2_policy_transitions",
            "payload_json",
            "TEXT NOT NULL DEFAULT '{}'",
        ),
        ("fm_v2_policy_transitions", "updated_at", "TEXT"),
        ("fm_v2_policy_transitions", "operation_id", "TEXT"),
        (
            "fm_v2_policy_transitions",
            "sequence",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "fm_v2_policy_transitions",
            "state",
            "TEXT NOT NULL DEFAULT 'pending'",
        ),
        ("fm_v2_policy_transitions", "previous_transition_id", "TEXT"),
        ("fm_v2_policy_transitions", "previous_event_hash", "TEXT"),
        ("fm_v2_policy_transitions", "event_hash", "TEXT"),
        ("fm_v2_policy_transitions", "old_bytes_ref", "TEXT"),
        ("fm_v2_policy_transitions", "new_bytes_ref", "TEXT"),
        ("fm_v2_policy_transitions", "error_json", "TEXT"),
        ("fm_v2_policy_trust", "trust_id", "TEXT NOT NULL DEFAULT ''"),
        (
            "fm_v2_policy_trust",
            "revision",
            "INTEGER NOT NULL DEFAULT 1",
        ),
        (
            "fm_v2_policy_trust",
            "created_by",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "fm_v2_policy_trust",
            "created_reason",
            "TEXT NOT NULL DEFAULT ''",
        ),
        ("fm_v2_policy_trust", "revoked_by", "TEXT"),
        ("fm_v2_policy_trust", "revoked_reason", "TEXT"),
        (
            "fm_v2_policy_profiles",
            "credential_hash",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "fm_v2_policy_profiles",
            "activation_revision",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "fm_v2_policy_profiles",
            "created_by",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "fm_v2_policy_profiles",
            "created_reason",
            "TEXT NOT NULL DEFAULT ''",
        ),
        ("fm_v2_policy_profiles", "revoked_by", "TEXT"),
        ("fm_v2_policy_profiles", "revoked_reason", "TEXT"),
        (
            "fm_v2_migration_runs",
            "outbox_high_water_mark",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        ("fm_v2_migration_runs", "backup_path", "TEXT"),
        ("fm_v2_migration_runs", "backup_hash", "TEXT"),
    ] {
        ensure_column(conn, table, column, definition)?;
    }
    conn.execute_batch(
        r#"
        DELETE FROM fm_v2_privacy_erasure_context;

        CREATE UNIQUE INDEX IF NOT EXISTS idx_fm_v2_policy_transition_sequence
            ON fm_v2_policy_transitions(owner_id, operation_id, sequence)
            WHERE operation_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_fm_v2_candidates_scope
            ON fm_v2_candidates(owner_id,workspace_key,project_key,current_revision);
        CREATE INDEX IF NOT EXISTS idx_fm_v2_candidate_state
            ON fm_v2_candidate_revisions(owner_id,state,created_at);
        CREATE INDEX IF NOT EXISTS idx_fm_v2_job_events_state
            ON fm_v2_job_events(owner_id,state,created_at);

        CREATE TRIGGER IF NOT EXISTS fm_v2_block_scope_insert
        BEFORE INSERT ON fm_v2_knowledge_blocks
        WHEN NOT EXISTS (
            SELECT 1 FROM fm_v2_entities entity
             WHERE entity.owner_id=NEW.owner_id
               AND entity.entity_id=NEW.subject_entity_id
               AND entity.workspace_key=NEW.workspace_key
               AND entity.project_key=NEW.project_key
        )
        BEGIN
            SELECT RAISE(ABORT, 'knowledge subject scope mismatch');
        END;

        CREATE TRIGGER IF NOT EXISTS fm_v2_block_scope_update
        BEFORE UPDATE OF subject_entity_id,workspace_key,project_key ON fm_v2_knowledge_blocks
        WHEN NOT EXISTS (
            SELECT 1 FROM fm_v2_entities entity
             WHERE entity.owner_id=NEW.owner_id
               AND entity.entity_id=NEW.subject_entity_id
               AND entity.workspace_key=NEW.workspace_key
               AND entity.project_key=NEW.project_key
        )
        BEGIN
            SELECT RAISE(ABORT, 'knowledge subject scope mismatch');
        END;

        CREATE TRIGGER IF NOT EXISTS fm_v2_knowledge_revision_validate
        BEFORE INSERT ON fm_v2_knowledge_revisions
        WHEN NEW.value_type NOT IN ('null','string','number','boolean','time','duration','uri','entity_ref','object','list')
          OR NEW.status NOT IN ('open','active','superseded','retracted')
          OR (NEW.status='open' AND (NEW.value_type<>'null' OR NEW.expected_value_type IS NULL))
          OR (NEW.revision=1 AND NEW.previous_revision IS NOT NULL)
          OR (NEW.revision>1 AND (
                NEW.previous_revision<>NEW.revision-1 OR NOT EXISTS (
                    SELECT 1 FROM fm_v2_knowledge_revisions prior
                     WHERE prior.owner_id=NEW.owner_id
                       AND prior.block_id=NEW.block_id
                       AND prior.revision=NEW.previous_revision
                )
             ))
          OR EXISTS (
                SELECT 1 FROM fm_v2_knowledge_blocks block
                JOIN fm_v2_predicates predicate ON predicate.predicate=block.predicate
                 WHERE block.owner_id=NEW.owner_id
                   AND block.block_id=NEW.block_id
                   AND (predicate.cardinality<>block.cardinality OR
                        predicate.value_type<>CASE WHEN NEW.value_type='null'
                            THEN COALESCE(NEW.expected_value_type,'') ELSE NEW.value_type END)
             )
        BEGIN
            SELECT RAISE(ABORT, 'invalid knowledge revision');
        END;

        CREATE TRIGGER IF NOT EXISTS fm_v2_entity_revision_validate
        BEFORE INSERT ON fm_v2_entity_revisions
        WHEN (NEW.revision=1 AND NEW.previous_revision IS NOT NULL)
          OR (NEW.revision>1 AND (
                NEW.previous_revision<>NEW.revision-1 OR NOT EXISTS (
                    SELECT 1 FROM fm_v2_entity_revisions prior
                     WHERE prior.owner_id=NEW.owner_id
                       AND prior.entity_id=NEW.entity_id
                       AND prior.revision=NEW.previous_revision
                )
             ))
        BEGIN
            SELECT RAISE(ABORT, 'invalid entity revision');
        END;

        CREATE TRIGGER IF NOT EXISTS fm_v2_candidate_revision_validate
        BEFORE INSERT ON fm_v2_candidate_revisions
        WHEN (NEW.revision=1 AND (
                NEW.previous_revision IS NOT NULL OR NEW.state<>'proposed'
             ))
          OR (NEW.revision>1 AND (
                NEW.previous_revision<>NEW.revision-1 OR NOT EXISTS (
                    SELECT 1 FROM fm_v2_candidate_revisions prior
                     WHERE prior.owner_id=NEW.owner_id
                       AND prior.candidate_id=NEW.candidate_id
                       AND prior.revision=NEW.previous_revision
                ) OR NOT EXISTS (
                    SELECT 1 FROM fm_v2_candidate_revisions prior
                     WHERE prior.owner_id=NEW.owner_id
                       AND prior.candidate_id=NEW.candidate_id
                       AND prior.revision=NEW.previous_revision
                       AND (
                           (prior.state='proposed' AND NEW.state='validating') OR
                           (prior.state='validating' AND NEW.state IN (
                               'awaiting_review','eligible_auto','duplicate',
                               'corroborating','conflicted','quarantined','rejected'
                           )) OR
                           (prior.state='awaiting_review' AND NEW.state IN (
                               'proposed','activated','rejected','cancelled'
                           )) OR
                           (prior.state='eligible_auto' AND NEW.state IN (
                               'activated','awaiting_review','quarantined'
                           ))
                       )
                )
             ))
          OR (NEW.state='activated' AND NEW.accepted_block_id IS NULL)
        BEGIN
            SELECT RAISE(ABORT, 'invalid candidate revision');
        END;

        CREATE TRIGGER IF NOT EXISTS fm_v2_knowledge_head_validate
        BEFORE UPDATE OF current_revision ON fm_v2_knowledge_blocks
        WHEN NEW.current_revision>0 AND NOT EXISTS (
            SELECT 1 FROM fm_v2_knowledge_revisions revision
             WHERE revision.owner_id=NEW.owner_id
               AND revision.block_id=NEW.block_id
               AND revision.revision=NEW.current_revision
        )
        BEGIN
            SELECT RAISE(ABORT, 'knowledge head revision missing');
        END;

        CREATE TRIGGER IF NOT EXISTS fm_v2_entity_head_validate
        BEFORE UPDATE OF current_revision ON fm_v2_entities
        WHEN NEW.current_revision>0 AND NOT EXISTS (
            SELECT 1 FROM fm_v2_entity_revisions revision
             WHERE revision.owner_id=NEW.owner_id
               AND revision.entity_id=NEW.entity_id
               AND revision.revision=NEW.current_revision
        )
        BEGIN
            SELECT RAISE(ABORT, 'entity head revision missing');
        END;

        CREATE TRIGGER IF NOT EXISTS fm_v2_candidate_head_validate
        BEFORE UPDATE OF current_revision ON fm_v2_candidates
        WHEN NEW.current_revision>0 AND NOT EXISTS (
            SELECT 1 FROM fm_v2_candidate_revisions revision
             WHERE revision.owner_id=NEW.owner_id
               AND revision.candidate_id=NEW.candidate_id
               AND revision.revision=NEW.current_revision
        )
        BEGIN
            SELECT RAISE(ABORT, 'candidate head revision missing');
        END;

        CREATE TRIGGER IF NOT EXISTS fm_v2_job_state_validate
        BEFORE UPDATE OF state ON fm_v2_jobs
        WHEN NOT (
            OLD.state=NEW.state OR
            (OLD.state='queued' AND NEW.state IN ('active','cancelled')) OR
            (OLD.state='active' AND NEW.state IN (
                'awaiting_review','retry_wait','succeeded','failed_terminal','cancelled'
            )) OR
            (OLD.state='retry_wait' AND NEW.state IN ('queued','cancelled')) OR
            (OLD.state='awaiting_review' AND NEW.state IN ('queued','succeeded','cancelled'))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid job state transition');
        END;

        CREATE TRIGGER IF NOT EXISTS fm_v2_attempt_state_validate
        BEFORE UPDATE OF state ON fm_v2_attempts
        WHEN NOT (
            OLD.state=NEW.state OR
            (OLD.state='leased' AND NEW.state IN ('running','cancelled','fenced')) OR
            (OLD.state='running' AND NEW.state IN (
                'succeeded','failed_retryable','failed_terminal','cancelled','fenced'
            ))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid attempt state transition');
        END;

        DROP TRIGGER IF EXISTS fm_v2_knowledge_revision_no_update;
        DROP TRIGGER IF EXISTS fm_v2_knowledge_revision_no_delete;
        DROP TRIGGER IF EXISTS fm_v2_entity_revision_no_update;
        DROP TRIGGER IF EXISTS fm_v2_entity_revision_no_delete;

        CREATE TRIGGER fm_v2_knowledge_revision_no_update
        BEFORE UPDATE ON fm_v2_knowledge_revisions
        BEGIN
            SELECT RAISE(ABORT, 'knowledge revisions are append-only');
        END;
        CREATE TRIGGER fm_v2_knowledge_revision_no_delete
        BEFORE DELETE ON fm_v2_knowledge_revisions
        WHEN NOT EXISTS (
            SELECT 1 FROM fm_v2_privacy_erasure_context context
             WHERE context.owner_id=OLD.owner_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'knowledge revisions are append-only');
        END;
        CREATE TRIGGER fm_v2_entity_revision_no_update
        BEFORE UPDATE ON fm_v2_entity_revisions
        BEGIN
            SELECT RAISE(ABORT, 'entity revisions are append-only');
        END;
        CREATE TRIGGER fm_v2_entity_revision_no_delete
        BEFORE DELETE ON fm_v2_entity_revisions
        WHEN NOT EXISTS (
            SELECT 1 FROM fm_v2_privacy_erasure_context context
             WHERE context.owner_id=OLD.owner_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'entity revisions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS fm_v2_candidate_revision_no_update
        BEFORE UPDATE ON fm_v2_candidate_revisions
        BEGIN
            SELECT RAISE(ABORT, 'candidate revisions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS fm_v2_candidate_revision_no_delete
        BEFORE DELETE ON fm_v2_candidate_revisions
        WHEN NOT EXISTS (
            SELECT 1 FROM fm_v2_privacy_erasure_context context
             WHERE context.owner_id=OLD.owner_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'candidate revisions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS fm_v2_conflict_event_no_update
        BEFORE UPDATE ON fm_v2_conflict_events BEGIN
            SELECT RAISE(ABORT, 'conflict events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS fm_v2_conflict_event_no_delete
        BEFORE DELETE ON fm_v2_conflict_events
        WHEN NOT EXISTS (
            SELECT 1 FROM fm_v2_privacy_erasure_context context
             WHERE context.owner_id=OLD.owner_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'conflict events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS fm_v2_job_event_no_update
        BEFORE UPDATE ON fm_v2_job_events BEGIN
            SELECT RAISE(ABORT, 'job events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS fm_v2_job_event_no_delete
        BEFORE DELETE ON fm_v2_job_events BEGIN
            SELECT RAISE(ABORT, 'job events are append-only');
        END;
        "#,
    )?;
    Ok(())
}

fn ensure_column(
    conn: &Connection,
    table: &str,
    column: &str,
    definition: &str,
) -> Result<(), SqliteError> {
    let present = conn
        .prepare(&format!("PRAGMA table_info({table})"))?
        .query_map([], |row| row.get::<_, String>(1))?
        .filter_map(Result::ok)
        .any(|name| name == column);
    if !present {
        conn.execute(
            &format!("ALTER TABLE {table} ADD COLUMN {column} {definition}"),
            [],
        )?;
    }
    Ok(())
}

fn table_exists(conn: &Connection, table: &str) -> Result<bool, SqliteError> {
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
        [table],
        |row| row.get(0),
    )
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

    #[test]
    fn legacy_generation_rows_migrate_once_and_become_read_only() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON; CREATE TABLE fm_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);").unwrap();
        install_schema(&conn).unwrap();
        conn.execute_batch(
            "CREATE TABLE fm_v2_index_generations(
                owner_id TEXT NOT NULL,generation_id TEXT NOT NULL,
                logical_space TEXT NOT NULL,config_fingerprint TEXT NOT NULL,
                source_watermark TEXT NOT NULL,state TEXT NOT NULL,
                health TEXT NOT NULL,created_at TEXT NOT NULL,published_at TEXT,
                PRIMARY KEY(owner_id,generation_id));
             CREATE TABLE fm_v2_index_heads(
                owner_id TEXT NOT NULL,logical_space TEXT NOT NULL,
                generation_id TEXT NOT NULL,published_at TEXT NOT NULL,
                PRIMARY KEY(owner_id,logical_space));
             INSERT INTO fm_v2_index_generations VALUES(
                'alice','legacy-g1','documents_vector','config','watermark',
                'published','healthy','created','published');
             INSERT INTO fm_v2_index_heads VALUES(
                'alice','documents_vector','legacy-g1','published');",
        )
        .unwrap();
        ensure_schema_extensions(&conn).unwrap();
        assert_eq!(
            conn.query_row(
                "SELECT state,retention_state FROM fm_v2_derived_generations WHERE owner_id='alice' AND generation_id='legacy-g1'",
                [],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            )
            .unwrap(),
            ("ready".into(), "retained".into())
        );
        assert_eq!(
            conn.query_row(
                "SELECT generation_id,publication_tx FROM fm_v2_index_pointers WHERE owner_id='alice' AND logical_space='documents_vector'",
                [],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            )
            .unwrap(),
            ("legacy-g1".into(), "compat_legacy-g1".into())
        );
        assert!(conn
            .execute(
                "UPDATE fm_v2_index_generations SET health='degraded' WHERE owner_id='alice'",
                [],
            )
            .unwrap_err()
            .to_string()
            .contains("read-only legacy state"));
    }
}
