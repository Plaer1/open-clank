//! Real synthetic authority/metadata/index fixtures. Serial execution only;
//! all HOME/authority are owned temporary paths, never user stores/services.
#![cfg(unix)]
#[path="code_visual_summary_tests.rs"]
mod summary_tests;
use super::*;
use crate::code::{CodeWorkTicket,producer_fingerprint};
use rusqlite::Connection;
use std::path::PathBuf;
const SECRET:[u8;32]=[39;32];
struct Fixture{root:PathBuf,scope:GraphScope,binding:WorkspaceBinding,previous:Vec<(&'static str,Option<std::ffi::OsString>)>}
impl Fixture{
 fn new(partition:bool)->Self{
  let root=std::env::temp_dir().join(format!("fm-map-{}",uuid::Uuid::new_v4()));std::fs::create_dir(&root).unwrap();let root=root.canonicalize().unwrap();std::fs::create_dir(root.join("home")).unwrap();std::fs::create_dir(root.join("source")).unwrap();
  let previous=["HOME","OPEN_CLANK_AUTHORITY_DB_PATH"].into_iter().map(|k|(k,std::env::var_os(k))).collect();std::env::set_var("HOME",root.join("home"));std::env::set_var("OPEN_CLANK_AUTHORITY_DB_PATH",root.join("authority.sqlite"));
  let db=Connection::open(root.join("authority.sqlite")).unwrap();db.execute_batch("CREATE TABLE file_policy_subject_aliases(username TEXT,subject_id TEXT,is_admin INTEGER); CREATE TABLE file_policy_workspaces(id TEXT,owner_subject_id TEXT,location_id TEXT,relative_folder TEXT,archived INTEGER,revision INTEGER); CREATE TABLE file_policy_locations(id TEXT,canonical_path TEXT,capabilities_json TEXT,enabled INTEGER,availability TEXT); CREATE TABLE file_policy_meta(singleton INTEGER,generation INTEGER); CREATE TABLE file_policy_bindings(binding_class TEXT,subject_kind TEXT,subject_id TEXT,location_id TEXT,status TEXT,expires_unix_ms INTEGER,workspace_id TEXT,chat_id TEXT,lifetime TEXT,remaining_uses INTEGER,capabilities_json TEXT); INSERT INTO file_policy_subject_aliases VALUES('alice','subject-alice',0); INSERT INTO file_policy_workspaces VALUES('workspace','subject-alice','location','',0,1); INSERT INTO file_policy_meta VALUES(1,1); INSERT INTO file_policy_bindings VALUES('people','user','subject-alice','location','active',NULL,NULL,NULL,'always',NULL,'[\"read\"]'); INSERT INTO file_policy_bindings VALUES('agent','user','subject-alice','location','active',NULL,NULL,NULL,'always',NULL,'[\"read\"]');").unwrap();
  db.execute("INSERT INTO file_policy_locations VALUES('location',?1,'[\"read\"]',1,'available')",params![root.join("source").to_str().unwrap()]).unwrap();
  let scope=GraphScope::new("alice","workspace").unwrap();let binding=WorkspaceBinding{owner:"alice".into(),owner_subject_id:"subject-alice".into(),workspace_id:"workspace".into(),location_id:"location".into(),workspace_revision:1,policy_generation:1,root:root.join("source"),origin:"app".into(),subscriber_id:"map-fixture".into(),authority_expires_unix_ms:None};
  let f=Self{root,scope,binding,previous};
  f.write("app/caller.py","from lib.target import target\ndef caller():\n    target()\n    target()\n    value = target\n    return value\n");f.write("lib/target.py","def target():\n    return 1\n");f.write("lib/__init__.py","");
  f.write("types/model.ts","export interface Shape { name: string }; export function make(): Shape { return {name:'x'}; }\n");f.write("types/user.ts","import {make, Shape} from './model'; export function use(value: Shape): Shape { const other = make; return other(); }\n");
  f.write("assets/image.bin","not a source body");f.write(".gitignore","ignored.py\n");f.write("ignored.py","private ignored fixture");f.write(".ssh/secret.py","never exposed");
  if partition{for n in 0..129{f.write(&format!("noise/f{n:03}.py"),&format!("def unrelated_{n}(): return {n}\n"));}}f
 }
 fn write(&self,path:&str,text:&str){let path=self.binding.root.join(path);std::fs::create_dir_all(path.parent().unwrap()).unwrap();std::fs::write(path,text).unwrap();}
 fn store(&self)->SqliteStore{SqliteStore::new(self.root.join("fm.sqlite").to_str().unwrap(),4).unwrap()}
 fn prepare(&self,store:&SqliteStore)->CodeWorkTicket{let state=store.code_workspace_prepare(&self.scope,&self.binding,false).unwrap();CodeWorkTicket{checkout_id:state["checkout_id"].as_str().unwrap().into(),admission_epoch:state["admission_epoch"].as_i64().unwrap(),config_fingerprint:producer_fingerprint(),policy_generation:1,workspace_revision:1}}
 fn index(&self,store:&SqliteStore){let ticket=self.prepare(store);store.code_index_bound(&self.scope,&self.binding,&ticket).unwrap();}
 fn query(&self,store:&SqliteStore,request:MapRequest)->MapResult{match store.code_visual(&self.scope,&self.binding,&VisualRequest::Map(request),&Arc::new(AtomicBool::new(false)),&SECRET).unwrap(){VisualResult::Map(r)=>r,_=>panic!("wrong map mode")}}
 fn counts(&self,store:&SqliteStore)->(i64,i64,i64){let c=store.conn.lock().unwrap();(c.query_row("SELECT count(*) FROM code_registrations",[],|r|r.get(0)).unwrap(),c.query_row("SELECT count(*) FROM code_index_runs",[],|r|r.get(0)).unwrap(),c.query_row("SELECT count(*) FROM code_index_heads",[],|r|r.get(0)).unwrap())}
}
impl Drop for Fixture{fn drop(&mut self){VISUAL_TEST_HOOK.with(|s|s.borrow_mut().take());CONTEXT_DECODE_TEST_HOOK.with(|s|s.borrow_mut().take());for(k,v)in &self.previous{if let Some(v)=v{std::env::set_var(k,v)}else{std::env::remove_var(k)}}assert!(self.root.file_name().unwrap().to_str().unwrap().starts_with("fm-map-"));let _=std::fs::remove_dir_all(&self.root);}}
fn overview()->MapRequest{serde_json::from_value(serde_json::json!({"schema":SCHEMA,"operation":"overview"})).unwrap()}

#[test]
fn context_decode_releases_mutex_and_preserves_owned_snapshot(){
 let f=Fixture::new(false);let store=Arc::new(f.store());f.index(&store);
 let before=store.map_context(&f.scope,&f.binding).unwrap();let owned=store.clone();
 CONTEXT_DECODE_TEST_HOOK.with(|s|*s.borrow_mut()=Some(Box::new(move||{
  let conn=owned.conn.try_lock().expect("pure decode must not retain the store mutex");
  conn.execute("UPDATE code_index_runs SET coverage='not-json'",[]).unwrap();
 })));
 let captured=store.map_context(&f.scope,&f.binding).unwrap();
 assert_eq!(captured.receipt,before.receipt);assert_eq!(captured.source.unwrap().file_manifest_digest,before.source.unwrap().file_manifest_digest);
 assert_eq!(serde_json::to_value(captured.coverage).unwrap(),serde_json::to_value(before.coverage).unwrap());
 assert_eq!(store.map_context(&f.scope,&f.binding).err().unwrap().code,"reindex_required","next observation must read current corrupt metadata");
}

#[test]
fn context_decode_keeps_final_erase_rebind_and_policy_fences(){
 for mode in ["erase","rebind","policy"]{
  let f=Fixture::new(false);let store=Arc::new(f.store());f.index(&store);
  let checkout=store.map_context(&f.scope,&f.binding).unwrap().checkout;
  let owned=store.clone();let scope=f.scope.clone();let authority=f.root.join("authority.sqlite");let root=f.binding.root.clone();
  CONTEXT_DECODE_TEST_HOOK.with(|s|*s.borrow_mut()=Some(Box::new(move||{
   assert!(owned.conn.try_lock().is_ok());
   match mode{
    "erase"=>{owned.code_remove(&scope,&checkout).unwrap();},
    "rebind"=>{Connection::open(authority).unwrap().execute("UPDATE file_policy_workspaces SET revision=revision+1",[]).unwrap();},
    _=>{std::fs::write(root.join(".gitignore"),"app/\n").unwrap();}
   }
  })));
  assert!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(overview()),&Arc::new(AtomicBool::new(false)),&SECRET).is_err(),"{mode}");
 }
}

