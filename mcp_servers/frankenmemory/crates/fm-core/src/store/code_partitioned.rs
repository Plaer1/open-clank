//! Disk-backed, immutable generation cache. These files are disposable code
//! metadata, outside durable app data/export. The FM head is the ONLY publisher.
use super::{graph::CodeIndexResult,sqlite::SqliteStore};
use crate::graph::GraphScope;
use crate::{code::*,code_evidence::{*,storage},code_query::*};
use rusqlite::{params,Connection,OptionalExtension};
use std::{collections::{BTreeMap,BTreeSet},fs::{File,OpenOptions},path::{Path,PathBuf},sync::atomic::{AtomicBool,Ordering}};

pub const STORE_VERSION:u32=1;
pub const MAX_SOURCE:usize=64*1024*1024;
pub const MAX_SOURCE_RECORDS:usize=4_000_000;
pub const MAX_RESOLUTIONS:usize=4_000_000;
pub const MAX_DISK:u64=2*1024*1024*1024;
const AUDIT_VERSION:u32=1;
const MAX_AUDIT_ENTRIES:usize=16;
const MAX_AUDIT_KEY_BYTES:usize=16*1024;

/// Positive immutable-cache audit only. Never source freshness or authority.
/// Store-owned, fixed-size metadata: no retained DB/file handles or payloads.
#[derive(Clone,PartialEq,Eq)]
struct AuditKey {
 owner:String,workspace:String,include_global:bool,checkout:String,head:String,
 producer:String,admission:i64,policy:i64,version:u32,expected:String,pinned:String,
}
impl AuditKey {
 fn new(scope:&GraphScope,receipt:&super::code_evidence::CodeEvidenceReceipt,expected:&str,pinned:&str)->Option<Self>{
  let parts=[scope.owner.as_str(),scope.workspace_id.as_str(),receipt.checkout_id.as_str(),receipt.head_generation.as_str(),receipt.config_fingerprint.as_str(),expected,pinned];
  if parts.iter().any(|p|p.len()>4096)||parts.iter().try_fold(0usize,|n,p|n.checked_add(p.len())).is_none_or(|n|n>MAX_AUDIT_KEY_BYTES){return None;}
  Some(Self{owner:scope.owner.clone(),workspace:scope.workspace_id.clone(),include_global:scope.include_global,checkout:receipt.checkout_id.clone(),head:receipt.head_generation.clone(),producer:receipt.config_fingerprint.clone(),admission:receipt.admission_epoch,policy:receipt.policy_generation,version:AUDIT_VERSION,expected:expected.into(),pinned:pinned.into()})
 }
}
#[derive(Default)]
pub(crate) struct AuditMemo { entries:std::sync::Mutex<std::collections::VecDeque<(AuditKey,Option<usize>)>> }
impl AuditMemo {
 fn contains(&self,key:&AuditKey)->bool{self.entries.lock().unwrap_or_else(|p|p.into_inner()).iter().any(|(k,_)|k==key)}
 fn remember(&self,key:AuditKey){let mut entries=self.entries.lock().unwrap_or_else(|p|p.into_inner());if entries.iter().any(|(k,_)|k==&key){return;}if entries.len()==MAX_AUDIT_ENTRIES{entries.pop_front();}entries.push_back((key,None));}
 fn count(&self,key:&AuditKey)->Option<usize>{self.entries.lock().unwrap_or_else(|p|p.into_inner()).iter().find(|(k,_)|k==key).and_then(|(_,n)|*n)}
 fn remember_count(&self,key:&AuditKey,count:usize){if let Some((_,value))=self.entries.lock().unwrap_or_else(|p|p.into_inner()).iter_mut().find(|(k,_)|k==key){*value=Some(count);}}
 pub(crate) fn invalidate(&self,owner:&str,workspace:Option<&str>,checkout:Option<&str>){self.entries.lock().unwrap_or_else(|p|p.into_inner()).retain(|(key,_)|!(key.owner==owner&&workspace.is_none_or(|w|key.workspace==w)&&checkout.is_none_or(|c|key.checkout==c)));}
}
#[cfg(test)]
thread_local! {static AUDIT_TEST_HOOK:std::cell::RefCell<Option<Box<dyn FnOnce()>>>=const{std::cell::RefCell::new(None)};}
#[cfg(test)]
fn audit_test_pause(){let hook=AUDIT_TEST_HOOK.with(|slot|slot.borrow_mut().take());if let Some(hook)=hook{hook();}}
const MAX_JOURNAL:u64=64*1024*1024;
const SCHEMA:&str=r#"
PRAGMA page_size=4096; PRAGMA max_page_count=524288;
PRAGMA journal_mode=OFF; PRAGMA synchronous=FULL;
PRAGMA temp_store=MEMORY; PRAGMA cache_size=-8192;
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT NOT NULL UNIQUE,hash TEXT NOT NULL,
 source_bytes INTEGER NOT NULL,mtime INTEGER NOT NULL,symbols INTEGER NOT NULL,
 header TEXT NOT NULL,interface BLOB NOT NULL,payload BLOB NOT NULL,payload_hash TEXT NOT NULL,graph BLOB NOT NULL,
 resolutions BLOB,resolution_hash TEXT,node_count INTEGER NOT NULL);
CREATE TABLE nodes(row INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,file INTEGER NOT NULL,
 kind TEXT NOT NULL,ordinal INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'metadata');
CREATE INDEX nodes_file ON nodes(file,kind,id);
CREATE TABLE keys(id INTEGER PRIMARY KEY,text TEXT NOT NULL UNIQUE,chars INTEGER NOT NULL);
CREATE INDEX keys_length ON keys(chars,id);
CREATE TABLE key_nodes(key INTEGER NOT NULL,node INTEGER NOT NULL,roles INTEGER NOT NULL,
 PRIMARY KEY(key,node));
CREATE TABLE terms(term TEXT NOT NULL,key INTEGER NOT NULL,PRIMARY KEY(term,key));
CREATE TABLE edges(occurrence TEXT PRIMARY KEY,caller TEXT,target TEXT,state TEXT NOT NULL,file INTEGER NOT NULL,ordinal INTEGER NOT NULL);
-- Additive derived metadata, deliberately separate from legacy evidence rows.
-- Key is the existing occurrence nodes.row, not a duplicated64hex identity.
CREATE TABLE occurrence_kinds(occurrence INTEGER PRIMARY KEY,kind INTEGER NOT NULL CHECK(kind IN(1,2,3,4)));
CREATE TABLE visual_files(file INTEGER PRIMARY KEY,path TEXT NOT NULL,resource TEXT NOT NULL UNIQUE);
CREATE TABLE visual_links(src INTEGER NOT NULL,dst INTEGER NOT NULL,kind INTEGER NOT NULL,state INTEGER NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(src,dst,kind,state));
CREATE INDEX edges_caller ON edges(caller) WHERE caller IS NOT NULL AND target IS NOT NULL AND state IN('exact','inferred');
CREATE INDEX edges_target ON edges(target) WHERE caller IS NOT NULL AND target IS NOT NULL AND state IN('exact','inferred');
CREATE TABLE graph_ids(id TEXT PRIMARY KEY);
"#;
fn err(e:impl std::fmt::Display)->String{format!("unavailable: partition cache: {e}")}
fn qerr(e:impl std::fmt::Display)->QueryError{let text=e.to_string();let code=if text.starts_with("budget_exceeded:")||text.contains("interrupted"){"budget_exceeded"}else if text.starts_with("denied:"){"denied"}else if text.starts_with("cancelled"){"cancelled"}else{"reindex_required"};QueryError{code:code.into(),message:text}}
fn evidence_error(e:impl std::fmt::Display)->super::code_evidence::CodeEvidenceError{use super::code_evidence::*;let text=e.to_string();let code=if text.starts_with("budget_exceeded"){CodeEvidenceErrorCode::BudgetExceeded}else if text.starts_with("stale_cursor"){CodeEvidenceErrorCode::StaleCursor}else if text.starts_with("denied"){CodeEvidenceErrorCode::Denied}else{CodeEvidenceErrorCode::ReindexRequired};CodeEvidenceError{code,message:text}}
pub(crate) fn policy_fence(binding:&WorkspaceBinding,source:&CodeSourceSnapshot)->Result<(SelectionPolicy,String),String>{
 let identity=crate::code::directory_identity(&binding.root)?;
 let scan=crate::code::scan_codebase_pinned(&binding.root,&identity)?;
 if scan.policy.digest()!=source.selection_policy_digest{return Err("reindex_required: repository ignore policy changed".into());}
 Ok((scan.policy,identity))
}
fn quota()->String{"budget_exceeded: partition generation quota".into()}
pub(crate) const VISUAL_LINKS:usize=65_536;
#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct VisualSummaryMetadata{
 pub version:u32,pub status:String,pub files:usize,pub links:usize,pub resolved_rows:u64,pub partial:bool,pub digest:String,
}
pub(crate) fn visual_summary_hash()->blake3::Hasher{let mut h=blake3::Hasher::new();h.update(b"openclank.visual-summary/v1\0");h}
pub(crate) fn visual_file_hash(h:&mut blake3::Hasher,id:i64,path:&str,resource:&str){h.update(b"f");h.update(&id.to_le_bytes());for s in [path,resource]{h.update(&(s.len() as u64).to_le_bytes());h.update(s.as_bytes());}}
pub(crate) fn visual_link_hash(h:&mut blake3::Hasher,src:i64,dst:i64,kind:u8,state:u8,count:u64){h.update(b"l");for n in [src,dst]{h.update(&n.to_le_bytes());}h.update(&[kind,state]);h.update(&count.to_le_bytes());}
pub(crate) fn visual_finish_hash(h:&mut blake3::Hasher,status:&str,partial:bool){h.update(b"s");h.update(status.as_bytes());h.update(&[u8::from(partial)]);}
// Bound SQLite work, not elapsed I/O/scheduler time. A policy scan or cold
// filesystem must not exhaust a caller's unchanged deterministic work budget.
fn sqlite_work_guard(maximum:usize)->impl FnMut()->bool+Send+'static{
 let counter=std::sync::atomic::AtomicUsize::new(0);
 move||counter.fetch_add(1000,Ordering::Relaxed)>maximum
}
fn install_sqlite_work_guard(db:&Connection,maximum:usize){
 db.progress_handler(1000,Some(sqlite_work_guard(maximum)));
}
fn run_name(run:&str)->Result<String,String>{
 let id=run.strip_prefix("codegen_").ok_or("invalid partition generation")?;
 if id.len()!=32 || !id.bytes().all(|b|b.is_ascii_hexdigit()){return Err("invalid partition generation".into());}
 Ok(format!("generation_{id}.sqlite"))
}
pub(crate) fn lock(file:&File,exclusive:bool)->Result<(),String>{
 #[cfg(unix)] {use std::os::fd::AsRawFd;let mode=if exclusive{libc::LOCK_EX}else{libc::LOCK_SH};
 if unsafe{libc::flock(file.as_raw_fd(),mode|libc::LOCK_NB)}!=0{return Err("unavailable: partition generation is pinned".into());}}
 Ok(())
}
#[cfg(not(unix))]
fn private_dir(path:&Path,create:bool)->Result<(),String>{
 if create {
  #[cfg(unix)]{use std::os::unix::fs::DirBuilderExt;let mut b=std::fs::DirBuilder::new();b.recursive(true).mode(0o700);b.create(path).map_err(err)?;}
  #[cfg(not(unix))]std::fs::create_dir_all(path).map_err(err)?;
 }
 let meta=std::fs::symlink_metadata(path).map_err(err)?;
 if !meta.is_dir() || meta.file_type().is_symlink(){return Err("unavailable: unsafe partition directory".into());}
 #[cfg(unix)]{use std::os::unix::fs::MetadataExt;if meta.mode()&0o077!=0{return Err("unavailable: partition directory must be private".into());}}
 Ok(())
}
pub(crate) struct CacheDirectory{pub(crate) path:PathBuf,pub(crate) pin:File,pub(crate) guards:Vec<File>}
fn optional_directory(value:Result<CacheDirectory,String>)->Result<Option<CacheDirectory>,String>{match value{Ok(v)=>Ok(Some(v)),Err(e)if e=="cache_missing"=>Ok(None),Err(e)=>Err(e)}}
pub(crate) fn directory_scope(conn:&Connection,scope:&GraphScope,checkout:&str,create:bool)->Result<Option<CacheDirectory>,String>{optional_directory(directory_parts(conn,&scope.owner,Some(&scope.workspace_id),Some(checkout),create))}
pub(crate) fn directory_workspace(conn:&Connection,scope:&GraphScope,create:bool)->Result<Option<CacheDirectory>,String>{optional_directory(directory_parts(conn,&scope.owner,Some(&scope.workspace_id),None,create))}
pub(crate) fn directory_owner(conn:&Connection,owner:&str,create:bool)->Result<Option<CacheDirectory>,String>{optional_directory(directory_parts(conn,owner,None,None,create))}
fn directory_parts(conn:&Connection,owner:&str,workspace:Option<&str>,checkout:Option<&str>,create:bool)->Result<CacheDirectory,String>{
 let database=conn.path().filter(|s|!s.is_empty()).ok_or(if create{"unavailable: partition mode requires file-backed FM store"}else{"cache_missing"})?;
 let database=Path::new(database).canonicalize().map_err(err)?;
 let home=PathBuf::from(std::env::var_os("HOME").ok_or("unavailable: HOME required for private cache")?).canonicalize().map_err(err)?;
 let digest=|value:&str|blake3::hash(value.as_bytes()).to_hex().to_string();
 let mut parts=vec!["Library".into(),"Caches".into(),"OpenClank-code-index".into(),digest(database.to_str().ok_or("invalid database path")?),digest(owner)];
 if let Some(workspace)=workspace{parts.push(digest(workspace));}if let Some(checkout)=checkout{parts.push(digest(checkout));}
 let mut path=home.clone();
 #[cfg(unix)]{
  use std::os::fd::{AsRawFd,FromRawFd};
  use std::os::unix::fs::{OpenOptionsExt,MetadataExt};
  let mut pin=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW).open(&home).map_err(err)?;let mut guards=Vec::new();
  for (i,part) in parts.iter().enumerate(){
   let name=std::ffi::CString::new(part.as_str()).map_err(err)?;
   if create {let made=unsafe{libc::mkdirat(pin.as_raw_fd(),name.as_ptr(),0o700)};
    if made!=0 && std::io::Error::last_os_error().raw_os_error()!=Some(libc::EEXIST){return Err(err(std::io::Error::last_os_error()));}}
   let fd=unsafe{libc::openat(pin.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
   if fd<0 {let e=std::io::Error::last_os_error();if e.kind()==std::io::ErrorKind::NotFound{return Err("cache_missing".into());}return Err(err(e));}
   pin=unsafe{File::from_raw_fd(fd)};path.push(part);
   if i>=2 && pin.metadata().map_err(err)?.mode()&0o077!=0{return Err("unavailable: cache directory must be private".into());}
   if i>=4 && i+1<parts.len(){lock(&pin,false)?;guards.push(pin.try_clone().map_err(err)?);}
  }
  Ok(CacheDirectory{path,pin,guards})
 }
 #[cfg(not(unix))]{for part in parts{path.push(part);}private_dir(&path,create)?;let pin=File::open(&path).map_err(err)?;Ok(CacheDirectory{path,pin,guards:vec![]})}
}
fn pinned_path(file:&File,readonly:bool)->Result<PathBuf,String>{
 #[cfg(unix)]{use std::os::fd::AsRawFd;let prefix=if readonly{"file:"}else{""};let suffix=if readonly{"?immutable=1"}else{""};Ok(PathBuf::from(format!("{prefix}/dev/fd/{}{suffix}",file.as_raw_fd())))}
 #[cfg(not(unix))]{let _=(file,readonly);Err("unavailable: descriptor-backed partition storage is not supported on this platform".into())}
}
pub(crate) fn open_at(dir:&CacheDirectory,name:&str,write:bool,create:bool)->Result<File,String>{
 #[cfg(unix)]{use std::os::fd::{AsRawFd,FromRawFd};let name=std::ffi::CString::new(name).map_err(err)?;
  let flags=(if write{libc::O_RDWR}else{libc::O_RDONLY})|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK|if create{libc::O_CREAT|libc::O_EXCL}else{0};
  let fd=unsafe{libc::openat(dir.pin.as_raw_fd(),name.as_ptr(),flags,0o600)};
  if fd<0{return Err(err(std::io::Error::last_os_error()));}let file=unsafe{File::from_raw_fd(fd)};
  let meta=file.metadata().map_err(err)?;use std::os::unix::fs::MetadataExt;
  if !meta.is_file()||meta.mode()&0o077!=0||meta.len()>MAX_DISK{return Err("unavailable: unsafe partition file".into());}Ok(file)}
 #[cfg(not(unix))]{let _=(dir,name,write,create);Err("unavailable: descriptor-backed partition storage is not supported on this platform".into())}
}
pub(crate) fn unlink_at(dir:&CacheDirectory,name:&str)->Result<(),String>{
 #[cfg(unix)]{use std::os::fd::AsRawFd;let name=std::ffi::CString::new(name).map_err(err)?;
  if unsafe{libc::unlinkat(dir.pin.as_raw_fd(),name.as_ptr(),0)}!=0{return Err(err(std::io::Error::last_os_error()));}Ok(())}
 #[cfg(not(unix))]{let _=(dir,name);Err("unavailable: descriptor-backed partition cleanup is not supported on this platform".into())}
}
pub(crate) struct OwnedConnection(Option<Connection>);
impl std::ops::Deref for OwnedConnection{type Target=Connection;fn deref(&self)->&Connection{self.0.as_ref().unwrap()}}
fn same_file(file:&File,path:&Path)->Result<(),String>{
 let actual=std::fs::symlink_metadata(path).map_err(err)?;let pinned=file.metadata().map_err(err)?;
 if !actual.is_file() || actual.file_type().is_symlink(){return Err("unavailable: unsafe partition file".into());}
 #[cfg(unix)]{use std::os::unix::fs::MetadataExt;
 if actual.dev()!=pinned.dev() || actual.ino()!=pinned.ino() || actual.mode()&0o077!=0{return Err("unavailable: partition identity changed".into());}}
 Ok(())
}
#[cfg(test)]
#[path="code_partitioned_tests.rs"]
mod tests;
#[cfg(test)]
#[path="code_partitioned_reuse_tests.rs"]
mod reuse_tests;
#[cfg(test)]
#[path="code_partitioned_capabilities_tests.rs"]
mod capabilities_tests;
#[cfg(test)]
#[path="code_partitioned_audit_length_tests.rs"]
mod audit_length_tests;
#[cfg(test)]
thread_local! {
 static REUSE_LOAD_TEST_HOOK:std::cell::RefCell<Option<Box<dyn FnOnce()>>>=const{std::cell::RefCell::new(None)};
 static REUSE_STAGE_TEST_HOOK:std::cell::RefCell<Option<Box<dyn FnOnce()>>>=const{std::cell::RefCell::new(None)};
 static NO_CHANGE_TEST_HOOK:std::cell::RefCell<Option<Box<dyn FnOnce()>>>=const{std::cell::RefCell::new(None)};
 static NO_CHANGE_FINAL_TEST_HOOK:std::cell::RefCell<Option<Box<dyn FnOnce()>>>=const{std::cell::RefCell::new(None)};
 static S04_SMALL_OUTPUT_TEST_LIMIT:std::cell::Cell<usize>=const{std::cell::Cell::new(8*1024*1024)};
 static S04_UPGRADE_TEST_HOOK:std::cell::RefCell<Option<Box<dyn FnOnce()>>>=const{std::cell::RefCell::new(None)};
 static S04_WORKER_TEST_HOOK:std::cell::RefCell<Option<std::sync::Arc<dyn Fn(&Path,bool)+Send+Sync>>>=const{std::cell::RefCell::new(None)};
}

