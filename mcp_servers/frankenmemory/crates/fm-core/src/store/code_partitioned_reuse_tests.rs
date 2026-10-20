//! Source-authored only until root releases the serialized native test slot.
//! Actual partition/store fixtures; run --test-threads=1 (HOME/authority).
use super::*;
use crate::code_metrics::{capture_code_metrics,CodeMetrics,Stage};

struct ReuseFixture { root:PathBuf,previous:Vec<(&'static str,Option<std::ffi::OsString>)>,scope:GraphScope,binding:WorkspaceBinding,preserve:bool }
impl ReuseFixture {
 fn new(fillers:usize)->Self {
  let root=std::env::temp_dir().join(format!("fm-partition-reuse-{}",uuid::Uuid::new_v4()));std::fs::create_dir(&root).unwrap();let root=root.canonicalize().unwrap();
  for child in ["source","home"]{std::fs::create_dir(root.join(child)).unwrap();}
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
  db.execute("INSERT INTO file_policy_locations VALUES('location',?1,'[\"read\"]',1,'available')",params![root.join("source").to_str().unwrap()]).unwrap();drop(db);
  let scope=GraphScope::new("alice","workspace").unwrap();
  let binding=WorkspaceBinding{owner:"alice".into(),owner_subject_id:"subject-alice".into(),workspace_id:"workspace".into(),location_id:"location".into(),workspace_revision:1,policy_generation:1,root:root.join("source"),origin:"agent".into(),subscriber_id:"reuse-fixture".into(),authority_expires_unix_ms:None};
  let fixture=Self{root,previous,scope,binding,preserve:false};
  for n in 0..fillers {fixture.write(&format!("f{n:03}.py"),&format!("def filler_{n}(): return {n}\n"));}
  fixture.write("lib.py","def target(): return 1\n");fixture.write("caller.py","from lib import target, future_name\ndef caller():\n    target()\n    return future_name()\n");
  fixture.write("empty.py","# valid zero source units\n");fixture.write("broken.py","def broken(:\n");
  std::fs::write(fixture.binding.root.join("binary.py"),b"\0binary").unwrap();fixture
 }
 fn write(&self,path:&str,text:&str){std::fs::write(self.binding.root.join(path),text).unwrap();}
 fn store(&self)->SqliteStore{SqliteStore::new(self.root.join("fm.sqlite").to_str().unwrap(),4).unwrap()}
 fn run(&self,store:&SqliteStore,binding:&WorkspaceBinding)->Result<(CodeIndexResult,CodeMetrics),String>{
  let scope=GraphScope::new(&binding.owner,&binding.workspace_id).unwrap();
  let state=store.code_workspace_prepare(&scope,binding,false)?;
  let ticket=CodeWorkTicket{checkout_id:state["checkout_id"].as_str().unwrap().into(),admission_epoch:state["admission_epoch"].as_i64().unwrap(),config_fingerprint:crate::code::producer_fingerprint(),policy_generation:binding.policy_generation,workspace_revision:binding.workspace_revision};
  let(result,metrics)=capture_code_metrics(||store.code_index_bound(&scope,binding,&ticket));result.map(|result|(result,metrics))
 }
 fn index(&self,store:&SqliteStore)->(CodeIndexResult,CodeMetrics){self.run(store,&self.binding).unwrap()}
 fn generation(&self,store:&SqliteStore)->Generation{
  let receipt=store.code_evidence_receipt(&self.scope,&self.binding).unwrap();let conn=store.conn.lock().unwrap();Generation::open(&conn,&self.scope,&receipt).unwrap()
 }
 fn cache_path(&self,store:&SqliteStore)->PathBuf{let generation=self.generation(store);generation.path.clone()}
 fn exact_target(&self,store:&SqliteStore,name:&str)->bool{
  let generation=self.generation(store);let mut names=BTreeMap::new();let mut resolutions=Vec::new();
  let ids:Vec<i64>=generation.db.prepare("SELECT id FROM files ORDER BY path").unwrap().query_map([],|r|r.get(0)).unwrap().map(Result::unwrap).collect();
  for id in ids{let file=generation.load(id).unwrap();for declaration in file.declarations{names.insert(declaration.id,(file.rel_path.clone(),declaration.name));}resolutions.extend(generation.resolutions(id).unwrap());}
  resolutions.iter().any(|r|r.state==ResolutionState::Exact&&r.caller_id.as_ref().and_then(|id|names.get(id)).is_some_and(|(path,_)|path=="caller.py")&&r.target_id.as_ref().and_then(|id|names.get(id)).is_some_and(|(path,target)|path=="lib.py"&&target==name))
 }
 fn clean_parity(&self,store:&SqliteStore,result:&CodeIndexResult){
  // Independent full extraction + whole-corpus resolution oracle, same
  // checkout/current sources. Do not normalize away semantic properties/IDs.
  let identity=directory_identity(&self.binding.root).unwrap();let scan=scan_codebase_pinned(&self.binding.root,&identity).unwrap();
  let mut budget=ExtractionBudget::default();budget.max_serialized_bytes=storage::MAX_DECODED;
  let mut clean=BTreeMap::new();let mut failures=0;
  for path in scan.files {let acquired=acquire_source_pinned(&self.binding.root,&path,&identity).unwrap();match index_acquired_budget(&result.codebase,&self.binding.root,&path,&acquired,&budget){Ok(file)=>{clean.insert(file.rel_path.clone(),file);},Err(_)=>failures+=1}}
  assert_eq!(failures,result.errors.len());assert_eq!(clean.len(),result.files_indexed+result.files_unchanged);
  let unavailable=result.coverage.iter().filter(|c|eligible_unavailable(c)).map(|c|UnavailableFile{rel_path:c.rel_path.clone(),source_hash:c.source_hash.clone(),reason:c.status.clone()}).collect();
  let oracle=resolve_evidence(&CorpusInput{checkout_id:result.codebase.clone(),files:clean.values().map(|f|f.evidence.clone()).collect(),unavailable_files:unavailable},&ResolutionBudget::default()).unwrap();
  let generation=self.generation(store);let mut actual_resolutions=Vec::new();
  let ids:Vec<i64>=generation.db.prepare("SELECT id FROM files ORDER BY path").unwrap().query_map([],|r|r.get(0)).unwrap().map(Result::unwrap).collect();
  assert_eq!(ids.len(),clean.len());
  for id in ids {let file=generation.load(id).unwrap();let expected=clean.get(&file.rel_path).unwrap();assert_eq!(file,expected.evidence);
   let graph:Vec<u8>=generation.db.query_row("SELECT graph FROM files WHERE id=?1",params![id],|r|r.get(0)).unwrap();
   let graph:crate::graph::GraphUpsertInput=storage::decode(&graph).unwrap();assert_eq!(serde_json::to_value(graph).unwrap(),serde_json::to_value(&expected.upsert).unwrap());
   actual_resolutions.extend(generation.resolutions(id).unwrap());
  }
  actual_resolutions.sort_by(|a,b|a.occurrence_id.cmp(&b.occurrence_id));let mut expected=oracle.resolutions;expected.sort_by(|a,b|a.occurrence_id.cmp(&b.occurrence_id));assert_eq!(actual_resolutions,expected);
 }
}
impl Drop for ReuseFixture{fn drop(&mut self){REUSE_LOAD_TEST_HOOK.with(|s|s.borrow_mut().take());REUSE_STAGE_TEST_HOOK.with(|s|s.borrow_mut().take());NO_CHANGE_TEST_HOOK.with(|s|s.borrow_mut().take());NO_CHANGE_FINAL_TEST_HOOK.with(|s|s.borrow_mut().take());for(k,v)in &self.previous{if let Some(v)=v{std::env::set_var(k,v)}else{std::env::remove_var(k)}}assert!(self.root.file_name().unwrap().to_str().unwrap().starts_with("fm-partition-reuse-"));if !self.preserve {let _=std::fs::remove_dir_all(&self.root);}}}

#[path="code_index_s07_tests.rs"]
mod s07_tests;
fn calls(metrics:&CodeMetrics,stage:Stage)->u64{metrics.stages[stage as usize].counters.calls}

fn prior_audit_open(fixture:&ReuseFixture,store:&SqliteStore,binding:Option<&WorkspaceBinding>)->Result<Option<PriorExtraction>,String>{
 // Match the production pre-lock authority check; the helper itself performs
 // no admission/index/publication. Derive the exact published policy receipt.
 if let Some(binding)=binding{binding.validate()?;}
 let receipt={let conn=store.conn.lock().unwrap();super::super::code_evidence::current_receipt(&conn,&fixture.scope,binding.unwrap_or(&fixture.binding),None).map_err(err)?};
 let identity=directory_identity(&fixture.binding.root)?;
 let policy=scan_codebase_pinned(&fixture.binding.root,&identity)?.policy.digest();
 let conn=store.conn.lock().unwrap();
 PriorExtraction::from_head(&conn,&fixture.scope,&receipt.checkout_id,&receipt.head_generation,&policy,binding,&store.code_query_audit)
}

#[test]
fn prior_audit_real_receipts_share_metadata_memo_with_query_and_map_without_skipping_rich_checks(){
 for first in ["prior","query","map"]{
  let fixture=ReuseFixture::new(129);let store=fixture.store();fixture.index(&store);
  assert!(store.code_query_audit.entries.lock().unwrap().is_empty());
  let(_,metrics)=capture_code_metrics(||match first{
   "prior"=>{drop(prior_audit_open(&fixture,&store,Some(&fixture.binding)).unwrap().unwrap());},
   "query"=>{store.code_query(&fixture.scope,&fixture.binding,&CodeQuery::default(),&AtomicBool::new(false),&[71;32]).unwrap();},
   "map"=>{let mut binding=fixture.binding.clone();binding.origin="app".into();let request=serde_json::from_value(serde_json::json!({"schema":crate::code_visual::SCHEMA,"operation":"overview"})).unwrap();store.code_visual(&fixture.scope,&binding,&crate::code_visual::VisualRequest::Map(request),&std::sync::Arc::new(AtomicBool::new(false)),&[71;32]).unwrap();},
   _=>unreachable!(),
  });
  assert_eq!(calls(&metrics,Stage::GenerationAuditMiss),1,"{first}");
  let(_,metrics)=capture_code_metrics(||drop(prior_audit_open(&fixture,&store,Some(&fixture.binding)).unwrap().unwrap()));
  assert_eq!((calls(&metrics,Stage::GenerationAuditHit),calls(&metrics,Stage::GenerationAuditMiss)),(1,0));
  let receipt=store.code_evidence_receipt(&fixture.scope,&fixture.binding).unwrap();
  {let entries=store.code_query_audit.entries.lock().unwrap();assert_eq!(entries.len(),1);assert_eq!(entries[0].0.admission,receipt.admission_epoch);assert_eq!(entries[0].0.policy,fixture.binding.policy_generation);}
  let before=s04_canonical_generation(&fixture,&store);
  let loaded=std::rc::Rc::new(std::cell::Cell::new(false));let observed=loaded.clone();REUSE_LOAD_TEST_HOOK.with(|s|*s.borrow_mut()=Some(Box::new(move||observed.set(true))));
  let(warm,metrics)=fixture.index(&store);assert!(loaded.get(),"memo hit must still load/validate rich evidence");
  assert_eq!(calls(&metrics,Stage::GenerationAuditHit),1);assert_eq!(calls(&metrics,Stage::GenerationAuditMiss),0);assert_eq!(calls(&metrics,Stage::Extract),0);assert_eq!(calls(&metrics,Stage::Resolve),0);
  assert_eq!(warm.files_unchanged,132);assert_eq!(s04_canonical_generation(&fixture,&store),before);fixture.clean_parity(&store,&warm);
  let(_,metrics)=capture_code_metrics(||store.code_query(&fixture.scope,&fixture.binding,&CodeQuery::default(),&AtomicBool::new(false),&[71;32]).unwrap());
  assert_eq!(calls(&metrics,Stage::GenerationAuditHit),1);
 }
}

#[test]
fn prior_audit_legacy_unbound_never_seeds_or_borrows_synthetic_epoch_memo(){
 let fixture=ReuseFixture::new(129);let store=fixture.store();fixture.index(&store);
 for _ in 0..2{let(_,metrics)=capture_code_metrics(||drop(prior_audit_open(&fixture,&store,None).unwrap().unwrap()));assert_eq!(calls(&metrics,Stage::GenerationAuditMiss),1);assert_eq!(calls(&metrics,Stage::GenerationAuditHit),0);assert!(store.code_query_audit.entries.lock().unwrap().is_empty());}
 drop(prior_audit_open(&fixture,&store,Some(&fixture.binding)).unwrap().unwrap());
 let(_,metrics)=capture_code_metrics(||drop(prior_audit_open(&fixture,&store,None).unwrap().unwrap()));assert_eq!(calls(&metrics,Stage::GenerationAuditHit),0);assert_eq!(calls(&metrics,Stage::GenerationAuditMiss),1);assert_eq!(store.code_query_audit.entries.lock().unwrap().len(),1);
}

#[test]
fn prior_audit_actual_epoch_policy_and_revocation_cannot_borrow_old_receipts(){
 let fixture=ReuseFixture::new(129);let store=fixture.store();let(cold,_)=fixture.index(&store);
 drop(prior_audit_open(&fixture,&store,Some(&fixture.binding)).unwrap().unwrap());
 let receipt=store.code_evidence_receipt(&fixture.scope,&fixture.binding).unwrap();
 store.conn.lock().unwrap().execute("UPDATE code_registrations SET admission_epoch=admission_epoch+1 WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![fixture.scope.owner,fixture.scope.workspace_id,cold.codebase]).unwrap();
 let(_,metrics)=capture_code_metrics(||drop(prior_audit_open(&fixture,&store,Some(&fixture.binding)).unwrap().unwrap()));assert_eq!(calls(&metrics,Stage::GenerationAuditHit),0);assert_eq!(calls(&metrics,Stage::GenerationAuditMiss),1);
 Connection::open(fixture.root.join("authority.sqlite")).unwrap().execute("UPDATE file_policy_meta SET generation=generation+1 WHERE singleton=1",[]).unwrap();
 assert!(prior_audit_open(&fixture,&store,Some(&fixture.binding)).is_err());
 let mut current=fixture.binding.clone();current.policy_generation+=1;
 let(_,metrics)=capture_code_metrics(||drop(prior_audit_open(&fixture,&store,Some(&current)).unwrap().unwrap()));assert_eq!(calls(&metrics,Stage::GenerationAuditMiss),1);assert_eq!(calls(&metrics,Stage::GenerationAuditHit),0);
 {let entries=store.code_query_audit.entries.lock().unwrap();assert!(entries.iter().any(|(key,_)|key.admission==receipt.admission_epoch+1&&key.policy==current.policy_generation));}
 Connection::open(fixture.root.join("authority.sqlite")).unwrap().execute("UPDATE file_policy_bindings SET status='revoked' WHERE binding_class='agent'",[]).unwrap();
 let(result,metrics)=capture_code_metrics(||prior_audit_open(&fixture,&store,Some(&current)));assert!(result.is_err());assert_eq!(calls(&metrics,Stage::GenerationAuditHit),0);
}

fn s04_canonical_generation(fixture:&ReuseFixture,store:&SqliteStore)->serde_json::Value{
 let generation=fixture.generation(store);
 let ids:Vec<i64>=generation.db.prepare("SELECT id FROM files ORDER BY path").unwrap().query_map([],|r|r.get(0)).unwrap().map(Result::unwrap).collect();
 let mut files=Vec::new();for id in ids{
  let evidence=generation.load(id).unwrap();let packed:Vec<u8>=generation.db.query_row("SELECT graph FROM files WHERE id=?1",params![id],|r|r.get(0)).unwrap();let graph:crate::graph::GraphUpsertInput=storage::decode(&packed).unwrap();let resolutions=generation.resolutions(id).unwrap();
  files.push(serde_json::json!({"evidence":evidence,"graph":graph,"resolutions":resolutions}));
 }serde_json::json!(files)
}

#[test]
fn s04_one_two_four_partition_workers_preserve_complete_canonical_semantics(){
 let fixture=ReuseFixture::new(129);let mut oracle=None;
 let seed=fixture.root.join("registered-seed.sqlite");
 {let store=SqliteStore::new(seed.to_str().unwrap(),4).unwrap();store.code_workspace_prepare(&fixture.scope,&fixture.binding,false).unwrap();}
 for threads in [1,2,4]{
  // Copy only a closed synthetic, registered-but-unindexed database. Every
  // cold run now has identical scope/checkout IDs, without normalizing them.
  let database=fixture.root.join(format!("workers-{threads}.sqlite"));std::fs::copy(&seed,&database).unwrap();
  let store=SqliteStore::new(database.to_str().unwrap(),4).unwrap();let scheduler=store.index_scheduler();scheduler.configure(1,threads).unwrap();
  let(result,metrics)=fixture.index(&store);assert_eq!((result.files_indexed,result.files_unchanged,result.errors.len()),(132,0,2));assert_eq!(calls(&metrics,Stage::Extract),134);fixture.clean_parity(&store,&result);assert!(fixture.exact_target(&store,"target"));
  let canonical=s04_canonical_generation(&fixture,&store);if let Some(expected)=&oracle{assert_eq!(&canonical,expected,"all IDs/properties/occurrences/resolutions, not only counts");}else{oracle=Some(canonical);}
  assert_eq!(scheduler.status().active,0);assert_eq!(scheduler.status().effective,threads);
  let(warm,metrics)=fixture.index(&store);assert_eq!(warm.files_unchanged,132);assert_eq!(calls(&metrics,Stage::Extract),0);assert_eq!(calls(&metrics,Stage::Resolve),0);assert_eq!(s04_canonical_generation(&fixture,&store),oracle.clone().unwrap());
 }
}

struct S04OutputLimit;
impl S04OutputLimit{fn force()->Self{S04_SMALL_OUTPUT_TEST_LIMIT.with(|limit|limit.set(64));Self}}
impl Drop for S04OutputLimit{fn drop(&mut self){S04_SMALL_OUTPUT_TEST_LIMIT.with(|limit|limit.set(8*1024*1024));S04_UPGRADE_TEST_HOOK.with(|hook|hook.borrow_mut().take());}}

struct S04WorkerHook;
impl S04WorkerHook{fn install(hook:std::sync::Arc<dyn Fn(&Path,bool)+Send+Sync>)->Self{S04_WORKER_TEST_HOOK.with(|slot|*slot.borrow_mut()=Some(hook));Self}}
impl Drop for S04WorkerHook{fn drop(&mut self){S04_WORKER_TEST_HOOK.with(|slot|slot.borrow_mut().take());}}

#[test]
fn s04_adverse_parser_completion_keeps_staging_and_canonical_relations_in_source_order(){
 let fixture=ReuseFixture::new(129);fixture.write("a.py","from b import selected_target\ndef selected_caller(): return selected_target()\n");fixture.write("b.py","def selected_target(): return 1\n");let store=fixture.store();
 let completed=std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));let recorded=completed.clone();let barrier=std::sync::Arc::new(std::sync::Barrier::new(2));
 let _hook=S04WorkerHook::install(std::sync::Arc::new(move|path,after|{
  if !after{return;}match path.file_name().and_then(|name|name.to_str()){
   Some("a.py")=>{barrier.wait();recorded.lock().unwrap().push(0);},
   Some("b.py")=>{recorded.lock().unwrap().push(1);barrier.wait();},_=>{}
  }
 }));
 let(result,metrics)=fixture.index(&store);assert_eq!((result.files_indexed,result.errors.len()),(134,2));assert_eq!(calls(&metrics,Stage::Extract),136);assert_eq!(*completed.lock().unwrap(),vec![1,0]);fixture.clean_parity(&store,&result);
 let generation=fixture.generation(&store);let first:Vec<String>=generation.db.prepare("SELECT path FROM files ORDER BY id LIMIT 2").unwrap().query_map([],|r|r.get(0)).unwrap().map(Result::unwrap).collect();assert_eq!(first,["a.py","b.py"],"staging follows source sequence, not callback completion");
 assert_eq!(store.index_scheduler().test_accounting(),(0,0,0,0));
}

#[test]
fn s04_held_cold_parser_cancel_erase_and_authorization_revoke_never_late_publish_and_recover(){
 for mode in ["cancel","erase","revoke"]{
  let fixture=ReuseFixture::new(129);let store=fixture.store();let(first,_)=fixture.index(&store);
  fixture.write(".gitignore","# force a policy-distinct cold extraction, same sources\n");
  let identity=directory_identity(&fixture.binding.root).unwrap();let scan=scan_codebase_pinned(&fixture.binding.root,&identity).unwrap();assert_ne!(scan.policy.digest(),first.source_snapshot.selection_policy_digest);
  let(entered,seen)=std::sync::mpsc::channel();let release=std::sync::Arc::new(std::sync::Barrier::new(2));let worker_release=release.clone();
  let hook:std::sync::Arc<dyn Fn(&Path,bool)+Send+Sync>=std::sync::Arc::new(move|path,after|{if !after&&path.file_name().is_some_and(|name|name=="caller.py"){entered.send(()).unwrap();worker_release.wait();}});
  let outcome=std::thread::scope(|threads|{
   let fixture_ref=&fixture;let store_ref=&store;let job=threads.spawn(move||{let _hook=S04WorkerHook::install(hook);fixture_ref.run(store_ref,&fixture_ref.binding)});
   seen.recv().expect("cold task reached captured pre-extraction barrier");
   match mode{
    "cancel"=>store.code_workspace_cancel(&fixture.scope,&first.codebase).unwrap(),
    "erase"=>{let result=store.code_remove(&fixture.scope,&first.codebase);assert!(result.is_ok()||result.err().unwrap().to_string().contains("cache_cleanup_pending"));},
    _=>{Connection::open(fixture.root.join("authority.sqlite")).unwrap().execute("UPDATE file_policy_bindings SET status='revoked' WHERE binding_class='agent'",[]).unwrap();}
   }
   release.wait();job.join().unwrap()
  });
  assert!(outcome.is_err(),"{mode} must reject old task output");let head=store.code_index_head(&fixture.scope,&first.codebase).unwrap();if mode=="erase"{assert!(head.is_none());}else{assert_eq!(head.unwrap().run_id,first.run_id);}
  assert_eq!(store.index_scheduler().status().active,0);assert_eq!(store.index_scheduler().test_accounting(),(0,0,0,0));
  if mode=="revoke"{Connection::open(fixture.root.join("authority.sqlite")).unwrap().execute("UPDATE file_policy_bindings SET status='active' WHERE binding_class='agent'",[]).unwrap();}
  store.code_workspace_prepare(&fixture.scope,&fixture.binding,true).unwrap();let(recovered,_)=fixture.index(&store);fixture.clean_parity(&store,&recovered);assert!(fixture.exact_target(&store,"target"));assert_eq!(store.index_scheduler().test_accounting(),(0,0,0,0));
 }
}

#[test]
fn s04_internal_output_upgrade_releases_batch_before_exclusive_retry_and_never_records_parse_failure(){
 let fixture=ReuseFixture::new(129);let store=fixture.store();let _limit=S04OutputLimit::force();
 let(result,metrics)=fixture.index(&store);assert_eq!((result.files_indexed,result.errors.len()),(132,2));assert!(result.errors.iter().all(|e|!e.contains("budget_exceeded")));assert!(calls(&metrics,Stage::Extract)>134);assert!(calls(&metrics,Stage::Extract)<=268,"at most one larger retry per source");fixture.clean_parity(&store,&result);assert!(fixture.exact_target(&store,"target"));assert_eq!(store.index_scheduler().status().active,0);
}

#[test]
fn s04_real_over_eight_mib_evidence_uses_single_large_retry_with_complete_semantics(){
 let fixture=ReuseFixture::new(129);let store=fixture.store();
 let source=(0..6000).map(|n|format!("def large_result_{n:05}(): return {n}\n")).collect::<String>();assert!(source.len()<=512*1024);fixture.write("large.py",&source);
 let(result,metrics)=fixture.index(&store);assert_eq!((result.files_indexed,result.errors.len()),(133,2));assert!(calls(&metrics,Stage::Extract)>135);assert!(calls(&metrics,Stage::Extract)<=270);
 // The old whole-corpus resolver intentionally validates each file against
 // 8 MiB. This fixture requires the 64 MiB active-partition contract instead,
 // using fresh serial extraction/interfaces as an independent input oracle.
 let identity=directory_identity(&fixture.binding.root).unwrap();let scan=scan_codebase_pinned(&fixture.binding.root,&identity).unwrap();let mut budget=ExtractionBudget::default();budget.max_serialized_bytes=storage::MAX_DECODED;
 let mut interfaces=Vec::new();let mut selected=BTreeMap::new();let mut failures=0;
 for path in scan.files{let acquired=acquire_source_pinned(&fixture.binding.root,&path,&identity).unwrap();match index_acquired_budget(&result.codebase,&fixture.binding.root,&path,&acquired,&budget){
  Ok(file)=>{interfaces.push(storage::module_interface(&file.evidence));if matches!(file.rel_path.as_str(),"large.py"|"caller.py"){selected.insert(file.rel_path.clone(),file);}},Err(_)=>failures+=1
 }}assert_eq!(failures,2);assert_eq!(interfaces.len(),133);
 let unavailable:Vec<_>=result.coverage.iter().filter(|c|eligible_unavailable(c)).map(|c|UnavailableFile{rel_path:c.rel_path.clone(),source_hash:c.source_hash.clone(),reason:c.status.clone()}).collect();
 let generation=fixture.generation(&store);
 for(rel,expected)in selected{
  let id:i64=generation.db.query_row("SELECT id FROM files WHERE path=?1",params![rel],|r|r.get(0)).unwrap();let evidence=generation.load(id).unwrap();assert_eq!(evidence,expected.evidence);let packed:Vec<u8>=generation.db.query_row("SELECT graph FROM files WHERE id=?1",params![id],|r|r.get(0)).unwrap();let graph:crate::graph::GraphUpsertInput=storage::decode(&packed).unwrap();assert_eq!(serde_json::to_value(graph).unwrap(),serde_json::to_value(expected.upsert).unwrap());
  let input=CorpusInput{checkout_id:result.codebase.clone(),files:interfaces.iter().map(|file|if file.rel_path==rel{expected.evidence.clone()}else{file.clone()}).collect(),unavailable_files:unavailable.clone()};let oracle=resolve_partition(&input,&rel).unwrap();assert_eq!(generation.resolutions(id).unwrap(),oracle.resolutions);
  if rel=="large.py"{assert_eq!(evidence.declarations.len(),6000);assert!(serde_json::to_vec(&evidence).unwrap().len()>8*1024*1024,"actual production threshold, not a forced hook");assert!(evidence.occurrences.is_empty());}
 }assert!(fixture.exact_target(&store,"target"));
}

#[test]
fn s04_upgrade_reacquires_current_source_and_honors_cancelled_run_before_publication(){
 for mode in ["same_stat_bytes","cancel"]{
  let fixture=ReuseFixture::new(129);let store=fixture.store();let _limit=S04OutputLimit::force();
  let caller=fixture.binding.root.join("caller.py");let modified=std::fs::metadata(&caller).unwrap().modified().unwrap();let fm=fixture.root.join("fm.sqlite");
  S04_UPGRADE_TEST_HOOK.with(|slot|*slot.borrow_mut()=Some(Box::new(move||{
   if mode=="same_stat_bytes"{let mut bytes=std::fs::read(&caller).unwrap();let position=bytes.iter().position(|b|*b==b't').unwrap();bytes[position]=b'x';std::fs::write(&caller,bytes).unwrap();std::fs::File::open(&caller).unwrap().set_times(std::fs::FileTimes::new().set_modified(modified)).unwrap();}
   else{let conn=Connection::open(fm).unwrap();conn.execute("UPDATE code_index_runs SET status='cancelled' WHERE status='running'",[]).unwrap();}
  })));
  let error=fixture.run(&store,&fixture.binding).err().expect("must discard unsafe upgrade");assert!(error.contains(if mode=="same_stat_bytes"{"source_changed"}else{"superseded_or_cancelled"}),"{mode}: {error}");let conn=store.conn.lock().unwrap();let heads:i64=conn.query_row("SELECT count(*) FROM code_index_heads",[],|r|r.get(0)).unwrap();assert_eq!(heads,0);
 }
}

#[test]
fn successful_partition_extraction_reuse_matches_clean_semantics_across_changes(){
 let fixture=ReuseFixture::new(129);let store=fixture.store();let(cold,m)=fixture.index(&store);
 assert_eq!(cold.files_indexed,132);assert_eq!(cold.files_unchanged,0);assert_eq!(cold.errors.len(),2);assert_eq!(calls(&m,Stage::Extract),134);fixture.clean_parity(&store,&cold);assert!(fixture.exact_target(&store,"target"));assert!(!fixture.exact_target(&store,"future_name"));
 let(warm,m)=fixture.index(&store);assert_eq!(warm.files_indexed,0);assert_eq!(warm.files_unchanged,132);assert_eq!(warm.files_removed,0);assert_eq!(warm.errors.len(),2);
 assert_eq!(calls(&m,Stage::Extract),0,"exact unchanged failed/binary bytes retain prior classification");assert_eq!(calls(&m,Stage::Hash),268,"all selected bytes acquired and hashed twice");assert_eq!(calls(&m,Stage::Resolve),0,"exact unchanged head retains prior resolutions");assert_eq!(calls(&m,Stage::Stage),0);assert_eq!(calls(&m,Stage::Seal),0);assert_eq!(calls(&m,Stage::Publish),0);fixture.clean_parity(&store,&warm);
 let path=fixture.binding.root.join("lib.py");let modified=std::fs::metadata(&path).unwrap().modified().unwrap();
 fixture.write("lib.py","def target(): return 2\n");std::fs::File::open(&path).unwrap().set_times(std::fs::FileTimes::new().set_modified(modified)).unwrap();
 let(body,_)=fixture.index(&store);assert_eq!((body.files_indexed,body.files_unchanged),(1,131));fixture.clean_parity(&store,&body);
 // A partial-edit rebuild publishes all retained data under its new sealed
 // head, even when extraction was reused. The following verification must
 // hit the exact no-change path, not miss forever on older per-file lineage.
 let published=store.code_index_head(&fixture.scope,&body.codebase).unwrap().unwrap().run_id;assert_eq!(published,body.run_id);
 for entry in body.coverage.iter().filter(|e|matches!(e.status.as_str(),"indexed"|"no_source_units"|"parse_failed"|"binary")){assert_eq!(entry.source_generation.as_deref(),Some(published.as_str()));}
 let before=main_projection(&store);let(reopened,m)=fixture.index(&store);assert_eq!(store.code_index_head(&fixture.scope,&body.codebase).unwrap().unwrap().run_id,published);assert_eq!(reopened.source_snapshot,body.source_snapshot);assert_eq!(serde_json::to_value(&reopened.coverage).unwrap(),serde_json::to_value(&body.coverage).unwrap());assert_eq!(main_projection(&store),before);
 for stage in [Stage::Extract,Stage::Interfaces,Stage::Resolve,Stage::Stage,Stage::Seal,Stage::Publish]{assert_eq!(calls(&m,stage),0,"partial-edit→no-change retains published lineage without {stage:?}");}assert_eq!(reopened.files_unchanged,132);fixture.clean_parity(&store,&reopened);
 fixture.write("brand_new.py","def brand_new(): return 8\n");let(added,_)=fixture.index(&store);assert_eq!((added.files_indexed,added.files_unchanged),(1,132));fixture.clean_parity(&store,&added);
 std::fs::remove_file(fixture.binding.root.join("brand_new.py")).unwrap();let(removed_addition,_)=fixture.index(&store);assert_eq!((removed_addition.files_indexed,removed_addition.files_unchanged,removed_addition.files_removed),(0,132,1));fixture.clean_parity(&store,&removed_addition);
 fixture.write("lib.py","def renamed(): return 2\n");let(interface,_)=fixture.index(&store);assert_eq!((interface.files_indexed,interface.files_unchanged),(1,131));fixture.clean_parity(&store,&interface);assert!(!fixture.exact_target(&store,"target"));
 fixture.write("lib.py","def target(): return 2\ndef future_name(): return 9\n");let(new_export,_)=fixture.index(&store);assert_eq!((new_export.files_indexed,new_export.files_unchanged),(1,131));fixture.clean_parity(&store,&new_export);assert!(fixture.exact_target(&store,"target"));assert!(fixture.exact_target(&store,"future_name"),"unchanged consumer gains newly introduced export only through full resolution");
 fixture.write("lib.py","def newly_added(): return 3\n");fixture.write("caller.py","from lib import newly_added\ndef caller(): return newly_added()\n");
 let(names,_)=fixture.index(&store);assert_eq!((names.files_indexed,names.files_unchanged),(2,130));fixture.clean_parity(&store,&names);
 std::fs::rename(fixture.binding.root.join("f000.py"),fixture.binding.root.join("moved.py")).unwrap();let(renamed,_)=fixture.index(&store);assert_eq!((renamed.files_indexed,renamed.files_unchanged,renamed.files_removed),(1,131,1));fixture.clean_parity(&store,&renamed);
 std::fs::remove_file(fixture.binding.root.join("moved.py")).unwrap();let(deleted,_)=fixture.index(&store);assert_eq!((deleted.files_indexed,deleted.files_unchanged,deleted.files_removed),(0,131,1));fixture.clean_parity(&store,&deleted);
 fixture.write("lib.py","def broken_again(:\n");let(failed,m)=fixture.index(&store);assert_eq!((failed.files_indexed,failed.files_unchanged,failed.files_removed),(0,130,1));assert_eq!(failed.errors.len(),3);assert_eq!(calls(&m,Stage::Extract),3);fixture.clean_parity(&store,&failed);
 fixture.write("lib.py","def newly_added(): return 4\n");let(recovered,_)=fixture.index(&store);assert_eq!((recovered.files_indexed,recovered.files_unchanged),(1,130));fixture.clean_parity(&store,&recovered);
}

#[test]
fn large_file_reuse_requires_current_hash_even_when_stat_is_unchanged(){
 let fixture=ReuseFixture::new(0);fixture.write("large.py",&format!("#{}\ndef large(): return 1\n","x".repeat(140_000)));let store=fixture.store();
 let(first,_)=fixture.index(&store);assert_eq!(store.code_index_head(&fixture.scope,&first.codebase).unwrap().unwrap().source_snapshot.evidence_store_version,1);
 let(warm,m)=fixture.index(&store);assert_eq!(warm.files_indexed,0);assert_eq!(warm.files_unchanged,4);assert_eq!(calls(&m,Stage::Extract),0);assert_eq!(calls(&m,Stage::Resolve),0);fixture.clean_parity(&store,&warm);
 let path=fixture.binding.root.join("large.py");let meta=std::fs::metadata(&path).unwrap();let text=std::fs::read_to_string(&path).unwrap().replace("return 1","return 2");fixture.write("large.py",&text);
 std::fs::File::open(&path).unwrap().set_times(std::fs::FileTimes::new().set_modified(meta.modified().unwrap())).unwrap();assert_eq!(std::fs::metadata(&path).unwrap().len(),meta.len());
 let(changed,_)=fixture.index(&store);assert_eq!((changed.files_indexed,changed.files_unchanged),(1,3));fixture.clean_parity(&store,&changed);
}

#[test]
fn reuse_missing_incompatible_policy_producer_and_other_scope_are_conservative(){
 let fixture=ReuseFixture::new(129);let store=fixture.store();let(first,_)=fixture.index(&store);
 fixture.write(".gitignore","# changed policy, same selection\n");let(policy,_)=fixture.index(&store);assert_eq!((policy.files_indexed,policy.files_unchanged),(132,0));fixture.clean_parity(&store,&policy);
 {let conn=store.conn.lock().unwrap();conn.execute("UPDATE code_index_runs SET config_fingerprint='incompatible-producer' WHERE run_id=?1",params![policy.run_id]).unwrap();}
 let(producer,_)=fixture.index(&store);assert_eq!((producer.files_indexed,producer.files_unchanged),(132,0));fixture.clean_parity(&store,&producer);
 let cache=fixture.cache_path(&store);std::fs::remove_file(cache).unwrap();let(missing,_)=fixture.index(&store);assert_eq!((missing.files_indexed,missing.files_unchanged),(132,0));fixture.clean_parity(&store,&missing);
 let mut other=fixture.binding.clone();other.workspace_id="other-workspace".into();
 Connection::open(fixture.root.join("authority.sqlite")).unwrap().execute("INSERT INTO file_policy_workspaces VALUES('other-workspace','subject-alice','location','',0,1)",[]).unwrap();
 let(other_result,_)=fixture.run(&store,&other).unwrap();assert_eq!((other_result.files_indexed,other_result.files_unchanged),(132,0));
 let(main_again,_)=fixture.index(&store);assert_eq!((main_again.files_indexed,main_again.files_unchanged),(0,132));assert_eq!(main_again.codebase,first.codebase);
}

#[test]
fn reuse_corrupt_payload_graph_and_changed_stamp_never_publish_cached_data(){
 for mode in ["payload","graph","resolution","header","stamp","unsafe"] {
  let fixture=ReuseFixture::new(129);let store=fixture.store();let(first,_)=fixture.index(&store);let path=fixture.cache_path(&store);
  drop(prior_audit_open(&fixture,&store,Some(&fixture.binding)).unwrap().unwrap());
  let(_,metrics)=capture_code_metrics(||drop(prior_audit_open(&fixture,&store,Some(&fixture.binding)).unwrap().unwrap()));assert_eq!(calls(&metrics,Stage::GenerationAuditHit),1);
  match mode {
   "unsafe"=>{use std::os::unix::fs::PermissionsExt;std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o644)).unwrap();},
   "stamp"=>{use std::io::Write;std::fs::OpenOptions::new().append(true).open(&path).unwrap().write_all(&[0]).unwrap();},
   _=>{
    let conn=Connection::open(&path).unwrap();
    if mode=="header"{conn.execute("UPDATE files SET header=?1 WHERE path='lib.py'",params!["x".repeat(65537)]).unwrap();}
    else if mode=="payload" {conn.execute("UPDATE files SET payload_hash=?1 WHERE path='lib.py'",params!["0".repeat(64)]).unwrap();}
    else if mode=="resolution"{conn.execute("UPDATE files SET resolution_hash=?1 WHERE path='lib.py'",params!["0".repeat(64)]).unwrap();}
    else {let graph:Vec<u8>=conn.query_row("SELECT graph FROM files WHERE path='lib.py'",[],|r|r.get(0)).unwrap();let mut graph:crate::graph::GraphUpsertInput=storage::decode(&graph).unwrap();graph.nodes[0].name="foreign-checkout::secret.py".into();conn.execute("UPDATE files SET graph=?1 WHERE path='lib.py'",params![storage::encode(&graph).unwrap()]).unwrap();}
    drop(conn);
    // Isolated hostile-metadata fixture: even a freshly supplied identity
    // cannot waive payload hash/graph provenance validation on selected data.
    use std::os::unix::fs::MetadataExt;let m=std::fs::metadata(&path).unwrap();let stamp=blake3::hash(format!("{}:{}:{}:{}:{}:{}:{}",m.dev(),m.ino(),m.len(),m.mtime(),m.mtime_nsec(),m.ctime(),m.ctime_nsec()).as_bytes()).to_hex().to_string();
    store.conn.lock().unwrap().execute("UPDATE code_index_runs SET source_snapshot=json_set(source_snapshot,'$.evidence_cache_identity',?1) WHERE run_id=?2",params![stamp,first.run_id]).unwrap();
    if mode=="header"{
     for _ in 0..2{let(result,metrics)=capture_code_metrics(||prior_audit_open(&fixture,&store,Some(&fixture.binding)));assert!(result.is_err());assert_eq!(calls(&metrics,Stage::GenerationAuditHit),0);assert_eq!(calls(&metrics,Stage::GenerationAuditMiss),1);}
    }else{
     // Metadata may legitimately audit successfully although selected rich
     // payload/graph/resolutions are hostile. Warm the NEW stamp so the next
     // index actually hits the memo and must still reject rich corruption.
     drop(prior_audit_open(&fixture,&store,Some(&fixture.binding)).unwrap().unwrap());
     let(_,metrics)=capture_code_metrics(||drop(prior_audit_open(&fixture,&store,Some(&fixture.binding)).unwrap().unwrap()));assert_eq!(calls(&metrics,Stage::GenerationAuditHit),1);
    }
   }
  }
  assert!(fixture.run(&store,&fixture.binding).is_err(),"{mode}");assert_eq!(store.code_index_head(&fixture.scope,&first.codebase).unwrap().unwrap().run_id,first.run_id);
 }
}