#[test]
fn context_decode_retains_malformed_and_bounded_metadata_rejection(){
 for (column,value,expected) in [("source_snapshot","not-json".to_string(),"reindex_required"),("coverage","not-json".to_string(),"reindex_required"),("source_snapshot","x".repeat(65537),"budget_exceeded"),("coverage","x".repeat(8*1024*1024+1),"budget_exceeded")]{
  let f=Fixture::new(false);let store=f.store();f.index(&store);
  store.conn.lock().unwrap().execute(&format!("UPDATE code_index_runs SET {column}=?1"),params![value]).unwrap();
  assert_eq!(store.map_context(&f.scope,&f.binding).err().unwrap().code,expected,"{column}");
 }
}

#[test]
fn visual_phase_capture_preserves_four_policy_checks_and_result(){
 let f=Fixture::new(false);let store=f.store();f.index(&store);
 assert!(crate::code_metrics::current_context().is_none());
 let (_,cold)=crate::code_metrics::capture_code_metrics(||{
  let result=f.query(&store,overview());
  store.code_visual_check(&f.scope,&f.binding,&result.authority,&Arc::new(AtomicBool::new(false))).unwrap();
 });
 // Cold summary installation adds its own final check; it is not removed to
 // fit the warm worker/delivery count below.
 assert_eq!(cold.stages[crate::code_metrics::Stage::MapContext as usize].counters.calls,2);
 assert_eq!(cold.stages[crate::code_metrics::Stage::MapPolicyEntry as usize].counters.calls,2);
 assert_eq!(cold.stages[crate::code_metrics::Stage::MapPolicyFinal as usize].counters.calls,3);
 assert_eq!(cold.stages[crate::code_metrics::Stage::MapProjection as usize].counters.calls,1);
 let (captured,metrics)=crate::code_metrics::capture_code_metrics(||{
  let result=f.query(&store,overview());
  store.code_visual_check(&f.scope,&f.binding,&result.authority,&Arc::new(AtomicBool::new(false))).unwrap();
  result
 });
 for (stage,count) in [(crate::code_metrics::Stage::MapContext,2),(crate::code_metrics::Stage::MapPolicyEntry,2),(crate::code_metrics::Stage::MapPolicyFinal,2),(crate::code_metrics::Stage::MapProjection,1)]{
  assert_eq!(metrics.stages[stage as usize].counters.calls,count);
 }
 for (stage,count) in [(crate::code_metrics::Stage::MapContextFence,2),(crate::code_metrics::Stage::MapContextConnectionWait,2),(crate::code_metrics::Stage::MapContextDecode,4)]{
  assert_eq!(metrics.stages[stage as usize].counters.calls,count);
 }
 assert!(crate::code_metrics::current_context().is_none());
 let normal=f.query(&store,overview());
 assert_eq!(serde_json::to_value(&captured.authority).unwrap(),serde_json::to_value(&normal.authority).unwrap());
 assert_eq!(serde_json::to_value(&captured.nodes).unwrap(),serde_json::to_value(&normal.nodes).unwrap());
 assert_eq!(serde_json::to_value(&captured.edges).unwrap(),serde_json::to_value(&normal.edges).unwrap());
}

const PREVIOUS_FILE_MAPPING_SQL:&str="SELECT f.id,CASE WHEN length(CAST(f.path AS BLOB))<=4096 THEN f.path END,CASE WHEN length(CAST(n.id AS BLOB))<=256 THEN n.id END FROM files f JOIN nodes n ON n.file=f.id AND n.kind='file' ORDER BY f.id LIMIT 10001";

