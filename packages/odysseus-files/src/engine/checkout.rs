//! Reviewed checkout's Files/History participant coordinator. Git authority,
//! lock lifetime and metadata transactions remain with the Git utility.
use super::*;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_ITEMS:usize=64;
const MAX_ITEM:usize=256*1024;
const MAX_RAW:usize=4*1024*1024;
const MAX_ENCODED:usize=8*1024*1024-4096;
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields,tag="kind",rename_all="snake_case")]
pub(crate) enum Before {Absent, File{sha256:String,size:u64,object_tag:String,mode:u32}}
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields,tag="kind",rename_all="snake_case")]
pub(crate) enum After {Absent, File{sha256:String,bytes:Vec<u8>,mode:u32}, Directory{mode:u32}}
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Item {relative_path:String,before:Before,after:After}
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Prepare {pub(crate) operation_id:String,request_digest:String,git_plan_digest:String,root_id:String,pub(crate) root_path:PathBuf,root_object_tag:String,items:Vec<Item>}
#[derive(Clone,Debug,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct GitObservation {result:String,index_sha256:String,head_ref:Option<String>,head_oid:String}
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Phase {pub(crate) operation_id:String,request_digest:String,git_plan_digest:String,root_id:String,root_object_tag:String,#[serde(default)] ordinal:Option<usize>,#[serde(default)] verify_applied:bool,#[serde(default)] git_observation:Option<GitObservation>}
fn hash(bytes:&[u8])->String {format!("{:x}",Sha256::digest(bytes))}
fn valid_digest(s:&str)->bool {s.len()==64&&s.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))}
fn relative(s:&str)->Result<&Path,EngineError> {let p=Path::new(s);if s.is_empty()||s.split('/').any(|part|part.is_empty()||part=="."||part=="..")||p.is_absolute()||p.components().any(|x|!matches!(x,Component::Normal(_)))||p.to_str()!=Some(s){return Err(EngineError::Conflict)}Ok(p)}
fn validate_text(bytes:&[u8])->Result<(),EngineError> {if bytes.len()>MAX_ITEM||bytes.contains(&0)||std::str::from_utf8(bytes).is_err(){Err(EngineError::BinaryOrUnsupportedText)}else{Ok(())}}
fn status(record:&ManagedJournalRecord)->serde_json::Value {
    let steps=record.commit_progress.iter().filter(|v|v.get("kind").and_then(serde_json::Value::as_str)==Some("checkout_done")).count();
    let undone=record.directory_events.iter().filter(|v|v.get("kind").and_then(serde_json::Value::as_str)==Some("checkout_rollback_done")).count();
    let count=record.plan["participants"].as_array().map_or(0,Vec::len);
    let rollback_intents=record.directory_events.iter().filter(|v|v["kind"]=="checkout_rollback_intent").count();
    let uncertain=record.commit_progress.len()!=steps*2 || rollback_intents!=undone;
    let compensated=record.directory_events.iter().any(|v|v["kind"]=="checkout_git_observation"&&v["observation"]["result"]=="compensated");
    let state=if uncertain {"recovery_required"} else if record.phase==ManagedJournalPhase::Complete {if compensated {"rolled_back"} else {"complete"}} else if record.phase==ManagedJournalPhase::NeedsReview || record.phase==ManagedJournalPhase::Prepared {"recovery_required"} else if undone>0 || record.directory_events.iter().any(|v|v["kind"]=="checkout_git_observation"&&v["observation"]["result"]=="compensated") {"rollback_in_progress"} else if steps==count&&count>0 {"files_applied"} else {"pending"};
    serde_json::json!({"operation_id":record.operation_id,"status":state,"next_ordinal":steps,"rollback_next":steps.checked_sub(undone+1),"history_pending":record.phase!=ManagedJournalPhase::Complete,"custody":record.directory_events.iter().filter(|v|v["kind"]=="checkout_custody").chain(record.cleanup_progress.iter().filter(|v|v["kind"]=="custody")).collect::<Vec<_>>(),"cleanup":if record.phase==ManagedJournalPhase::Complete {"pending"} else {"not_started"},"potential_custody":if uncertain {record.plan.get("participants").cloned()} else {None}})
}
fn checkout_scope(engine:&FileEngine,scope:&AppScope,path:&Path,root_id:&str)->Result<(),EngineError>{
    if scope.host{return Ok(())}let allowed=engine.registry.authorize_app(scope,path,&BTreeSet::from([Capability::Write]))?;
    if allowed.id!=root_id || !matches!(allowed.kind,RootKind::RecursiveDirectory) {return Err(EngineError::Conflict)}
    if let Some(active)=&scope.active_folder {if active.root_id!=root_id || !component_contains(Path::new(&active.canonical_path),path) || !active.capabilities.contains(&Capability::Write){return Err(EngineError::Conflict)}}Ok(())
}
fn parent_guard(record:&ManagedJournalRecord,item:&serde_json::Value)->Result<(),EngineError>{
    let path=Path::new(item["destination"].as_str().ok_or(EngineError::Conflict)?);let parent=path.parent().ok_or(EngineError::Conflict)?;
    let guard=Path::new(item["parent_guard_path"].as_str().ok_or(EngineError::Conflict)?);let guard_fd=open_managed_directory(record,guard)?;if managed_object_identity(&guard_fd)?!=item["parent_guard_identity"] {return Err(EngineError::Conflict)}
    if let Some(index)=item["parent_created"].as_u64(){let event=record.commit_progress.iter().find(|v|v["kind"]=="checkout_done"&&v["index"].as_u64()==Some(index));
        let rolled=record.directory_events.iter().any(|v|v["kind"]=="checkout_rollback_done"&&v["index"].as_u64()==Some(index));
        match event.filter(|_|!rolled) {Some(v)=>{let fd=open_managed_directory(record,parent)?;if managed_object_identity(&fd)?!=v["identity"] {return Err(EngineError::Conflict)}},None=>{match fs::symlink_metadata(parent){Err(e) if e.kind()==io::ErrorKind::NotFound=>{},_=>return Err(EngineError::Conflict)}}}
    }else{let fd=open_managed_directory(record,parent)?;if managed_object_identity(&fd)?!=item["parent_identity"]{return Err(EngineError::Conflict)}}Ok(())
}
fn created_directory(record:&ManagedJournalRecord,index:usize,path:&Path)->Result<File,EngineError>{
    use std::os::unix::fs::MetadataExt;
    let event=record.commit_progress.iter().find(|v|v["kind"]=="checkout_done"&&v["index"].as_u64()==Some(index as u64)).ok_or(EngineError::Conflict)?;
    let fd=open_managed_directory(record,path)?;if managed_object_identity(&fd)?!=event["identity"] || fd.metadata()?.mode()&0o777!=0o755{return Err(EngineError::Conflict)}Ok(fd)
}
fn directory_empty(fd:&File)->Result<bool,EngineError>{
    use std::os::fd::AsRawFd;
    let path=format!("/proc/self/fd/{}",fd.as_raw_fd());Ok(fs::read_dir(path)?.next().transpose()?.is_none())
}
fn mkdir_checkout(record:&ManagedJournalRecord,path:&Path)->Result<serde_json::Value,EngineError>{
    use std::ffi::CString;use std::os::fd::{AsRawFd,FromRawFd};use std::os::unix::ffi::OsStrExt;
    let parent=path.parent().ok_or(EngineError::Conflict)?;let dir=open_managed_directory(record,parent)?;let name=CString::new(path.file_name().ok_or(EngineError::Conflict)?.as_bytes()).map_err(io::Error::other)?;
    if unsafe{libc::mkdirat(dir.as_raw_fd(),name.as_ptr(),0o700)}!=0{return Err(io::Error::last_os_error().into())}
    let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};if fd<0{return Err(io::Error::last_os_error().into())}let file=unsafe{File::from_raw_fd(fd)};
    if unsafe{libc::fchmod(file.as_raw_fd(),0o755)}!=0{return Err(io::Error::last_os_error().into())}file.sync_all()?;dir.sync_all()?;Ok(managed_object_identity(&file)?)
}

