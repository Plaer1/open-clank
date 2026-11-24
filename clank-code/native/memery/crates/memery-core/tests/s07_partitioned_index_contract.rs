//! Real file-backed partition publication and public readers. Synthetic corpus
//! only. Run this target serially: its single test temporarily isolates HOME
//! and authority; other integration targets run in separate processes.
#![cfg(unix)]
use memery_core::code::{producer_fingerprint, CodeWorkTicket, WorkspaceBinding};
use memery_core::code_evidence::{DeclarationEvidence, ResolutionEvidence, ResolutionState, SourceClaim, SourcePredicate, ValidationState};
use memery_core::code_query::{Channel, CodeQuery, Field, Operation, QueryFilter};
use memery_core::graph::GraphScope;
use memery_core::store::{code_evidence::{CodeEvidenceAvailability, CodeEvidenceKind, CodeEvidenceQuery}, sqlite::SqliteStore};
use rusqlite::{params, Connection};
use std::{ffi::OsString, path::{Path, PathBuf}, sync::atomic::AtomicBool};

struct Fixture { root: PathBuf, home_before: Option<OsString>, authority_before: Option<OsString> }
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("openclank-s07-partition-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap(); let root = root.canonicalize().unwrap();
        for child in ["home", "source"] { std::fs::create_dir(root.join(child)).unwrap(); }
        let home_before = std::env::var_os("HOME");
        let authority_before = std::env::var_os("OPEN_CLANK_AUTHORITY_DB_PATH");
        std::env::set_var("HOME", root.join("home"));
        std::env::set_var("OPEN_CLANK_AUTHORITY_DB_PATH", root.join("authority.sqlite"));
        Self { root, home_before, authority_before }
    }
    fn binding(&self) -> (GraphScope, WorkspaceBinding) {
        let authority = Connection::open(self.root.join("authority.sqlite")).unwrap();
        authority.execute_batch("CREATE TABLE file_policy_subject_aliases(username TEXT,subject_id TEXT,is_admin INTEGER);
            CREATE TABLE file_policy_workspaces(id TEXT,owner_subject_id TEXT,location_id TEXT,relative_folder TEXT,archived INTEGER,revision INTEGER);
            CREATE TABLE file_policy_locations(id TEXT,canonical_path TEXT,capabilities_json TEXT,enabled INTEGER,availability TEXT);
            CREATE TABLE file_policy_meta(singleton INTEGER,generation INTEGER);
            CREATE TABLE file_policy_bindings(binding_class TEXT,subject_kind TEXT,subject_id TEXT,location_id TEXT,status TEXT,expires_unix_ms INTEGER,workspace_id TEXT,chat_id TEXT,lifetime TEXT,remaining_uses INTEGER,capabilities_json TEXT);
            INSERT INTO file_policy_subject_aliases VALUES('alice','subject-alice',0);
            INSERT INTO file_policy_workspaces VALUES('workspace','subject-alice','location','',0,1);
            INSERT INTO file_policy_meta VALUES(1,1);
            INSERT INTO file_policy_bindings VALUES('people','user','subject-alice','location','active',NULL,NULL,NULL,'always',NULL,'[\"read\"]');
            INSERT INTO file_policy_bindings VALUES('agent','user','subject-alice','location','active',NULL,NULL,NULL,'always',NULL,'[\"read\"]');").unwrap();
        authority.execute("INSERT INTO file_policy_locations VALUES('location',?1,'[\"read\"]',1,'available')", params![self.root.join("source").to_str().unwrap()]).unwrap();
        (GraphScope::new("alice", "workspace").unwrap(), WorkspaceBinding { owner: "alice".into(), owner_subject_id: "subject-alice".into(),
            workspace_id: "workspace".into(), location_id: "location".into(), workspace_revision: 1, policy_generation: 1,
            root: self.root.join("source"), origin: "agent".into(), subscriber_id: "s07-partition".into(), authority_expires_unix_ms: None })
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        for (key, before) in [("HOME", &self.home_before), ("OPEN_CLANK_AUTHORITY_DB_PATH", &self.authority_before)] {
            if let Some(value) = before { std::env::set_var(key, value); } else { std::env::remove_var(key); }
        }
        assert!(self.root.file_name().unwrap().to_string_lossy().starts_with("openclank-s07-partition-"));
        let _ = std::fs::remove_dir_all(&self.root);
    }
}
fn ticket(store: &SqliteStore, scope: &GraphScope, binding: &WorkspaceBinding) -> CodeWorkTicket {
    let state = store.code_workspace_prepare(scope, binding, false).unwrap();
    CodeWorkTicket { checkout_id: state["checkout_id"].as_str().unwrap().into(), admission_epoch: state["admission_epoch"].as_i64().unwrap(),
        config_fingerprint: producer_fingerprint(), policy_generation: binding.policy_generation, workspace_revision: binding.workspace_revision }
}
fn cache_files(path: &Path, files: &mut Vec<PathBuf>) {
    if !path.exists() { return; }
    for entry in std::fs::read_dir(path).unwrap() {
        let entry = entry.unwrap(); let kind = entry.file_type().unwrap(); assert!(!kind.is_symlink());
        if kind.is_dir() { cache_files(&entry.path(), files); }
        else { assert!(kind.is_file()); files.push(entry.path()); }
    }
}