#[test]
fn partition_file_mapping_preserves_order_and_avoids_occurrence_scan(){
 let mut db=Connection::open_in_memory().unwrap();
 db.execute_batch("CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT NOT NULL UNIQUE); CREATE TABLE nodes(row INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,file INTEGER NOT NULL,kind TEXT NOT NULL); CREATE INDEX nodes_file ON nodes(file,kind,id);").unwrap();
 let tx=db.transaction().unwrap();
 for file in 1..=129i64{
  tx.execute("INSERT INTO files VALUES(?1,?2)",params![file,format!("f{file:04}.py")]).unwrap();
  tx.execute("INSERT INTO nodes(id,file,kind) VALUES(?1,?2,'file')",params![format!("file:{file:064x}"),file]).unwrap();
  for ordinal in 0..128i64{tx.execute("INSERT INTO nodes(id,file,kind) VALUES(?1,?2,'occurrence')",params![format!("occ:{:064x}",file*128+ordinal),file]).unwrap();}
 }
 tx.commit().unwrap();
 let read=|sql:&str|{let mut s=db.prepare(sql).unwrap();let rows=s.query_map([],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,Option<String>>(2)?))).unwrap().collect::<Result<Vec<_>,_>>().unwrap();(rows,s.get_status(rusqlite::StatementStatus::VmStep))};
 let(old,old_vm)=read(PREVIOUS_FILE_MAPPING_SQL);let(new,new_vm)=read(PARTITION_FILE_MAPPING_SQL);
 assert_eq!(old,new);assert_eq!(new.len(),129);assert!(new.windows(2).all(|w|w[0].0<w[1].0));
 assert!(old_vm>new_vm*5,"old={old_vm} new={new_vm}");
 // Non-file nodes must never supply a projected resource. SQL retains both
 // original byte guards, and a missing file-node still returns no mapping.
 db.execute("UPDATE nodes SET id=?1 WHERE file=1 AND kind='file'",params!["x".repeat(257)]).unwrap();
 let(rows,_)=read(PARTITION_FILE_MAPPING_SQL);assert!(rows[0].2.is_none());
 db.execute("DELETE FROM nodes WHERE file=2 AND kind='file'",[]).unwrap();
 let(rows,_)=read(PARTITION_FILE_MAPPING_SQL);assert_eq!(rows.len(),128);assert!(!rows.iter().any(|r|r.0==2));
}

#[test]
fn partition_file_mapping_duplicate_rejected_before_resource_overwrite(){
 let cancel=AtomicBool::new(false);let mut work=Work::new(MAX_WORK,&cancel);
 let mut summary=structure(&[],&mut work).unwrap();
 let index=summary.nodes.len();summary.nodes.push(Vertex{path:"f.py".into(),kind:NodeKind::File,parent:Some(0),resource:None,source:0,assets:0,languages:BTreeMap::new()});
 let by_path=BTreeMap::from([("f.py".to_string(),index)]);let mut by_file=BTreeMap::new();
 project_partition_file(&mut summary,&by_path,&mut by_file,1,Some("f.py".into()),Some("file:ff".into())).unwrap();
 let error=project_partition_file(&mut summary,&by_path,&mut by_file,1,Some("f.py".into()),Some("file:00".into())).unwrap_err();
 assert_eq!(error.code,"invalid_evidence");assert_eq!(summary.nodes[index].resource.as_deref(),Some("file:ff"));assert_eq!(by_file,BTreeMap::from([(1,index)]));
}

#[test]
fn partition_file_mapping_real_generation_has_unique_file_nodes(){
 let f=Fixture::new(true);let store=f.store();f.index(&store);let context=store.map_context(&f.scope,&f.binding).unwrap();
 let conn=store.conn.lock().unwrap();let generation=Generation::open(&conn,&f.scope,&context.receipt.unwrap().evidence).unwrap();drop(conn);
 let duplicates:i64=generation.db.query_row("SELECT count(*) FROM (SELECT file FROM nodes WHERE kind='file' GROUP BY file HAVING count(*)!=1)",[],|r|r.get(0)).unwrap();assert_eq!(duplicates,0);
 let read=|sql:&str|{generation.db.prepare(sql).unwrap().query_map([],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,Option<String>>(2)?))).unwrap().collect::<Result<Vec<_>,_>>().unwrap()};
 let old=read(PREVIOUS_FILE_MAPPING_SQL);let new=read(PARTITION_FILE_MAPPING_SQL);assert_eq!(old,new);assert!(new.len()>128);
 let map=f.query(&store,overview());assert!(matches!(map.authority,MapAuthority::Published{..}));
}

const PREVIOUS_UNRESOLVED_SQL:&str="SELECT count(*) FROM edges e JOIN nodes o ON o.id='occ:'||e.occurrence AND o.file=e.file AND o.kind='occurrence' JOIN occurrence_kinds k ON k.occurrence=o.row WHERE k.kind IN(1,2) AND (e.state NOT IN('exact','inferred') OR e.target IS NULL)";

fn unresolved_sql_fixture()->Connection{
 let db=Connection::open_in_memory().unwrap();
 db.execute_batch("CREATE TABLE nodes(row INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,file INTEGER NOT NULL,kind TEXT NOT NULL,ordinal INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'metadata'); CREATE INDEX nodes_file ON nodes(file,kind,id); CREATE TABLE edges(occurrence TEXT PRIMARY KEY,caller TEXT,target TEXT,state TEXT NOT NULL,file INTEGER NOT NULL,ordinal INTEGER NOT NULL); CREATE TABLE occurrence_kinds(occurrence INTEGER PRIMARY KEY,kind INTEGER NOT NULL CHECK(kind IN(1,2,3,4))); CREATE INDEX edges_caller ON edges(caller) WHERE caller IS NOT NULL AND target IS NOT NULL AND state IN('exact','inferred'); CREATE INDEX edges_target ON edges(target) WHERE caller IS NOT NULL AND target IS NOT NULL AND state IN('exact','inferred');").unwrap();db
}