// A pinned derived artifact, never a retained authority decision. The current
// source is acquired/hashed under current policy before this can supply data.
struct PriorExtraction { generation:Generation, stamp:String }
impl PriorExtraction {
 fn from_head(conn:&Connection,scope:&GraphScope,checkout:&str,run:&str,policy:&str,binding:Option<&WorkspaceBinding>,memo:&std::sync::Arc<AuditMemo>)->Result<Option<Self>,String>{
  let (status,producer,snapshot):(Option<String>,Option<String>,Option<String>)=conn.query_row(
   "SELECT CASE WHEN length(CAST(status AS BLOB))<=32 THEN status END,CASE WHEN length(CAST(config_fingerprint AS BLOB))<=256 THEN config_fingerprint END,CASE WHEN length(CAST(source_snapshot AS BLOB))<=65536 THEN source_snapshot END FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND run_id=?4",
   params![scope.owner,scope.workspace_id,checkout,run],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).map_err(err)?;
  let status=status.ok_or_else(quota)?;let producer=producer.ok_or_else(quota)?;
  if status!="ready"||producer!=crate::code::producer_fingerprint(){return Ok(None);}
  let Some(snapshot)=snapshot else{return Ok(None)};
  let snapshot:CodeSourceSnapshot=serde_json::from_str(&snapshot).map_err(err)?;
  if snapshot.evidence_store_version!=STORE_VERSION||snapshot.selection_policy_digest!=policy||snapshot.evidence_cache_identity.is_empty(){return Ok(None);}
  // Bound indexing shares only the existing positive immutable metadata audit
  // with readers, using their actual current registration/policy receipt. An
  // unbound legacy index has no policy authority receipt and must not seed or
  // borrow a memo entry using synthetic epochs. Rich reuse checks stay below.
  let (receipt,memo)=if let Some(binding)=binding {
   if binding.owner!=scope.owner||binding.workspace_id!=scope.workspace_id{return Err("denied: extraction binding scope differs".into());}
   let receipt=super::code_evidence::current_receipt(conn,scope,binding,Some(checkout)).map_err(err)?;
   if receipt.head_generation!=run||receipt.config_fingerprint!=producer{return Err("superseded_or_cancelled: extraction receipt differs".into());}
   (receipt,Some(memo))
  }else{(super::code_evidence::CodeEvidenceReceipt{checkout_id:checkout.into(),head_generation:run.into(),admission_epoch:0,policy_generation:0,config_fingerprint:producer},None)};
  Self::open(conn,scope,&receipt,policy,&snapshot.evidence_cache_identity,memo)
 }
 fn open(conn:&Connection,scope:&GraphScope,receipt:&super::code_evidence::CodeEvidenceReceipt,policy:&str,stamp:&str,memo:Option<&std::sync::Arc<AuditMemo>>)->Result<Option<Self>,String>{
  let Some(folder)=directory_scope(conn,scope,&receipt.checkout_id,false)? else{return Ok(None)};
  // Only genuine absence is a conservative cache miss. Unsafe paths, corrupt
  // metadata/payloads and races fail closed rather than being called misses.
  match std::fs::symlink_metadata(folder.path.join(run_name(&receipt.head_generation)?)){
   Err(e)if e.kind()==std::io::ErrorKind::NotFound=>return Ok(None),Err(e)=>return Err(err(e)),Ok(m)if !m.is_file()||m.file_type().is_symlink()=>return Err("reindex_required: unsafe extraction cache".into()),Ok(_)=>{}
  }
  let generation=if let Some(memo)=memo{Generation::open_memoized(conn,scope,receipt,stamp,memo)?}else{Generation::open(conn,scope,receipt)?};generation.verify_stamp(stamp)?;
  let actual:Option<String>=generation.db.query_row("SELECT CASE WHEN length(CAST(value AS BLOB))<=4096 THEN value END FROM meta WHERE key='policy'",[],|r|r.get(0)).map_err(err)?;
  if actual.as_deref()!=Some(policy){return Err("reindex_required: extraction cache policy differs".into());}
  generation.verify_stamp(stamp)?;Ok(Some(Self{generation,stamp:stamp.into()}))
 }
 fn reuse(&self,checkout:&str,rel:&str,path:&Path,acquired:&AcquiredSource,hash:&str)->Result<Option<IndexedFile>,String>{
  self.generation.verify_stamp(&self.stamp)?;
  let row:Option<(i64,Option<String>,i64)>=self.generation.db.query_row("SELECT id,CASE WHEN length(CAST(hash AS BLOB))=64 THEN hash END,source_bytes FROM files WHERE path=?1",params![rel],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional().map_err(err)?;
  let Some((id,old_hash,bytes))=row else{self.generation.verify_stamp(&self.stamp)?;return Ok(None)};
  if old_hash.as_deref()!=Some(hash){self.generation.verify_stamp(&self.stamp)?;return Ok(None);}
  if usize::try_from(bytes).ok()!=Some(acquired.bytes.len()){return Err("reindex_required: extraction cache source size differs".into());}
  let evidence=self.generation.load(id)?;
  if evidence.checkout_id!=checkout||evidence.rel_path!=rel||evidence.source_hash!=hash||evidence.source_len!=acquired.bytes.len() as u64||evidence.capability!=language_capability(rel){return Err("reindex_required: extraction provenance differs".into());}
  let packed:Option<Vec<u8>>=self.generation.db.query_row("SELECT CASE WHEN length(graph)<=8388608 THEN graph END FROM files WHERE id=?1",params![id],|r|r.get(0)).map_err(err)?;
  let upsert:crate::graph::GraphUpsertInput=storage::decode(&packed.ok_or_else(quota)?)?;
  validate_reused_graph(&evidence,&upsert,path)?;
  #[cfg(test)] {let hook=REUSE_LOAD_TEST_HOOK.with(|slot|slot.borrow_mut().take());if let Some(hook)=hook{hook();}}
  self.generation.verify_stamp(&self.stamp)?;
  Ok(Some(IndexedFile{symbol_count:evidence.declarations.len(),evidence,rel_path:rel.into(),blake3:hash.into(),mtime_ns:acquired.metadata.modified().ok().and_then(|t|t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d|d.as_nanos() as i64).unwrap_or(0),size:acquired.bytes.len() as i64,upsert}))
 }
}
fn validate_reused_graph(file:&FileEvidence,graph:&crate::graph::GraphUpsertInput,path:&Path)->Result<(),String>{
 let bad=||"reindex_required: extraction graph provenance differs".to_string();
 let lang=Lang::from_path(path).ok_or_else(bad)?;let fqn=format!("{}::{}",file.checkout_id,file.rel_path);
 if graph.nodes.len()!=file.declarations.len()+1{return Err(bad());}
 let root=&graph.nodes[0];if root.kind!="file"||root.name!=fqn||root.label.as_deref()!=Some(format!("{lang:?}").to_lowercase().as_str())||root.layer.as_deref()!=Some("semantic")||root.trust!=Some(3){return Err(bad());}
 let mut definitions=BTreeSet::new();let mut cues=BTreeSet::new();
 for (node,decl) in graph.nodes[1..].iter().zip(&file.declarations){
  let name=format!("{fqn}::{}",decl.id);
  if node.kind!="code_symbol"||node.name!=name||node.label.as_deref()!=Some(decl.qualified_name.as_str())||node.layer.as_deref()!=Some("semantic")||node.trust!=Some(3){return Err(bad());}
  definitions.insert(name.clone());for cue in identifier_cues(&decl.name).into_iter().chain([decl.name.to_lowercase()]){cues.insert((cue,name.clone()));}
 }
 let prefix=format!("{}::",file.checkout_id);
 for edge in &graph.edges{
  if edge.src.kind!="file"||edge.src.name!=fqn||edge.fact.is_some(){return Err(bad());}
  match edge.tag.as_str(){
   "defines" if edge.dst.kind=="code_symbol"&&edge.trust==Some(3)&&definitions.remove(&edge.dst.name)=>{},
   "imports" if edge.dst.kind=="module"&&edge.trust==Some(3)&&edge.dst.name.strip_prefix(&prefix).is_some_and(|n|!n.is_empty())=>{},
   "calls" if edge.dst.kind=="callable"&&edge.trust==Some(0)&&edge.dst.name.strip_prefix(&prefix).is_some_and(|n|!n.is_empty())=>{},
   _=>return Err(bad()),
  }
 }
 if !definitions.is_empty()||graph.cues.len()!=cues.len(){return Err(bad());}
 for cue in &graph.cues{if cue.node.kind!="code_symbol"||cue.source.as_deref()!=Some("code_index")||!cues.remove(&(cue.cue.clone(),cue.node.name.clone())){return Err(bad());}}
 if !cues.is_empty(){return Err(bad());}Ok(())
}

pub(crate) struct Generation {
 pub db:OwnedConnection,path:PathBuf,directory:CacheDirectory,pin:File,remove:bool,
 pub source_bytes:usize,pub records:usize,pub resolution_rows:usize,
 audit_memo:Option<(std::sync::Arc<AuditMemo>,AuditKey)>,
}
impl Drop for Generation {fn drop(&mut self){if let Some(db)=self.db.0.take(){let _=db.close();}if self.remove{if let Some(name)=self.path.file_name().and_then(|p|p.to_str()){let _=unlink_at(&self.directory,name);}}}}
impl Generation {
 pub fn create(conn:&Connection,scope:&GraphScope,checkout:&str,run:&str,policy:&str)->Result<Self,String>{
  let folder=directory_scope(conn,scope,checkout,true)?.ok_or("unavailable: partition cache directory missing")?;
  lock(&folder.pin,false)?;
  let active:Option<String>=conn.query_row("SELECT run_id FROM code_index_heads WHERE owner=?1 AND workspace_id=?2 AND codebase=?3",params![scope.owner,scope.workspace_id,checkout],|r|r.get(0)).optional().map_err(err)?;
  let active_name=active.as_deref().map(run_name).transpose()?;
  // At most old active + this staging generation. A pinned old generation
  // blocks a new admission instead of being evicted underneath a reader.
  for item in super::code_cache_lifecycle::directory_names(&folder.pin)? {
   let name=item.to_str().ok_or("unsafe cache filename")?;
   if Some(name)==active_name.as_deref(){continue;}
   if !name.starts_with("generation_") || !name.ends_with(".sqlite") || name.len()!=50{return Err("unavailable: unexpected partition cache entry".into());}
   let file=open_at(&folder,name,true,false)?;lock(&file,true)?;
   unlink_at(&folder,name)?;
  }
  let name=run_name(run)?;let path=folder.path.join(&name);
  let pin=open_at(&folder,&name,true,true)?;lock(&pin,true)?;same_file(&pin,&path)?;
  // SQLite's Darwin locks conflict with the separately retained EX flock.
  // This one unpublished writer is already exclusively pinned; sqlite-none
  // is never used for the durable FM database or an unpinned writable cache.
  let writer_uri=format!("{}?vfs=unix-none",pinned_path(&pin,true)?.to_string_lossy().trim_end_matches("?immutable=1"));
  let db=Connection::open_with_flags(writer_uri,rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE|rusqlite::OpenFlags::SQLITE_OPEN_URI).map_err(err)?;
  same_file(&pin,&path)?;
  let value=Self{db:OwnedConnection(Some(db)),path,directory:folder,pin,remove:true,source_bytes:0,records:0,resolution_rows:0,audit_memo:None};
  value.db.execute_batch(SCHEMA).map_err(err)?;
  for (k,v) in [("owner",scope.owner.clone()),("workspace",scope.workspace_id.clone()),("checkout",checkout.into()),("run",run.into()),("policy",policy.into()),("producer",crate::code::producer_fingerprint()),("version",STORE_VERSION.to_string()),("visual_kind_version","1".into())]{value.db.execute("INSERT INTO meta VALUES(?1,?2)",params![k,v]).map_err(err)?;}
  Ok(value)
 }
 pub fn open(conn:&Connection,scope:&GraphScope,receipt:&super::code_evidence::CodeEvidenceReceipt)->Result<Self,String>{
  Self::open_inner(conn,scope,receipt,None)
 }
 pub(crate) fn open_memoized(conn:&Connection,scope:&GraphScope,receipt:&super::code_evidence::CodeEvidenceReceipt,expected:&str,memo:&std::sync::Arc<AuditMemo>)->Result<Self,String>{
  Self::open_inner(conn,scope,receipt,Some((expected,memo)))
 }
 fn open_inner(conn:&Connection,scope:&GraphScope,receipt:&super::code_evidence::CodeEvidenceReceipt,memo:Option<(&str,&std::sync::Arc<AuditMemo>)>)->Result<Self,String>{
  let _timing=crate::code_metrics::span(crate::code_metrics::Stage::QueryOpen);
  let folder=directory_scope(conn,scope,&receipt.checkout_id,false)?.ok_or("reindex_required: partition cache directory missing")?;let name=run_name(&receipt.head_generation)?;let path=folder.path.join(run_name(&receipt.head_generation)?);
  lock(&folder.pin,false)?;
  let pin=open_at(&folder,&name,false,false).map_err(|_|"reindex_required: partition cache is missing".to_owned())?;same_file(&pin,&path)?;lock(&pin,false)?;
  let db=Connection::open_with_flags(pinned_path(&pin,true)?,rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY|rusqlite::OpenFlags::SQLITE_OPEN_URI).map_err(err)?;same_file(&pin,&path)?;
  db.execute_batch("PRAGMA query_only=ON; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-8192; BEGIN").map_err(err)?;
  install_sqlite_work_guard(&db,128_000_000);
  for (key,wanted) in [("owner",scope.owner.as_str()),("workspace",scope.workspace_id.as_str()),("checkout",receipt.checkout_id.as_str()),("run",receipt.head_generation.as_str()),("producer",receipt.config_fingerprint.as_str()),("sealed","1"),("version","1")]{let found:Option<String>=db.query_row("SELECT CASE WHEN length(CAST(value AS BLOB))<=4096 THEN value END FROM meta WHERE key=?1",params![key],|r|r.get(0)).map_err(err)?;if found.as_deref()!=Some(wanted){return Err("reindex_required: partition receipt differs".into());}}
  let mut value=Self{db:OwnedConnection(Some(db)),path,directory:folder,pin,remove:false,source_bytes:0,records:0,resolution_rows:0,audit_memo:None};
  let key=if let Some((expected,_))=memo{value.verify_stamp(expected)?;AuditKey::new(scope,receipt,expected,&value.stamp()?)}else{None};
  if let (Some(key),Some((expected,memo)))=(&key,memo){if memo.contains(key){
   let _hit=crate::code_metrics::span(crate::code_metrics::Stage::GenerationAuditHit);
   value.verify_stamp(expected)?;value.audit_memo=Some((memo.clone(),key.clone()));return Ok(value);
  }}
  let _miss=crate::code_metrics::span(crate::code_metrics::Stage::GenerationAuditMiss);
  value.audit_fields()?;
  #[cfg(test)] audit_test_pause();
  if let Some((expected,memo))=memo{value.verify_stamp(expected)?;if let Some(key)=key{memo.remember(key.clone());value.audit_memo=Some((memo.clone(),key));}}
  Ok(value)
 }
 fn audit_fields(&self)->Result<(),String>{
  let db=&self.db;
  for (table,key) in [("files","files"),("nodes","nodes"),("edges","edges")]{let actual:i64=db.query_row(&format!("SELECT count(*) FROM {table}"),[],|r|r.get(0)).map_err(err)?;let wanted:Option<String>=db.query_row("SELECT CASE WHEN length(CAST(value AS BLOB))<=64 THEN value END FROM meta WHERE key=?1",params![key],|r|r.get(0)).map_err(err)?;if Some(actual.to_string())!=wanted{return Err("reindex_required: incomplete partition index".into());}}
  let unsafe_headers:i64=db.query_row("SELECT count(*) FROM files WHERE octet_length(header)>65536 OR octet_length(path)>4096 OR octet_length(hash)!=64 OR octet_length(payload_hash)!=64 OR octet_length(resolution_hash)!=64 OR length(payload)>8388608 OR length(interface)>8388608 OR length(resolutions)>8388608",[],|r|r.get(0)).map_err(err)?;
  if unsafe_headers!=0{return Err("reindex_required: partition fields exceed bounds".into());}
  Ok(())
 }
 pub fn check_disk(&self)->Result<(),String>{
  same_file(&self.pin,&self.path)?;
  if self.pin.metadata().map_err(err)?.len()>MAX_DISK{return Err(quota());}
  let journal=PathBuf::from(format!("{}-journal",self.path.display()));
  if journal.exists() && std::fs::symlink_metadata(&journal).map_err(err)?.len()>MAX_JOURNAL{return Err(quota());}
  Ok(())
 }
 fn key(&self,text:&str,node:i64,roles:i64)->Result<(),String>{
  self.db.execute("INSERT OR IGNORE INTO keys(text,chars)VALUES(?1,?2)",params![text,text.to_lowercase().chars().count() as i64]).map_err(err)?;
  let key:i64=self.db.query_row("SELECT id FROM keys WHERE text=?1",params![text],|r|r.get(0)).map_err(err)?;
  self.db.execute("INSERT INTO key_nodes VALUES(?1,?2,?3)ON CONFLICT(key,node)DO UPDATE SET roles=roles|excluded.roles",params![key,node,roles]).map_err(err)?;
  if roles&2!=0{for term in crate::code_query::identifier_terms(text){self.db.execute("INSERT OR IGNORE INTO terms VALUES(?1,?2)",params![term,key]).map_err(err)?;}}
  Ok(())
 }
 pub fn stage(&mut self,file:&FileEvidence,mtime:i64,graph:&crate::graph::GraphUpsertInput,scope:&GraphScope)->Result<(),String>{
  let mut timing=crate::code_metrics::span(crate::code_metrics::Stage::Stage);timing.add(1,file.source_len as usize);
  self.source_bytes=self.source_bytes.checked_add(file.source_len as usize).ok_or_else(quota)?;
  self.records=self.records.checked_add(file.scopes.len()+file.bindings.len()+file.declarations.len()+file.imports.len()+file.occurrences.len()).ok_or_else(quota)?;
  if self.source_bytes>MAX_SOURCE || self.records>MAX_SOURCE_RECORDS{return Err(quota());}
  let payload=storage::encode(file)?;let interface=storage::module_interface(file);let packed_interface=storage::encode(&interface)?;
  let mut header=file.clone();header.scopes.clear();header.bindings.clear();header.declarations.clear();header.imports.clear();header.occurrences.clear();header.diagnostics.clear();
  let header=serde_json::to_string(&header).map_err(err)?;
  self.db.execute_batch("BEGIN IMMEDIATE").map_err(err)?;
  let stage=(||{
   let packed_graph=storage::encode(graph)?;
   self.db.execute("INSERT INTO files(path,hash,source_bytes,mtime,symbols,header,interface,payload,payload_hash,node_count,graph)VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",params![file.rel_path,file.source_hash,file.source_len,mtime,file.declarations.len() as i64,header,packed_interface,payload,blake3::hash(&payload).to_hex().to_string(),1+file.declarations.len()+file.occurrences.len(),packed_graph]).map_err(err)?;
   let id=self.db.last_insert_rowid();
   for (kind,name) in graph.nodes.iter().map(|n|(&n.kind,&n.name)).chain(graph.edges.iter().flat_map(|e|[(&e.src.kind,&e.src.name),(&e.dst.kind,&e.dst.name)])){
    self.db.execute("INSERT OR IGNORE INTO graph_ids VALUES(?1)",params![scope.node_id(kind,name)]).map_err(err)?;
   }
   let rows=std::iter::once((format!("file:{}",file.file_id),"file",file.rel_path.as_str(),file.rel_path.as_str(),0usize))
    .chain(file.declarations.iter().enumerate().map(|(i,d)|(format!("decl:{}",d.id),"declaration",d.name.as_str(),d.qualified_name.as_str(),i)))
    .chain(file.occurrences.iter().enumerate().map(|(i,o)|(format!("occ:{}",o.id),"occurrence",o.symbol_text.as_str(),o.symbol_text.as_str(),i)));
   for (i,(node,kind,name,label,ordinal)) in rows.enumerate(){
    let state=if kind=="file"{if graph.edges.is_empty(){"no_source_units"}else{"indexed"}}else{"metadata"};
    self.db.execute("INSERT INTO nodes(id,file,kind,ordinal,state)VALUES(?1,?2,?3,?4,?5)",params![node,id,kind,ordinal as i64,state]).map_err(err)?;let row=self.db.last_insert_rowid();
    if kind=="occurrence" {
     let occurrence=file.occurrences.get(ordinal).ok_or("invalid_evidence: occurrence kind ordinal")?;
     let typed=match &occurrence.kind{OccurrenceKind::Call=>1,OccurrenceKind::ImportReference=>2,OccurrenceKind::ValueReference=>3,OccurrenceKind::TypeReference=>4};
     self.db.execute("INSERT INTO occurrence_kinds VALUES(?1,?2)",params![row,typed]).map_err(err)?;
    }
    self.key(name,row,1|4|if kind!="declaration"{2}else{0})?;self.key(label,row,1|2)?;self.key(&file.rel_path,row,1)?;
    if i%512==0{self.check_disk()?;}
   }
   self.check_disk()
  })();
  if stage.is_err(){let _=self.db.execute_batch("ROLLBACK");return stage;}
  self.db.execute_batch("COMMIT").map_err(err)?;self.check_disk()
 }
 pub fn load(&self,id:i64)->Result<FileEvidence,String>{
  let (payload,hash):(Option<Vec<u8>>,String)=self.db.query_row("SELECT CASE WHEN length(payload)<=8388608 THEN payload END,payload_hash FROM files WHERE id=?1",params![id],|r|Ok((r.get(0)?,r.get(1)?))).map_err(err)?;
  let payload=payload.ok_or_else(quota)?;
  if payload.len()>storage::MAX_ENCODED || blake3::hash(&payload).to_hex().as_str()!=hash{return Err("reindex_required: corrupt partition payload".into());}
  let file:FileEvidence=storage::decode(&payload)?;let mut b=ExtractionBudget::default();b.max_serialized_bytes=storage::MAX_DECODED;
  validate_file_evidence(&file,&b).map_err(err)?;Ok(file)
 }
 pub fn interfaces(&self)->Result<Vec<FileEvidence>,String>{
  let _timing=crate::code_metrics::span(crate::code_metrics::Stage::Interfaces);
  let mut out=Vec::new();let mut bytes=0usize;
  let mut stmt=self.db.prepare("SELECT CASE WHEN length(interface)<=8388608 THEN interface END FROM files ORDER BY id LIMIT 10001").map_err(err)?;
  for row in stmt.query_map([],|r|r.get::<_,Option<Vec<u8>>>(0)).map_err(err)?{
   if out.len()>=10_000{return Err(quota());}
   let file:FileEvidence=storage::decode(&row.map_err(err)?.ok_or_else(quota)?)?;
   bytes=bytes.checked_add(serde_json::to_vec(&file).map_err(err)?.len()).ok_or_else(quota)?;
   if bytes>MAX_MODULE_INTERFACE_BYTES{return Err(format!("budget_exceeded: module interfaces {bytes} exceed {MAX_MODULE_INTERFACE_BYTES}"));}out.push(file);
  } Ok(out)
 }
 pub fn resolve(&mut self,checkout:&str,unavailable:&[UnavailableFile],mut check:impl FnMut()->Result<(),String>)->Result<(),String>{
  let _timing=crate::code_metrics::span(crate::code_metrics::Stage::Resolve);
  let input=CorpusInput{checkout_id:checkout.into(),files:self.interfaces()?,unavailable_files:unavailable.to_vec()};
  self.db.execute("INSERT INTO meta VALUES('unavailable',?1)",params![serde_json::to_string(unavailable).map_err(err)?]).map_err(err)?;
  let resolver={let _timing=crate::code_metrics::span(crate::code_metrics::Stage::Interfaces);crate::code_evidence::PartitionResolver::new(&input).map_err(err)?};
  let ids:Vec<i64>=self.db.prepare("SELECT id FROM files ORDER BY id").map_err(err)?.query_map([],|r|r.get(0)).map_err(err)?.collect::<rusqlite::Result<_>>().map_err(err)?;
  for id in ids{
   check()?;
   let file=self.load(id)?;
   let result=resolver.resolve(&file).map_err(err)?;
   self.resolution_rows=self.resolution_rows.checked_add(result.resolutions.len()).ok_or_else(quota)?;if self.resolution_rows>MAX_RESOLUTIONS{return Err(quota());}
   let packed=storage::encode(&result.resolutions)?;
   let mut _staging=crate::code_metrics::span(crate::code_metrics::Stage::Stage);_staging.add(result.resolutions.len(),packed.len());
   self.db.execute_batch("BEGIN IMMEDIATE").map_err(err)?;
   let result=(||{
    self.db.execute("UPDATE files SET resolutions=?1,resolution_hash=?2 WHERE id=?3",params![packed,blake3::hash(&packed).to_hex().to_string(),id]).map_err(err)?;
    for (i,r) in result.resolutions.iter().enumerate(){
     let state=serde_json::to_value(&r.state).map_err(err)?.as_str().unwrap().to_owned();
     self.db.execute("INSERT INTO edges VALUES(?1,?2,?3,?4,?5,?6)",params![r.occurrence_id,r.caller_id,r.target_id,state,id,i as i64]).map_err(err)?;
     self.db.execute("UPDATE nodes SET state=?1 WHERE id=?2",params![state,format!("occ:{}",r.occurrence_id)]).map_err(err)?;
     if i%512==0{self.check_disk()?;}
    } self.check_disk()
   })();
   if result.is_err(){let _=self.db.execute_batch("ROLLBACK");return result;}
   self.db.execute_batch("COMMIT").map_err(err)?;self.check_disk()?;
  } self.build_visual_summary(&mut check)
 }
 // Derived metadata is built once in the unpublished writer, never by a read.
 // Fixed primitive keys have a conservative 256B/key (16MiB) domain; no rich
 // payload/target strings or SQL GROUP BY temporary aggregate is retained.
 fn build_visual_summary(&mut self,check:&mut impl FnMut()->Result<(),String>)->Result<(),String>{
  self.build_visual_summary_with_budget(check,128_000_000)
 }
 fn build_visual_summary_with_budget(&mut self,check:&mut impl FnMut()->Result<(),String>,maximum:usize)->Result<(),String>{
  if !self.remove{return Err("denied: published summary is immutable".into());}check()?;
  self.db.execute_batch("BEGIN IMMEDIATE").map_err(err)?;
  let exhausted=std::sync::Arc::new(AtomicBool::new(false));let flag=exhausted.clone();let mut vm=0usize;let interval=maximum.clamp(1,1000);
  self.db.progress_handler(interval as i32,Some(move||{vm=vm.saturating_add(interval);if vm>maximum{flag.store(true,Ordering::Relaxed);true}else{false}}));
  let built=(||{
   self.db.execute_batch("DELETE FROM visual_files; DELETE FROM visual_links").map_err(err)?;
   let partial:bool=self.db.query_row("SELECT EXISTS(SELECT 1 FROM edges e CROSS JOIN nodes o INDEXED BY sqlite_autoindex_nodes_1 ON o.id='occ:'||e.occurrence AND o.file=e.file AND o.kind='occurrence' CROSS JOIN occurrence_kinds k ON k.occurrence=o.row WHERE k.kind IN(1,2) AND (e.state NOT IN('exact','inferred') OR e.target IS NULL))",[],|r|r.get(0)).map_err(err)?;
   let mut hash=visual_summary_hash();let mut ids=BTreeSet::new();
   {let mut stmt=self.db.prepare("SELECT f.id,CASE WHEN octet_length(f.path)<=4096 THEN f.path END,CASE WHEN octet_length(n.id)<=256 THEN n.id END FROM files f CROSS JOIN nodes n INDEXED BY nodes_file ON n.file=f.id AND n.kind='file' ORDER BY f.id LIMIT 10001").map_err(err)?;
    for row in stmt.query_map([],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,Option<String>>(2)?))).map_err(err)?{
     let(id,path,resource)=row.map_err(err)?;if ids.contains(&id){return Err("invalid_evidence: compact file mapping".into());}if ids.len()>=10_000{return Err("budget_exceeded: compact summary capacity".into());}ids.insert(id);
     let path=path.ok_or_else(quota)?;let resource=resource.ok_or_else(quota)?;
     self.db.execute("INSERT INTO visual_files VALUES(?1,?2,?3)",params![id,path,resource]).map_err(err)?;visual_file_hash(&mut hash,id,&path,&resource);if ids.len()%128==0{check()?;self.check_disk()?;}
    }}
   let mut links:BTreeMap<(i64,i64,u8,u8),u64>=BTreeMap::new();let mut rows=0u64;let mut overflow=false;
   {let mut stmt=self.db.prepare("SELECT e.file,t.file,k.kind,CASE e.state WHEN 'exact' THEN 1 WHEN 'inferred' THEN 2 END FROM edges e JOIN nodes o ON o.id='occ:'||e.occurrence AND o.file=e.file AND o.kind='occurrence' JOIN occurrence_kinds k ON k.occurrence=o.row JOIN nodes t ON t.id='decl:'||e.target WHERE k.kind IN(1,2) AND e.state IN('exact','inferred')").map_err(err)?;
    for row in stmt.query_map([],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,i64>(1)?,r.get::<_,u8>(2)?,r.get::<_,u8>(3)?))).map_err(err)?{
     let(src,dst,kind,state)=row.map_err(err)?;if !ids.contains(&src)||!ids.contains(&dst){return Err("invalid_evidence: compact dependency file".into());}
     rows=rows.checked_add(1).ok_or_else(quota)?;if rows>MAX_RESOLUTIONS as u64{return Err(quota());}
     let key=(src,dst,kind,state);if !links.contains_key(&key)&&links.len()==VISUAL_LINKS{overflow=true;links.clear();rows=0;break;}
     let value=links.entry(key).or_default();*value=value.checked_add(1).ok_or_else(quota)?;if rows%512==0{check()?;}
    }}
   for (i,(&(src,dst,kind,state),&count)) in links.iter().enumerate(){
    self.db.execute("INSERT INTO visual_links VALUES(?1,?2,?3,?4,?5)",params![src,dst,kind,state,count]).map_err(err)?;visual_link_hash(&mut hash,src,dst,kind,state,count);if i%128==0{check()?;self.check_disk()?;}
   }
   let status=if overflow{"budget_exceeded"}else{"complete"};visual_finish_hash(&mut hash,status,partial);
   let metadata=VisualSummaryMetadata{version:1,status:status.into(),files:ids.len(),links:links.len(),resolved_rows:rows,partial,digest:hash.finalize().to_hex().to_string()};
   self.db.execute("INSERT OR REPLACE INTO meta VALUES('visual_summary',?1)",params![serde_json::to_string(&metadata).map_err(err)?]).map_err(err)?;check()?;self.check_disk()
  })();
  self.db.progress_handler(0,None::<fn()->bool>);
  if let Err(error)=built{
   if !self.db.is_autocommit(){self.db.execute_batch("ROLLBACK").map_err(err)?;}
   if exhausted.load(Ordering::Relaxed)||error=="budget_exceeded: compact summary capacity"{return self.mark_visual_summary_budget(check);}
   return Err(error);
  }
  self.db.execute_batch("COMMIT").map_err(err)?;self.check_disk()
 }
 fn mark_visual_summary_budget(&mut self,check:&mut impl FnMut()->Result<(),String>)->Result<(),String>{
  check()?;let mut hash=visual_summary_hash();visual_finish_hash(&mut hash,"budget_exceeded",true);
  let metadata=VisualSummaryMetadata{version:1,status:"budget_exceeded".into(),files:0,links:0,resolved_rows:0,partial:true,digest:hash.finalize().to_hex().to_string()};
  self.db.execute_batch("BEGIN IMMEDIATE").map_err(err)?;
  let result=(||{self.db.execute_batch("DELETE FROM visual_files; DELETE FROM visual_links").map_err(err)?;self.db.execute("INSERT OR REPLACE INTO meta VALUES('visual_summary',?1)",params![serde_json::to_string(&metadata).map_err(err)?]).map_err(err)?;check()?;self.check_disk()})();
  if result.is_err(){let _=self.db.execute_batch("ROLLBACK");return result;}self.db.execute_batch("COMMIT").map_err(err)?;self.check_disk()
 }
 pub fn seal(&mut self)->Result<(),String>{
  let _timing=crate::code_metrics::span(crate::code_metrics::Stage::Seal);
  self.check_disk()?;
  let integrity:String=self.db.query_row("PRAGMA quick_check(1)",[],|r|r.get(0)).map_err(err)?;
  if integrity!="ok"{return Err("invalid_evidence: partition integrity".into());}
  for table in ["files","nodes","edges"]{let count:i64=self.db.query_row(&format!("SELECT count(*) FROM {table}"),[],|r|r.get(0)).map_err(err)?;self.db.execute("INSERT INTO meta VALUES(?1,?2)",params![table,count.to_string()]).map_err(err)?;}
  let kinds:i64=self.db.query_row("SELECT count(*) FROM occurrence_kinds",[],|r|r.get(0)).map_err(err)?;
  let occurrences:i64=self.db.query_row("SELECT count(*) FROM nodes WHERE kind='occurrence'",[],|r|r.get(0)).map_err(err)?;
  if kinds!=occurrences{return Err("invalid_evidence: incomplete typed occurrence metadata".into());}
  self.db.execute("INSERT INTO meta VALUES('visual_occurrences',?1)",params![kinds.to_string()]).map_err(err)?;
  self.db.execute("INSERT INTO meta VALUES('sealed','1')",[]).map_err(err)?;
  // Finish SQLite's writer lifetime before capturing immutable lineage. Read
  // opens must not change this best-effort corruption identity.
  if let Some(db)=self.db.0.take(){db.close().map_err(|(_,e)|err(e))?;}
  self.pin.sync_all().map_err(err)?;self.directory.pin.sync_all().map_err(err)?;
  let db=Connection::open_with_flags(pinned_path(&self.pin,true)?,rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY|rusqlite::OpenFlags::SQLITE_OPEN_URI).map_err(err)?;
  db.execute_batch("PRAGMA query_only=ON; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-8192; BEGIN").map_err(err)?;
  self.db.0=Some(db);Ok(())
 }
 pub(crate) fn stamp(&self)->Result<String,String>{
  same_file(&self.pin,&self.path)?;let m=self.pin.metadata().map_err(err)?;
  #[cfg(unix)] {use std::os::unix::fs::MetadataExt;
   Ok(blake3::hash(format!("{}:{}:{}:{}:{}:{}:{}",m.dev(),m.ino(),m.len(),m.mtime(),m.mtime_nsec(),m.ctime(),m.ctime_nsec()).as_bytes()).to_hex().to_string())}
  #[cfg(not(unix))] {let _=m;Err("reindex_required: partition identity is unsupported".into())}
 }
 pub(crate) fn verify_stamp(&self,expected:&str)->Result<(),String>{
  if expected.is_empty()||self.stamp()?!=expected{return Err("reindex_required: partition cache identity changed".into());}Ok(())
 }
 pub fn published(&mut self){self.remove=false;}
}