#[test]
fn reuse_final_stamp_and_current_cancel_revoke_erase_fences_remain_effective(){
 for mode in ["during_load","cancel","revoke","policy","erase"] {
  let fixture=ReuseFixture::new(129);let store=fixture.store();let(first,_)=fixture.index(&store);let old=first.run_id.clone();
  if mode=="during_load" {let path=fixture.cache_path(&store);REUSE_LOAD_TEST_HOOK.with(|slot|*slot.borrow_mut()=Some(Box::new(move||{use std::io::Write;std::fs::OpenOptions::new().append(true).open(path).unwrap().write_all(&[0]).unwrap();})));}
  else {
   // This regression still targets the changed-input staging path; exact
   // no-change completion now has its own final verification fence below.
   fixture.write("lib.py","def target(): return 2\n");
   let main=fixture.root.join("fm.sqlite");let authority=fixture.root.join("authority.sqlite");let source=fixture.binding.root.clone();let scope=fixture.scope.clone();let checkout=first.codebase.clone();
   REUSE_STAGE_TEST_HOOK.with(|slot|*slot.borrow_mut()=Some(Box::new(move||{
    if mode=="policy" {std::fs::write(source.join(".gitignore"),"lib.py\n").unwrap();}
    else if mode=="revoke" {Connection::open(authority).unwrap().execute("UPDATE file_policy_bindings SET status='revoked' WHERE binding_class='agent'",[]).unwrap();}
    else {let other=SqliteStore::new(main.to_str().unwrap(),4).unwrap();if mode=="cancel" {other.code_workspace_cancel(&scope,&checkout).unwrap();}else{let result=other.code_remove(&scope,&checkout);assert!(result.is_ok()||result.unwrap_err().to_string().contains("cache_cleanup_pending"));}}
   })));
  }
  assert!(fixture.run(&store,&fixture.binding).is_err(),"{mode}");
  let head=store.code_index_head(&fixture.scope,&first.codebase).unwrap();if mode=="erase"{assert!(head.is_none());}else{assert_eq!(head.unwrap().run_id,old);}
 }
}