#[test]
fn partition_unresolved_exists_preserves_all_original_membership_predicates(){
 let db=unresolved_sql_fixture();
 for kind in [1,2,3,4]{for state in ["exact","inferred","unresolved","ambiguous",""]{for target in [None,Some("declaration")]{for shape in 0..5{
  db.execute_batch("DELETE FROM edges; DELETE FROM occurrence_kinds; DELETE FROM nodes;").unwrap();
  if shape!=1{db.execute("INSERT INTO nodes(row,id,file,kind,ordinal) VALUES(1,'occ:abc',?1,?2,0)",params![if shape==2{2}else{1},if shape==3{"declaration"}else{"occurrence"}]).unwrap();}
  if shape!=4{db.execute("INSERT INTO occurrence_kinds VALUES(1,?1)",params![kind]).unwrap();}
  db.execute("INSERT INTO edges VALUES('abc',NULL,?1,?2,1,0)",params![target,state]).unwrap();
  let old:i64=db.query_row(PREVIOUS_UNRESOLVED_SQL,[],|r|r.get(0)).unwrap();
  let actual:bool=db.query_row(PARTITION_HAS_UNRESOLVED_SQL,[],|r|r.get(0)).unwrap();
  let expected=shape==0&&kind<=2&&(!matches!(state,"exact"|"inferred")||target.is_none());
  assert_eq!(actual,old>0,"kind={kind} state={state} shape={shape}");assert_eq!(actual,expected);
 }}}}
 db.execute_batch("DELETE FROM edges; DELETE FROM occurrence_kinds; DELETE FROM nodes;").unwrap();
 assert!(!db.query_row(PARTITION_HAS_UNRESOLVED_SQL,[],|r|r.get::<_,bool>(0)).unwrap());
}

#[test]
fn partition_unresolved_exists_bounds_many_same_file_occurrences_with_real_sqlite_vm(){
 let mut db=unresolved_sql_fixture();let tx=db.transaction().unwrap();
 {
  let mut node=tx.prepare("INSERT INTO nodes(row,id,file,kind,ordinal) VALUES(?1,?2,1,'occurrence',?1)").unwrap();
  let mut edge=tx.prepare("INSERT INTO edges VALUES(?1,NULL,NULL,'unresolved',1,0)").unwrap();
  let mut kind=tx.prepare("INSERT INTO occurrence_kinds VALUES(?1,1)").unwrap();
  for i in 1..=16000{let id=format!("{i:064x}");node.execute(params![i,format!("occ:{id}")]).unwrap();edge.execute(params![id]).unwrap();kind.execute(params![i]).unwrap();}
 }tx.commit().unwrap();
 let cancel=Arc::new(AtomicBool::new(false));let mut work=Work::new(MAX_WORK,&cancel);sql_guard(&db,&work,&cancel);
 assert!(db.query_row(PARTITION_HAS_UNRESOLVED_SQL,[],|r|r.get::<_,bool>(0)).unwrap());
 db.progress_handler(0,None::<fn()->bool>);
 let positive_vm=work.vm.load(Ordering::Relaxed);assert!(positive_vm<=1000,"existence should stop at first matching occurrence, actual={positive_vm}");
 work=Work::new(MAX_WORK,&cancel);sql_guard(&db,&work,&cancel);
 let previous=db.query_row(PREVIOUS_UNRESOLVED_SQL,[],|r|r.get::<_,i64>(0));
 db.progress_handler(0,None::<fn()->bool>);
 let previous_vm=work.vm.load(Ordering::Relaxed);match previous{Ok(n)=>assert_eq!(n,16000),Err(e)=>assert!(e.to_string().contains("interrupted"),"unexpected prior-query failure: {e}")};
 // Worst-case absence still walks actual rows using the production SQL, with
 // unresolved-looking edges but excluded typed metadata. This forces identity
 // lookup rather than an edge-state-only shortcut. No timing-only oracle.
 db.execute("UPDATE occurrence_kinds SET kind=3",[]).unwrap();
 work=Work::new(MAX_WORK,&cancel);sql_guard(&db,&work,&cancel);
 assert!(!db.query_row(PARTITION_HAS_UNRESOLVED_SQL,[],|r|r.get::<_,bool>(0)).unwrap());
 db.progress_handler(0,None::<fn()->bool>);
 let absent_vm=work.vm.load(Ordering::Relaxed);assert!(absent_vm>0&&absent_vm<2_000_000,"keyed absence traversal must stay linear, actual={absent_vm}");
 let old:i64=db.query_row(PREVIOUS_UNRESOLVED_SQL,[],|r|r.get(0)).unwrap();assert_eq!(old,0);
 // Cancellation still interrupts the same SQL before summary admission.
 cancel.store(true,Ordering::Relaxed);work=Work::new(MAX_WORK,&cancel);sql_guard(&db,&work,&cancel);
 assert!(db.query_row(PARTITION_HAS_UNRESOLVED_SQL,[],|r|r.get::<_,bool>(0)).is_err());db.progress_handler(0,None::<fn()->bool>);
 println!("S07_UNRESOLVED_SQL sqlite={} rows=16000 positive_vm={positive_vm} previous_vm={previous_vm} absent_vm={absent_vm}",rusqlite::version());
}

#[test]
fn registered_no_head_preview_is_metadata_only_and_never_creates_index_authority(){
 let f=Fixture::new(false);let store=f.store();assert!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(overview()),&Arc::new(AtomicBool::new(false)),&SECRET).is_err());
 f.prepare(&store);let before=f.counts(&store);let r=f.query(&store,overview());assert!(matches!(r.authority,MapAuthority::StructurePreview{..}));assert!(r.coverage.indexed_files.is_none());assert_eq!(r.cost.source_bytes_read,0);assert!(r.edges.iter().all(|e|e.relation==RelationKind::Containment&&e.selection_id.is_none()));assert!(r.nodes.iter().all(|n|n.label!=".ssh"&&n.label!="ignored.py"));assert_eq!(f.counts(&store),before);
 store.code_visual_check(&f.scope,&f.binding,&r.authority,&Arc::new(AtomicBool::new(false))).unwrap();
 let request=MapSelectionRequest{schema:SCHEMA.into(),authority:r.authority.clone(),selection_id:"forged".into(),cursor:None,max_refs:50};assert!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Selection(request),&Arc::new(AtomicBool::new(false)),&SECRET).is_err());
 f.write("fresh/new.py","pass\n");assert!(store.code_visual_check(&f.scope,&f.binding,&r.authority,&Arc::new(AtomicBool::new(false))).is_err());assert_eq!(f.counts(&store),before);
}

fn set_head_producer(f:&Fixture,store:&SqliteStore,producer:&str){
 let changed=store.conn.lock().unwrap().execute("UPDATE code_index_runs SET config_fingerprint=?1 WHERE owner=?2 AND workspace_id=?3 AND run_id IN(SELECT run_id FROM code_index_heads WHERE owner=?2 AND workspace_id=?3)",params![producer,f.scope.owner,f.scope.workspace_id]).unwrap();assert_eq!(changed,1);
}