fn authorize_base(engine:&FileEngine,scope:&AppScope,owner:&str,record:&ManagedJournalRecord)->Result<(),EngineError> {
    if record.owner_id!=owner||record.plan["kind"]!="checkout"||record.plan["policy_generation"].as_u64()!=Some(scope.generation) {return Err(EngineError::Conflict)}
    let root=Path::new(record.plan["root_path"].as_str().ok_or(EngineError::Conflict)?);
    let hook=engine.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
    if hook.managed_account_id().map_err(EngineError::HistoryCapture)?!=record.plan["account_id"].as_str().ok_or(EngineError::Conflict)? {return Err(EngineError::Conflict)}
    if hook.managed_authorize_root(root).map_err(EngineError::HistoryCapture)?!=(record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?.to_owned(),root.to_path_buf()){return Err(EngineError::Conflict)}
    let root_fd=open_managed_directory(record,root)?;if managed_object_identity(&root_fd)?!=record.plan["root_identity"]{return Err(EngineError::Conflict)}
    for item in record.plan["participants"].as_array().ok_or(EngineError::Conflict)? {
        let path=Path::new(item["destination"].as_str().ok_or(EngineError::Conflict)?);
        checkout_scope(engine,scope,path,record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?)?;
    } Ok(())
}
fn authorize(engine:&FileEngine,scope:&AppScope,owner:&str,record:&ManagedJournalRecord)->Result<(),EngineError>{authorize_base(engine,scope,owner,record)?;for item in record.plan["participants"].as_array().ok_or(EngineError::Conflict)? {parent_guard(record,item)?;}Ok(())}
fn opened(record:&ManagedJournalRecord,path:&Path)->Result<Option<(File,Vec<u8>,serde_json::Value,u32)>,EngineError> {
    use std::os::unix::fs::MetadataExt;use std::os::unix::ffi::OsStrExt;use std::os::fd::{AsRawFd,FromRawFd};use std::ffi::CString;
    let dir=open_managed_directory(record,path.parent().ok_or(EngineError::Conflict)?)?;
    let name=CString::new(path.file_name().ok_or(EngineError::Conflict)?.as_bytes()).map_err(io::Error::other)?;
    let raw=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_NONBLOCK|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
    let result=if raw<0 {Err(io::Error::last_os_error())} else {Ok(unsafe{File::from_raw_fd(raw)})};
    match result {
        Ok(mut f)=>{let m=f.metadata()?;if !m.is_file()||m.nlink()!=1||m.len()>MAX_ITEM as u64{return Err(EngineError::BinaryOrUnsupportedText)}let id=managed_object_identity(&f)?;let mut bytes=Vec::new();(&mut f).take(MAX_ITEM as u64+1).read_to_end(&mut bytes)?;validate_text(&bytes)?;Ok(Some((f,bytes,id,m.mode()&0o777)))},
        Err(e) if e.kind()==io::ErrorKind::NotFound=>Ok(None),Err(e)=>Err(e.into())
    }
}
fn bind(record:&ManagedJournalRecord,phase:&Phase)->Result<(),EngineError> {
    if record.operation_id!=phase.operation_id||record.request_digest!=phase.request_digest||record.plan["git_plan_digest"]!=phase.git_plan_digest||record.plan["root_id"]!=phase.root_id||record.plan["root_object_tag"]!=phase.root_object_tag {return Err(EngineError::Conflict)}Ok(())
}
impl FileEngine {
    pub(crate) fn checkout_prepare(&self,scope:&AppScope,owner:&str,request:&Prepare)->Result<serde_json::Value,EngineError> {
        use std::os::unix::fs::OpenOptionsExt;
        if request.operation_id.len()<16||request.operation_id.len()>80||!request.operation_id.bytes().all(|c|c.is_ascii_alphanumeric()||c==b'-'||c==b'_')||request.items.is_empty()||request.items.len()>MAX_ITEMS||!valid_digest(&request.request_digest)||!valid_digest(&request.git_plan_digest)||!request.root_path.is_absolute(){return Err(EngineError::Conflict)}
        let value=serde_json::to_value(&request.items).map_err(io::Error::other)?;
        let hook=self.capture_hook.as_ref().ok_or(EngineError::Conflict)?;let account_id=hook.managed_account_id().map_err(EngineError::HistoryCapture)?;
        let material=serde_json::json!(["openclank.git-checkout.files.v1",account_id,request.operation_id,request.root_id,request.root_object_tag,request.git_plan_digest,value]);
        let encoded=serde_json::to_vec(&material).map_err(io::Error::other)?;if encoded.len()>MAX_ENCODED||hash(&encoded)!=request.request_digest {return Err(EngineError::LimitExceeded)}
        let mut raw=0usize;let mut seen=BTreeSet::new();
        for item in &request.items {if let After::Directory{mode}=&item.after {if *mode!=0o755 || !matches!(item.before,Before::Absent){return Err(EngineError::Conflict)}}if matches!((&item.before,&item.after),(Before::Absent,After::Absent)){return Err(EngineError::Conflict)}let path=relative(&item.relative_path)?;if !seen.insert(path.to_path_buf()){return Err(EngineError::Conflict)}if let After::File{sha256,bytes,mode}= &item.after {validate_text(bytes)?;raw=raw.checked_add(bytes.len()).ok_or(EngineError::LimitExceeded)?;if hash(bytes)!=*sha256||!matches!(*mode,0o644|0o755){return Err(EngineError::Conflict)}if let Before::File{mode:old,..}=&item.before {if *old!=*mode{return Err(EngineError::Conflict)}}} }
        if raw>MAX_RAW{return Err(EngineError::LimitExceeded)}
        if hook.managed_authorize_root(&request.root_path).map_err(EngineError::HistoryCapture)?!=(request.root_id.clone(),request.root_path.clone()){return Err(EngineError::Conflict)}
        let rootfd=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&request.root_path)?;let root_identity=managed_object_identity(&rootfd)?;
        let journal_root=self.managed_journal_root.as_ref().ok_or(EngineError::Conflict)?;
        let prior=match self.existing_journal(journal_root,&request.operation_id,false) {Ok(value)=>value,Err(e) if e.kind()==io::ErrorKind::NotFound=>None,Err(e)=>return Err(e.into())};
        if let Some(existing)=prior {if let Some(record)=existing.read()? {
            if record.request_digest!=request.request_digest||record.plan["git_plan_digest"]!=request.git_plan_digest||record.plan["root_id"]!=request.root_id||record.plan["root_path"]!=request.root_path.to_string_lossy().as_ref()||record.plan["root_object_tag"]!=request.root_object_tag {return Err(EngineError::Conflict)}
            authorize(self,scope,owner,&record)?;return Ok(status(&record));
        }}
        if crate::service::ObjectIdentity::from_file(&rootfd)?.opaque_tag()!=request.root_object_tag{return Err(EngineError::Conflict)}
        let action=hook.managed_action_id(&request.operation_id).map_err(EngineError::HistoryCapture)?;let timestamp=SystemTime::now().duration_since(UNIX_EPOCH).map_err(io::Error::other)?.as_millis() as u64;
        let mut record=ManagedJournalRecord{version:1,operation_id:request.operation_id.clone(),owner_id:owner.to_owned(),request_digest:request.request_digest.clone(),phase:ManagedJournalPhase::Prepared,plan:serde_json::json!({"kind":"checkout","account_id":account_id,"root_id":request.root_id,"root_path":request.root_path,"root_identity":root_identity,"root_object_tag":request.root_object_tag,"git_plan_digest":request.git_plan_digest,"policy_generation":scope.generation,"history_action_id":action,"history_timestamp_millis":timestamp,"participants":[]}),file_stages:vec![],directory_events:vec![],commit_progress:vec![],cleanup_progress:vec![],preparation:vec![],registry_plan:None,history_prepare:None,history_completion:None,review_outcome:None};
        let mut targets=Vec::new();let mut participants=Vec::new();let mut before_bytes=Vec::new();let mut directories=Vec::new();let mut created_dirs=std::collections::HashMap::<PathBuf,usize>::new();
        for (i,item) in request.items.iter().enumerate(){let path=request.root_path.join(relative(&item.relative_path)?);checkout_scope(self,scope,&path,&request.root_id)?;
            let parent=path.parent().ok_or(EngineError::Conflict)?;let parent_created=created_dirs.get(parent).copied();let mut guard=parent.to_path_buf();while let Some(_) = created_dirs.get(&guard) {guard=guard.parent().ok_or(EngineError::Conflict)?.to_path_buf();}
            let guardfd=open_managed_directory(&record,&guard).map_err(|_|EngineError::Conflict)?;let parent_identity=if parent_created.is_none(){Some(managed_object_identity(&open_managed_directory(&record,parent).map_err(|_|EngineError::Conflict)?)?)}else{None};
            let current=if parent_created.is_some(){None}else{match &item.after {After::Directory{..}=>{match fs::symlink_metadata(&path) {Err(e) if e.kind()==io::ErrorKind::NotFound=>None,_=>return Err(EngineError::Conflict)}},_=>opened(&record,&path)?}};
            let (before,before_identity)=match (&item.before,current) {(Before::Absent,None)=>(None,None),(Before::File{sha256,size,object_tag,mode},Some((file,bytes,id,m))) if hash(&bytes)==*sha256&&bytes.len() as u64==*size&&crate::service::ObjectIdentity::from_file(&file)?.opaque_tag()==*object_tag&&m==*mode=>(Some(bytes),Some(id)),_=>return Err(EngineError::Conflict)};
            raw=raw.checked_add(before.as_ref().map_or(0,Vec::len)).ok_or(EngineError::LimitExceeded)?;if raw>MAX_RAW{return Err(EngineError::LimitExceeded)}
            let hold=parent.join(format!(".openclank-checkout-{}-{i}.hold",request.operation_id));let rollback=guard.join(format!(".openclank-checkout-{}-{i}.rollback",request.operation_id));
            {for reserved in [&rollback] {match fs::symlink_metadata(reserved) {Err(e) if e.kind()==io::ErrorKind::NotFound=>{},_=>return Err(EngineError::Conflict)}}}if parent_created.is_none(){match fs::symlink_metadata(&hold){Err(e) if e.kind()==io::ErrorKind::NotFound=>{},_=>return Err(EngineError::Conflict)}}
            let (after_digest,mode,directory)=match &item.after {After::Absent=>(None,None,false),After::File{sha256,mode,..}=>(Some(sha256.clone()),Some(*mode),false),After::Directory{mode}=>(None,Some(*mode),true)};
            if directory {created_dirs.insert(path.clone(),i);directories.push(path.clone());}
            participants.push(serde_json::json!({"destination":path,"directory":directory,"parent_identity":parent_identity,"parent_created":parent_created,"parent_guard_path":guard,"parent_guard_identity":managed_object_identity(&guardfd)?,"before_digest":before.as_ref().map(|v|hash(v)),"before_identity":before_identity,"before_mode":match &item.before {Before::File{mode,..}=>Some(mode),_=>None},"replacement_digest":after_digest,"mode":mode,"hold":hold,"rollback_hold":rollback}));targets.push(CaptureTarget{resource_id:path,before:before.clone()});before_bytes.push(before);
        }
        record.plan["participants"]=serde_json::Value::Array(participants);let root=self.managed_journal_root.as_ref().ok_or(EngineError::Conflict)?;let journal=self.operation_journal(root,&request.operation_id)?;
        if journal.read()?.is_some(){return Err(EngineError::Conflict)}journal.write(&record)?;
        for (i,item) in request.items.iter().enumerate(){if let Some(bytes)=&before_bytes[i]{journal.stage(&format!("before-{i}"),bytes)?;}if let After::File{bytes,..}=&item.after {journal.stage(&format!("after-{i}"),bytes)?;if record.plan["participants"][i]["parent_created"].is_null(){journal.stage_destination(&mut record,i,bytes)?;}}}
        authorize(self,scope,owner,&record)?;
        let registrations={let mut checkpoint=journal.preparation_checkpoint(&mut record);if directories.is_empty(){hook.register_recoverable(&targets,&mut checkpoint)}else{hook.register_checkout_targets(&targets,&request.root_id,&request.operation_id,&directories,&mut checkpoint)}.map_err(EngineError::HistoryCapture)?};
        let digest=journal.freeze_registry_plan(&mut record,serde_json::Value::Array(registrations.clone()))?;
        let _ticket={let mut checkpoint=journal.preparation_checkpoint(&mut record);if directories.is_empty(){hook.prepare_recoverable("git_checkout",&targets,&action,timestamp,&digest,&mut checkpoint)}else{hook.prepare_checkout_targets(&targets,&directories,&registrations,&action,timestamp,&digest,&mut checkpoint)}.map_err(EngineError::HistoryCapture)?};
        let mut next=record.clone();next.phase=ManagedJournalPhase::HistoryPrepared;journal.write(&next)?;Ok(status(&next))
    }
}
#[cfg(target_os="linux")]
fn rename_noreplace(record:&ManagedJournalRecord,from:&Path,to:&Path)->io::Result<()> {
    use std::ffi::CString;use std::os::fd::AsRawFd;use std::os::unix::ffi::OsStrExt;
    let parent=from.parent().ok_or_else(||io::Error::other("checkout parent missing"))?;if to.parent()!=Some(parent){return Err(io::Error::other("checkout parent changed"))}
    let fd=open_managed_directory(record,parent)?;let a=CString::new(from.file_name().ok_or_else(||io::Error::other("checkout name missing"))?.as_bytes()).map_err(io::Error::other)?;let b=CString::new(to.file_name().ok_or_else(||io::Error::other("checkout name missing"))?.as_bytes()).map_err(io::Error::other)?;
    if unsafe{libc::renameat2(fd.as_raw_fd(),a.as_ptr(),fd.as_raw_fd(),b.as_ptr(),libc::RENAME_NOREPLACE)}!=0{return Err(io::Error::last_os_error())}fd.sync_all()
}
fn rename_between(record:&ManagedJournalRecord,from:&Path,to:&Path)->io::Result<()> {
    use std::ffi::CString;use std::os::fd::AsRawFd;use std::os::unix::ffi::OsStrExt;
    let a=open_managed_directory(record,from.parent().ok_or_else(||io::Error::other("parent unavailable"))?)?;let b=open_managed_directory(record,to.parent().ok_or_else(||io::Error::other("parent unavailable"))?)?;
    let x=CString::new(from.file_name().ok_or_else(||io::Error::other("name unavailable"))?.as_bytes()).map_err(io::Error::other)?;let y=CString::new(to.file_name().ok_or_else(||io::Error::other("name unavailable"))?.as_bytes()).map_err(io::Error::other)?;
    if unsafe{libc::renameat2(a.as_raw_fd(),x.as_ptr(),b.as_raw_fd(),y.as_ptr(),libc::RENAME_NOREPLACE)}!=0{return Err(io::Error::last_os_error())}a.sync_all()?;b.sync_all()
}
fn prune_created_dir(journal:&ManagedJournal,record:&mut ManagedJournalRecord,index:usize,path:&Path,quarantine:&Path)->Result<(),EngineError>{
    use std::ffi::CString;use std::os::fd::AsRawFd;use std::os::unix::ffi::OsStrExt;
    let original=created_directory(record,index,path)?;let identity=managed_object_identity(&original)?;if !directory_empty(&original)?{return Err(EngineError::Conflict)}
    append_event(journal,record,serde_json::json!({"kind":"checkout_rollback_intent","index":index}))?;rename_noreplace(record,path,quarantine)?;
    let checked=||->Result<(),EngineError>{let fd=open_managed_directory(record,quarantine)?;if managed_object_identity(&fd)?!=identity||!directory_empty(&fd)?{return Err(EngineError::Conflict)}Ok(())};
    if let Err(error)=checked(){let restored=rename_noreplace(record,quarantine,path).is_ok();let _=append_event(journal,record,serde_json::json!({"kind":"checkout_custody","path":path,"custody":quarantine,"restored":restored}));let _=journal.guard_needs_review(record,"partial_needs_review");return Err(error)}
    // Recheck exact inode and emptiness immediately before rmdir. If an entry
    // appears or rmdir fails, restore the exact directory without replacement.
    if let Err(error)=checked(){let restored=rename_noreplace(record,quarantine,path).is_ok();let _=append_event(journal,record,serde_json::json!({"kind":"checkout_custody","path":path,"custody":quarantine,"restored":restored}));let _=journal.guard_needs_review(record,"partial_needs_review");return Err(error)}
    let parent=open_managed_directory(record,path.parent().ok_or(EngineError::Conflict)?)?;let name=CString::new(quarantine.file_name().ok_or(EngineError::Conflict)?.as_bytes()).map_err(io::Error::other)?;
    if unsafe{libc::unlinkat(parent.as_raw_fd(),name.as_ptr(),libc::AT_REMOVEDIR)}!=0 {let e=io::Error::last_os_error();let restored=rename_noreplace(record,quarantine,path).is_ok();let _=append_event(journal,record,serde_json::json!({"kind":"checkout_custody","path":path,"custody":quarantine,"restored":restored}));let _=journal.guard_needs_review(record,"partial_needs_review");return Err(e.into())}parent.sync_all()?;
    append_event(journal,record,serde_json::json!({"kind":"checkout_rollback_done","index":index}))?;Ok(())
}

