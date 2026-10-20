//! App-only visual projection. No admission/index/source read API lives here.
#[cfg(test)]
#[path="code_visual_tests.rs"]
mod tests;
#[cfg(test)]
thread_local!{static VISUAL_TEST_HOOK:std::cell::RefCell<Option<Box<dyn FnOnce()>>>=const{std::cell::RefCell::new(None)};}
#[cfg(test)]
fn visual_test_pause(){let hook=VISUAL_TEST_HOOK.with(|s|s.borrow_mut().take());if let Some(hook)=hook{hook();}}
#[cfg(test)]
thread_local!{static CONTEXT_DECODE_TEST_HOOK:std::cell::RefCell<Option<Box<dyn FnOnce()>>>=const{std::cell::RefCell::new(None)};}
use crate::{code::{CodeSourceSnapshot,ScanCoverage,WorkspaceBinding},code_query::{QueryError,QueryReceipt},code_visual::*,graph::GraphScope};
use super::{sqlite::SqliteStore,code_partitioned::{Generation,is_partitioned}};
use rusqlite::{params,OptionalExtension};
use std::{collections::{BTreeMap,BTreeSet,VecDeque},sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}}};

fn fail(code:&str)->QueryError{QueryError{code:code.into(),message:format!("Code map {code}")}}
fn bad(error:impl std::fmt::Display)->QueryError{let text=error.to_string();fail(if text.contains("interrupted")||text.starts_with("budget_exceeded"){"budget_exceeded"}else if text.starts_with("cancelled"){"cancelled"}else if text.starts_with("denied"){"denied"}else{"reindex_required"})}
fn check(cancel:&AtomicBool)->Result<(),QueryError>{if cancel.load(Ordering::Relaxed){Err(fail("cancelled"))}else{Ok(())}}
struct Work<'a>{remaining:usize,limit:usize,cancel:&'a AtomicBool,vm:Arc<std::sync::atomic::AtomicUsize>}
impl<'a> Work<'a>{fn new(remaining:usize,cancel:&'a AtomicBool)->Self{Self{remaining,limit:remaining,cancel,vm:Arc::new(std::sync::atomic::AtomicUsize::new(0))}}fn charge(&mut self,n:usize)->Result<(),QueryError>{check(self.cancel)?;self.remaining=self.remaining.checked_sub(n).ok_or_else(||fail("budget_exceeded"))?;Ok(())}}

#[derive(Clone)]
struct Vertex {path:String,kind:NodeKind,parent:Option<usize>,resource:Option<String>,source:u64,assets:u64,languages:BTreeMap<String,u64>}
#[derive(Clone,PartialEq,Eq,PartialOrd,Ord)]
struct Link {from:usize,to:usize,kind:u8,state:u8,count:u64}
struct Summary {nodes:Vec<Vertex>,links:Vec<Link>,coverage:MapCoverage,charged:usize}
impl Summary {
 fn charge(&mut self)->Result<(),QueryError>{
  // Exact retained vector/string capacities plus conservative BTree allocation
  // allowance. Temporary lookup maps/build reservations remain separately bound.
  let mut bytes=self.nodes.capacity()*std::mem::size_of::<Vertex>()+self.links.capacity()*std::mem::size_of::<Link>()+4096;
  for n in &self.nodes{bytes=bytes.checked_add(n.path.capacity()+n.resource.as_ref().map_or(0,String::capacity)+n.languages.len()*256+n.languages.keys().map(String::capacity).sum::<usize>()).ok_or_else(||fail("budget_exceeded"))?;}
  if self.nodes.len()>SUMMARY_NODES||self.links.len()>SUMMARY_RELATIONS||bytes>SUMMARY_ENTRY_BYTES{return Err(fail("budget_exceeded"));}self.charged=bytes;Ok(())
 }
}
struct Entry {key:String,summary:Summary}
struct Cached {entry:Arc<Entry>,valid:bool}
#[derive(Default)]
struct CacheState {entries:VecDeque<Cached>,building:bool,revision:u64}
#[derive(Default)]
pub(crate) struct SummaryCache {state:Arc<Mutex<CacheState>>}
struct CacheLease {entry:Arc<Entry>,state:Arc<Mutex<CacheState>>}
impl std::ops::Deref for CacheLease {type Target=Entry;fn deref(&self)->&Entry{&self.entry}}
impl Drop for CacheLease {fn drop(&mut self){let pointer=Arc::as_ptr(&self.entry);self.state.lock().unwrap_or_else(|p|p.into_inner()).entries.retain(|item|item.valid||Arc::as_ptr(&item.entry)!=pointer||Arc::strong_count(&item.entry)>2);}}
impl SummaryCache {
 pub(crate) fn invalidate(&self,owner:&str,workspace:Option<&str>,checkout:Option<&str>){
  let mut s=self.state.lock().unwrap_or_else(|p|p.into_inner());
  s.revision=s.revision.saturating_add(1);
  for item in &mut s.entries {
   if let Ok(key)=serde_json::from_str::<Vec<String>>(&item.entry.key){if key.first().is_some_and(|x|x==owner)&&workspace.is_none_or(|w|key.get(1).is_some_and(|x|x==w))&&checkout.is_none_or(|c|key.get(2).is_some_and(|x|x==c)){item.valid=false;}}
  }
  s.entries.retain(|item|item.valid||Arc::strong_count(&item.entry)>1);
 }
 fn get(&self,key:&str)->Option<CacheLease>{self.state.lock().unwrap_or_else(|p|p.into_inner()).entries.iter().find(|e|e.valid&&e.entry.key==key).map(|e|CacheLease{entry:e.entry.clone(),state:self.state.clone()})}
 fn reserve(&self,key:&str)->Result<Build<'_>,QueryError>{
  if key.len()>16*1024{return Err(fail("budget_exceeded"));}
  let mut s=self.state.lock().unwrap_or_else(|p|p.into_inner());
  if s.building||s.revision==u64::MAX{return Err(fail("unavailable"));}
  while s.entries.len()>=SUMMARY_ENTRIES||s.entries.iter().map(|e|e.entry.summary.charged+e.entry.key.capacity()).sum::<usize>()+SUMMARY_ENTRY_BYTES+key.len()>SUMMARY_BYTES {
   let Some(i)=s.entries.iter().position(|e|Arc::strong_count(&e.entry)==1)else{return Err(fail("unavailable"))};s.entries.remove(i);
  }
  s.building=true;Ok(Build{cache:self,committed:false,revision:s.revision})
 }
}
struct Build<'a>{cache:&'a SummaryCache,committed:bool,revision:u64}
impl Build<'_>{fn finish(mut self,key:String,summary:Summary)->Result<CacheLease,QueryError>{let mut s=self.cache.state.lock().unwrap_or_else(|p|p.into_inner());if s.revision!=self.revision{return Err(fail("stale_cursor"));}let entry=Arc::new(Entry{key,summary});s.entries.push_back(Cached{entry:entry.clone(),valid:true});s.building=false;self.committed=true;Ok(CacheLease{entry,state:self.cache.state.clone()})}}
impl Drop for Build<'_>{fn drop(&mut self){if !self.committed{self.cache.state.lock().unwrap_or_else(|p|p.into_inner()).building=false;}}}

