//! Run with --test-threads=1: fixtures temporarily isolate process HOME.
#[path="code_partitioned_visual_summary_tests.rs"]
mod visual_summary_tests;
use super::*;
use super::super::code_evidence::CodeEvidenceReceipt;

fn metric_calls(metrics:&crate::code_metrics::CodeMetrics,stage:crate::code_metrics::Stage)->u64{metrics.stages[stage as usize].counters.calls}
fn memo_open(fixture:&Fixture,memo:&std::sync::Arc<AuditMemo>,expected:&str)->Result<Generation,String>{Generation::open_memoized(&fixture.main,&fixture.scope,&fixture.receipt(1),expected,memo)}
fn fixture_file_stamp(fixture:&Fixture,path:&Path)->String{
    let path=path.canonicalize().unwrap();let root=fixture.root.canonicalize().unwrap();
    assert!(path.starts_with(&root));let m=std::fs::metadata(path).unwrap();
    #[cfg(unix)]{use std::os::unix::fs::MetadataExt;blake3::hash(format!("{}:{}:{}:{}:{}:{}:{}",m.dev(),m.ino(),m.len(),m.mtime(),m.mtime_nsec(),m.ctime(),m.ctime_nsec()).as_bytes()).to_hex().to_string()}
    #[cfg(not(unix))]{let _=m;panic!("partition cache stamps unsupported on this test platform")}
}
fn published_stamp(fixture:&Fixture)->String{let generation=Generation::open(&fixture.main,&fixture.scope,&fixture.receipt(1)).unwrap();let stamp=generation.stamp().unwrap();assert_eq!(stamp,fixture_file_stamp(fixture,&generation.path));stamp}

#[test]
fn audit_memo_is_bounded_scoped_and_never_caches_handles_or_oversize_keys(){
    let scope=GraphScope::new("a","w").unwrap();
    let receipt=CodeEvidenceReceipt{checkout_id:"c".into(),head_generation:"h".into(),admission_epoch:1,policy_generation:1,config_fingerprint:"p".into()};
    let key=AuditKey::new(&scope,&receipt,"expected","pinned").unwrap();let memo=AuditMemo::default();
    memo.remember(key.clone());memo.remember_count(&key,123);assert_eq!(memo.count(&key),Some(123));
    for field in 0..11{let mut changed=key.clone();match field{0=>changed.owner.push('x'),1=>changed.workspace.push('x'),2=>changed.checkout.push('x'),3=>changed.head.push('x'),4=>changed.producer.push('x'),5=>changed.version+=1,6=>changed.expected.push('x'),7=>changed.pinned.push('x'),8=>changed.admission+=1,9=>changed.policy+=1,_=>changed.include_global=!changed.include_global};assert!(!memo.contains(&changed));}
    let mut large=receipt.clone();large.checkout_id="x".repeat(4097);assert!(AuditKey::new(&scope,&large,"e","p").is_none());
    let mut total=scope.clone();total.owner="x".repeat(4096);total.workspace_id="y".repeat(4096);large=receipt.clone();large.checkout_id="z".repeat(4096);large.head_generation="h".repeat(4096);assert!(AuditKey::new(&total,&large,"e","p").is_none());
    for n in 0..17{let mut next=key.clone();next.head=n.to_string();memo.remember(next);}
    assert_eq!(memo.entries.lock().unwrap().len(),16);assert!(!memo.contains(&key));
    // Scoped invalidation never keeps an erased scope merely because its file
    // might still exist; it has no effect on another owner/workspace.
    memo.remember(key.clone());let mut other=key.clone();other.owner="b".into();memo.remember(other.clone());
    memo.invalidate("a",Some("w"),None);assert!(!memo.contains(&key));assert!(memo.contains(&other));
}

