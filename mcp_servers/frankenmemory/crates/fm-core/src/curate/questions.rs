//! Open-question (kind=unknown) lifecycle: the fuzzy matcher, the
//! passive-resolution pass that closes questions when admitted content
//! answers them, and the groom op that promotes a question asked in
//! enough workspaces to global scope.
//!
//! Called ONLY from admission sites (candidate accept, capture direct
//! admission, authored ingest) and the groom dispatcher — never from
//! upsert_curated, so maintenance passes (decay/dedup/reflect) can
//! never resolve or promote anything.

use std::collections::BTreeSet;

use crate::record::*;
use crate::store::MemoryStore;

/// One threshold for both promotion ("same question?") and passive
/// resolution ("does this content answer it?"). Conservative on
/// purpose: a false resolve silently kills a question, so prefer
/// missing over misfiring. Deterministic v1 — no embeddings.
pub const QUESTION_MATCH_THRESHOLD: f32 = 0.6;

/// Distinct workspaces the same question must be open in before it
/// merges into one global record.
pub const PROMOTE_WORKSPACES_MIN: usize = 3;

const STOPWORDS: &[&str] = &[
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "do", "does", "did", "have", "has",
    "had", "what", "whats", "who", "whos", "whom", "where", "when", "which", "how", "why", "of",
    "for", "to", "in", "on", "at", "by", "with", "about", "my", "your", "yours", "our", "their",
    "his", "her", "its", "it", "this", "that", "these", "those", "and", "or", "not", "no", "yes",
    "i", "im", "ive", "id", "you", "we", "they", "he", "she", "me", "us", "them", "s", "t", "re",
    "ll", "ve", "d", "m",
];

fn tokens(text: &str) -> BTreeSet<String> {
    text.to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .filter(|token| !token.is_empty() && !STOPWORDS.contains(token))
        .map(|token| {
            // Light plural/possessive stem so "users name?" meets
            // "user's name is E" — one trailing s drops, double-s
            // ("address") stays.
            if token.len() > 3 && token.ends_with('s') && !token.ends_with("ss") {
                token[..token.len() - 1].to_string()
            } else {
                token.to_string()
            }
        })
        .collect()
}

/// Overlap coefficient (|A ∩ B| / min(|A|, |B|)) over stopword-free
/// tokens. Questions are fragments, so containment beats Jaccard: the
/// answer "the user's name is E" fully contains "user's name?" and
/// scores 1.0 instead of being diluted by its own extra words.
pub fn question_similarity(a: &str, b: &str) -> f32 {
    let a = tokens(a);
    let b = tokens(b);
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    let intersection = a.intersection(&b).count();
    intersection as f32 / a.len().min(b.len()) as f32
}

/// Passive resolution (U7c): when a NON-unknown record is admitted,
/// close every open question in its recall scope (workspace ∪ global)
/// that the new content answers. Archives with {resolved_by,
/// resolved_at} provenance and returns the resolved ids.
pub fn resolve_against_open_unknowns(
    store: &dyn MemoryStore,
    record: &MemoryRecord,
) -> Vec<String> {
    if record.kind == MemoryKind::Unknown || record.archived {
        return Vec::new();
    }
    let Some(owner) = record.owner.as_deref().filter(|o| !o.trim().is_empty()) else {
        return Vec::new();
    };
    let Some(sqlite) = store.as_sqlite() else {
        return Vec::new();
    };
    let Ok(open) = sqlite.list_open_unknowns(owner, Some(&record.workspace_id)) else {
        return Vec::new();
    };
    let now = chrono::Utc::now().to_rfc3339();
    let mut resolved = Vec::new();
    for question in open {
        if question_similarity(&question.content, &record.content) >= QUESTION_MATCH_THRESHOLD {
            let patch = serde_json::json!({
                "resolved_by": record.id,
                "resolved_at": now,
            });
            if sqlite.archive_curated_record(&question.id, &patch, Some("unknown"), None, None) {
                resolved.push(question.id);
            }
        }
    }
    if !resolved.is_empty() {
        sqlite.metric_add("unknowns_resolved_passively", resolved.len());
        tracing::info!(
            "passively resolved {} open question(s) via {}",
            resolved.len(),
            record.id
        );
    }
    resolved
}