impl SqliteStore {
 // Exact no-change verification retains the published head, never certifies
 // source-current evidence. All current bytes and membership are checked; the
 // prior artifact supplies data only under its pinned published identity.
 fn verify_partition_no_change(&self,scope:&GraphScope,root:&Path,binding:Option<&WorkspaceBinding>,identity:&str,
  paths:&[PathBuf],policy:&SelectionPolicy,result:&CodeIndexResult,head:&str,previous:&PriorExtraction)->Result<Option<CodeIndexResult>,String>{
  // Acquire before any FM/authority lock. Prior payload validation is serial
  // and can decode the existing larger component window. On a conservative
  // miss this drops before ordinary extraction starts.
  let _partition_data=self.index_scheduler().data(crate::code_index_scheduler::LARGE_DATA_BYTES)?;
  let checkout=&result.codebase;
  let (snapshot,coverage,errors,symbols):(Option<String>,Option<String>,Option<String>,i64)={let conn=self.conn.lock().unwrap();conn.query_row(
   "SELECT CASE WHEN length(CAST(source_snapshot AS BLOB))<=65536 THEN source_snapshot END,CASE WHEN length(CAST(coverage AS BLOB))<=8388608 THEN coverage END,CASE WHEN length(CAST(errors AS BLOB))<=1048576 THEN errors END,symbols FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND run_id=?4 AND status='ready'",
   params![scope.owner,scope.workspace_id,checkout,head],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).map_err(err)?};
  let Some(snapshot)=snapshot else{return Ok(None)};
  let snapshot:CodeSourceSnapshot=serde_json::from_str(&snapshot).map_err(err)?;
  // Optional eligibility bounds are not new limits on ordinary indexing.
  // Oversize metadata is never materialized here; the full pipeline remains
  // available. Malformed metadata within these bounds still fails closed.
  let (Some(coverage),Some(errors))=(coverage,errors)else{return Ok(None)};
  let coverage:Vec<ScanCoverage>=serde_json::from_str(&coverage).map_err(err)?;
  let errors:Vec<String>=serde_json::from_str(&errors).map_err(err)?;
  if coverage.len()>100_000||errors.len()>10_000{return Ok(None);}if symbols<0{return Err(quota());}
  let mut current_snapshot=result.source_snapshot.clone();current_snapshot.evidence_store_version=snapshot.evidence_store_version;current_snapshot.evidence_cache_identity=snapshot.evidence_cache_identity.clone();current_snapshot.file_manifest_digest=snapshot.file_manifest_digest.clone();
  if current_snapshot!=snapshot{return Ok(None);}
  let mut by_path=BTreeMap::new();for entry in &coverage{
   if entry.rel_path.len()>4096||entry.status.len()>32||entry.detail.as_ref().is_some_and(|s|s.len()>4096)||entry.source_hash.as_ref().is_some_and(|h|h.len()!=64)||entry.source_generation.as_ref().is_some_and(|s|s.len()>256)||by_path.insert(entry.rel_path.as_str(),entry).is_some(){return Err("reindex_required: invalid prior coverage".into());}
  }
  if result.coverage.len()!=coverage.len(){return Ok(None);}
  let selected:BTreeSet<_>=paths.iter().map(|p|p.strip_prefix(root).ok().and_then(Path::to_str)).collect();
  if selected.contains(&None){return Err("unsupported source path".into());}
  for entry in &result.coverage{
   let Some(old)=by_path.get(entry.rel_path.as_str())else{return Ok(None)};
   if selected.contains(&Some(entry.rel_path.as_str())){
    if entry.status!="selected"||!matches!(old.status.as_str(),"indexed"|"no_source_units"|"parse_failed"|"binary")||old.source_hash.as_ref().is_none_or(|h|!h.bytes().all(|b|b.is_ascii_hexdigit()))||old.source_generation.as_deref()!=Some(head){return Ok(None);}
   }else if entry.status!=old.status||entry.detail!=old.detail||old.source_hash.is_some(){return Ok(None);}
  }
  let mut expected=BTreeMap::new();let mut bytes=0usize;let mut successes=0usize;let mut declarations=0usize;
  for (completed,path) in paths.iter().enumerate(){
   self.index_scheduler().verification_checkpoint()?;
   if let Some(b)=binding{b.validate()?;}policy.validate_source(root,identity,path)?;
   {let conn=self.conn.lock().unwrap();let n=conn.execute("UPDATE code_index_runs SET completed_files=?1,total_files=?2,heartbeat_at=?3 WHERE owner=?4 AND workspace_id=?5 AND codebase=?6 AND run_id=?7 AND status='running'",params![completed as i64,paths.len() as i64,chrono::Utc::now().to_rfc3339(),scope.owner,scope.workspace_id,checkout,result.run_id]).map_err(err)?;if n!=1{return Err("superseded_or_cancelled".into());}}
   let rel=path.strip_prefix(root).map_err(err)?.to_str().ok_or("unsupported source path")?;let old=by_path.get(rel).copied().ok_or("reindex_required: selected coverage absent")?;
   let acquired=match acquire_source_pinned(root,path,identity){Ok(a)=>a,Err(_)=>return Ok(None)};bytes=bytes.checked_add(acquired.bytes.len()).ok_or_else(quota)?;if bytes>MAX_SOURCE{return Err(quota());}
   let hash=crate::code_metrics::source_hash(&acquired.bytes).to_hex().to_string();if old.source_hash.as_deref()!=Some(hash.as_str()){return Ok(None);}
   if matches!(old.status.as_str(),"indexed"|"no_source_units"){
    let Some(indexed)=previous.reuse(checkout,rel,path,&acquired,&hash)?else{return Ok(None)};
    let id:i64=previous.generation.db.query_row("SELECT id FROM files WHERE path=?1",params![rel],|r|r.get(0)).map_err(err)?;
    // Validate the retained sealed resolution payload hash/codec too, without
    // invoking a new resolver or copying rows into a new generation.
    previous.generation.resolutions(id)?;successes+=1;declarations=declarations.checked_add(indexed.symbol_count).ok_or_else(quota)?;
   }
   expected.insert(rel.to_owned(),hash);policy.validate_source(root,identity,path)?;if let Some(b)=binding{b.validate()?;}
  }
  let prior_successes:i64=previous.generation.db.query_row("SELECT count(*) FROM files",[],|r|r.get(0)).map_err(err)?;
  if usize::try_from(prior_successes).ok()!=Some(successes)||usize::try_from(symbols).ok()!=Some(declarations){return Err("reindex_required: no-change file counts differ".into());}
  let mut manifest=BTreeMap::new();for entry in &coverage{
   let value=if matches!(entry.status.as_str(),"indexed"|"no_source_units"){expected.get(&entry.rel_path).ok_or("reindex_required: missing no-change hash")?.clone()}
    else if selected.contains(&Some(entry.rel_path.as_str())){format!("failed:{}",entry.detail.as_deref().ok_or("reindex_required: missing prior failure")?)}
    else{format!("{}:{}",entry.status,entry.detail.as_deref().unwrap_or(""))};manifest.insert(&entry.rel_path,value);
  }
  let mut digest=blake3::Hasher::new();digest.update(policy.digest().as_bytes());digest.update(&[0]);for(path,value)in manifest{digest.update(path.as_bytes());digest.update(&[0]);digest.update(value.as_bytes());digest.update(&[0]);}
  if digest.finalize().to_hex().as_str()!=snapshot.file_manifest_digest{return Err("reindex_required: no-change manifest differs".into());}
  #[cfg(test)] {let hook=NO_CHANGE_TEST_HOOK.with(|slot|slot.borrow_mut().take());if let Some(hook)=hook{hook();}}
  // Re-acquire every selected source: policy-only validation cannot prove a
  // same-stat byte edit did not race preflight. Check run/epoch BEFORE each
  // acquisition too, so cancellation does not read the rest of the corpus.
  let mut final_bytes=0usize;for path in paths{
   self.index_scheduler().verification_checkpoint()?;
   {let conn=self.conn.lock().unwrap();let active:i64=conn.query_row("SELECT count(*) FROM code_index_runs r JOIN code_registrations c ON c.owner=r.owner AND c.workspace_id=r.workspace_id AND c.checkout_id=r.codebase WHERE r.owner=?1 AND r.workspace_id=?2 AND r.codebase=?3 AND r.run_id=?4 AND r.status='running' AND c.lifecycle!='erased' AND r.admission_epoch=c.admission_epoch AND r.config_fingerprint=c.config_fingerprint",params![scope.owner,scope.workspace_id,checkout,result.run_id],|r|r.get(0)).map_err(err)?;if active!=1{return Err("superseded_or_cancelled".into());}}
   if let Some(b)=binding{b.validate()?;}policy.validate_source(root,identity,path)?;let acquired=acquire_source_pinned(root,path,identity)?;final_bytes=final_bytes.checked_add(acquired.bytes.len()).ok_or_else(quota)?;if final_bytes>MAX_SOURCE{return Err(quota());}
   let rel=path.strip_prefix(root).map_err(err)?.to_str().ok_or("unsupported source path")?;if expected.get(rel)!=Some(&crate::code_metrics::source_hash(&acquired.bytes).to_hex().to_string()){return Err("source_changed: partition no-change bytes changed".into());}policy.validate_source(root,identity,path)?;
  }
  #[cfg(test)] {let hook=NO_CHANGE_FINAL_TEST_HOOK.with(|slot|slot.borrow_mut().take());if let Some(hook)=hook{hook();}}
  // Final complete discovery runs AFTER byte verification so a new nested
  // control/directory created during that pass cannot evade known-input-only
  // policy validation. This is still a bounded snapshot, not an atomic FS lock.
  self.index_scheduler().verification_checkpoint()?;
  let final_scan=scan_codebase_pinned(root,identity)?;
  if final_scan.policy.digest()!=policy.digest()||final_scan.files.as_slice()!=paths||final_scan.coverage.len()!=result.coverage.len()||!final_scan.coverage.iter().zip(&result.coverage).all(|(a,b)|a.rel_path==b.rel_path&&a.status==b.status&&a.detail==b.detail){return Err("source_changed: partition no-change inventory changed".into());}
  let mut verified=result.clone();verified.source_snapshot=snapshot;verified.coverage=coverage;verified.errors=errors;verified.files_indexed=0;verified.files_unchanged=successes;verified.files_removed=0;verified.symbols=declarations;
  policy.validate(root,identity)?;previous.generation.verify_stamp(&previous.stamp)?;
  let authority=binding.map(WorkspaceBinding::publication_guard).transpose()?;
  let conn=self.conn.lock().unwrap();conn.execute_batch("BEGIN IMMEDIATE").map_err(err)?;
  let committed=(||{
   let current:Option<String>=conn.query_row("SELECT run_id FROM code_index_heads WHERE owner=?1 AND workspace_id=?2 AND codebase=?3",params![scope.owner,scope.workspace_id,checkout],|r|r.get(0)).optional().map_err(err)?;
   if current.as_deref()!=Some(head){return Err("superseded partition base".into());}
   let fenced:i64=conn.query_row("SELECT count(*) FROM code_index_runs r JOIN code_registrations c ON c.owner=r.owner AND c.workspace_id=r.workspace_id AND c.checkout_id=r.codebase WHERE r.owner=?1 AND r.workspace_id=?2 AND r.codebase=?3 AND r.run_id=?4 AND r.status='running' AND c.lifecycle!='erased' AND r.admission_epoch=c.admission_epoch AND r.config_fingerprint=c.config_fingerprint",params![scope.owner,scope.workspace_id,checkout,result.run_id],|r|r.get(0)).map_err(err)?;
   if fenced!=1{return Err("superseded_or_cancelled".into());}policy.validate(root,identity)?;previous.generation.verify_stamp(&previous.stamp)?;
   if directory_identity(root)?!=identity{return Err("denied: directory changed before no-change commit".into());}
   if let (Some(b),Some(a))=(binding,authority.as_ref()){b.validate_authority(a)?;}
   Self::finish_code_index_run(&conn,scope,checkout,&result.run_id,"verified",&verified)
  })();
  if let Err(e)=committed{let _=conn.execute_batch("ROLLBACK");return Err(e);}if let Err(e)=conn.execute_batch("COMMIT"){let _=conn.execute_batch("ROLLBACK");return Err(err(e));}Ok(Some(verified))
 }
 // Drain every owned small reservation before waiting for an exclusive retry.
 // Results remain reserved while borrowed for stage; no naked IndexedFile is
 // moved into a retained whole-corpus vector. One deterministic source batch
 // is replayed only on internal small-output exhaustion, never parser failure.
 fn flush_partition_extraction(&self,pending:&mut Vec<(PathBuf,String,String,crate::code_index_scheduler::TaskHandle<IndexedFile>)>,
  scope:&GraphScope,root:&Path,binding:Option<&WorkspaceBinding>,identity:&str,policy:&SelectionPolicy,checkout:&str,run:&str,
  generation:&mut Generation,result:&mut CodeIndexResult,manifest:&mut BTreeMap<String,String>)->Result<(),String>{
  if pending.is_empty(){return Ok(());}
  let mut done=Vec::with_capacity(pending.len());
  for(path,rel,hash,handle)in std::mem::take(pending){let output=handle.wait();drop(handle);done.push((path,rel,hash,output));}
  for(_,_,_,output)in &done{if let Err(e)=output{if e.starts_with("worker_fence:")||e.starts_with("cancelled:")||e.starts_with("index_worker_failed:"){return Err(e.clone());}}}
  let upgrade=done.iter().any(|(_,_,_,output)|output.as_ref().is_err_and(|e|e.starts_with("budget_exceeded:")));
  if upgrade{
   // Retain only bounded path/hash descriptors. Drop all task results before
   // any exclusive wait, including successful out-of-order outputs. Their
   // second attempts are measured honestly; source changes abort, not mix.
   let retry:Vec<_>=done.into_iter().map(|(path,rel,hash,output)|{drop(output);(path,rel,hash)}).collect();
   #[cfg(test)] {let hook=S04_UPGRADE_TEST_HOOK.with(|slot|slot.borrow_mut().take());if let Some(hook)=hook{hook();}}
   let scheduler=self.index_scheduler();
   for(path,rel,hash)in retry{
    let _reservation=scheduler.data(crate::code_index_scheduler::LARGE_DATA_BYTES)?;
    self.partition_extraction_fence(scope,root,binding,identity,policy,checkout,run,&path)?;
    let acquired=acquire_source_pinned(root,&path,identity)?;
    if crate::code_metrics::source_hash(&acquired.bytes).to_hex().as_str()!=hash{return Err("source_changed: indexing upgrade source changed".into());}
    policy.validate_source(root,identity,&path)?;
    let mut budget=ExtractionBudget::default();budget.max_serialized_bytes=storage::MAX_DECODED;
    let indexed=crate::code::index_acquired_budget(checkout,root,&path,&acquired,&budget);
    drop(acquired);
    self.accept_partition_extraction(scope,root,binding,identity,policy,checkout,run,&path,&rel,&hash,indexed.as_ref().map_err(String::as_str),generation,result,manifest)?;
   }
  }else{
   for(path,rel,hash,indexed)in done{
    self.accept_partition_extraction(scope,root,binding,identity,policy,checkout,run,&path,&rel,&hash,indexed.as_ref().map(|value|&**value).map_err(String::as_str),generation,result,manifest)?;
   }
  }
  Ok(())
 }
 fn partition_extraction_fence(&self,scope:&GraphScope,root:&Path,binding:Option<&WorkspaceBinding>,identity:&str,policy:&SelectionPolicy,checkout:&str,run:&str,path:&Path)->Result<(),String>{
  if let Some(b)=binding{b.validate()?;}policy.validate_source(root,identity,path)?;
  let conn=self.conn.lock().unwrap();let current:i64=conn.query_row("SELECT count(*) FROM code_index_runs r JOIN code_registrations c ON c.owner=r.owner AND c.workspace_id=r.workspace_id AND c.checkout_id=r.codebase WHERE r.owner=?1 AND r.workspace_id=?2 AND r.codebase=?3 AND r.run_id=?4 AND r.status='running' AND c.lifecycle!='erased' AND r.admission_epoch=c.admission_epoch AND r.config_fingerprint=c.config_fingerprint",params![scope.owner,scope.workspace_id,checkout,run],|r|r.get(0)).map_err(err)?;
  if current!=1{return Err("superseded_or_cancelled".into());}Ok(())
 }
 fn accept_partition_extraction(&self,scope:&GraphScope,root:&Path,binding:Option<&WorkspaceBinding>,identity:&str,policy:&SelectionPolicy,checkout:&str,run:&str,path:&Path,rel:&str,hash:&str,indexed:Result<&IndexedFile,&str>,generation:&mut Generation,result:&mut CodeIndexResult,manifest:&mut BTreeMap<String,String>)->Result<(),String>{
  self.partition_extraction_fence(scope,root,binding,identity,policy,checkout,run,path)?;
  let entry=result.coverage.iter_mut().find(|c|c.rel_path==rel).ok_or("invalid scan coverage")?;entry.source_hash=Some(hash.into());entry.source_generation=Some(run.into());
  match indexed{
   Ok(indexed)=>{
    if indexed.blake3!=hash||indexed.rel_path!=rel{return Err("source_changed: indexing worker receipt differs".into());}
    generation.stage(&indexed.evidence,indexed.mtime_ns,&indexed.upsert,scope)?;result.files_indexed+=1;result.symbols+=indexed.symbol_count;entry.status=if indexed.upsert.edges.is_empty(){"no_source_units"}else{"indexed"}.into();entry.detail=None;manifest.insert(rel.into(),hash.into());
   },
   Err(e)=>{if e.starts_with("budget_exceeded:"){return Err(e.into());}entry.status=if e=="binary source"{"binary"}else{"parse_failed"}.into();entry.detail=Some(e.into());result.errors.push(format!("{rel}: {e}"));manifest.insert(rel.into(),format!("failed:{e}"));}
  }Ok(())
 }
 pub(crate) fn code_index_partitioned(&self,scope:&GraphScope,root:&Path,binding:Option<&WorkspaceBinding>,identity:&str,
   paths:Vec<PathBuf>,policy:&SelectionPolicy,mut result:CodeIndexResult)->Result<CodeIndexResult,String>{
  let checkout=result.codebase.clone();let run=result.run_id.clone();
  if let Some(binding)=binding{binding.validate()?;}
  let (prior,previous)={let conn=self.conn.lock().unwrap();
   let prior:Option<String>=conn.query_row("SELECT run_id FROM code_index_heads WHERE owner=?1 AND workspace_id=?2 AND codebase=?3",params![scope.owner,scope.workspace_id,checkout],|r|r.get(0)).optional().map_err(err)?;
   let previous=prior.as_deref().map(|head|PriorExtraction::from_head(&conn,scope,&checkout,head,&policy.digest(),binding,&self.code_query_audit)).transpose()?.flatten();
   (prior,previous)};
  if let (Some(head),Some(previous))=(prior.as_deref(),previous.as_ref()){if let Some(verified)=self.verify_partition_no_change(scope,root,binding,identity,&paths,policy,&result,head,previous)?{return Ok(verified);}}
  let mut generation={let conn=self.conn.lock().unwrap();Generation::create(&conn,scope,&checkout,&run,&policy.digest())?};
  let mut source_bytes=0usize;let mut manifest=BTreeMap::new();
  let mut extraction=ExtractionBudget::default();extraction.max_serialized_bytes=storage::MAX_DECODED;
  let scheduler=self.index_scheduler();
  let worker_policy=std::sync::Arc::new(policy.clone());
  let mut pending=Vec::new();
  let selected=paths.len();
  for (completed,path) in paths.iter().enumerate(){
   if let Some(b)=binding{b.validate()?;}
   {let conn=self.conn.lock().unwrap();let changed=conn.execute("UPDATE code_index_runs SET heartbeat_at=?1,completed_files=?2,total_files=?3 WHERE owner=?4 AND workspace_id=?5 AND codebase=?6 AND run_id=?7 AND status='running'",params![chrono::Utc::now().to_rfc3339(),completed as i64,selected as i64,scope.owner,scope.workspace_id,checkout,run]).map_err(err)?;if changed!=1{return Err("superseded_or_cancelled".into());}}
   policy.validate_source(root,identity,path)?;
   let rel=path.strip_prefix(root).map_err(err)?.to_str().ok_or("unsupported source path")?.to_owned();
   // Prior-cache decoding has independent 64 MiB component bounds; do not
   // decode it under a small-task reservation. This first scheduler increment
   // retains conservative serial reuse. A cold generation has no such cache.
   if previous.is_some(){self.flush_partition_extraction(&mut pending,scope,root,binding,identity,policy,&checkout,&run,&mut generation,&mut result,&mut manifest)?;}
   let mut reservation=Some(if previous.is_some(){scheduler.data(crate::code_index_scheduler::LARGE_DATA_BYTES)?}else{
    match scheduler.try_data(crate::code_index_scheduler::SMALL_DATA_BYTES)?{
     Some(data)=>data,None=>{self.flush_partition_extraction(&mut pending,scope,root,binding,identity,policy,&checkout,&run,&mut generation,&mut result,&mut manifest)?;scheduler.data(crate::code_index_scheduler::SMALL_DATA_BYTES)?}
    }
   });
   let acquired=crate::code::acquire_source_pinned(root,path,identity);
   let mut hash=None;
   let (indexed,reused)=match acquired {
    Ok(acquired)=>{
     policy.validate_source(root,identity,path)?;
     source_bytes=source_bytes.checked_add(acquired.bytes.len()).ok_or_else(quota)?;if source_bytes>MAX_SOURCE{return Err(quota());}
     let current_hash=crate::code_metrics::source_hash(&acquired.bytes).to_hex().to_string();hash=Some(current_hash.clone());
     if previous.is_none(){
      let task_root=root.to_path_buf();let task_path=path.clone();let task_identity=identity.to_owned();let task_policy=worker_policy.clone();let task_binding=binding.cloned();let task_checkout=checkout.clone();
      #[cfg(not(test))] let output_limit=8*1024*1024;
      #[cfg(test)] let output_limit=S04_SMALL_OUTPUT_TEST_LIMIT.with(|limit|limit.get());
      #[cfg(test)] let worker_hook=S04_WORKER_TEST_HOOK.with(|slot|slot.borrow().clone());
      let task=scheduler.submit(reservation.take().ok_or("invalid: missing extraction reservation")?,move||{
       #[cfg(test)] if let Some(hook)=&worker_hook{hook(&task_path,false);}
       let fence=||->Result<(),String>{if let Some(b)=&task_binding{b.validate()?;}task_policy.validate_source(&task_root,&task_identity,&task_path)};
       fence().map_err(|e|format!("worker_fence: {e}"))?;
       let mut budget=ExtractionBudget::default();budget.max_serialized_bytes=output_limit;
       let indexed=crate::code::index_acquired_graph_budget(&task_checkout,&task_root,&task_path,&acquired,&budget,output_limit);
       #[cfg(test)] if let Some(hook)=&worker_hook{hook(&task_path,true);}
       fence().map_err(|e|format!("worker_fence: {e}"))?;indexed
      })?;
      pending.push((path.clone(),rel,current_hash,task));
      if pending.len()>=scheduler.status().effective{self.flush_partition_extraction(&mut pending,scope,root,binding,identity,policy,&checkout,&run,&mut generation,&mut result,&mut manifest)?;}
      continue;
     }
     let reused=previous.as_ref().map(|prior|prior.reuse(&checkout,&rel,path,&acquired,&current_hash)).transpose()?.flatten();
     let output=if let Some(indexed)=reused{(Ok(indexed),true)}else{(crate::code::index_acquired_budget(&checkout,root,path,&acquired,&extraction),false)};
     if let Some(b)=binding{b.validate()?;}policy.validate_source(root,identity,path)?;output
    },Err(e)=>(Err(e),false)
   };
   // Acquisition failures must not leapfrog earlier queued source results.
   if previous.is_none(){drop(reservation.take());}
   self.flush_partition_extraction(&mut pending,scope,root,binding,identity,policy,&checkout,&run,&mut generation,&mut result,&mut manifest)?;
   let entry=result.coverage.iter_mut().find(|c|c.rel_path==rel).ok_or("invalid scan coverage")?;
   entry.source_hash=hash.clone();entry.source_generation=Some(run.clone());
   match indexed {
    Ok(indexed)=>{generation.stage(&indexed.evidence,indexed.mtime_ns,&indexed.upsert,scope)?;
     if reused{result.files_unchanged+=1;}else{result.files_indexed+=1;}result.symbols+=indexed.symbol_count;entry.status=if indexed.upsert.edges.is_empty(){"no_source_units"}else{"indexed"}.into();entry.detail=None;
     manifest.insert(rel,indexed.blake3);
     #[cfg(test)] if reused{let hook=REUSE_STAGE_TEST_HOOK.with(|slot|slot.borrow_mut().take());if let Some(hook)=hook{hook();}}
    },
    Err(e)=>{if e.starts_with("budget_exceeded:"){return Err(e);}
     entry.status=if e=="binary source"{"binary"}else{"parse_failed"}.into();entry.detail=Some(e.clone());
     result.errors.push(format!("{rel}: {e}"));manifest.insert(rel,format!("failed:{e}"));
    }
   }
   drop(reservation);
  }
  self.flush_partition_extraction(&mut pending,scope,root,binding,identity,policy,&checkout,&run,&mut generation,&mut result,&mut manifest)?;
  if let Some(previous)=&previous{previous.generation.verify_stamp(&previous.stamp)?;}drop(previous);
  for entry in &result.coverage {manifest.entry(entry.rel_path.clone()).or_insert_with(||format!("{}:{}",entry.status,entry.detail.as_deref().unwrap_or("")));}
  let mut digest=blake3::Hasher::new();digest.update(policy.digest().as_bytes());digest.update(&[0]);
  for (p,h) in manifest{digest.update(p.as_bytes());digest.update(&[0]);digest.update(h.as_bytes());digest.update(&[0]);}
  result.source_snapshot.file_manifest_digest=digest.finalize().to_hex().to_string();result.source_snapshot.evidence_store_version=STORE_VERSION;
  let unavailable:Vec<_>=result.coverage.iter().filter(|c|crate::code::eligible_unavailable(c)).map(|c|UnavailableFile{rel_path:c.rel_path.clone(),source_hash:c.source_hash.clone(),reason:c.status.clone()}).collect();
  // All extraction handles/results/leases and prior-cache references have
  // dropped. Reserve before resolution and BEFORE publication guards/locks;
  // retain through serial active-payload decode, seal and graph publication.
  // This is not a claim that codec/allocator/interface RSS fits 256 MiB.
  let _partition_data=scheduler.data(crate::code_index_scheduler::LARGE_DATA_BYTES)?;
  generation.resolve(&checkout,&unavailable,||{
   if let Some(b)=binding{b.validate()?;}
   let conn=self.conn.lock().unwrap();let count=conn.execute("UPDATE code_index_runs SET heartbeat_at=?1 WHERE owner=?2 AND workspace_id=?3 AND codebase=?4 AND run_id=?5 AND status='running'",params![chrono::Utc::now().to_rfc3339(),scope.owner,scope.workspace_id,checkout,run]).map_err(err)?;
   if count!=1{return Err("superseded_or_cancelled".into());}Ok(())
  })?;generation.seal()?;
  result.source_snapshot.evidence_cache_identity=generation.stamp()?;
  policy.validate(root,identity)?;
  let authority=if let Some(b)=binding{Some(b.publication_guard()?)}else{None};
  if crate::code::directory_identity(root)?!=identity{return Err("denied: directory replaced before partition publication".into());}
  generation.check_disk()?;
  let _publication_timing=crate::code_metrics::span(crate::code_metrics::Stage::Publish);
  let conn=self.conn.lock().unwrap();conn.execute_batch("BEGIN IMMEDIATE").map_err(err)?;
  let publish=(||{
   let current:Option<String>=conn.query_row("SELECT run_id FROM code_index_heads WHERE owner=?1 AND workspace_id=?2 AND codebase=?3",params![scope.owner,scope.workspace_id,checkout],|r|r.get(0)).optional().map_err(err)?;
   if current!=prior{return Err("superseded partition base".into());}
   let fenced:i64=conn.query_row("SELECT count(*) FROM code_index_runs r JOIN code_registrations c ON c.owner=r.owner AND c.workspace_id=r.workspace_id AND c.checkout_id=r.codebase WHERE r.owner=?1 AND r.workspace_id=?2 AND r.codebase=?3 AND r.run_id=?4 AND r.status='running' AND c.lifecycle!='erased' AND r.admission_epoch=c.admission_epoch AND r.config_fingerprint=c.config_fingerprint",params![scope.owner,scope.workspace_id,checkout,run],|r|r.get(0)).map_err(err)?;
   if fenced!=1{return Err("superseded_or_cancelled".into());}
   let key=Self::codebase_key(scope,&checkout);
   let prior_files:i64=conn.query_row("SELECT count(*) FROM code_files WHERE codebase=?1",params![key],|r|r.get(0)).map_err(err)?;
   let mut old=conn.prepare("SELECT id FROM graph_nodes WHERE owner=?1 AND workspace_id=?2 AND substr(name,1,length(?3))=?3").map_err(err)?;
   let old=old.query_map(params![scope.owner,scope.workspace_id,format!("{checkout}::")],|r|r.get::<_,String>(0)).map_err(err)?.collect::<rusqlite::Result<Vec<_>>>().map_err(err)?;
   if old.len()>MAX_SOURCE_RECORDS{return Err(quota());}
   for id in old {let keep:i64=generation.db.query_row("SELECT count(*) FROM graph_ids WHERE id=?1",params![id],|r|r.get(0)).map_err(err)?;
    if keep==0 {conn.execute("DELETE FROM graph_edges WHERE src_id=?1 OR dst_id=?1",params![id]).map_err(err)?;conn.execute("DELETE FROM graph_cues WHERE node_id=?1",params![id]).map_err(err)?;let _=conn.execute("DELETE FROM graph_cues_fts WHERE node_id=?1",params![id]);conn.execute("DELETE FROM graph_nodes WHERE id=?1",params![id]).map_err(err)?;}
   }
   let removed:i64=conn.query_row("SELECT count(*) FROM code_files WHERE codebase=?1 AND rel_path NOT IN (SELECT json_extract(value,'$.rel_path') FROM json_each(?2) WHERE json_extract(value,'$.status') IN ('indexed','no_source_units'))",params![key,serde_json::to_string(&result.coverage).map_err(err)?],|r|r.get(0)).map_err(err)?;
   result.files_removed=removed as usize;
   conn.execute("DELETE FROM code_files WHERE codebase=?1",params![key]).map_err(err)?;
   conn.execute("DELETE FROM code_resolutions WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,checkout]).map_err(err)?;
   conn.execute("DELETE FROM code_file_evidence WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,checkout]).map_err(err)?;
   let mut stmt=generation.db.prepare("SELECT path,hash,source_bytes,mtime,symbols,CASE WHEN length(graph)<=8388608 THEN graph END FROM files ORDER BY id").map_err(err)?;
   for row in stmt.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,i64>(2)?,r.get::<_,i64>(3)?,r.get::<_,i64>(4)?,r.get::<_,Option<Vec<u8>>>(5)?))).map_err(err)? {
    let (path,hash,bytes,mtime,symbols,graph)=row.map_err(err)?;
    let upsert:crate::graph::GraphUpsertInput=storage::decode(&graph.ok_or_else(quota)?)?;
    Self::graph_upsert_inner(&conn,scope,&upsert).map_err(err)?;
    conn.execute("INSERT INTO code_files(codebase,rel_path,blake3,mtime_ns,size,symbol_count,indexed_at,config_fingerprint,source_generation)VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",params![key,path,hash,mtime,bytes,symbols,chrono::Utc::now().to_rfc3339(),crate::code::producer_fingerprint(),run]).map_err(err)?;
   }
   let _=prior_files;
   policy.validate(root,identity)?;generation.check_disk()?;
   if crate::code::directory_identity(root)?!=identity{return Err("denied: directory changed before commit".into());}
   if let (Some(b),Some(a))=(binding,authority.as_ref()){b.validate_authority(a)?;}
   Self::finish_code_index_run(&conn,scope,&checkout,&run,"ready",&result)?;
   Ok(())
  })();
  if let Err(e)=publish{let _=conn.execute_batch("ROLLBACK");return Err(e);}
  // From this point a crash may leave a sealed orphan, never delete a cache
  // that a successful main-DB COMMIT has made authoritative.
  generation.published();
  if let Err(e)=conn.execute_batch("COMMIT"){let _=conn.execute_batch("ROLLBACK");return Err(err(e));}
  Ok(result)
 }
}

