//! Real partition fixtures; process HOME/authority require --test-threads=1.
//! Source-authored only until root releases the native test slot.
use super::*;
use crate::code_metrics::{capture_code_metrics,CodeMetrics,Stage};

struct Fixture {
 root:PathBuf,previous:Vec<(&'static str,Option<std::ffi::OsString>)>,
 scope:GraphScope,binding:WorkspaceBinding,
}
impl Fixture {
 fn new()->Self {
  let root=std::env::temp_dir().join(format!("fm-capabilities-{}",uuid::Uuid::new_v4()));
  std::fs::create_dir(&root).unwrap();let root=root.canonicalize().unwrap();
  for name in ["source","home"]{std::fs::create_dir(root.join(name)).unwrap();}
  let previous=["HOME","OPEN_CLANK_AUTHORITY_DB_PATH"].into_iter().map(|k|(k,std::env::var_os(k))).collect();
  std::env::set_var("HOME",root.join("home"));std::env::set_var("OPEN_CLANK_AUTHORITY_DB_PATH",root.join("authority.sqlite"));
  let db=Connection::open(root.join("authority.sqlite")).unwrap();
  db.execute_batch("CREATE TABLE file_policy_subject_aliases(username TEXT,subject_id TEXT,is_admin INTEGER);
   CREATE TABLE file_policy_workspaces(id TEXT,owner_subject_id TEXT,location_id TEXT,relative_folder TEXT,archived INTEGER,revision INTEGER);
   CREATE TABLE file_policy_locations(id TEXT,canonical_path TEXT,capabilities_json TEXT,enabled INTEGER,availability TEXT);
   CREATE TABLE file_policy_meta(singleton INTEGER,generation INTEGER);
   CREATE TABLE file_policy_bindings(binding_class TEXT,subject_kind TEXT,subject_id TEXT,location_id TEXT,status TEXT,expires_unix_ms INTEGER,workspace_id TEXT,chat_id TEXT,lifetime TEXT,remaining_uses INTEGER,capabilities_json TEXT);
   INSERT INTO file_policy_subject_aliases VALUES('alice','subject-alice',0);
   INSERT INTO file_policy_workspaces VALUES('workspace','subject-alice','location','',0,1);
   INSERT INTO file_policy_meta VALUES(1,1);
   INSERT INTO file_policy_bindings VALUES('people','user','subject-alice','location','active',NULL,NULL,NULL,'always',NULL,'[\"read\"]');
   INSERT INTO file_policy_bindings VALUES('agent','user','subject-alice','location','active',NULL,NULL,NULL,'always',NULL,'[\"read\"]');").unwrap();
  db.execute("INSERT INTO file_policy_locations VALUES('location',?1,'[\"read\"]',1,'available')",params![root.join("source").to_str().unwrap()]).unwrap();
  let scope=GraphScope::new("alice","workspace").unwrap();
  let binding=WorkspaceBinding{owner:"alice".into(),owner_subject_id:"subject-alice".into(),workspace_id:"workspace".into(),location_id:"location".into(),workspace_revision:1,policy_generation:1,root:root.join("source"),origin:"agent".into(),subscriber_id:"capabilities-fixture".into(),authority_expires_unix_ms:None};
  for n in 0..129 {std::fs::write(binding.root.join(format!("f{n:03}.py")),format!("def filler_{n}(): return {n}\n")).unwrap();}
  std::fs::write(binding.root.join("empty.py"),"# valid zero units\n").unwrap();
  std::fs::write(binding.root.join("broken.py"),"def broken(:\n").unwrap();
  Self{root,previous,scope,binding}
 }
 fn store(&self)->SqliteStore{SqliteStore::new(self.root.join("fm.sqlite").to_str().unwrap(),4).unwrap()}
 fn index(&self,store:&SqliteStore)->CodeIndexResult {
  let state=store.code_workspace_prepare(&self.scope,&self.binding,false).unwrap();
  let ticket=CodeWorkTicket{checkout_id:state["checkout_id"].as_str().unwrap().into(),admission_epoch:state["admission_epoch"].as_i64().unwrap(),config_fingerprint:producer_fingerprint(),policy_generation:1,workspace_revision:1};
  let result=store.code_index_bound(&self.scope,&self.binding,&ticket).unwrap();
  assert_eq!(result.source_snapshot.evidence_store_version,STORE_VERSION);assert!(result.files_indexed+result.files_unchanged>128);result
 }
 fn query(&self,store:&SqliteStore,q:&CodeQuery)->Result<CodeQueryResult,QueryError>{store.code_query(&self.scope,&self.binding,q,&AtomicBool::new(false),&[57;32])}
 fn snapshot(&self,store:&SqliteStore,q:&CodeQuery)->Result<QuerySnapshot,QueryError>{
  let conn=store.conn.lock().unwrap();let receipt=super::super::code_evidence::current_receipt(&conn,&self.scope,&self.binding,None).unwrap();
  let(registration,source,coverage):(String,String,String)=conn.query_row("SELECT c.registration_id,r.source_snapshot,r.coverage FROM code_index_runs r JOIN code_registrations c ON c.owner=r.owner AND c.workspace_id=r.workspace_id AND c.checkout_id=r.codebase WHERE r.owner=?1 AND r.workspace_id=?2 AND r.codebase=?3 AND r.run_id=?4",params![self.scope.owner,self.scope.workspace_id,receipt.checkout_id,receipt.head_generation],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap();
  let generation=Generation::open(&conn,&self.scope,&receipt).unwrap();drop(conn);
  generation.query_snapshot(&self.scope,&self.binding,receipt,registration,serde_json::from_str(&source).unwrap(),serde_json::from_str(&coverage).unwrap(),q,&AtomicBool::new(false))
 }
 fn mutate_cache(&self,store:&SqliteStore,sql:&str,refresh_stamp:bool){
  let receipt=store.code_evidence_receipt(&self.scope,&self.binding).unwrap();let conn=store.conn.lock().unwrap();
  let generation=Generation::open(&conn,&self.scope,&receipt).unwrap();drop(conn);
  // Close the immutable reader transaction before deliberately corrupting
  // this owned fixture, including its shared file lock.
  let path=generation.path.clone();drop(generation);
  let writer=Connection::open(&path).unwrap();writer.execute_batch(sql).unwrap();drop(writer);
  if refresh_stamp {use std::os::unix::fs::MetadataExt;let path=path.canonicalize().unwrap();assert!(path.starts_with(self.root.canonicalize().unwrap()));let m=std::fs::metadata(path).unwrap();
   let stamp=blake3::hash(format!("{}:{}:{}:{}:{}:{}:{}",m.dev(),m.ino(),m.len(),m.mtime(),m.mtime_nsec(),m.ctime(),m.ctime_nsec()).as_bytes()).to_hex().to_string();
   store.conn.lock().unwrap().execute("UPDATE code_index_runs SET source_snapshot=json_set(source_snapshot,'$.evidence_cache_identity',?1) WHERE owner=?2 AND workspace_id=?3 AND run_id=?4",params![stamp,self.scope.owner,self.scope.workspace_id,receipt.head_generation]).unwrap();}
 }
}
impl Drop for Fixture {fn drop(&mut self){
 AUDIT_TEST_HOOK.with(|s|s.borrow_mut().take());
 for(k,v)in &self.previous{if let Some(v)=v{std::env::set_var(k,v)}else{std::env::remove_var(k)}}
 assert!(self.root.file_name().unwrap().to_str().unwrap().starts_with("fm-capabilities-"));let _=std::fs::remove_dir_all(&self.root);
}}
fn query()->CodeQuery{CodeQuery{operation:Operation::Capabilities,..Default::default()}}
fn calls(metrics:&CodeMetrics,stage:Stage)->u64{metrics.stages[stage as usize].counters.calls}
fn noncost(result:&CodeQueryResult)->serde_json::Value{let mut value=serde_json::to_value(result).unwrap();value.as_object_mut().unwrap().remove("cost");value}

#[test]
fn capabilities_match_old_full_snapshot_without_headers_or_eligible_sql(){
 let fixture=Fixture::new();let store=fixture.store();fixture.index(&store);
 let mut variants=vec![query()];
 variants.push(CodeQuery{filter:QueryFilter{path_prefix:Some("f".into()),kinds:vec!["declaration".into()],..Default::default()},channels:vec![Channel::Semantic],fields:vec![],..query()});
 for q in variants {
  // Before this seam, Capabilities/Facets shared the exact same snapshot
  // preparation arm (all headers + exact eligible JOIN, no selected payload).
  // Retain that production arm as the old-snapshot oracle, then project with
  // the ORIGINAL Capabilities request/fingerprint, not a Facets response.
  let old=fixture.snapshot(&store,&CodeQuery{operation:Operation::Facets,..q.clone()}).unwrap();
  let old_result=run_query(&old,&q,&AtomicBool::new(false),&[57;32]).unwrap();
  let(fast,metrics)=capture_code_metrics(||fixture.snapshot(&store,&q));let fast=fast.unwrap();
  assert_eq!(calls(&metrics,Stage::QueryPrepare),1);assert_eq!(calls(&metrics,Stage::QuerySql),0);
  assert!(fast.files.is_empty()&&fast.resolutions.is_empty());assert!(fast.eligible_records.is_none());
  assert_eq!(serde_json::to_value(&fast.coverage).unwrap(),serde_json::to_value(&old.coverage).unwrap());assert_eq!(fast.receipt,old.receipt);assert_eq!(fast.scope_key,old.scope_key);
  assert_eq!(fast.metadata_bytes,0);assert_eq!(fast.metadata_records,fast.coverage.len());
  assert!(old.metadata_bytes>fast.metadata_bytes);assert_eq!(old.metadata_records,fast.metadata_records+old.files.len());
  let result=run_query(&fast,&q,&AtomicBool::new(false),&[57;32]).unwrap();
  assert_eq!(noncost(&result),noncost(&old_result));assert_eq!(noncost(&fixture.query(&store,&q).unwrap()),noncost(&result));
  assert_eq!(result.cost.metadata_bytes,0);assert_eq!(result.cost.metadata_records_validated,fast.coverage.len());
  assert_eq!(result.cost.files_bytes_read,0);assert_eq!(result.cost.validation_bytes_read,0);
  assert!(result.cost.serialized_bytes<=q.budgets.bytes.min(q.budgets.tokens));
 }
 let(_,m)=capture_code_metrics(||fixture.query(&store,&query()).unwrap());
 assert_eq!(calls(&m,Stage::GenerationAuditHit),1);assert_eq!(calls(&m,Stage::EligibleCountHit),0);assert_eq!(calls(&m,Stage::EligibleCountMiss),0);assert_eq!(calls(&m,Stage::QuerySql),0);
}

#[test]
fn capabilities_keep_query_cursor_receipt_cancel_and_envelope_guards(){
 let fixture=Fixture::new();let store=fixture.store();fixture.index(&store);let first=fixture.query(&store,&query()).unwrap();
 let mut invalid=query();invalid.schema="wrong".into();assert_eq!(fixture.query(&store,&invalid).unwrap_err().code,"invalid");
 let mut invalid=query();invalid.budgets.work=0;assert_eq!(fixture.query(&store,&invalid).unwrap_err().code,"invalid");
 let mut invalid=query();invalid.filter.path_prefix=Some("".into());assert_eq!(fixture.query(&store,&invalid).unwrap_err().code,"invalid");
 let mut wrong=query();let mut receipt=first.receipt.clone().unwrap();receipt.evidence.head_generation="codegen_00000000000000000000000000000000".into();wrong.expected_receipt=Some(receipt);assert!(fixture.query(&store,&wrong).is_err());
 let mut cursor=query();cursor.cursor=Some("not-a-valid-cursor".into());assert_eq!(fixture.query(&store,&cursor).unwrap_err().code,"stale_cursor");
 assert_eq!(store.code_query(&fixture.scope,&fixture.binding,&query(),&AtomicBool::new(true),&[57;32]).unwrap_err().code,"cancelled");
 let mut tiny=query();tiny.budgets.work=1;assert_eq!(fixture.query(&store,&tiny).unwrap_err().code,"budget_exceeded");
 let mut tiny=query();tiny.budgets.bytes=512;tiny.budgets.tokens=512;assert_eq!(fixture.query(&store,&tiny).unwrap_err().code,"budget_exceeded");
 // Fewer actual preparation records can legitimately permit a work budget
 // that could not pay for the old unused headers. No fabricated old charge.
 let old=fixture.snapshot(&store,&CodeQuery{operation:Operation::Facets,..query()}).unwrap();
 let mut reduced=query();reduced.budgets.work=old.coverage.len();
 assert_eq!(run_query(&old,&reduced,&AtomicBool::new(false),&[57;32]).unwrap_err().code,"budget_exceeded");
 assert!(fixture.query(&store,&reduced).is_ok());
}

#[test]
fn capabilities_keep_current_policy_authority_scope_head_and_erase_fences(){
 let fixture=Fixture::new();let store=fixture.store();let indexed=fixture.index(&store);let first=fixture.query(&store,&query()).unwrap();
 let mut other=fixture.scope.clone();other.workspace_id="different-workspace".into();
 assert_eq!(store.code_query(&other,&fixture.binding,&query(),&AtomicBool::new(false),&[57;32]).unwrap_err().code,"denied");
 std::fs::write(fixture.binding.root.join(".gitignore"),"f000.py\n").unwrap();
 assert_eq!(fixture.query(&store,&query()).unwrap_err().code,"reindex_required");std::fs::remove_file(fixture.binding.root.join(".gitignore")).unwrap();
 let authority=fixture.root.join("authority.sqlite");
 Connection::open(&authority).unwrap().execute("UPDATE file_policy_bindings SET status='revoked' WHERE binding_class='agent'",[]).unwrap();
 assert_eq!(fixture.query(&store,&query()).unwrap_err().code,"denied");
 Connection::open(&authority).unwrap().execute("UPDATE file_policy_bindings SET status='active' WHERE binding_class='agent'",[]).unwrap();
 // An exact no-change verification intentionally retains the ready head.
 // This test needs a real changed publication before demanding stale receipt.
 std::fs::write(fixture.binding.root.join("f000.py"),"def changed_filler(): return 99\n").unwrap();
 let changed=fixture.index(&store);assert_ne!(store.code_index_head(&fixture.scope,&indexed.codebase).unwrap().unwrap().run_id,indexed.run_id);assert_eq!(store.code_index_head(&fixture.scope,&indexed.codebase).unwrap().unwrap().run_id,changed.run_id);
 let mut stale=query();stale.expected_receipt=first.receipt;assert_eq!(fixture.query(&store,&stale).unwrap_err().code,"stale_cursor");
 assert!(fixture.query(&store,&query()).is_ok());store.code_remove(&fixture.scope,&indexed.codebase).unwrap();
 assert_eq!(fixture.query(&store,&query()).unwrap_err().code,"erased");
 store.code_workspace_prepare(&fixture.scope,&fixture.binding,true).unwrap();assert_eq!(fixture.query(&store,&query()).unwrap_err().code,"missing");
}

#[test]
fn capabilities_do_not_disclose_or_decode_unselected_corrupt_header_but_keep_audits(){
 for mode in ["header-json","payload-hash","counts","oversize-header","stamp"] {
  let fixture=Fixture::new();let store=fixture.store();fixture.index(&store);fixture.query(&store,&query()).unwrap();
  let sql=match mode {
   "header-json"=>"UPDATE files SET header='{' WHERE id=1",
   "payload-hash"=>"UPDATE files SET payload_hash='0000000000000000000000000000000000000000000000000000000000000000' WHERE id=1",
   "counts"=>"UPDATE meta SET value='99999999' WHERE key='nodes'",
   "oversize-header"=>"UPDATE files SET header=replace(hex(zeroblob(32769)),'0','x') WHERE id=1",
   _=>"UPDATE meta SET value='stamp-change' WHERE key='policy'",
  };
  fixture.mutate_cache(&store,sql,mode!="stamp");
  if mode=="header-json"||mode=="payload-hash" {
   // Explicit operation-specific boundary: a newly stamp-bound internal
   // corruption fixture may still report constant capabilities, no evidence.
   let result=fixture.query(&store,&query()).unwrap();assert!(result.hits.is_empty()&&result.edges.is_empty());
   assert_eq!(result.cost.metadata_bytes,0);
   if mode=="header-json" {assert!(fixture.snapshot(&store,&CodeQuery{operation:Operation::Facets,..query()}).is_err());}
   else {let receipt=store.code_evidence_receipt(&fixture.scope,&fixture.binding).unwrap();let conn=store.conn.lock().unwrap();let generation=Generation::open(&conn,&fixture.scope,&receipt).unwrap();assert!(generation.load(1).is_err());}
  }else{assert!(fixture.query(&store,&query()).is_err(),"{mode}");}
 }
}

#[test]
fn capabilities_do_not_skip_authority_recheck_after_audited_open(){
 let fixture=Fixture::new();let store=fixture.store();fixture.index(&store);
 let authority=fixture.root.join("authority.sqlite");
 AUDIT_TEST_HOOK.with(|slot|*slot.borrow_mut()=Some(Box::new(move||{
  Connection::open(authority).unwrap().execute("UPDATE file_policy_bindings SET status='revoked' WHERE binding_class='agent'",[]).unwrap();
 })));
 assert_eq!(fixture.query(&store,&query()).unwrap_err().code,"denied");
}