fn append_event(j:&ManagedJournal,r:&mut ManagedJournalRecord,event:serde_json::Value)->Result<(),EngineError>{let mut next=r.clone();next.directory_events.push(event);j.write(&next)?;*r=next;Ok(())}
// The trusted Host callback has checked Git's terminal ACK; this is evidence,
// not an independently authenticated Git transaction proof.
fn save_git_observation(j:&ManagedJournal,r:&mut ManagedJournalRecord,phase:&Phase,expected:&str)->Result<(),EngineError>{
    let o=phase.git_observation.as_ref().ok_or(EngineError::Conflict)?;
    if phase.verify_applied||o.result!=expected||!valid_digest(&o.index_sha256)||!matches!(o.head_oid.len(),40|64)||!o.head_oid.bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c))||o.head_ref.as_ref().is_some_and(|v|v.is_empty()||v.len()>1024||v.chars().any(char::is_control)){return Err(EngineError::Conflict)}
    let event=serde_json::json!({"kind":"checkout_git_observation","observation":o});
    if let Some(saved)=r.directory_events.iter().find(|v|v["kind"]=="checkout_git_observation") {if saved!=&event{return Err(EngineError::Conflict)}}else{append_event(j,r,event)?;}Ok(())
}
fn verified_current(record:&ManagedJournalRecord,path:&Path,digest:Option<&str>,identity:Option<&serde_json::Value>,mode:Option<u32>)->Result<(),EngineError>{
    match (digest,opened(record,path)?) {(None,None)=>Ok(()),(Some(expected),Some((_,bytes,id,actual_mode))) if hash(&bytes)==expected && identity==Some(&id) && mode==Some(actual_mode)=>Ok(()),_=>Err(EngineError::Conflict)}
}
/// Delete only exact journal-owned private objects after History is complete.
/// A changed object is restored to its original name when possible and is
/// otherwise retained with an explicit custody locator.
fn cleanup_checkout(j:&ManagedJournal,r:&mut ManagedJournalRecord)->Result<bool,EngineError>{
    if r.phase!=ManagedJournalPhase::Complete{return Err(EngineError::Conflict)}
    let parts=r.plan["participants"].as_array().cloned().ok_or(EngineError::Conflict)?;
    let done=r.commit_progress.iter().filter(|v|v["kind"]=="checkout_done").count();
    let rolled=r.directory_events.iter().filter(|v|v["kind"]=="checkout_rollback_done").count();
    let compensated=r.directory_events.iter().any(|v|v["kind"]=="checkout_git_observation"&&v["observation"]["result"]=="compensated");
    let mut objects=Vec::new();
    for (i,p) in parts.iter().enumerate(){
        let stage=r.file_stages.iter().find(|v|v["index"].as_u64()==Some(i as u64));
        if !compensated && i<done && p["before_digest"].is_string() {objects.push((format!("before-{i}"),p["hold"].clone(),p["before_identity"].clone(),p["before_digest"].clone(),p["before_mode"].clone()));}
        if compensated && i<done && p["replacement_digest"].is_string() {let stage=stage.ok_or(EngineError::Conflict)?;objects.push((format!("reverse-{i}"),p["rollback_hold"].clone(),stage["identity"].clone(),p["replacement_digest"].clone(),p["mode"].clone()));}
        if compensated && i>=done && p["replacement_digest"].is_string() {if let Some(stage)=stage {objects.push((format!("stage-{i}"),stage["path"].clone(),stage["identity"].clone(),p["replacement_digest"].clone(),p["mode"].clone()));}}
    }
    for (key,path,id,digest,mode) in objects {
        if r.cleanup_progress.iter().any(|v|v["kind"]=="done"&&v["key"]==key){continue}
        let path=PathBuf::from(path.as_str().ok_or(EngineError::Conflict)?);let parent=path.parent().ok_or(EngineError::Conflict)?;
        let quarantine=parent.join(format!(".openclank-checkout-cleanup-{}-{key}",r.operation_id));
        let intent=serde_json::json!({"kind":"intent","key":key,"path":path,"quarantine":quarantine,"identity":id,"digest":digest,"mode":mode});
        if let Some(saved)=r.cleanup_progress.iter().find(|v|v["kind"]=="intent"&&v["key"]==key){if saved!=&intent{return Ok(false)}} else {
            match opened(r,&quarantine)? {None=>{},Some(_)=>return Ok(false)}
            verified_current(r,&path,digest.as_str(),Some(&id),mode.as_u64().map(|n|n as u32))?;
            let mut next=r.clone();next.cleanup_progress.push(intent);j.write(&next)?;*r=next;
        }
        if opened(r,&quarantine)?.is_none(){if verified_current(r,&path,digest.as_str(),Some(&id),mode.as_u64().map(|n|n as u32)).is_err(){return Ok(false)}if rename_noreplace(r,&path,&quarantine).is_err(){return Ok(false)}}
        if verified_current(r,&quarantine,digest.as_str(),Some(&id),mode.as_u64().map(|n|n as u32)).is_err(){
            let restored=rename_noreplace(r,&quarantine,&path).is_ok();let mut next=r.clone();next.cleanup_progress.push(serde_json::json!({"kind":"custody","key":key,"path":path,"custody":quarantine,"restored":restored}));j.write(&next)?;*r=next;return Ok(false)
        }
        use std::ffi::CString;use std::os::fd::AsRawFd;use std::os::unix::ffi::OsStrExt;
        let dir=open_managed_directory(r,parent)?;let name=CString::new(quarantine.file_name().ok_or(EngineError::Conflict)?.as_bytes()).map_err(io::Error::other)?;
        // Reopen immediately before unlink; changed bytes or identity are never deliberately removed.
        if verified_current(r,&quarantine,digest.as_str(),Some(&id),mode.as_u64().map(|n|n as u32)).is_err(){let restored=rename_noreplace(r,&quarantine,&path).is_ok();let mut next=r.clone();next.cleanup_progress.push(serde_json::json!({"kind":"custody","key":key,"path":path,"custody":quarantine,"restored":restored}));j.write(&next)?;*r=next;return Ok(false)}
        if unsafe{libc::unlinkat(dir.as_raw_fd(),name.as_ptr(),0)}!=0{return Ok(false)}dir.sync_all()?;
        let mut next=r.clone();next.cleanup_progress.push(serde_json::json!({"kind":"done","key":key}));j.write(&next)?;*r=next;
    }Ok(true)
}