/// Groom op promote_unknowns (U4): cluster an owner's open questions by
/// fuzzy match; a cluster open in >= PROMOTE_WORKSPACES_MIN distinct
/// workspaces merges into ONE global record (earliest copy is
/// canonical), the workspace copies archiving with {merged_into}.
/// A cluster that already has a global copy absorbs its workspace
/// duplicates regardless of count — the global question already exists,
/// the copies are noise. Idempotent: the global id is derived from the
/// canonical content, and archived copies leave the open set.
pub async fn run_promote_unknowns(
    store: &dyn MemoryStore,
    owner: Option<&str>,
    dry_run: bool,
) -> GroomResult {
    let mut result = GroomResult {
        op: GroomOp::PromoteUnknowns,
        records_archived: 0,
        records_merged: 0,
        records_reflected: 0,
        alerts: vec![],
    };
    let Some(owner) = owner.filter(|o| !o.trim().is_empty()) else {
        result.alerts.push("promote_unknowns requires owner".into());
        return result;
    };
    let Some(sqlite) = store.as_sqlite() else {
        result
            .alerts
            .push("promote_unknowns requires the sqlite store".into());
        return result;
    };
    let open = match sqlite.list_open_unknowns(owner, None) {
        Ok(open) => open,
        Err(error) => {
            result.alerts.push(format!("promote_unknowns: {error}"));
            return result;
        }
    };

    // Greedy clustering: earliest-first order makes the first member of
    // each cluster the canonical (oldest) copy.
    let mut clusters: Vec<Vec<&MemoryRecord>> = Vec::new();
    for record in &open {
        match clusters.iter_mut().find(|c| {
            question_similarity(&c[0].content, &record.content) >= QUESTION_MATCH_THRESHOLD
        }) {
            Some(cluster) => cluster.push(record),
            None => clusters.push(vec![record]),
        }
    }

    let now = chrono::Utc::now().to_rfc3339();
    for cluster in clusters {
        let global_copy = cluster.iter().find(|r| r.workspace_id == "global");
        let workspace_copies: Vec<&&MemoryRecord> = cluster
            .iter()
            .filter(|r| r.workspace_id != "global")
            .collect();
        if workspace_copies.is_empty() {
            continue;
        }
        let distinct_workspaces: BTreeSet<&str> = workspace_copies
            .iter()
            .map(|r| r.workspace_id.as_str())
            .collect();

        let global_id = match global_copy {
            Some(existing) => existing.id.clone(),
            None => {
                if distinct_workspaces.len() < PROMOTE_WORKSPACES_MIN {
                    continue;
                }
                let canonical = cluster[0];
                let global_id = format!(
                    "m_{}",
                    &blake3::hash(
                        format!("promoted\u{1f}{owner}\u{1f}{}", canonical.content).as_bytes()
                    )
                    .to_hex()[..24]
                );
                if !dry_run {
                    let mut promoted = canonical.clone();
                    promoted.id = global_id.clone();
                    promoted.workspace_id = "global".into();
                    promoted.workspace_path = None;
                    promoted.updated_at = now.clone();
                    let mut metadata = serde_json::json!({
                        "promoted_at": now,
                        "promoted_from": cluster.iter().map(|r| r.id.as_str()).collect::<Vec<_>>(),
                    });
                    if let (Some(object), Some(original)) =
                        (metadata.as_object_mut(), canonical.metadata.as_object())
                    {
                        for (key, value) in original {
                            object.entry(key.clone()).or_insert_with(|| value.clone());
                        }
                    }
                    promoted.metadata = metadata;
                    if !store.upsert_curated(&promoted, None).await {
                        result.alerts.push(format!(
                            "failed to write promoted global for {}",
                            canonical.id
                        ));
                        continue;
                    }
                }
                result.records_merged += 1;
                result.alerts.push(format!(
                    "promoted \"{}\" to global from {} workspaces",
                    cluster[0].content,
                    distinct_workspaces.len()
                ));
                global_id
            }
        };

        for copy in workspace_copies {
            if dry_run {
                result.records_archived += 1;
                continue;
            }
            let patch = serde_json::json!({"merged_into": global_id, "merged_at": now});
            if sqlite.archive_curated_record(&copy.id, &patch, Some("unknown"), None, None) {
                result.records_archived += 1;
            }
        }
    }
    if dry_run {
        result.alerts.push("dry_run: no writes performed".into());
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::sqlite::SqliteStore;
    use std::sync::Arc;

    fn question(content: &str, workspace: &str) -> MemoryRecord {
        let mut r = MemoryRecord::new(normalize_question(content));
        r.kind = MemoryKind::Unknown;
        r.source_type = SourceType::Human;
        r.owner = Some("alice".into());
        r.workspace_id = workspace.into();
        r
    }

    fn fact(content: &str, workspace: &str) -> MemoryRecord {
        let mut r = MemoryRecord::new(content);
        r.kind = MemoryKind::Fact;
        r.source_type = SourceType::Human;
        r.owner = Some("alice".into());
        r.workspace_id = workspace.into();
        r
    }

    #[test]
    fn matcher_matrix() {
        for (a, b, matches) in [
            ("user's name?", "What is the user's name?", true),
            ("user's name?", "the user's name is E", true),
            ("user's name?", "favorite color?", false),
            ("user's name?", "prefers green tea in the mornings", false),
            (
                "editor of choice?",
                "which editor does the user prefer?",
                false,
            ),
            (
                "preferred shell?",
                "the user's preferred shell is zsh",
                true,
            ),
            ("", "anything", false),
            ("what is that?", "what is that?", false),
        ] {
            let similar = question_similarity(a, b) >= QUESTION_MATCH_THRESHOLD;
            assert_eq!(similar, matches, "{a:?} vs {b:?}");
        }
    }

    #[tokio::test]
    async fn promotion_needs_three_distinct_workspaces() {
        let store = Arc::new(SqliteStore::memory(4).unwrap());
        let mut first = question("user's name", "ws-a");
        first.created_at = "2026-07-17T00:00:01Z".into();
        store.upsert_curated(&first, None).await;
        let mut second = question("What is the user's name", "ws-b");
        second.created_at = "2026-07-17T00:00:02Z".into();
        store.upsert_curated(&second, None).await;

        let two = run_promote_unknowns(store.as_ref(), Some("alice"), false).await;
        assert_eq!(two.records_merged, 0);
        assert_eq!(two.records_archived, 0);

        let mut third = question("users name", "ws-c");
        third.created_at = "2026-07-17T00:00:03Z".into();
        store.upsert_curated(&third, None).await;
        let three = run_promote_unknowns(store.as_ref(), Some("alice"), false).await;
        assert_eq!(three.records_merged, 1, "one global promotion");
        assert_eq!(three.records_archived, 3, "all workspace copies archived");

        let open = store.list_open_unknowns("alice", None).unwrap();
        assert_eq!(open.len(), 1);
        assert_eq!(open[0].workspace_id, "global");
        assert_eq!(
            open[0].content, "user's name?",
            "earliest copy is canonical"
        );
        assert!(open[0].metadata["promoted_from"].is_array());

        // Idempotent: nothing left to do.
        let again = run_promote_unknowns(store.as_ref(), Some("alice"), false).await;
        assert_eq!(again.records_merged, 0);
        assert_eq!(again.records_archived, 0);

        // Archived copies carry merged_into provenance.
        let digest = store.digest("alice", "ws-a", false).unwrap();
        assert!(digest["open_questions"].as_array().unwrap().is_empty());
    }

    #[tokio::test]
    async fn promotion_scopes_by_owner_and_honors_dry_run() {
        let store = Arc::new(SqliteStore::memory(4).unwrap());
        for ws in ["ws-a", "ws-b", "ws-c"] {
            store
                .upsert_curated(&question("deploy cadence", ws), None)
                .await;
            let mut bobs = question("deploy cadence", ws);
            bobs.id = format!("bob_{ws}");
            bobs.owner = Some("bob".into());
            store.upsert_curated(&bobs, None).await;
        }
        let dry = run_promote_unknowns(store.as_ref(), Some("alice"), true).await;
        assert_eq!(dry.records_merged, 1);
        assert_eq!(dry.records_archived, 3);
        assert_eq!(
            store.list_open_unknowns("alice", None).unwrap().len(),
            3,
            "dry run writes nothing"
        );

        run_promote_unknowns(store.as_ref(), Some("alice"), false).await;
        assert_eq!(store.list_open_unknowns("alice", None).unwrap().len(), 1);
        assert_eq!(
            store.list_open_unknowns("bob", None).unwrap().len(),
            3,
            "bob untouched"
        );
    }

    #[tokio::test]
    async fn passive_resolve_closes_in_scope_only() {
        let store = Arc::new(SqliteStore::memory(4).unwrap());
        store
            .upsert_curated(&question("user's name", "ws-a"), None)
            .await;
        let mut copy_b = question("user's name", "ws-b");
        copy_b.id = "q_ws_b".into();
        store.upsert_curated(&copy_b, None).await;
        store
            .upsert_curated(&question("preferred shell", "ws-a"), None)
            .await;

        let answer = fact("the user's name is E", "ws-a");
        store.upsert_curated(&answer, None).await;
        let resolved = resolve_against_open_unknowns(store.as_ref(), &answer);
        assert_eq!(resolved.len(), 1, "only the matching ws-a question closes");

        let open = store.list_open_unknowns("alice", None).unwrap();
        let contents: Vec<&str> = open.iter().map(|r| r.content.as_str()).collect();
        assert!(
            contents.contains(&"preferred shell?"),
            "unrelated question survives"
        );
        assert_eq!(
            open.iter().filter(|r| r.workspace_id == "ws-b").count(),
            1,
            "workspace B's copy stays open"
        );

        // Provenance readable back through the record.
        let closed = store
            .get_curated_record(&resolved[0], "alice", "ws-a")
            .unwrap();
        assert!(closed.is_none(), "resolved question leaves the active set");
    }

    #[tokio::test]
    async fn passive_resolve_reaches_global_questions() {
        let store = Arc::new(SqliteStore::memory(4).unwrap());
        store
            .upsert_curated(&question("user's name", "global"), None)
            .await;
        let answer = fact("the user's name is E", "ws-a");
        store.upsert_curated(&answer, None).await;
        assert_eq!(
            resolve_against_open_unknowns(store.as_ref(), &answer).len(),
            1
        );
    }

    #[tokio::test]
    async fn unknown_admission_never_self_resolves() {
        let store = Arc::new(SqliteStore::memory(4).unwrap());
        store
            .upsert_curated(&question("user's name", "ws-a"), None)
            .await;
        let twin = question("What is the user's name", "ws-a");
        store.upsert_curated(&twin, None).await;
        assert!(resolve_against_open_unknowns(store.as_ref(), &twin).is_empty());
        assert_eq!(store.list_open_unknowns("alice", None).unwrap().len(), 2);
    }

    #[tokio::test]
    async fn maintenance_passes_never_resolve_questions() {
        let store = Arc::new(SqliteStore::memory(4).unwrap());
        // The answering fact exists FIRST, then the question — so only a
        // hook misplaced into upsert_curated/maintenance could close it.
        let answer = fact("the user's name is E", "ws-a");
        store.upsert_curated(&answer, None).await;
        store
            .upsert_curated(&question("user's name", "ws-a"), None)
            .await;
        let near_dup = fact("the users name is E", "ws-a");
        store.upsert_curated(&near_dup, None).await;

        let embed: Arc<dyn crate::embed::EmbeddingClient> =
            Arc::new(crate::embed::NoopEmbeddingClient::new(4));
        crate::curate::dedup::run_dedup(store.as_ref(), &embed, false).await;
        crate::curate::reflect::run_reflect(store.as_ref()).await;
        crate::curate::decay::run_decay(store.as_ref(), &Default::default(), Some("ws-a")).await;

        let open = store.list_open_unknowns("alice", None).unwrap();
        assert_eq!(
            open.len(),
            1,
            "dedup/reflect/decay must never close a question"
        );
        assert_eq!(open[0].content, "user's name?");
    }

    #[tokio::test]
    async fn archive_surface_round_trips_provenance() {
        let store = Arc::new(SqliteStore::memory(4).unwrap());
        let q = question("user's name", "ws-a");
        store.upsert_curated(&q, None).await;

        // Kind guard: a fact cannot be "resolved".
        let f = fact("the sky is blue", "ws-a");
        store.upsert_curated(&f, None).await;
        assert!(!store.archive_curated_record(
            &f.id,
            &serde_json::json!({"resolved_by": "x"}),
            Some("unknown"),
            None,
            None
        ));

        // Scope guard across the tool boundary.
        assert!(!store.archive_curated_record(
            &q.id,
            &serde_json::json!({"resolved_by": "x"}),
            Some("unknown"),
            Some("bob"),
            Some("ws-a")
        ));

        assert!(store.archive_curated_record(
            &q.id,
            &serde_json::json!({"resolved_by": "m_answer", "resolved_at": "2026-07-17T00:00:00Z"}),
            Some("unknown"),
            Some("alice"),
            Some("ws-a")
        ));
        // Double-archive is a no-op failure, not corruption.
        assert!(!store.archive_curated_record(
            &q.id,
            &serde_json::json!({}),
            Some("unknown"),
            None,
            None
        ));
        assert!(store.list_open_unknowns("alice", None).unwrap().is_empty());
    }
}