#[test]
fn audited_open_and_exact_unfiltered_count_have_real_hit_miss_paths_and_filter_bypass(){
    use crate::code_metrics::{capture_code_metrics,Stage};
    let fixture=Fixture::new();fixture.publish(1);let stamp=published_stamp(&fixture);let memo=std::sync::Arc::new(AuditMemo::default());
    let (first,metrics)=capture_code_metrics(||memo_open(&fixture,&memo,&stamp));let first=first.unwrap();
    assert_eq!(metric_calls(&metrics,Stage::GenerationAuditMiss),1);assert_eq!(metric_calls(&metrics,Stage::GenerationAuditHit),0);
    let q=CodeQuery::default();
    let (count,metrics)=capture_code_metrics(||first.eligible_nodes(&q));let count=count.unwrap();
    assert_eq!(metric_calls(&metrics,Stage::EligibleCountMiss),1);assert_eq!(metric_calls(&metrics,Stage::QuerySql),1);
    drop(first);
    let (next,metrics)=capture_code_metrics(||memo_open(&fixture,&memo,&stamp));let next=next.unwrap();
    assert_eq!(metric_calls(&metrics,Stage::GenerationAuditHit),1);assert_eq!(metric_calls(&metrics,Stage::GenerationAuditMiss),0);
    let (again,metrics)=capture_code_metrics(||next.eligible_nodes(&q));assert_eq!(again.unwrap(),count);
    assert_eq!(metric_calls(&metrics,Stage::EligibleCountHit),1);assert_eq!(metric_calls(&metrics,Stage::QuerySql),0);
    for filter in [QueryFilter{rel_path:Some("".into()),..Default::default()},QueryFilter{path_prefix:Some("".into()),..Default::default()},QueryFilter{kinds:vec!["file".into()],..Default::default()},QueryFilter{languages:vec!["python".into()],..Default::default()},QueryFilter{states:vec!["metadata".into()],..Default::default()}]{
        let q=CodeQuery{operation:Operation::Catalog,filter,..Default::default()};let (value,metrics)=capture_code_metrics(||next.eligible_nodes(&q));value.unwrap();
        assert_eq!(metric_calls(&metrics,Stage::QuerySql),1);assert_eq!(metric_calls(&metrics,Stage::EligibleCountHit),0);assert_eq!(metric_calls(&metrics,Stage::EligibleCountMiss),0);
        if q.filter.rel_path.as_deref()==Some("")||q.filter.path_prefix.as_deref()==Some(""){
            let indexed=fixture.snapshot(&next,&q,true);let legacy=fixture.oracle(&indexed);
            let actual=run_query(&indexed,&q,&AtomicBool::new(false),&[9;32]).unwrap_err();let expected=run_query(&legacy,&q,&AtomicBool::new(false),&[9;32]).unwrap_err();
            assert_eq!(actual.code,"invalid");assert_eq!(serde_json::to_value(actual).unwrap(),serde_json::to_value(expected).unwrap());
        }else{assert_parity(&fixture,&next,&q,true);}
    }
    for partial in [false,true]{assert_parity(&fixture,&next,&CodeQuery{operation:Operation::Overview,..Default::default()},partial);}
}

#[test]
fn exact_join_count_does_not_count_orphan_nodes_even_with_consistent_meta(){
    let fixture=Fixture::new();let mut generation=fixture.stage(1);
    generation.db.execute("INSERT INTO nodes(id,file,kind,ordinal,state)VALUES('orphan',999999,'occurrence',0,'metadata')",[]).unwrap();
    // Keep all sealed occurrence-kind metadata consistent, but deliberately
    // leave the missing file: the exact JOIN must still exclude this node.
    generation.db.execute("INSERT INTO occurrence_kinds(occurrence,kind)SELECT row,1 FROM nodes WHERE id='orphan'",[]).unwrap();
    generation.seal().unwrap();let stamp=generation.stamp().unwrap();generation.published();drop(generation);
    fixture.main.execute("INSERT INTO code_index_heads VALUES(?1,?2,'checkout',?3)",params![fixture.scope.owner,fixture.scope.workspace_id,Fixture::run(1)]).unwrap();
    let memo=std::sync::Arc::new(AuditMemo::default());let generation=memo_open(&fixture,&memo,&stamp).unwrap();
    let all:i64=generation.db.query_row("SELECT count(*) FROM nodes",[],|r|r.get(0)).unwrap();
    let joined=generation.eligible_nodes(&CodeQuery::default()).unwrap();assert_eq!(joined+1,all as usize);
    assert_eq!(generation.eligible_nodes(&CodeQuery::default()).unwrap(),joined);
}

#[test]
fn failed_or_changed_audits_are_never_admitted_and_small_metadata_still_checked(){
    let fixture=Fixture::new();let path=fixture.publish(1);let stamp=published_stamp(&fixture);let memo=std::sync::Arc::new(AuditMemo::default());
    let writer=Connection::open(&path).unwrap();writer.execute("UPDATE meta SET value='999999' WHERE key='nodes'",[]).unwrap();drop(writer);
    let changed=fixture_file_stamp(&fixture,&path);
    assert!(memo_open(&fixture,&memo,&stamp).is_err());assert!(memo_open(&fixture,&memo,&changed).is_err());assert!(memo.entries.lock().unwrap().is_empty());
}

#[test]
fn mutation_after_audit_before_stamp_recheck_cannot_enter_memo(){
    let fixture=Fixture::new();let path=fixture.publish(1);let stamp=published_stamp(&fixture);let memo=std::sync::Arc::new(AuditMemo::default());
    let check=memo.clone();AUDIT_TEST_HOOK.with(|slot|*slot.borrow_mut()=Some(Box::new(move||{use std::io::Write;assert!(check.entries.try_lock().is_ok());std::fs::OpenOptions::new().append(true).open(path).unwrap().write_all(&[0]).unwrap();})));
    assert!(memo_open(&fixture,&memo,&stamp).is_err());assert!(memo.entries.lock().unwrap().is_empty());
}