pub(crate) fn is_partitioned(conn:&Connection,scope:&GraphScope,receipt:&super::code_evidence::CodeEvidenceReceipt)->Result<bool,String>{
 let mode:Option<i64>=conn.query_row("SELECT CASE WHEN length(CAST(source_snapshot AS BLOB))<=65536 THEN json_extract(source_snapshot,'$.evidence_store_version') ELSE -1 END FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND run_id=?4",params![scope.owner,scope.workspace_id,receipt.checkout_id,receipt.head_generation],|r|r.get(0)).map_err(err)?;
 match mode{None|Some(0)=>Ok(false),Some(1)=>Ok(true),_=>Err("reindex_required: unsupported partition store".into())}
}
fn filter_sql()->&'static str{
 "(?1 IS NULL OR f.path=?1) AND (?2 IS NULL OR substr(f.path,1,length(?2))=?2) AND (json_array_length(?3)=0 OR n.kind IN(SELECT value FROM json_each(?3))) AND (json_array_length(?4)=0 OR json_extract(f.header,'$.language') IN(SELECT value FROM json_each(?4))) AND (json_array_length(?5)=0 OR n.state IN(SELECT value FROM json_each(?5)))"
}
fn matches_coverage(row:&ScanCoverage,filter:&QueryFilter)->bool{
 filter.rel_path.as_ref().is_none_or(|p|p==&row.rel_path)&&filter.path_prefix.as_ref().is_none_or(|p|row.rel_path.starts_with(p))
 &&(filter.kinds.is_empty()||filter.kinds.iter().any(|k|k=="file"))&&filter.languages.is_empty()
 &&(filter.states.is_empty()||filter.states.contains(&row.status))
}
impl Generation {
 fn eligible_nodes(&self,query:&CodeQuery)->Result<usize,QueryError>{
  let count=||{
   let _sql=crate::code_metrics::span(crate::code_metrics::Stage::QuerySql);
   let count:i64=self.db.query_row(&format!("SELECT count(*) FROM nodes n JOIN files f ON f.id=n.file WHERE {}",filter_sql()),params![query.filter.rel_path,query.filter.path_prefix,serde_json::to_string(&query.filter.kinds).unwrap(),serde_json::to_string(&query.filter.languages).unwrap(),serde_json::to_string(&query.filter.states).unwrap()],|r|r.get(0)).map_err(qerr)?;
   usize::try_from(count).map_err(|_|qerr("invalid eligible count"))
  };
  let filter=&query.filter;
  let unfiltered=filter.rel_path.is_none()&&filter.path_prefix.is_none()&&filter.kinds.is_empty()&&filter.languages.is_empty()&&filter.states.is_empty();
  if unfiltered{if let Some((memo,key))=&self.audit_memo{
   self.verify_stamp(&key.expected).map_err(qerr)?;
   if let Some(value)=memo.count(key){let _hit=crate::code_metrics::span(crate::code_metrics::Stage::EligibleCountHit);self.verify_stamp(&key.expected).map_err(qerr)?;return Ok(value);}
   let _miss=crate::code_metrics::span(crate::code_metrics::Stage::EligibleCountMiss);
   let value=count()?;self.verify_stamp(&key.expected).map_err(qerr)?;memo.remember_count(key,value);return Ok(value);
  }}
  count()
 }
 fn resolutions(&self,file:i64)->Result<Vec<ResolutionEvidence>,String>{
  let (payload,hash):(Option<Vec<u8>>,Option<String>)=self.db.query_row("SELECT CASE WHEN length(resolutions)<=8388608 THEN resolutions END,resolution_hash FROM files WHERE id=?1",params![file],|r|Ok((r.get(0)?,r.get(1)?))).map_err(err)?;
  let payload=payload.ok_or_else(quota)?;if Some(blake3::hash(&payload).to_hex().to_string())!=hash{return Err("reindex_required: corrupt resolution partition".into());}storage::decode(&payload)
 }
 fn filtered_nodes(&self,query:&CodeQuery,condition:&str,value:impl rusqlite::ToSql,role:i64)->Result<Vec<(String,i64,String,usize)>,QueryError>{
  let filter=&query.filter;let kinds=serde_json::to_string(&filter.kinds).unwrap();let languages=serde_json::to_string(&filter.languages).unwrap();let states=serde_json::to_string(&filter.states).unwrap();
  let sql=format!("SELECT CASE WHEN length(CAST(n.id AS BLOB))<=4096 THEN n.id END,n.file,CASE WHEN length(CAST(n.kind AS BLOB))<=64 THEN n.kind END,n.ordinal FROM nodes n JOIN files f ON f.id=n.file {condition} AND {} LIMIT ?8",filter_sql());
  let mut stmt=self.db.prepare(&sql).map_err(qerr)?;
  let rows=stmt.query_map(params![filter.rel_path,filter.path_prefix,kinds,languages,states,value,role,query.budgets.work.min(100_000)+1],|r|Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?,r.get::<_,String>(2)?,r.get::<_,usize>(3)?))).map_err(qerr)?.collect::<rusqlite::Result<Vec<_>>>().map_err(qerr)?;
   if rows.len()>query.budgets.work.min(100_000){return Err(qerr(quota()));}Ok(rows)
 }
 fn query_snapshot(&self,scope:&GraphScope,binding:&WorkspaceBinding,receipt:super::code_evidence::CodeEvidenceReceipt,
  registration_id:String,source:CodeSourceSnapshot,coverage:Vec<ScanCoverage>,query:&CodeQuery,cancel:&AtomicBool)->Result<QuerySnapshot,QueryError>{
  let _timing=crate::code_metrics::span(crate::code_metrics::Stage::QueryPrepare);
  let work=std::cell::Cell::new(coverage.len());let mut bytes=0usize;
  let cancelled=||if cancel.load(Ordering::Relaxed){Err(qerr("cancelled"))}else{Ok(())};
  let charge=|n:usize|->Result<(),QueryError>{work.set(work.get().checked_add(n).ok_or_else(||qerr(quota()))?);cancelled()?;if work.get()>query.budgets.work{Err(qerr(quota()))}else{Ok(())}};
  if query.operation==Operation::Capabilities {
   // run_query validates the request, receipt, cursor and envelope before its
   // constant capabilities response. No file evidence/candidate count is used.
   // Keep parsed coverage and its work charge; callers still own full current
   // authority/policy, audited immutable-open and pre/post stamp/head fences.
   charge(0)?;
   return Ok(QuerySnapshot{receipt:QueryReceipt{workspace_id:scope.workspace_id.clone(),registration_id,source_snapshot_id:source.file_manifest_digest,evidence:receipt},
    scope_key:serde_json::to_string(&(scope.owner.as_str(),binding.owner_subject_id.as_str(),scope.workspace_id.as_str(),binding.location_id.as_str(),binding.workspace_revision,binding.policy_generation,binding.origin.as_str(),binding.subscriber_id.as_str())).unwrap(),
    files:vec![],resolutions:vec![],coverage,metadata_bytes:0,metadata_records:work.get(),eligible_records:None});
  }
  let mut files=BTreeMap::new();let mut indexed_paths=BTreeSet::new();
  let mut stmt=self.db.prepare("SELECT id,path,hash,CASE WHEN length(CAST(header AS BLOB))<=65536 THEN header END FROM files ORDER BY id LIMIT 10001").map_err(qerr)?;
  for row in stmt.query_map([],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,Option<String>>(3)?))).map_err(qerr)?{
   charge(1)?;let (id,path,hash,header)=row.map_err(qerr)?;let header=header.ok_or_else(||qerr(quota()))?;bytes=bytes.checked_add(header.len()).ok_or_else(||qerr(quota()))?;
   if files.len()>=10_000 || bytes>storage::MAX_DECODED{return Err(qerr(quota()));}
   let file:FileEvidence=serde_json::from_str(&header).map_err(qerr)?;
   if file.rel_path!=path||file.source_hash!=hash||file.checkout_id!=receipt.checkout_id||file.producer_fingerprint!=receipt.config_fingerprint{return Err(qerr("reindex_required: partition header differs"));}
   indexed_paths.insert(path);files.insert(id,file);
  }
  drop(stmt);
  let count=self.eligible_nodes(query)?;
  let eligible=count+coverage.iter().filter(|c|!indexed_paths.contains(&c.rel_path)&&matches_coverage(c,&query.filter)).count();
  let mut nodes:BTreeMap<String,(i64,String,usize)>=BTreeMap::new();
  let mut add=|rows:Vec<(String,i64,String,usize)>|->Result<(),QueryError>{
   for (id,file,kind,ordinal) in rows {if id.len()>80||kind.len()>16{return Err(qerr("invalid partition node"));}nodes.insert(id,(file,kind,ordinal));}
   if nodes.len()>100_000{return Err(qerr(quota()));}Ok(())
  };
  match query.operation {
   Operation::Search=>{
    let text=query.query.as_deref().unwrap_or("");let mut keys:BTreeMap<i64,i64>=BTreeMap::new();
    if !text.is_empty(){
     if query.channels.contains(&Channel::Exact){let key:Option<i64>=self.db.query_row("SELECT id FROM keys WHERE text=?1",params![text],|r|r.get(0)).optional().map_err(qerr)?;charge(1)?;if let Some(k)=key{*keys.entry(k).or_default()|=1;}}
     if query.channels.contains(&Channel::Identifier){
      let terms=crate::code_query::identifier_terms(text);
      if let Some(term)=terms.first(){let mut stmt=self.db.prepare("SELECT k.id,CASE WHEN length(CAST(k.text AS BLOB))<=524288 THEN k.text END FROM terms t JOIN keys k ON k.id=t.key WHERE t.term=?1").map_err(qerr)?;
       for row in stmt.query_map(params![term],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,Option<String>>(1)?))).map_err(qerr)?{
        let (id,key)=row.map_err(qerr)?;let key=key.ok_or_else(||qerr(quota()))?;let (matched,used)=crate::code_query::index_key_match(text,&key,2,&[Channel::Identifier],query.budgets.work.saturating_sub(work.get()))?;charge(used)?;if matched{*keys.entry(id).or_default()|=2;}
       }
      }
     }
     if query.channels.contains(&Channel::Fuzzy){
      let chars=text.to_lowercase().chars().count();let mut stmt=self.db.prepare("SELECT id,CASE WHEN length(CAST(text AS BLOB))<=524288 THEN text END FROM keys WHERE chars BETWEEN ?1 AND ?2").map_err(qerr)?;
      for row in stmt.query_map(params![chars.saturating_sub(1) as i64,(chars+1)as i64],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,Option<String>>(1)?))).map_err(qerr)?{
       let (id,key)=row.map_err(qerr)?;let key=key.ok_or_else(||qerr(quota()))?;let (matched,used)=crate::code_query::index_key_match(text,&key,4,&[Channel::Fuzzy],query.budgets.work.saturating_sub(work.get()))?;charge(used)?;if matched{*keys.entry(id).or_default()|=4;}
      }
     }
    }
    for (key,roles) in keys {let rows=self.filtered_nodes(query,"JOIN key_nodes kn ON kn.node=n.row WHERE kn.key=?6 AND (kn.roles&?7)!=0",key,roles)?;charge(rows.len())?;add(rows)?;}
   },
   Operation::Capabilities|Operation::Overview|Operation::Catalog|Operation::Facets=>{},
   Operation::Outline|Operation::Read=>{
    let mut paths=BTreeSet::new();if let Some(path)=&query.filter.rel_path{paths.insert(path.clone());}
    for seed in &query.seed_ids{let path:Option<String>=self.db.query_row("SELECT f.path FROM nodes n JOIN files f ON f.id=n.file WHERE n.id=?1",params![seed],|r|r.get(0)).optional().map_err(qerr)?;if let Some(p)=path{paths.insert(p);}}
    if paths.is_empty(){add(self.filtered_nodes(query,"WHERE n.kind='declaration' AND ?6 IS NULL AND ?7=0",rusqlite::types::Null,0)?)?;}
    else{for p in paths{add(self.filtered_nodes(query,"WHERE f.path=?6 AND (n.kind='declaration' OR ?7=1)",p,i64::from(query.operation==Operation::Read))?)?;}}
   },
   Operation::Inspect|Operation::Neighbors|Operation::Impact|Operation::Path=>{
    let mut frontier:BTreeSet<String>=query.seed_ids.iter().cloned().collect();let mut visited=BTreeSet::new();
    let hops=if query.operation==Operation::Inspect{0}else if query.operation==Operation::Neighbors{1}else{query.budgets.hops};
    let direction=query.direction.unwrap_or(if query.operation==Operation::Impact{Direction::Incoming}else{Direction::Outgoing});
    for depth in 0..=hops{
     let mut next=BTreeSet::new();for node in frontier{
      if !visited.insert(node.clone()){continue;}
      add(self.filtered_nodes(query,"WHERE n.id=?6 AND ?7=0",&node,0)?)?;charge(1)?;
      if depth==hops{continue;}
      let Some(id)=node.strip_prefix("decl:")else{continue;};
      for incoming in [false,true]{if (incoming&&direction==Direction::Outgoing)||(!incoming&&direction==Direction::Incoming){continue;}
       let condition=if incoming{"target"}else{"caller"};let mut stmt=self.db.prepare(&format!("SELECT occurrence,caller,target FROM edges WHERE {condition}=?1 AND caller IS NOT NULL AND target IS NOT NULL AND state IN('exact','inferred') LIMIT ?2")).map_err(qerr)?;
       for row in stmt.query_map(params![id,query.budgets.work.min(100_000)+1],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?))).map_err(qerr)?{
        charge(1)?;let (occ,caller,target)=row.map_err(qerr)?;add(self.filtered_nodes(query,"WHERE n.id=?6 AND ?7=0",format!("occ:{occ}"),0)?)?;
        next.insert(format!("decl:{}",if incoming{caller}else{target}));if next.len()>100_000{return Err(qerr(quota()));}
       }
      }
     }frontier=next;
    }
   },Operation::Pipeline=>return Err(qerr("unsupported partition pipeline"))
  }
  drop(add);
  let mut selected:BTreeMap<i64,Vec<(String,String,usize)>>=BTreeMap::new();
  for (id,(file,kind,ordinal)) in nodes{selected.entry(file).or_default().push((id,kind,ordinal));}
  let mut resolutions=Vec::new();
  for (id,rows) in selected{
   cancelled()?;let full=self.load(id).map_err(qerr)?;
   charge(full.scopes.len()+full.bindings.len()+full.declarations.len()+full.occurrences.len()+full.imports.len())?;
   let header=files.get_mut(&id).ok_or_else(||qerr("invalid partition file"))?;
   if full.file_id!=header.file_id||full.rel_path!=header.rel_path||full.source_hash!=header.source_hash{return Err(qerr("invalid partition provenance"));}
   let mut occurrence_ids=BTreeSet::new();
   for (node,kind,ordinal) in rows{
    if kind=="declaration"{let d=full.declarations.get(ordinal).ok_or_else(||qerr("invalid declaration ordinal"))?;if node!=format!("decl:{}",d.id){return Err(qerr("invalid declaration index"));}bytes+=serde_json::to_vec(d).map_err(qerr)?.len();header.declarations.push(d.clone());}
    else if kind=="occurrence"{let o=full.occurrences.get(ordinal).ok_or_else(||qerr("invalid occurrence ordinal"))?;if node!=format!("occ:{}",o.id){return Err(qerr("invalid occurrence index"));}bytes+=serde_json::to_vec(o).map_err(qerr)?.len();header.occurrences.push(o.clone());occurrence_ids.insert(o.id.clone());}
   }
   if !occurrence_ids.is_empty(){for r in self.resolutions(id).map_err(qerr)?{if occurrence_ids.contains(&r.occurrence_id){bytes+=serde_json::to_vec(&r).map_err(qerr)?.len();resolutions.push(r);}}}
   if bytes>storage::MAX_DECODED{return Err(qerr(quota()));}
  }
  Ok(QuerySnapshot{receipt:QueryReceipt{workspace_id:scope.workspace_id.clone(),registration_id,source_snapshot_id:source.file_manifest_digest,evidence:receipt},
   scope_key:serde_json::to_string(&(scope.owner.as_str(),binding.owner_subject_id.as_str(),scope.workspace_id.as_str(),binding.location_id.as_str(),binding.workspace_revision,binding.policy_generation,binding.origin.as_str(),binding.subscriber_id.as_str())).unwrap(),
   files:files.into_values().collect(),resolutions,coverage,metadata_bytes:bytes,metadata_records:work.get(),eligible_records:Some(eligible)})
 }
}

