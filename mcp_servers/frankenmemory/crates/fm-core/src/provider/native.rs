use async_trait::async_trait;
use tracing::info;

use crate::config::FmConfig;
use crate::curate;
use crate::embed::EmbeddingClient;
use crate::provider::*;
use crate::record::*;
use crate::retrieval;
use crate::store::MemoryStore;
use std::sync::Arc;

pub struct NativeProvider {
    store: Arc<dyn MemoryStore>,
    embed: Arc<dyn EmbeddingClient>,
    config: FmConfig,
}

impl NativeProvider {
    pub fn new(
        store: Arc<dyn MemoryStore>,
        embed: Arc<dyn EmbeddingClient>,
        config: FmConfig,
    ) -> Self {
        Self {
            store,
            embed,
            config,
        }
    }

    pub async fn update_curated_record(
        &self,
        id: &str,
        content: Option<&str>,
        kind: Option<MemoryKind>,
        category: Option<&str>,
        pinned: Option<bool>,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> bool {
        self.store
            .update_curated_record(id, content, kind, category, pinned, owner, workspace_id)
            .await
    }

    pub async fn delete_curated_record(
        &self,
        id: &str,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> bool {
        self.store
            .delete_curated_record(id, owner, workspace_id)
            .await
    }

    pub fn get_curated_record(
        &self,
        id: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Option<MemoryRecord>, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "direct reads require SQLite".to_string())?
            .get_curated_record(id, owner, workspace_id)
            .map_err(|error| error.to_string())
    }

    pub fn list_curated_records(
        &self,
        owner: &str,
        workspace_id: &str,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<MemoryRecord>, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "paginated reads require SQLite".to_string())?
            .list_curated_records(owner, workspace_id, limit, offset)
            .map_err(|error| error.to_string())
    }

    pub fn record_curated_access(
        &self,
        ids: &[String],
        owner: &str,
        workspace_id: &str,
    ) -> Result<usize, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "access accounting requires SQLite".to_string())?
            .record_curated_access(ids, owner, workspace_id)
            .map_err(|error| error.to_string())
    }

    pub fn list_candidates(
        &self,
        owner: Option<&str>,
        workspace_id: Option<&str>,
        status: Option<&str>,
        limit: usize,
    ) -> Result<Vec<CandidateRecord>, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "candidate inspection requires SQLite".to_string())?
            .list_candidates(owner, workspace_id, status, limit)
            .map_err(|error| error.to_string())
    }

    pub fn quality_status(&self) -> Result<serde_json::Value, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "quality status requires SQLite".to_string())?
            .quality_status()
            .map_err(|error| error.to_string())
    }

    pub fn rebuild_graph_cue_fts(&self) -> Result<serde_json::Value, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "graph FTS rebuild requires SQLite".to_string())?
            .rebuild_graph_cue_fts()
            .map_err(|error| error.to_string())
    }

    pub fn list_quarantine(
        &self,
        owner: Option<&str>,
        workspace_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<serde_json::Value>, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "quarantine inspection requires SQLite".to_string())?
            .list_quarantine(owner, workspace_id, limit)
            .map_err(|error| error.to_string())
    }

    pub fn quarantine_legacy_state(
        &self,
        dry_run: bool,
        reason: &str,
    ) -> Result<serde_json::Value, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "quarantine migration requires SQLite".to_string())?
            .quarantine_legacy_state(dry_run, reason)
            .map_err(|error| error.to_string())
    }

    pub async fn review_candidate(
        &self,
        id: &str,
        accept: bool,
        reason: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Option<String>, String> {
        let sqlite = self
            .store
            .as_sqlite()
            .ok_or_else(|| "candidate review requires SQLite".to_string())?;
        let candidate = sqlite
            .candidate_by_id(id, owner, workspace_id)
            .map_err(|error| error.to_string())?
            .ok_or_else(|| "candidate not found in this scope".to_string())?;
        if !accept {
            sqlite
                .set_candidate_status(
                    id,
                    CandidateStatus::Rejected,
                    reason,
                    None,
                    owner,
                    workspace_id,
                )
                .map_err(|error| error.to_string())?;
            sqlite.metric_add("candidates_rejected_by_review", 1);
            return Ok(None);
        }
        if let Some(existing) = candidate.accepted_curated_id {
            return Ok(Some(existing));
        }
        if prefilter_candidate(&candidate.content, &candidate.evidence_role).is_some() {
            return Err("candidate still fails deterministic admission policy".into());
        }
        let curated_id = stable_id("m", &[id]);
        let mut record = MemoryRecord::new(&candidate.content);
        record.id = curated_id.clone();
        record.kind = candidate.kind;
        record.confidence_score = candidate.confidence_score;
        record.importance_score = candidate.importance_score;
        record.source = candidate.source.clone();
        record.source_type = SourceType::Human;
        record.owner = Some(owner.to_string());
        record.workspace_id = workspace_id.to_string();
        record.workspace_path = candidate.workspace_path.clone();
        record.session_id = candidate.session_id.clone();
        record.session_key = candidate.session_id.clone();
        record.metadata = serde_json::json!({
            "candidate_id": candidate.id,
            "turn_id": candidate.turn_id,
            "raw_evidence_ids": candidate.raw_evidence_ids,
            "admission_reason": reason,
            "manually_reviewed": true,
        });
        let embedding = self.embed.embed(&candidate.content).await.ok();
        if !self
            .store
            .upsert_curated(&record, embedding.as_deref())
            .await
        {
            return Err("failed to persist accepted curated memory".into());
        }
        sqlite
            .set_candidate_status(
                id,
                CandidateStatus::Accepted,
                reason,
                Some(&curated_id),
                owner,
                workspace_id,
            )
            .map_err(|error| error.to_string())?;
        sqlite.metric_add("candidates_accepted_by_review", 1);
        if embedding.is_some() {
            sqlite.metric_add("curated_embeddings_written", 1);
        }
        Ok(Some(curated_id))
    }
}