#[test]
fn matching_positive_memo_candidate_cannot_bypass_per_open_small_metadata(){
    use crate::code_metrics::{capture_code_metrics,Stage};
    let fixture=Fixture::new();let path=fixture.publish(1);let stamp=published_stamp(&fixture);let memo=std::sync::Arc::new(AuditMemo::default());
    drop(memo_open(&fixture,&memo,&stamp).unwrap());assert_eq!(memo.entries.lock().unwrap().len(),1);
    let writer=Connection::open(&path).unwrap();writer.execute("UPDATE meta SET value='wrong-producer' WHERE key='producer'",[]).unwrap();drop(writer);
    let changed=fixture_file_stamp(&fixture,&path);
    // Deliberately seed a would-be matching positive entry. Real production
    // cannot carry the old audit across changed ctime; this stronger internal
    // fixture proves small meta is checked EVEN IF a matching entry exists.
    let key=AuditKey::new(&fixture.scope,&fixture.receipt(1),&changed,&changed).unwrap();memo.remember(key.clone());assert!(memo.contains(&key));
    let(result,metrics)=capture_code_metrics(||memo_open(&fixture,&memo,&changed));assert!(result.is_err());assert_eq!(metric_calls(&metrics,Stage::GenerationAuditHit),0);
}

#[test]
fn oversize_header_audit_failure_never_enters_positive_memo(){
    use crate::code_metrics::{capture_code_metrics,Stage};
    let fixture=Fixture::new();let path=fixture.publish(1);let memo=std::sync::Arc::new(AuditMemo::default());
    let writer=Connection::open(&path).unwrap();writer.execute("UPDATE files SET header=?1 WHERE id=1",params!["x".repeat(65537)]).unwrap();drop(writer);
    let changed=fixture_file_stamp(&fixture,&path);
    let(result,metrics)=capture_code_metrics(||memo_open(&fixture,&memo,&changed));assert!(result.is_err());assert_eq!(metric_calls(&metrics,Stage::GenerationAuditMiss),1);assert!(memo.entries.lock().unwrap().is_empty());
}

#[cfg(unix)]
#[test]
fn memo_does_not_hide_same_mtime_edits_inode_swaps_private_files_or_selected_payload_errors(){
    use std::os::unix::fs::PermissionsExt;
    let fixture=Fixture::new();let path=fixture.publish(1);let stamp=published_stamp(&fixture);let memo=std::sync::Arc::new(AuditMemo::default());
    drop(memo_open(&fixture,&memo,&stamp).unwrap());let modified=std::fs::metadata(&path).unwrap().modified().unwrap();
    let writer=Connection::open(&path).unwrap();writer.execute("UPDATE files SET payload_hash=?1 WHERE id=1",params!["0".repeat(64)]).unwrap();drop(writer);
    std::fs::File::open(&path).unwrap().set_times(std::fs::FileTimes::new().set_modified(modified)).unwrap();
    assert_eq!(std::fs::metadata(&path).unwrap().modified().unwrap(),modified);assert!(memo_open(&fixture,&memo,&stamp).is_err());
    let current=Generation::open(&fixture.main,&fixture.scope,&fixture.receipt(1)).unwrap();let changed=current.stamp().unwrap();drop(current);
    let generation=memo_open(&fixture,&memo,&changed).unwrap();assert!(generation.load(1).is_err());drop(generation);
    let replacement=path.with_extension("replacement");std::fs::write(&replacement,std::fs::read(&path).unwrap()).unwrap();std::fs::set_permissions(&replacement,std::fs::Permissions::from_mode(0o600)).unwrap();std::fs::rename(&replacement,&path).unwrap();
    assert!(memo_open(&fixture,&memo,&changed).is_err());
    std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o644)).unwrap();assert!(memo_open(&fixture,&memo,&changed).is_err());
}

#[test]
fn concurrent_audit_misses_do_not_hold_memo_mutex_across_sql_or_share_connections(){
    let fixture=Fixture::new();fixture.publish(1);let stamp=published_stamp(&fixture);let memo=std::sync::Arc::new(AuditMemo::default());let rendezvous=std::sync::Arc::new((std::sync::Mutex::new(0),std::sync::Condvar::new()));
    let mut workers=vec![];
    for _ in 0..2{let memo=memo.clone();let rendezvous=rendezvous.clone();let scope=fixture.scope.clone();let receipt=fixture.receipt(1);let main=fixture.root.join("main.sqlite");let stamp=stamp.clone();
        workers.push(std::thread::spawn(move||{let check=memo.clone();AUDIT_TEST_HOOK.with(|slot|*slot.borrow_mut()=Some(Box::new(move||{assert!(check.entries.try_lock().is_ok());let(lock,wake)=&*rendezvous;let mut entered=lock.lock().unwrap();*entered+=1;wake.notify_all();let(entered,_)=wake.wait_timeout_while(entered,std::time::Duration::from_secs(10),|n|*n<2).unwrap();assert_eq!(*entered,2,"bounded concurrent miss rendezvous");})));let db=Connection::open(main).unwrap();let generation=Generation::open_memoized(&db,&scope,&receipt,&stamp,&memo).unwrap();generation.eligible_nodes(&CodeQuery::default()).unwrap()}));
    }
    let counts:Vec<_>=workers.into_iter().map(|worker|worker.join().unwrap()).collect();assert_eq!(counts[0],counts[1]);assert_eq!(memo.entries.lock().unwrap().len(),1);
}