struct Context {fence:String,identity:String,registration:String,checkout:String,admission:i64,receipt:Option<QueryReceipt>,source:Option<CodeSourceSnapshot>,coverage:Vec<ScanCoverage>,policy_bytes:std::cell::Cell<usize>,metadata_bytes:std::cell::Cell<usize>}
fn limited(value:Option<String>,max:usize)->Result<String,QueryError>{let value=value.ok_or_else(||fail("budget_exceeded"))?;if value.len()>max{return Err(fail("budget_exceeded"));}Ok(value)}
fn parse_coverage(raw:&str)->Result<Vec<ScanCoverage>,QueryError>{
 use serde::de::{SeqAccess,Visitor,Error};use serde::Deserializer;
 struct Rows;
 impl<'de> Visitor<'de> for Rows {type Value=Vec<ScanCoverage>;fn expecting(&self,f:&mut std::fmt::Formatter)->std::fmt::Result{f.write_str("bounded code coverage")}
  fn visit_seq<A:SeqAccess<'de>>(self,mut seq:A)->Result<Self::Value,A::Error>{let mut rows=Vec::new();loop{if rows.len()>=PREVIEW_ENTRIES{if seq.next_element::<serde::de::IgnoredAny>()?.is_some(){return Err(A::Error::custom("coverage row budget"));}break;}let Some(row)=seq.next_element::<ScanCoverage>()?else{break};if row.rel_path.len()>4096||row.status.len()>32||row.detail.as_ref().is_some_and(|s|s.len()>4096)||row.source_hash.as_ref().is_some_and(|s|s.len()>128)||row.source_generation.as_ref().is_some_and(|s|s.len()>256){return Err(A::Error::custom("coverage field budget"));}rows.push(row);}Ok(rows)}
 }
 let mut decoder=serde_json::Deserializer::from_str(raw);let rows=decoder.deserialize_seq(Rows).map_err(bad)?;decoder.end().map_err(bad)?;Ok(rows)
}
fn scan_metadata_bytes(scan:&crate::code::ScanPlan)->usize{scan.coverage.iter().map(|r|r.rel_path.len()+r.status.len()+r.detail.as_ref().map_or(0,String::len)).sum::<usize>()+scan.files.iter().map(|p|p.as_os_str().as_encoded_bytes().len()).sum::<usize>()+scan.policy.inputs().iter().map(|(p,h)|p.as_os_str().as_encoded_bytes().len()+h.as_ref().map_or(0,String::len)).sum::<usize>()}
impl SqliteStore {
 fn map_context(&self,scope:&GraphScope,binding:&WorkspaceBinding)->Result<Context,QueryError>{
  let _timing=crate::code_metrics::span(crate::code_metrics::Stage::MapContext);
  if binding.origin!="app"{return Err(fail("denied"));}
  let fence={let _timing=crate::code_metrics::span(crate::code_metrics::Stage::MapContextFence);self.code_read_fence(scope,binding,None)?};
  let identity=crate::code::directory_identity(&binding.root).map_err(bad)?;
  let conn={let _timing=crate::code_metrics::span(crate::code_metrics::Stage::MapContextConnectionWait);self.conn.lock().unwrap()};
  let rows:Vec<(Option<String>,Option<String>,i64,Option<String>,Option<String>,Option<String>)>={
   let mut stmt=conn.prepare("SELECT CASE WHEN length(CAST(c.registration_id AS BLOB))<=256 THEN c.registration_id END,CASE WHEN length(CAST(c.checkout_id AS BLOB))<=256 THEN c.checkout_id END,c.admission_epoch,h.run_id,CASE WHEN length(CAST(r.source_snapshot AS BLOB))<=65536 THEN r.source_snapshot END,CASE WHEN length(CAST(r.coverage AS BLOB))<=8388608 THEN r.coverage END FROM code_registrations c LEFT JOIN code_index_heads h ON h.owner=c.owner AND h.workspace_id=c.workspace_id AND h.codebase=c.checkout_id LEFT JOIN code_index_runs r ON r.owner=h.owner AND r.workspace_id=h.workspace_id AND r.codebase=h.codebase AND r.run_id=h.run_id AND r.status IN('ready','verified') WHERE c.owner=?1 AND c.workspace_id=?2 AND c.directory_identity=?3 AND c.owner_subject_id=?4 AND c.location_id=?5 AND c.lifecycle!='erased' LIMIT 2").map_err(bad)?;
   let rows=stmt.query_map(params![scope.owner,scope.workspace_id,identity,binding.owner_subject_id,binding.location_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).map_err(bad)?.collect::<Result<Vec<_>,_>>().map_err(bad)?;rows
  };
  if rows.len()!=1{return Err(fail("unavailable"));}
  let(registration,checkout,admission,head,snapshot,coverage)=rows.into_iter().next().unwrap();
  let metadata_bytes=registration.as_ref().map_or(0,String::len)+checkout.as_ref().map_or(0,String::len)+head.as_ref().map_or(0,String::len)+snapshot.as_ref().map_or(0,String::len)+coverage.as_ref().map_or(0,String::len);
  let registration=limited(registration,256)?;let checkout=limited(checkout,256)?;
  // Capture receipt and bounded owned metadata under the same store mutex.
  // Retain any receipt error until after snapshot decoding, as before.
  let evidence=head.as_ref().map(|_|super::code_evidence::current_receipt(&conn,scope,binding,None).map_err(super::code_query::store_error));
  drop(conn);
  #[cfg(test)] {let hook=CONTEXT_DECODE_TEST_HOOK.with(|s|s.borrow_mut().take());if let Some(hook)=hook{hook();}}
  let(source,receipt,coverage)=if head.is_some(){
   let source:CodeSourceSnapshot={let _timing=crate::code_metrics::span(crate::code_metrics::Stage::MapContextDecode);serde_json::from_str(&limited(snapshot,65536)?).map_err(bad)?};
   let evidence=evidence.expect("head captures receipt")?;
   let coverage={let _timing=crate::code_metrics::span(crate::code_metrics::Stage::MapContextDecode);parse_coverage(&limited(coverage,8*1024*1024)?)?};
   if coverage.len()>PREVIEW_ENTRIES{return Err(fail("budget_exceeded"));}
   if evidence.config_fingerprint!=crate::code::producer_fingerprint(){
    // A retained incompatible head is not evidence authority. Use only the
    // fresh-policy metadata preview path, keeping the original head/epoch
    // fence so publication or erasure during the scan still rejects delivery.
    (None,None,Vec::new())
   }else{
    let receipt=QueryReceipt{workspace_id:scope.workspace_id.clone(),registration_id:registration.clone(),source_snapshot_id:source.file_manifest_digest.clone(),evidence};
    (Some(source),Some(receipt),coverage)
   }
  }else{(None,None,Vec::new())};
  Ok(Context{fence,identity,registration,checkout,admission,receipt,source,coverage,policy_bytes:std::cell::Cell::new(0),metadata_bytes:std::cell::Cell::new(metadata_bytes)})
 }
 fn map_policy(&self,binding:&WorkspaceBinding,context:&Context,stage:crate::code_metrics::Stage)->Result<(),QueryError>{
  let _timing=crate::code_metrics::span(stage);
  let scan=crate::code::scan_codebase_pinned(&binding.root,&context.identity).map_err(bad)?;
  if context.source.as_ref().is_none_or(|s|s.selection_policy_digest!=scan.policy.digest()){return Err(fail("reindex_required"));}
  context.policy_bytes.set(context.policy_bytes.get().saturating_add(scan.policy_bytes_read));context.metadata_bytes.set(context.metadata_bytes.get().saturating_add(scan_metadata_bytes(&scan)));Ok(())
 }
 fn map_final(&self,scope:&GraphScope,binding:&WorkspaceBinding,context:&Context,cancel:&AtomicBool)->Result<(),QueryError>{
  check(cancel)?;
  if context.source.is_some(){self.map_policy(binding,context,crate::code_metrics::Stage::MapPolicyFinal)?;}
  if self.code_read_fence(scope,binding,context.receipt.as_ref())?!=context.fence{return Err(fail("stale_cursor"));}
  if crate::code::directory_identity(&binding.root).map_err(bad)?!=context.identity{return Err(fail("denied"));}Ok(())
 }
}

fn language(path:&str)->String{match std::path::Path::new(path).extension().and_then(|s|s.to_str()){Some("py")=>"python",Some("rs")=>"rust",Some("ts")=>"typescript",Some("tsx")=>"tsx",_=>"other"}.into()}
fn coverage(rows:&[ScanCoverage],published:bool)->MapCoverage{
 let count=|names:&[&str]|rows.iter().filter(|r|names.contains(&r.status.as_str())).count() as u64;
 let failed=count(&["parse_failed"]);let unavailable=count(&["outside_root","oversize","unavailable"]);
 MapCoverage{inventory_complete:true,observed_entries:rows.len() as u64,total_entries:Some(rows.len() as u64),indexed_files:published.then(||count(&["indexed","no_source_units","no_units"])),parse_failed_files:published.then_some(failed),ignored_entries:count(&["ignored"]),unsupported_entries:count(&["unsupported"]),unavailable_entries:unavailable,dependencies:if !published{DependencyCoverage::Indexing}else if failed+unavailable>0{DependencyCoverage::Partial}else{DependencyCoverage::Complete}}
}
fn structure(rows:&[ScanCoverage],work:&mut Work)->Result<Summary,QueryError>{
 let mut paths:BTreeMap<String,(NodeKind,bool)>=BTreeMap::new();paths.insert(String::new(),(NodeKind::Repository,false));let mut charged=0usize;
 for row in rows {
  work.charge(1)?;
  if matches!(row.status.as_str(),"ignored"|"outside_root")||matches!(row.detail.as_deref(),Some("not a regular file"|"non-UTF8 filename excluded before source identity publication")){continue;}
  let path=std::path::Path::new(&row.rel_path);
  if path.is_absolute()||path.components().any(|c|!matches!(c,std::path::Component::Normal(_)))||row.rel_path.len()>4096{return Err(fail("invalid_evidence"));}
  let mut current=std::path::PathBuf::new();let depth=path.components().count();
  for(i,part)in path.components().enumerate(){current.push(part);let p=current.to_str().ok_or_else(||fail("invalid_evidence"))?;
   if !paths.contains_key(p){charged+=p.len()+256;if paths.len()>=SUMMARY_NODES||charged>SUMMARY_ENTRY_BYTES{return Err(fail("budget_exceeded"));}paths.insert(p.into(),(if i+1==depth{NodeKind::File}else{NodeKind::Folder},i+1==depth&&language(p)!="other"));}
  }
 }
 let positions:BTreeMap<_,_>=paths.keys().enumerate().map(|(i,k)|(k.clone(),i)).collect();
 let mut nodes:Vec<Vertex>=paths.into_iter().map(|(path,(kind,source))|{let parent=if path.is_empty(){None}else{let p=std::path::Path::new(&path).parent().unwrap().to_str().unwrap();Some(positions[p])};let mut languages=BTreeMap::new();if source{languages.insert(language(&path),1);}Vertex{path,kind,parent,resource:None,source:u64::from(source),assets:u64::from(kind==NodeKind::File&&!source),languages}}).collect();
 for i in (1..nodes.len()).rev(){let n=nodes[i].clone();let parent=n.parent.unwrap();nodes[parent].source=nodes[parent].source.checked_add(n.source).ok_or_else(||fail("budget_exceeded"))?;nodes[parent].assets=nodes[parent].assets.checked_add(n.assets).ok_or_else(||fail("budget_exceeded"))?;for(lang,count)in n.languages{*nodes[parent].languages.entry(lang).or_default()+=count;}}
 let mut summary=Summary{nodes,links:Vec::new(),coverage:coverage(rows,false),charged:0};summary.charge()?;Ok(summary)
}
fn append_link(links:&mut BTreeMap<(usize,usize,u8,u8),u64>,from:usize,to:usize,kind:&str,state:&str,work:&mut Work)->Result<(),QueryError>{
 append_link_count(links,from,to,kind,state,1,work)
}
fn append_link_count(links:&mut BTreeMap<(usize,usize,u8,u8),u64>,from:usize,to:usize,kind:&str,state:&str,count:u64,work:&mut Work)->Result<(),QueryError>{
 if count==0{return Err(fail("invalid_evidence"));}work.charge(usize::try_from(count).map_err(|_|fail("budget_exceeded"))?)?;let kind=match kind{"call"=>1,"import_reference"=>2,"value_reference"|"type_reference"=>return Ok(()),_=>return Err(fail("invalid_evidence"))};
 let state=match state{"exact"=>1,"inferred"=>2,_=>return Ok(())};
 let key=(from,to,kind,state);if !links.contains_key(&key)&&links.len()>=SUMMARY_RELATIONS{return Err(fail("budget_exceeded"));}
 let value=links.entry(key).or_default();*value=value.checked_add(count).ok_or_else(||fail("budget_exceeded"))?;Ok(())
}
fn sql_guard(db:&rusqlite::Connection,work:&Work,cancel:&Arc<AtomicBool>){let cancel=cancel.clone();let count=work.vm.clone();let maximum=work.limit.saturating_mul(32);db.progress_handler(1000,Some(move||cancel.load(Ordering::Relaxed)||count.fetch_add(1000,Ordering::Relaxed).saturating_add(1000)>maximum));}

// Only existence affects coverage. Keep edges outermost and look up the unique
// occurrence identity before its typed metadata: a file-only automatic join can
// otherwise multiply work by all occurrences in the same file. These are the
// original membership/state predicates; no dependency is inferred or omitted.
const PARTITION_HAS_UNRESOLVED_SQL:&str="SELECT EXISTS(SELECT 1 FROM edges e CROSS JOIN nodes o INDEXED BY sqlite_autoindex_nodes_1 ON o.id='occ:'||e.occurrence AND o.file=e.file AND o.kind='occurrence' CROSS JOIN occurrence_kinds k ON k.occurrence=o.row WHERE k.kind IN(1,2) AND (e.state NOT IN('exact','inferred') OR e.target IS NULL))";
// Keep files outermost: scanning every occurrence node is unnecessary here.
const PARTITION_FILE_MAPPING_SQL:&str="SELECT f.id,CASE WHEN length(CAST(f.path AS BLOB))<=4096 THEN f.path END,CASE WHEN length(CAST(n.id AS BLOB))<=256 THEN n.id END FROM files f CROSS JOIN nodes n INDEXED BY nodes_file ON n.file=f.id AND n.kind='file' ORDER BY f.id LIMIT 10001";

fn project_partition_file(summary:&mut Summary,by_path:&BTreeMap<String,usize>,by_file:&mut BTreeMap<i64,usize>,id:i64,path:Option<String>,resource:Option<String>)->Result<(),QueryError>{
 // Duplicate file nodes have no canonical last-wins resource. Reject before
 // overwriting either lookup or the projected vertex, regardless of tie order.
 if by_file.contains_key(&id){return Err(fail("invalid_evidence"));}
 if by_file.len()>=10_000{return Err(fail("budget_exceeded"));}
 let path=limited(path,4096)?;let index=*by_path.get(&path).ok_or_else(||fail("invalid_evidence"))?;
 let resource=limited(resource,256)?;summary.nodes[index].resource=Some(resource);by_file.insert(id,index);Ok(())
}

impl Generation {
 pub(crate) fn map_typed_metadata(&self)->Result<(),QueryError>{
  let version:Option<String>=self.db.query_row("SELECT CASE WHEN length(CAST(value AS BLOB))<=16 THEN value END FROM meta WHERE key='visual_kind_version'",[],|r|r.get(0)).optional().map_err(bad)?.flatten();
  if version.as_deref()!=Some("1"){return Err(fail("reindex_required"));}
  let count:i64=self.db.query_row("SELECT count(*) FROM occurrence_kinds",[],|r|r.get(0)).map_err(bad)?;
  let expected:Option<String>=self.db.query_row("SELECT CASE WHEN length(CAST(value AS BLOB))<=32 THEN value END FROM meta WHERE key='visual_occurrences'",[],|r|r.get(0)).optional().map_err(bad)?.flatten();
  if count<0||count as usize>4_000_000||Some(count.to_string())!=expected{return Err(fail("reindex_required"));}Ok(())
 }
 fn map_summary(&self,rows:&[ScanCoverage],work:&mut Work)->Result<Summary,QueryError>{
  use super::code_partitioned::{VisualSummaryMetadata,VISUAL_LINKS,visual_summary_hash,visual_file_hash,visual_link_hash,visual_finish_hash};
  let raw:Option<String>=self.db.query_row("SELECT CASE WHEN octet_length(value)<=4096 THEN value END FROM meta WHERE key='visual_summary'",[],|r|r.get(0)).optional().map_err(bad)?.flatten();
  let metadata:VisualSummaryMetadata=serde_json::from_str(&raw.ok_or_else(||fail("reindex_required"))?).map_err(bad)?;
  if metadata.version!=1||metadata.files>10_000||metadata.links>VISUAL_LINKS||metadata.resolved_rows>4_000_000||metadata.digest.len()!=64||!metadata.digest.bytes().all(|b|b.is_ascii_hexdigit()){return Err(fail("reindex_required"));}
  let overflow=metadata.status=="budget_exceeded";if !overflow&&metadata.status!="complete"{return Err(fail("reindex_required"));}if overflow&&(metadata.links!=0||metadata.resolved_rows!=0){return Err(fail("reindex_required"));}
  let mut summary=structure(rows,work)?;summary.coverage=coverage(rows,true);work.charge(1)?;if metadata.partial{summary.coverage.dependencies=DependencyCoverage::Partial;}
  let mut hash=visual_summary_hash();let mut files=0usize;
  let by_path:BTreeMap<_,_>=summary.nodes.iter().enumerate().map(|(i,n)|(n.path.clone(),i)).collect();let mut by_file=BTreeMap::new();
  let mut stmt=self.db.prepare("SELECT file,CASE WHEN octet_length(path)<=4096 THEN path END,CASE WHEN octet_length(resource)<=256 THEN resource END FROM visual_files ORDER BY file LIMIT 10001").map_err(bad)?;
  for row in stmt.query_map([],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,Option<String>>(2)?))).map_err(bad)?{
   work.charge(1)?;let(id,path,resource)=row.map_err(bad)?;let path=limited(path,4096)?;let resource=limited(resource,256)?;if !canonical_resource(&resource)||!resource.starts_with("file:"){return Err(fail("invalid_evidence"));}
   visual_file_hash(&mut hash,id,&path,&resource);project_partition_file(&mut summary,&by_path,&mut by_file,id,Some(path),Some(resource))?;files+=1;
  }
  if files!=metadata.files{return Err(fail("reindex_required"));}
  let mut links=BTreeMap::new();let mut count_rows=0usize;let mut resolved_rows=0u64;
  let mut stmt=self.db.prepare("SELECT src,dst,kind,state,count FROM visual_links ORDER BY src,dst,kind,state LIMIT 65537").map_err(bad)?;
  for row in stmt.query_map([],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,i64>(1)?,r.get::<_,u8>(2)?,r.get::<_,u8>(3)?,r.get::<_,u64>(4)?))).map_err(bad)?{
   let(src,dst,kind,state,count)=row.map_err(bad)?;count_rows+=1;if count_rows>VISUAL_LINKS||count==0{return Err(fail("invalid_evidence"));}
   resolved_rows=resolved_rows.checked_add(count).ok_or_else(||fail("budget_exceeded"))?;if resolved_rows>4_000_000{return Err(fail("budget_exceeded"));}
   visual_link_hash(&mut hash,src,dst,kind,state,count);
   let from=*by_file.get(&src).ok_or_else(||fail("invalid_evidence"))?;let to=*by_file.get(&dst).ok_or_else(||fail("invalid_evidence"))?;
   let kind=match kind{1=>"call",2=>"import_reference",_=>return Err(fail("invalid_evidence"))};let state=match state{1=>"exact",2=>"inferred",_=>return Err(fail("invalid_evidence"))};append_link_count(&mut links,from,to,kind,state,count,work)?;
   if links.len().saturating_mul(256)+summary.charged>SUMMARY_ENTRY_BYTES{return Err(fail("budget_exceeded"));}
  }
  visual_finish_hash(&mut hash,&metadata.status,metadata.partial);if count_rows!=metadata.links||resolved_rows!=metadata.resolved_rows||hash.finalize().to_hex().as_str()!=metadata.digest{return Err(fail("reindex_required"));}
  if overflow{return Err(fail("budget_exceeded"));}
  summary.links=links.into_iter().map(|((from,to,kind,state),count)|Link{from,to,kind,state,count}).collect();summary.charge()?;Ok(summary)
 }
}

