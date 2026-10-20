//! Root-owned persisted-query qualification. Synthetic temporary SQLite and
//! canonical authority only; no app, native service, embedding or model runs.
use fm_core::code::{producer_fingerprint, CodeWorkTicket, WorkspaceBinding};
use fm_core::code_evidence::{SourceClaim, SourcePredicate, ValidationState};
use fm_core::code_query::*;
use fm_core::graph::GraphScope;
use fm_core::store::sqlite::SqliteStore;
use rusqlite::{params, Connection};
use std::path::PathBuf;
use std::sync::{atomic::AtomicBool, Mutex};
use std::time::Instant;

static AUTHORITY: Mutex<()> = Mutex::new(());
const SECRET: [u8; 32] = [29; 32];

struct Fixture {
    directory: PathBuf,
    authority: PathBuf,
    database: PathBuf,
    old_authority: Option<std::ffi::OsString>,
    store: SqliteStore,
}
impl Fixture {
    fn new() -> Self {
        let directory = std::env::temp_dir().join(format!("openclank-s05-store-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&directory).unwrap();
        let directory = directory.canonicalize().unwrap();
        let authority = directory.join("authority.db");
        let database = directory.join("fm.db");
        let conn = Connection::open(&authority).unwrap();
        conn.execute_batch("CREATE TABLE file_policy_subject_aliases(username TEXT,subject_id TEXT,is_admin INTEGER);
            CREATE TABLE file_policy_workspaces(id TEXT,owner_subject_id TEXT,location_id TEXT,relative_folder TEXT,archived INTEGER,revision INTEGER);
            CREATE TABLE file_policy_locations(id TEXT,canonical_path TEXT,capabilities_json TEXT,enabled INTEGER,availability TEXT);
            CREATE TABLE file_policy_meta(singleton INTEGER,generation INTEGER);
            CREATE TABLE file_policy_bindings(binding_class TEXT,subject_kind TEXT,subject_id TEXT,location_id TEXT,status TEXT,expires_unix_ms INTEGER,workspace_id TEXT,chat_id TEXT,lifetime TEXT,remaining_uses INTEGER,capabilities_json TEXT);
            INSERT INTO file_policy_meta VALUES(1,1);").unwrap();
        let old_authority = std::env::var_os("OPEN_CLANK_AUTHORITY_DB_PATH");
        std::env::set_var("OPEN_CLANK_AUTHORITY_DB_PATH", &authority);
        let store = SqliteStore::new(database.to_str().unwrap(), 4).unwrap();
        Self { directory, authority, database, old_authority, store }
    }
    fn workspace(&self, owner: &str, name: &str) -> (GraphScope, WorkspaceBinding) {
        let root = self.directory.join(name);
        std::fs::create_dir(&root).unwrap();
        let subject = format!("subject-{owner}");
        let location = format!("location-{name}");
        let scope = GraphScope::new(owner, name).unwrap();
        let conn = Connection::open(&self.authority).unwrap();
        conn.execute("INSERT INTO file_policy_subject_aliases SELECT ?1,?2,0 WHERE NOT EXISTS(SELECT 1 FROM file_policy_subject_aliases WHERE username=?1)", params![owner, subject]).unwrap();
        conn.execute("INSERT INTO file_policy_workspaces VALUES(?1,?2,?3,'',0,1)", params![name, subject, location]).unwrap();
        conn.execute("INSERT INTO file_policy_locations VALUES(?1,?2,'[\"read\"]',1,'available')", params![location, root.to_str().unwrap()]).unwrap();
        for class in ["people", "agent"] {
            conn.execute("INSERT INTO file_policy_bindings VALUES(?1,'user',?2,?3,'active',NULL,NULL,NULL,'always',NULL,'[\"read\"]')", params![class, subject, location]).unwrap();
        }
        let binding = WorkspaceBinding { owner: owner.into(), owner_subject_id: subject,
            workspace_id: name.into(), location_id: location, workspace_revision: 1,
            policy_generation: 1, root, origin: "agent".into(), subscriber_id: "root-s05-test".into(), authority_expires_unix_ms: None };
        (scope, binding)
    }
    fn index(&self, scope: &GraphScope, binding: &WorkspaceBinding) {
        let state = self.store.code_workspace_prepare(scope, binding, false).unwrap();
        let ticket = CodeWorkTicket { checkout_id: state["checkout_id"].as_str().unwrap().into(),
            admission_epoch: state["admission_epoch"].as_i64().unwrap(), config_fingerprint: producer_fingerprint(),
            policy_generation: binding.policy_generation, workspace_revision: binding.workspace_revision };
        self.store.code_index_bound(scope, binding, &ticket).unwrap();
    }
    fn query(&self, scope: &GraphScope, binding: &WorkspaceBinding, query: &CodeQuery) -> CodeQueryResult {
        self.store.code_query(scope, binding, query, &AtomicBool::new(false), &SECRET).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Some(value) = &self.old_authority { std::env::set_var("OPEN_CLANK_AUTHORITY_DB_PATH", value); }
        else { std::env::remove_var("OPEN_CLANK_AUTHORITY_DB_PATH"); }
        // Only the exact uniquely-created synthetic fixture tree is removed.
        assert!(self.directory.file_name().unwrap().to_string_lossy().starts_with("openclank-s05-store-"));
        let _ = std::fs::remove_dir_all(&self.directory);
    }
}
fn write(binding: &WorkspaceBinding, path: &str, body: &str) {
    std::fs::write(binding.root.join(path), body).unwrap();
}
fn search(text: &str, channels: Vec<Channel>) -> CodeQuery {
    CodeQuery { query: Some(text.into()), channels, filter: QueryFilter { kinds: vec!["declaration".into()], ..Default::default() }, ..Default::default() }
}
fn labels(result: &CodeQueryResult) -> Vec<String> {
    result.hits.iter().filter_map(|h| h.label.clone()).collect()
}

#[test]
fn persisted_enabled_channels_solve_named_tasks_with_witnesses_and_scope() {
    let _lock = AUTHORITY.lock().unwrap();
    let f = Fixture::new();
    let (scope, binding) = f.workspace("alice", "quality");
    write(&binding, "source.py", "def HTTPRequestParser():\n return 1\ndef café():\n return 2\ndef target():\n return 3\ndef caller():\n return target()\ndef carton():\n return 4\ndef carbon():\n return 5\n");
    write(&binding, "shadow.py", "def target():\n return 99\n");
    f.index(&scope, &binding);
    let cases = [("HTTPRequestParser", Channel::Exact, "HTTPRequestParser"), ("http request parser", Channel::Identifier, "HTTPRequestParser"), ("café", Channel::Exact, "café"), ("targat", Channel::Fuzzy, "target")];
    for (text, channel, expected) in cases {
        let mut query = search(text, vec![channel]);
        query.filter.rel_path = Some("source.py".into());
        let result = f.query(&scope, &binding, &query);
        assert_eq!(labels(&result), vec![expected], "task {text}");
        let hit = &result.hits[0];
        assert!(hit.matches.iter().any(|m| m.channel == channel && !m.explanation.is_empty()));
        assert!(!hit.current_source_verified, "Metadata is not current disk proof");
        assert_eq!(hit.source.as_ref().unwrap().rel_path, "source.py");
        if channel == Channel::Fuzzy { assert_eq!(hit.evidence_state, "suggestion"); }
        assert!(result.cost.metadata_records_validated > 0 && result.cost.metadata_bytes > 0);
    }
    for text in ["tgt", "totally_unrelated", "arget"] {
        assert!(f.query(&scope, &binding, &search(text, vec![Channel::Fuzzy])).hits.is_empty());
    }
    let homonyms = f.query(&scope, &binding, &search("carcon", vec![Channel::Fuzzy]));
    assert_eq!(homonyms.hits.len(), 2);
    assert!(homonyms.hits.iter().all(|h| h.evidence_state == "suggestion"));
    let mut scoped = search("target", vec![Channel::Exact, Channel::Semantic]);
    scoped.filter.rel_path = Some("shadow.py".into()); scoped.budgets.rows = 1;
    let selected = f.query(&scope, &binding, &scoped);
    assert_eq!(selected.hits[0].source.as_ref().unwrap().rel_path, "shadow.py");
    assert!(selected.degraded && selected.capabilities.iter().any(|c| c.channel == Channel::Semantic && !c.available));
    let mut source_query = search("target", vec![Channel::Exact]); source_query.filter.rel_path = Some("source.py".into());
    let target = f.query(&scope, &binding, &source_query).hits[0].id.clone();
    let caller = f.query(&scope, &binding, &search("caller", vec![Channel::Exact])).hits[0].id.clone();
    let path = f.query(&scope, &binding, &CodeQuery { operation: Operation::Path, seed_ids: vec![caller.clone()], target_id: Some(target.clone()), fields: vec![Field::Source, Field::Witnesses], ..Default::default() });
    assert_eq!(path.paths.len(), 1);
    assert_eq!(path.paths[0].node_ids, vec![caller, target]);
    assert_eq!(path.paths[0].edges.len(), 1);
    assert_eq!(path.paths[0].edges[0].resolution_state, "exact");
    assert!(!path.paths[0].edges[0].witnesses.as_ref().unwrap().is_empty());
    let (other_scope, other_binding) = f.workspace("bob", "other");
    write(&other_binding, "source.py", "def target():\n return 'private-other'\n");
    f.index(&other_scope, &other_binding);
    let other = f.query(&other_scope, &other_binding, &search("target", vec![Channel::Exact]));
    assert_ne!(other.receipt.as_ref().unwrap().evidence.checkout_id, selected.receipt.as_ref().unwrap().evidence.checkout_id);
    assert_eq!(f.store.code_query(&other_scope, &binding, &scoped, &AtomicBool::new(false), &SECRET).unwrap_err().code, "denied");
    println!("S05 quality: 4 named exact/identifier/Unicode/typo tasks; 3 no-hit controls; 2 homonym suggestions; filtered source, unavailable semantic, directed witnessed path and cross-owner denial passed");
}

#[test]
fn persisted_restart_provenance_and_requested_work_fail_closed() {
    let _lock = AUTHORITY.lock().unwrap();
    let f = Fixture::new(); let (scope, binding) = f.workspace("alice", "persisted");
    write(&binding, "source.py", "def target():\n return 1\n"); f.index(&scope, &binding);
    let query = search("target", vec![Channel::Exact]); let before = f.query(&scope, &binding, &query);
    let reopened = SqliteStore::new(f.database.to_str().unwrap(), 4).unwrap();
    let after = reopened.code_query(&scope, &binding, &query, &AtomicBool::new(false), &SECRET).unwrap();
    assert_eq!(before, after);
    let mut tiny = query.clone(); tiny.budgets.work = 1;
    assert_eq!(reopened.code_query(&scope, &binding, &tiny, &AtomicBool::new(false), &SECRET).unwrap_err().code, "budget_exceeded");
    assert_eq!(reopened.code_query(&scope, &binding, &query, &AtomicBool::new(true), &SECRET).unwrap_err().code, "cancelled");
    let conn = Connection::open(&f.database).unwrap();
    conn.execute("UPDATE code_file_evidence SET source_hash=?1 WHERE owner=?2", params!["0".repeat(64), scope.owner]).unwrap();
    assert_eq!(reopened.code_query(&scope, &binding, &query, &AtomicBool::new(false), &SECRET).unwrap_err().code, "invalid_evidence");
}

#[test]
fn missing_read_fence_changes_on_erase_readmission_without_a_head() {
    let _lock = AUTHORITY.lock().unwrap();
    let f = Fixture::new(); let (scope, binding) = f.workspace("alice", "missing");
    let initial = f.store.code_read_fence(&scope, &binding, None).unwrap();
    assert!(f.store.code_workspace_checkout(&scope, &binding).unwrap().is_none());
    assert_eq!(f.store.code_query(&scope, &binding, &CodeQuery::default(), &AtomicBool::new(false), &SECRET).unwrap_err().code, "missing");
    assert_eq!(initial, f.store.code_read_fence(&scope, &binding, None).unwrap(), "a missing query cannot admit or mutate a workspace");
    f.store.code_workspace_remove(&scope, &binding).unwrap();
    assert_eq!(f.store.code_read_fence(&scope, &binding, None).unwrap_err().code, "erased");
    assert_eq!(f.store.code_query(&scope, &binding, &CodeQuery::default(), &AtomicBool::new(false), &SECRET).unwrap_err().code, "erased");
    f.store.code_workspace_prepare(&scope, &binding, true).unwrap();
    let readmitted = f.store.code_read_fence(&scope, &binding, None).unwrap();
    assert_ne!(initial, readmitted);
    assert_eq!(f.store.code_query(&scope, &binding, &CodeQuery::default(), &AtomicBool::new(false), &SECRET).unwrap_err().code, "missing");
    assert_eq!(readmitted, f.store.code_read_fence(&scope, &binding, None).unwrap());
}

#[test]
fn measured_source_validation_counts_two_passes_and_stale_observation() {
    let _lock = AUTHORITY.lock().unwrap();
    let f = Fixture::new(); let (scope, binding) = f.workspace("alice", "validation");
    let body = "def café():\n return 1\n"; write(&binding, "source.py", body); f.index(&scope, &binding);
    let result = f.query(&scope, &binding, &search("café", vec![Channel::Exact]));
    let source = result.hits[0].source.clone().unwrap();
    let text = body.as_bytes()[source.span.start_byte as usize..source.span.end_byte as usize].to_vec();
    let claim = SourceClaim { source, predicate: SourcePredicate::ExactText { text: String::from_utf8(text).unwrap() } };
    let receipt = &result.receipt.unwrap().evidence;
    let (current, bytes) = f.store.code_validate_source_measured(&scope, &binding, receipt, &claim);
    assert_eq!(current.unwrap().state, ValidationState::Current); assert_eq!(bytes, 2 * body.len());
    write(&binding, "source.py", &body.replace("return 1", "return 2"));
    let (stale, stale_bytes) = f.store.code_validate_source_measured(&scope, &binding, receipt, &claim);
    assert_eq!(stale.unwrap().state, ValidationState::Stale); assert!(stale_bytes >= body.len() && stale_bytes <= 2 * body.len());
    Connection::open(&f.authority).unwrap().execute("UPDATE file_policy_bindings SET expires_unix_ms=1", []).unwrap();
    let (denied, denied_bytes) = f.store.code_validate_source_measured(&scope, &binding, receipt, &claim);
    assert!(denied.is_err()); assert_eq!(denied_bytes, 0, "No successful source acquisition after expired grants");
}

#[test]
fn small_medium_large_persisted_query_baseline_reports_actual_costs() {
    let _lock = AUTHORITY.lock().unwrap();
    let f = Fixture::new();
    for count in [16usize, 128, 512] {
        let (scope, binding) = f.workspace("baseline", &format!("files-{count}"));
        let mut digest = blake3::Hasher::new();
        for i in 0..count {
            let path = format!("file_{i:04}.py"); let body = format!("def target_{i:04}():\n return {i}\n");
            digest.update(path.as_bytes()); digest.update(body.as_bytes()); write(&binding, &path, &body);
        }
        let started = Instant::now(); f.index(&scope, &binding); let index_us = started.elapsed().as_micros();
        let query = search("target_0000", vec![Channel::Exact, Channel::Identifier]);
        let started = Instant::now(); let cold = f.query(&scope, &binding, &query); let cold_us = started.elapsed().as_micros();
        assert_eq!(labels(&cold), vec!["target_0000"]);
        assert!(cold.cost.metadata_records_validated >= count);
        assert!(cold.cost.inspected_records <= query.budgets.work);
        assert_eq!(cold.cost.files_bytes_read, 0); assert_eq!(cold.cost.validation_bytes_read, 0);
        assert_eq!(serde_json::to_vec(&cold).unwrap().len(), cold.cost.serialized_bytes);
        assert!(cold.cost.serialized_bytes <= query.budgets.tokens);
        let _warmup = f.query(&scope, &binding, &query);
        let mut samples = Vec::new();
        for _ in 0..20 {
            let now = Instant::now(); let warm = f.query(&scope, &binding, &query); samples.push(now.elapsed().as_micros());
            assert_eq!(warm, cold, "Warm query must retain exact IDs, witnesses, receipt and costs");
        }
        samples.sort_unstable();
        let mut exhausted = query.clone(); exhausted.budgets.work = count - 1;
        assert_eq!(f.store.code_query(&scope, &binding, &exhausted, &AtomicBool::new(false), &SECRET).unwrap_err().code, "budget_exceeded");
        println!("S05_BASELINE files={count} fixture_blake3={} index_us={index_us} cold_query_us={cold_us} warmup=1 samples=20 p50_us={} p95_nearest_rank_us={} max_us={} metadata_bytes={} metadata_records={} inspected_records={} envelope_bytes={}",
            digest.finalize().to_hex(), samples[9], samples[18], samples[19], cold.cost.metadata_bytes, cold.cost.metadata_records_validated, cold.cost.inspected_records, cold.cost.serialized_bytes);
    }
}

/// C08/S08 preparation only: one observed lifecycle sequence per size, not a
/// product timeout or latency percentile. All paths belong to this fixture.
#[test]
fn measured_incremental_rename_delete_erase_and_readmit_keep_current_facts() {
    let _lock = AUTHORITY.lock().unwrap();
    let f = Fixture::new();
    for count in [16usize, 128, 512] {
        let (scope, binding) = f.workspace("lifecycle-baseline", &format!("lifecycle-{count}"));
        for i in 0..count {
            write(&binding, &format!("file_{i:04}.py"), &format!("def target_{i:04}():\n return {i}\n"));
        }
        let started = Instant::now(); f.index(&scope, &binding); let initial_us = started.elapsed().as_micros();
        let query = search("target_0000", vec![Channel::Exact]);
        let initial = f.query(&scope, &binding, &query);
        assert_eq!(labels(&initial), vec!["target_0000"]);
        let initial_locator = initial.hits[0].source.as_ref().unwrap();

        // Same byte length: changed bytes, rather than size, invalidate source.
        write(&binding, "file_0000.py", "def target_0000():\n return 9\n");
        let started = Instant::now(); f.index(&scope, &binding); let edit_us = started.elapsed().as_micros();
        let edited = f.query(&scope, &binding, &query);
        assert_eq!(labels(&edited), vec!["target_0000"]);
        assert_ne!(edited.hits[0].source.as_ref().unwrap().source_hash, initial_locator.source_hash);
        assert_ne!(edited.receipt, initial.receipt);

        std::fs::rename(binding.root.join("file_0000.py"), binding.root.join("moved.py")).unwrap();
        let started = Instant::now(); f.index(&scope, &binding); let rename_us = started.elapsed().as_micros();
        let moved = f.query(&scope, &binding, &query);
        assert_eq!(labels(&moved), vec!["target_0000"]);
        assert_eq!(moved.hits[0].source.as_ref().unwrap().rel_path, "moved.py");
        let mut old_path = query.clone(); old_path.filter.rel_path = Some("file_0000.py".into());
        assert!(f.query(&scope, &binding, &old_path).hits.is_empty());

        std::fs::remove_file(binding.root.join("moved.py")).unwrap();
        let started = Instant::now(); f.index(&scope, &binding); let delete_us = started.elapsed().as_micros();
        assert!(f.query(&scope, &binding, &query).hits.is_empty());
        let survivor = search(&format!("target_{:04}", count - 1), vec![Channel::Exact]);
        let before_erase = f.query(&scope, &binding, &survivor);
        assert_eq!(before_erase.hits.len(), 1);

        let started = Instant::now(); f.store.code_workspace_remove(&scope, &binding).unwrap(); let erase_us = started.elapsed().as_micros();
        assert_eq!(f.store.code_query(&scope, &binding, &survivor, &AtomicBool::new(false), &SECRET).unwrap_err().code, "erased");
        assert!(f.store.code_workspace_prepare(&scope, &binding, false).is_err(), "ordinary reopen cannot re-admit erased code");
        let started = Instant::now();
        f.store.code_workspace_prepare(&scope, &binding, true).unwrap();
        f.index(&scope, &binding);
        let readmit_rebuild_us = started.elapsed().as_micros();
        let rebuilt = f.query(&scope, &binding, &survivor);
        assert_eq!(labels(&rebuilt), labels(&before_erase));
        assert_eq!(rebuilt.hits[0].source.as_ref().unwrap().source_hash, before_erase.hits[0].source.as_ref().unwrap().source_hash);
        assert!(rebuilt.receipt.as_ref().unwrap().evidence.admission_epoch > before_erase.receipt.as_ref().unwrap().evidence.admission_epoch);
        assert!(f.query(&scope, &binding, &query).hits.is_empty());
        println!("S08_LIFECYCLE files={count} initial_us={initial_us} same_size_edit_us={edit_us} rename_us={rename_us} delete_us={delete_us} erase_us={erase_us} explicit_readmit_rebuild_us={readmit_rebuild_us} source_facts_verified=true");
    }
}