impl FileEngine {
    fn checkout_load(&self,scope:&AppScope,owner:&str,phase:&Phase,exclusive:bool)->Result<(ManagedJournal,ManagedJournalRecord),EngineError>{
        let root=self.managed_journal_root.as_ref().ok_or(EngineError::Conflict)?;let journal=self.existing_journal(root,&phase.operation_id,exclusive)?.ok_or(EngineError::Conflict)?;let record=journal.read()?.ok_or(EngineError::Conflict)?;bind(&record,phase)?;authorize(self,scope,owner,&record)?;Ok((journal,record))
    }
    pub(crate) fn checkout_target_matches(&self,scope:&AppScope,owner:&str,phase:&Phase,target:&Path)->Result<(),EngineError>{let root=self.managed_journal_root.as_ref().ok_or(EngineError::Conflict)?;let journal=self.existing_journal(root,&phase.operation_id,false)?.ok_or(EngineError::Conflict)?;let record=journal.read()?.ok_or(EngineError::Conflict)?;bind(&record,phase)?;authorize_base(self,scope,owner,&record)?;if record.plan["root_path"].as_str().map(Path::new)!=Some(target){return Err(EngineError::Conflict)}Ok(())}
    pub(crate) fn checkout_status(&self,scope:&AppScope,owner:&str,phase:&Phase)->Result<serde_json::Value,EngineError>{
        if phase.ordinal.is_some()||phase.git_observation.is_some(){return Err(EngineError::Conflict)}let root=self.managed_journal_root.as_ref().ok_or(EngineError::Conflict)?;let journal=self.existing_journal(root,&phase.operation_id,!phase.verify_applied)?.ok_or(EngineError::Conflict)?;let mut record=journal.read()?.ok_or(EngineError::Conflict)?;bind(&record,phase)?;authorize_base(self,scope,owner,&record)?;if phase.verify_applied {authorize(self,scope,owner,&record)?;}
        if !phase.verify_applied && record.phase==ManagedJournalPhase::HistoryPending && record.directory_events.iter().any(|v|v["kind"]=="checkout_git_observation") {
            let hook=self.capture_hook.as_ref().ok_or(EngineError::Conflict)?;let action=record.plan["history_action_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();
            let remote=hook.recoverable_action(&action,&record.operation_id).map_err(EngineError::HistoryCapture)?;
            if remote.get("Action").and_then(|v|v.get("state")).and_then(serde_json::Value::as_str)==Some("Complete") {
                let saved=record.history_completion.as_ref().ok_or(EngineError::Conflict)?;let completion=saved.get("request").cloned().ok_or(EngineError::Conflict)?;let mut stages=Vec::new();
                for part in saved["stages"].as_array().ok_or(EngineError::Conflict)? {let id=part["id"].as_str().ok_or(EngineError::Conflict)?.to_owned();let label=part["label"].as_str().ok_or(EngineError::Conflict)?;let digest=part["digest"].as_str().ok_or(EngineError::Conflict)?;let fingerprint=part["fingerprint"].as_str().ok_or(EngineError::Conflict)?.to_owned();stages.push((id,journal.read_stage(label,digest)?,fingerprint));}
                hook.complete_recoverable(&action,&completion,&stages,true).map_err(EngineError::HistoryCapture)?;let mut next=record.clone();next.phase=ManagedJournalPhase::Complete;journal.write(&next)?;record=next;
            }
        }
        let mut result=status(&record);
        if phase.verify_applied {if result["status"]!="files_applied" {result["verification"]=serde_json::json!("unknown");return Ok(result)}
            let mut verification="verified";for (i,part) in record.plan["participants"].as_array().ok_or(EngineError::Conflict)?.iter().enumerate(){let path=PathBuf::from(part["destination"].as_str().ok_or(EngineError::Conflict)?);let stage=record.file_stages.iter().find(|v|v["index"].as_u64()==Some(i as u64));let checked=if part["directory"]==true {created_directory(&record,i,&path).map(|_|())} else {verified_current(&record,&path,part["replacement_digest"].as_str(),stage.and_then(|v|v.get("identity")),part["mode"].as_u64().map(|n|n as u32))};match checked {Ok(())=>{},Err(EngineError::Conflict)=>{verification="stale";break},Err(_)=>{verification="unknown";break}}}result["verification"]=serde_json::json!(verification);
        } Ok(result)
    }
    pub(crate) fn checkout_step(&self,scope:&AppScope,owner:&str,phase:&Phase)->Result<serde_json::Value,EngineError>{
        let (journal,mut record)=self.checkout_load(scope,owner,phase,true)?;let index=phase.ordinal.ok_or(EngineError::Conflict)?;
        if phase.verify_applied||phase.git_observation.is_some(){return Err(EngineError::Conflict)}
        if record.phase==ManagedJournalPhase::HistoryPrepared {let mut next=record.clone();next.phase=ManagedJournalPhase::Committing;journal.write(&next)?;record=next;}
        if record.phase!=ManagedJournalPhase::Committing || !record.directory_events.iter().all(|v|v["kind"]!="checkout_rollback_intent") {return Err(EngineError::Conflict)}
        let done=record.commit_progress.iter().filter(|v|v["kind"]=="checkout_done").count();if index!=done||record.commit_progress.len()!=done*2{return Err(EngineError::Conflict)}
        let part=record.plan["participants"].as_array().and_then(|v|v.get(index)).cloned().ok_or(EngineError::Conflict)?;let path=PathBuf::from(part["destination"].as_str().ok_or(EngineError::Conflict)?);let hold=PathBuf::from(part["hold"].as_str().ok_or(EngineError::Conflict)?);
        if part["directory"]==true {
            match fs::symlink_metadata(&path){Err(e) if e.kind()==io::ErrorKind::NotFound=>{},_=>return Err(EngineError::Conflict)}
            let mut next=record.clone();next.commit_progress.push(serde_json::json!({"kind":"checkout_intent","index":index}));journal.write(&next)?;record=next;
            let identity=mkdir_checkout(&record,&path)?;let mut next=record.clone();next.commit_progress.push(serde_json::json!({"kind":"checkout_done","index":index,"identity":identity}));journal.write(&next)?;return Ok(status(&next))
        }
        verified_current(&record,&path,part["before_digest"].as_str(),part.get("before_identity").filter(|v|!v.is_null()),part["before_mode"].as_u64().map(|n|n as u32))?;
        if part["replacement_digest"].is_string(){if !record.file_stages.iter().any(|v|v["index"].as_u64()==Some(index as u64)){let bytes=journal.read_stage(&format!("after-{index}"),part["replacement_digest"].as_str().ok_or(EngineError::Conflict)?)?;journal.stage_destination(&mut record,index,&bytes)?;}journal.verify_destination_stage(&record,index)?}
        let mut next=record.clone();next.commit_progress.push(serde_json::json!({"kind":"checkout_intent","index":index}));journal.write(&next)?;record=next;
        if part["before_digest"].is_string(){rename_noreplace(&record,&path,&hold)?;if let Err(error)=verified_current(&record,&hold,part["before_digest"].as_str(),part.get("before_identity").filter(|v|!v.is_null()),part["before_mode"].as_u64().map(|n|n as u32)) {let restored=rename_noreplace(&record,&hold,&path).is_ok();let _=append_event(&journal,&mut record,serde_json::json!({"kind":"checkout_custody","path":path,"custody":hold,"restored":restored}));let _=journal.guard_needs_review(&mut record,"partial_needs_review");return Err(error)}}
        if part["replacement_digest"].is_string(){let stage=record.file_stages.iter().find(|v|v["index"].as_u64()==Some(index as u64)).ok_or(EngineError::Conflict)?;let stage_path=PathBuf::from(stage["path"].as_str().ok_or(EngineError::Conflict)?);rename_noreplace(&record,&stage_path,&path)?;verified_current(&record,&path,part["replacement_digest"].as_str(),stage.get("identity"),part["mode"].as_u64().map(|n|n as u32))?;}
        let mut next=record.clone();next.commit_progress.push(serde_json::json!({"kind":"checkout_done","index":index}));journal.write(&next)?;Ok(status(&next))
    }
    pub(crate) fn checkout_rollback(&self,scope:&AppScope,owner:&str,phase:&Phase)->Result<serde_json::Value,EngineError>{
        let (journal,mut record)=self.checkout_load(scope,owner,phase,true)?;let index=phase.ordinal.ok_or(EngineError::Conflict)?;
        if record.phase!=ManagedJournalPhase::Committing{return Err(EngineError::Conflict)}let done=record.commit_progress.iter().filter(|v|v["kind"]=="checkout_done").count();if record.commit_progress.len()!=done*2{return Err(EngineError::Conflict)}
        let rollbacks=record.directory_events.iter().filter(|v|v["kind"]=="checkout_rollback_done").count();let intents=record.directory_events.iter().filter(|v|v["kind"]=="checkout_rollback_intent").count();if intents!=rollbacks||done.checked_sub(rollbacks+1)!=Some(index){return Err(EngineError::Conflict)}
        save_git_observation(&journal,&mut record,phase,"compensated")?;
        let part=record.plan["participants"].as_array().and_then(|v|v.get(index)).cloned().ok_or(EngineError::Conflict)?;let path=PathBuf::from(part["destination"].as_str().ok_or(EngineError::Conflict)?);let hold=PathBuf::from(part["hold"].as_str().ok_or(EngineError::Conflict)?);let reverse=PathBuf::from(part["rollback_hold"].as_str().ok_or(EngineError::Conflict)?);
        if part["directory"]==true {let quarantine=path.parent().ok_or(EngineError::Conflict)?.join(format!(".openclank-checkout-prune-{}-{index}",record.operation_id));prune_created_dir(&journal,&mut record,index,&path,&quarantine)?;return Ok(status(&record))}
        let stage_identity=record.file_stages.iter().find(|v|v["index"].as_u64()==Some(index as u64)).and_then(|v|v.get("identity")).cloned();verified_current(&record,&path,part["replacement_digest"].as_str(),stage_identity.as_ref(),part["mode"].as_u64().map(|n|n as u32))?;
        if part["before_digest"].is_string(){verified_current(&record,&hold,part["before_digest"].as_str(),part.get("before_identity").filter(|v|!v.is_null()),part["before_mode"].as_u64().map(|n|n as u32))?}
        append_event(&journal,&mut record,serde_json::json!({"kind":"checkout_rollback_intent","index":index}))?;
        if part["replacement_digest"].is_string(){rename_between(&record,&path,&reverse)?;if let Err(error)=verified_current(&record,&reverse,part["replacement_digest"].as_str(),stage_identity.as_ref(),part["mode"].as_u64().map(|n|n as u32)) {let restored=rename_between(&record,&reverse,&path).is_ok();let _=append_event(&journal,&mut record,serde_json::json!({"kind":"checkout_custody","path":path,"custody":reverse,"restored":restored}));let _=journal.guard_needs_review(&mut record,"partial_needs_review");return Err(error)}}
        if part["before_digest"].is_string(){rename_noreplace(&record,&hold,&path)?;verified_current(&record,&path,part["before_digest"].as_str(),part.get("before_identity").filter(|v|!v.is_null()),part["before_mode"].as_u64().map(|n|n as u32))?}
        append_event(&journal,&mut record,serde_json::json!({"kind":"checkout_rollback_done","index":index}))?;Ok(status(&record))
    }
    pub(crate) fn checkout_finalize(&self,scope:&AppScope,owner:&str,phase:&Phase)->Result<serde_json::Value,EngineError>{
        if phase.ordinal.is_some(){return Err(EngineError::Conflict)}let (journal,mut record)=self.checkout_load(scope,owner,phase,true)?;
        if record.phase==ManagedJournalPhase::Complete {let saved=record.directory_events.iter().find(|v|v["kind"]=="checkout_git_observation").ok_or(EngineError::Conflict)?;if saved["observation"]!=serde_json::to_value(phase.git_observation.as_ref().ok_or(EngineError::Conflict)?).map_err(io::Error::other)? {return Err(EngineError::Conflict)}let clean=cleanup_checkout(&journal,&mut record)?;let mut out=status(&record);out["cleanup"]=serde_json::json!(if clean {"complete"}else{"pending"});return Ok(out)}
        if record.phase==ManagedJournalPhase::HistoryPrepared && record.commit_progress.is_empty() && phase.git_observation.as_ref().is_some_and(|o|o.result=="compensated") {let mut next=record.clone();next.phase=ManagedJournalPhase::Committing;journal.write(&next)?;record=next;}
        let done=record.commit_progress.iter().filter(|v|v["kind"]=="checkout_done").count();let rollback=record.directory_events.iter().filter(|v|v["kind"]=="checkout_rollback_done").count();let items=record.plan["participants"].as_array().cloned().ok_or(EngineError::Conflict)?;
        let zero_compensated=done==0 && phase.git_observation.as_ref().is_some_and(|o|o.result=="compensated");let restoring=rollback>0||zero_compensated;
        let rollback_intents=record.directory_events.iter().filter(|v|v["kind"]=="checkout_rollback_intent").count();if record.phase!=ManagedJournalPhase::Committing || record.commit_progress.len()!=done*2 || rollback_intents!=rollback || !((done==items.len()&&rollback==0)||(done>0&&rollback==done)||zero_compensated){return Err(EngineError::Conflict)}
        save_git_observation(&journal,&mut record,phase,if restoring {"compensated"} else {"committed"})?;
        let mut after=Vec::new();for (i,part) in items.iter().enumerate(){let path=PathBuf::from(part["destination"].as_str().ok_or(EngineError::Conflict)?);
            if part["directory"]==true {if restoring {match fs::symlink_metadata(&path){Err(e) if e.kind()==io::ErrorKind::NotFound=>{},_=>return Err(EngineError::Conflict)}after.push(CaptureAfter{resource_id:path,after:None});}else{let fd=created_directory(&record,i,&path)?;let root_id=record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?;let snapshot=snapshot_managed_folder(&path,&fd,|child|checkout_scope(self,scope,child,root_id))?;after.push(CaptureAfter{resource_id:path,after:Some(snapshot.history)});}continue}
            let digest=if restoring {part["before_digest"].as_str()} else {part["replacement_digest"].as_str()};let identity=if restoring {part.get("before_identity").filter(|v|!v.is_null())}else{record.file_stages.iter().find(|v|v["index"].as_u64()==Some(i as u64)).and_then(|v|v.get("identity"))};if restoring && part["parent_created"].is_u64() {parent_guard(&record,part)?;match fs::symlink_metadata(&path){Err(e) if e.kind()==io::ErrorKind::NotFound=>{},_=>return Err(EngineError::Conflict)}}else{verified_current(&record,&path,digest,identity,if restoring {part["before_mode"].as_u64().map(|n|n as u32)}else{part["mode"].as_u64().map(|n|n as u32)})?;}let bytes=match digest {Some(d)=>Some(journal.read_stage(&format!("{}-{i}",if restoring {"before"}else{"after"}),d)?),None=>None};after.push(CaptureAfter{resource_id:path,after:bytes});}
        let mut next=record.clone();next.phase=ManagedJournalPhase::LiveCommitted;journal.write(&next)?;record=next;
        let hook=self.capture_hook.as_ref().ok_or(EngineError::Conflict)?;let action=record.plan["history_action_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();let prepared=record.history_prepare.as_ref().ok_or(EngineError::Conflict)?;
        let (completion,stages)=hook.plan_recoverable_completion(&action,prepared,&after).map_err(EngineError::HistoryCapture)?;
        let mut descriptors=Vec::new();for (i,(id,bytes,fingerprint)) in stages.iter().enumerate() {let label=format!("history-after-{i}");let digest=journal.stage(&label,bytes)?;descriptors.push(serde_json::json!({"id":id,"label":label,"digest":digest,"fingerprint":fingerprint}));}
        let mut next=record.clone();next.history_completion=Some(serde_json::json!({"request":completion,"stages":descriptors}));next.phase=ManagedJournalPhase::HistoryPending;journal.write(&next)?;record=next;
        let remote=hook.recoverable_action(&action,&record.operation_id).map_err(EngineError::HistoryCapture)?;let state=remote.get("Action").and_then(|x|x.get("state")).and_then(serde_json::Value::as_str);if !matches!(state,Some("BeforeDurable"|"Applied"|"Complete")){return Err(EngineError::Conflict)}
        hook.complete_recoverable(&action,&completion,&stages,state==Some("Complete")).map_err(EngineError::HistoryCapture)?;
        let mut next=record.clone();next.phase=ManagedJournalPhase::Complete;journal.write(&next)?;record=next;let clean=cleanup_checkout(&journal,&mut record)?;let mut out=status(&record);out["cleanup"]=serde_json::json!(if clean {"complete"}else{"pending"});Ok(out)
    }
}

#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Probe { pub root_id:String, #[serde(deserialize_with="required_nullable")] pub parent_object_tag:Option<String>, #[serde(deserialize_with="required_nullable")] pub component:Option<String> }
fn required_nullable<'de,D:serde::Deserializer<'de>>(d:D)->Result<Option<String>,D::Error>{Option::<String>::deserialize(d)}
impl FileEngine {
    pub(crate) fn checkout_probe(&self,scope:&AppScope,parent:&Path,args:&Probe)->Result<serde_json::Value,EngineError>{
        use std::ffi::CString;use std::os::fd::{AsRawFd,FromRawFd};use std::os::unix::ffi::OsStrExt;
        let hook=self.capture_hook.as_ref().ok_or(EngineError::Conflict)?;let (root_id,root)=hook.managed_authorize_root(parent).map_err(EngineError::HistoryCapture)?;if root_id!=args.root_id||!parent.starts_with(&root)||fs::canonicalize(parent)?!=parent{return Err(EngineError::Conflict)}
        if !scope.host {let allowed=self.authorize_app_checked(scope,parent,&BTreeSet::from([Capability::Read]),false)?;if allowed.id!=args.root_id{return Err(EngineError::Conflict)}}
        let parent_file=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(parent)?;let actual=crate::service::ObjectIdentity::from_file(&parent_file)?.opaque_tag();
        if args.parent_object_tag.as_ref().is_some_and(|tag|tag!=&actual){return Err(EngineError::Conflict)}
        let Some(component)=args.component.as_deref() else{return Ok(serde_json::json!({"state":"present","kind":"directory","object_tag":actual}))};
        if args.parent_object_tag.is_none(){return Err(EngineError::Conflict)}let name=relative(component)?;if name.components().count()!=1{return Err(EngineError::Conflict)}
        if !scope.host {let child=parent.join(name);let allowed=self.authorize_app_checked(scope,&child,&BTreeSet::from([Capability::Read]),true)?;if allowed.id!=args.root_id{return Err(EngineError::Conflict)}}
        let c=CString::new(name.as_os_str().as_bytes()).map_err(io::Error::other)?;let mut st:libc::stat=unsafe{std::mem::zeroed()};
        if unsafe{libc::fstatat(parent_file.as_raw_fd(),c.as_ptr(),&mut st,libc::AT_SYMLINK_NOFOLLOW)}!=0 {let e=io::Error::last_os_error();return Ok(serde_json::json!({"state":if e.kind()==io::ErrorKind::NotFound {"absent"} else {"unknown"}}))}
        let kind=match st.st_mode&libc::S_IFMT {libc::S_IFREG=>"file",libc::S_IFDIR=>"directory",libc::S_IFLNK=>"symlink",_=>"other"};
        if kind=="symlink"||kind=="other" {return Ok(serde_json::json!({"state":"present","kind":kind,"object_tag":null}))}
        let fd=unsafe{libc::openat(parent_file.as_raw_fd(),c.as_ptr(),libc::O_RDONLY|libc::O_NONBLOCK|libc::O_NOFOLLOW|libc::O_CLOEXEC)};if fd<0{return Ok(serde_json::json!({"state":"unknown"}))}let file=unsafe{File::from_raw_fd(fd)};let meta=file.metadata()?;use std::os::unix::fs::MetadataExt;
        if meta.dev()!=st.st_dev as u64||meta.ino()!=st.st_ino as u64{return Ok(serde_json::json!({"state":"unknown"}))}
        Ok(serde_json::json!({"state":"present","kind":kind,"object_tag":crate::service::ObjectIdentity::from_file(&file)?.opaque_tag()}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    fn tag(path:&Path)->String {crate::service::ObjectIdentity::from_file(&File::open(path).unwrap()).unwrap().opaque_tag()}
    fn obs(result:&str)->GitObservation {GitObservation{result:result.into(),index_sha256:"a".repeat(64),head_ref:Some("refs/heads/main".into()),head_oid:"b".repeat(40)}}
    fn before(path:&Path)->Before {let bytes=fs::read(path).unwrap();Before::File{sha256:hash(&bytes),size:bytes.len() as u64,object_tag:tag(path),mode:fs::metadata(path).unwrap().permissions().mode()&0o777}}
    fn after(bytes:&[u8])->After {After::File{sha256:hash(bytes),bytes:bytes.to_vec(),mode:0o644}}
    fn request(root:&Path,op:&str,items:Vec<Item>)->Prepare {let root_object_tag=tag(root);let git_plan_digest="c".repeat(64);let material=serde_json::json!(["openclank.git-checkout.files.v1","6d20d6c0-54af-4efe-bd5f-2d1cd3e99201",op,"root",root_object_tag,git_plan_digest,items]);Prepare{operation_id:op.into(),request_digest:hash(&serde_json::to_vec(&material).unwrap()),git_plan_digest,root_id:"root".into(),root_path:root.to_path_buf(),root_object_tag,items}}
    fn phase(req:&Prepare,ordinal:Option<usize>,git_observation:Option<GitObservation>)->Phase {Phase{operation_id:req.operation_id.clone(),request_digest:req.request_digest.clone(),git_plan_digest:req.git_plan_digest.clone(),root_id:req.root_id.clone(),root_object_tag:req.root_object_tag.clone(),ordinal,verify_applied:false,git_observation}}
    #[test]
    #[ignore = "requires isolated History fixture"]
    fn checkout_real_history_complete_and_known_rollback() {
        assert!(relative("a//b").is_err() && relative("a/../b").is_err());
        let root=PathBuf::from(std::env::var_os("CHECKOUT_FIXTURE_ROOT").expect("isolated fixture root"));let journal=PathBuf::from(std::env::var_os("CHECKOUT_FIXTURE_JOURNAL").unwrap());let socket=std::env::var_os("OPENCLANK_HISTORY_SOCKET").unwrap();
        let hook=crate::HistoryServiceHook::new_with_actor_kind(socket,"human:alice","6d20d6c0-54af-4efe-bd5f-2d1cd3e99201","files:root",&root,"fixture-token","human").unwrap();
        let registry:RootRegistry=serde_json::from_value(serde_json::json!({"roots":{"root":{"id":"root","owner_id":"alice","kind":"recursive_directory","canonical_path":root,"display_path":root,"enabled":true,"capabilities":["read","write"],"platform_identity":{"volume_id":null,"file_id":null,"device":null,"inode":null,"case_sensitive":true},"last_validated_unix_ms":null,"availability":"available"}}})).unwrap();
        let engine=FileEngine::new(EngineConfig::default(),registry).require_capture(true).with_managed_journal_root(Some(journal)).with_capture_hook(hook);let scope=AppScope::assigned(["root".into()],[Capability::Read,Capability::Write],1);
        let first=root.join("first.txt");let second=root.join("second.txt");fs::write(&first,b"original").unwrap();fs::set_permissions(&first,fs::Permissions::from_mode(0o644)).unwrap();
        let req=request(&root,"checkout-operation-00001",vec![Item{relative_path:"first.txt".into(),before:before(&first),after:after(b"changed")},Item{relative_path:"second.txt".into(),before:Before::Absent,after:after(b"created")}]);
        assert_eq!(engine.checkout_prepare(&scope,"alice",&req).unwrap()["status"],"pending");let p=phase(&req,Some(0),None);engine.checkout_step(&scope,"alice",&p).unwrap();assert_eq!(engine.checkout_status(&scope,"alice",&phase(&req,None,None)).unwrap()["next_ordinal"],1);
        engine.checkout_step(&scope,"alice",&phase(&req,Some(1),None)).unwrap();let mut check=phase(&req,None,None);check.verify_applied=true;assert_eq!(engine.checkout_status(&scope,"alice",&check).unwrap()["verification"],"verified");
        let result=engine.checkout_finalize(&scope,"alice",&phase(&req,None,Some(obs("committed")))).unwrap();assert_eq!(result["status"],"complete");assert_eq!(result["cleanup"],"complete");assert_eq!(fs::read(&first).unwrap(),b"changed");assert_eq!(fs::read(&second).unwrap(),b"created");
        let req2=request(&root,"checkout-operation-00002",vec![Item{relative_path:"first.txt".into(),before:before(&first),after:after(b"temporary")},Item{relative_path:"second.txt".into(),before:before(&second),after:after(b"later")}]);engine.checkout_prepare(&scope,"alice",&req2).unwrap();engine.checkout_step(&scope,"alice",&phase(&req2,Some(0),None)).unwrap();
        let result=engine.checkout_rollback(&scope,"alice",&phase(&req2,Some(0),Some(obs("compensated")))).unwrap();assert_eq!(result["status"],"rollback_in_progress");let result=engine.checkout_finalize(&scope,"alice",&phase(&req2,None,Some(obs("compensated")))).unwrap();assert_eq!(result["status"],"rolled_back");assert_eq!(result["cleanup"],"complete");assert_eq!(fs::read(&first).unwrap(),b"changed");assert_eq!(fs::read(&second).unwrap(),b"created");
        // An interrupted issued step retains original custody and is status-only.
        let req3=request(&root,"checkout-operation-00003",vec![Item{relative_path:"first.txt".into(),before:before(&first),after:after(b"never-issued")}]);engine.checkout_prepare(&scope,"alice",&req3).unwrap();
        let phase3=phase(&req3,Some(0),None);let (journal,mut rec)=engine.checkout_load(&scope,"alice",&phase3,true).unwrap();let mut next=rec.clone();next.phase=ManagedJournalPhase::Committing;journal.write(&next).unwrap();rec=next;let mut next=rec.clone();next.commit_progress.push(serde_json::json!({"kind":"checkout_intent","index":0}));journal.write(&next).unwrap();rec=next;
        let held=PathBuf::from(rec.plan["participants"][0]["hold"].as_str().unwrap());rename_noreplace(&rec,&first,&held).unwrap();drop(journal);
        let unknown=engine.checkout_status(&scope,"alice",&phase(&req3,None,None)).unwrap();assert_eq!(unknown["status"],"recovery_required");assert!(!unknown["potential_custody"].is_null());assert_eq!(fs::read(&held).unwrap(),b"changed");assert!(engine.checkout_step(&scope,"alice",&phase3).is_err());assert_eq!(fs::read(&held).unwrap(),b"changed");
        std::os::unix::fs::symlink("/does-not-exist",root.join("unrelated-link")).unwrap();
        let parent_tag=tag(&root);let self_probe=Probe{root_id:"root".into(),parent_object_tag:None,component:None};assert_eq!(engine.checkout_probe(&scope,&root,&self_probe).unwrap()["object_tag"],parent_tag);let rejected=Probe{root_id:"root".into(),parent_object_tag:None,component:Some("absent-child".into())};assert!(engine.checkout_probe(&scope,&root,&rejected).is_err());let probe=Probe{root_id:"root".into(),parent_object_tag:Some(parent_tag.clone()),component:Some("absent-child".into())};assert_eq!(engine.checkout_probe(&scope,&root,&probe).unwrap()["state"],"absent");let symlink=Probe{root_id:"root".into(),parent_object_tag:Some(parent_tag.clone()),component:Some("unrelated-link".into())};let observed=engine.checkout_probe(&scope,&root,&symlink).unwrap();assert_eq!(observed["state"],"present");assert_eq!(observed["kind"],"symlink");
        let nested=request(&root,"checkout-operation-00004",vec![Item{relative_path:"nested".into(),before:Before::Absent,after:After::Directory{mode:0o755}},Item{relative_path:"nested/deep".into(),before:Before::Absent,after:After::Directory{mode:0o755}},Item{relative_path:"nested/deep/file.txt".into(),before:Before::Absent,after:after(b"nested content")}]);
        engine.checkout_prepare(&scope,"alice",&nested).unwrap();for i in 0..3 {engine.checkout_step(&scope,"alice",&phase(&nested,Some(i),None)).unwrap();}
        let mut verify=phase(&nested,None,None);verify.verify_applied=true;assert_eq!(engine.checkout_status(&scope,"alice",&verify).unwrap()["verification"],"verified");let out=engine.checkout_finalize(&scope,"alice",&phase(&nested,None,Some(obs("committed")))).unwrap();assert_eq!(out["status"],"complete");assert_eq!(fs::read(root.join("nested/deep/file.txt")).unwrap(),b"nested content");assert!(fs::symlink_metadata(root.join("unrelated-link")).unwrap().is_symlink());
        let nested2=request(&root,"checkout-operation-00005",vec![Item{relative_path:"rollback-dir".into(),before:Before::Absent,after:After::Directory{mode:0o755}},Item{relative_path:"rollback-dir/deep".into(),before:Before::Absent,after:After::Directory{mode:0o755}},Item{relative_path:"rollback-dir/deep/file.txt".into(),before:Before::Absent,after:after(b"temporary")}]);
        engine.checkout_prepare(&scope,"alice",&nested2).unwrap();for i in 0..3 {engine.checkout_step(&scope,"alice",&phase(&nested2,Some(i),None)).unwrap();}for i in (0..3).rev(){engine.checkout_rollback(&scope,"alice",&phase(&nested2,Some(i),Some(obs("compensated")))).unwrap();}let out=engine.checkout_finalize(&scope,"alice",&phase(&nested2,None,Some(obs("compensated")))).unwrap();assert_eq!(out["status"],"rolled_back");assert!(!root.join("rollback-dir").exists());assert!(fs::symlink_metadata(root.join("unrelated-link")).unwrap().is_symlink());
        let untouched=fs::read(&second).unwrap();let zero=request(&root,"checkout-operation-00007",vec![Item{relative_path:"second.txt".into(),before:before(&second),after:after(b"not-issued")}]);engine.checkout_prepare(&scope,"alice",&zero).unwrap();let out=engine.checkout_finalize(&scope,"alice",&phase(&zero,None,Some(obs("compensated")))).unwrap();assert_eq!(out["status"],"rolled_back");assert_eq!(out["cleanup"],"complete");assert_eq!(fs::read(&second).unwrap(),untouched);
        let human=request(&root,"checkout-operation-00006",vec![Item{relative_path:"human-dir".into(),before:Before::Absent,after:After::Directory{mode:0o755}}]);engine.checkout_prepare(&scope,"alice",&human).unwrap();engine.checkout_step(&scope,"alice",&phase(&human,Some(0),None)).unwrap();fs::write(root.join("human-dir/new-human.txt"),b"preserve").unwrap();assert!(engine.checkout_rollback(&scope,"alice",&phase(&human,Some(0),Some(obs("compensated")))).is_err());assert_eq!(fs::read(root.join("human-dir/new-human.txt")).unwrap(),b"preserve");assert_eq!(engine.checkout_status(&scope,"alice",&phase(&human,None,None)).unwrap()["status"],"rollback_in_progress");

    }
}