#[test]
fn incompatible_head_preview_uses_fresh_metadata_without_old_evidence_or_writes(){
 for partition in [false,true]{
  let f=Fixture::new(partition);let store=f.store();f.index(&store);
  let mut old_request=overview();old_request.limits.groups=2;let old=f.query(&store,old_request.clone());assert!(old.page.next_cursor.is_some());
  let published=f.query(&store,overview());let edge=published.edges.iter().find(|e|e.relation==RelationKind::Call).unwrap();
  let selection=MapSelectionRequest{schema:SCHEMA.into(),authority:published.authority.clone(),selection_id:edge.selection_id.clone().unwrap(),cursor:None,max_refs:50};
  set_head_producer(&f,&store,"previous-producer");let before=f.counts(&store);
  f.write("fresh/new.py","pass\n");f.write(".gitignore","ignored.py\napp/\n");
  let preview=f.query(&store,overview());assert!(matches!(preview.authority,MapAuthority::StructurePreview{..}));assert_eq!(preview.cost.source_bytes_read,0);assert!(!preview.cost.summary_cache_hit);assert!(preview.coverage.indexed_files.is_none());assert!(preview.selection.is_none());
  assert!(preview.nodes.iter().any(|n|n.label=="fresh"));assert!(preview.nodes.iter().all(|n|n.label!="app"));assert!(preview.edges.iter().all(|e|e.relation==RelationKind::Containment&&e.selection_id.is_none()));assert!(preview.nodes.iter().all(|n|published.nodes.iter().all(|old|old.id!=n.id)));
  store.code_visual_check(&f.scope,&f.binding,&preview.authority,&Arc::new(AtomicBool::new(false))).unwrap();
  assert_eq!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Selection(selection),&Arc::new(AtomicBool::new(false)),&SECRET).unwrap_err().code,"published_required");
  old_request.expected_authority=Some(old.authority.clone());old_request.cursor=old.page.next_cursor;
  assert_eq!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(old_request),&Arc::new(AtomicBool::new(false)),&SECRET).unwrap_err().code,"stale_cursor");
  assert!(store.code_visual_check(&f.scope,&f.binding,&published.authority,&Arc::new(AtomicBool::new(false))).is_err());assert_eq!(f.counts(&store),before);
 }
}

#[test]
fn incompatible_head_preview_rejects_corrupt_metadata_denial_and_erasure(){
 for mode in ["snapshot","coverage","revoke","erase"]{
  let f=Fixture::new(false);let store=f.store();f.index(&store);set_head_producer(&f,&store,"previous-producer");
  let checkout=store.map_context(&f.scope,&f.binding).unwrap().checkout;
  match mode{
   "snapshot"=>{store.conn.lock().unwrap().execute("UPDATE code_index_runs SET source_snapshot='not-json'",[]).unwrap();},
   "coverage"=>{store.conn.lock().unwrap().execute("UPDATE code_index_runs SET coverage='not-json'",[]).unwrap();},
   "revoke"=>{Connection::open(f.root.join("authority.sqlite")).unwrap().execute("UPDATE file_policy_bindings SET status='revoked' WHERE binding_class='people'",[]).unwrap();},
   _=>{store.code_remove(&f.scope,&checkout).unwrap();}
  }
  let before=f.counts(&store);assert!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(overview()),&Arc::new(AtomicBool::new(false)),&SECRET).is_err(),"{mode}");assert_eq!(f.counts(&store),before);
 }
}

#[test]
fn incompatible_head_preview_keeps_final_head_policy_cancel_and_revoke_fences(){
 for mode in ["head","policy","cancel","revoke","erase"]{
  let f=Fixture::new(false);let store=Arc::new(f.store());f.index(&store);set_head_producer(&f,&store,"previous-producer");
  let checkout=store.map_context(&f.scope,&f.binding).unwrap().checkout;let owned=store.clone();let scope=f.scope.clone();let root=f.binding.root.clone();let authority=f.root.join("authority.sqlite");let cancel=Arc::new(AtomicBool::new(false));let signal=cancel.clone();
  VISUAL_TEST_HOOK.with(|s|*s.borrow_mut()=Some(Box::new(move||match mode{
   "head"=>{owned.conn.lock().unwrap().execute("UPDATE code_index_runs SET config_fingerprint=?1",params![producer_fingerprint()]).unwrap();},
   "policy"=>{std::fs::write(root.join(".gitignore"),"app/\n").unwrap();},
   "cancel"=>signal.store(true,Ordering::Relaxed),
   "revoke"=>{Connection::open(authority).unwrap().execute("UPDATE file_policy_bindings SET status='revoked' WHERE binding_class='people'",[]).unwrap();},
   _=>{owned.code_remove(&scope,&checkout).unwrap();}
  })));
  assert!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(overview()),&cancel,&SECRET).is_err(),"{mode}");
 }
}

#[test]
fn incompatible_head_preview_is_invalidated_by_compatible_publication(){
 let f=Fixture::new(false);let store=f.store();f.index(&store);set_head_producer(&f,&store,"previous-producer");let preview=f.query(&store,overview());let before=f.counts(&store);
 // Restore the original valid producer metadata to model compatible publication
 // without requiring this read-only transition test to start another index run.
 set_head_producer(&f,&store,&producer_fingerprint());let published=f.query(&store,overview());assert!(matches!(published.authority,MapAuthority::Published{..}));assert!(published.nodes.iter().all(|n|preview.nodes.iter().all(|old|old.id!=n.id)));
 assert!(store.code_visual_check(&f.scope,&f.binding,&preview.authority,&Arc::new(AtomicBool::new(false))).is_err());
 let mut request=overview();request.expected_authority=Some(preview.authority);assert_eq!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(request),&Arc::new(AtomicBool::new(false)),&SECRET).unwrap_err().code,"stale_cursor");assert_eq!(f.counts(&store),before);
}