fn main_projection(store:&SqliteStore)->Vec<Vec<Vec<rusqlite::types::Value>>>{
 let conn=store.conn.lock().unwrap();["graph_nodes","graph_edges","graph_cues","graph_cues_fts","code_files","code_file_evidence","code_resolutions","code_index_heads"].iter().map(|table|{
  let mut stmt=conn.prepare(&format!("SELECT * FROM {table} ORDER BY 1,2")).unwrap();let columns=stmt.column_count();let rows=stmt.query_map([],|r|(0..columns).map(|i|r.get(i)).collect::<rusqlite::Result<Vec<rusqlite::types::Value>>>()).unwrap().collect::<rusqlite::Result<Vec<_>>>().unwrap();rows
 }).collect()
}

#[test]
fn exact_partition_no_change_retains_head_cache_graph_and_original_lineage(){
 let fixture=ReuseFixture::new(129);let store=fixture.store();let(cold,_)=fixture.index(&store);let before=main_projection(&store);let path=fixture.cache_path(&store);let stamp=fixture.generation(&store).stamp().unwrap();
 {let conn=store.conn.lock().unwrap();conn.execute_batch("CREATE TEMP TABLE no_change_writes(n INTEGER); CREATE TEMP TRIGGER no_change_node_update AFTER UPDATE ON graph_nodes BEGIN INSERT INTO no_change_writes VALUES(1); END; CREATE TEMP TRIGGER no_change_cue_update AFTER UPDATE ON graph_cues BEGIN INSERT INTO no_change_writes VALUES(1); END; CREATE TEMP TRIGGER no_change_file_insert AFTER INSERT ON code_files BEGIN INSERT INTO no_change_writes VALUES(1); END;").unwrap();}
 for _ in 0..2{
  let(warm,m)=fixture.index(&store);assert_ne!(warm.run_id,cold.run_id);assert_eq!(warm.files_unchanged,132);assert_eq!(warm.errors,cold.errors);assert_eq!(serde_json::to_value(&warm.coverage).unwrap(),serde_json::to_value(&cold.coverage).unwrap());assert_eq!(warm.source_snapshot,cold.source_snapshot);
  assert_eq!(store.code_index_head(&fixture.scope,&cold.codebase).unwrap().unwrap().run_id,cold.run_id);assert_eq!(fixture.cache_path(&store),path);assert_eq!(fixture.generation(&store).stamp().unwrap(),stamp);assert_eq!(main_projection(&store),before,"no graph/FTS projection, file-row or head rewrite");
  let status:String=store.conn.lock().unwrap().query_row("SELECT status FROM code_index_runs WHERE run_id=?1",params![warm.run_id],|r|r.get(0)).unwrap();assert_eq!(status,"verified");
  let writes:i64=store.conn.lock().unwrap().query_row("SELECT count(*) FROM no_change_writes",[],|r|r.get(0)).unwrap();assert_eq!(writes,0,"no redundant graph/cue/file mutation");
  for stage in [Stage::Extract,Stage::Interfaces,Stage::Resolve,Stage::Stage,Stage::Seal,Stage::Publish]{assert_eq!(calls(&m,stage),0,"no {stage:?} for unchanged generation");}assert_eq!(calls(&m,Stage::Hash),268);fixture.clean_parity(&store,&warm);assert!(fixture.exact_target(&store,"target"));
 }
 // Previously unsuccessful sources are not permanently frozen: changed bytes
 // force the normal resolver/stager and regain real declarations/evidence.
 fixture.write("broken.py","def recovered_parse(): return 1\n");fixture.write("binary.py","def recovered_binary(): return 2\n");let(fixed,m)=fixture.index(&store);assert_eq!((fixed.files_indexed,fixed.files_unchanged),(2,132));assert!(fixed.errors.is_empty());assert_eq!(calls(&m,Stage::Resolve),1);assert_ne!(store.code_index_head(&fixture.scope,&cold.codebase).unwrap().unwrap().run_id,cold.run_id);fixture.clean_parity(&store,&fixed);
}