fn category_to_kind(category: Option<&str>) -> MemoryKind {
    match category
        .unwrap_or("episodic")
        .trim()
        .to_lowercase()
        .as_str()
    {
        "persona" | "identity" => MemoryKind::Persona,
        "instruction" | "preference" => MemoryKind::Instruction,
        "fact" | "contact" | "project" | "goal" => MemoryKind::Fact,
        "fabric" => MemoryKind::Fabric,
        "wiki" | "reference" => MemoryKind::Wiki,
        _ => MemoryKind::Episodic,
    }
}

fn stable_id(prefix: &str, parts: &[&str]) -> String {
    let joined = parts.join("\u{1f}");
    format!(
        "{prefix}_{}",
        &blake3::hash(joined.as_bytes()).to_hex()[..24]
    )
}

fn normalized(value: &str) -> String {
    value
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim()
        .to_lowercase()
}

fn prefilter_candidate(content: &str, evidence_role: &str) -> Option<&'static str> {
    let text = normalized(content);
    let words = text.split_whitespace().count();
    if text.is_empty() || words < 3 {
        return Some("empty_or_short_acknowledgement");
    }
    if matches!(
        text.as_str(),
        "ok thanks" | "thank you" | "sounds good" | "got it" | "pong pong pong"
    ) {
        return Some("acknowledgement");
    }
    if text.contains("/tmp/")
        || text.contains("temporary permission test")
        || text.contains("permission-test")
    {
        return Some("temporary_fixture");
    }
    if text.contains("distill this conversation")
        || text.contains("memory dream")
        || text.contains("dream cycle")
        || text.contains("dream/distill")
    {
        return Some("automation_prompt");
    }
    if text.contains("<tool_call")
        || text.contains("tool protocol")
        || text.contains("mcp tool result")
        || text.contains("calltoolresult")
    {
        return Some("tool_protocol");
    }
    if evidence_role == "assistant"
        && (text.starts_with("i will ")
            || text.starts_with("i'll ")
            || text.starts_with("let me ")
            || text.starts_with("i am going to "))
    {
        return Some("assistant_process_narration");
    }
    None
}

fn automatic_admission(
    content: &str,
    evidence_role: &str,
) -> Option<(&'static str, MemoryKind, &'static str, bool)> {
    if evidence_role != "user" || content.contains('?') {
        return None;
    }
    let text = normalized(content);
    if [
        "password",
        "api key",
        "secret",
        "access token",
        "credit card",
        "ssn",
    ]
    .iter()
    .any(|term| text.contains(term))
    {
        return None;
    }
    if ["my name is ", "call me "]
        .iter()
        .any(|prefix| text.starts_with(prefix))
    {
        return Some(("auto_identity_claim", MemoryKind::Persona, "identity", true));
    }
    if ["i like ", "i love ", "i prefer ", "i dislike ", "i hate "]
        .iter()
        .any(|prefix| text.starts_with(prefix))
    {
        return Some((
            "auto_preference_claim",
            MemoryKind::Instruction,
            "preference",
            false,
        ));
    }
    if [
        "i live in ",
        "i am from ",
        "i'm from ",
        "i work on ",
        "i am working on ",
        "i'm working on ",
        "i am traveling to ",
        "i'm traveling to ",
        "i am travelling to ",
        "i'm travelling to ",
    ]
    .iter()
    .any(|prefix| text.starts_with(prefix))
    {
        return Some(("auto_user_claim", MemoryKind::Fact, "fact", false));
    }
    None
}