#[test]
fn small_and_partition_maps_have_real_connected_typed_edges_and_reversible_evidence(){
 for partition in [false,true]{
  let f=Fixture::new(partition);let store=f.store();f.index(&store);let r=f.query(&store,overview());let again=f.query(&store,overview());assert_eq!(r.authority,again.authority);assert!(again.cost.summary_cache_hit);assert_eq!(r.nodes,again.nodes);assert_eq!(r.edges,again.edges);assert_eq!(r.cost.source_bytes_read,0);assert!(matches!(r.authority,MapAuthority::Published{..}));
  let edge=r.edges.iter().find(|e|e.relation==RelationKind::Call&&e.state==RelationState::Exact).expect("real cross-folder call aggregate");
  let request=MapSelectionRequest{schema:SCHEMA.into(),authority:r.authority.clone(),selection_id:edge.selection_id.clone().unwrap(),cursor:None,max_refs:1};
  let selected=match store.code_visual(&f.scope,&f.binding,&VisualRequest::Selection(request.clone()),&Arc::new(AtomicBool::new(false)),&SECRET).unwrap(){VisualResult::Selection(r)=>r,_=>panic!("wrong selection mode")};selected.validate(&request).unwrap();assert_eq!(selected.references.len(),1);assert_eq!(selected.references[0].relation,RelationKind::Call);assert!(selected.references[0].caller_id.is_some());
  assert_eq!(edge.count,2);assert_eq!(selected.remaining,1);let mut continuation=request.clone();continuation.cursor=selected.next_cursor.clone();let tail=match store.code_visual(&f.scope,&f.binding,&VisualRequest::Selection(continuation.clone()),&Arc::new(AtomicBool::new(false)),&SECRET).unwrap(){VisualResult::Selection(r)=>r,_=>unreachable!()};tail.validate(&continuation).unwrap();assert!(tail.complete);assert_ne!(tail.references[0].occurrence_id,selected.references[0].occurrence_id);assert_eq!(tail.receipt,selected.receipt);
  let mut q=overview();q.operation=MapOperation::Neighbors;q.expected_authority=Some(r.authority.clone());q.root=Some(MapRoot::Visual{id:edge.from.clone()});let neighbors=f.query(&store,q.clone());assert!(neighbors.edges.iter().any(|e|e.relation==RelationKind::Call));assert!(neighbors.root.is_some());neighbors.validate(&q).unwrap();
  if partition{assert!(store.map_context(&f.scope,&f.binding).is_ok());}
 }
}

#[test]
fn small_resolution_ids_are_byte_bounded_in_sql_before_materialization(){
 let f=Fixture::new(false);let store=f.store();f.index(&store);let context=store.map_context(&f.scope,&f.binding).unwrap();let conn=store.conn.lock().unwrap();
 let original:String=conn.query_row("SELECT occurrence_id FROM code_resolutions WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3 LIMIT 1",params![f.scope.owner,f.scope.workspace_id,context.checkout],|r|r.get(0)).unwrap();
 for hostile in ["é".repeat(64),"x".repeat(128*1024)]{
  conn.execute("UPDATE code_resolutions SET occurrence_id=?1 WHERE owner=?2 AND workspace_id=?3 AND checkout_id=?4 AND occurrence_id=?5",params![hostile,f.scope.owner,f.scope.workspace_id,context.checkout,original]).unwrap();
  let mut stmt=conn.prepare(SMALL_RESOLUTION_METADATA_SQL).unwrap();let projected:Vec<Option<String>>=stmt.query_map(params![f.scope.owner,f.scope.workspace_id,context.checkout],|r|r.get(0)).unwrap().collect::<Result<_,_>>().unwrap();assert_eq!(projected.iter().filter(|v|v.is_none()).count(),1,"actual producer SQL must project NULL, not the hostile bytes");drop(stmt);
  let cancel=AtomicBool::new(false);let mut work=Work::new(MAX_WORK,&cancel);let error=small_summary(&conn,&f.scope,&context,&mut work).err().expect("oversize ID rejected before summary admission");assert_eq!(error.code,"budget_exceeded");assert!(store.code_visual_cache.state.lock().unwrap().entries.is_empty());
  conn.execute("UPDATE code_resolutions SET occurrence_id=?1 WHERE owner=?2 AND workspace_id=?3 AND checkout_id=?4 AND occurrence_id=?5",params![original,f.scope.owner,f.scope.workspace_id,context.checkout,hostile]).unwrap();
 }
}

#[test]
fn typed_filter_never_relabels_value_or_type_references_as_calls(){
 let cancel=AtomicBool::new(false);let mut work=Work::new(MAX_WORK,&cancel);let mut links=BTreeMap::new();
 for kind in ["call","import_reference","value_reference","type_reference"]{append_link(&mut links,1,2,kind,"exact",&mut work).unwrap();}
 assert_eq!(links.len(),2);assert_eq!(links.get(&(1,2,1,1)),Some(&1));assert_eq!(links.get(&(1,2,2,1)),Some(&1));assert!(append_link(&mut links,1,2,"invented","exact",&mut work).is_err());
 let f=Fixture::new(true);let store=f.store();f.index(&store);let context=store.map_context(&f.scope,&f.binding).unwrap();let conn=store.conn.lock().unwrap();let generation=Generation::open(&conn,&f.scope,&context.receipt.unwrap().evidence).unwrap();drop(conn);
 let kinds:BTreeSet<i64>=generation.db.prepare("SELECT DISTINCT kind FROM occurrence_kinds").unwrap().query_map([],|r|r.get(0)).unwrap().collect::<Result<_,_>>().unwrap();assert_eq!(kinds,BTreeSet::from([1,2,3,4]));
 generation.map_typed_metadata().unwrap();
}