#[test]
fn partition_removed_counts_only_previously_successful_files_not_retained_successes() {
    let fixture=Fixture::new();let(scope,binding)=fixture.binding();
    for n in 0..129 {std::fs::write(binding.root.join(format!("f{n:03}.py")),format!("def filler_{n}(): return {n}\n")).unwrap();}
    for(path,text)in [("empty.py","# valid without source units\n"),("normal.py","def normal(): return 1\n"),("flip.py","def flip(): return 2\n"),("delete.py","def removed(): return 3\n")] {
        std::fs::write(binding.root.join(path),text).unwrap();
    }
    let store=SqliteStore::new(fixture.root.join("fm.sqlite").to_str().unwrap(),4).unwrap();
    let index=|scope:&GraphScope,binding:&WorkspaceBinding| {
        let work=ticket(&store,scope,binding);store.code_index_bound(scope,binding,&work).unwrap()
    };
    let first=index(&scope,&binding);
    assert_eq!(first.files_indexed,133);assert_eq!(first.files_removed,0);assert!(first.errors.is_empty());
    assert!(first.coverage.iter().any(|c|c.rel_path=="empty.py"&&c.status=="no_source_units"));
    assert!(first.coverage.iter().any(|c|c.rel_path=="normal.py"&&c.status=="indexed"));
    let head=store.code_index_head(&scope,&first.codebase).unwrap().unwrap();
    assert_eq!(head.source_snapshot.evidence_store_version,1,"must exercise actual >128-file partition path");
    let unchanged=index(&scope,&binding);
    assert_eq!(unchanged.files_indexed,0);assert_eq!(unchanged.files_unchanged,133);assert_eq!(unchanged.files_removed,0);

    // Separate scope with the same relative names and root. Publication in A
    // must neither count B's files nor replace its ready head/file rows.
    let other_scope=GraphScope::new("alice","workspace-two").unwrap();
    let mut other=binding.clone();other.workspace_id="workspace-two".into();other.subscriber_id="s03-other-scope".into();
    let authority=Connection::open(fixture.root.join("authority.sqlite")).unwrap();
    authority.execute("INSERT INTO file_policy_workspaces VALUES('workspace-two','subject-alice','location','',0,1)",[]).unwrap();
    drop(authority);
    let other_first=index(&other_scope,&other);assert_eq!(other_first.files_indexed,133);assert_eq!(other_first.files_removed,0);
    let other_head=store.code_index_head(&other_scope,&other_first.codebase).unwrap().unwrap().run_id;
    std::fs::remove_file(binding.root.join("delete.py")).unwrap();
    std::fs::write(binding.root.join("flip.py"),"def broken(:\n").unwrap();
    std::fs::write(binding.root.join("normal.py"),"# successful transition to zero source units\n").unwrap();
    let changed=index(&scope,&binding);
    assert_eq!(changed.files_indexed,1);assert_eq!(changed.files_unchanged,130);assert_eq!(changed.files_indexed+changed.files_unchanged,131);assert_eq!(changed.errors.len(),1);assert_eq!(changed.files_removed,2,"one real deletion plus one previously indexed parse-failure transition");
    for path in ["empty.py","normal.py"] {assert!(changed.coverage.iter().any(|c|c.rel_path==path&&c.status=="no_source_units"));}
    assert!(changed.coverage.iter().any(|c|c.rel_path=="flip.py"&&c.status=="parse_failed"));
    assert_eq!(store.code_index_head(&other_scope,&other_first.codebase).unwrap().unwrap().run_id,other_head);
    {
        let conn=Connection::open(fixture.root.join("fm.sqlite")).unwrap();
        for(scope,base,count)in [(&scope,&changed.codebase,131),(&other_scope,&other_first.codebase,133)] {
            assert_eq!(conn.query_row("SELECT count(*) FROM code_files f JOIN code_index_heads h ON f.source_generation=h.run_id WHERE h.owner=?1 AND h.workspace_id=?2 AND h.codebase=?3",params![scope.owner,scope.workspace_id,base],|r|r.get::<_,i64>(0)).unwrap(),count);
        }
    }
    let repeated=index(&scope,&binding);assert_eq!(repeated.files_indexed,0);assert_eq!(repeated.files_unchanged,131);assert_eq!(repeated.files_removed,0,"already absent/failed files are not removed twice");assert_eq!(repeated.errors.len(),1);
    std::fs::write(binding.root.join("flip.py"),"def recovered(): return 4\n").unwrap();
    let recovered=index(&scope,&binding);assert_eq!(recovered.files_indexed,1);assert_eq!(recovered.files_unchanged,131);assert_eq!(recovered.files_indexed+recovered.files_unchanged,132);assert_eq!(recovered.files_removed,0);assert!(recovered.errors.is_empty());
}