#[test]
fn sqlite_work_guard_preserves_exact_budget_and_fresh_counter(){
    // Drive callbacks directly: no sleeping or load-dependent wall clock.
    let mut guard=sqlite_work_guard(2000);
    assert!(!guard()); // previous VM estimate 0
    assert!(!guard()); // 1000
    assert!(!guard()); // 2000: preserve the original inclusive comparison
    assert!(guard());  // 3000
    let mut fresh=sqlite_work_guard(2000);
    assert!(!fresh());
    let mut zero=sqlite_work_guard(0);
    assert!(!zero());
    assert!(zero());
}

#[test]
fn sqlite_work_guard_bounds_real_vm_and_recovers_with_new_budget(){
    let db=Connection::open_in_memory().unwrap();
    let sql="WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n";
    install_sqlite_work_guard(&db,0);
    let failure=db.query_row(sql,[],|r|r.get::<_,i64>(0)).unwrap_err();
    assert!(matches!(&failure,rusqlite::Error::SqliteFailure(error,_) if error.code==rusqlite::ErrorCode::OperationInterrupted));
    assert_eq!(qerr(&failure).code,"budget_exceeded");
    install_sqlite_work_guard(&db,1_000_000);
    assert_eq!(db.query_row(sql,[],|r|r.get::<_,i64>(0)).unwrap(),50_005_000);
}

#[test]
fn sqlite_work_guard_is_shared_by_both_sites_without_elapsed_deadline(){
    // Guard wiring is part of the regression: neither audited open nor the
    // indexed query may quietly retain a separate elapsed-time interruption.
    let source=include_str!("code_partitioned.rs");
    assert_eq!(source.matches("install_sqlite_work_guard(").count(),3);
    assert!(source.contains("install_sqlite_work_guard(&db,128_000_000)"));
    assert!(source.contains("install_sqlite_work_guard(&generation.db,query.budgets.work.saturating_mul(32))"));
    assert!(!source.contains("Instant::"));
    assert!(!source.contains(".elapsed()"));
}