#[test]
fn map_cursor_policy_scope_cancel_and_erase_guards_are_not_summary_cache_authority(){
 let f=Fixture::new(true);let store=f.store();f.index(&store);let mut request=overview();request.limits.groups=2;let first=f.query(&store,request.clone());assert!(first.page.next_cursor.is_some());
 request.expected_authority=Some(first.authority.clone());request.cursor=first.page.next_cursor.clone();let next=f.query(&store,request.clone());assert_ne!(next.page.next_cursor,request.cursor);assert_eq!(next.authority,first.authority);
 let mut wrong=request.clone();wrong.direction=MapDirection::Incoming;assert!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(wrong),&Arc::new(AtomicBool::new(false)),&SECRET).is_err());
 assert_eq!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(overview()),&Arc::new(AtomicBool::new(true)),&SECRET).unwrap_err().code,"cancelled");
 f.write(".gitignore","ignored.py\napp/\n");assert!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(overview()),&Arc::new(AtomicBool::new(false)),&SECRET).is_err());
 f.write(".gitignore","ignored.py\n");let before=f.query(&store,overview());assert!(before.cost.summary_cache_hit);
 let authority=Connection::open(f.root.join("authority.sqlite")).unwrap();authority.execute("UPDATE file_policy_bindings SET status='revoked' WHERE binding_class='people'",[]).unwrap();assert!(store.code_visual_check(&f.scope,&f.binding,&before.authority,&Arc::new(AtomicBool::new(false))).is_err());
 authority.execute("UPDATE file_policy_bindings SET status='active' WHERE binding_class='people'",[]).unwrap();let checkout=match before.authority{MapAuthority::Published{receipt,..}=>receipt.evidence.checkout_id,_=>unreachable!()};store.code_remove(&f.scope,&checkout).unwrap();assert!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(overview()),&Arc::new(AtomicBool::new(false)),&SECRET).is_err());assert!(store.code_visual_cache.state.lock().unwrap().entries.is_empty());
}

#[test]
fn summary_reservations_are_bounded_and_invalidation_cannot_publish_inflight_build(){
 let cache=SummaryCache::default();let cancel=AtomicBool::new(false);let mut work=Work::new(MAX_WORK,&cancel);let summary=structure(&[],&mut work).unwrap();let key=serde_json::to_string(&vec!["alice","workspace","checkout"]).unwrap();
 let build=cache.reserve(&key).unwrap();assert!(cache.reserve("other").is_err());cache.invalidate("alice",Some("workspace"),None);assert!(build.finish(key.clone(),summary).is_err());assert!(cache.get(&key).is_none());
 let mut pins=Vec::new();for n in 0..SUMMARY_ENTRIES{let key=format!("key{n}");let build=cache.reserve(&key).unwrap();let summary=structure(&[],&mut work).unwrap();pins.push(build.finish(key,summary).unwrap());}assert!(cache.reserve("fifth").is_err());drop(pins);assert!(cache.reserve("next").is_ok());
 let cache=SummaryCache::default();let key=serde_json::to_string(&vec!["alice","workspace","checkout"]).unwrap();let lease=cache.reserve(&key).unwrap().finish(key.clone(),structure(&[],&mut work).unwrap()).unwrap();cache.invalidate("alice",Some("workspace"),None);assert!(cache.get(&key).is_none());assert_eq!(cache.state.lock().unwrap().entries.len(),1,"pinned invalidated data remains charged");drop(lease);assert!(cache.state.lock().unwrap().entries.is_empty(),"last reader drains invalidated metadata immediately");
}

#[test]
fn typed_metadata_absence_refuses_map_without_migrating_or_disabling_generic_evidence(){
 use std::os::unix::fs::MetadataExt;
 let f=Fixture::new(true);let store=f.store();f.index(&store);f.query(&store,overview());let context=store.map_context(&f.scope,&f.binding).unwrap();let receipt=context.receipt.unwrap();
 let path={let conn=store.conn.lock().unwrap();let directory=super::super::code_partitioned::directory_scope(&conn,&f.scope,&receipt.evidence.checkout_id,false).unwrap().unwrap();directory.path.join(format!("generation_{}.sqlite",receipt.evidence.head_generation.strip_prefix("codegen_").unwrap()))};assert!(path.canonicalize().unwrap().starts_with(&f.root));
 let writer=Connection::open(&path).unwrap();writer.execute_batch("DELETE FROM meta WHERE key='visual_kind_version'; DROP TABLE occurrence_kinds;").unwrap();drop(writer);let m=std::fs::metadata(&path).unwrap();let stamp=blake3::hash(format!("{}:{}:{}:{}:{}:{}:{}",m.dev(),m.ino(),m.len(),m.mtime(),m.mtime_nsec(),m.ctime(),m.ctime_nsec()).as_bytes()).to_hex().to_string();
 store.conn.lock().unwrap().execute("UPDATE code_index_runs SET source_snapshot=json_set(source_snapshot,'$.evidence_cache_identity',?1) WHERE owner=?2 AND workspace_id=?3 AND run_id=?4",params![stamp,f.scope.owner,f.scope.workspace_id,receipt.evidence.head_generation]).unwrap();let before=f.counts(&store);
 assert_eq!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(overview()),&Arc::new(AtomicBool::new(false)),&SECRET).unwrap_err().code,"reindex_required");assert_eq!(f.counts(&store),before);
 let generic=crate::code_query::CodeQuery{operation:crate::code_query::Operation::Capabilities,..Default::default()};assert!(store.code_query(&f.scope,&f.binding,&generic,&AtomicBool::new(false),&SECRET).unwrap().error.is_none());
 let reader=Connection::open_with_flags(path,rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();let tables:i64=reader.query_row("SELECT count(*) FROM sqlite_schema WHERE name='occurrence_kinds'",[],|r|r.get(0)).unwrap();assert_eq!(tables,0,"read did not migrate the immutable cache");
}

#[test]
fn direct_app_only_contract_and_bounded_preview_scanner_keep_old_selection_semantics(){
 let f=Fixture::new(false);let store=f.store();f.prepare(&store);let mut agent=f.binding.clone();agent.origin="agent".into();assert_eq!(store.code_visual(&f.scope,&agent,&VisualRequest::Map(overview()),&Arc::new(AtomicBool::new(false)),&SECRET).unwrap_err().code,"denied");
 let identity=crate::code::directory_identity(&f.binding.root).unwrap();let cancel=AtomicBool::new(false);let full=crate::code::scan_codebase_pinned(&f.binding.root,&identity).unwrap();let bounded=crate::code::scan_visual_inventory(&f.binding.root,&identity,&cancel,100_000).unwrap();assert!(same_inventory(&full,&bounded));
 assert!(crate::code::scan_visual_inventory(&f.binding.root,&identity,&cancel,1).unwrap_err().starts_with("budget_exceeded"));assert!(crate::code::scan_visual_inventory(&f.binding.root,&identity,&AtomicBool::new(true),100_000).unwrap_err().starts_with("cancelled"));
}