#[test]
fn memoized_query_keeps_policy_authority_cancellation_head_and_erase_fences(){
    use memery_core::code_metrics::{capture_code_metrics,Stage};
    let fixture=Fixture::new();let(scope,binding)=fixture.binding();
    for n in 0..129{std::fs::write(binding.root.join(format!("f{n:03}.py")),format!("def target_{n}(): return {n}\n")).unwrap();}
    let store=SqliteStore::new(fixture.root.join("fm.sqlite").to_str().unwrap(),4).unwrap();let work=ticket(&store,&scope,&binding);let indexed=store.code_index_bound(&scope,&binding,&work).unwrap();
    let request=CodeQuery{operation:Operation::Overview,..Default::default()};
    let execute=|request:&CodeQuery,cancel:&AtomicBool|store.code_query(&scope,&binding,request,cancel,&[37;32]);
    let first=execute(&request,&AtomicBool::new(false)).unwrap();
    let(warm,metrics)=capture_code_metrics(||execute(&request,&AtomicBool::new(false)));let warm=warm.unwrap();
    assert_eq!(serde_json::to_value(&first).unwrap(),serde_json::to_value(&warm).unwrap());
    assert_eq!(metrics.stages[Stage::GenerationAuditHit as usize].counters.calls,1);assert_eq!(metrics.stages[Stage::EligibleCountHit as usize].counters.calls,1);
    assert_eq!(execute(&request,&AtomicBool::new(true)).unwrap_err().code,"cancelled");
    std::fs::write(binding.root.join(".gitignore"),"f000.py\n").unwrap();
    assert_eq!(execute(&request,&AtomicBool::new(false)).unwrap_err().code,"reindex_required");std::fs::remove_file(binding.root.join(".gitignore")).unwrap();
    let authority=Connection::open(fixture.root.join("authority.sqlite")).unwrap();authority.execute("UPDATE file_policy_bindings SET status='revoked' WHERE binding_class='agent'",[]).unwrap();
    assert_eq!(execute(&request,&AtomicBool::new(false)).unwrap_err().code,"denied");
    authority.execute("UPDATE file_policy_bindings SET status='active' WHERE binding_class='agent'",[]).unwrap();assert!(execute(&request,&AtomicBool::new(false)).is_ok());
    // Unchanged verification keeps its head; staleness requires an actual
    // changed publication, not an extra invocation of indexing.
    std::fs::write(binding.root.join("f000.py"),"def changed_target(): return 99\n").unwrap();
    let old=CodeQuery{expected_receipt:first.receipt.clone(),..request.clone()};let work=ticket(&store,&scope,&binding);let changed=store.code_index_bound(&scope,&binding,&work).unwrap();assert_ne!(changed.run_id,indexed.run_id);assert_eq!(store.code_index_head(&scope,&indexed.codebase).unwrap().unwrap().run_id,changed.run_id);
    assert_eq!(execute(&old,&AtomicBool::new(false)).unwrap_err().code,"stale_cursor");assert!(execute(&request,&AtomicBool::new(false)).is_ok());
    store.code_remove(&scope,&indexed.codebase).unwrap();assert_eq!(execute(&request,&AtomicBool::new(false)).unwrap_err().code,"erased");
    store.code_workspace_prepare(&scope,&binding,true).unwrap();assert_eq!(execute(&request,&AtomicBool::new(false)).unwrap_err().code,"missing");
}