impl SqliteStore {
 pub(crate) fn partitioned_query(&self,scope:&GraphScope,binding:&WorkspaceBinding,query:&CodeQuery,cancel:&AtomicBool,secret:&[u8;32])->Result<Option<CodeQueryResult>,QueryError>{
  let before=self.code_read_fence(scope,binding,query.expected_receipt.as_ref())?;
  let captured={let conn=self.conn.lock().unwrap();let receipt=super::code_evidence::current_receipt(&conn,scope,binding,None).map_err(super::code_query::store_error)?;
   if !is_partitioned(&conn,scope,&receipt).map_err(qerr)?{return Ok(None);}
   if receipt.config_fingerprint!=crate::code::producer_fingerprint(){return Err(qerr("reindex_required: partition producer changed"));}
   let (registration,snapshot,coverage):(String,Option<String>,Option<String>)=conn.query_row("SELECT c.registration_id,CASE WHEN length(CAST(r.source_snapshot AS BLOB))<=65536 THEN r.source_snapshot END,CASE WHEN length(CAST(r.coverage AS BLOB))<=67108864 THEN r.coverage END FROM code_index_runs r JOIN code_registrations c ON c.owner=r.owner AND c.workspace_id=r.workspace_id AND c.checkout_id=r.codebase WHERE r.owner=?1 AND r.workspace_id=?2 AND r.codebase=?3 AND r.run_id=?4",params![scope.owner,scope.workspace_id,receipt.checkout_id,receipt.head_generation],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).map_err(qerr)?;
   let source:CodeSourceSnapshot=serde_json::from_str(snapshot.as_deref().ok_or_else(||qerr(quota()))?).map_err(qerr)?;
   let generation=Generation::open_memoized(&conn,scope,&receipt,&source.evidence_cache_identity,&self.code_query_audit).map_err(qerr)?;
   (generation,receipt,registration,snapshot.ok_or_else(||qerr(quota()))?,coverage.ok_or_else(||qerr(quota()))?)
  };
  let (generation,receipt,registration,snapshot,coverage)=captured;
  install_sqlite_work_guard(&generation.db,query.budgets.work.saturating_mul(32));
  let source:CodeSourceSnapshot=serde_json::from_str(&snapshot).map_err(qerr)?;let coverage:Vec<ScanCoverage>=serde_json::from_str(&coverage).map_err(qerr)?;
  generation.verify_stamp(&source.evidence_cache_identity).map_err(qerr)?;
  if coverage.len()>1_000_000{return Err(qerr(quota()));}
  let (policy,identity)=policy_fence(binding,&source).map_err(qerr)?;
   let snapshot=generation.query_snapshot(scope,binding,receipt,registration,source.clone(),coverage,query,cancel)?;
  let result=run_query(&snapshot,query,cancel,secret)?;
  policy.validate(&binding.root,&identity).map_err(qerr)?;
  policy_fence(binding,&source).map_err(qerr)?;
  generation.verify_stamp(&source.evidence_cache_identity).map_err(qerr)?;
  let after=self.code_read_fence(scope,binding,query.expected_receipt.as_ref())?;
  if before!=after{return Err(QueryError{code:"stale_cursor".into(),message:"Code head changed during indexed query".into()});}
  Ok(Some(result))
 }