fn metadata_string(metadata: &serde_json::Value, key: &str) -> String {
    metadata
        .get(key)
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn metadata_strings(metadata: &serde_json::Value, key: &str) -> Vec<String> {
    metadata
        .get(key)
        .and_then(serde_json::Value::as_array)
        .map(|values| {
            values
                .iter()
                .filter_map(serde_json::Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

#[async_trait]
impl MemoryProvider for NativeProvider {
    fn id(&self) -> &str {
        "native"
    }

    async fn capture(&self, turn: &CompletedTurn) -> CaptureResult {
        let capture_mode = metadata_string(&turn.metadata, "capture_mode");
        // Direct provider callers predate the MCP capture-mode contract and
        // represent deliberate saves. The MCP boundary stamps every automatic
        // turn explicitly as raw_only, so compatibility here is safe.
        let capture_mode = if capture_mode.is_empty() {
            "manual"
        } else {
            capture_mode.as_str()
        };
        let workspace_id = turn.workspace_id.trim();
        let owner = turn.owner.as_deref().unwrap_or_default().trim();
        // Conversational memory writes to the "global" workspace by
        // convention; only a missing owner disqualifies automatic capture.
        if capture_mode != "manual" && owner.is_empty() {
            tracing::warn!("automatic capture rejected: owner is required");
            return CaptureResult {
                records_captured: 0,
                record_ids: vec![],
                vectors_written: 0,
                providers_succeeded: 0,
                providers_failed: 1,
            };
        }

        let has_user = !turn.user_text.trim().is_empty();
        let has_assistant = !turn.assistant_text.trim().is_empty();
        if !has_user && !has_assistant {
            return CaptureResult {
                records_captured: 0,
                record_ids: vec![],
                vectors_written: 0,
                providers_succeeded: 1,
                providers_failed: 0,
            };
        }

        let effective_owner = if owner.is_empty() { "legacy" } else { owner };
        let effective_workspace = if workspace_id.is_empty() {
            "global"
        } else {
            workspace_id
        };
        let source_message_ids = metadata_strings(&turn.metadata, "source_message_ids");
        let mut source_event_id = metadata_string(&turn.metadata, "source_event_id");
        if source_event_id.is_empty() {
            source_event_id = stable_id(
                "event",
                &[
                    effective_owner,
                    effective_workspace,
                    &turn.session_id,
                    &turn.user_text,
                    &turn.assistant_text,
                ],
            );
        }
        let turn_id = stable_id("turn", &[&source_event_id]);
        let mut metadata = turn.metadata.clone();
        if !metadata.is_object() {
            metadata = serde_json::json!({});
        }
        if let Some(object) = metadata.as_object_mut() {
            object.insert("turn_id".into(), turn_id.clone().into());
            object.insert("source_event_id".into(), source_event_id.clone().into());
            object.insert(
                "source_message_ids".into(),
                serde_json::json!(source_message_ids),
            );
        }

        let mut records_captured = 0usize;
        let mut record_ids = Vec::new();
        let mut raw_ids = Vec::new();
        for (role, content, message_index) in [
            ("user", turn.user_text.as_str(), 0usize),
            ("assistant", turn.assistant_text.as_str(), 1usize),
        ] {
            if content.trim().is_empty() {
                continue;
            }
            let mut raw = RawTurn::new(role, content);
            raw.id = stable_id("raw", &[&source_event_id, role]);
            raw.session_key = turn.session_key.clone();
            raw.session_id = turn.session_id.clone();
            raw.workspace_id = effective_workspace.to_string();
            raw.owner = Some(effective_owner.to_string());
            raw.workspace_path = turn.workspace_path.clone();
            raw.metadata = metadata.clone();
            if let Some(object) = raw.metadata.as_object_mut() {
                if let Some(message_id) = source_message_ids.get(message_index) {
                    object.insert("source_message_id".into(), message_id.clone().into());
                }
            }
            let raw_id = raw.id.clone();
            if self.store.upsert_raw(&raw, None).await {
                records_captured += 1;
                raw_ids.push(raw_id.clone());
                if let Some(sqlite) = self.store.as_sqlite() {
                    sqlite.metric_add("raw_rows_written", 1);
                    sqlite.metric_add("raw_embeddings_avoided", 1);
                }
            } else if let Some(sqlite) = self.store.as_sqlite() {
                sqlite.metric_add("dedup_hits", 1);
            }
            record_ids.push(raw_id);
        }

        if capture_mode == "raw_only" {
            info!("native raw capture: {} rows", records_captured);
            return CaptureResult {
                records_captured,
                record_ids,
                vectors_written: 0,
                providers_succeeded: 1,
                providers_failed: 0,
            };
        }

        let (content, evidence_role) = if has_user {
            (turn.user_text.trim(), "user")
        } else {
            (turn.assistant_text.trim(), "assistant")
        };
        let rejection = prefilter_candidate(content, evidence_role);
        let auto_admission = automatic_admission(content, evidence_role);
        let dedup_key = stable_id(
            "dedup",
            &[effective_owner, effective_workspace, &normalized(content)],
        );
        let candidate_id = stable_id("candidate", &[&dedup_key]);
        let now = chrono::Utc::now().to_rfc3339();
        let manual = capture_mode == "manual";
        let candidate_kind = turn
            .category
            .as_deref()
            .map(|category| category_to_kind(Some(category)))
            .or_else(|| auto_admission.map(|(_, kind, _, _)| kind))
            .unwrap_or(MemoryKind::Episodic);
        let admission_reason = if manual {
            Some("manual_admission")
        } else {
            auto_admission.map(|(reason, _, _, _)| reason)
        };
        let mut candidate = CandidateRecord {
            id: candidate_id.clone(),
            content: content.to_string(),
            kind: candidate_kind,
            confidence_score: if manual {
                0.95
            } else if auto_admission.is_some() {
                0.88
            } else {
                0.55
            },
            importance_score: if manual {
                0.8
            } else if auto_admission.is_some() {
                0.7
            } else {
                0.5
            },
            owner: effective_owner.to_string(),
            workspace_id: effective_workspace.to_string(),
            workspace_path: turn.workspace_path.clone(),
            session_id: turn.session_id.clone(),
            turn_id: turn_id.clone(),
            raw_evidence_ids: raw_ids,
            evidence_role: evidence_role.to_string(),
            source: turn.source.clone(),
            source_event_id,
            dedup_key: dedup_key.clone(),
            status: if rejection.is_some() {
                CandidateStatus::Rejected
            } else {
                CandidateStatus::Pending
            },
            reason: rejection
                .unwrap_or(admission_reason.unwrap_or("awaiting_review"))
                .into(),
            accepted_curated_id: None,
            created_at: now.clone(),
            updated_at: now,
        };
        let Some(sqlite) = self.store.as_sqlite() else {
            return CaptureResult {
                records_captured,
                record_ids,
                vectors_written: 0,
                providers_succeeded: 0,
                providers_failed: 1,
            };
        };
        match sqlite.insert_candidate(&candidate) {
            Ok(true) => {
                records_captured += 1;
                record_ids.push(candidate_id.clone());
                sqlite.metric_add("candidates_created", 1);
            }
            Ok(false) => {
                sqlite.metric_add("dedup_hits", 1);
                if let Ok(Some(existing)) = sqlite.candidate_by_dedup(&dedup_key) {
                    if let Some(id) = existing.accepted_curated_id {
                        record_ids.insert(0, id);
                    }
                }
                return CaptureResult {
                    records_captured,
                    record_ids,
                    vectors_written: 0,
                    providers_succeeded: 1,
                    providers_failed: 0,
                };
            }
            Err(error) => {
                tracing::warn!("candidate insert failed: {error}");
                return CaptureResult {
                    records_captured,
                    record_ids,
                    vectors_written: 0,
                    providers_succeeded: 0,
                    providers_failed: 1,
                };
            }
        }
        if let Some(reason) = rejection {
            sqlite.metric_add(&format!("rejected_{reason}"), 1);
            return CaptureResult {
                records_captured,
                record_ids,
                vectors_written: 0,
                providers_succeeded: 1,
                providers_failed: 0,
            };
        }
        let Some(admission_reason) = admission_reason else {
            sqlite.metric_add("candidates_pending", 1);
            return CaptureResult {
                records_captured,
                record_ids,
                vectors_written: 0,
                providers_succeeded: 1,
                providers_failed: 0,
            };
        };

        let curated_id = stable_id("m", &[&candidate_id]);
        let mut record = MemoryRecord::new(content);
        record.id = curated_id.clone();
        record.kind = candidate.kind;
        record.metadata = metadata;
        if let Some(object) = record.metadata.as_object_mut() {
            object.insert("candidate_id".into(), candidate_id.clone().into());
            object.insert("admission_reason".into(), admission_reason.into());
            if let Some((_, _, category, pinned)) = auto_admission {
                object.entry("category").or_insert_with(|| category.into());
                if pinned {
                    object.insert("pinned".into(), true.into());
                }
            }
        }
        record.source = turn.source.clone();
        record.source_type = SourceType::Human;
        record.owner = Some(effective_owner.to_string());
        record.workspace_id = effective_workspace.to_string();
        record.workspace_path = turn.workspace_path.clone();
        record.session_key = turn.session_key.clone();
        record.session_id = turn.session_id.clone();
        record.source_message_ids = source_message_ids;
        record.confidence_score = candidate.confidence_score;
        record.importance_score = candidate.importance_score;
        if auto_admission.is_some_and(|(_, _, _, pinned)| pinned) {
            record.exempt_from_decay = true;
            record.exempt_from_dedup = true;
        }

        let embedding = self.embed.embed(content).await.ok();
        if self
            .store
            .upsert_curated(&record, embedding.as_deref())
            .await
        {
            records_captured += 1;
            record_ids.insert(0, curated_id.clone());
            sqlite.metric_add("candidates_accepted", 1);
            if embedding.is_some() {
                sqlite.metric_add("curated_embeddings_written", 1);
            }
            let _ = sqlite.set_candidate_status(
                &candidate_id,
                CandidateStatus::Accepted,
                admission_reason,
                Some(&curated_id),
                effective_owner,
                effective_workspace,
            );
            candidate.status = CandidateStatus::Accepted;
        }

        CaptureResult {
            records_captured,
            record_ids,
            vectors_written: usize::from(embedding.is_some()),
            providers_succeeded: 1,
            providers_failed: 0,
        }
    }

    async fn recall(&self, q: &RecallQuery) -> RecallResult {
        let Some(owner) = q.owner.as_deref().filter(|value| !value.trim().is_empty()) else {
            return RecallResult {
                prepend_context: String::new(),
                append_system_context: String::new(),
                memories: vec![],
                recall_strategy: "scope_required".into(),
                ground_truth_preamble: None,
            };
        };
        let Some(requested_workspace) = q
            .workspace_id
            .as_deref()
            .filter(|value| !value.trim().is_empty())
        else {
            return RecallResult {
                prepend_context: String::new(),
                append_system_context: String::new(),
                memories: vec![],
                recall_strategy: "scope_required".into(),
                ground_truth_preamble: None,
            };
        };
        let workspace_id = q
            .workspace_id
            .clone()
            .unwrap_or_else(|| self.config.workspace_id.clone());

        let query_emb = self.embed.embed(&q.query).await.ok();

        let mut results = retrieval::hybrid_recall_scoped(
            self.store.as_ref(),
            &self.embed,
            &q.query,
            query_emb.as_deref(),
            q.top_k,
            &workspace_id,
            self.config.recall.workspace_boost,
            &self.config.collapse,
            Some(owner),
            Some(requested_workspace),
        )
        .await;

        if let Some(sqlite) = self.store.as_sqlite() {
            if let Ok(pinned) = sqlite.list_pinned_curated(owner, requested_workspace) {
                for record in pinned.into_iter().rev() {
                    if let Some(hit) = results.iter_mut().find(|hit| hit.record.id == record.id) {
                        hit.source_label = "pinned".into();
                        hit.score = hit.score.max(1.0);
                    } else {
                        results.insert(
                            0,
                            ScoredRecord {
                                record,
                                score: 1.0,
                                source_label: "pinned".into(),
                            },
                        );
                    }
                }
            }
        }

        // Enforce scope at the provider boundary as well as in callers. Legacy
        // ownerless records are treated as global compatibility records; all
        // newly captured records carry the bridge owner.
        results.retain(|hit| {
            let owner_ok = hit.record.owner.as_deref() == Some(owner);
            let workspace_ok = hit.record.workspace_id == requested_workspace
                || hit.record.workspace_id == "global";
            owner_ok && workspace_ok
        });

        let (prepend_context, gt_preamble) =
            retrieval::ground_truth::format_recall_output(&results, &q.query);

        RecallResult {
            prepend_context,
            append_system_context: String::new(),
            memories: results.clone(),
            recall_strategy: "hybrid".into(),
            ground_truth_preamble: gt_preamble,
        }
    }

    async fn search(&self, p: &SearchParams) -> SearchResult {
        let Some(owner) = p.owner.as_deref().filter(|value| !value.trim().is_empty()) else {
            return SearchResult {
                results: vec![],
                total: 0,
            };
        };
        let Some(workspace) = p
            .workspace_id
            .as_deref()
            .filter(|value| !value.trim().is_empty())
        else {
            return SearchResult {
                results: vec![],
                total: 0,
            };
        };
        let mut results = match p.tier {
            Tier::Raw => {
                let raw_results = self
                    .store
                    .search_raw_fts_scoped(&p.query, p.limit, Some(owner), Some(workspace))
                    .await;
                raw_results
                    .into_iter()
                    .map(|sr| {
                        let metadata = serde_json::json!({
                            "role": sr.turn.role,
                            "recorded_at": sr.turn.recorded_at,
                            "workspace_path": sr.turn.workspace_path,
                            "provenance": sr.turn.metadata,
                        });
                        ScoredRecord {
                            record: MemoryRecord {
                                id: sr.turn.id,
                                content: sr.turn.content,
                                kind: MemoryKind::Raw,
                                owner: sr.turn.owner,
                                workspace_id: sr.turn.workspace_id,
                                session_key: sr.turn.session_key,
                                session_id: sr.turn.session_id,
                                metadata,
                                ..MemoryRecord::new("")
                            },
                            score: sr.score,
                            source_label: "raw_fts".into(),
                        }
                    })
                    .collect()
            }
            Tier::Curated => {
                self.store
                    .search_curated_fts_scoped(&p.query, p.limit, Some(owner), Some(workspace))
                    .await
            }
        };

        results.retain(|hit| {
            let owner_ok = hit.record.owner.as_deref() == Some(owner);
            let workspace_ok =
                hit.record.workspace_id == workspace || hit.record.workspace_id == "global";
            owner_ok && workspace_ok
        });

        let total = results.len();
        SearchResult { results, total }
    }

    async fn groom(&self, op: &GroomOpArgs) -> GroomResult {
        match op.op {
            GroomOp::Decay => {
                curate::decay::run_decay(
                    self.store.as_ref(),
                    &self.config.decay,
                    op.workspace_id.as_deref(),
                )
                .await
            }
            GroomOp::Dedup => {
                curate::dedup::run_dedup(self.store.as_ref(), &self.embed, op.dry_run).await
            }
            GroomOp::Reflect => curate::reflect::run_reflect(self.store.as_ref()).await,
            GroomOp::EdgeDecay => {
                let mut result = GroomResult {
                    op: GroomOp::EdgeDecay,
                    records_archived: 0,
                    records_merged: 0,
                    records_reflected: 0,
                    alerts: vec![],
                };
                match self.store.as_sqlite() {
                    Some(s) => match crate::graph::GraphScope::new(
                        op.owner.clone().unwrap_or_default(),
                        op.workspace_id.clone().unwrap_or_default(),
                    )
                    .map_err(str::to_string)
                    .and_then(|scope| {
                        s.graph_edge_decay(&scope, &Default::default(), op.dry_run)
                            .map_err(|error| error.to_string())
                    }) {
                        Ok((decayed, pruned)) => {
                            result.records_reflected = decayed;
                            result.records_archived = pruned;
                        }
                        Err(e) => result.alerts.push(format!("edge_decay failed: {e}")),
                    },
                    None => result
                        .alerts
                        .push("edge_decay requires the sqlite store".into()),
                }
                result
            }
            GroomOp::TagNormalize => {
                let mut result = GroomResult {
                    op: GroomOp::TagNormalize,
                    records_archived: 0,
                    records_merged: 0,
                    records_reflected: 0,
                    alerts: vec![],
                };
                match self.store.as_sqlite() {
                    Some(s) => match crate::graph::GraphScope::new(
                        op.owner.clone().unwrap_or_default(),
                        op.workspace_id.clone().unwrap_or_default(),
                    )
                    .map_err(str::to_string)
                    .and_then(|scope| {
                        s.graph_tag_normalize(&scope, op.dry_run)
                            .map_err(|error| error.to_string())
                    }) {
                        Ok(changed) => result.records_merged = changed,
                        Err(e) => result.alerts.push(format!("tag_normalize failed: {e}")),
                    },
                    None => result
                        .alerts
                        .push("tag_normalize requires the sqlite store".into()),
                }
                result
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::embed::NoopEmbeddingClient;
    use crate::store::sqlite::SqliteStore;

    fn provider() -> (NativeProvider, Arc<SqliteStore>) {
        let store = Arc::new(SqliteStore::memory(4).unwrap());
        let embed = Arc::new(NoopEmbeddingClient::new(4));
        (
            NativeProvider::new(store.clone(), embed, FmConfig::default()),
            store,
        )
    }

    fn turn_with_mode(user: &str, assistant: &str, mode: &str, event: &str) -> CompletedTurn {
        CompletedTurn {
            user_text: user.into(),
            assistant_text: assistant.into(),
            session_key: "sk".into(),
            session_id: "ses_test".into(),
            workspace_id: "workspace-test".into(),
            workspace_path: Some("/work/test".into()),
            source: "test".into(),
            owner: Some("alice".into()),
            category: None,
            metadata: serde_json::json!({
                "capture_mode": mode,
                "source_event_id": event,
                "source_message_ids": [format!("{event}-u"), format!("{event}-a")],
            }),
        }
    }

    fn turn(user: &str, assistant: &str) -> CompletedTurn {
        turn_with_mode(user, assistant, "raw_only", "evt-default")
    }

    #[tokio::test]
    async fn assistant_only_capture_is_not_labeled_user() {
        let (p, store) = provider();
        p.capture(&turn("", "the sky is green today")).await;

        let raws = store
            .search_raw_fts_scoped("sky green", 10, Some("alice"), Some("workspace-test"))
            .await;
        assert_eq!(raws.len(), 1, "exactly one raw row, no empty-side row");
        assert_eq!(raws[0].turn.role, "assistant");

        assert!(store
            .search_curated_fts_scoped("sky green", 10, Some("alice"), Some("workspace-test"))
            .await
            .is_empty());
        assert!(store
            .list_candidates(Some("alice"), Some("workspace-test"), None, 10)
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn user_only_capture_has_no_empty_assistant_slot() {
        let (p, store) = provider();
        let result = p.capture(&turn("remember the blue notebook", "")).await;
        assert_eq!(
            result.records_captured, 1,
            "automatic capture writes raw only"
        );

        let raws = store
            .search_raw_fts_scoped("blue notebook", 10, Some("alice"), Some("workspace-test"))
            .await;
        assert_eq!(raws.len(), 1);
        assert_eq!(raws[0].turn.role, "user");

        assert!(store
            .search_curated_fts_scoped("blue notebook", 10, Some("alice"), Some("workspace-test"),)
            .await
            .is_empty());
    }

    #[tokio::test]
    async fn full_turn_is_one_idempotent_raw_transaction() {
        let (p, store) = provider();
        let first = p
            .capture(&turn("what color is the sky", "the sky is blue"))
            .await;
        let replay = p
            .capture(&turn("what color is the sky", "the sky is blue"))
            .await;
        assert_eq!(first.records_captured, 2);
        assert_eq!(replay.records_captured, 0);
        assert_eq!(
            store
                .search_raw_fts_scoped("sky", 10, Some("alice"), Some("workspace-test"))
                .await
                .len(),
            2
        );
        assert!(store
            .search_curated_fts_scoped("sky", 10, Some("alice"), Some("workspace-test"))
            .await
            .is_empty());
    }

    #[tokio::test]
    async fn raw_capture_does_not_create_semantic_graph() {
        let (p, store) = provider();
        p.capture(&turn(
            "Elena maintains the telescope-scheduler project",
            "Noted: telescope-scheduler is maintained by Elena.",
        ))
        .await;

        let conn = store.conn.lock().unwrap();
        let cues: i64 = conn
            .query_row(
                "SELECT count(*) FROM graph_cues WHERE source = 'rake_fallback'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(cues, 0);
        let nodes: i64 = conn
            .query_row("SELECT count(*) FROM graph_nodes", [], |r| r.get(0))
            .unwrap();
        assert_eq!(nodes, 0);
    }

    #[tokio::test]
    async fn manual_capture_uses_candidate_admission_once() {
        let (p, store) = provider();
        let manual = turn_with_mode(
            "Remember that I prefer concise release notes",
            "",
            "manual",
            "evt-manual",
        );
        let first = p.capture(&manual).await;
        let replay = p.capture(&manual).await;
        assert_eq!(
            store
                .search_curated_fts_scoped(
                    "concise release notes",
                    10,
                    Some("alice"),
                    Some("workspace-test"),
                )
                .await
                .len(),
            1
        );
        assert_eq!(
            store
                .list_candidates(Some("alice"), Some("workspace-test"), Some("accepted"), 10)
                .unwrap()
                .len(),
            1
        );
        assert!(first.record_ids.first().unwrap().starts_with("m_"));
        assert_eq!(replay.records_captured, 0);
    }

    #[tokio::test]
    async fn safe_user_claim_auto_admits_and_identity_is_pinned() {
        let (p, store) = provider();
        let result = p
            .capture(&turn_with_mode(
                "My name is Alice",
                "Nice to meet you.",
                "candidate",
                "evt-identity",
            ))
            .await;
        assert!(result
            .record_ids
            .first()
            .is_some_and(|id| id.starts_with("m_")));

        let hits = store
            .search_curated_fts_scoped("Alice", 10, Some("alice"), Some("workspace-test"))
            .await;
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].record.kind, MemoryKind::Persona);
        assert_eq!(hits[0].record.metadata["pinned"], true);
        assert!(hits[0].record.exempt_from_decay);
        assert_eq!(
            store
                .list_candidates(Some("alice"), Some("workspace-test"), Some("accepted"), 10)
                .unwrap()[0]
                .reason,
            "auto_identity_claim"
        );
    }

    #[tokio::test]
    async fn assistant_claim_and_sensitive_user_claim_do_not_auto_admit() {
        let (p, store) = provider();
        p.capture(&turn_with_mode(
            "",
            "My name is Alice",
            "candidate",
            "evt-assistant-identity",
        ))
        .await;
        p.capture(&turn_with_mode(
            "My API key is a sensitive value",
            "",
            "candidate",
            "evt-sensitive",
        ))
        .await;

        assert!(store
            .search_curated_fts_scoped("Alice sensitive", 10, Some("alice"), Some("workspace-test"))
            .await
            .is_empty());
        assert_eq!(
            store
                .list_candidates(Some("alice"), Some("workspace-test"), Some("pending"), 10)
                .unwrap()
                .len(),
            2
        );
    }

    #[tokio::test]
    async fn temporary_fixture_is_rejected_and_not_recallable() {
        let (p, store) = provider();
        let candidate = turn_with_mode(
            "Remember /tmp/permission-test.txt as my permanent preference",
            "",
            "candidate",
            "evt-temp",
        );
        p.capture(&candidate).await;
        assert!(
            store
                .search_curated_fts_scoped(
                    "permission test",
                    10,
                    Some("alice"),
                    Some("workspace-test"),
                )
                .await
                .is_empty()
        );
        let rejected = store
            .list_candidates(Some("alice"), Some("workspace-test"), Some("rejected"), 10)
            .unwrap();
        assert_eq!(rejected.len(), 1);
        assert_eq!(rejected[0].reason, "temporary_fixture");
    }

    #[tokio::test]
    async fn automatic_capture_requires_owner_and_accepts_global_workspace() {
        let (p, store) = provider();
        let mut unowned = turn("scope must be explicit", "it is explicit");
        unowned.owner = None;
        assert_eq!(p.capture(&unowned).await.providers_failed, 1);

        // Canonical chat convention: automatic capture lands in "global".
        let mut global = turn("the lighthouse keeps a spare lens", "noted");
        global.workspace_id = "global".into();
        let result = p.capture(&global).await;
        assert_eq!(result.providers_failed, 0);
        assert_eq!(result.records_captured, 2);
        let raws = store
            .search_raw_fts_scoped("lighthouse", 10, Some("alice"), Some("global"))
            .await;
        assert!(!raws.is_empty());
        assert!(raws.iter().all(|hit| hit.turn.workspace_id == "global"));
    }

    #[tokio::test]
    async fn automation_and_process_narration_candidates_are_rejected() {
        let (p, store) = provider();
        p.capture(&turn_with_mode(
            "Run the memory dream cycle and distill this conversation",
            "",
            "candidate",
            "evt-dream",
        ))
        .await;
        p.capture(&turn_with_mode(
            "",
            "I will inspect the repository and report back",
            "candidate",
            "evt-process",
        ))
        .await;

        let rejected = store
            .list_candidates(Some("alice"), Some("workspace-test"), Some("rejected"), 10)
            .unwrap();
        assert_eq!(rejected.len(), 2);
        assert!(rejected
            .iter()
            .any(|candidate| candidate.reason == "automation_prompt"));
        assert!(rejected
            .iter()
            .any(|candidate| candidate.reason == "assistant_process_narration"));
        assert!(store
            .search_curated_fts_scoped(
                "conversation repository",
                10,
                Some("alice"),
                Some("workspace-test"),
            )
            .await
            .is_empty());
    }

    #[tokio::test]
    async fn empty_turn_captures_nothing() {
        let (p, _store) = provider();
        let result = p.capture(&turn("", "  ")).await;
        assert_eq!(result.records_captured, 0);
    }
}