#[test]
fn real_partition_publication_reopen_current_policy_abort_and_cleanup() {
    let fixture = Fixture::new(); let (scope, binding) = fixture.binding();
    for number in 0..129 { std::fs::write(binding.root.join(format!("f{number:03}.py")), format!("def symbol_{number}():\n    return {number}\n")).unwrap(); }
    std::fs::write(binding.root.join("lib.py"), "def target():\n    return 1\n").unwrap();
    std::fs::write(binding.root.join("caller.py"), "from lib import target\ndef caller():\n    return target()\n").unwrap();
    std::fs::write(binding.root.join("broken.py"), "def broken(:\n").unwrap();
    for number in 1..6 {std::fs::write(binding.root.join(format!("broken{number}.py")),"def broken(:\n").unwrap();}
    let oversized=vec![b'#';512*1024+1];std::fs::write(binding.root.join("large.py"),&oversized).unwrap();
    std::fs::write(fixture.root.join("outside.py"),"def never_acquired(): pass\n").unwrap();
    std::os::unix::fs::symlink(fixture.root.join("outside.py"),binding.root.join("external.py")).unwrap();
    let database = fixture.root.join("fm.sqlite");
    let store = SqliteStore::new(database.to_str().unwrap(), 4).unwrap();
    let admitted = ticket(&store, &scope, &binding);
    let indexed = store.code_index_bound(&scope, &binding, &admitted).unwrap();
    assert_eq!(indexed.files_indexed, 131);
    assert_eq!(indexed.errors.len(), 6, "{:?}", indexed.errors);
    assert!(indexed.coverage.iter().any(|c| c.rel_path == "broken.py" && c.status == "parse_failed" && c.source_hash.is_some()));
    for (path,status) in [("large.py","oversize"),("external.py","outside_root")] {
        assert!(indexed.coverage.iter().any(|c|c.rel_path==path&&c.status==status&&c.source_hash.is_none()));
    }
    let head = store.code_index_head(&scope, &indexed.codebase).unwrap().unwrap();
    assert_eq!(head.source_snapshot.evidence_store_version, 1);
    assert_eq!(head.source_snapshot.evidence_cache_identity.len(), 64);
    let mut caches = Vec::new(); cache_files(&fixture.root.join("home"), &mut caches);
    assert_eq!(caches.len(), 1); assert!(caches[0].metadata().unwrap().len() > 0);
    assert!(caches[0].metadata().unwrap().len() <= 2 * 1024 * 1024 * 1024);
    let main = Connection::open(&database).unwrap();
    assert_eq!(main.query_row("SELECT count(*) FROM code_file_evidence", [], |r| r.get::<_, i64>(0)).unwrap(), 0);
    drop(main); drop(store);
    let store = SqliteStore::new(database.to_str().unwrap(), 4).unwrap();
    assert_eq!(store.code_index_head(&scope, &indexed.codebase).unwrap().unwrap().run_id, head.run_id);
    let query = |text: &str, channel| CodeQuery { query: Some(text.into()), channels: vec![channel],
        filter: QueryFilter { kinds: vec!["declaration".into()], ..Default::default() }, ..Default::default() };
    let exact = query("target", Channel::Exact);
    let execute = |request: &CodeQuery| store.code_query(&scope, &binding, request, &AtomicBool::new(false), &[37;32]);
    for (text, channel) in [("target", Channel::Exact), ("targat", Channel::Fuzzy)] {
        let result = execute(&query(text, channel)).unwrap();
        assert_eq!(result.hits.len(), 1, "{text}: {result:?}");
        assert_eq!(result.hits[0].label.as_deref(), Some("target"));
        assert!(!result.hits[0].current_source_verified);
        assert!(result.partial, "the syntax-failed selected source must remain explicit");
    }
    let target = execute(&exact).unwrap().hits[0].id.clone();
    let caller = execute(&query("caller", Channel::Exact)).unwrap().hits[0].id.clone();
    let path = execute(&CodeQuery { operation: Operation::Path, seed_ids: vec![caller], target_id: Some(target),
        fields: vec![Field::Source, Field::Witnesses], ..Default::default() }).unwrap();
    assert_eq!(path.paths.len(), 1);
    let receipt = store.code_evidence_receipt(&scope, &binding).unwrap();
    let declarations = store.code_evidence_page(&scope, &binding, &receipt, CodeEvidenceQuery { kind: CodeEvidenceKind::Declarations, rel_path: Some("lib.py".into()), ..Default::default() }).unwrap();
    assert_eq!(declarations.availability, CodeEvidenceAvailability::Partial);
    assert!(!declarations.current_source_verified);
    let declaration: DeclarationEvidence = serde_json::from_value(declarations.rows[0]["evidence"].clone()).unwrap();
    let claim = SourceClaim { source: declaration.source, predicate: SourcePredicate::ExactText { text: "def target():\n    return 1".into() } };
    let (valid, charged) = store.code_validate_source_measured(&scope, &binding, &receipt, &claim);
    assert_eq!(valid.unwrap().state, ValidationState::Current);
    assert_eq!(charged, 2 * std::fs::metadata(binding.root.join("lib.py")).unwrap().len() as usize);
    let resolutions = store.code_evidence_page(&scope, &binding, &receipt, CodeEvidenceQuery { kind: CodeEvidenceKind::Resolutions, rel_path: Some("caller.py".into()), ..Default::default() }).unwrap();
    let resolution: ResolutionEvidence = resolutions.rows.iter().map(|row| serde_json::from_value::<ResolutionEvidence>(row["evidence"].clone()).unwrap())
        .find(|r| r.state == ResolutionState::Exact && r.target_id.is_some()).expect("exact caller-to-target resolution");
    assert_eq!(store.code_validate_resolution(&scope, &binding, &receipt, &resolution).unwrap().state, ValidationState::Current);
    assert_eq!(store.code_validate_path(&scope, &binding, &receipt, &[resolution.occurrence_id.clone()]).unwrap().state, ValidationState::Current);
    // A stable scanner exclusion is not an unknown selected source. Neither
    // excluded body can enter the two-pass plan; boundary changes still stale it.
    std::fs::write(binding.root.join("large.py"),"def newly_eligible(): pass\n").unwrap();
    assert_eq!(store.code_validate_resolution(&scope,&binding,&receipt,&resolution).unwrap().state,ValidationState::Stale);
    std::fs::write(binding.root.join("large.py"),&oversized).unwrap();
    std::fs::remove_file(binding.root.join("external.py")).unwrap();
    assert_eq!(store.code_validate_path(&scope,&binding,&receipt,&[resolution.occurrence_id.clone()]).unwrap().state,ValidationState::Stale);
    std::os::unix::fs::symlink(fixture.root.join("outside.py"),binding.root.join("external.py")).unwrap();
    assert_eq!(store.code_validate_resolution(&scope,&binding,&receipt,&resolution).unwrap().state,ValidationState::Current);

    // Changed ignore policy rejects existing metadata before source reads.
    std::fs::write(binding.root.join(".gitignore"), "lib.py\n").unwrap();
    assert_eq!(execute(&exact).unwrap_err().code, "reindex_required");
    let (refused, bytes) = store.code_validate_source_measured(&scope, &binding, &receipt, &claim);
    assert_eq!(refused.unwrap_err().code, memery_core::store::code_evidence::CodeEvidenceErrorCode::ReindexRequired);
    assert_eq!(bytes, 0);
    // Deterministic scan-policy budget abort must preserve the prior head and
    // sidecar, never publish an empty/reduced successful corpus.
    std::fs::write(binding.root.join(".gitignore"), vec![b'x'; 65_537]).unwrap();
    let retry = ticket(&store, &scope, &binding);
    let error = store.code_index_bound(&scope, &binding, &retry).unwrap_err();
    assert!(error.contains("ignore_policy") || error.contains("exceeds"), "{error}");
    assert_eq!(store.code_index_head(&scope, &indexed.codebase).unwrap().unwrap().run_id, head.run_id);
    assert!(caches[0].exists());
    std::fs::remove_file(binding.root.join(".gitignore")).unwrap();
    assert_eq!(execute(&exact).unwrap().hits.len(), 1);
    assert_eq!(store.code_remove(&scope, &indexed.codebase).unwrap(), 131);
    assert!(!caches[0].exists());
    assert!(store.code_index_head(&scope, &indexed.codebase).unwrap().is_none());
    assert_eq!(store.code_remove(&scope, &indexed.codebase).unwrap(), 0);
}