 pub(crate) fn partitioned_evidence_page(&self,scope:&GraphScope,binding:&WorkspaceBinding,expected:&super::code_evidence::CodeEvidenceReceipt,query:&super::code_evidence::CodeEvidenceQuery)->Result<Option<super::code_evidence::CodeEvidencePage>,super::code_evidence::CodeEvidenceError>{
  use super::code_evidence::*;
  let before=self.code_read_fence(scope,binding,None).map_err(|e|evidence_error(format!("{}: {}",e.code,e.message)))?;
  let captured={let conn=self.conn.lock().unwrap();let receipt=current_receipt(&conn,scope,binding,None)?;
   if !is_partitioned(&conn,scope,&receipt).map_err(evidence_error)?{return Ok(None);}
   if &receipt!=expected{return Err(evidence_error("stale_cursor: evidence head changed"));}
   let (state,count,_)=availability(&conn,scope,&receipt)?;
   if state==CodeEvidenceAvailability::ReindexRequired{return Err(evidence_error("reindex_required: partition producer changed"));}
   let source:Option<String>=conn.query_row("SELECT CASE WHEN length(CAST(source_snapshot AS BLOB))<=65536 THEN source_snapshot END FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND run_id=?4",params![scope.owner,scope.workspace_id,receipt.checkout_id,receipt.head_generation],|r|r.get(0)).map_err(evidence_error)?;
   (Generation::open(&conn,scope,&receipt).map_err(evidence_error)?,receipt,state,count,source.ok_or_else(||evidence_error(quota()))?)
  };
  let (generation,receipt,state,count,source)=captured;let source:CodeSourceSnapshot=serde_json::from_str(&source).map_err(evidence_error)?;
  generation.verify_stamp(&source.evidence_cache_identity).map_err(evidence_error)?;
  let (policy,identity)=policy_fence(binding,&source).map_err(evidence_error)?;
  let fingerprint=blake3::hash(&serde_json::to_vec(&(&scope.owner,&scope.workspace_id,&receipt,query.kind,&query.rel_path,query.max_rows,query.max_bytes)).map_err(evidence_error)?).to_hex().to_string();
  let mut after_file=String::new();let mut after_id=String::new();
  if let Some(cursor)=&query.cursor {let v:serde_json::Value=serde_json::from_str(cursor).map_err(evidence_error)?;
   if v["version"]!=1||v["fingerprint"]!=fingerprint{return Err(evidence_error("stale_cursor: partition continuation differs"));}
   after_file=v["after_file"].as_str().ok_or_else(||evidence_error("invalid cursor"))?.to_owned();after_id=v["after_id"].as_str().ok_or_else(||evidence_error("invalid cursor"))?.to_owned();
  }
  let mut page=CodeEvidencePage{receipt:receipt.clone(),kind:query.kind,availability:state,source_files:count,evidence_files:count,rows:vec![],next_cursor:None,returned_rows:0,returned_bytes:0,partial:false,total_matches:None,current_source_verified:false};
  let mut stmt=generation.db.prepare("SELECT id,path FROM files WHERE path>=?1 AND (?2 IS NULL OR path=?2) ORDER BY path LIMIT 10001").map_err(evidence_error)?;
  let paths=stmt.query_map(params![after_file,query.rel_path],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?))).map_err(evidence_error)?.collect::<rusqlite::Result<Vec<_>>>().map_err(evidence_error)?;drop(stmt);
  let mut more=false;let mut continuation=(after_file.clone(),after_id.clone());
  'files:for (id,path) in paths{
   let file=generation.load(id).map_err(evidence_error)?;
   let rows:Vec<(String,serde_json::Value)>=match query.kind {
    CodeEvidenceKind::Files=>vec![(file.file_id.clone(),serde_json::json!({"file_id":file.file_id,"rel_path":file.rel_path,"source_hash":file.source_hash,"source_generation":receipt.head_generation,"source_len":file.source_len,"format_version":file.format_version,"producer_fingerprint":file.producer_fingerprint,"language":file.language,"capability":file.capability,"public_surface_fingerprint":file.public_surface_fingerprint}))],
    CodeEvidenceKind::Declarations=>file.declarations.iter().map(|d|(d.id.clone(),serde_json::to_value(d).unwrap())).collect(),
    CodeEvidenceKind::Occurrences=>file.occurrences.iter().map(|o|(o.id.clone(),serde_json::to_value(o).unwrap())).collect(),
    CodeEvidenceKind::Resolutions=>generation.resolutions(id).map_err(evidence_error)?.iter().map(|r|(r.occurrence_id.clone(),serde_json::to_value(r).unwrap())).collect(),
   };
   let mut rows=rows;rows.sort_by(|a,b|a.0.cmp(&b.0));
   for (row_id,value) in rows{
    if path==after_file&&row_id<=after_id{continue;}
    if page.rows.len()>=query.max_rows{more=true;break 'files;}
    page.rows.push(serde_json::json!({"rel_path":path,"source_hash":file.source_hash,"source_generation":receipt.head_generation,"evidence":value}));
    let next=serde_json::to_string(&serde_json::json!({"version":1,"fingerprint":fingerprint,"after_file":path,"after_id":row_id})).map_err(evidence_error)?;
    page.next_cursor=Some(next);page.returned_rows=page.rows.len();
    for _ in 0..4{let n=serde_json::to_vec(&page).map_err(evidence_error)?.len();if n==page.returned_bytes{break;}page.returned_bytes=n;}
    if page.returned_bytes>query.max_bytes{
     page.rows.pop();page.returned_rows=page.rows.len();more=true;
     if page.rows.is_empty(){return Err(evidence_error("budget_exceeded: first evidence unit exceeds envelope"));}
     page.next_cursor=Some(serde_json::to_string(&serde_json::json!({"version":1,"fingerprint":fingerprint,"after_file":continuation.0,"after_id":continuation.1})).map_err(evidence_error)?);break 'files;
    }
    continuation=(path.clone(),row_id);
   }
  }
  if !more{page.next_cursor=None;}page.partial=more;
  for _ in 0..8{let n=serde_json::to_vec(&page).map_err(evidence_error)?.len();if n==page.returned_bytes{break;}page.returned_bytes=n;}
  if page.returned_bytes>query.max_bytes{return Err(evidence_error(quota()));}
  policy.validate(&binding.root,&identity).map_err(evidence_error)?;
  policy_fence(binding,&source).map_err(evidence_error)?;
  generation.verify_stamp(&source.evidence_cache_identity).map_err(evidence_error)?;
  if before!=self.code_read_fence(scope,binding,None).map_err(|e|evidence_error(format!("{}: {}",e.code,e.message)))?{return Err(evidence_error("stale_cursor: partition head changed"));}
  Ok(Some(page))
 }
}