struct Fixture {
    main: Connection,
    root: PathBuf,
    previous_home: Option<std::ffi::OsString>,
    scope: GraphScope,
    binding: WorkspaceBinding,
    files: Vec<FileEvidence>,
    source: CodeSourceSnapshot,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Some(home) = &self.previous_home { std::env::set_var("HOME", home); }
        else { std::env::remove_var("HOME"); }
        let _ = std::fs::remove_dir_all(&self.root);
    }
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("fm-partition-fixture-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        let home = root.join("home"); std::fs::create_dir(&home).unwrap();
        let source_root = root.join("source"); std::fs::create_dir(&source_root).unwrap();
        let previous_home = std::env::var_os("HOME"); std::env::set_var("HOME", &home);
        let main = Connection::open(root.join("main.sqlite")).unwrap();
        main.execute_batch("CREATE TABLE code_index_heads(owner TEXT,workspace_id TEXT,codebase TEXT,run_id TEXT)").unwrap();
        let scope = GraphScope { owner:"owner".into(),workspace_id:"workspace".into(),include_global:false };
        let binding = WorkspaceBinding { owner:scope.owner.clone(),owner_subject_id:"subject".into(),workspace_id:scope.workspace_id.clone(),
            location_id:"location".into(),workspace_revision:1,policy_generation:1,root:source_root.clone(),origin:"app".into(),subscriber_id:"test".into(),authority_expires_unix_ms:None };
        let sources = [
            ("lib.py","def target(value):\n    return value\n"),
            ("caller.py","from lib import target as action\ndef caller():\n    return action(1)\ncaller()\n"),
            ("client.ts","export function parseHTTPResponse(value: string) { return value; }\nparseHTTPResponse('x');\n"),
            ("native.rs","pub fn rust_target() {}\nfn caller() { rust_target(); }\n"),
        ];
        let files = sources.iter().map(|(path,text)| {
            std::fs::write(source_root.join(path),text).unwrap();
            extract_evidence("checkout",path,text.as_bytes(),&ExtractionBudget::default()).unwrap()
        }).collect();
        let source = CodeSourceSnapshot { evidence_store_version:STORE_VERSION, evidence_cache_identity:String::new(), selection_policy_digest:"policy".into(), repository_id:"registration".into(),
            git_commit:None,git_ref:None,git_dirty:false,git_dirty_known:false,git_metadata_state:"unavailable".into(),file_manifest_digest:"snapshot".into() };
        Self { main,root,previous_home,scope,binding,files,source }
    }
    fn run(number: u32) -> String { format!("codegen_{number:032x}") }
    fn receipt(&self,number:u32) -> CodeEvidenceReceipt {
        CodeEvidenceReceipt { checkout_id:"checkout".into(),head_generation:Self::run(number),admission_epoch:1,policy_generation:1,config_fingerprint:crate::code::producer_fingerprint() }
    }
    fn create(&self,number:u32) -> Generation {
        Generation::create(&self.main,&self.scope,"checkout",&Self::run(number),"policy").unwrap()
    }
    fn stage(&self,number:u32) -> Generation {
        let mut generation = self.create(number);
        for file in &self.files {
            generation.stage(file,0,&crate::graph::GraphUpsertInput { nodes:vec![],edges:vec![],cues:vec![] },&self.scope).unwrap();
        }
        generation.resolve("checkout",&[],||Ok(())).unwrap();
        generation
    }
    fn publish(&self,number:u32) -> PathBuf {
        let mut generation = self.stage(number);
        generation.seal().unwrap();
        self.main.execute("DELETE FROM code_index_heads",[]).unwrap();
        self.main.execute("INSERT INTO code_index_heads VALUES(?1,?2,'checkout',?3)",params![self.scope.owner,self.scope.workspace_id,Self::run(number)]).unwrap();
        generation.published();
        generation.path.clone()
    }
    fn snapshot(&self,generation:&Generation,query:&CodeQuery,partial:bool) -> QuerySnapshot {
        let mut coverage: Vec<_> = self.files.iter().map(|file|ScanCoverage { rel_path:file.rel_path.clone(),status:"no_source_units".into(),source_hash:Some(file.source_hash.clone()),..Default::default() }).collect();
        if partial { coverage.push(ScanCoverage { rel_path:"broken.py".into(),status:"parse_failed".into(),detail:Some("fixture parse failure".into()),..Default::default() }); }
        generation.query_snapshot(&self.scope,&self.binding,self.receipt(1),"registration".into(),self.source.clone(),coverage,query,&AtomicBool::new(false)).unwrap()
    }
    fn oracle(&self,snapshot:&QuerySnapshot) -> QuerySnapshot {
        let corpus = resolve_evidence(&CorpusInput { checkout_id:"checkout".into(),files:self.files.clone(),unavailable_files:vec![] },&ResolutionBudget::default()).unwrap();
        QuerySnapshot { receipt:snapshot.receipt.clone(),scope_key:snapshot.scope_key.clone(),files:self.files.clone(),resolutions:corpus.resolutions,
            coverage:snapshot.coverage.clone(),metadata_bytes:0,metadata_records:0,eligible_records:None }
    }
}
fn public_without_cost(result: &CodeQueryResult) -> serde_json::Value {
    let mut value = serde_json::to_value(result).unwrap();
    value.as_object_mut().unwrap().remove("cost");
    value
}
fn assert_parity(fixture:&Fixture,generation:&Generation,query:&CodeQuery,partial:bool) -> CodeQueryResult {
    let indexed = fixture.snapshot(generation,query,partial);
    let legacy = fixture.oracle(&indexed);
    let result = run_query(&indexed,query,&AtomicBool::new(false),&[9;32]).unwrap();
    let expected = run_query(&legacy,query,&AtomicBool::new(false),&[9;32]).unwrap();
    assert_eq!(public_without_cost(&result),public_without_cost(&expected),"operation {:?}, query {:?}, filter {:?}",query.operation,query.query,query.filter);
    result
}

#[test]
fn partition_crash_child() {
    let Ok(mode)=std::env::var("FM_PARTITION_CRASH_MODE") else{return;};
    let root=PathBuf::from(std::env::var_os("FM_PARTITION_CRASH_ROOT").unwrap()).canonicalize().unwrap();
    assert!(root.file_name().unwrap().to_str().unwrap().starts_with("fm-partition-fixture-"));
    let main=Connection::open(root.join("main.sqlite")).unwrap();
    let scope=GraphScope{owner:"owner".into(),workspace_id:"workspace".into(),include_global:false};
    let mut generation=Generation::create(&main,&scope,"checkout",&Fixture::run(2),"policy").unwrap();
    let file=extract_evidence("checkout","child.py",b"def child(): pass\n",&ExtractionBudget::default()).unwrap();
    generation.stage(&file,0,&crate::graph::GraphUpsertInput{nodes:vec![],edges:vec![],cues:vec![]},&scope).unwrap();
    if mode!="staging" {generation.resolve("checkout",&[],||Ok(())).unwrap();generation.seal().unwrap();}
    if mode=="committed" {
        main.execute_batch("BEGIN IMMEDIATE; DELETE FROM code_index_heads").unwrap();
        main.execute("INSERT INTO code_index_heads VALUES('owner','workspace','checkout',?1)",params![Fixture::run(2)]).unwrap();
        main.execute_batch("COMMIT").unwrap();
    }
    // Deliberately bypass Rust Drop, exactly as an exited/crashed writer does.
    std::process::exit(0);
}