#[test]
fn package_manifest_real_partition_source_to_query() {
    let fixture=Fixture::new(); let (scope,binding)=fixture.binding();
    for n in 0..129 { std::fs::write(binding.root.join(format!("f{n:03}.py")),"def f(): return 1\n").unwrap(); }
    std::fs::create_dir(binding.root.join("src")).unwrap();
    std::fs::write(binding.root.join("package.json"),r#"{"dependencies":{"react":"VERSION_SECRET"},"scripts":{"test":"SCRIPT_SECRET"}}"#).unwrap();
    std::fs::write(binding.root.join("src/main.ts"),"import React from \"react\";\nexport function render() { return React; }\n").unwrap();
    let store=SqliteStore::new(fixture.root.join("fm.sqlite").to_str().unwrap(),4).unwrap();
    let work=ticket(&store,&scope,&binding); let indexed=store.code_index_bound(&scope,&binding,&work).unwrap();
    assert!(indexed.errors.is_empty(),"{:?}",indexed.errors);
    let source_id=memery_core::code_query::file_id(&work.checkout_id,"src/main.ts");
    let request=CodeQuery{operation:Operation::Neighbors,seed_ids:vec![format!("file:{source_id}")],filter:QueryFilter{relation_kinds:vec!["manifest_dependency".into()],..Default::default()},fields:vec![Field::Source,Field::Witnesses],..Default::default()};
    let result=store.code_query(&scope,&binding,&request,&AtomicBool::new(false),&[37;32]).unwrap();
    assert!(result.edges.iter().any(|e|e.relation_kind=="manifest_dependency" && e.resolution_state=="inferred"),"{result:?}");
    let response=serde_json::to_string(&result).unwrap(); assert!(!response.contains("VERSION_SECRET")&&!response.contains("SCRIPT_SECRET"));
}

#[test]
fn sql_static_real_partition_source_to_query() {
    let fixture=Fixture::new();let(scope,binding)=fixture.binding();
    for n in 0..129 {std::fs::write(binding.root.join(format!("f{n:03}.py")),"def f(): return 1\n").unwrap();}
    std::fs::write(binding.root.join("schema.sql"),"CREATE TABLE users (id INTEGER DEFAULT 'PRIVATE_VALUE');\n").unwrap();
    std::fs::write(binding.root.join("access.py"),"def read(conn):\n    return conn.execute(\"SELECT id FROM users WHERE id = ?\", (42,))\n").unwrap();
    std::fs::write(binding.root.join("dynamic.py"),"def query(conn, sql):\n    conn.execute(sql)\n").unwrap();
    let store=SqliteStore::new(fixture.root.join("fm.sqlite").to_str().unwrap(),4).unwrap();let work=ticket(&store,&scope,&binding);
    let indexed=store.code_index_bound(&scope,&binding,&work).unwrap();assert!(indexed.errors.is_empty(),"{:?}",indexed.errors);
    assert!(indexed.coverage.iter().any(|c|c.rel_path=="dynamic.py" && c.detail.as_deref()==Some("sql_static_profile_partial")));
    let empty=CodeQuery{operation:Operation::Neighbors,seed_ids:vec![format!("file:{}",memery_core::code_query::file_id(&work.checkout_id,"f000.py"))],filter:QueryFilter{relation_kinds:vec!["sql_table_reference".into()],..Default::default()},..Default::default()};
    let negative=store.code_query(&scope,&binding,&empty,&AtomicBool::new(false),&[37;32]).unwrap();
    assert!(negative.edges.is_empty() && negative.partial && negative.reasons.iter().any(|r|r=="sql_static_coverage_partial"));
    let no_match=CodeQuery{query:Some("absent_sql_symbol_xyz".into()),channels:vec![Channel::Exact],..Default::default()};
    let negative=store.code_query(&scope,&binding,&no_match,&AtomicBool::new(false),&[37;32]).unwrap();
    assert!(negative.hits.is_empty() && negative.partial && negative.reasons.iter().any(|r|r=="sql_static_coverage_partial"));
    let repeat=ticket(&store,&scope,&binding);let unchanged=store.code_index_bound(&scope,&binding,&repeat).unwrap();
    assert!(unchanged.coverage.iter().any(|c|c.rel_path=="dynamic.py" && c.detail.as_deref()==Some("sql_static_profile_partial")));
    let query=CodeQuery{query:Some("read".into()),channels:vec![Channel::Exact],filter:QueryFilter{kinds:vec!["declaration".into()],..Default::default()},..Default::default()};
    let execute=|q:&CodeQuery|store.code_query(&scope,&binding,q,&AtomicBool::new(false),&[37;32]);
    let caller=execute(&query).unwrap().hits[0].id.clone();
    let result=execute(&CodeQuery{operation:Operation::Neighbors,seed_ids:vec![caller],filter:QueryFilter{relation_kinds:vec!["sql_table_reference".into()],..Default::default()},fields:vec![Field::Source,Field::Witnesses],..Default::default()}).unwrap();
    assert!(result.partial);assert!(result.edges.iter().any(|e|e.relation_kind=="sql_table_reference"&&e.resolution_state=="inferred"),"{result:?}");
    assert!(!serde_json::to_string(&result).unwrap().contains("PRIVATE_VALUE"));
    let receipt=store.code_evidence_receipt(&scope,&binding).unwrap();
    let res=store.code_evidence_page(&scope,&binding,&receipt,CodeEvidenceQuery{kind:CodeEvidenceKind::Resolutions,rel_path:Some("access.py".into()),..Default::default()}).unwrap();
    let row:ResolutionEvidence=res.rows.iter().map(|r|serde_json::from_value(r["evidence"].clone()).unwrap()).find(|r:&ResolutionEvidence|r.reason.contains("unique admitted syntactic table")).unwrap();
    let source=std::fs::read_to_string(binding.root.join("access.py")).unwrap();assert_eq!(&source[row.source.span.start_byte as usize..row.source.span.end_byte as usize],"users");
    let witness=row.witnesses.iter().find(|w|w.kind=="sql_table_declaration").unwrap();let ddl=std::fs::read_to_string(binding.root.join("schema.sql")).unwrap();assert_eq!(&ddl[witness.source.span.start_byte as usize..witness.source.span.end_byte as usize],"users");
    std::fs::write(binding.root.join("second.sql"),"CREATE TABLE users (id INTEGER);\n").unwrap();
    let work=ticket(&store,&scope,&binding);store.code_index_bound(&scope,&binding,&work).unwrap();let receipt=store.code_evidence_receipt(&scope,&binding).unwrap();
    let res=store.code_evidence_page(&scope,&binding,&receipt,CodeEvidenceQuery{kind:CodeEvidenceKind::Resolutions,rel_path:Some("access.py".into()),..Default::default()}).unwrap();
    assert!(res.rows.iter().map(|r|serde_json::from_value::<ResolutionEvidence>(r["evidence"].clone()).unwrap()).any(|r|r.state==ResolutionState::Ambiguous && r.reason.contains("multiple admitted")));
    assert!(memery_core::code_evidence::extract_evidence("check","bad.sql",b"CREATE TABLE x (a TEXT DEFAULT 'unterminated);",&Default::default()).is_err());
    let invalid=memery_core::code_evidence::extract_evidence("check","invalid.py",b"def f(conn):\n    conn.execute('SELECT *, id FROM users')\n",&Default::default()).unwrap();
    assert!(!invalid.occurrences.iter().any(|o|o.syntax_kind=="sql_table_reference"));
    assert!(invalid.diagnostics.iter().any(|d|d.code=="unsupported_sql_code"));
    let unsupported=memery_core::code_evidence::extract_evidence("check","dynamic.py",b"def f(conn, query):\n    conn.execute(query)\n",&Default::default()).unwrap();
    assert!(unsupported.diagnostics.iter().any(|d|d.code=="unsupported_sql_code"));
    assert!(memery_core::code_evidence::storage::module_interface(&unsupported).diagnostics.iter().any(|d|d.code=="unsupported_sql_code"));
}

#[test]
fn callback_registration_real_partition_source_to_query() {
    let fixture=Fixture::new();let(scope,binding)=fixture.binding();
    for n in 0..129 {std::fs::write(binding.root.join(format!("f{n:03}.py")),"def f(): return 1\n").unwrap();}
    let source="function handler() { return 1; }\nfunction attach(button) { button.addEventListener(\"click\", handler); }\nbutton.addEventListener('ready', handler);\nfunction shadow(button, handler) { button.addEventListener('x', handler); }\nbutton.addEventListener('x', () => 1);\n";
    std::fs::write(binding.root.join("events.ts"),source).unwrap();
    let store=SqliteStore::new(fixture.root.join("fm.sqlite").to_str().unwrap(),4).unwrap();let work=ticket(&store,&scope,&binding);
    let indexed=store.code_index_bound(&scope,&binding,&work).unwrap();assert!(indexed.errors.is_empty(),"{:?}",indexed.errors);
    assert!(indexed.coverage.iter().any(|c|c.rel_path=="events.ts" && c.detail.as_deref()==Some("callback_registration_partial")));
    let execute=|q:&CodeQuery|store.code_query(&scope,&binding,q,&AtomicBool::new(false),&[37;32]);
    let search=CodeQuery{query:Some("attach".into()),channels:vec![Channel::Exact],filter:QueryFilter{kinds:vec!["declaration".into()],..Default::default()},..Default::default()};
    let caller=execute(&search).unwrap().hits[0].id.clone();
    for seed in [caller,format!("file:{}",memery_core::code_query::file_id(&work.checkout_id,"events.ts"))] {
        let result=execute(&CodeQuery{operation:Operation::Neighbors,seed_ids:vec![seed],filter:QueryFilter{relation_kinds:vec!["callback_registration".into()],..Default::default()},fields:vec![Field::Source,Field::Witnesses],..Default::default()}).unwrap();
        assert!(result.partial && result.reasons.iter().any(|r|r=="callback_registration_partial"));
        assert!(result.edges.iter().any(|e|e.relation_kind=="callback_registration"&&e.resolution_state=="inferred"),"{result:?}");
    }
    let receipt=store.code_evidence_receipt(&scope,&binding).unwrap();
    let res=store.code_evidence_page(&scope,&binding,&receipt,CodeEvidenceQuery{kind:CodeEvidenceKind::Resolutions,rel_path:Some("events.ts".into()),..Default::default()}).unwrap();
    let rows:Vec<ResolutionEvidence>=res.rows.iter().map(|r|serde_json::from_value(r["evidence"].clone()).unwrap()).collect();
    let row=rows.iter().find(|r|r.reason.contains("syntactic event registration only")&&r.caller_id.is_some()).unwrap();
    assert_eq!(&source[row.source.span.start_byte as usize..row.source.span.end_byte as usize],"handler");
    let witness=row.witnesses.iter().find(|w|w.kind=="callback_registration_call").unwrap();
    assert_eq!(&source[witness.source.span.start_byte as usize..witness.source.span.end_byte as usize],"button.addEventListener(\"click\", handler)");
    assert!(rows.iter().any(|r|r.state==ResolutionState::Unsupported && r.reason.contains("local/parameter")));
}

#[test]
fn selected_outline_witness_is_scoped_complete_and_bounded() {
 for fillers in [0,129] {
    let fixture=Fixture::new();let(scope,binding)=fixture.binding();
    for n in 0..fillers {std::fs::write(binding.root.join(format!("f{n:03}.py")),"def f(): return 1\n").unwrap();}
    std::fs::write(binding.root.join("chosen.py"),"def first(): return 1\ndef second(): return 2\n").unwrap();
    std::fs::write(binding.root.join("unrelated.py"),"def query(conn, sql):\n    conn.execute(sql)\n").unwrap();
    let store=SqliteStore::new(fixture.root.join("fm.sqlite").to_str().unwrap(),4).unwrap();let work=ticket(&store,&scope,&binding);
    store.code_index_bound(&scope,&binding,&work).unwrap();
    let execute=|q:&CodeQuery|store.code_query(&scope,&binding,q,&AtomicBool::new(false),&[37;32]);
    let receipt=execute(&CodeQuery{operation:Operation::Capabilities,..Default::default()}).unwrap().receipt.unwrap();
    let id=format!("file:{}",memery_core::code_query::file_id(&work.checkout_id,"chosen.py"));
    let query=CodeQuery{operation:Operation::Outline,seed_ids:vec![id.clone()],filter:QueryFilter{rel_path:Some("chosen.py".into()),..Default::default()},fields:vec![Field::Label,Field::Source,Field::Properties],channels:vec![],expected_receipt:Some(receipt.clone()),..Default::default()};
    let result=execute(&query).unwrap();assert!(result.partial && result.next_cursor.is_none());
    let witness=result.outline_completeness.as_ref().expect("complete file witness despite unrelated partial");
    assert_eq!(witness.file_id,id);assert_eq!(witness.rel_path,"chosen.py");assert_eq!(witness.declaration_count,2);
    println!("OUTLINE_CASE={}",serde_json::to_string(&result).unwrap());
    let mut tiny=query.clone();tiny.budgets.rows=1;let partial=execute(&tiny).unwrap();assert!(partial.next_cursor.is_some() && partial.outline_completeness.is_none());
    std::fs::write(binding.root.join("chosen.py"),"def first():\n    global x\n    return 1\n").unwrap();
    let work=ticket(&store,&scope,&binding);store.code_index_bound(&scope,&binding,&work).unwrap();
    assert!(execute(&query).is_err(),"old receipt must not qualify");
    let fresh=execute(&CodeQuery{operation:Operation::Capabilities,..Default::default()}).unwrap().receipt.unwrap();
    let unsupported=execute(&CodeQuery{expected_receipt:Some(fresh),..query}).unwrap();assert!(unsupported.outline_completeness.is_none());
 }
}

#[test]
fn straight_line_backward_slice_scoped_index_and_cursor() {
 for fillers in [0,129] {
    let fixture=Fixture::new();let(scope,binding)=fixture.binding();
    for n in 0..fillers {std::fs::write(binding.root.join(format!("f{n:03}.py")),"def filler(): return 1\n").unwrap();}
    let source="def total(price, tax):\n    \"description\"\n    subtotal = price * 2\n    subtotal = subtotal + tax\n    result = subtotal\n    return result\n\ndef broken(a):\n    if a:\n        return a\n    return a\n";
    std::fs::write(binding.root.join("calc.py"),source).unwrap();
    let store=SqliteStore::new(fixture.root.join("fm.sqlite").to_str().unwrap(),4).unwrap();let work=ticket(&store,&scope,&binding);
    let indexed=store.code_index_bound(&scope,&binding,&work).unwrap();assert!(indexed.errors.is_empty(),"{:?}",indexed.errors);
    let execute=|q:&CodeQuery|store.code_query(&scope,&binding,q,&AtomicBool::new(false),&[37;32]);
    let find=CodeQuery{query:Some("total".into()),channels:vec![Channel::Exact],filter:QueryFilter{kinds:vec!["declaration".into()],..Default::default()},fields:vec![Field::Label,Field::Source,Field::Properties],..Default::default()};
    let found=execute(&find).unwrap();let receipt=found.receipt.clone().unwrap();
    let seed=found.hits.iter().find(|h|h.label.as_deref()==Some("total")).unwrap().properties.as_ref().unwrap()["backwardSlice"]["seed"].as_str().unwrap().to_string();
    let mut query=CodeQuery{operation:Operation::BackwardSlice,seed_ids:vec![seed.clone()],expected_receipt:Some(receipt.clone()),channels:vec![],fields:vec![Field::Label,Field::Source,Field::Properties],..Default::default()};query.budgets.rows=2;query.budgets.bytes=7168;query.budgets.tokens=7168;
    let mut units=vec![];loop {let result=execute(&query).unwrap();assert!(result.partial && result.reasons.iter().any(|v|v=="syntactic_straight_line_only"));let slice=result.slice_analysis.as_ref().expect("slice analysis");assert_eq!(format!("occ:{}",slice.seed_occurrence_id),seed);assert_eq!(slice.source.rel_path,"calc.py");units.extend(slice.units.clone());println!("SLICE_CASE={}",serde_json::to_string(&result).unwrap());if let Some(cursor)=result.next_cursor{query.cursor=Some(cursor);}else{break;}}
    assert_eq!(units.len(),5);let first=units.iter().find(|d|d.kind=="assignment"&&d.name=="subtotal"&&d.uses.len()==1).unwrap();let second=units.iter().find(|d|d.kind=="assignment"&&d.name=="subtotal"&&d.uses.len()==2).unwrap();assert_eq!(second.uses[0].definition_id,first.id);
    for unit in &units {let text=&source[unit.target_span.start_byte as usize..unit.target_span.end_byte as usize];assert_eq!(text,unit.name);}
    let broken=CodeQuery{query:Some("broken".into()),..find.clone()};let out=execute(&broken).unwrap();let profile=&out.hits[0].properties.as_ref().unwrap()["backwardSlice"];assert_eq!(profile["status"],"unsupported");
    let mut tiny=query.clone();tiny.cursor=None;tiny.budgets.hops=0;assert!(execute(&tiny).is_err());
    std::fs::write(binding.root.join("calc.py"),"def total(price):\n    return price\n").unwrap();let new=ticket(&store,&scope,&binding);store.code_index_bound(&scope,&binding,&new).unwrap();query.cursor=None;assert!(execute(&query).is_err(),"stale receipt must refuse");
 }
}

#[test]
fn python_error_boundaries_scoped_index_and_cursor() {
 for fillers in [0,129] {
    let fixture=Fixture::new();let(scope,binding)=fixture.binding();
    for n in 0..fillers {std::fs::write(binding.root.join(format!("f{n:03}.py")),"def filler(): return 1\n").unwrap();}
    let source="def outer():\n    try:\n        work()\n        try:\n            inner()\n        except ValueError:\n            recover()\n        def nested():\n            other()\n    except Exception:\n        fallback()\n    else:\n        success()\n    finally:\n        cleanup()\n";
    std::fs::write(binding.root.join("bounds.py"),source).unwrap();
    let store=SqliteStore::new(fixture.root.join("fm.sqlite").to_str().unwrap(),4).unwrap();let work=ticket(&store,&scope,&binding);
    let indexed=store.code_index_bound(&scope,&binding,&work).unwrap();assert!(indexed.errors.is_empty(),"{:?}",indexed.errors);
    let execute=|q:&CodeQuery|store.code_query(&scope,&binding,q,&AtomicBool::new(false),&[37;32]);
    let find=CodeQuery{query:Some("outer".into()),channels:vec![Channel::Exact],filter:QueryFilter{kinds:vec!["declaration".into()],..Default::default()},fields:vec![Field::Label,Field::Source,Field::Properties],..Default::default()};
    let found=execute(&find).unwrap();let receipt=found.receipt.clone().unwrap();let seed=found.hits.iter().find(|h|h.label.as_deref()==Some("outer")).unwrap().id.clone();
    let mut query=CodeQuery{operation:Operation::ErrorBoundaries,seed_ids:vec![seed.clone()],expected_receipt:Some(receipt),channels:vec![],fields:vec![Field::Label,Field::Source,Field::Properties],..Default::default()};query.budgets.rows=2;query.budgets.bytes=7168;query.budgets.tokens=7168;
    let mut regions=vec![];loop {let result=execute(&query).unwrap();assert!(result.partial&&result.reasons.iter().any(|v|v=="syntactic_error_boundaries_only"));let analysis=result.error_boundary_analysis.as_ref().unwrap();assert_eq!(format!("decl:{}",analysis.function_id),seed);assert_eq!(analysis.source.rel_path,"bounds.py");regions.extend(analysis.regions.clone());println!("BOUNDARY_CASE={}",serde_json::to_string(&result).unwrap());if let Some(cursor)=result.next_cursor{query.cursor=Some(cursor)}else{break}}
    assert_eq!(regions.len(),2);assert_eq!(regions[0].handlers.len(),1);assert!(regions[0].else_clause.is_some()&&regions[0].finally_clause.is_some());assert_eq!(regions[0].calls.len(),3);assert_eq!(regions[1].calls.len(),1);
    std::fs::write(binding.root.join("bounds.py"),"def outer():\n    try:\n        work()\n    except* ValueError:\n        pass\n").unwrap();let new=ticket(&store,&scope,&binding);store.code_index_bound(&scope,&binding,&new).unwrap();query.cursor=None;assert!(execute(&query).is_err(),"stale receipt must refuse");
    let fresh=execute(&find).unwrap();query.expected_receipt=fresh.receipt;query.seed_ids=vec![fresh.hits.iter().find(|h|h.label.as_deref()==Some("outer")).unwrap().id.clone()];assert!(execute(&query).is_err(),"unsupported except-star must not prove absence");
 }
}