#[test]
fn exact_no_change_final_source_membership_authority_epoch_and_stamp_fences_fail_closed(){
 for mode in ["same_stat_bytes","membership","policy","cancel","revoke","erase","stamp","late_membership","late_control"]{
  let fixture=ReuseFixture::new(129);let store=fixture.store();let(cold,_)=fixture.index(&store);let source=fixture.binding.root.clone();let main=fixture.root.join("fm.sqlite");let authority=fixture.root.join("authority.sqlite");let scope=fixture.scope.clone();let checkout=cold.codebase.clone();let cache=fixture.cache_path(&store);
  let hook:Box<dyn FnOnce()>=Box::new(move||{
   match mode{
    "same_stat_bytes"=>{let path=source.join("lib.py");let old=std::fs::metadata(&path).unwrap();std::fs::write(&path,"def target(): return 2\n").unwrap();std::fs::File::open(&path).unwrap().set_times(std::fs::FileTimes::new().set_modified(old.modified().unwrap())).unwrap();assert_eq!(std::fs::metadata(path).unwrap().len(),old.len());},
    "membership"=>std::fs::write(source.join("newly_added.py"),"def appeared(): pass\n").unwrap(),
    "late_membership"=>std::fs::write(source.join("added_after_second_read.py"),"def appeared_late(): pass\n").unwrap(),
    "late_control"=>{std::fs::create_dir(source.join("new_nested")).unwrap();std::fs::write(source.join("new_nested/.gitignore"),"*.py\n").unwrap();},
    "policy"=>std::fs::write(source.join(".gitignore"),"lib.py\n").unwrap(),
    "revoke"=>{Connection::open(authority).unwrap().execute("UPDATE file_policy_bindings SET status='revoked' WHERE binding_class='agent'",[]).unwrap();},
    "stamp"=>{use std::io::Write;std::fs::OpenOptions::new().append(true).open(cache).unwrap().write_all(&[0]).unwrap();},
    _=>{let other=SqliteStore::new(main.to_str().unwrap(),4).unwrap();if mode=="cancel"{other.code_workspace_cancel(&scope,&checkout).unwrap();}else{let result=other.code_remove(&scope,&checkout);assert!(result.is_ok()||result.unwrap_err().to_string().contains("cache_cleanup_pending"));}}
   }
  });
  if mode.starts_with("late_"){NO_CHANGE_FINAL_TEST_HOOK.with(|slot|*slot.borrow_mut()=Some(hook));}else{NO_CHANGE_TEST_HOOK.with(|slot|*slot.borrow_mut()=Some(hook));}
  let state=store.code_workspace_prepare(&fixture.scope,&fixture.binding,false).unwrap();let ticket=CodeWorkTicket{checkout_id:state["checkout_id"].as_str().unwrap().into(),admission_epoch:state["admission_epoch"].as_i64().unwrap(),config_fingerprint:producer_fingerprint(),policy_generation:1,workspace_revision:1};
  let(result,metrics)=capture_code_metrics(||store.code_index_bound(&fixture.scope,&fixture.binding,&ticket));assert!(result.is_err(),"{mode}");if mode=="cancel"{assert_eq!(calls(&metrics,Stage::Hash),134,"cancelled epoch must stop before the first second-pass acquisition");}let head=store.code_index_head(&fixture.scope,&cold.codebase).unwrap();if mode=="erase"{assert!(head.is_none());}else{assert_eq!(head.unwrap().run_id,cold.run_id);}
  let verified:i64=store.conn.lock().unwrap().query_row("SELECT count(*) FROM code_index_runs WHERE status='verified'",[],|r|r.get(0)).unwrap();assert_eq!(verified,0,"{mode} must not finish a no-change verification");
 }
}