#[test]
fn partition_process_exit_preserves_head_and_cleans_only_unpublished_orphans() {
    for mode in ["staging","sealed","committed"] {
        let fixture=Fixture::new();let old=fixture.publish(1);
        let mut child=std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact","store::code_partitioned::tests::partition_crash_child","--test-threads=1"])
            .env("FM_PARTITION_CRASH_MODE",mode).env("FM_PARTITION_CRASH_ROOT",&fixture.root)
            .env("HOME",fixture.root.join("home")).stdout(std::process::Stdio::null()).spawn().unwrap();
        let start=std::time::Instant::now();
        loop {
            if let Some(status)=child.try_wait().unwrap(){assert!(status.success());break;}
            if start.elapsed()>std::time::Duration::from_secs(20){let _=child.kill();let _=child.wait();panic!("owned crash child timed out");}
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        let expected=if mode=="committed"{2}else{1};
        let head:String=fixture.main.query_row("SELECT run_id FROM code_index_heads",[],|r|r.get(0)).unwrap();
        assert_eq!(head,Fixture::run(expected));
        let active=Generation::open(&fixture.main,&fixture.scope,&fixture.receipt(expected)).unwrap();
        let folder=active.directory.path.clone();drop(active);
        let orphan=folder.join(run_name(&Fixture::run(2)).unwrap());assert!(orphan.exists());
        let next=fixture.create(3);
        if mode=="committed"{assert!(orphan.exists());assert!(!old.exists());}
        else{assert!(!orphan.exists());assert!(old.exists());}
        drop(next);
    }
}

#[test]
fn partition_lookup_indexes_are_narrow_and_used() {
    let db=Connection::open_in_memory().unwrap();db.execute_batch(SCHEMA).unwrap();
    assert_eq!(db.query_row("SELECT count(*) FROM sqlite_master WHERE type='index' AND name='key_nodes_node'",[],|r|r.get::<_,i64>(0)).unwrap(),0);
    for i in 0..1000 {
        let state=if i%3==0{"unresolved"}else if i%3==1{"exact"}else{"inferred"};
        let caller=if i%5==0{None}else{Some(format!("caller-{}",i%7))};
        let target=if state=="unresolved"{None}else{Some(format!("target-{}",i%11))};
        db.execute("INSERT INTO edges VALUES(?1,?2,?3,?4,1,?5)",params![format!("occ-{i}"),caller,target,state,i]).unwrap();
    }
    assert_eq!(db.query_row("SELECT count(*) FROM edges",[],|r|r.get::<_,i64>(0)).unwrap(),1000,"all evidence states retained");
    let eligible:i64=db.query_row("SELECT count(*) FROM edges WHERE caller IS NOT NULL AND target IS NOT NULL AND state IN('exact','inferred')",[],|r|r.get(0)).unwrap();
    assert!(eligible>0&&eligible<1000);
    for endpoint in ["caller","target"] {
        let sql=format!("SELECT occurrence,caller,target FROM edges WHERE {endpoint}=?1 AND caller IS NOT NULL AND target IS NOT NULL AND state IN('exact','inferred') LIMIT ?2");
        let plans:Vec<String>=db.prepare(&format!("EXPLAIN QUERY PLAN {sql}")).unwrap().query_map(params!["caller-1",101],|r|r.get(3)).unwrap().collect::<rusqlite::Result<_>>().unwrap();
        assert!(plans.iter().any(|p|p.contains(&format!("USING INDEX edges_{endpoint}"))),"{plans:?}");
        let columns:Vec<String>=db.prepare(&format!("PRAGMA index_info(edges_{endpoint})")).unwrap().query_map([],|r|r.get(2)).unwrap().collect::<rusqlite::Result<_>>().unwrap();
        assert_eq!(columns,vec![endpoint.to_owned()],"no duplicated covering identifiers");
        let indexed:i64=db.query_row(&format!("SELECT count(*) FROM edges INDEXED BY edges_{endpoint} WHERE caller IS NOT NULL AND target IS NOT NULL AND state IN('exact','inferred')"),[],|r|r.get(0)).unwrap();
        assert_eq!(indexed,eligible,"partial index excludes only nontraversable rows");
    }
    let plans:Vec<String>=db.prepare("EXPLAIN QUERY PLAN SELECT n.id FROM key_nodes kn JOIN nodes n ON n.row=kn.node WHERE kn.key=?1 AND (kn.roles&?2)!=0").unwrap().query_map(params![1,1],|r|r.get(3)).unwrap().collect::<rusqlite::Result<_>>().unwrap();
    assert!(plans.iter().any(|p|p.contains("sqlite_autoindex_key_nodes_1")&&p.contains("key=?")),"{plans:?}");
}

#[test]
fn partition_adjacency_budget_refuses_instead_of_accepting_prefix() {
    let fixture=Fixture::new();let generation=fixture.stage(1);
    let caller=fixture.files[1].declarations.iter().find(|d|d.name=="caller").unwrap();
    let target=fixture.files[0].declarations.iter().find(|d|d.name=="target").unwrap();
    let query=CodeQuery { operation:Operation::Neighbors,seed_ids:vec![format!("decl:{}",caller.id)],budgets:QueryBudget { work:256,..Default::default() },..Default::default() };
    let snapshot=||generation.query_snapshot(&fixture.scope,&fixture.binding,fixture.receipt(1),"registration".into(),fixture.source.clone(),vec![],&query,&AtomicBool::new(false));
    assert!(snapshot().is_ok(),"ordinary metadata and real adjacency fit this budget");
    for i in 0..512 {generation.db.execute("INSERT INTO edges VALUES(?1,?2,?3,'exact',1,0)",params![format!("fanout-{i}"),caller.id,target.id]).unwrap();}
    let error=match snapshot(){Err(error)=>error,Ok(_)=>panic!("oversized adjacency must not return a successful prefix")};
    assert_eq!(error.code,"budget_exceeded");
}

#[test]
fn partition_generation_query_parity_and_lifecycle_boundaries() {
    let fixture = Fixture::new();
    // An uncommitted or merely sealed stage is disposable; it cannot replace
    // an active generation by existing as a file on disk.
    let stage = fixture.create(90); let path = stage.path.clone(); drop(stage); assert!(!path.exists());
    let mut stage = fixture.stage(91); stage.seal().unwrap(); let path = stage.path.clone(); drop(stage); assert!(!path.exists());
    let active_path = fixture.publish(1); assert!(active_path.exists());
    let generation = Generation::open(&fixture.main,&fixture.scope,&fixture.receipt(1)).unwrap();
    assert_eq!(generation.load(1).unwrap(),fixture.files[0]);
    for operation in [Operation::Capabilities,Operation::Overview,Operation::Catalog,Operation::Facets,Operation::Outline] {
        assert_parity(&fixture,&generation,&CodeQuery { operation,..Default::default() },false);
        for state in ["indexed","no_source_units","metadata","exact","unresolved"] {
            assert_parity(&fixture,&generation,&CodeQuery{operation,filter:QueryFilter{states:vec![state.into()],..Default::default()},..Default::default()},false);
        }
        assert_parity(&fixture,&generation,&CodeQuery { operation,filter:QueryFilter { kinds:vec!["declaration".into()],languages:vec!["python".into()],..Default::default() },..Default::default() },true);
    }
    for (text,channels) in [
        ("target",vec![Channel::Exact]),("action",vec![Channel::Exact]),
        ("http response",vec![Channel::Identifier]),("xarget",vec![Channel::Fuzzy]),
        ("arget",vec![Channel::Fuzzy]),("ttarget",vec![Channel::Fuzzy]),
        ("targat",vec![Channel::Fuzzy]),("target",vec![Channel::Exact,Channel::Identifier,Channel::Fuzzy]),
    ] {
        for filter in [QueryFilter::default(),QueryFilter { kinds:vec!["declaration".into()],..Default::default() },
            QueryFilter { states:vec!["exact".into()],..Default::default() },QueryFilter { languages:vec!["typescript".into()],..Default::default() },
            QueryFilter { path_prefix:Some("call".into()),..Default::default() }] {
            assert_parity(&fixture,&generation,&CodeQuery { query:Some(text.into()),channels:channels.clone(),filter,..Default::default() },false);
        }
    }
    let target = fixture.files[0].declarations.iter().find(|d|d.name=="target").unwrap();
    let caller = fixture.files[1].declarations.iter().find(|d|d.name=="caller").unwrap();
    let tiny=CodeQuery { operation:Operation::Neighbors,seed_ids:vec![format!("decl:{}",caller.id)],budgets:QueryBudget { work:1,..Default::default() },..Default::default() };
    let error=match generation.query_snapshot(&fixture.scope,&fixture.binding,fixture.receipt(1),"registration".into(),fixture.source.clone(),vec![],&tiny,&AtomicBool::new(false)){Err(error)=>error,Ok(_)=>panic!("tiny adjacency budget must refuse")};
    assert_eq!(error.code,"budget_exceeded","tiny metadata budget must refuse");
    for operation in [Operation::Inspect,Operation::Neighbors,Operation::Impact,Operation::Path] {
        for direction in [Direction::Incoming,Direction::Outgoing,Direction::Both] {
            let query = CodeQuery { operation,seed_ids:vec![format!("decl:{}",caller.id)],target_id:Some(format!("decl:{}",target.id)),direction:Some(direction),..Default::default() };
            if operation==Operation::Path&&direction!=Direction::Outgoing{
                let indexed=fixture.snapshot(&generation,&query,false);let legacy=fixture.oracle(&indexed);
                let actual=run_query(&indexed,&query,&AtomicBool::new(false),&[9;32]).unwrap_err();
                let expected=run_query(&legacy,&query,&AtomicBool::new(false),&[9;32]).unwrap_err();
                assert_eq!(serde_json::to_value(actual).unwrap(),serde_json::to_value(expected).unwrap());continue;
            }
            assert_parity(&fixture,&generation,&query,false);
        }
    }
    let mut query = CodeQuery { operation:Operation::Outline,budgets:QueryBudget { rows:2,..Default::default() },..Default::default() };
    let mut pages = 0;
    loop {
        let result = assert_parity(&fixture,&generation,&query,false);pages+=1;
        query.cursor = result.next_cursor;
        if query.cursor.is_none() { break; }
        assert!(pages<20,"bounded paging");
    }
    assert!(pages>1);
    let second_path = fixture.publish(2);
    // The old active is now a nonactive pinned reader. A third admission must
    // refuse instead of unlinking it; it cannot remove the new active either.
    assert!(Generation::create(&fixture.main,&fixture.scope,"checkout",&Fixture::run(3),"policy").is_err());
    assert!(active_path.exists()&&second_path.exists());
    drop(generation);
    let third = fixture.create(3);assert!(!active_path.exists());drop(third);assert!(second_path.exists());
}

#[test]
fn partition_sealed_identity_is_readonly_stable_and_detects_index_edits() {
    let fixture=Fixture::new();let mut stage=fixture.stage(1);stage.seal().unwrap();
    let identity=stage.stamp().unwrap();let path=stage.path.clone();
    assert!(stage.verify_stamp("").is_err());stage.verify_stamp(&identity).unwrap();
    stage.published();drop(stage);
    let reader=Generation::open(&fixture.main,&fixture.scope,&fixture.receipt(1)).unwrap();
    reader.verify_stamp(&identity).unwrap();reader.load(1).unwrap();reader.interfaces().unwrap();
    reader.verify_stamp(&identity).unwrap();drop(reader);
    let edit=Connection::open(&path).unwrap();
    edit.execute("UPDATE keys SET text=text||'changed' WHERE id=(SELECT min(id) FROM keys)",[]).unwrap();drop(edit);
    let reader=Generation::open(&fixture.main,&fixture.scope,&fixture.receipt(1)).unwrap();
    assert!(reader.verify_stamp(&identity).unwrap_err().starts_with("reindex_required:"));
    let changed_identity=reader.stamp().unwrap();drop(reader);
    let edit=Connection::open(&path).unwrap();edit.execute("DELETE FROM key_nodes WHERE key=(SELECT min(id) FROM keys)",[]).unwrap();drop(edit);
    let reader=Generation::open(&fixture.main,&fixture.scope,&fixture.receipt(1)).unwrap();
    assert!(reader.verify_stamp(&changed_identity).is_err());
}

#[test]
fn partition_missing_corrupt_oversize_and_nonregular_cache_fail_closed() {
    let fixture = Fixture::new();
    let path = fixture.publish(1);
    let bytes = std::fs::read(&path).unwrap();
    std::fs::remove_file(&path).unwrap();
    assert!(Generation::open(&fixture.main,&fixture.scope,&fixture.receipt(1)).is_err());
    std::fs::write(&path,&bytes).unwrap();
    #[cfg(unix)] {use std::os::unix::fs::PermissionsExt;std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o600)).unwrap();}
    let corrupt = Connection::open(&path).unwrap();
    corrupt.execute("UPDATE files SET payload=zeroblob(8388609) WHERE id=1",[]).unwrap();drop(corrupt);
    assert!(Generation::open(&fixture.main,&fixture.scope,&fixture.receipt(1)).is_err());
    std::fs::write(&path,&bytes).unwrap();
    let corrupt = Connection::open(&path).unwrap();
    corrupt.execute("UPDATE files SET payload_hash=?1 WHERE id=1",params!["0".repeat(64)]).unwrap();drop(corrupt);
    let generation = Generation::open(&fixture.main,&fixture.scope,&fixture.receipt(1)).unwrap();
    assert!(generation.load(1).unwrap_err().contains("corrupt"));drop(generation);
    #[cfg(unix)] {
        use std::os::unix::ffi::OsStrExt;
        std::fs::remove_file(&path).unwrap();
        let name = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe {libc::mkfifo(name.as_ptr(),0o600)},0);
        let start = std::time::Instant::now();
        assert!(Generation::open(&fixture.main,&fixture.scope,&fixture.receipt(1)).is_err());
        assert!(start.elapsed()<std::time::Duration::from_secs(2),"FIFO must be refused without blocking");
    }
}

#[cfg(unix)]
#[test]
fn partition_parent_replacement_cannot_redirect_staging_cleanup() {
    use std::os::unix::fs::symlink;
    let fixture = Fixture::new();
    let generation = fixture.create(1);
    let original = generation.directory.path.clone();
    let moved = original.with_extension("moved");
    let outside = fixture.root.join("outside");std::fs::create_dir(&outside).unwrap();
    let canary = outside.join(generation.path.file_name().unwrap());std::fs::write(&canary,b"must survive").unwrap();
    std::fs::rename(&original,&moved).unwrap();symlink(&outside,&original).unwrap();
    assert!(generation.check_disk().is_err());
    let name = generation.path.file_name().unwrap().to_owned();
    drop(generation);
    assert!(!moved.join(name).exists());assert_eq!(std::fs::read(canary).unwrap(),b"must survive");
}