#[test]
fn high_degree_page_endpoints_progress_complete_and_root_outside_first_overview(){
 let cancel=AtomicBool::new(false);let mut work=Work::new(MAX_WORK,&cancel);let mut rows=vec![ScanCoverage{rel_path:"selected.py".into(),status:"indexed".into(),..Default::default()}];for n in 0..220{rows.push(ScanCoverage{rel_path:format!("neighbor{n:03}.py"),status:"indexed".into(),..Default::default()});}
 let mut summary=structure(&rows,&mut work).unwrap();let root=summary.nodes.iter().position(|n|n.path=="selected.py").unwrap();summary.links=(1..summary.nodes.len()).filter(|i|*i!=root).map(|i|Link{from:root,to:i,kind:1,state:1,count:1}).collect();summary.charge().unwrap();
 let authority:MapAuthority=serde_json::from_value(serde_json::from_str::<serde_json::Value>(include_str!("../../tests/fixtures/code_map_published.json")).unwrap()["authority"].clone()).unwrap();let mut request=overview();request.expected_authority=Some(authority.clone());request.operation=MapOperation::Neighbors;request.root=Some(MapRoot::Visual{id:token("vn1",&[root],"fixture-scope",&SECRET)});request.relations=vec![RelationKind::Call];request.limits.nodes=20;
 let mut cursors=BTreeSet::new();let mut edges=BTreeSet::new();let mut pages=0;loop{let mut page=rendered_page(&summary,&authority,&request,root,"fixture-scope",&SECRET,&mut work).unwrap();finalize_map(&mut page,&request).unwrap();assert!(page.nodes.iter().any(|n|Some(&n.id)==page.root.as_ref()));for e in &page.edges{assert!(edges.insert(e.id.clone()));assert!(page.nodes.iter().any(|n|n.id==e.from));assert!(page.nodes.iter().any(|n|n.id==e.to));}pages+=1;if page.page.complete{assert!(page.page.next_cursor.is_none());break;}let cursor=page.page.next_cursor.unwrap();assert!(cursors.insert(cursor.clone()));request.cursor=Some(cursor);assert!(pages<128);}
 assert!(pages>1);assert_eq!(edges.len(),220);
 let mut request=overview();request.limits.groups=1;let first=rendered_page(&summary,&authority,&request,0,"fixture-scope",&SECRET,&mut work).unwrap();assert!(first.nodes.iter().all(|n|n.id!=token("vn1",&[root],"fixture-scope",&SECRET)),"fixed selected file deliberately outside first overview");
}

#[test]
fn disconnected_empty_assets_and_parse_failed_coverage_never_fabricate_dependencies(){
 for rows in [Vec::new(),vec![ScanCoverage{rel_path:"one.py".into(),status:"no_source_units".into(),..Default::default()},ScanCoverage{rel_path:"two.py".into(),status:"indexed".into(),..Default::default()}],vec![ScanCoverage{rel_path:"image.png".into(),status:"unsupported".into(),..Default::default()},ScanCoverage{rel_path:"bad.py".into(),status:"parse_failed".into(),..Default::default()},ScanCoverage{rel_path:"secret.py".into(),status:"ignored".into(),..Default::default()}]]{
  let cancel=AtomicBool::new(false);let mut work=Work::new(MAX_WORK,&cancel);let mut summary=structure(&rows,&mut work).unwrap();summary.coverage=coverage(&rows,true);assert!(summary.links.is_empty());assert_eq!(summary.coverage.indexed_files,Some(rows.iter().filter(|r|matches!(r.status.as_str(),"indexed"|"no_source_units")).count() as u64));if rows.iter().any(|r|r.status=="parse_failed"){assert_eq!(summary.coverage.dependencies,DependencyCoverage::Partial);}assert!(summary.nodes.iter().all(|n|n.path!="secret.py"));
 }
}

#[test]
fn preview_membership_and_control_changes_during_read_are_rejected(){
 for control in [false,true]{let f=Fixture::new(false);let store=f.store();f.prepare(&store);let root=f.binding.root.clone();VISUAL_TEST_HOOK.with(|s|*s.borrow_mut()=Some(Box::new(move||{if control{std::fs::write(root.join(".gitignore"),"app/\n").unwrap();}else{std::fs::create_dir(root.join("fresh")).unwrap();std::fs::write(root.join("fresh/new.py"),"pass\n").unwrap();}})));assert!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(overview()),&Arc::new(AtomicBool::new(false)),&SECRET).is_err());}
}

#[test]
fn published_cancel_revoke_and_erase_during_cached_projection_never_disclose_result(){
 for mode in ["cancel","revoke","erase"]{let f=Fixture::new(true);let store=Arc::new(f.store());f.index(&store);let first=f.query(&store,overview());let scope=f.scope.clone();let binding=f.binding.clone();let authority=f.root.join("authority.sqlite");let cancel=Arc::new(AtomicBool::new(false));let signal=cancel.clone();let owned=store.clone();let checkout=match first.authority{MapAuthority::Published{receipt,..}=>receipt.evidence.checkout_id,_=>unreachable!()};
  VISUAL_TEST_HOOK.with(|s|*s.borrow_mut()=Some(Box::new(move||match mode{"cancel"=>signal.store(true,Ordering::Relaxed),"revoke"=>{Connection::open(authority).unwrap().execute("UPDATE file_policy_bindings SET status='revoked' WHERE binding_class='people'",[]).unwrap();},_=>{let _=owned.code_remove(&scope,&checkout);}})));
  assert!(store.code_visual(&f.scope,&binding,&VisualRequest::Map(overview()),&cancel,&SECRET).is_err());
 }
}

#[test]
fn preview_to_published_changes_namespace_and_rejects_late_preview_geometry(){
 let f=Fixture::new(false);let store=f.store();f.prepare(&store);let preview=f.query(&store,overview());let old=preview.nodes[0].id.clone();f.index(&store);let published=f.query(&store,overview());assert!(matches!(published.authority,MapAuthority::Published{..}));assert!(published.nodes.iter().all(|n|n.id!=old));assert!(store.code_visual_check(&f.scope,&f.binding,&preview.authority,&Arc::new(AtomicBool::new(false))).is_err());let mut request=overview();request.operation=MapOperation::Neighbors;request.expected_authority=Some(published.authority);request.root=Some(MapRoot::Visual{id:old});assert!(store.code_visual(&f.scope,&f.binding,&VisualRequest::Map(request),&Arc::new(AtomicBool::new(false)),&SECRET).is_err());
}