const SMALL_RESOLUTION_METADATA_SQL:&str="SELECT CASE WHEN length(CAST(occurrence_id AS BLOB))=64 THEN occurrence_id END,CASE WHEN length(CAST(json_extract(payload_json,'$.target_id') AS BLOB))=64 THEN json_extract(payload_json,'$.target_id') END,CASE WHEN length(CAST(json_extract(payload_json,'$.state') AS BLOB))<=32 THEN json_extract(payload_json,'$.state') END FROM code_resolutions WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3";
fn small_summary(conn:&rusqlite::Connection,scope:&GraphScope,context:&Context,work:&mut Work)->Result<Summary,QueryError>{
 let(count,bytes):(i64,i64)=conn.query_row("SELECT count(*),coalesce(sum(length(CAST(payload_json AS BLOB))),0) FROM code_file_evidence WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,context.checkout],|r|Ok((r.get(0)?,r.get(1)?))).map_err(bad)?;
 if count<0||count>10_000||bytes<0||bytes>8*1024*1024{return Err(fail("budget_exceeded"));}
 let(count,bytes):(i64,i64)=conn.query_row("SELECT count(*),coalesce(sum(length(CAST(payload_json AS BLOB))),0) FROM code_resolutions WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,context.checkout],|r|Ok((r.get(0)?,r.get(1)?))).map_err(bad)?;
 if count<0||count>4_000_000||bytes<0||bytes>8*1024*1024{return Err(fail("budget_exceeded"));}
 let mut summary=structure(&context.coverage,work)?;summary.coverage=coverage(&context.coverage,true);
 let by_path:BTreeMap<_,_>=summary.nodes.iter().enumerate().map(|(i,n)|(n.path.clone(),i)).collect();
 let mut declarations=BTreeMap::new();let mut kinds=BTreeMap::new();let mut bytes=0usize;
 // JSON1 projects scalar metadata; never transfer/decode whole rich payloads
 // into this map process. Existing small-store producer/receipt remains required.
 let mut stmt=conn.prepare("SELECT CASE WHEN length(CAST(f.rel_path AS BLOB))<=4096 THEN f.rel_path END,CASE WHEN length(CAST(json_extract(f.payload_json,'$.file_id') AS BLOB))=64 THEN json_extract(f.payload_json,'$.file_id') END FROM code_file_evidence f WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3 LIMIT 10001").map_err(bad)?;
 for row in stmt.query_map(params![scope.owner,scope.workspace_id,context.checkout],|r|Ok((r.get::<_,Option<String>>(0)?,r.get::<_,Option<String>>(1)?))).map_err(bad)? {
  work.charge(1)?;let(path,id)=row.map_err(bad)?;let index=*by_path.get(&limited(path,4096)?).ok_or_else(||fail("invalid_evidence"))?;summary.nodes[index].resource=Some(format!("file:{}",limited(id,64)?));
 }
 let mut stmt=conn.prepare("SELECT CASE WHEN length(CAST(f.rel_path AS BLOB))<=4096 THEN f.rel_path END,CASE WHEN length(CAST(json_extract(d.value,'$.id') AS BLOB))=64 THEN json_extract(d.value,'$.id') END FROM code_file_evidence f,json_each(f.payload_json,'$.declarations') d WHERE f.owner=?1 AND f.workspace_id=?2 AND f.checkout_id=?3").map_err(bad)?;
 for row in stmt.query_map(params![scope.owner,scope.workspace_id,context.checkout],|r|Ok((r.get::<_,Option<String>>(0)?,r.get::<_,Option<String>>(1)?))).map_err(bad)? {
  work.charge(1)?;let(path,id)=row.map_err(bad)?;let id=limited(id,64)?;bytes+=id.len()+128;if bytes>SUMMARY_ENTRY_BYTES{return Err(fail("budget_exceeded"));}declarations.insert(id,*by_path.get(&limited(path,4096)?).ok_or_else(||fail("invalid_evidence"))?);
 }
 let mut stmt=conn.prepare("SELECT CASE WHEN length(CAST(f.rel_path AS BLOB))<=4096 THEN f.rel_path END,CASE WHEN length(CAST(json_extract(o.value,'$.id') AS BLOB))=64 THEN json_extract(o.value,'$.id') END,CASE WHEN length(CAST(json_extract(o.value,'$.kind') AS BLOB))<=32 THEN json_extract(o.value,'$.kind') END FROM code_file_evidence f,json_each(f.payload_json,'$.occurrences') o WHERE f.owner=?1 AND f.workspace_id=?2 AND f.checkout_id=?3").map_err(bad)?;
 for row in stmt.query_map(params![scope.owner,scope.workspace_id,context.checkout],|r|Ok((r.get::<_,Option<String>>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,Option<String>>(2)?))).map_err(bad)? {
  work.charge(1)?;let(path,id,kind)=row.map_err(bad)?;let id=limited(id,64)?;let kind=limited(kind,32)?;bytes+=id.len()+kind.len()+128;if bytes>SUMMARY_ENTRY_BYTES{return Err(fail("budget_exceeded"));}kinds.insert(id,(*by_path.get(&limited(path,4096)?).ok_or_else(||fail("invalid_evidence"))?,kind));
 }
 let mut links=BTreeMap::new();let mut stmt=conn.prepare(SMALL_RESOLUTION_METADATA_SQL).map_err(bad)?;
 for row in stmt.query_map(params![scope.owner,scope.workspace_id,context.checkout],|r|Ok((r.get::<_,Option<String>>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,Option<String>>(2)?))).map_err(bad)?{
  work.charge(1)?;let(occurrence,target,state)=row.map_err(bad)?;let occurrence=limited(occurrence,64)?;let(from,kind)=kinds.get(&occurrence).ok_or_else(||fail("invalid_evidence"))?;if matches!(kind.as_str(),"value_reference"|"type_reference"){continue;}if !matches!(kind.as_str(),"call"|"import_reference"){return Err(fail("invalid_evidence"));}let state=limited(state,32)?;if !matches!(state.as_str(),"exact"|"inferred"){summary.coverage.dependencies=DependencyCoverage::Partial;continue;}let target=target.ok_or_else(||fail("invalid_evidence"))?;let to=*declarations.get(&target).ok_or_else(||fail("invalid_evidence"))?;append_link(&mut links,*from,to,kind,&state,work)?;
  if links.len().saturating_mul(256)+bytes+summary.charged>2*SUMMARY_ENTRY_BYTES{return Err(fail("budget_exceeded"));}
 }
 summary.links=links.into_iter().map(|((from,to,kind,state),count)|Link{from,to,kind,state,count}).collect();summary.charge()?;Ok(summary)
}
fn kind(value:u8)->RelationKind{match value{0=>RelationKind::Containment,1=>RelationKind::Call,_=>RelationKind::Import}}
fn state(value:u8)->RelationState{match value{0=>RelationState::Structure,1=>RelationState::Exact,_=>RelationState::Inferred}}
fn namespace(scope:&GraphScope,binding:&WorkspaceBinding,authority:&MapAuthority)->Result<String,QueryError>{
 let raw=serde_json::to_vec(&(scope,binding.owner_subject_id.as_str(),binding.location_id.as_str(),binding.workspace_revision,binding.policy_generation,authority)).map_err(bad)?;
 if raw.len()>16*1024{return Err(fail("budget_exceeded"));}Ok(blake3::hash(&raw).to_hex().to_string())
}
fn token(tag:&str,values:&[usize],ns:&str,secret:&[u8;32])->String{
 let body=values.iter().map(usize::to_string).collect::<Vec<_>>().join(".");let message=format!("map-v1:{ns}:{tag}:{body}");format!("{tag}:{body}:{}",blake3::keyed_hash(secret,message.as_bytes()).to_hex())
}
fn untoken(value:&str,tag:&str,count:usize,ns:&str,secret:&[u8;32])->Result<Vec<usize>,QueryError>{
 if value.len()>MAX_CURSOR_BYTES{return Err(fail("stale_cursor"));}
 let mut parts=value.split(':');if parts.next()!=Some(tag){return Err(fail("stale_cursor"));}let body=parts.next().ok_or_else(||fail("stale_cursor"))?;let mac=parts.next().ok_or_else(||fail("stale_cursor"))?;if parts.next().is_some(){return Err(fail("stale_cursor"));}
 let values=body.split('.').map(str::parse).collect::<Result<Vec<usize>,_>>().map_err(|_|fail("stale_cursor"))?;if values.len()!=count||values.iter().any(|v|*v>4_000_000){return Err(fail("stale_cursor"));}
 let expected=token(tag,&values,ns,secret);let expected=expected.rsplit(':').next().unwrap();
 if mac.len()!=expected.len()||mac.bytes().zip(expected.bytes()).fold(0u8,|d,(a,b)|d|(a^b))!=0{return Err(fail("stale_cursor"));}Ok(values)
}
fn node(summary:&Summary,index:usize,ns:&str,secret:&[u8;32])->MapNode{
 let n=&summary.nodes[index];let mut label=if n.path.is_empty(){"Repository".into()}else{std::path::Path::new(&n.path).file_name().unwrap().to_string_lossy().into_owned()};while label.len()>MAX_LABEL_BYTES{label.pop();}
 MapNode{id:token("vn1",&[index],ns,secret),label,kind:n.kind,parent:n.parent.map(|p|token("vn1",&[p],ns,secret)),source_files:n.source,asset_files:n.assets,languages:n.languages.iter().map(|(language,count)|LanguageCount{language:language.clone(),count:*count}).collect()}
}
fn contains(summary:&Summary,parent:usize,child:usize)->bool{
 let p=&summary.nodes[parent].path;let c=&summary.nodes[child].path;parent==child||p.is_empty()||c.strip_prefix(p).is_some_and(|rest|rest.starts_with('/'))
}
fn rendered_page(summary:&Summary,authority:&MapAuthority,request:&MapRequest,root:usize,ns:&str,secret:&[u8;32],work:&mut Work)->Result<MapResult,QueryError>{
 let _timing=crate::code_metrics::span(crate::code_metrics::Stage::MapProjection);
 if root>=summary.nodes.len(){return Err(fail("stale_cursor"));}
 let binding=blake3::hash(&serde_json::to_vec(&(request.operation,&request.root,request.direction,&request.relations,&request.limits)).map_err(bad)?).to_hex().to_string();let page_ns=format!("{ns}:{binding}");
 let offset=match &request.cursor{Some(c)=>untoken(c,"vc1",1,&page_ns,secret)?[0],None=>0};
 let mut vertices=Vec::new();let mut links:BTreeMap<(usize,usize,u8,u8),u64>=BTreeMap::new();
 if request.operation==MapOperation::Neighbors{
  if request.relations.contains(&RelationKind::Containment){if request.direction!=MapDirection::Outgoing{if let Some(parent)=summary.nodes[root].parent{links.insert((parent,root,0,0),1);}}if request.direction!=MapDirection::Incoming{for(i,n)in summary.nodes.iter().enumerate(){work.charge(1)?;if n.parent==Some(root){links.insert((root,i,0,0),1);}}}}
  for edge in &summary.links{work.charge(1)?;if !request.relations.contains(&kind(edge.kind)){continue;}
   let from=contains(summary,root,edge.from);let to=contains(summary,root,edge.to);
   let endpoints=match request.direction {MapDirection::Outgoing if from=>Some((root,edge.to)),MapDirection::Incoming if to=>Some((edge.from,root)),MapDirection::Both if from=>Some((root,edge.to)),MapDirection::Both if to=>Some((edge.from,root)),_=>None};
   if let Some((a,b))=endpoints {if a==b{continue;}let count=links.entry((a,b,edge.kind,edge.state)).or_default();*count=count.checked_add(edge.count).ok_or_else(||fail("budget_exceeded"))?;}
  }
 }else{
  vertices=summary.nodes.iter().enumerate().filter(|(_,n)|n.parent==Some(root)).map(|(i,_)|i).collect();
  let mut groups=vec![None;summary.nodes.len()];for &i in &vertices{groups[i]=Some(i);}for i in 1..summary.nodes.len(){work.charge(1)?;if groups[i].is_none(){groups[i]=summary.nodes[i].parent.and_then(|p|groups[p]);}}
  for edge in &summary.links{work.charge(1)?;if !request.relations.contains(&kind(edge.kind)){continue;}if let(Some(a),Some(b))=(groups[edge.from],groups[edge.to]){if a!=b{let count=links.entry((a,b,edge.kind,edge.state)).or_default();*count=count.checked_add(edge.count).ok_or_else(||fail("budget_exceeded"))?;}}}
 }
 // One finite deterministic unit stream. Edges always carry their endpoints;
 // repeated endpoint rendering is not an adjacency completeness claim.
 let all_links:Vec<_>=links.into_iter().collect();let total=vertices.len()+all_links.len();if offset>total{return Err(fail("stale_cursor"));}
 let mut result=MapResult{schema:SCHEMA.into(),operation:request.operation,authority:authority.clone(),root:Some(token("vn1",&[root],ns,secret)),direction:request.direction,relations:request.relations.clone(),nodes:vec![node(summary,root,ns,secret)],edges:Vec::new(),coverage:summary.coverage.clone(),page:MapPage{returned_nodes:1,returned_edges:0,remaining_nodes:Some(0),remaining_edges:Some(0),complete:false,next_cursor:None,truncated:false},selection:None,cost:MapCost::default()};
 let mut included=BTreeSet::from([root]);let mut next=offset;
 for unit in offset..total{
  work.charge(1)?;let old_nodes=result.nodes.len();let old_edges=result.edges.len();let old_included=included.clone();
  if unit<vertices.len(){let i=vertices[unit];if included.insert(i){result.nodes.push(node(summary,i,ns,secret));}if request.relations.contains(&RelationKind::Containment){result.edges.push(MapEdge{id:token("ve1",&[root,i,0,0],ns,secret),from:token("vn1",&[root],ns,secret),to:token("vn1",&[i],ns,secret),relation:RelationKind::Containment,state:RelationState::Structure,count:1,selection_id:None});}}
  else{let((a,b,k,s),count)=all_links[unit-vertices.len()];for i in [a,b]{if included.insert(i){result.nodes.push(node(summary,i,ns,secret));}}result.edges.push(MapEdge{id:token("ve1",&[a,b,k as usize,s as usize],ns,secret),from:token("vn1",&[a],ns,secret),to:token("vn1",&[b],ns,secret),relation:kind(k),state:state(s),count,selection_id:(k!=0).then(||token("vs1",&[a,b,k as usize,s as usize],ns,secret))});}
  // Reserve worst-case small cursor/cost counters before allocating final JSON.
  let fits=result.nodes.len()<=request.limits.nodes&&result.nodes.iter().filter(|n|n.kind!=NodeKind::File).count()<=request.limits.groups&&result.edges.len()<=request.limits.edges&&serde_json::to_vec(&result).map_err(bad)?.len()+1024<=request.limits.bytes;
  if !fits{result.nodes.truncate(old_nodes);result.edges.truncate(old_edges);included=old_included;if unit==offset{return Err(fail("budget_exceeded"));}break;}
  next=unit+1;
 }
 let containment_remaining=if request.relations.contains(&RelationKind::Containment){vertices.len().saturating_sub(next)}else{0};let edge_remaining=total.saturating_sub(next.max(vertices.len()))+containment_remaining;
 result.page.returned_nodes=result.nodes.len();result.page.returned_edges=result.edges.len();result.page.remaining_edges=Some(edge_remaining as u64);result.page.remaining_nodes=if edge_remaining==0{Some(vertices.len().saturating_sub(next) as u64)}else{None};result.page.complete=next==total;result.page.next_cursor=(next<total).then(||token("vc1",&[next],&page_ns,secret));
 Ok(result)
}

fn inventory_id(plan:&crate::code::ScanPlan)->String{
 let mut h=blake3::Hasher::new();h.update(b"code-map-inventory-v1\0");h.update(plan.policy.digest().as_bytes());
 for row in &plan.coverage{for part in [&row.rel_path,&row.status]{h.update(&(part.len() as u64).to_le_bytes());h.update(part.as_bytes());}}h.finalize().to_hex().to_string()
}
fn same_inventory(a:&crate::code::ScanPlan,b:&crate::code::ScanPlan)->bool{a.policy.inputs()==b.policy.inputs()&&a.files==b.files&&a.coverage.len()==b.coverage.len()&&a.coverage.iter().zip(&b.coverage).all(|(a,b)|a.rel_path==b.rel_path&&a.status==b.status)}
fn result_error(code:&str)->QueryError{fail(if code=="budget_exceeded"{"budget_exceeded"}else{"invalid_evidence"})}
fn finalize_map(result:&mut MapResult,request:&MapRequest)->Result<(),QueryError>{
 // Include its own decimal size until stable; no token budget or rich trimming.
 for _ in 0..8{let size=serde_json::to_vec(result).map_err(bad)?.len();if size==result.cost.serialized_bytes{return result.validate(request).map_err(result_error);}result.cost.serialized_bytes=size;}
 Err(fail("invalid_evidence"))
}
impl SqliteStore {
 fn published_entry(&self,scope:&GraphScope,binding:&WorkspaceBinding,context:&Context,cancel:&Arc<AtomicBool>,work:&mut Work)->Result<(CacheLease,Option<Generation>,bool),QueryError>{
  let receipt=context.receipt.as_ref().ok_or_else(||fail("published_required"))?;let source=context.source.as_ref().unwrap();
  self.map_policy(binding,context,crate::code_metrics::Stage::MapPolicyEntry)?;
  let generation={let conn=self.conn.lock().unwrap();if is_partitioned(&conn,scope,&receipt.evidence).map_err(bad)?{Some(Generation::open_memoized(&conn,scope,&receipt.evidence,&source.evidence_cache_identity,&self.code_query_audit).map_err(bad)?)}else{None}};
  if let Some(g)=&generation{g.verify_stamp(&source.evidence_cache_identity).map_err(bad)?;let version:Option<String>=g.db.query_row("SELECT CASE WHEN length(CAST(value AS BLOB))<=16 THEN value END FROM meta WHERE key='visual_kind_version'",[],|r|r.get(0)).optional().map_err(bad)?.flatten();if version.as_deref()!=Some("1"){return Err(fail("reindex_required"));}}
  let parts=vec![scope.owner.clone(),scope.workspace_id.clone(),context.checkout.clone(),binding.owner_subject_id.clone(),binding.location_id.clone(),scope.include_global.to_string(),binding.workspace_revision.to_string(),binding.policy_generation.to_string(),context.registration.clone(),receipt.evidence.head_generation.clone(),source.evidence_cache_identity.clone(),receipt.evidence.config_fingerprint.clone(),source.selection_policy_digest.clone(),SCHEMA.into()];
  if parts.iter().any(|s|s.len()>4096){return Err(fail("budget_exceeded"));}let key=serde_json::to_string(&parts).map_err(bad)?;if key.len()>16*1024{return Err(fail("budget_exceeded"));}
  if let Some(entry)=self.code_visual_cache.get(&key){return Ok((entry,generation,true));}
  let build=self.code_visual_cache.reserve(&key)?;
  let summary=if let Some(g)=&generation{sql_guard(&g.db,work,cancel);let result=g.map_summary(&context.coverage,work);g.db.progress_handler(0,None::<fn()->bool>);result?}
   else{let conn=self.conn.lock().unwrap();sql_guard(&conn,work,cancel);let result=small_summary(&conn,scope,context,work);conn.progress_handler(0,None::<fn()->bool>);result?};
  if summary.charged+key.capacity()+std::mem::size_of::<Entry>()>SUMMARY_ENTRY_BYTES{return Err(fail("budget_exceeded"));}
  check(cancel)?;if let Some(g)=&generation{g.verify_stamp(&source.evidence_cache_identity).map_err(bad)?;}
  self.map_final(scope,binding,context,cancel)?;
  if let Some(g)=&generation{g.verify_stamp(&source.evidence_cache_identity).map_err(bad)?;}
  Ok((build.finish(key,summary)?,generation,false))
 }
 fn resource_root(&self,scope:&GraphScope,context:&Context,summary:&Summary,generation:Option<&Generation>,resource:&str,cancel:&Arc<AtomicBool>,work:&mut Work)->Result<usize,QueryError>{
  if !canonical_resource(resource){return Err(fail("invalid"));}work.charge(summary.nodes.len())?;
  if let Some(i)=summary.nodes.iter().position(|n|n.resource.as_deref()==Some(resource)){return Ok(i);}
  let paths:Vec<Option<String>>=if let Some(g)=generation {
   sql_guard(&g.db,work,cancel);let rows=(||{let mut stmt=g.db.prepare("SELECT CASE WHEN length(CAST(f.path AS BLOB))<=4096 THEN f.path END FROM nodes n JOIN files f ON f.id=n.file WHERE n.id=?1 LIMIT 2").map_err(bad)?;let rows=stmt.query_map(params![resource],|r|r.get(0)).map_err(bad)?.collect::<Result<Vec<_>,_>>().map_err(bad)?;Ok::<_,QueryError>(rows)})();g.db.progress_handler(0,None::<fn()->bool>);rows?
  }else{
   let(kind,id)=resource.split_once(':').ok_or_else(||fail("invalid"))?;let field=match kind{"decl"=>"declarations","occ"=>"occurrences",_=>return Err(fail("unavailable"))};let conn=self.conn.lock().unwrap();sql_guard(&conn,work,cancel);
   let rows=(||{let sql=format!("SELECT CASE WHEN length(CAST(f.rel_path AS BLOB))<=4096 THEN f.rel_path END FROM code_file_evidence f,json_each(f.payload_json,'$.{field}') d WHERE f.owner=?1 AND f.workspace_id=?2 AND f.checkout_id=?3 AND json_extract(d.value,'$.id')=?4 LIMIT 2");let mut stmt=conn.prepare(&sql).map_err(bad)?;let rows=stmt.query_map(params![scope.owner,scope.workspace_id,context.checkout,id],|r|r.get(0)).map_err(bad)?.collect::<Result<Vec<_>,_>>().map_err(bad)?;Ok::<_,QueryError>(rows)})();conn.progress_handler(0,None::<fn()->bool>);rows?
  };
  if paths.len()!=1{return Err(fail("unavailable"));}let path=paths.into_iter().next().flatten().ok_or_else(||fail("invalid_evidence"))?;summary.nodes.iter().position(|n|n.path==path).ok_or_else(||fail("unavailable"))
 }
 pub fn code_visual(&self,scope:&GraphScope,binding:&WorkspaceBinding,request:&VisualRequest,cancel:&Arc<AtomicBool>,secret:&[u8;32])->Result<VisualResult,QueryError>{
  match request {VisualRequest::Map(q)=>self.code_map(scope,binding,q,cancel,secret).map(VisualResult::Map),VisualRequest::Selection(q)=>self.code_map_selection(scope,binding,q,cancel,secret).map(VisualResult::Selection)}
 }
 fn code_map(&self,scope:&GraphScope,binding:&WorkspaceBinding,request:&MapRequest,cancel:&Arc<AtomicBool>,secret:&[u8;32])->Result<MapResult,QueryError>{
  request.validate().map_err(|_|fail("invalid"))?;check(cancel)?;let context=self.map_context(scope,binding)?;let mut work=Work::new(request.limits.work,cancel);
  if context.receipt.is_none(){
   let first=crate::code::scan_visual_inventory(&binding.root,&context.identity,cancel,work.remaining/4).map_err(bad)?;work.charge(first.coverage.len()+first.policy.inputs().len())?;
   let authority=MapAuthority::StructurePreview{workspace_id:scope.workspace_id.clone(),registration_id:context.registration.clone(),checkout_id:context.checkout.clone(),admission_epoch:context.admission,policy_generation:binding.policy_generation,workspace_revision:binding.workspace_revision,selection_policy_digest:first.policy.digest(),inventory_id:inventory_id(&first)};
   if request.expected_authority.as_ref().is_some_and(|a|a!=&authority){return Err(fail("stale_cursor"));}
   let ns=namespace(scope,binding,&authority)?;let summary=structure(&first.coverage,&mut work)?;
   let root=match &request.root{None=>0,Some(MapRoot::Visual{id})=>untoken(id,"vn1",1,&ns,secret)?[0],_=>return Err(fail("published_required"))};
   let mut result=rendered_page(&summary,&authority,request,root,&ns,secret,&mut work)?;
   #[cfg(test)] visual_test_pause();
   let after=crate::code::scan_visual_inventory(&binding.root,&context.identity,cancel,work.remaining/2).map_err(bad)?;work.charge(after.coverage.len()+after.policy.inputs().len())?;
   if !same_inventory(&first,&after){return Err(fail("stale_cursor"));}self.map_final(scope,binding,&context,cancel)?;
   result.cost.inspected_entries=request.limits.work-work.remaining;result.cost.metadata_bytes=context.metadata_bytes.get()+scan_metadata_bytes(&first)+scan_metadata_bytes(&after);result.cost.policy_bytes_read=first.policy_bytes_read.saturating_add(after.policy_bytes_read);finalize_map(&mut result,request)?;return Ok(result);
  }
  let(entry,generation,hit)=self.published_entry(scope,binding,&context,cancel,&mut work)?;
  let source=context.source.as_ref().unwrap();let receipt=context.receipt.as_ref().unwrap();
  let authority=MapAuthority::Published{receipt:receipt.clone(),selection_policy_digest:source.selection_policy_digest.clone(),summary_id:blake3::hash(entry.key.as_bytes()).to_hex().to_string()};
  if request.expected_authority.as_ref().is_some_and(|a|a!=&authority){return Err(fail("stale_cursor"));}
  let ns=namespace(scope,binding,&authority)?;let root=match &request.root{None=>0,Some(MapRoot::Visual{id})=>untoken(id,"vn1",1,&ns,secret)?[0],Some(MapRoot::Resource{resource_id,..})=>self.resource_root(scope,&context,&entry.summary,generation.as_ref(),resource_id,cancel,&mut work)?};
  let mut result=rendered_page(&entry.summary,&authority,request,root,&ns,secret,&mut work)?;
  #[cfg(test)] visual_test_pause();
  if request.operation!=MapOperation::Overview{if let Some(resource_id)=entry.summary.nodes.get(root).and_then(|n|n.resource.clone()){result.selection=Some(SelectionBinding{visual_id:result.root.clone().unwrap(),resource_id,receipt:receipt.clone()});}}
  if let Some(g)=&generation{g.verify_stamp(&source.evidence_cache_identity).map_err(bad)?;}self.map_final(scope,binding,&context,cancel)?;if let Some(g)=&generation{g.verify_stamp(&source.evidence_cache_identity).map_err(bad)?;}
  result.cost.inspected_entries=request.limits.work-work.remaining;result.cost.sql_vm_steps=work.vm.load(Ordering::Relaxed);result.cost.summary_cache_hit=hit;result.cost.retained_summary_bytes=entry.summary.charged;result.cost.metadata_bytes=context.metadata_bytes.get();result.cost.policy_bytes_read=context.policy_bytes.get();finalize_map(&mut result,request)?;Ok(result)
 }
}

fn occurrence_rows(db:&rusqlite::Connection,partition:bool,scope:&GraphScope,context:&Context,from:&str,to:&str,kind:u8,state:u8,offset:usize,limit:usize,work:&mut Work)->Result<Vec<MapOccurrenceRef>,QueryError>{
 let wanted_kind=if kind==1{"call"}else{"import_reference"};let wanted_state=if state==1{"exact"}else{"inferred"};
 // No GROUP BY or unbounded sort. Partition occurrence primary key provides
 // stable witness order; filters are exact bound metadata, never browser paths.
 let partition_sql="SELECT CASE WHEN length(CAST(e.occurrence AS BLOB))=64 THEN e.occurrence END,CASE WHEN length(CAST(e.caller AS BLOB))=64 THEN e.caller END,CASE WHEN length(CAST(e.target AS BLOB))=64 THEN e.target END FROM edges e JOIN nodes o ON o.id='occ:'||e.occurrence AND o.file=e.file AND o.kind='occurrence' JOIN occurrence_kinds k ON k.occurrence=o.row JOIN files f ON f.id=e.file JOIN nodes n ON n.id='decl:'||e.target JOIN files t ON t.id=n.file WHERE k.kind=?1 AND e.state=?2 AND (?3='' OR f.path=?3 OR substr(f.path,1,length(?3)+1)=?3||'/') AND (?4='' OR t.path=?4 OR substr(t.path,1,length(?4)+1)=?4||'/') ORDER BY e.occurrence LIMIT ?5 OFFSET ?6";
 let small_sql="SELECT CASE WHEN length(CAST(r.occurrence_id AS BLOB))=64 THEN r.occurrence_id END,CASE WHEN length(CAST(json_extract(r.payload_json,'$.caller_id') AS BLOB))=64 THEN json_extract(r.payload_json,'$.caller_id') END,CASE WHEN length(CAST(json_extract(r.payload_json,'$.target_id') AS BLOB))=64 THEN json_extract(r.payload_json,'$.target_id') END FROM code_resolutions r JOIN code_file_evidence f ON f.owner=r.owner AND f.workspace_id=r.workspace_id AND f.checkout_id=r.checkout_id AND f.rel_path=r.rel_path JOIN json_each(f.payload_json,'$.occurrences') o ON json_extract(o.value,'$.id')=r.occurrence_id JOIN code_file_evidence t ON t.owner=r.owner AND t.workspace_id=r.workspace_id AND t.checkout_id=r.checkout_id JOIN json_each(t.payload_json,'$.declarations') d ON json_extract(d.value,'$.id')=json_extract(r.payload_json,'$.target_id') WHERE json_extract(o.value,'$.kind')=?1 AND json_extract(r.payload_json,'$.state')=?2 AND (?3='' OR f.rel_path=?3 OR substr(f.rel_path,1,length(?3)+1)=?3||'/') AND (?4='' OR t.rel_path=?4 OR substr(t.rel_path,1,length(?4)+1)=?4||'/') AND r.owner=?7 AND r.workspace_id=?8 AND r.checkout_id=?9 ORDER BY r.occurrence_id LIMIT ?5 OFFSET ?6";
 let mut stmt=db.prepare(if partition{partition_sql}else{small_sql}).map_err(bad)?;
 let mapper=|r:&rusqlite::Row<'_>|Ok((r.get::<_,Option<String>>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,Option<String>>(2)?));
 let rows=if partition{stmt.query_map(params![kind,wanted_state,from,to,limit,offset],mapper)}else{stmt.query_map(params![wanted_kind,wanted_state,from,to,limit,offset,scope.owner,scope.workspace_id,context.checkout],mapper)}.map_err(bad)?;
 let mut references=Vec::new();for row in rows{work.charge(1)?;let(occurrence,caller,target)=row.map_err(bad)?;references.push(MapOccurrenceRef{occurrence_id:format!("occ:{}",limited(occurrence,64)?),caller_id:caller.map(|v|format!("decl:{v}")),target_id:format!("decl:{}",limited(target,64)?),relation:if kind==1{RelationKind::Call}else{RelationKind::Import},state:if state==1{RelationState::Exact}else{RelationState::Inferred}});}Ok(references)
}
impl SqliteStore {
 fn code_map_selection(&self,scope:&GraphScope,binding:&WorkspaceBinding,request:&MapSelectionRequest,cancel:&Arc<AtomicBool>,secret:&[u8;32])->Result<MapSelectionResult,QueryError>{
  request.validate().map_err(|_|fail("invalid"))?;check(cancel)?;let context=self.map_context(scope,binding)?;let receipt=context.receipt.as_ref().ok_or_else(||fail("published_required"))?;let source=context.source.as_ref().unwrap();let mut work=Work::new(MAX_WORK,cancel);
  let(entry,generation,hit)=self.published_entry(scope,binding,&context,cancel,&mut work)?;
  let authority=MapAuthority::Published{receipt:receipt.clone(),selection_policy_digest:source.selection_policy_digest.clone(),summary_id:blake3::hash(entry.key.as_bytes()).to_hex().to_string()};
  if authority!=request.authority{return Err(fail("stale_cursor"));}let ns=namespace(scope,binding,&authority)?;let selection=untoken(&request.selection_id,"vs1",4,&ns,secret)?;let(a,b,k,s)=(selection[0],selection[1],selection[2],selection[3]);
  if a>=entry.summary.nodes.len()||b>=entry.summary.nodes.len()||!matches!(k,1|2)||!matches!(s,1|2){return Err(fail("invalid"));}
  let mut total=0u64;for link in &entry.summary.links{work.charge(1)?;if link.kind==k as u8&&link.state==s as u8&&contains(&entry.summary,a,link.from)&&contains(&entry.summary,b,link.to){total=total.checked_add(link.count).ok_or_else(||fail("budget_exceeded"))?;}}
  if total==0{return Err(fail("unavailable"));}
  let selection_ns=format!("{ns}:{}:{}",request.selection_id,request.max_refs);let offset=match &request.cursor{None=>0,Some(c)=>untoken(c,"vsc1",1,&selection_ns,secret)?[0]};if offset as u64>=total{return Err(fail("stale_cursor"));}
  let from=&entry.summary.nodes[a].path;let to=&entry.summary.nodes[b].path;
  let references=if let Some(g)=&generation {g.verify_stamp(&source.evidence_cache_identity).map_err(bad)?;sql_guard(&g.db,&work,cancel);let result=occurrence_rows(&g.db,true,scope,&context,from,to,k as u8,s as u8,offset,request.max_refs,&mut work);g.db.progress_handler(0,None::<fn()->bool>);result?}
   else{let conn=self.conn.lock().unwrap();sql_guard(&conn,&work,cancel);let result=occurrence_rows(&conn,false,scope,&context,from,to,k as u8,s as u8,offset,request.max_refs,&mut work);conn.progress_handler(0,None::<fn()->bool>);result?};
  let next=offset.checked_add(references.len()).ok_or_else(||fail("budget_exceeded"))?;if references.is_empty()||next as u64>total{return Err(fail("invalid_evidence"));}
  let remaining=total-next as u64;let mut result=MapSelectionResult{schema:SCHEMA.into(),authority,selection_id:request.selection_id.clone(),receipt:receipt.clone(),references,remaining,complete:remaining==0,next_cursor:(remaining>0).then(||token("vsc1",&[next],&selection_ns,secret)),cost:MapCost{inspected_entries:MAX_WORK-work.remaining,sql_vm_steps:work.vm.load(Ordering::Relaxed),retained_summary_bytes:entry.summary.charged,summary_cache_hit:hit,..Default::default()}};
  #[cfg(test)] visual_test_pause();
  self.map_final(scope,binding,&context,cancel)?;if let Some(g)=&generation{g.verify_stamp(&source.evidence_cache_identity).map_err(bad)?;}
  result.cost.metadata_bytes=context.metadata_bytes.get();result.cost.policy_bytes_read=context.policy_bytes.get();
  for _ in 0..8{let bytes=serde_json::to_vec(&result).map_err(bad)?.len();if bytes==result.cost.serialized_bytes{result.validate(request).map_err(result_error)?;return Ok(result);}result.cost.serialized_bytes=bytes;}
  Err(fail("invalid_evidence"))
 }
 /// Fresh delivery fence for the private owned-job poll. No new job/admission.
 pub fn code_visual_check(&self,scope:&GraphScope,binding:&WorkspaceBinding,expected:&MapAuthority,cancel:&Arc<AtomicBool>)->Result<(),QueryError>{
  check(cancel)?;expected.validate().map_err(|_|fail("invalid"))?;let context=self.map_context(scope,binding)?;
  match expected {
   MapAuthority::Published{receipt,selection_policy_digest,summary_id}=>{
    if context.receipt.as_ref()!=Some(receipt)||context.source.as_ref().is_none_or(|s|&s.selection_policy_digest!=selection_policy_digest){return Err(fail("stale_cursor"));}
    let mut work=Work::new(MAX_WORK,cancel);let(entry,generation,_)=self.published_entry(scope,binding,&context,cancel,&mut work)?;if blake3::hash(entry.key.as_bytes()).to_hex().as_str()!=summary_id{return Err(fail("stale_cursor"));}self.map_final(scope,binding,&context,cancel)?;if let Some(g)=generation{g.verify_stamp(&context.source.as_ref().unwrap().evidence_cache_identity).map_err(bad)?;}
   },
   MapAuthority::StructurePreview{workspace_id,registration_id,checkout_id,admission_epoch,policy_generation,workspace_revision,selection_policy_digest,inventory_id:expected_inventory}=>{
    if context.receipt.is_some()||workspace_id!=&scope.workspace_id||registration_id!=&context.registration||checkout_id!=&context.checkout||admission_epoch!=&context.admission||policy_generation!=&binding.policy_generation||workspace_revision!=&binding.workspace_revision{return Err(fail("stale_cursor"));}
    let first=crate::code::scan_visual_inventory(&binding.root,&context.identity,cancel,PREVIEW_ENTRIES).map_err(bad)?;let after=crate::code::scan_visual_inventory(&binding.root,&context.identity,cancel,PREVIEW_ENTRIES).map_err(bad)?;
    if !same_inventory(&first,&after)||first.policy.digest()!=*selection_policy_digest||inventory_id(&first)!=*expected_inventory{return Err(fail("stale_cursor"));}self.map_final(scope,binding,&context,cancel)?;
   }
  }Ok(())
 }
}