#[test]
fn optional_no_change_metadata_bounds_fall_back_without_waiving_malformed_metadata(){
 for mode in ["large_snapshot","large_errors","large_coverage","malformed_snapshot","malformed_errors","malformed_coverage"]{
  let fixture=ReuseFixture::new(129);let store=fixture.store();let(cold,_)=fixture.index(&store);
  {let conn=store.conn.lock().unwrap();match mode{
   "large_snapshot"=>{let mut oversized=cold.source_snapshot.clone();oversized.git_ref=Some("r".repeat(65536));conn.execute("UPDATE code_index_runs SET source_snapshot=?1 WHERE run_id=?2",params![serde_json::to_string(&oversized).unwrap(),cold.run_id]).unwrap();},
   "large_errors"=>{let oversized=serde_json::to_string(&vec!["x".repeat(1024*1024)]).unwrap();conn.execute("UPDATE code_index_runs SET errors=?1 WHERE run_id=?2",params![oversized,cold.run_id]).unwrap();},
   "large_coverage"=>{let oversized=format!("{}{}",serde_json::to_string(&cold.coverage).unwrap()," ".repeat(8*1024*1024));conn.execute("UPDATE code_index_runs SET coverage=?1 WHERE run_id=?2",params![oversized,cold.run_id]).unwrap();},
   "malformed_errors"=>{conn.execute("UPDATE code_index_runs SET errors='{}' WHERE run_id=?1",params![cold.run_id]).unwrap();},
   "malformed_snapshot"=>{conn.execute("UPDATE code_index_runs SET source_snapshot='{}' WHERE run_id=?1",params![cold.run_id]).unwrap();},
   _=>{conn.execute("UPDATE code_index_runs SET coverage='{}' WHERE run_id=?1",params![cold.run_id]).unwrap();}
  }}
  let result=fixture.run(&store,&fixture.binding);
  if mode.starts_with("large_"){let(rebuilt,metrics)=result.unwrap();assert_eq!(calls(&metrics,Stage::Resolve),1,"optional metadata admission miss must use the normal full resolver");assert_eq!((rebuilt.files_indexed,rebuilt.files_unchanged),if mode=="large_snapshot"{(132,0)}else{(0,132)});assert_eq!(rebuilt.errors.len(),2);assert_ne!(store.code_index_head(&fixture.scope,&cold.codebase).unwrap().unwrap().run_id,cold.run_id);fixture.clean_parity(&store,&rebuilt);}
  else{assert!(result.is_err(),"bounded malformed metadata must not become a successful no-change match");let head:String=store.conn.lock().unwrap().query_row("SELECT run_id FROM code_index_heads WHERE owner=?1 AND workspace_id=?2 AND codebase=?3",params![fixture.scope.owner,fixture.scope.workspace_id,cold.codebase],|r|r.get(0)).unwrap();assert_eq!(head,cold.run_id,"read only the pointer, not the deliberately malformed snapshot");}
 }
}
