//! Journal-owned, pre-publication plain-blob staging. No protocol operation or
//! publication grant is exposed by this module.
use super::*;
use std::collections::{BTreeMap, BTreeSet};
use std::os::fd::{AsRawFd, FromRawFd};
use std::ffi::CString;
use base64::Engine as _;
use std::os::unix::fs::{FileExt, MetadataExt};

const HEADER: u64 = 12 + 4 * 1024 * 1024;
const MAX_TOTAL: u64 = 512 * 1024 * 1024;
const MARKER_RESERVE: u64 = 1024 * 1024;
const MAX_BLOB: u64 = 32 * 1024 * 1024;
const CHUNK: usize = 128 * 1024;
const LABEL: &str = "worktree-tree-v2";

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SourceEntry { pub path: String, pub mode: u32, pub oid: String, pub size: u64 }
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct SourcePlan { commit_oid: String, tree_oid: String, entries: Vec<SourceEntry> }
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct CompletedBlob { path:String, mode:u32, oid:String, size:u64, sha256:String }

fn valid_hex(value:&str,width:usize)->bool {
    value.len()==width && value.bytes().all(|b|b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn valid_path(path:&str)->bool {
    !path.is_empty() && path.len()<=4096 && !path.starts_with('/') && !path.contains('\\') && !path.contains('\0')
        && path.split('/').all(|p|!p.is_empty() && p!="." && p!=".." && !matches!(p,".git"|".clanker"|".clankers"|".references"))
}
fn validate_plan(plan:&SourcePlan)->io::Result<(Vec<String>,u64)> {
    let width=plan.commit_oid.len();
    if !matches!(width,40|64) || !valid_hex(&plan.commit_oid,width) || !valid_hex(&plan.tree_oid,width) {return Err(io::Error::other("worktree source unavailable"));}
    if plan.entries.len()+1>8192 {return Err(io::Error::other("worktree entry bound"));}
    let mut dirs=BTreeSet::new();let mut prior:Option<&str>=None;let mut total=0u64;
    for entry in &plan.entries {
        if !valid_path(&entry.path) || !matches!(entry.mode,0o100644|0o100755) || !valid_hex(&entry.oid,width) || entry.size>MAX_BLOB
            || prior.is_some_and(|last|last.as_bytes()>=entry.path.as_bytes()) {return Err(io::Error::other("worktree source unavailable"));}
        total=total.checked_add(entry.size).filter(|n|*n<=MAX_TOTAL-MARKER_RESERVE).ok_or_else(||io::Error::other("worktree byte bound"))?;
        let components:Vec<_>=entry.path.split('/').collect();for n in 1..components.len(){dirs.insert(components[..n].join("/"));if plan.entries.len()+dirs.len()+1>8192 {return Err(io::Error::other("worktree entry bound"));}}
        prior=Some(&entry.path);
    }
    if plan.entries.len()+dirs.len()+1>8192 || plan.entries.iter().any(|e|dirs.contains(&e.path)) {return Err(io::Error::other("worktree entry bound"));}
    // Fail before allocating payload space if even the exact v2 metadata
    // shapes (using the maximum marker length) exceed its 4 MiB header.
    let mut manifest_entries:BTreeMap<&str,serde_json::Value>=BTreeMap::new();
    for path in &dirs {manifest_entries.insert(path,serde_json::json!({"kind":"directory","path":path,"mode":0o755}));}
    let mut offset=0u64;for entry in &plan.entries {manifest_entries.insert(&entry.path,serde_json::json!({"kind":"file","path":entry.path,"mode":entry.mode&0o777,"offset":offset,"length":entry.size,"sha256":"0".repeat(64)}));offset+=entry.size;}
    let mut entries:Vec<_>=manifest_entries.into_values().collect();entries.push(serde_json::json!({"kind":"git_metadata","path":".git","mode":0o644,"offset":offset,"length":MARKER_RESERVE,"sha256":"0".repeat(64),"owner":"source_worktree"}));
    let header=serde_json::json!({"version":2,"source":{"commit_oid":plan.commit_oid,"tree_oid":plan.tree_oid,"target_manifest_sha256":"0".repeat(64)},"entries":entries});
    if serde_json::to_vec(&header).map_err(io::Error::other)?.len()>4*1024*1024 {return Err(io::Error::other("worktree manifest bound"));}
    Ok((dirs.into_iter().collect(),total))
}
fn target_digest(dirs:&[String],files:&[CompletedBlob])->io::Result<String> {
    let mut items:BTreeMap<&str,(u8,u32,Option<&CompletedBlob>)>=BTreeMap::new();
    for path in dirs {items.insert(path,(b'd',0o755,None));}
    for file in files {if items.insert(&file.path,(b'f',file.mode,Some(file))).is_some(){return Err(io::Error::other("worktree path conflict"));}}
    let mut digest=Sha256::new();digest.update(b"openclank.worktree.target.v2\0");
    for (path,(kind,mode,file)) in items {digest.update([kind]);digest.update(u32::try_from(path.len()).map_err(io::Error::other)?.to_be_bytes());digest.update(path.as_bytes());digest.update(mode.to_be_bytes());if let Some(file)=file {digest.update(file.size.to_be_bytes());digest.update(file.sha256.as_bytes());}}
    Ok(format!("{:x}",digest.finalize()))
}

fn verify_materialized_record(record:&ManagedJournalRecord,expected_dirs:&[String],expected_blobs:&[CompletedBlob],root:&File,deadline:std::time::Instant)->io::Result<()> {
    let current=||if std::time::Instant::now()>=deadline {Err(io::Error::other("worktree verification deadline"))}else{Ok(())};
    current()?;let created=record.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_created").ok_or_else(||io::Error::other("worktree private identity unavailable"))?;
        if created.get("identity")!=Some(&managed_object_identity(root)?) {return Err(io::Error::other("worktree root changed"));}
        let complete=record.preparation.iter().find(|v|v["kind"]=="worktree_materialize_complete").ok_or_else(||io::Error::other("worktree materialization unavailable"))?;
        if complete["target_manifest_sha256"]!=target_digest(expected_dirs,expected_blobs)? {return Err(io::Error::other("worktree target changed"));}
        let parse=|key:&str|->io::Result<BTreeMap<String,serde_json::Value>> {let list=complete[key].as_array().ok_or_else(||io::Error::other("worktree identities unavailable"))?;let mut map=BTreeMap::new();
            for entry in list {let path=entry["path"].as_str().filter(|s|valid_path(s)).ok_or_else(||io::Error::other("worktree identity path unavailable"))?;let identity=entry.get("identity").filter(|v|v.is_object()).ok_or_else(||io::Error::other("worktree identity unavailable"))?;
                if map.insert(path.to_owned(),identity.clone()).is_some(){return Err(io::Error::other("duplicate worktree identity"))}}
            Ok(map)};
        let dirs=parse("directories")?;let files=parse("files")?;if dirs.len()!=expected_dirs.len() || files.len()!=expected_blobs.len() || expected_dirs.iter().any(|path|!dirs.contains_key(path)) || expected_blobs.iter().any(|blob|!files.contains_key(&blob.path)){return Err(io::Error::other("worktree identity inventory changed"));}
        let open_dir=|path:&str|->io::Result<File>{let mut dir=root.try_clone()?;let mut prefix=String::new();if path.is_empty(){return Ok(dir)}
            for component in path.split('/') {if !prefix.is_empty(){prefix.push('/')}prefix.push_str(component);let name=CString::new(component).map_err(io::Error::other)?;let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
                if fd<0{return Err(io::Error::last_os_error())}let next=unsafe{File::from_raw_fd(fd)};if dirs.get(&prefix)!=Some(&managed_object_identity(&next)?) || next.metadata()?.mode()&0o777!=0o755{return Err(io::Error::other("worktree directory changed"))}dir=next;}Ok(dir)};
        let mut expected_children:BTreeMap<String,BTreeSet<std::ffi::OsString>>=BTreeMap::new();expected_children.entry(String::new()).or_default().insert(".git".into());
        for path in dirs.keys().chain(files.keys()){let (parent,name)=path.rsplit_once('/').unwrap_or(("",path.as_str()));expected_children.entry(parent.into()).or_default().insert(name.into());}for path in dirs.keys(){expected_children.entry(path.clone()).or_default();}
        for (path,expected) in expected_children {current()?;let dir=open_dir(&path)?;let mut found=BTreeSet::new();for item in fs::read_dir(format!("/proc/self/fd/{}",dir.as_raw_fd()))? {current()?;if found.len()>=expected.len() || !found.insert(item?.file_name()){return Err(io::Error::other("worktree entries changed"))}}if found!=expected{return Err(io::Error::other("worktree entries changed"))}}
        let mut buffer=vec![0u8;CHUNK];for blob in expected_blobs {current()?;let (parent,name)=blob.path.rsplit_once('/').unwrap_or(("",blob.path.as_str()));let dir=open_dir(parent)?;let name=CString::new(name).map_err(io::Error::other)?;
            let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK)};if fd<0{return Err(io::Error::last_os_error())}let mut file=unsafe{File::from_raw_fd(fd)};let before=file.metadata()?;
            if files.get(&blob.path)!=Some(&managed_object_identity(&file)?) || !before.is_file() || before.nlink()!=1 || before.mode()&0o777!=blob.mode || before.len()!=blob.size{return Err(io::Error::other("worktree file changed"))}
            let mut hasher=Sha256::new();let mut size=0u64;loop {current()?;let n=file.read(&mut buffer)?;if n==0{break}size+=n as u64;if size>blob.size{return Err(io::Error::other("worktree file changed"))}hasher.update(&buffer[..n]);}let after=file.metadata()?;
            let mut named:libc::stat=unsafe{std::mem::zeroed()};if size!=blob.size || format!("{:x}",hasher.finalize())!=blob.sha256 || (before.dev(),before.ino(),before.len(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())!=(after.dev(),after.ino(),after.len(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()) || unsafe{libc::fstatat(dir.as_raw_fd(),name.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=before.dev() || named.st_ino as u64!=before.ino(){return Err(io::Error::other("worktree file changed"))}
        }
        let witness:RegistrationWitness=serde_json::from_value(record.preparation.iter().find(|v|v["kind"]=="worktree_registration_accepted").and_then(|v|v.get("witness")).cloned().ok_or_else(||io::Error::other("worktree marker unavailable"))?).map_err(io::Error::other)?;
        let name=CString::new(".git").map_err(io::Error::other)?;let fd=unsafe{libc::openat(root.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK)};if fd<0{return Err(io::Error::last_os_error())}let mut marker=unsafe{File::from_raw_fd(fd)};let meta=marker.metadata()?;let expected=base64::engine::general_purpose::STANDARD.decode(&witness.marker.bytes_base64).map_err(io::Error::other)?;
        let sealed=record.preparation.iter().find(|v|v["kind"]=="worktree_container_sealed").ok_or_else(||io::Error::other("worktree container unavailable"))?;
        if !meta.is_file() || meta.nlink()!=1 || !matches!(meta.mode()&0o777,0o600|0o644) || sealed["marker_mode"].as_u64()!=Some((meta.mode()&0o777) as u64) || meta.dev()!=canonical_u64(&witness.marker.device)? || meta.ino()!=canonical_u64(&witness.marker.inode)? || meta.len()!=witness.marker.size{return Err(io::Error::other("worktree marker changed"))}
        let mut bytes=Vec::new();(&mut marker).take(4097).read_to_end(&mut bytes)?;let after=marker.metadata()?;let mut named:libc::stat=unsafe{std::mem::zeroed()};
        if bytes!=expected || format!("{:x}",Sha256::digest(&bytes))!=witness.marker.sha256 || (meta.dev(),meta.ino(),meta.len(),meta.mode(),meta.mtime(),meta.mtime_nsec(),meta.ctime(),meta.ctime_nsec())!=(after.dev(),after.ino(),after.len(),after.mode(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()) || unsafe{libc::fstatat(root.as_raw_fd(),name.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=meta.dev() || named.st_ino as u64!=meta.ino(){return Err(io::Error::other("worktree marker changed"))}current()

}

/// The exclusive journal lock and stage FD remain held until this original
/// process stops or hands off to the eventual Git-marker owner. A restarted
/// process cannot reconstruct this writer from a stage filename.
pub(crate) struct WorktreeStageWriter {
    journal:ManagedJournal, record:ManagedJournalRecord, file:File, identity:serde_json::Value,
    plan:SourcePlan, dirs:Vec<String>, index:usize, offset:u64, payload:u64,
    hasher:Sha256, complete:Vec<CompletedBlob>, sealed:bool, failed:bool,
}
/// Held by the original writer; its path is private protocol material, never a
/// user-selected publication target. No automatic cleanup or adoption exists.
pub(crate) struct PrivateWorktreeDirectory { pub path:PathBuf, pub identity:serde_json::Value, pub file:File }
impl WorktreeStageWriter {
    pub(crate) fn is_complete(&self)->bool {self.record.phase==ManagedJournalPhase::Complete && !self.failed}
    pub(super) fn begin(journal:ManagedJournal,owner:&str,request_digest:&str)->io::Result<Self> {
        if !journal.exclusive {return Err(io::Error::other("exclusive journal required"));}
        let mut record=journal.read()?.ok_or_else(||io::Error::other("worktree journal unavailable"))?;
        if record.phase!=ManagedJournalPhase::Prepared || record.owner_id!=owner || record.request_digest!=request_digest
            || record.plan.get("kind").and_then(serde_json::Value::as_str)!=Some("source_worktree_create_v2")
            || record.history_prepare.is_some() || record.preparation.iter().any(|v|v.get("kind").and_then(serde_json::Value::as_str).is_some_and(|s|s.starts_with("worktree_stage_"))) {
            return Err(io::Error::other("worktree journal unavailable"));
        }
        let plan:SourcePlan=serde_json::from_value(record.plan.get("source").cloned().ok_or_else(||io::Error::other("worktree source unavailable"))?).map_err(io::Error::other)?;
        let (dirs,total)=validate_plan(&plan)?;let allocation=HEADER.checked_add(total).and_then(|n|n.checked_add(MARKER_RESERVE)).ok_or_else(||io::Error::other("worktree stage bound"))?;
        // Hold real disk for Native staging; reserve room for History's upload
        // and Lore copy as a conservative admission, not a global reservation.
        let directory=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&journal.directory)?;let mut stat:libc::statvfs=unsafe{std::mem::zeroed()};
        if unsafe{libc::fstatvfs(directory.as_raw_fd(),&mut stat)}!=0 {return Err(io::Error::last_os_error());}
        let available=(stat.f_bavail as u128)*(stat.f_frsize as u128);let required=(allocation as u128)*3+64*1024*1024;
        if available<required {return Err(io::Error::other("worktree staging disk unavailable"));}
        let mut next=record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_stage_intent","label":LABEL,"allocation":allocation}));journal.write(&next)?;record=next;
        let path=journal.staged_path(LABEL)?;let mut options=OpenOptions::new();options.read(true).write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC);
        let file=options.open(path)?;let metadata=file.metadata()?;
        if !metadata.is_file() || metadata.nlink()!=1 {return Err(io::Error::other("worktree stage unavailable"));}
        let result=unsafe{libc::posix_fallocate(file.as_raw_fd(),0,i64::try_from(allocation).map_err(io::Error::other)?)};
        if result!=0 {return Err(io::Error::from_raw_os_error(result));}
        file.sync_all()?;directory.sync_all()?;let identity=managed_object_identity(&file)?;
        let mut next=record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_stage_created","label":LABEL,"identity":identity,"allocation":allocation}));journal.write(&next)?;record=next;
        Ok(Self{journal,record,file,identity,plan,dirs,index:0,offset:0,payload:0,hasher:Sha256::new(),complete:vec![],sealed:false,failed:false})
    }
    pub(crate) fn write_chunk(&mut self,ordinal:usize,offset:u64,bytes:&[u8])->io::Result<()> {
        if self.failed {return Err(io::Error::other("worktree stage unavailable"));}
        let result=self.write_chunk_inner(ordinal,offset,bytes);if result.is_err(){self.failed=true;}result
    }
    fn write_chunk_inner(&mut self,ordinal:usize,offset:u64,bytes:&[u8])->io::Result<()> {
        if self.sealed || bytes.is_empty() || bytes.len()>CHUNK || ordinal!=self.index || offset!=self.offset {return Err(io::Error::other("worktree chunk unavailable"));}
        let entry=self.plan.entries.get(self.index).ok_or_else(||io::Error::other("worktree entry unavailable"))?;
        let next=offset.checked_add(bytes.len() as u64).filter(|n|*n<=entry.size).ok_or_else(||io::Error::other("worktree chunk bound"))?;
        if managed_object_identity(&self.file)?!=self.identity {return Err(io::Error::other("worktree stage changed"));}
        self.file.write_all_at(bytes,HEADER+self.payload+offset)?;self.hasher.update(bytes);self.offset=next;Ok(())
    }
    /// The Source Git owner supplies this digest only after it independently
    /// verified the exact Git object header, byte count and reviewed OID.
    pub(crate) fn finish_blob(&mut self,ordinal:usize,git_oid:&str,source_sha256:&str)->io::Result<()> {
        if self.failed {return Err(io::Error::other("worktree stage unavailable"));}
        let result=self.finish_blob_inner(ordinal,git_oid,source_sha256);if result.is_err(){self.failed=true;}result
    }
    fn finish_blob_inner(&mut self,ordinal:usize,git_oid:&str,source_sha256:&str)->io::Result<()> {
        if self.sealed || ordinal!=self.index {return Err(io::Error::other("worktree entry unavailable"));}
        let entry=self.plan.entries.get(self.index).ok_or_else(||io::Error::other("worktree entry unavailable"))?;
        let digest=format!("{:x}",self.hasher.clone().finalize());
        if self.offset!=entry.size || entry.oid!=git_oid || !valid_hex(source_sha256,64) || digest!=source_sha256 {return Err(io::Error::other("worktree source changed"));}
        self.complete.push(CompletedBlob{path:entry.path.clone(),mode:entry.mode&0o777,oid:entry.oid.clone(),size:entry.size,sha256:digest});
        self.payload=self.payload.checked_add(entry.size).ok_or_else(||io::Error::other("worktree byte bound"))?;self.index+=1;self.offset=0;self.hasher=Sha256::new();Ok(())
    }
    pub(crate) fn seal_target(&mut self)->io::Result<String> {
        if self.failed {return Err(io::Error::other("worktree stage unavailable"));}
        let result=self.seal_target_inner();if result.is_err(){self.failed=true;}result
    }
    fn seal_target_inner(&mut self)->io::Result<String> {
        if self.sealed || self.index!=self.plan.entries.len() || self.offset!=0 || managed_object_identity(&self.file)?!=self.identity {return Err(io::Error::other("worktree target incomplete"));}
        let before=self.file.metadata()?;
        if !before.is_file() || before.nlink()!=1 || before.len()!=HEADER+self.payload+MARKER_RESERVE {return Err(io::Error::other("worktree stage changed"));}
        let mut position=0u64;let mut buffer=vec![0u8;CHUNK];
        for blob in &self.complete {
            let mut hash=Sha256::new();let mut read=0u64;
            while read<blob.size {let n=((blob.size-read).min(CHUNK as u64)) as usize;self.file.read_exact_at(&mut buffer[..n],HEADER+position+read)?;hash.update(&buffer[..n]);read+=n as u64;}
            if format!("{:x}",hash.finalize())!=blob.sha256 {return Err(io::Error::other("worktree stage changed"));}position+=blob.size;
        }
        let after=self.file.metadata()?;
        if (before.dev(),before.ino(),before.len(),before.nlink(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())
            !=(after.dev(),after.ino(),after.len(),after.nlink(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()) {return Err(io::Error::other("worktree stage changed"));}
        self.file.sync_all()?;let digest=target_digest(&self.dirs,&self.complete)?;
        let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_stage_target_sealed","label":LABEL,"identity":self.identity,"payload_bytes":self.payload,"target_manifest_sha256":digest,"directories":self.dirs,"blobs":self.complete}));
        self.journal.write(&next)?;self.record=next;self.sealed=true;Ok(digest)
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Begin {
    pub operation_id:String, pub request_digest:String, pub git_plan_digest:String,
    pub root_id:String, pub root_path:PathBuf, pub root_object_tag:String,
    pub commit_oid:String, pub tree_oid:String, pub entries:Vec<SourceEntry>,
    pub directories:Vec<String>, pub total_bytes:u64,
}
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Binding {
    pub operation_id:String, pub request_digest:String, pub git_plan_digest:String,
    pub root_id:String, pub root_object_tag:String,
}
fn valid_digest(value:&str)->bool {valid_hex(value,64)}
fn validate_root(engine:&FileEngine,scope:&AppScope,root:&Path,root_id:&str)->Result<(),EngineError> {
    if !scope.host {
        let allowed=engine.registry.authorize_app(scope,root,&BTreeSet::from([Capability::Write]))?;
        if allowed.id!=root_id || !matches!(allowed.kind,RootKind::RecursiveDirectory) {return Err(EngineError::Conflict)}
        if let Some(active)=&scope.active_folder {
            if active.root_id!=root_id || !component_contains(Path::new(&active.canonical_path),root) || !active.capabilities.contains(&Capability::Write){return Err(EngineError::Conflict)}
        }
    }
    Ok(())
}
impl FileEngine {
    /// A private operation-owned stage; the caller retains the original
    /// authenticated Files scope and must never recreate an interrupted stage.
    pub(crate) fn worktree_stage_begin(&self,scope:&AppScope,owner:&str,args:&Begin)->Result<WorktreeStageWriter,EngineError> {
        use std::os::unix::fs::OpenOptionsExt;
        if args.operation_id.len()<16 || args.operation_id.len()>80 || !args.operation_id.bytes().all(|b|b.is_ascii_alphanumeric()||b==b'-'||b==b'_')
            || !valid_digest(&args.request_digest) || !valid_digest(&args.git_plan_digest) || !valid_digest(&args.root_object_tag)
            || args.root_id.is_empty() || args.root_id.len()>256 || !args.root_path.is_absolute() || args.root_path.as_os_str().as_encoded_bytes().len()>4096 {return Err(EngineError::Conflict)}
        let plan=SourcePlan{commit_oid:args.commit_oid.clone(),tree_oid:args.tree_oid.clone(),entries:args.entries.clone()};
        let (directories,total)=validate_plan(&plan).map_err(|_|EngineError::Conflict)?;
        if directories!=args.directories || total!=args.total_bytes {return Err(EngineError::Conflict)}
        let rows:Vec<_>=plan.entries.iter().map(|e|serde_json::json!([e.path,e.mode,e.oid,e.size])).collect();
        let material=serde_json::json!(["openclank.git-worktree.plan.v1",plan.commit_oid,plan.tree_oid,rows,directories,total]);
        if format!("{:x}",Sha256::digest(serde_json::to_vec(&material).map_err(io::Error::other)?))!=args.git_plan_digest {return Err(EngineError::Conflict)}
        validate_root(self,scope,&args.root_path,&args.root_id)?;
        let hook=self.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
        let account=hook.managed_account_id().map_err(EngineError::HistoryCapture)?;
        let actor=hook.managed_actor_id().map_err(EngineError::HistoryCapture)?;let action=hook.managed_action_id(&args.operation_id).map_err(EngineError::HistoryCapture)?;
        if account.is_empty() || actor.is_empty() || action.is_empty() {return Err(EngineError::Conflict)}
        let timestamp=SystemTime::now().duration_since(UNIX_EPOCH).map_err(io::Error::other)?.as_millis() as u64;
        if hook.managed_authorize_root(&args.root_path).map_err(EngineError::HistoryCapture)?!=(args.root_id.clone(),args.root_path.clone()) {return Err(EngineError::Conflict)}
        let root=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&args.root_path)?;
        let root_identity=managed_object_identity(&root)?;
        if crate::service::ObjectIdentity::from_file(&root)?.opaque_tag()!=args.root_object_tag {return Err(EngineError::Conflict)}
        let journal=self.operation_journal(self.managed_journal_root.as_ref().ok_or(EngineError::Conflict)?,&args.operation_id)?;
        if journal.read()?.is_some(){return Err(EngineError::Conflict)}
        let record=ManagedJournalRecord{version:1,operation_id:args.operation_id.clone(),owner_id:owner.into(),request_digest:args.request_digest.clone(),phase:ManagedJournalPhase::Prepared,
            plan:serde_json::json!({"kind":"source_worktree_create_v2","account_id":account,"history_account_id":account,"history_actor_id":actor,"history_action_id":action,"history_timestamp_millis":timestamp,"root_id":args.root_id,"root_path":args.root_path,"root_identity":root_identity,"root_object_tag":args.root_object_tag,"policy_generation":scope.generation,"git_plan_digest":args.git_plan_digest,"source":plan}),
            file_stages:vec![],directory_events:vec![],commit_progress:vec![],cleanup_progress:vec![],preparation:vec![],registry_plan:None,history_prepare:None,history_completion:None,review_outcome:None};
        journal.write(&record)?;
        WorktreeStageWriter::begin(journal,owner,&args.request_digest).map_err(EngineError::from)
    }
}
/// Original journal locks and newly created inverse journal remain held by the
/// same private operation. No filesystem effect is authorized by construction.
#[allow(dead_code)]
pub(crate) struct WorktreeInverse {
    original: ManagedJournal,
    journal: ManagedJournal,
    record: ManagedJournalRecord,
    stage: Option<File>,
    quarantine: Option<File>,
    git_admin: Option<File>,
}

/// A fresh operation owns the completed inverse and original journals until
/// its exact History reservation and both no-replace publications settle.
#[allow(dead_code)]
pub(crate) struct WorktreeRedo {
    inverse: ManagedJournal,
    original: ManagedJournal,
    journal: ManagedJournal,
    record: ManagedJournalRecord,
    stage: Option<File>,
    quarantine: Option<File>,
    git_admin: Option<File>,
}

#[allow(dead_code)]
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RedoBegin {
    pub operation_id:String, pub request_digest:String, pub inverse_operation_id:String,
    pub inverse_version_id:String, pub git_plan_digest:String, pub root_id:String,
    pub root_path:PathBuf, pub root_object_tag:String,
}

impl FileEngine {
    /// No live effect is issued here. All facts are derived from the held
    /// completed inverse and original journals, not a request-supplied path.
    #[allow(dead_code)]
    pub(crate) fn worktree_redo_begin(&self,scope:&AppScope,owner:&str,args:&RedoBegin)->Result<WorktreeRedo,EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        let valid_id=|s:&str|(16..=80).contains(&s.len()) && s.bytes().all(|b|b.is_ascii_alphanumeric() || b==b'-' || b==b'_');
        if !valid_id(&args.operation_id) || !valid_id(&args.inverse_operation_id) || args.operation_id==args.inverse_operation_id
            || !valid_digest(&args.request_digest) || !valid_digest(&args.git_plan_digest) || !valid_digest(&args.root_object_tag)
            || args.inverse_version_id.is_empty() || args.inverse_version_id.len()>256 || args.inverse_version_id.bytes().any(|b|b.is_ascii_control())
            || args.root_id.is_empty() || args.root_id.len()>256 || !args.root_path.is_absolute() || args.root_path.as_os_str().as_encoded_bytes().len()>4096 {return Err(EngineError::Conflict)}
        validate_root(self,scope,&args.root_path,&args.root_id)?;
        let hook=self.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
        let account=hook.managed_account_id().map_err(EngineError::HistoryCapture)?;let actor=hook.managed_actor_id().map_err(EngineError::HistoryCapture)?;
        let action=hook.managed_action_id(&args.operation_id).map_err(EngineError::HistoryCapture)?;
        if account.is_empty() || actor.is_empty() || action.is_empty() || hook.managed_authorize_root(&args.root_path).map_err(EngineError::HistoryCapture)?!=(args.root_id.clone(),args.root_path.clone()) {return Err(EngineError::Conflict)}
        let root=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&args.root_path)?;
        if crate::service::ObjectIdentity::from_file(&root)?.opaque_tag()!=args.root_object_tag {return Err(EngineError::Conflict)}
        let root_identity=managed_object_identity(&root)?;let journal_root=self.managed_journal_root.as_ref().ok_or(EngineError::Conflict)?;
        let inverse=self.existing_journal_nowait(journal_root,&args.inverse_operation_id)?.ok_or(EngineError::Conflict)?;
        let undo=inverse.read()?.ok_or(EngineError::Conflict)?;
        if undo.owner_id!=owner || undo.phase!=ManagedJournalPhase::Complete || undo.plan["kind"]!="source_worktree_inverse_v2"
            || undo.plan["history_account_id"]!=account || undo.plan["root_id"]!=args.root_id || undo.plan["root_path"]!=serde_json::json!(args.root_path)
            || undo.plan["root_identity"]!=root_identity || undo.plan["git_plan_digest"]!=args.git_plan_digest
            || undo.history_prepare.as_ref().and_then(|v|v.pointer("/before/version_id")).and_then(serde_json::Value::as_str)!=Some(args.inverse_version_id.as_str())
            || undo.history_completion.is_none() || !undo.commit_progress.iter().any(|v|v["kind"]=="worktree_inverse_quarantine_done")
            || !undo.commit_progress.iter().any(|v|v["kind"]=="worktree_inverse_admin_done") {return Err(EngineError::Conflict)}
        let original_id=undo.plan["inverse_of"].as_str().filter(|v|valid_id(v) && *v!=args.operation_id).ok_or(EngineError::Conflict)?;
        let original=self.existing_journal_nowait(journal_root,original_id)?.ok_or(EngineError::Conflict)?;let forward=original.read()?.ok_or(EngineError::Conflict)?;
        if forward.owner_id!=owner || forward.phase!=ManagedJournalPhase::Complete || forward.plan["kind"]!="source_worktree_create_v2"
            || forward.plan["history_account_id"]!=account || forward.plan["root_identity"]!=root_identity || forward.plan["git_plan_digest"]!=args.git_plan_digest {return Err(EngineError::Conflict)}
        let inverse_action=undo.plan["history_action_id"].as_str().filter(|v|!v.is_empty()).ok_or(EngineError::Conflict)?;
        let sha=undo.plan["original_container_sha256"].as_str().filter(|v|valid_digest(v)).ok_or(EngineError::Conflict)?;
        let length=undo.plan["original_container_length"].as_u64().filter(|v|*v>0 && *v<=HEADER+MAX_TOTAL).ok_or(EngineError::Conflict)?;
        let journal=self.operation_journal(journal_root,&args.operation_id)?;if journal.read()?.is_some(){return Err(EngineError::Conflict)}
        let record=ManagedJournalRecord{version:1,operation_id:args.operation_id.clone(),owner_id:owner.into(),request_digest:args.request_digest.clone(),phase:ManagedJournalPhase::Prepared,
            plan:serde_json::json!({"kind":"source_worktree_redo_v2","inverse_of":args.inverse_operation_id,"inverse_history_action_id":inverse_action,"inverse_version_id":args.inverse_version_id,
                "container_sha256":sha,"container_length":length,"original_operation_id":original_id,"history_account_id":account,"history_actor_id":actor,"history_action_id":action,
                "git_plan_digest":args.git_plan_digest,"root_id":args.root_id,"root_path":args.root_path,"root_identity":root_identity,"root_object_tag":args.root_object_tag,"policy_generation":scope.generation}),
            file_stages:vec![],directory_events:vec![],commit_progress:vec![],cleanup_progress:vec![],preparation:vec![],registry_plan:None,history_prepare:None,history_completion:None,review_outcome:None};
        journal.write(&record)?;Ok(WorktreeRedo{inverse,original,journal,record,stage:None,quarantine:None,git_admin:None})
    }
}

impl WorktreeRedo {
    pub(crate) fn check_binding(&self,engine:&FileEngine,scope:&AppScope,owner:&str,binding:&Binding,target:&Path)->Result<(),EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        if self.record.owner_id!=owner || self.record.operation_id!=binding.operation_id || self.record.request_digest!=binding.request_digest
            || self.record.plan["git_plan_digest"]!=binding.git_plan_digest || self.record.plan["root_id"]!=binding.root_id
            || self.record.plan["root_object_tag"]!=binding.root_object_tag || self.record.plan["root_path"]!=serde_json::json!(target)
            || self.record.plan["policy_generation"].as_u64()!=Some(scope.generation){return Err(EngineError::Conflict)}
        validate_root(engine,scope,target,&binding.root_id)?;let root=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(target)?;
        if self.record.plan["root_identity"]!=managed_object_identity(&root)? {return Err(EngineError::Conflict)}
        if self.record.commit_progress.iter().any(|v|v["kind"]=="worktree_redo_tree_done") {
            let tree=self.quarantine.as_ref().ok_or(EngineError::Conflict)?;let done=self.record.commit_progress.iter().find(|v|v["kind"]=="worktree_redo_tree_done").ok_or(EngineError::Conflict)?;
            if done.get("identity")!=Some(&managed_object_identity(tree)?) {return Err(EngineError::Conflict)}
            let forward=self.original.read()?.ok_or(EngineError::Conflict)?;let intent=forward.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;
            let parent_path=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);engine.authorize_stage_directory_scoped(scope,&parent_path)?;
            let parent=open_managed_directory(&forward,&parent_path)?;if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&parent)?) {return Err(EngineError::Conflict)}
            let name=CString::new(intent["destination_name"].as_str().ok_or(EngineError::Conflict)?.as_bytes()).map_err(io::Error::other)?;let mut named:libc::stat=unsafe{std::mem::zeroed()};let meta=tree.metadata()?;
            if unsafe{libc::fstatat(parent.as_raw_fd(),name.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || (named.st_dev as u64,named.st_ino as u64)!=(meta.dev(),meta.ino()){return Err(EngineError::Conflict)}
        } else if self.record.plan["root_object_tag"]!=crate::service::ObjectIdentity::from_file(&root)?.opaque_tag(){return Err(EngineError::Conflict)}
        Ok(())
    }
    pub(crate) fn history_state(&self)->&'static str {
        if self.record.phase==ManagedJournalPhase::Complete {"completed"}
        else if self.record.commit_progress.last().is_some_and(|v|v["kind"]=="worktree_redo_tree_done") {"tree_restored"}
        else if self.record.commit_progress.last().is_some_and(|v|v["kind"]=="worktree_redo_admin_done") {"admin_restored"}
        else if self.record.phase==ManagedJournalPhase::HistoryPrepared {"history_prepared"}
        else if self.record.phase==ManagedJournalPhase::Prepared && self.record.preparation.is_empty() {"pending"}
        else {"unknown"}
    }
    /// Project only paths derived from the two locked completed journals to
    /// the retained private Source child; neither is a caller-selected path.
    #[allow(dead_code)]
    pub(crate) fn source_evidence(&self)->Result<serde_json::Value,EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        let undo=self.inverse.read()?.ok_or(EngineError::Conflict)?;let forward=self.original.read()?.ok_or(EngineError::Conflict)?;
        if undo.phase!=ManagedJournalPhase::Complete || forward.phase!=ManagedJournalPhase::Complete || undo.owner_id!=self.record.owner_id || forward.owner_id!=self.record.owner_id {return Err(EngineError::Conflict)}
        let q=undo.commit_progress.iter().find(|v|v["kind"]=="worktree_inverse_quarantine_done").ok_or(EngineError::Conflict)?;
        let d=undo.commit_progress.iter().find(|v|v["kind"]=="worktree_inverse_admin_done").ok_or(EngineError::Conflict)?;
        let qlabel=format!("{}.worktree-undo",self.record.plan["inverse_of"].as_str().ok_or(EngineError::Conflict)?);
        let dlabel=format!("{}.worktree-git-admin",self.record.plan["inverse_of"].as_str().ok_or(EngineError::Conflict)?);
        if q["label"]!=qlabel || d["label"]!=dlabel {return Err(EngineError::Conflict)}
        let qpath=self.journal.directory.join(&qlabel);let dpath=self.journal.directory.join(&dlabel);
        let open=|p:&Path|OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(p);
        let tree=open(&qpath)?;let admin=open(&dpath)?;if q.get("identity")!=Some(&managed_object_identity(&tree)?) {return Err(EngineError::Conflict)}
        let admin_id:PrivateIdentity=serde_json::from_value(d["identity"].clone()).map_err(io::Error::other)?;let am=admin.metadata()?;
        if (am.dev(),am.ino())!=check_private_identity(&admin_id)? || fs::canonicalize(&qpath)?!=qpath || fs::canonicalize(&dpath)?!=dpath {return Err(EngineError::Conflict)}
        let mut registration=forward.preparation.iter().find(|v|v["kind"]=="worktree_registration_accepted").and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?;
        let parsed:RegistrationWitness=serde_json::from_value(registration.clone()).map_err(io::Error::other)?;
        if parsed.admin_identity!=admin_id {return Err(EngineError::Conflict)}
        let index:PrivateFileWitness=serde_json::from_value(forward.preparation.iter().find(|v|v["kind"]=="worktree_index_accepted").and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
        registration.as_object_mut().ok_or(EngineError::Conflict)?.insert("index".into(),serde_json::to_value(index).map_err(io::Error::other)?);
        let final_witness:BacklinkCommittedWitness=serde_json::from_value(forward.commit_progress.iter().find(|v|v["kind"]=="worktree_backlink_committed").and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
        let intent=forward.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;
        let parent=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);let name=intent["destination_name"].as_str().ok_or(EngineError::Conflict)?;
        let root=PathBuf::from(self.record.plan["root_path"].as_str().ok_or(EngineError::Conflict)?);
        if !parent.is_absolute() || !parent.starts_with(root) || name.is_empty() || name=="." || name==".." || name.len()>255 || name.bytes().any(|v|v<32 || v==127 || v==b'/' || v==b'\\') {return Err(EngineError::Conflict)}
        let destination=parent.join(name);let destination=destination.to_str().ok_or(EngineError::Conflict)?;let text=format!("{}/.git\n",destination);
        if text.len() as u64!=final_witness.backlink.size || format!("{:x}",Sha256::digest(text.as_bytes()))!=final_witness.backlink.sha256 {return Err(EngineError::Conflict)}
        let source:SourcePlan=serde_json::from_value(forward.plan["source"].clone()).map_err(io::Error::other)?;
        Ok(serde_json::json!({"registration":registration,"finalBacklink":final_witness.backlink,"targetOID":source.commit_oid,"treeOID":source.tree_oid,"destinationPath":destination,"privateTreePath":qpath,"privateAdminPath":dpath}))
    }
    fn verified_stage_and_tree(&self,tree:&File,deadline:std::time::Instant)->Result<ManagedJournalRecord,EngineError>{
        let forward=self.original.read()?.ok_or(EngineError::Conflict)?;let source:SourcePlan=serde_json::from_value(forward.plan["source"].clone()).map_err(io::Error::other)?;
        let (dirs,total)=validate_plan(&source).map_err(EngineError::from)?;let sealed=forward.preparation.iter().find(|v|v["kind"]=="worktree_stage_target_sealed").ok_or(EngineError::Conflict)?;
        let saved:Vec<String>=serde_json::from_value(sealed["directories"].clone()).map_err(io::Error::other)?;let blobs:Vec<CompletedBlob>=serde_json::from_value(sealed["blobs"].clone()).map_err(io::Error::other)?;
        if forward.phase!=ManagedJournalPhase::Complete || forward.owner_id!=self.record.owner_id || dirs!=saved || blobs.len()!=source.entries.len()
            || sealed["payload_bytes"].as_u64()!=Some(total) || source.entries.iter().zip(&blobs).any(|(a,b)|a.path!=b.path || a.oid!=b.oid || a.size!=b.size || a.mode&0o777!=b.mode || !valid_digest(&b.sha256))
            || sealed["target_manifest_sha256"]!=target_digest(&dirs,&blobs)? {return Err(EngineError::Conflict)}
        let stage=self.stage.as_ref().ok_or(EngineError::Conflict)?;let staged=self.record.preparation.iter().find(|v|v["kind"]=="worktree_redo_history_complete").ok_or(EngineError::Conflict)?;
        let length=self.record.plan["container_length"].as_u64().ok_or(EngineError::Conflict)?;let before=stage.metadata()?;
        if !before.is_file() || before.nlink()!=1 || before.len()!=length || staged.get("identity")!=Some(&managed_object_identity(stage)?) {return Err(EngineError::Conflict)}
        let mut hash=Sha256::new();let mut offset=0u64;let mut buf=vec![0u8;CHUNK];while offset<length {if std::time::Instant::now()>=deadline{return Err(EngineError::Conflict)}let n=((length-offset).min(CHUNK as u64)) as usize;stage.read_exact_at(&mut buf[..n],offset)?;hash.update(&buf[..n]);offset+=n as u64;}
        let after=stage.metadata()?;if self.record.plan["container_sha256"]!=format!("{:x}",hash.finalize()) || (before.dev(),before.ino(),before.len(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())!=(after.dev(),after.ino(),after.len(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()){return Err(EngineError::Conflict)}
        verify_materialized_record(&forward,&dirs,&blobs,tree,deadline)?;Ok(forward)
    }

    /// Move only the journal-owned admin inode into its original absent name.
    /// The retained Source child owns the exact Git locks in `witness`.
    #[allow(dead_code)]
    pub(crate) fn restore_admin(&mut self,engine:&FileEngine,scope:&AppScope,witness:&InverseGitAdminWitness,deadline:std::time::Instant)->Result<(),EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        if self.record.phase!=ManagedJournalPhase::HistoryPrepared || self.git_admin.is_some() || std::time::Instant::now()>=deadline{return Err(EngineError::Conflict)}
        let evidence=self.source_evidence()?;let tree_path=PathBuf::from(evidence["privateTreePath"].as_str().ok_or(EngineError::Conflict)?);let admin_path=PathBuf::from(evidence["privateAdminPath"].as_str().ok_or(EngineError::Conflict)?);
        let open=|p:&Path|OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(p);
        let tree=open(&tree_path)?;let admin=open(&admin_path)?;let forward=self.verified_stage_and_tree(&tree,deadline)?;
        let registration:RegistrationWitness=serde_json::from_value(evidence["registration"].as_object().map(|v|{let mut x=v.clone();x.remove("index");serde_json::Value::Object(x)}).ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
        let committed:PrivateFileWitness=serde_json::from_value(evidence["finalBacklink"].clone()).map_err(io::Error::other)?;let source:SourcePlan=serde_json::from_value(forward.plan["source"].clone()).map_err(io::Error::other)?;
        if witness.admin_name!=registration.admin_name || witness.admin_identity!=registration.admin_identity || witness.backlink!=committed || witness.target_oid!=source.commit_oid || witness.tree_oid!=source.tree_oid {return Err(EngineError::Conflict)}
        for id in [&witness.common_git_identity,&witness.worktrees_identity,&witness.admin_identity,&witness.locks.head,&witness.locks.backlink,&witness.locks.index] {check_private_identity(id)?;}
        check_private_file(&witness.head)?;check_private_file(&witness.backlink)?;if witness.index.size==0 || witness.index.size>8*1024*1024 || !valid_digest(&witness.index.sha256){return Err(EngineError::Conflict)}
        let marker=base64::engine::general_purpose::STANDARD.decode(&registration.marker.bytes_base64).map_err(io::Error::other)?;let marker=std::str::from_utf8(&marker).map_err(io::Error::other)?;
        let original=PathBuf::from(marker.strip_prefix("gitdir: ").and_then(|v|v.strip_suffix('\n')).filter(|v|!v.chars().any(char::is_control)).ok_or(EngineError::Conflict)?);
        let worktrees_path=original.parent().ok_or(EngineError::Conflict)?;let common_path=worktrees_path.parent().ok_or(EngineError::Conflict)?;
        if !original.is_absolute() || original.file_name().and_then(|v|v.to_str())!=Some(witness.admin_name.as_str()) || worktrees_path.file_name().and_then(|v|v.to_str())!=Some("worktrees")
            || common_path.file_name().and_then(|v|v.to_str())!=Some(".git") || fs::canonicalize(worktrees_path)?!=worktrees_path {return Err(EngineError::Conflict)}
        let common=open(common_path)?;let worktrees=open(worktrees_path)?;let pair=|f:&File|->io::Result<(u64,u64)>{let m=f.metadata()?;Ok((m.dev(),m.ino()))};
        if pair(&common)?!=check_private_identity(&witness.common_git_identity)? || pair(&worktrees)?!=check_private_identity(&witness.worktrees_identity)? || pair(&admin)?!=check_private_identity(&witness.admin_identity)? {return Err(EngineError::Conflict)}
        let head=format!("{}\n",source.commit_oid);let destination=evidence["destinationPath"].as_str().ok_or(EngineError::Conflict)?;let backlink=format!("{destination}/.git\n");
        if witness.head.size!=head.len() as u64 || witness.head.sha256!=format!("{:x}",Sha256::digest(head.as_bytes())) || witness.backlink.size!=backlink.len() as u64 || witness.backlink.sha256!=format!("{:x}",Sha256::digest(backlink.as_bytes())) {return Err(EngineError::Conflict)}
        for (name,id) in [("HEAD.lock",&witness.locks.head),("gitdir.lock",&witness.locks.backlink),("index.lock",&witness.locks.index)] {verify_inverse_admin_lock(&admin,name,id)?;}
        verify_inverse_admin_file(&admin,"HEAD",&witness.head,4096,deadline)?;verify_inverse_admin_file(&admin,"gitdir",&witness.backlink,4096,deadline)?;verify_inverse_admin_file(&admin,"index",&witness.index,8*1024*1024,deadline)?;
        let root_path=PathBuf::from(self.record.plan["root_path"].as_str().ok_or(EngineError::Conflict)?);let root_id=self.record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();validate_root(engine,scope,&root_path,&root_id)?;
        let intent=forward.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;let parent_path=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);
        engine.authorize_stage_directory_scoped(scope,&parent_path)?;let parent=open_managed_directory(&forward,&parent_path)?;if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&parent)?) {return Err(EngineError::Conflict)}
        let target=CString::new(intent["destination_name"].as_str().ok_or(EngineError::Conflict)?.as_bytes()).map_err(io::Error::other)?;let name=CString::new(witness.admin_name.as_bytes()).map_err(io::Error::other)?;
        let journal=open(&self.journal.directory)?;let label=CString::new(admin_path.file_name().ok_or(EngineError::Conflict)?.as_encoded_bytes()).map_err(io::Error::other)?;let mut stat:libc::stat=unsafe{std::mem::zeroed()};
        let absent=|dir:&File,name:&CString,st:&mut libc::stat| unsafe{libc::fstatat(dir.as_raw_fd(),name.as_ptr(),st,libc::AT_SYMLINK_NOFOLLOW)}!=0 && io::Error::last_os_error().raw_os_error()==Some(libc::ENOENT);
        if !absent(&parent,&target,&mut stat) || !absent(&worktrees,&name,&mut stat) || journal.metadata()?.dev()!=worktrees.metadata()?.dev(){return Err(EngineError::Conflict)}
        let mut next=self.record.clone();next.phase=ManagedJournalPhase::Committing;self.journal.write(&next)?;self.record=next;
        let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_redo_admin_issued","witness":witness}));self.journal.write(&next)?;self.record=next;
        self.verified_stage_and_tree(&tree,deadline)?;for (name,id) in [("HEAD.lock",&witness.locks.head),("gitdir.lock",&witness.locks.backlink),("index.lock",&witness.locks.index)] {verify_inverse_admin_lock(&admin,name,id)?;}
        verify_inverse_admin_file(&admin,"HEAD",&witness.head,4096,deadline)?;verify_inverse_admin_file(&admin,"gitdir",&witness.backlink,4096,deadline)?;verify_inverse_admin_file(&admin,"index",&witness.index,8*1024*1024,deadline)?;
        validate_root(engine,scope,&root_path,&root_id)?;engine.authorize_stage_directory_scoped(scope,&parent_path)?;
        let current_root=open(&root_path)?;if self.record.plan["policy_generation"].as_u64()!=Some(scope.generation) || self.record.plan["root_identity"]!=managed_object_identity(&current_root)?
            || self.record.plan["root_object_tag"]!=crate::service::ObjectIdentity::from_file(&current_root)?.opaque_tag() || pair(&open(worktrees_path)?)?!=pair(&worktrees)? {return Err(EngineError::Conflict)}
        if !absent(&parent,&target,&mut stat) || !absent(&worktrees,&name,&mut stat) || unsafe{libc::fstatat(journal.as_raw_fd(),label.as_ptr(),&mut stat,libc::AT_SYMLINK_NOFOLLOW)}!=0 || (stat.st_dev as u64,stat.st_ino as u64)!=pair(&admin)? || std::time::Instant::now()>=deadline {return Err(EngineError::Conflict)}
        if unsafe{libc::renameat2(journal.as_raw_fd(),label.as_ptr(),worktrees.as_raw_fd(),name.as_ptr(),libc::RENAME_NOREPLACE)}!=0{return Err(io::Error::last_os_error().into())}journal.sync_all()?;worktrees.sync_all()?;
        let restored=open(&original)?;if pair(&restored)?!=check_private_identity(&witness.admin_identity)? || !absent(&journal,&label,&mut stat) || !absent(&parent,&target,&mut stat){return Err(EngineError::Conflict)}
        for (name,id) in [("HEAD.lock",&witness.locks.head),("gitdir.lock",&witness.locks.backlink),("index.lock",&witness.locks.index)] {verify_inverse_admin_lock(&restored,name,id)?;}
        verify_inverse_admin_file(&restored,"HEAD",&witness.head,4096,deadline)?;verify_inverse_admin_file(&restored,"gitdir",&witness.backlink,4096,deadline)?;verify_inverse_admin_file(&restored,"index",&witness.index,8*1024*1024,deadline)?;
        let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_redo_admin_done","witness":witness}));self.journal.write(&next)?;self.record=next;self.quarantine=Some(tree);self.git_admin=Some(restored);Ok(())
    }

    /// Publish only the exact quarantined tree into its original absent name.
    #[allow(dead_code)]
    pub(crate) fn restore_tree(&mut self,engine:&FileEngine,scope:&AppScope,witness:&InverseGitAdminWitness,deadline:std::time::Instant)->Result<(),EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        if self.record.phase!=ManagedJournalPhase::Committing || self.record.commit_progress.last().is_none_or(|v|v["kind"]!="worktree_redo_admin_done" || v["witness"]!=serde_json::to_value(witness).unwrap_or_default()) {return Err(EngineError::Conflict)}
        let tree=self.quarantine.as_ref().ok_or(EngineError::Conflict)?;let admin=self.git_admin.as_ref().ok_or(EngineError::Conflict)?;
        let forward=self.verified_stage_and_tree(tree,deadline)?;let undo=self.inverse.read()?.ok_or(EngineError::Conflict)?;
        let q=undo.commit_progress.iter().find(|v|v["kind"]=="worktree_inverse_quarantine_done").ok_or(EngineError::Conflict)?;
        if q.get("identity")!=Some(&managed_object_identity(tree)?) {return Err(EngineError::Conflict)}
        for (name,id) in [("HEAD.lock",&witness.locks.head),("gitdir.lock",&witness.locks.backlink),("index.lock",&witness.locks.index)] {verify_inverse_admin_lock(admin,name,id)?;}
        verify_inverse_admin_file(admin,"HEAD",&witness.head,4096,deadline)?;verify_inverse_admin_file(admin,"gitdir",&witness.backlink,4096,deadline)?;verify_inverse_admin_file(admin,"index",&witness.index,8*1024*1024,deadline)?;
        let registration:RegistrationWitness=serde_json::from_value(forward.preparation.iter().find(|v|v["kind"]=="worktree_registration_accepted").and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
        let marker=base64::engine::general_purpose::STANDARD.decode(&registration.marker.bytes_base64).map_err(io::Error::other)?;let marker=std::str::from_utf8(&marker).map_err(io::Error::other)?;
        let admin_path=PathBuf::from(marker.strip_prefix("gitdir: ").and_then(|v|v.strip_suffix('\n')).filter(|v|!v.chars().any(char::is_control)).ok_or(EngineError::Conflict)?);
        let open=|p:&Path|OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(p);
        let named_admin=open(&admin_path)?;let am=named_admin.metadata()?;if (am.dev(),am.ino())!=check_private_identity(&witness.admin_identity)? || managed_object_identity(&named_admin)?!=managed_object_identity(admin)? {return Err(EngineError::Conflict)}
        let intent=forward.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;
        let parent_path=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);let name=CString::new(intent["destination_name"].as_str().ok_or(EngineError::Conflict)?.as_bytes()).map_err(io::Error::other)?;
        let root_path=PathBuf::from(self.record.plan["root_path"].as_str().ok_or(EngineError::Conflict)?);let root_id=self.record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();
        validate_root(engine,scope,&root_path,&root_id)?;engine.authorize_stage_directory_scoped(scope,&parent_path)?;
        let parent=open_managed_directory(&forward,&parent_path)?;if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&parent)?) {return Err(EngineError::Conflict)}
        let journal=open(&self.journal.directory)?;let label=CString::new(q["label"].as_str().ok_or(EngineError::Conflict)?.as_bytes()).map_err(io::Error::other)?;let mut stat:libc::stat=unsafe{std::mem::zeroed()};
        let absent=|dir:&File,name:&CString,st:&mut libc::stat| unsafe{libc::fstatat(dir.as_raw_fd(),name.as_ptr(),st,libc::AT_SYMLINK_NOFOLLOW)}!=0 && io::Error::last_os_error().raw_os_error()==Some(libc::ENOENT);
        if !absent(&parent,&name,&mut stat) || journal.metadata()?.dev()!=parent.metadata()?.dev(){return Err(EngineError::Conflict)}
        let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_redo_tree_issued","witness":witness}));self.journal.write(&next)?;self.record=next;
        self.verified_stage_and_tree(tree,deadline)?;for (name,id) in [("HEAD.lock",&witness.locks.head),("gitdir.lock",&witness.locks.backlink),("index.lock",&witness.locks.index)] {verify_inverse_admin_lock(admin,name,id)?;}
        verify_inverse_admin_file(admin,"HEAD",&witness.head,4096,deadline)?;verify_inverse_admin_file(admin,"gitdir",&witness.backlink,4096,deadline)?;verify_inverse_admin_file(admin,"index",&witness.index,8*1024*1024,deadline)?;
        validate_root(engine,scope,&root_path,&root_id)?;engine.authorize_stage_directory_scoped(scope,&parent_path)?;let current_root=open(&root_path)?;
        if self.record.plan["policy_generation"].as_u64()!=Some(scope.generation) || self.record.plan["root_identity"]!=managed_object_identity(&current_root)? || self.record.plan["root_object_tag"]!=crate::service::ObjectIdentity::from_file(&current_root)?.opaque_tag()
            || !absent(&parent,&name,&mut stat) || unsafe{libc::fstatat(journal.as_raw_fd(),label.as_ptr(),&mut stat,libc::AT_SYMLINK_NOFOLLOW)}!=0 || (stat.st_dev as u64,stat.st_ino as u64)!=(tree.metadata()?.dev(),tree.metadata()?.ino()) || std::time::Instant::now()>=deadline{return Err(EngineError::Conflict)}
        if unsafe{libc::renameat2(journal.as_raw_fd(),label.as_ptr(),parent.as_raw_fd(),name.as_ptr(),libc::RENAME_NOREPLACE)}!=0{return Err(io::Error::last_os_error().into())}journal.sync_all()?;parent.sync_all()?;
        let fd=unsafe{libc::openat(parent.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};if fd<0{return Err(io::Error::last_os_error().into())}let restored=unsafe{File::from_raw_fd(fd)};
        if managed_object_identity(&restored)?!=managed_object_identity(tree)? || !absent(&journal,&label,&mut stat){return Err(EngineError::Conflict)}self.verified_stage_and_tree(&restored,deadline)?;
        let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_redo_tree_done","witness":witness,"identity":managed_object_identity(&restored)?}));self.journal.write(&next)?;self.record=next;self.quarantine=Some(restored);Ok(())
    }

    /// Complete only after the retained Source child acknowledges both exact
    /// publications while its Git locks and original utility remain held.
    #[allow(dead_code)]
    pub(crate) fn finish_redo(&mut self,engine:&FileEngine,scope:&AppScope,witness:&InverseGitAdminWitness,deadline:std::time::Instant)->Result<(),EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        if self.record.phase!=ManagedJournalPhase::Committing || self.record.commit_progress.last().is_none_or(|v|v["kind"]!="worktree_redo_tree_done" || v["witness"]!=serde_json::to_value(witness).unwrap_or_default()){return Err(EngineError::Conflict)}
        let tree=self.quarantine.as_ref().ok_or(EngineError::Conflict)?;let admin=self.git_admin.as_ref().ok_or(EngineError::Conflict)?;let forward=self.verified_stage_and_tree(tree,deadline)?;
        let root_path=PathBuf::from(self.record.plan["root_path"].as_str().ok_or(EngineError::Conflict)?);let root_id=self.record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();
        validate_root(engine,scope,&root_path,&root_id)?;let root=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&root_path)?;
        if self.record.plan["root_identity"]!=managed_object_identity(&root)? || self.record.plan["policy_generation"].as_u64()!=Some(scope.generation){return Err(EngineError::Conflict)}
        let intent=forward.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;let parent_path=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);
        let name=CString::new(intent["destination_name"].as_str().ok_or(EngineError::Conflict)?.as_bytes()).map_err(io::Error::other)?;
        engine.authorize_stage_directory_scoped(scope,&parent_path)?;let parent=open_managed_directory(&forward,&parent_path)?;if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&parent)?) {return Err(EngineError::Conflict)}
        let mut named:libc::stat=unsafe{std::mem::zeroed()};let tm=tree.metadata()?;if unsafe{libc::fstatat(parent.as_raw_fd(),name.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || (named.st_dev as u64,named.st_ino as u64)!=(tm.dev(),tm.ino()) {return Err(EngineError::Conflict)}
        for (name,id) in [("HEAD.lock",&witness.locks.head),("gitdir.lock",&witness.locks.backlink),("index.lock",&witness.locks.index)] {verify_inverse_admin_lock(admin,name,id)?;}
        verify_inverse_admin_file(admin,"HEAD",&witness.head,4096,deadline)?;verify_inverse_admin_file(admin,"gitdir",&witness.backlink,4096,deadline)?;verify_inverse_admin_file(admin,"index",&witness.index,8*1024*1024,deadline)?;
        let registration:RegistrationWitness=serde_json::from_value(forward.preparation.iter().find(|v|v["kind"]=="worktree_registration_accepted").and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
        let marker=base64::engine::general_purpose::STANDARD.decode(&registration.marker.bytes_base64).map_err(io::Error::other)?;let marker=std::str::from_utf8(&marker).map_err(io::Error::other)?;
        let admin_path=PathBuf::from(marker.strip_prefix("gitdir: ").and_then(|v|v.strip_suffix('\n')).filter(|v|!v.chars().any(char::is_control)).ok_or(EngineError::Conflict)?);
        let named_admin=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(admin_path)?;if managed_object_identity(&named_admin)?!=managed_object_identity(admin)? {return Err(EngineError::Conflict)}
        let journal=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&self.journal.directory)?;
        let inverse_id=self.record.plan["inverse_of"].as_str().ok_or(EngineError::Conflict)?;
        for suffix in ["worktree-undo","worktree-git-admin"] {let label=CString::new(format!("{inverse_id}.{suffix}")).map_err(io::Error::other)?;
            if unsafe{libc::fstatat(journal.as_raw_fd(),label.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}==0 || io::Error::last_os_error().raw_os_error()!=Some(libc::ENOENT){return Err(EngineError::Conflict)}}
        let staged=self.record.preparation.iter().find(|v|v["kind"]=="worktree_redo_history_complete").ok_or(EngineError::Conflict)?;
        let sha=staged["sha256"].as_str().ok_or(EngineError::Conflict)?;let length=staged["length"].as_u64().ok_or(EngineError::Conflict)?;
        let sealed=forward.preparation.iter().find(|v|v["kind"]=="worktree_container_sealed").ok_or(EngineError::Conflict)?;let count=sealed["entry_count"].as_u64().ok_or(EngineError::Conflict)?;
        let prepared=self.record.history_prepare.as_ref().ok_or(EngineError::Conflict)?;let entries=prepared["before_resources"].as_array().filter(|v|v.len()==1).ok_or(EngineError::Conflict)?;let entry=&entries[0];
        if entry["existence"]!="Absent" || entry["resource_type"]!="Directory" {return Err(EngineError::Conflict)}
        let key=entry.get("resource_key").cloned().ok_or(EngineError::Conflict)?;let locator=entry.get("new_locator").cloned().unwrap_or(serde_json::Value::Null);
        let action=self.record.plan["history_action_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();let fingerprint=format!("sha256:{sha}:{length}");
        let live=serde_json::json!({"action_id":action,"status":"Committed","fingerprint":fingerprint,"committed_resources":[{"resource_key":key,"locator":locator,"existence":"Present","resource_type":"Directory","metadata":{"mode":tm.mode()&0o777,"size":null,"modified_millis":null,"opaque":null},"fingerprint":fingerprint,"coverage":{"byte_len":length,"content_digest":fingerprint,"metadata_digest":null,"metadata":{"source_worktree_tree_v2_after":{"journal_operation_id":self.record.operation_id,"container_sha256":sha,"container_length":length,"entry_count":count}}}}]});
        let descriptor=serde_json::json!({"label":"worktree-redo-v2","identity":staged["identity"],"sha256":sha,"length":length});
        let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_redo_source_confirmed","witness":witness}));next.phase=ManagedJournalPhase::LiveCommitted;next.history_completion=Some(serde_json::json!({"worktree_stage":descriptor,"live":live}));self.journal.write(&next)?;self.record=next;
        let mut next=self.record.clone();next.phase=ManagedJournalPhase::HistoryPending;self.journal.write(&next)?;self.record=next;
        let hook=engine.capture_hook.as_ref().ok_or(EngineError::Conflict)?;let stage=self.journal.worktree_history_stage(&self.record.owner_id,&action,&self.record.request_digest,deadline)?;
        hook.finish_worktree_history(&stage,&live,deadline).map_err(EngineError::HistoryCapture)?;drop(stage);
        validate_root(engine,scope,&root_path,&root_id)?;engine.authorize_stage_directory_scoped(scope,&parent_path)?;
        let root_after=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&root_path)?;
        if self.record.plan["root_identity"]!=managed_object_identity(&root_after)? || self.record.plan["policy_generation"].as_u64()!=Some(scope.generation)
            || hook.managed_account_id().map_err(EngineError::HistoryCapture)?!=self.record.plan["history_account_id"].as_str().ok_or(EngineError::Conflict)?
            || hook.managed_actor_id().map_err(EngineError::HistoryCapture)?!=self.record.plan["history_actor_id"].as_str().ok_or(EngineError::Conflict)?
            || unsafe{libc::fstatat(parent.as_raw_fd(),name.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || (named.st_dev as u64,named.st_ino as u64)!=(tm.dev(),tm.ino()) {return Err(EngineError::Conflict)}
        self.verified_stage_and_tree(tree,deadline)?;for (name,id) in [("HEAD.lock",&witness.locks.head),("gitdir.lock",&witness.locks.backlink),("index.lock",&witness.locks.index)] {verify_inverse_admin_lock(admin,name,id)?;}
        let mut next=self.record.clone();next.phase=ManagedJournalPhase::Complete;self.journal.write(&next)?;self.record=next;Ok(())
    }

    /// One issued History preparation and stream into an operation-owned FD.
    /// Any lost response leaves the original journal and stage in custody.
    #[allow(dead_code)]
    pub(crate) fn prepare_history(&mut self,engine:&FileEngine,scope:&AppScope,deadline:std::time::Instant)->Result<(),EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        if self.record.phase!=ManagedJournalPhase::Prepared || !self.record.preparation.is_empty() || std::time::Instant::now()>=deadline {return Err(EngineError::Conflict)}
        let root_path=PathBuf::from(self.record.plan["root_path"].as_str().ok_or(EngineError::Conflict)?);let root_id=self.record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();
        validate_root(engine,scope,&root_path,&root_id)?;
        let root=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&root_path)?;
        if self.record.plan["root_identity"]!=managed_object_identity(&root)? || self.record.plan["root_object_tag"]!=crate::service::ObjectIdentity::from_file(&root)?.opaque_tag(){return Err(EngineError::Conflict)}
        let undo=self.inverse.read()?.ok_or(EngineError::Conflict)?;let forward=self.original.read()?.ok_or(EngineError::Conflict)?;
        if undo.phase!=ManagedJournalPhase::Complete || forward.phase!=ManagedJournalPhase::Complete || undo.plan["history_action_id"]!=self.record.plan["inverse_history_action_id"] {return Err(EngineError::Conflict)}
        let resource=forward.history_prepare.as_ref().and_then(|v|v.get("entries")).and_then(serde_json::Value::as_array).filter(|v|v.len()==1).and_then(|v|v[0].get("resource_key")).cloned().ok_or(EngineError::Conflict)?;
        let source:SourcePlan=serde_json::from_value(forward.plan["source"].clone()).map_err(io::Error::other)?;
        let target=forward.preparation.iter().find(|v|v["kind"]=="worktree_stage_target_sealed").and_then(|v|v["target_manifest_sha256"].as_str()).filter(|v|valid_digest(v)).ok_or(EngineError::Conflict)?.to_owned();
        let get=|name:&str|self.record.plan[name].as_str().filter(|v|!v.is_empty()).map(str::to_owned).ok_or(EngineError::Conflict);
        let binding=WorktreeRedoHistoryBinding{action_id:get("history_action_id")?,actor_id:get("history_actor_id")?,account_id:get("history_account_id")?,root_id:root_id.clone(),operation_id:self.record.operation_id.clone(),
            inverse_action_id:get("inverse_history_action_id")?,inverse_version_id:get("inverse_version_id")?,container_sha256:get("container_sha256")?,container_length:self.record.plan["container_length"].as_u64().ok_or(EngineError::Conflict)?,
            commit_oid:source.commit_oid,tree_oid:source.tree_oid,target_manifest_sha256:target,resource_key:resource,timestamp_millis:SystemTime::now().duration_since(UNIX_EPOCH).map_err(io::Error::other)?.as_millis() as u64};
        let hook=engine.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
        if hook.managed_account_id().map_err(EngineError::HistoryCapture)?!=binding.account_id || hook.managed_actor_id().map_err(EngineError::HistoryCapture)?!=binding.actor_id
            || !hook.managed_worktree_capture_ready(&root_id,deadline).map_err(EngineError::HistoryCapture)? {return Err(EngineError::Conflict)}
        let label="worktree-redo-v2";let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_redo_history_issued","action_id":binding.action_id,"label":label}));self.journal.write(&next)?;self.record=next;
        let path=self.journal.staged_path(label)?;let mut options=OpenOptions::new();options.read(true).write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC);
        let file=options.open(path)?;let meta=file.metadata()?;if !meta.is_file() || meta.nlink()!=1 {return Err(EngineError::Conflict)}
        let allocated=unsafe{libc::posix_fallocate(file.as_raw_fd(),0,i64::try_from(binding.container_length).map_err(io::Error::other)?)};if allocated!=0{return Err(io::Error::from_raw_os_error(allocated).into())}
        file.sync_all()?;OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&self.journal.directory)?.sync_all()?;let identity=managed_object_identity(&file)?;
        let stage=WorktreeRedoReadStage{_journal:&self.journal,file:&file,binding:&binding};let prepared=hook.prepare_worktree_redo_history(&stage,deadline).map_err(EngineError::HistoryCapture)?;drop(stage);file.sync_all()?;
        validate_root(engine,scope,&root_path,&root_id)?;let current_root=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&root_path)?;
        if self.record.plan["root_identity"]!=managed_object_identity(&current_root)? || self.record.plan["root_object_tag"]!=crate::service::ObjectIdentity::from_file(&current_root)?.opaque_tag()
            || hook.managed_account_id().map_err(EngineError::HistoryCapture)?!=binding.account_id || hook.managed_actor_id().map_err(EngineError::HistoryCapture)?!=binding.actor_id
            || prepared["action_id"]!=binding.action_id || prepared["state"]!="BeforeDurable" || prepared["resource_key"]!=binding.resource_key || std::time::Instant::now()>=deadline {return Err(EngineError::Conflict)}
        let before=file.metadata()?;if !before.is_file() || before.nlink()!=1 || before.len()!=binding.container_length || managed_object_identity(&file)?!=identity{return Err(EngineError::Conflict)}
        let mut hash=Sha256::new();let mut offset=0u64;let mut buf=vec![0u8;CHUNK];while offset<binding.container_length {if std::time::Instant::now()>=deadline{return Err(EngineError::Conflict)}let n=((binding.container_length-offset).min(CHUNK as u64)) as usize;file.read_exact_at(&mut buf[..n],offset)?;hash.update(&buf[..n]);offset+=n as u64;}
        let after=file.metadata()?;if format!("{:x}",hash.finalize())!=binding.container_sha256 || (before.dev(),before.ino(),before.len(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())!=(after.dev(),after.ino(),after.len(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()){return Err(EngineError::Conflict)}
        let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_redo_history_complete","label":label,"identity":identity,"sha256":binding.container_sha256,"length":binding.container_length}));next.history_prepare=Some(prepared);next.phase=ManagedJournalPhase::HistoryPrepared;self.journal.write(&next)?;self.record=next;self.stage=Some(file);Ok(())
    }
}

#[allow(dead_code)]
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct InverseBegin {
    pub operation_id: String,
    pub request_digest: String,
    pub original_operation_id: String,
    pub original_version_id: String,
    pub git_plan_digest: String,
    pub root_id: String,
    pub root_path: PathBuf,
    pub root_object_tag: String,
}

#[derive(Clone,Debug,Deserialize,Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct WorktreeLookup {pub operation_id:String,pub root_id:String,pub root_path:PathBuf,pub root_object_tag:String}

impl FileEngine {
    /// Read the exact History version selected by a completed owned journal.
    pub(crate) fn worktree_lookup(&self,scope:&AppScope,owner:&str,args:&WorktreeLookup,deadline:std::time::Instant)->Result<serde_json::Value,EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        if !valid_digest(&args.root_object_tag) || !args.root_path.is_absolute() || args.root_id.is_empty() || args.root_id.len()>256 {return Err(EngineError::Conflict)}
        validate_root(self,scope,&args.root_path,&args.root_id)?;let root=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&args.root_path)?;
        if crate::service::ObjectIdentity::from_file(&root)?.opaque_tag()!=args.root_object_tag {return Err(EngineError::Conflict)}
        let hook=self.capture_hook.as_ref().ok_or(EngineError::Conflict)?;let account=hook.managed_account_id().map_err(EngineError::HistoryCapture)?;let actor=hook.managed_actor_id().map_err(EngineError::HistoryCapture)?;
        if hook.managed_authorize_root(&args.root_path).map_err(EngineError::HistoryCapture)?!=(args.root_id.clone(),args.root_path.clone()) {return Err(EngineError::Conflict)}
        let journal=self.existing_journal_nowait(self.managed_journal_root.as_ref().ok_or(EngineError::Conflict)?,&args.operation_id)?.ok_or(EngineError::Conflict)?;
        let record=journal.read()?.ok_or(EngineError::Conflict)?;
        if record.phase!=ManagedJournalPhase::Complete || record.owner_id!=owner || record.plan["history_account_id"]!=account || record.plan["root_id"]!=args.root_id
            || record.plan["root_path"]!=serde_json::json!(args.root_path) || record.plan["root_identity"]!=managed_object_identity(&root)?
            || !matches!(record.plan["kind"].as_str(),Some("source_worktree_create_v2"|"source_worktree_inverse_v2"|"source_worktree_redo_v2")) {return Err(EngineError::Conflict)}
        let action=record.plan["history_action_id"].as_str().filter(|v|!v.is_empty()).ok_or(EngineError::Conflict)?;
        let version=hook.lookup_worktree_history_version(&args.root_id,action,&args.operation_id,deadline).map_err(EngineError::HistoryCapture)?;
        validate_root(self,scope,&args.root_path,&args.root_id)?;let after=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&args.root_path)?;
        if crate::service::ObjectIdentity::from_file(&after)?.opaque_tag()!=args.root_object_tag || managed_object_identity(&after)?!=managed_object_identity(&root)?
            || hook.managed_account_id().map_err(EngineError::HistoryCapture)?!=account || hook.managed_actor_id().map_err(EngineError::HistoryCapture)?!=actor || std::time::Instant::now()>=deadline {return Err(EngineError::Conflict)}
        Ok(serde_json::json!({"state":"ready","versionID":version,"operation":record.plan["kind"]}))
    }

    /// Freeze original identity and current human authority before History
    /// preparation. The original action/version is revalidated by History;
    /// this constructor alone never authorizes a live mutation.
    #[allow(dead_code)]
    pub(crate) fn worktree_inverse_begin(&self, scope: &AppScope, owner: &str, args: &InverseBegin) -> Result<WorktreeInverse, EngineError> {
        use std::os::unix::fs::OpenOptionsExt;
        fn valid_id(s: &str) -> bool {
            (16..=80).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        }
        if !valid_id(&args.operation_id) || !valid_id(&args.original_operation_id)
            || args.operation_id == args.original_operation_id || !valid_digest(&args.request_digest)
            || !valid_digest(&args.git_plan_digest) || !valid_digest(&args.root_object_tag) || args.original_version_id.is_empty()
            || args.original_version_id.len() > 256 || args.original_version_id.bytes().any(|b| b.is_ascii_control())
            || args.root_id.is_empty() || args.root_id.len() > 256 || !args.root_path.is_absolute()
            || args.root_path.as_os_str().as_encoded_bytes().len() > 4096 { return Err(EngineError::Conflict); }
        validate_root(self, scope, &args.root_path, &args.root_id)?;
        let hook = self.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
        let account = hook.managed_account_id().map_err(EngineError::HistoryCapture)?;
        let actor = hook.managed_actor_id().map_err(EngineError::HistoryCapture)?;
        let action = hook.managed_action_id(&args.operation_id).map_err(EngineError::HistoryCapture)?;
        if account.is_empty() || actor.is_empty() || action.is_empty()
            || hook.managed_authorize_root(&args.root_path).map_err(EngineError::HistoryCapture)? != (args.root_id.clone(), args.root_path.clone()) { return Err(EngineError::Conflict); }
        let root = OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC).open(&args.root_path)?;
        if crate::service::ObjectIdentity::from_file(&root)?.opaque_tag() != args.root_object_tag { return Err(EngineError::Conflict); }
        let root_identity = managed_object_identity(&root)?;
        let journal_root = self.managed_journal_root.as_ref().ok_or(EngineError::Conflict)?;
        let original = self.existing_journal_nowait(journal_root, &args.original_operation_id)?.ok_or(EngineError::Conflict)?;
        let forward = original.read()?.ok_or(EngineError::Conflict)?;
        if forward.owner_id != owner || forward.phase != ManagedJournalPhase::Complete
            || forward.plan["kind"] != "source_worktree_create_v2" || forward.plan["history_account_id"] != account
            || forward.plan["root_id"] != args.root_id || forward.plan["root_path"] != serde_json::json!(args.root_path)
            || forward.plan["root_identity"] != root_identity || forward.plan["git_plan_digest"] != args.git_plan_digest || forward.history_prepare.is_none() || forward.history_completion.is_none()
            || !forward.commit_progress.iter().any(|event| event["kind"] == "worktree_publish_done")
            || !forward.commit_progress.iter().any(|event| event["kind"] == "worktree_backlink_committed") { return Err(EngineError::Conflict); }
        let sealed = forward.preparation.iter().find(|event| event["kind"] == "worktree_container_sealed").ok_or(EngineError::Conflict)?;
        let sha = sealed["sha256"].as_str().filter(|v| valid_digest(v)).ok_or(EngineError::Conflict)?;
        let length = sealed["length"].as_u64().filter(|v| *v > 0).ok_or(EngineError::Conflict)?;
        let original_action = forward.plan["history_action_id"].as_str().filter(|v| !v.is_empty()).ok_or(EngineError::Conflict)?;
        let original_actor = forward.plan["history_actor_id"].as_str().filter(|v| !v.is_empty()).ok_or(EngineError::Conflict)?;
        let destination = forward.preparation.iter().find(|event| event["kind"] == "worktree_private_directory_intent").cloned().ok_or(EngineError::Conflict)?;
        let journal = self.operation_journal(journal_root, &args.operation_id)?;
        if journal.read()?.is_some() { return Err(EngineError::Conflict); }
        let record = ManagedJournalRecord { version: 1, operation_id: args.operation_id.clone(), owner_id: owner.into(), request_digest: args.request_digest.clone(), phase: ManagedJournalPhase::Prepared,
            plan: serde_json::json!({"kind":"source_worktree_inverse_v2", "inverse_of":args.original_operation_id, "original_history_action_id":original_action,
                "original_history_actor_id":original_actor, "original_version_id":args.original_version_id, "original_container_sha256":sha, "original_container_length":length,
                "history_account_id":account, "history_actor_id":actor, "history_action_id":action, "git_plan_digest":args.git_plan_digest, "root_id":args.root_id, "root_path":args.root_path,
                "root_identity":root_identity, "root_object_tag":args.root_object_tag, "policy_generation":scope.generation, "original_destination":destination}),
            file_stages: vec![], directory_events: vec![], commit_progress: vec![], cleanup_progress: vec![], preparation: vec![], registry_plan: None,
            history_prepare: None, history_completion: None, review_outcome: None };
        journal.write(&record)?;
        Ok(WorktreeInverse { original, journal, record, stage:None, quarantine:None, git_admin:None })
    }
}

impl WorktreeInverse {
    /// Closed, bounded original witnesses reconstructed only from the held
    /// completed journal; no client-supplied Git path or witness is adopted.
    pub(crate) fn original_evidence(&self)->Result<serde_json::Value,EngineError>{
        let forward=self.original.read()?.ok_or(EngineError::Conflict)?;
        if forward.phase!=ManagedJournalPhase::Complete || forward.owner_id!=self.record.owner_id {return Err(EngineError::Conflict)}
        let mut registration=forward.preparation.iter().find(|v|v["kind"]=="worktree_registration_accepted").and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?;
        let parsed:RegistrationWitness=serde_json::from_value(registration.clone()).map_err(io::Error::other)?;
        check_private_identity(&parsed.private_identity)?;check_private_identity(&parsed.admin_identity)?;
        check_private_file(&parsed.backlink)?;check_private_file(&parsed.head)?;
        check_private_identity(&PrivateIdentity{device:parsed.marker.device.clone(),inode:parsed.marker.inode.clone()})?;
        if parsed.admin_name.is_empty() || parsed.admin_name.len()>255 || parsed.admin_name=="." || parsed.admin_name==".."
            || parsed.admin_name.bytes().any(|v|v<32 || v==127 || v==b'/' || v==b'\\')
            || parsed.marker.size==0 || parsed.marker.size>4096 || !valid_digest(&parsed.marker.sha256) {return Err(EngineError::Conflict)}
        let marker=base64::engine::general_purpose::STANDARD.decode(&parsed.marker.bytes_base64).map_err(io::Error::other)?;
        if marker.len() as u64!=parsed.marker.size || format!("{:x}",Sha256::digest(&marker))!=parsed.marker.sha256 {return Err(EngineError::Conflict)}
        let index:PrivateFileWitness=serde_json::from_value(forward.preparation.iter().find(|v|v["kind"]=="worktree_index_accepted").and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
        check_private_identity(&PrivateIdentity{device:index.device.clone(),inode:index.inode.clone()})?;
        if index.size==0 || index.size>8*1024*1024 || !valid_digest(&index.sha256) {return Err(EngineError::Conflict)}
        let final_witness:BacklinkCommittedWitness=serde_json::from_value(forward.commit_progress.iter().find(|v|v["kind"]=="worktree_backlink_committed").and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
        if final_witness.admin_name!=parsed.admin_name || final_witness.admin_identity!=parsed.admin_identity {return Err(EngineError::Conflict)}
        check_private_file(&final_witness.backlink)?;
        registration.as_object_mut().ok_or(EngineError::Conflict)?.insert("index".into(),serde_json::to_value(index).map_err(io::Error::other)?);
        let source:SourcePlan=serde_json::from_value(forward.plan["source"].clone()).map_err(io::Error::other)?;
        let intent=forward.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;
        let parent=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);
        let name=intent["destination_name"].as_str().ok_or(EngineError::Conflict)?;
        let root=PathBuf::from(self.record.plan["root_path"].as_str().ok_or(EngineError::Conflict)?);
        if !parent.is_absolute() || !parent.starts_with(&root) || name.is_empty() || name=="." || name==".."
            || name.len()>255 || name.bytes().any(|v|v<32 || v==127 || v==b'/' || v==b'\\') {return Err(EngineError::Conflict)}
        let destination=parent.join(name);let destination=destination.to_str().ok_or(EngineError::Conflict)?;
        let expected_backlink=format!("{}/.git\n",destination);
        if destination.len()+6>4096 || expected_backlink.len() as u64!=final_witness.backlink.size || format!("{:x}",Sha256::digest(expected_backlink.as_bytes()))!=final_witness.backlink.sha256 {return Err(EngineError::Conflict)}
        Ok(serde_json::json!({"registration":registration,"finalBacklink":final_witness.backlink,"targetOID":source.commit_oid,"treeOID":source.tree_oid,"destinationPath":destination}))
    }
    pub(crate) fn check_binding(&self,engine:&FileEngine,scope:&AppScope,owner:&str,binding:&Binding,target:&Path)->Result<(),EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        if self.record.owner_id!=owner || self.record.operation_id!=binding.operation_id || self.record.request_digest!=binding.request_digest
            || self.record.plan["git_plan_digest"]!=binding.git_plan_digest || self.record.plan["root_id"]!=binding.root_id
            || self.record.plan["root_object_tag"]!=binding.root_object_tag || self.record.plan["root_path"]!=serde_json::json!(target)
            || self.record.plan["policy_generation"].as_u64()!=Some(scope.generation) {return Err(EngineError::Conflict)}
        validate_root(engine,scope,target,&binding.root_id)?;
        let root=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(target)?;
        if self.record.plan["root_identity"]!=managed_object_identity(&root)? {return Err(EngineError::Conflict)}
        if let Some(done)=self.record.commit_progress.iter().find(|v|v["kind"]=="worktree_inverse_quarantine_done") {
            let held=self.quarantine.as_ref().ok_or(EngineError::Conflict)?;let held_meta=held.metadata()?;
            if done.get("identity")!=Some(&managed_object_identity(held)?) {return Err(EngineError::Conflict)}
            let name=done["label"].as_str().filter(|v|*v==format!("{}.worktree-undo",self.record.operation_id)).ok_or(EngineError::Conflict)?;
            let journal=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&self.journal.directory)?;
            let label=CString::new(name.as_bytes()).map_err(io::Error::other)?;let mut named:libc::stat=unsafe{std::mem::zeroed()};
            if unsafe{libc::fstatat(journal.as_raw_fd(),label.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=held_meta.dev() || named.st_ino as u64!=held_meta.ino(){return Err(EngineError::Conflict)}
            let forward=self.original.read()?.ok_or(EngineError::Conflict)?;let intent=forward.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;
            let parent_path=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);let child=CString::new(intent["destination_name"].as_str().ok_or(EngineError::Conflict)?.as_bytes()).map_err(io::Error::other)?;
            let parent=open_managed_directory(&forward,&parent_path)?;if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&parent)?) {return Err(EngineError::Conflict)}
            if unsafe{libc::fstatat(parent.as_raw_fd(),child.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}==0 || io::Error::last_os_error().raw_os_error()!=Some(libc::ENOENT){return Err(EngineError::Conflict)}
        } else if crate::service::ObjectIdentity::from_file(&root)?.opaque_tag()!=binding.root_object_tag {return Err(EngineError::Conflict)}
        Ok(())
    }
    pub(crate) fn history_state(&self)->&'static str {
        if self.record.preparation.iter().any(|v|v["kind"]=="worktree_inverse_stage_issued")
            && !self.record.preparation.iter().any(|v|v["kind"]=="worktree_inverse_stage_complete") {"unknown"}
        else if self.record.phase==ManagedJournalPhase::Complete {"completed"}
        else if self.record.commit_progress.last().is_some_and(|v|v["kind"]=="worktree_inverse_admin_done") {"admin_relocated"}
        else if self.record.commit_progress.last().is_some_and(|v|v["kind"]=="worktree_inverse_quarantine_done") {"quarantined"}
        else if self.record.phase==ManagedJournalPhase::Committing || self.record.phase==ManagedJournalPhase::HistoryPending || self.record.phase==ManagedJournalPhase::LiveCommitted {"unknown"}
        else if self.record.phase==ManagedJournalPhase::HistoryPrepared {"history_prepared"}
        else if self.record.preparation.iter().any(|v|v["kind"]=="worktree_inverse_history_issued") {"unknown"}
        else {"pending"}
    }
    /// Retain an exact History stream in the inverse's own pinned stage.
    /// An interruption after the issue marker never resumes or adopts it.
    pub(crate) fn stage_history(&mut self,engine:&FileEngine,scope:&AppScope,deadline:std::time::Instant)->Result<(),EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        if self.record.phase!=ManagedJournalPhase::HistoryPrepared || self.stage.is_some()
            || self.record.preparation.iter().any(|v|v["kind"]=="worktree_inverse_stage_issued") || std::time::Instant::now()>=deadline {return Err(EngineError::Conflict)}
        let root_id=self.record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();
        let root_path=PathBuf::from(self.record.plan["root_path"].as_str().ok_or(EngineError::Conflict)?);
        validate_root(engine,scope,&root_path,&root_id)?;
        if self.record.plan["policy_generation"].as_u64()!=Some(scope.generation){return Err(EngineError::Conflict)}
        let root=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&root_path)?;
        if self.record.plan["root_identity"]!=managed_object_identity(&root)? || self.record.plan["root_object_tag"]!=crate::service::ObjectIdentity::from_file(&root)?.opaque_tag(){return Err(EngineError::Conflict)}
        let expected_sha=self.record.plan["original_container_sha256"].as_str().filter(|v|valid_digest(v)).ok_or(EngineError::Conflict)?.to_owned();
        let length=self.record.plan["original_container_length"].as_u64().filter(|v|*v>0 && *v<=HEADER+MAX_TOTAL).ok_or(EngineError::Conflict)?;
        let action=self.record.history_prepare.as_ref().ok_or(EngineError::Conflict)?;
        let version=action.pointer("/before/version_id").and_then(serde_json::Value::as_str).filter(|v|!v.is_empty()).ok_or(EngineError::Conflict)?.to_owned();
        let fingerprint=format!("sha256:{expected_sha}:{length}");
        if action.pointer("/before/fingerprint").and_then(serde_json::Value::as_str)!=Some(fingerprint.as_str()) {return Err(EngineError::Conflict)}
        let hook=engine.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
        if !hook.managed_worktree_capture_ready(&root_id,deadline).map_err(EngineError::HistoryCapture)? {return Err(EngineError::Conflict)}
        let label="worktree-inverse-v2";let mut next=self.record.clone();
        next.preparation.push(serde_json::json!({"kind":"worktree_inverse_stage_issued","label":label,"version_id":version,"fingerprint":fingerprint}));self.journal.write(&next)?;self.record=next;
        let path=self.journal.staged_path(label)?;let mut options=OpenOptions::new();options.read(true).write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC);
        let file=options.open(path)?;let meta=file.metadata()?;if !meta.is_file() || meta.nlink()!=1{return Err(EngineError::Conflict)}
        let allocated=unsafe{libc::posix_fallocate(file.as_raw_fd(),0,i64::try_from(length).map_err(io::Error::other)?)};if allocated!=0{return Err(io::Error::from_raw_os_error(allocated).into())}
        file.sync_all()?;
        OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&self.journal.directory)?.sync_all()?;
        let identity=managed_object_identity(&file)?;
        let actor=self.record.plan["history_actor_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();let account=self.record.plan["history_account_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();
        let action_id=self.record.plan["history_action_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();
        let stage=WorktreeInverseReadStage{_journal:&self.journal,file:&file,action_id,actor_id:actor,account_id:account,root_id,version_id:version.clone(),fingerprint:fingerprint.clone(),length};
        hook.read_worktree_inverse_history(&stage,deadline).map_err(EngineError::HistoryCapture)?;drop(stage);file.sync_all()?;
        validate_root(engine,scope,&root_path,self.record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?)?;
        let current_root=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&root_path)?;
        if self.record.plan["root_identity"]!=managed_object_identity(&current_root)? || self.record.plan["root_object_tag"]!=crate::service::ObjectIdentity::from_file(&current_root)?.opaque_tag()
            || hook.managed_actor_id().map_err(EngineError::HistoryCapture)?!=self.record.plan["history_actor_id"].as_str().ok_or(EngineError::Conflict)?
            || hook.managed_account_id().map_err(EngineError::HistoryCapture)?!=self.record.plan["history_account_id"].as_str().ok_or(EngineError::Conflict)? {return Err(EngineError::Conflict)}
        let before=file.metadata()?;if !before.is_file() || before.nlink()!=1 || before.len()!=length || managed_object_identity(&file)?!=identity{return Err(EngineError::Conflict)}
        let mut hash=Sha256::new();let mut offset=0u64;let mut buffer=vec![0u8;CHUNK];while offset<length {if std::time::Instant::now()>=deadline{return Err(EngineError::Conflict)}let count=((length-offset).min(CHUNK as u64)) as usize;file.read_exact_at(&mut buffer[..count],offset)?;hash.update(&buffer[..count]);offset+=count as u64;}
        let after=file.metadata()?;if format!("{:x}",hash.finalize())!=expected_sha || (before.dev(),before.ino(),before.len(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())!=(after.dev(),after.ino(),after.len(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()){return Err(EngineError::Conflict)}
        let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_inverse_stage_complete","label":label,"identity":identity,"fingerprint":fingerprint,"version_id":version}));self.journal.write(&next)?;self.record=next;self.stage=Some(file);Ok(())
    }
    fn inverse_inventory(&self)->Result<(ManagedJournalRecord,Vec<String>,Vec<CompletedBlob>),EngineError>{
        let forward=self.original.read()?.ok_or(EngineError::Conflict)?;
        if forward.phase!=ManagedJournalPhase::Complete || forward.owner_id!=self.record.owner_id {return Err(EngineError::Conflict)}
        let source:SourcePlan=serde_json::from_value(forward.plan["source"].clone()).map_err(io::Error::other)?;let (dirs,total)=validate_plan(&source).map_err(EngineError::from)?;
        let sealed=forward.preparation.iter().find(|v|v["kind"]=="worktree_stage_target_sealed").ok_or(EngineError::Conflict)?;
        let saved_dirs:Vec<String>=serde_json::from_value(sealed["directories"].clone()).map_err(io::Error::other)?;
        let blobs:Vec<CompletedBlob>=serde_json::from_value(sealed["blobs"].clone()).map_err(io::Error::other)?;
        if dirs!=saved_dirs || blobs.len()!=source.entries.len() || sealed["payload_bytes"].as_u64()!=Some(total)
            || source.entries.iter().zip(&blobs).any(|(entry,blob)|entry.path!=blob.path || entry.oid!=blob.oid || entry.size!=blob.size || entry.mode&0o777!=blob.mode || !valid_digest(&blob.sha256))
            || sealed["target_manifest_sha256"]!=target_digest(&dirs,&blobs)? {return Err(EngineError::Conflict)}
        Ok((forward,dirs,blobs))
    }
    /// Compare the complete current tree and marker against the original
    /// journal inventory and the independently retained Lore bytes.
    pub(crate) fn verify_target(&self,engine:&FileEngine,scope:&AppScope,deadline:std::time::Instant)->Result<(),EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        let issuing=self.record.phase==ManagedJournalPhase::Committing && self.record.commit_progress.last().is_some_and(|v|v["kind"]=="worktree_inverse_quarantine_issued");
        if (self.record.phase!=ManagedJournalPhase::HistoryPrepared && !issuing) || std::time::Instant::now()>=deadline {return Err(EngineError::Conflict)}
        let file=self.stage.as_ref().ok_or(EngineError::Conflict)?;
        let staged=self.record.preparation.iter().find(|v|v["kind"]=="worktree_inverse_stage_complete").ok_or(EngineError::Conflict)?;
        let expected=self.record.plan["original_container_sha256"].as_str().filter(|v|valid_digest(v)).ok_or(EngineError::Conflict)?;
        let length=self.record.plan["original_container_length"].as_u64().filter(|v|*v>0 && *v<=HEADER+MAX_TOTAL).ok_or(EngineError::Conflict)?;
        let before=file.metadata()?;if !before.is_file() || before.nlink()!=1 || before.len()!=length || staged.get("identity")!=Some(&managed_object_identity(file)?) {return Err(EngineError::Conflict)}
        let mut hash=Sha256::new();let mut offset=0u64;let mut buffer=vec![0u8;CHUNK];while offset<length {if std::time::Instant::now()>=deadline{return Err(EngineError::Conflict)}let count=((length-offset).min(CHUNK as u64)) as usize;file.read_exact_at(&mut buffer[..count],offset)?;hash.update(&buffer[..count]);offset+=count as u64;}
        let after=file.metadata()?;if format!("{:x}",hash.finalize())!=expected || (before.dev(),before.ino(),before.len(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())!=(after.dev(),after.ino(),after.len(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()) {return Err(EngineError::Conflict)}
        let (forward,dirs,blobs)=self.inverse_inventory()?;
        let intent=forward.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;
        let parent_path=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);let name=intent["destination_name"].as_str().ok_or(EngineError::Conflict)?;
        engine.authorize_stage_directory_scoped(scope,&parent_path)?;engine.authorize_stage_directory_scoped(scope,&parent_path.join(name))?;
        let parent=open_managed_directory(&forward,&parent_path)?;if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&parent)?) {return Err(EngineError::Conflict)}
        let component=CString::new(name.as_bytes()).map_err(io::Error::other)?;let fd=unsafe{libc::openat(parent.as_raw_fd(),component.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
        if fd<0{return Err(io::Error::last_os_error().into())}let target=unsafe{File::from_raw_fd(fd)};
        verify_materialized_record(&forward,&dirs,&blobs,&target,deadline)?;
        let mut named:libc::stat=unsafe{std::mem::zeroed()};let metadata=target.metadata()?;
        if unsafe{libc::fstatat(parent.as_raw_fd(),component.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=metadata.dev() || named.st_ino as u64!=metadata.ino(){return Err(EngineError::Conflict)}
        Ok(())
    }
    /// Dormant physical primitive: the eventual caller must hold the exact
    /// Source Git locks and current utility through this entire call. This is
    /// not exposed as a service operation until that owner is joined.
    #[allow(dead_code)]
    pub(crate) fn quarantine_target(&mut self,engine:&FileEngine,scope:&AppScope,deadline:std::time::Instant)->Result<(),EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        if self.record.phase!=ManagedJournalPhase::HistoryPrepared || self.quarantine.is_some() {return Err(EngineError::Conflict)}
        self.verify_target(engine,scope,deadline)?;
        let (forward,dirs,blobs)=self.inverse_inventory()?;
        let intent=forward.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;
        let parent_path=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);
        let name=intent["destination_name"].as_str().ok_or(EngineError::Conflict)?;
        let parent=open_managed_directory(&forward,&parent_path)?;
        if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&parent)?) {return Err(EngineError::Conflict)}
        let component=CString::new(name.as_bytes()).map_err(io::Error::other)?;
        let fd=unsafe{libc::openat(parent.as_raw_fd(),component.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
        if fd<0{return Err(io::Error::last_os_error().into())}let target=unsafe{File::from_raw_fd(fd)};
        let identity=managed_object_identity(&target)?;let created=forward.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_created").ok_or(EngineError::Conflict)?;
        if created.get("identity")!=Some(&identity) {return Err(EngineError::Conflict)}
        let journal_dir=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&self.journal.directory)?;
        if parent.metadata()?.dev()!=journal_dir.metadata()?.dev(){return Err(EngineError::Conflict)}
        let label=format!("{}.worktree-undo",self.record.operation_id);let destination=CString::new(label.as_bytes()).map_err(io::Error::other)?;
        let mut next=self.record.clone();next.phase=ManagedJournalPhase::Committing;self.journal.write(&next)?;self.record=next;
        let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_inverse_quarantine_issued","identity":identity,"label":label}));self.journal.write(&next)?;self.record=next;
        self.verify_target(engine,scope,deadline)?;
        let mut named:libc::stat=unsafe{std::mem::zeroed()};let meta=target.metadata()?;
        if unsafe{libc::fstatat(parent.as_raw_fd(),component.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=meta.dev() || named.st_ino as u64!=meta.ino() || std::time::Instant::now()>=deadline {return Err(EngineError::Conflict)}
        if unsafe{libc::renameat2(parent.as_raw_fd(),component.as_ptr(),journal_dir.as_raw_fd(),destination.as_ptr(),libc::RENAME_NOREPLACE)}!=0{return Err(io::Error::last_os_error().into())}
        parent.sync_all()?;journal_dir.sync_all()?;
        let fd=unsafe{libc::openat(journal_dir.as_raw_fd(),destination.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
        if fd<0{return Err(io::Error::last_os_error().into())}let quarantined=unsafe{File::from_raw_fd(fd)};
        if managed_object_identity(&quarantined)?!=identity{return Err(EngineError::Conflict)}
        verify_materialized_record(&forward,&dirs,&blobs,&quarantined,deadline)?;
        let mut installed:libc::stat=unsafe{std::mem::zeroed()};
        if unsafe{libc::fstatat(journal_dir.as_raw_fd(),destination.as_ptr(),&mut installed,libc::AT_SYMLINK_NOFOLLOW)}!=0
            || installed.st_dev as u64!=meta.dev() || installed.st_ino as u64!=meta.ino() {return Err(EngineError::Conflict)}
        if unsafe{libc::fstatat(parent.as_raw_fd(),component.as_ptr(),&mut installed,libc::AT_SYMLINK_NOFOLLOW)}==0
            || io::Error::last_os_error().raw_os_error()!=Some(libc::ENOENT) {return Err(EngineError::Conflict)}
        let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_inverse_quarantine_done","identity":identity,"label":label}));self.journal.write(&next)?;self.record=next;self.quarantine=Some(quarantined);Ok(())
    }
    /// Dormant typed syscall executor. A retained Source child must own and
    /// revalidate the supplied lock witnesses and authorize this exact phase;
    /// no generic Files route may expose this method.
    #[allow(dead_code)]
    pub(crate) fn relocate_git_admin(&mut self,witness:&InverseGitAdminWitness,deadline:std::time::Instant)->Result<(),EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        if self.record.phase!=ManagedJournalPhase::Committing || self.git_admin.is_some() || self.quarantine.is_none()
            || !self.record.commit_progress.last().is_some_and(|v|v["kind"]=="worktree_inverse_quarantine_done") || std::time::Instant::now()>=deadline {return Err(EngineError::Conflict)}
        let (forward,dirs,blobs)=self.inverse_inventory()?;
        let quarantine=self.quarantine.as_ref().ok_or(EngineError::Conflict)?;
        verify_materialized_record(&forward,&dirs,&blobs,quarantine,deadline)?;
        let stage=self.stage.as_ref().ok_or(EngineError::Conflict)?;
        let staged=self.record.preparation.iter().find(|v|v["kind"]=="worktree_inverse_stage_complete").ok_or(EngineError::Conflict)?;
        let before=stage.metadata()?;let length=self.record.plan["original_container_length"].as_u64().ok_or(EngineError::Conflict)?;
        if !before.is_file() || before.nlink()!=1 || before.len()!=length || staged.get("identity")!=Some(&managed_object_identity(stage)?) {return Err(EngineError::Conflict)}
        let mut hash=Sha256::new();let mut offset=0u64;let mut bytes=vec![0u8;CHUNK];while offset<length {if std::time::Instant::now()>=deadline{return Err(EngineError::Conflict)}let count=((length-offset).min(CHUNK as u64)) as usize;stage.read_exact_at(&mut bytes[..count],offset)?;hash.update(&bytes[..count]);offset+=count as u64;}
        let after=stage.metadata()?;if self.record.plan["original_container_sha256"]!=format!("{:x}",hash.finalize())
            || (before.dev(),before.ino(),before.len(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())!=(after.dev(),after.ino(),after.len(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()){return Err(EngineError::Conflict)}
        let registration:RegistrationWitness=serde_json::from_value(forward.preparation.iter().find(|v|v["kind"]=="worktree_registration_accepted")
            .and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
        let committed:BacklinkCommittedWitness=serde_json::from_value(forward.commit_progress.iter().find(|v|v["kind"]=="worktree_backlink_committed")
            .and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
        let source:SourcePlan=serde_json::from_value(forward.plan["source"].clone()).map_err(io::Error::other)?;
        if witness.admin_name!=registration.admin_name || witness.admin_identity!=registration.admin_identity || witness.backlink!=committed.backlink
            || witness.target_oid!=source.commit_oid || witness.tree_oid!=source.tree_oid {return Err(EngineError::Conflict)}
        check_private_identity(&witness.common_git_identity)?;check_private_identity(&witness.worktrees_identity)?;check_private_identity(&witness.admin_identity)?;
        for lock in [&witness.locks.head,&witness.locks.backlink,&witness.locks.index] {check_private_identity(lock)?;}
        check_private_file(&witness.head)?;check_private_file(&witness.backlink)?;
        check_private_identity(&PrivateIdentity{device:witness.index.device.clone(),inode:witness.index.inode.clone()})?;
        if witness.index.size==0 || witness.index.size>8*1024*1024 || !valid_digest(&witness.index.sha256){return Err(EngineError::Conflict)}
        let marker=base64::engine::general_purpose::STANDARD.decode(&registration.marker.bytes_base64).map_err(io::Error::other)?;
        if marker.len()!=registration.marker.size as usize || format!("{:x}",Sha256::digest(&marker))!=registration.marker.sha256 {return Err(EngineError::Conflict)}
        let marker=std::str::from_utf8(&marker).map_err(io::Error::other)?;
        let text=marker.strip_prefix("gitdir: ").and_then(|v|v.strip_suffix('\n')).filter(|v|!v.chars().any(char::is_control)).ok_or(EngineError::Conflict)?;
        let admin_path=PathBuf::from(text);let worktrees_path=admin_path.parent().ok_or(EngineError::Conflict)?;let common_path=worktrees_path.parent().ok_or(EngineError::Conflict)?;
        if !admin_path.is_absolute() || admin_path.file_name().and_then(|v|v.to_str())!=Some(witness.admin_name.as_str())
            || worktrees_path.file_name().and_then(|v|v.to_str())!=Some("worktrees") || common_path.file_name().and_then(|v|v.to_str())!=Some(".git")
            || fs::canonicalize(&admin_path)?!=admin_path {return Err(EngineError::Conflict)}
        let common=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(common_path)?;
        let worktrees=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(worktrees_path)?;
        let admin=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&admin_path)?;
        let as_pair=|file:&File|->io::Result<(u64,u64)>{let m=file.metadata()?;Ok((m.dev(),m.ino()))};
        if as_pair(&common)?!=check_private_identity(&witness.common_git_identity)? || as_pair(&worktrees)?!=check_private_identity(&witness.worktrees_identity)?
            || as_pair(&admin)?!=check_private_identity(&witness.admin_identity)? {return Err(EngineError::Conflict)}
        let expected_head=format!("{}\n",source.commit_oid);let intent=forward.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;
        let parent_path=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);let target_name=intent["destination_name"].as_str().ok_or(EngineError::Conflict)?;
        let expected_backlink=format!("{}/.git\n",parent_path.join(target_name).to_str().ok_or(EngineError::Conflict)?);
        if witness.head.size!=expected_head.len() as u64 || witness.head.sha256!=format!("{:x}",Sha256::digest(expected_head.as_bytes()))
            || witness.backlink.size!=expected_backlink.len() as u64 || witness.backlink.sha256!=format!("{:x}",Sha256::digest(expected_backlink.as_bytes())) {return Err(EngineError::Conflict)}
        verify_inverse_admin_file(&admin,"HEAD",&witness.head,4096,deadline)?;verify_inverse_admin_file(&admin,"gitdir",&witness.backlink,4096,deadline)?;verify_inverse_admin_file(&admin,"index",&witness.index,8*1024*1024,deadline)?;
        for (name,identity) in [("HEAD.lock",&witness.locks.head),("gitdir.lock",&witness.locks.backlink),("index.lock",&witness.locks.index)] {verify_inverse_admin_lock(&admin,name,identity)?;}
        let parent=open_managed_directory(&forward,&parent_path)?;if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&parent)?) {return Err(EngineError::Conflict)}
        let target=CString::new(target_name.as_bytes()).map_err(io::Error::other)?;let mut state:libc::stat=unsafe{std::mem::zeroed()};
        if unsafe{libc::fstatat(parent.as_raw_fd(),target.as_ptr(),&mut state,libc::AT_SYMLINK_NOFOLLOW)}==0 || io::Error::last_os_error().raw_os_error()!=Some(libc::ENOENT){return Err(EngineError::Conflict)}
        let journal_dir=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&self.journal.directory)?;
        if worktrees.metadata()?.dev()!=journal_dir.metadata()?.dev(){return Err(EngineError::Conflict)}
        let quarantine_label=CString::new(format!("{}.worktree-undo",self.record.operation_id)).map_err(io::Error::other)?;let held=quarantine.metadata()?;let mut held_named:libc::stat=unsafe{std::mem::zeroed()};
        if unsafe{libc::fstatat(journal_dir.as_raw_fd(),quarantine_label.as_ptr(),&mut held_named,libc::AT_SYMLINK_NOFOLLOW)}!=0
            || held_named.st_dev as u64!=held.dev() || held_named.st_ino as u64!=held.ino(){return Err(EngineError::Conflict)}
        let label=format!("{}.worktree-git-admin",self.record.operation_id);let dest=CString::new(label.as_bytes()).map_err(io::Error::other)?;let name=CString::new(witness.admin_name.as_bytes()).map_err(io::Error::other)?;
        let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_inverse_admin_issued","identity":witness.admin_identity,"label":label,"witness":witness}));self.journal.write(&next)?;self.record=next;
        if std::time::Instant::now()>=deadline{return Err(EngineError::Conflict)}
        for (name,identity) in [("HEAD.lock",&witness.locks.head),("gitdir.lock",&witness.locks.backlink),("index.lock",&witness.locks.index)] {verify_inverse_admin_lock(&admin,name,identity)?;}
        verify_inverse_admin_file(&admin,"HEAD",&witness.head,4096,deadline)?;verify_inverse_admin_file(&admin,"gitdir",&witness.backlink,4096,deadline)?;verify_inverse_admin_file(&admin,"index",&witness.index,8*1024*1024,deadline)?;
        if unsafe{libc::fstatat(parent.as_raw_fd(),target.as_ptr(),&mut state,libc::AT_SYMLINK_NOFOLLOW)}==0 || io::Error::last_os_error().raw_os_error()!=Some(libc::ENOENT){return Err(EngineError::Conflict)}
        let mut named:libc::stat=unsafe{std::mem::zeroed()};let meta=admin.metadata()?;
        if unsafe{libc::fstatat(worktrees.as_raw_fd(),name.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=meta.dev() || named.st_ino as u64!=meta.ino(){return Err(EngineError::Conflict)}
        if unsafe{libc::renameat2(worktrees.as_raw_fd(),name.as_ptr(),journal_dir.as_raw_fd(),dest.as_ptr(),libc::RENAME_NOREPLACE)}!=0{return Err(io::Error::last_os_error().into())}
        worktrees.sync_all()?;journal_dir.sync_all()?;
        let fd=unsafe{libc::openat(journal_dir.as_raw_fd(),dest.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
        if fd<0{return Err(io::Error::last_os_error().into())}let relocated=unsafe{File::from_raw_fd(fd)};
        if as_pair(&relocated)?!=check_private_identity(&witness.admin_identity)? {return Err(EngineError::Conflict)}
        for (name,identity) in [("HEAD.lock",&witness.locks.head),("gitdir.lock",&witness.locks.backlink),("index.lock",&witness.locks.index)] {verify_inverse_admin_lock(&relocated,name,identity)?;}
        verify_inverse_admin_file(&relocated,"HEAD",&witness.head,4096,deadline)?;verify_inverse_admin_file(&relocated,"gitdir",&witness.backlink,4096,deadline)?;verify_inverse_admin_file(&relocated,"index",&witness.index,8*1024*1024,deadline)?;
        let mut installed:libc::stat=unsafe{std::mem::zeroed()};
        if unsafe{libc::fstatat(journal_dir.as_raw_fd(),dest.as_ptr(),&mut installed,libc::AT_SYMLINK_NOFOLLOW)}!=0 || installed.st_dev as u64!=meta.dev() || installed.st_ino as u64!=meta.ino()
            || unsafe{libc::fstatat(worktrees.as_raw_fd(),name.as_ptr(),&mut installed,libc::AT_SYMLINK_NOFOLLOW)}==0 || io::Error::last_os_error().raw_os_error()!=Some(libc::ENOENT)
            || unsafe{libc::fstatat(parent.as_raw_fd(),target.as_ptr(),&mut installed,libc::AT_SYMLINK_NOFOLLOW)}==0 || io::Error::last_os_error().raw_os_error()!=Some(libc::ENOENT) {return Err(EngineError::Conflict)}
        let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_inverse_admin_done","identity":witness.admin_identity,"label":label,"witness":witness}));self.journal.write(&next)?;self.record=next;self.git_admin=Some(relocated);Ok(())
    }
    /// Dormant terminal phase: only the retained Source callback may provide
    /// its repeated, exact held-lock proof after the private admin relocation.
    #[allow(dead_code)]
    pub(crate) fn finish_inverse(&mut self,engine:&FileEngine,scope:&AppScope,witness:&InverseGitAdminWitness,deadline:std::time::Instant)->Result<(),EngineError>{
        use std::os::unix::fs::OpenOptionsExt;
        if self.record.phase!=ManagedJournalPhase::Committing || std::time::Instant::now()>=deadline {return Err(EngineError::Conflict)}
        let done=self.record.commit_progress.last().filter(|v|v["kind"]=="worktree_inverse_admin_done").ok_or(EngineError::Conflict)?;
        if done.get("witness")!=Some(&serde_json::to_value(witness).map_err(io::Error::other)?) {return Err(EngineError::Conflict)}
        let root_id=self.record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();let root_path=PathBuf::from(self.record.plan["root_path"].as_str().ok_or(EngineError::Conflict)?);
        let binding=Binding{operation_id:self.record.operation_id.clone(),request_digest:self.record.request_digest.clone(),git_plan_digest:self.record.plan["git_plan_digest"].as_str().ok_or(EngineError::Conflict)?.into(),root_id:root_id.clone(),root_object_tag:self.record.plan["root_object_tag"].as_str().ok_or(EngineError::Conflict)?.into()};
        self.check_binding(engine,scope,&self.record.owner_id,&binding,&root_path)?;
        let admin=self.git_admin.as_ref().ok_or(EngineError::Conflict)?;let quarantine=self.quarantine.as_ref().ok_or(EngineError::Conflict)?;
        let (forward,dirs,blobs)=self.inverse_inventory()?;verify_materialized_record(&forward,&dirs,&blobs,quarantine,deadline)?;
        let journal_dir=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&self.journal.directory)?;
        let label=CString::new(format!("{}.worktree-git-admin",self.record.operation_id)).map_err(io::Error::other)?;let mut named:libc::stat=unsafe{std::mem::zeroed()};let meta=admin.metadata()?;
        if unsafe{libc::fstatat(journal_dir.as_raw_fd(),label.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=meta.dev() || named.st_ino as u64!=meta.ino()
            || (meta.dev(),meta.ino())!=check_private_identity(&witness.admin_identity)? {return Err(EngineError::Conflict)}
        for (name,identity) in [("HEAD.lock",&witness.locks.head),("gitdir.lock",&witness.locks.backlink),("index.lock",&witness.locks.index)] {verify_inverse_admin_lock(admin,name,identity)?;}
        verify_inverse_admin_file(admin,"HEAD",&witness.head,4096,deadline)?;verify_inverse_admin_file(admin,"gitdir",&witness.backlink,4096,deadline)?;verify_inverse_admin_file(admin,"index",&witness.index,8*1024*1024,deadline)?;
        let prepared=self.record.history_prepare.as_ref().ok_or(EngineError::Conflict)?;let resources=prepared["before_resources"].as_array().filter(|v|v.len()==1).ok_or(EngineError::Conflict)?;
        let key=resources[0].get("resource_key").cloned().ok_or(EngineError::Conflict)?;let action_id=self.record.plan["history_action_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();
        let live=serde_json::json!({"action_id":action_id,"status":"Committed","fingerprint":"missing","committed_resources":[{"resource_key":key,"locator":null,"existence":"Absent","resource_type":"Directory","metadata":{"mode":null,"size":null,"modified_millis":null,"opaque":null},"fingerprint":"missing","coverage":{"content_digest":null,"metadata_digest":null,"byte_len":0,"metadata":{"source_worktree_inverse_v2":{"journal_operation_id":self.record.operation_id}}}}]});
        let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_inverse_source_confirmed","witness":witness}));next.phase=ManagedJournalPhase::LiveCommitted;next.history_completion=Some(serde_json::json!({"live":live}));self.journal.write(&next)?;self.record=next;
        let mut next=self.record.clone();next.phase=ManagedJournalPhase::HistoryPending;self.journal.write(&next)?;self.record=next;
        let actor=self.record.plan["history_actor_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();let account=self.record.plan["history_account_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();
        let stage=WorktreeInverseFinishStage{journal:&self.journal,action_id,actor_id:actor,account_id:account,root_id,operation_id:self.record.operation_id.clone(),live};
        stage.verify()?;let hook=engine.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
        hook.finish_worktree_inverse_history(&stage,deadline).map_err(EngineError::HistoryCapture)?;stage.verify()?;drop(stage);
        self.check_binding(engine,scope,&self.record.owner_id,&binding,&root_path)?;
        verify_materialized_record(&forward,&dirs,&blobs,quarantine,deadline)?;
        for (name,identity) in [("HEAD.lock",&witness.locks.head),("gitdir.lock",&witness.locks.backlink),("index.lock",&witness.locks.index)] {verify_inverse_admin_lock(admin,name,identity)?;}
        verify_inverse_admin_file(admin,"index",&witness.index,8*1024*1024,deadline)?;
        let mut next=self.record.clone();next.phase=ManagedJournalPhase::Complete;self.journal.write(&next)?;self.record=next;Ok(())
    }
    /// Issue the original journal-bound History action exactly once. Failure
    /// after the durable issue marker is unknown and never permits reissue.
    #[allow(dead_code)]
    pub(crate) fn prepare_history(&mut self, engine: &FileEngine, scope: &AppScope, deadline: std::time::Instant) -> Result<(), EngineError> {
        use std::os::unix::fs::OpenOptionsExt;
        if self.record.phase != ManagedJournalPhase::Prepared || self.record.history_prepare.is_some()
            || self.record.preparation.iter().any(|v| v["kind"] == "worktree_inverse_history_issued")
            || std::time::Instant::now() >= deadline { return Err(EngineError::Conflict); }
        let root_id=self.record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?;
        let root_path=PathBuf::from(self.record.plan["root_path"].as_str().ok_or(EngineError::Conflict)?);
        validate_root(engine,scope,&root_path,root_id)?;
        if self.record.plan["policy_generation"].as_u64()!=Some(scope.generation) {return Err(EngineError::Conflict)}
        let root=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&root_path)?;
        if self.record.plan["root_identity"]!=managed_object_identity(&root)?
            || self.record.plan["root_object_tag"]!=crate::service::ObjectIdentity::from_file(&root)?.opaque_tag() {return Err(EngineError::Conflict)}
        let forward=self.original.read()?.ok_or(EngineError::Conflict)?;
        if forward.phase!=ManagedJournalPhase::Complete || forward.owner_id!=self.record.owner_id
            || forward.plan["history_action_id"]!=self.record.plan["original_history_action_id"] {return Err(EngineError::Conflict)}
        let resource=forward.history_prepare.as_ref().and_then(|v|v.get("entries")).and_then(serde_json::Value::as_array)
            .filter(|v|v.len()==1).and_then(|v|v[0].get("resource_key")).cloned().ok_or(EngineError::Conflict)?;
        let get=|name:&str|self.record.plan[name].as_str().filter(|v|!v.is_empty()).map(str::to_owned).ok_or(EngineError::Conflict);
        let binding=WorktreeInverseHistoryBinding {action_id:get("history_action_id")?, actor_id:get("history_actor_id")?, account_id:get("history_account_id")?,
            root_id:root_id.into(), operation_id:self.record.operation_id.clone(), original_action_id:get("original_history_action_id")?, original_operation_id:get("inverse_of")?,
            original_version_id:get("original_version_id")?, container_sha256:get("original_container_sha256")?,
            container_length:self.record.plan["original_container_length"].as_u64().ok_or(EngineError::Conflict)?, resource_key:resource,
            timestamp_millis:SystemTime::now().duration_since(UNIX_EPOCH).map_err(io::Error::other)?.as_millis() as u64};
        let hook=engine.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
        if hook.managed_account_id().map_err(EngineError::HistoryCapture)?!=binding.account_id
            || hook.managed_actor_id().map_err(EngineError::HistoryCapture)?!=binding.actor_id
            || !hook.managed_worktree_capture_ready(root_id,deadline).map_err(EngineError::HistoryCapture)? {return Err(EngineError::Conflict)}
        let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_inverse_history_issued","action_id":binding.action_id}));
        self.journal.write(&next)?;self.record=next;
        let prepared=hook.prepare_worktree_inverse_history(&binding,deadline).map_err(EngineError::HistoryCapture)?;
        if prepared["action_id"]!=binding.action_id || prepared["state"]!="BeforeDurable" || prepared["resource_key"]!=binding.resource_key
            || prepared.pointer("/coverage/metadata/source_worktree_tree_v2/original_version_id").and_then(serde_json::Value::as_str)!=Some(binding.original_version_id.as_str())
            || std::time::Instant::now()>=deadline {return Err(EngineError::Conflict)}
        let mut next=self.record.clone();next.history_prepare=Some(prepared);next.phase=ManagedJournalPhase::HistoryPrepared;
        self.journal.write(&next)?;self.record=next;Ok(())
    }
}

#[derive(Clone,Debug,Deserialize,Serialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct PrivateIdentity {device:String,inode:String}
#[derive(Clone,Debug,Deserialize,Serialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct PrivateFileWitness {device:String,inode:String,size:u64,sha256:String}
#[derive(Clone,Debug,Deserialize,Serialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
pub(crate) struct MarkerWitness {device:String,inode:String,size:u64,sha256:String,bytes_base64:String}
#[derive(Clone,Debug,Deserialize,Serialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
pub(crate) struct RegistrationWitness {private_identity:PrivateIdentity,marker:MarkerWitness,admin_name:String,admin_identity:PrivateIdentity,backlink:PrivateFileWitness,head:PrivateFileWitness}
fn canonical_u64(value:&str)->io::Result<u64> {
    let parsed=value.parse::<u64>().map_err(io::Error::other)?;
    if parsed.to_string()!=value {return Err(io::Error::other("worktree identity unavailable"));}Ok(parsed)
}
fn check_private_identity(identity:&PrivateIdentity)->io::Result<(u64,u64)> {
    let device=canonical_u64(&identity.device)?;let inode=canonical_u64(&identity.inode)?;
    if inode==0{return Err(io::Error::other("worktree identity unavailable"));}Ok((device,inode))
}
fn check_private_file(file:&PrivateFileWitness)->io::Result<()> {
    check_private_identity(&PrivateIdentity{device:file.device.clone(),inode:file.inode.clone()})?;
    if file.size==0 || file.size>4096 || !valid_hex(&file.sha256,64) {return Err(io::Error::other("worktree file witness unavailable"));}Ok(())
}
#[derive(Clone,Debug,Deserialize,Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct InverseGitLocks {head:PrivateIdentity,backlink:PrivateIdentity,index:PrivateIdentity}
#[derive(Clone,Debug,Deserialize,Serialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
pub(crate) struct InverseGitAdminWitness {admin_name:String,admin_identity:PrivateIdentity,common_git_identity:PrivateIdentity,worktrees_identity:PrivateIdentity,
    #[serde(rename="targetOID")] target_oid:String,#[serde(rename="treeOID")] tree_oid:String,head:PrivateFileWitness,backlink:PrivateFileWitness,index:PrivateFileWitness,locks:InverseGitLocks}
fn verify_inverse_admin_lock(admin:&File,name:&str,identity:&PrivateIdentity)->io::Result<()> {
    let component=CString::new(name).map_err(io::Error::other)?;let fd=unsafe{libc::openat(admin.as_raw_fd(),component.as_ptr(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK)};
    if fd<0{return Err(io::Error::last_os_error())}let file=unsafe{File::from_raw_fd(fd)};let meta=file.metadata()?;let expected=check_private_identity(identity)?;
    let mut named:libc::stat=unsafe{std::mem::zeroed()};if !meta.is_file() || meta.nlink()!=1 || meta.len()!=0 || (meta.dev(),meta.ino())!=expected
        || unsafe{libc::fstatat(admin.as_raw_fd(),component.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || (named.st_dev as u64,named.st_ino as u64)!=expected {return Err(io::Error::other("worktree Git lock changed"))}Ok(())
}
fn verify_inverse_admin_file(admin:&File,name:&str,witness:&PrivateFileWitness,limit:u64,deadline:std::time::Instant)->io::Result<()> {
    let component=CString::new(name).map_err(io::Error::other)?;let fd=unsafe{libc::openat(admin.as_raw_fd(),component.as_ptr(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK)};
    if fd<0{return Err(io::Error::last_os_error())}let mut file=unsafe{File::from_raw_fd(fd)};let before=file.metadata()?;
    let expected=check_private_identity(&PrivateIdentity{device:witness.device.clone(),inode:witness.inode.clone()})?;
    if !before.is_file() || before.nlink()!=1 || (before.dev(),before.ino())!=expected || before.len()!=witness.size || before.len()==0 || before.len()>limit || !valid_digest(&witness.sha256) {return Err(io::Error::other("worktree Git file changed"))}
    let mut digest=Sha256::new();let mut count=0u64;let mut chunk=vec![0u8;CHUNK];loop {if std::time::Instant::now()>=deadline{return Err(io::Error::other("worktree Git deadline"))}let n=file.read(&mut chunk)?;if n==0{break}count+=n as u64;if count>limit{return Err(io::Error::other("worktree Git file changed"))}digest.update(&chunk[..n]);}
    let after=file.metadata()?;let mut named:libc::stat=unsafe{std::mem::zeroed()};
    if count!=witness.size || format!("{:x}",digest.finalize())!=witness.sha256 || (before.dev(),before.ino(),before.len(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())!=(after.dev(),after.ino(),after.len(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec())
        || unsafe{libc::fstatat(admin.as_raw_fd(),component.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || (named.st_dev as u64,named.st_ino as u64)!=expected {return Err(io::Error::other("worktree Git file changed"))}Ok(())
}

#[derive(Clone,Debug,Deserialize,Serialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
pub(crate) struct PublicationLockWitness {admin_name:String,admin_identity:PrivateIdentity,lock_identity:PrivateIdentity,#[serde(rename="candidateSHA256")] candidate_sha256:String,backlink:PrivateFileWitness}
#[derive(Clone,Debug,Deserialize,Serialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
pub(crate) struct BacklinkCommittedWitness {admin_name:String,admin_identity:PrivateIdentity,backlink:PrivateFileWitness,head:PrivateFileWitness}
impl WorktreeStageWriter {
    fn verify_private(&self,private:&PrivateWorktreeDirectory)->io::Result<()> {
        let now=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&private.path)?;
        if managed_object_identity(&private.file)?!=private.identity || managed_object_identity(&now)?!=private.identity {return Err(io::Error::other("worktree private directory changed"));}Ok(())
    }
    pub(crate) fn registration_issued(&mut self,private:&PrivateWorktreeDirectory)->io::Result<()> {
        if self.failed {return Err(io::Error::other("worktree registration unavailable"));}
        let result=(|| {self.verify_private(private)?;if self.record.preparation.iter().any(|v|v["kind"]=="worktree_registration_issued") {return Err(io::Error::other("worktree registration already issued"));}
            let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_registration_issued","private_identity":private.identity}));self.journal.write(&next)?;self.record=next;Ok(())})();
        if result.is_err(){self.failed=true;}result
    }
    pub(crate) fn index_issued(&mut self,private:&PrivateWorktreeDirectory,target_oid:&str)->io::Result<()> {
        if self.failed {return Err(io::Error::other("worktree index unavailable"));}
        let result=(|| {self.verify_private(private)?;if target_oid!=self.plan.commit_oid || !self.record.preparation.iter().any(|v|v["kind"]=="worktree_registration_accepted") || self.record.preparation.iter().any(|v|v["kind"]=="worktree_index_intent") {return Err(io::Error::other("worktree index unavailable"));}
            let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_index_intent","target_oid":target_oid,"private_identity":private.identity}));self.journal.write(&next)?;self.record=next;Ok(())})();if result.is_err(){self.failed=true;}result
    }
    pub(crate) fn accept_index(&mut self,private:&PrivateWorktreeDirectory,index:&PrivateFileWitness)->io::Result<()> {
        if self.failed {return Err(io::Error::other("worktree index unavailable"));}
        let result=(|| {self.verify_private(private)?;check_private_identity(&PrivateIdentity{device:index.device.clone(),inode:index.inode.clone()})?;
            if index.size==0 || index.size>8*1024*1024 || !valid_hex(&index.sha256,64) || !self.record.preparation.iter().any(|v|v["kind"]=="worktree_index_intent" && v["private_identity"]==private.identity) || self.record.preparation.iter().any(|v|v["kind"]=="worktree_index_accepted") {return Err(io::Error::other("worktree index unavailable"));}
            let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_index_accepted","witness":index}));self.journal.write(&next)?;self.record=next;Ok(())})();if result.is_err(){self.failed=true;}result
    }
    pub(crate) fn accept_registration(&mut self,private:&PrivateWorktreeDirectory,witness:&RegistrationWitness)->io::Result<()> {
        if self.failed {return Err(io::Error::other("worktree registration unavailable"));}
        let result=(|| {
            self.verify_private(private)?;let meta=private.file.metadata()?;let (dev,ino)=check_private_identity(&witness.private_identity)?;
            if (meta.dev(),meta.ino())!=(dev,ino) || !self.record.preparation.iter().any(|v|v["kind"]=="worktree_registration_issued" && v["private_identity"]==private.identity)
                || self.record.preparation.iter().any(|v|v["kind"]=="worktree_registration_accepted") {return Err(io::Error::other("worktree registration unavailable"));}
            check_private_identity(&witness.admin_identity)?;check_private_file(&witness.backlink)?;check_private_file(&witness.head)?;
            if witness.admin_name.is_empty() || witness.admin_name.len()>255 || matches!(witness.admin_name.as_str(),"."|"..") || witness.admin_name.contains('/') || witness.admin_name.contains('\\') || witness.admin_name.chars().any(|c|(c as u32)<32 || c=='\u{7f}') {return Err(io::Error::other("worktree admin unavailable"));}
            let marker=&witness.marker;let identity=PrivateIdentity{device:marker.device.clone(),inode:marker.inode.clone()};let (mdev,mino)=check_private_identity(&identity)?;
            if marker.size==0 || marker.size>4096 || !valid_hex(&marker.sha256,64) || marker.bytes_base64.len()>5464 {return Err(io::Error::other("worktree marker unavailable"));}
            let expected=base64::engine::general_purpose::STANDARD.decode(&marker.bytes_base64).map_err(io::Error::other)?;
            if expected.len()!=marker.size as usize || base64::engine::general_purpose::STANDARD.encode(&expected)!=marker.bytes_base64 || format!("{:x}",Sha256::digest(&expected))!=marker.sha256 {return Err(io::Error::other("worktree marker unavailable"));}
            let name=CString::new(".git").map_err(io::Error::other)?;let raw=unsafe{libc::openat(private.file.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK)};
            if raw<0{return Err(io::Error::last_os_error());}let mut file=unsafe{File::from_raw_fd(raw)};let before=file.metadata()?;
            if !before.is_file() || before.nlink()!=1 || !matches!(before.mode()&0o777,0o600|0o644) || (before.dev(),before.ino(),before.len())!=(mdev,mino,marker.size) {return Err(io::Error::other("worktree marker changed"));}
            let mut bytes=Vec::new();(&mut file).take(4097).read_to_end(&mut bytes)?;let after=file.metadata()?;
            if bytes!=expected || (before.dev(),before.ino(),before.len(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())!=(after.dev(),after.ino(),after.len(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()) {return Err(io::Error::other("worktree marker changed"));}
            let mut named:libc::stat=unsafe{std::mem::zeroed()};
            if unsafe{libc::fstatat(private.file.as_raw_fd(),name.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=mdev || named.st_ino as u64!=mino || (named.st_mode & libc::S_IFMT)!=libc::S_IFREG {return Err(io::Error::other("worktree marker namespace changed"));}
            self.verify_private(private)?;let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_registration_accepted","witness":witness}));self.journal.write(&next)?;self.record=next;Ok(())
        })();if result.is_err(){self.failed=true;}result
    }
}
impl WorktreeStageWriter {
    pub(crate) fn check_binding(&self,engine:&FileEngine,scope:&AppScope,owner:&str,args:&Binding,target:&Path)->Result<(),EngineError> {
        self.check_binding_inner(engine,scope,owner,args,target,false)
    }
    pub(crate) fn check_binding_after_publication(&self,engine:&FileEngine,scope:&AppScope,owner:&str,args:&Binding,target:&Path)->Result<(),EngineError> {
        if !self.has_published(){return Err(EngineError::Conflict)}
        self.check_binding_inner(engine,scope,owner,args,target,true)
    }
    pub(crate) fn has_published(&self)->bool {self.record.commit_progress.iter().any(|v|v["kind"]=="worktree_publish_done")}
    fn check_binding_inner(&self,engine:&FileEngine,scope:&AppScope,owner:&str,args:&Binding,target:&Path,after_publication:bool)->Result<(),EngineError> {
        if self.record.owner_id!=owner || self.record.operation_id!=args.operation_id || self.record.request_digest!=args.request_digest
            || self.record.plan["git_plan_digest"]!=args.git_plan_digest || self.record.plan["root_id"]!=args.root_id
            || self.record.plan["root_object_tag"]!=args.root_object_tag || self.record.plan["policy_generation"].as_u64()!=Some(scope.generation)
            || self.record.plan["root_path"].as_str()!=target.to_str() {return Err(EngineError::Conflict)}
        validate_root(engine,scope,target,&args.root_id)?;
        let hook=engine.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
        if hook.managed_account_id().map_err(EngineError::HistoryCapture)?!=self.record.plan["account_id"].as_str().ok_or(EngineError::Conflict)?
            || hook.managed_authorize_root(target).map_err(EngineError::HistoryCapture)?!=(args.root_id.clone(),target.to_path_buf()) {return Err(EngineError::Conflict)}
        let root=open_managed_directory(&self.record,target)?;
        if managed_object_identity(&root)?!=self.record.plan["root_identity"] || (!after_publication && crate::service::ObjectIdentity::from_file(&root)?.opaque_tag()!=args.root_object_tag) {return Err(EngineError::Conflict)}
        Ok(())
    }
    /// Create only after sealing, under the existing private journal directory.
    /// `target_parent` must already be authorized and pinned by the caller.
    /// Cross-device publication is deliberately unavailable in this profile.
    pub(crate) fn create_private_directory(&mut self,target_parent:&File,parent_path:&Path,destination_name:&str)->io::Result<PrivateWorktreeDirectory> {
        if self.failed {return Err(io::Error::other("worktree stage unavailable"));}
        let result=self.create_private_directory_inner(target_parent,parent_path,destination_name);if result.is_err(){self.failed=true;}result
    }
    fn create_private_directory_inner(&mut self,target_parent:&File,parent_path:&Path,destination_name:&str)->io::Result<PrivateWorktreeDirectory> {
        if !self.sealed || self.record.preparation.iter().any(|v|v["kind"].as_str().is_some_and(|k|k.starts_with("worktree_private_directory_"))) {return Err(io::Error::other("worktree private directory unavailable"));}
        let journal_dir=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&self.journal.directory)?;
        let parent=journal_dir.metadata()?;let target=target_parent.metadata()?;
        if !parent.is_dir() || parent.mode()&0o777!=0o700 || parent.uid()!=unsafe{libc::geteuid()} || !target.is_dir() || parent.dev()!=target.dev() {return Err(io::Error::other("worktree destination filesystem unavailable"));}
        if destination_name.is_empty() || destination_name.len()>255 || matches!(destination_name,"."|".."|".git"|".clanker"|".clankers"|".references") || destination_name.contains('/') || destination_name.contains('\\') || destination_name.chars().any(|c|(c as u32)<32 || c=='\u{7f}') {return Err(io::Error::other("worktree destination unavailable"));}
        let child=CString::new(destination_name.as_bytes()).map_err(io::Error::other)?;let mut child_stat:libc::stat=unsafe{std::mem::zeroed()};
        if unsafe{libc::fstatat(target_parent.as_raw_fd(),child.as_ptr(),&mut child_stat,libc::AT_SYMLINK_NOFOLLOW)}==0 || io::Error::last_os_error().raw_os_error()!=Some(libc::ENOENT) {return Err(io::Error::other("worktree destination occupied"));}
        let name=format!("{}.worktree-private",self.record.operation_id);let component=CString::new(name.as_bytes()).map_err(io::Error::other)?;
        let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_private_directory_intent","name":name,"parent_identity":managed_object_identity(&journal_dir)?,"destination_parent":parent_path,"destination_parent_identity":managed_object_identity(target_parent)?,"destination_name":destination_name}));self.journal.write(&next)?;self.record=next;
        if unsafe{libc::mkdirat(journal_dir.as_raw_fd(),component.as_ptr(),0o700)}!=0 {return Err(io::Error::last_os_error());}
        let raw=unsafe{libc::openat(journal_dir.as_raw_fd(),component.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
        if raw<0{return Err(io::Error::last_os_error());}let file=unsafe{File::from_raw_fd(raw)};let metadata=file.metadata()?;
        if !metadata.is_dir() || metadata.mode()&0o777!=0o700 || metadata.uid()!=unsafe{libc::geteuid()} || metadata.dev()!=target.dev() {return Err(io::Error::other("worktree private directory changed"));}
        let identity=managed_object_identity(&file)?;file.sync_all()?;journal_dir.sync_all()?;
        let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_private_directory_created","name":name,"identity":identity}));self.journal.write(&next)?;self.record=next;
        Ok(PrivateWorktreeDirectory{path:self.journal.directory.join(name),identity,file})
    }
    /// Materialize only the sealed inventory in the original private directory.
    /// One durable phase intent precedes all effects; interrupted work is
    /// status-only and is never resumed, adopted, cleaned or reissued.
    pub(crate) fn materialize_private(&mut self,private:&PrivateWorktreeDirectory,deadline:std::time::Instant)->io::Result<String> {
        if self.failed {return Err(io::Error::other("worktree stage unavailable"));}
        let result=self.materialize_private_inner(private,deadline);if result.is_err(){self.failed=true;}result
    }
    fn materialize_private_inner(&mut self,private:&PrivateWorktreeDirectory,deadline:std::time::Instant)->io::Result<String> {
        fn current(deadline:std::time::Instant)->io::Result<()> {if std::time::Instant::now()>=deadline {Err(io::Error::other("worktree materialization deadline"))}else{Ok(())}}
        fn open_parent(root:&File,path:&str,dirs:&std::collections::BTreeMap<String,serde_json::Value>)->io::Result<File> {
            let mut dir=root.try_clone()?;let mut prefix=String::new();
            if path.is_empty(){return Ok(dir)}
            for component in path.split('/') {if !prefix.is_empty(){prefix.push('/')}prefix.push_str(component);let name=CString::new(component).map_err(io::Error::other)?;
                let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
                if fd<0{return Err(io::Error::last_os_error())}let next=unsafe{File::from_raw_fd(fd)};
                if dirs.get(&prefix)!=Some(&managed_object_identity(&next)?) {return Err(io::Error::other("worktree directory changed"))}dir=next;
            }Ok(dir)
        }
        self.verify_private(private)?;current(deadline)?;
        let registration=self.record.preparation.iter().find(|v|v["kind"]=="worktree_registration_accepted").and_then(|v|v.get("witness")).cloned().ok_or_else(||io::Error::other("worktree registration unavailable"))?;
        if !self.record.preparation.iter().any(|v|v["kind"]=="worktree_index_accepted") || self.record.preparation.iter().any(|v|v["kind"].as_str().is_some_and(|k|k.starts_with("worktree_materialize_"))) {return Err(io::Error::other("worktree materialization already issued"));}
        let witness:RegistrationWitness=serde_json::from_value(registration).map_err(io::Error::other)?;
        let check_marker=||->io::Result<()> {
            let name=CString::new(".git").map_err(io::Error::other)?;let fd=unsafe{libc::openat(private.file.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK)};
            if fd<0{return Err(io::Error::last_os_error())}let mut file=unsafe{File::from_raw_fd(fd)};let meta=file.metadata()?;let expected=base64::engine::general_purpose::STANDARD.decode(&witness.marker.bytes_base64).map_err(io::Error::other)?;
            if !meta.is_file() || meta.nlink()!=1 || !matches!(meta.mode()&0o777,0o600|0o644) || meta.dev()!=canonical_u64(&witness.marker.device)? || meta.ino()!=canonical_u64(&witness.marker.inode)? || meta.len()!=witness.marker.size {return Err(io::Error::other("worktree marker changed"));}
            let mut bytes=Vec::new();(&mut file).take(4097).read_to_end(&mut bytes)?;if bytes!=expected || format!("{:x}",Sha256::digest(&bytes))!=witness.marker.sha256{return Err(io::Error::other("worktree marker changed"));}
            let mut named:libc::stat=unsafe{std::mem::zeroed()};if unsafe{libc::fstatat(private.file.as_raw_fd(),name.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=meta.dev() || named.st_ino as u64!=meta.ino(){return Err(io::Error::other("worktree marker changed"));}Ok(())
        };
        // The existing journal rewrites one bounded record. Reserve the
        // complete identity inventory before the first private-tree effect.
        let identity_bound=serde_json::json!({"kind":"unix","device":u64::MAX,"inode":u64::MAX});let mut maximum=self.record.clone();
        maximum.preparation.push(serde_json::json!({"kind":"worktree_materialize_intent","private_identity":private.identity,"target_manifest_sha256":target_digest(&self.dirs,&self.complete)?}));
        let bound_dirs:Vec<_>=self.dirs.iter().map(|path|serde_json::json!({"path":path,"identity":identity_bound})).collect();
        let bound_files:Vec<_>=self.complete.iter().map(|blob|serde_json::json!({"path":blob.path,"identity":identity_bound})).collect();
        maximum.preparation.push(serde_json::json!({"kind":"worktree_materialize_complete","target_manifest_sha256":"0".repeat(64),"directories":bound_dirs,"files":bound_files}));
        if serde_json::to_vec(&maximum).map_err(io::Error::other)?.len()>16*1024*1024-65536 {return Err(io::Error::other("worktree journal bound"));}drop(maximum);
        check_marker()?;let children:Vec<_>=fs::read_dir(format!("/proc/self/fd/{}",private.file.as_raw_fd()))?.take(2).collect::<Result<_,_>>()?;
        if children.len()!=1 || children[0].file_name()!=".git" {return Err(io::Error::other("worktree private directory occupied"));}
        let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_materialize_intent","private_identity":private.identity,"target_manifest_sha256":target_digest(&self.dirs,&self.complete)?}));self.journal.write(&next)?;self.record=next;
        let mut dirs=std::collections::BTreeMap::new();let mut files=std::collections::BTreeMap::new();
        for path in self.dirs.clone() {
            current(deadline)?;let (parent,name)=path.rsplit_once('/').unwrap_or(("",path.as_str()));let dir=open_parent(&private.file,parent,&dirs)?;let component=CString::new(name).map_err(io::Error::other)?;
            if unsafe{libc::mkdirat(dir.as_raw_fd(),component.as_ptr(),0o755)}!=0{return Err(io::Error::last_os_error())}
            let fd=unsafe{libc::openat(dir.as_raw_fd(),component.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};if fd<0{return Err(io::Error::last_os_error())}let created=unsafe{File::from_raw_fd(fd)};
            if unsafe{libc::fchmod(created.as_raw_fd(),0o755)}!=0{return Err(io::Error::last_os_error())}created.sync_all()?;dir.sync_all()?;let identity=managed_object_identity(&created)?;
            dirs.insert(path,identity);
        }
        let mut payload_offset=0u64;let mut buffer=vec![0u8;CHUNK];
        for index in 0..self.complete.len() {
            current(deadline)?;let blob=&self.complete[index];let path=blob.path.clone();let mode=blob.mode;let size=blob.size;let expected=blob.sha256.clone();let (parent,name)=path.rsplit_once('/').unwrap_or(("",path.as_str()));let dir=open_parent(&private.file,parent,&dirs)?;let component=CString::new(name).map_err(io::Error::other)?;
            let fd=unsafe{libc::openat(dir.as_raw_fd(),component.as_ptr(),libc::O_WRONLY|libc::O_CREAT|libc::O_EXCL|libc::O_NOFOLLOW|libc::O_CLOEXEC,mode)};if fd<0{return Err(io::Error::last_os_error())}let mut file=unsafe{File::from_raw_fd(fd)};let mut offset=0u64;let mut hash=Sha256::new();
            while offset<size {current(deadline)?;let count=((size-offset).min(CHUNK as u64)) as usize;self.file.read_exact_at(&mut buffer[..count],HEADER+payload_offset+offset)?;hash.update(&buffer[..count]);file.write_all(&buffer[..count])?;offset+=count as u64;}
            if format!("{:x}",hash.finalize())!=expected {return Err(io::Error::other("worktree staged blob changed"));}
            if unsafe{libc::fchmod(file.as_raw_fd(),mode)}!=0{return Err(io::Error::last_os_error())}file.sync_all()?;dir.sync_all()?;let metadata=file.metadata()?;
            if !metadata.is_file() || metadata.nlink()!=1 || metadata.len()!=size || metadata.mode()&0o777!=mode{return Err(io::Error::other("worktree file changed"));}
            let identity=managed_object_identity(&file)?;files.insert(path,(identity,index));payload_offset+=size;
        }
        for (path,(expected,index)) in &files {
            current(deadline)?;let (parent,name)=path.rsplit_once('/').unwrap_or(("",path.as_str()));let dir=open_parent(&private.file,parent,&dirs)?;let name=CString::new(name).map_err(io::Error::other)?;
            let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK)};if fd<0{return Err(io::Error::last_os_error())}let mut file=unsafe{File::from_raw_fd(fd)};
            if &managed_object_identity(&file)?!=expected{return Err(io::Error::other("worktree file identity changed"))}let blob=self.complete.get(*index).ok_or_else(||io::Error::other("worktree file unavailable"))?;let before=file.metadata()?;let mut hash=Sha256::new();let mut count=0u64;
            loop {current(deadline)?;let n=file.read(&mut buffer)?;if n==0{break}count+=n as u64;if count>blob.size{return Err(io::Error::other("worktree file changed"))}hash.update(&buffer[..n]);}
            let after=file.metadata()?;if count!=blob.size || format!("{:x}",hash.finalize())!=blob.sha256 || before.mode()&0o777!=blob.mode || before.nlink()!=1 || (before.dev(),before.ino(),before.len(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())!=(after.dev(),after.ino(),after.len(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()){return Err(io::Error::other("worktree file changed"));}
        }
        let mut expected_children:std::collections::BTreeMap<String,BTreeSet<std::ffi::OsString>>=std::collections::BTreeMap::new();
        expected_children.entry(String::new()).or_default().insert(".git".into());
        for path in dirs.keys().chain(files.keys()) {let (parent,name)=path.rsplit_once('/').unwrap_or(("",path.as_str()));expected_children.entry(parent.into()).or_default().insert(name.into());}
        for path in dirs.keys(){expected_children.entry(path.clone()).or_default();}
        for (path,expected) in expected_children {
            current(deadline)?;let directory=open_parent(&private.file,&path,&dirs)?;if !path.is_empty() && directory.metadata()?.mode()&0o777!=0o755{return Err(io::Error::other("worktree directory mode changed"));}
            let mut found=BTreeSet::new();for entry in fs::read_dir(format!("/proc/self/fd/{}",directory.as_raw_fd()))? {current(deadline)?;let entry=entry?;if found.len()>=expected.len() || !found.insert(entry.file_name()){return Err(io::Error::other("worktree directory changed"));}}
            if found!=expected{return Err(io::Error::other("worktree directory changed"));}
        }
        current(deadline)?;self.verify_private(private)?;check_marker()?;let digest=target_digest(&self.dirs,&self.complete)?;
        let directory_identities:Vec<_>=dirs.iter().map(|(path,identity)|serde_json::json!({"path":path,"identity":identity})).collect();
        let file_identities:Vec<_>=files.iter().map(|(path,(identity,_))|serde_json::json!({"path":path,"identity":identity})).collect();
        let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_materialize_complete","target_manifest_sha256":digest,"directories":directory_identities,"files":file_identities}));self.journal.write(&next)?;self.record=next;Ok(digest)
    }
    /// Seal the single History v2 container from already-verified stage bytes
    /// and the Source-accepted marker. No public state or History action is
    /// changed by this internal constructor.
    pub(crate) fn seal_history_container(&mut self,private:&PrivateWorktreeDirectory,deadline:std::time::Instant)->io::Result<(String,u64)> {
        if self.failed {return Err(io::Error::other("worktree stage unavailable"));}
        let result=self.seal_history_container_inner(private,deadline);if result.is_err(){self.failed=true;}result
    }
    fn seal_history_container_inner(&mut self,private:&PrivateWorktreeDirectory,deadline:std::time::Instant)->io::Result<(String,u64)> {
        let current=||if std::time::Instant::now()>=deadline {Err(io::Error::other("worktree container deadline"))}else{Ok(())};
        current()?;self.verify_private(private)?;
        if self.record.preparation.iter().any(|v|v["kind"].as_str().is_some_and(|k|k.starts_with("worktree_container_"))) || !self.record.preparation.iter().any(|v|v["kind"]=="worktree_materialize_complete") {return Err(io::Error::other("worktree container unavailable"));}
        let witness:RegistrationWitness=serde_json::from_value(self.record.preparation.iter().find(|v|v["kind"]=="worktree_registration_accepted").and_then(|v|v.get("witness")).cloned().ok_or_else(||io::Error::other("worktree registration unavailable"))?).map_err(io::Error::other)?;
        let marker=base64::engine::general_purpose::STANDARD.decode(&witness.marker.bytes_base64).map_err(io::Error::other)?;
        if marker.is_empty() || marker.len()>4096 || format!("{:x}",Sha256::digest(&marker))!=witness.marker.sha256 {return Err(io::Error::other("worktree marker unavailable"));}
        let marker_name=CString::new(".git").map_err(io::Error::other)?;let fd=unsafe{libc::openat(private.file.as_raw_fd(),marker_name.as_ptr(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK)};
        if fd<0{return Err(io::Error::last_os_error())}let mut marker_file=unsafe{File::from_raw_fd(fd)};let meta=marker_file.metadata()?;
        if !meta.is_file() || meta.nlink()!=1 || !matches!(meta.mode()&0o777,0o600|0o644) || meta.dev()!=canonical_u64(&witness.marker.device)? || meta.ino()!=canonical_u64(&witness.marker.inode)? || meta.len()!=marker.len() as u64{return Err(io::Error::other("worktree marker changed"));}
        let mut observed=Vec::new();(&mut marker_file).take(4097).read_to_end(&mut observed)?;if observed!=marker{return Err(io::Error::other("worktree marker changed"));}
        let mut named:libc::stat=unsafe{std::mem::zeroed()};if unsafe{libc::fstatat(private.file.as_raw_fd(),marker_name.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=meta.dev() || named.st_ino as u64!=meta.ino(){return Err(io::Error::other("worktree marker changed"));}
        let target=target_digest(&self.dirs,&self.complete)?;let mut nodes:BTreeMap<&str,serde_json::Value>=BTreeMap::new();
        for path in &self.dirs {nodes.insert(path,serde_json::json!({"kind":"directory","path":path,"mode":0o755}));}
        let mut offset=0u64;for blob in &self.complete {nodes.insert(&blob.path,serde_json::json!({"kind":"file","path":blob.path,"mode":blob.mode,"offset":offset,"length":blob.size,"sha256":blob.sha256}));offset=offset.checked_add(blob.size).ok_or_else(||io::Error::other("worktree size bound"))?;}
        if offset!=self.payload{return Err(io::Error::other("worktree payload changed"));}
        let mut entries:Vec<_>=nodes.into_values().collect();entries.push(serde_json::json!({"kind":"git_metadata","path":".git","mode":meta.mode()&0o777,"offset":offset,"length":marker.len(),"sha256":witness.marker.sha256,"owner":"source_worktree"}));
        let count=entries.len();let manifest=serde_json::json!({"version":2,"source":{"commit_oid":self.plan.commit_oid,"tree_oid":self.plan.tree_oid,"target_manifest_sha256":target},"entries":entries});
        let mut header=serde_json::to_vec(&manifest).map_err(io::Error::other)?;if header.len()>4*1024*1024{return Err(io::Error::other("worktree manifest bound"));}header.resize(4*1024*1024,b' ');
        let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_container_intent","identity":self.identity,"target_manifest_sha256":target,"entry_count":count}));self.journal.write(&next)?;self.record=next;
        current()?;self.file.write_all_at(b"OCKTREE2",0)?;self.file.write_all_at(&(4u32*1024*1024).to_be_bytes(),8)?;self.file.write_all_at(&header,12)?;self.file.write_all_at(&marker,HEADER+self.payload)?;
        let length=HEADER.checked_add(self.payload).and_then(|v|v.checked_add(marker.len() as u64)).ok_or_else(||io::Error::other("worktree size bound"))?;self.file.set_len(length)?;self.file.sync_all()?;
        let mut blob_offset=0u64;let mut buffer=vec![0u8;CHUNK];for blob in &self.complete {let mut hash=Sha256::new();let mut count=0u64;while count<blob.size {current()?;let n=((blob.size-count).min(CHUNK as u64)) as usize;self.file.read_exact_at(&mut buffer[..n],HEADER+blob_offset+count)?;hash.update(&buffer[..n]);count+=n as u64;}if format!("{:x}",hash.finalize())!=blob.sha256{return Err(io::Error::other("worktree stage changed"));}blob_offset+=blob.size;}
        let before=self.file.metadata()?;let mut full=Sha256::new();let mut position=0u64;while position<length {current()?;let n=((length-position).min(CHUNK as u64)) as usize;self.file.read_exact_at(&mut buffer[..n],position)?;full.update(&buffer[..n]);position+=n as u64;}
        let after=self.file.metadata()?;if managed_object_identity(&self.file)?!=self.identity || !before.is_file() || before.nlink()!=1 || before.len()!=length || (before.dev(),before.ino(),before.len(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())!=(after.dev(),after.ino(),after.len(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()){return Err(io::Error::other("worktree stage changed"));}
        let mut marker_after=vec![0u8;marker.len()];marker_file.read_exact_at(&mut marker_after,0)?;let later=marker_file.metadata()?;
        if marker_after!=marker || (meta.dev(),meta.ino(),meta.len(),meta.mtime(),meta.mtime_nsec(),meta.ctime(),meta.ctime_nsec())!=(later.dev(),later.ino(),later.len(),later.mtime(),later.mtime_nsec(),later.ctime(),later.ctime_nsec()) || unsafe{libc::fstatat(private.file.as_raw_fd(),marker_name.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=meta.dev() || named.st_ino as u64!=meta.ino(){return Err(io::Error::other("worktree marker changed"));}
        let digest=format!("{:x}",full.finalize());self.verify_private(private)?;
        let mut next=self.record.clone();next.preparation.push(serde_json::json!({"kind":"worktree_container_sealed","label":LABEL,"identity":self.identity,"sha256":digest,"length":length,"entry_count":count,"target_manifest_sha256":target,"marker_mode":meta.mode()&0o777}));self.journal.write(&next)?;self.record=next;Ok((digest,length))
    }
    fn verify_history_container(&self,deadline:std::time::Instant)->io::Result<()> {
        let sealed=self.record.preparation.iter().find(|v|v["kind"]=="worktree_container_sealed").ok_or_else(||io::Error::other("worktree container unavailable"))?;
        let length=sealed["length"].as_u64().filter(|n|*n<=HEADER+MAX_TOTAL).ok_or_else(||io::Error::other("worktree container bound"))?;let expected=sealed["sha256"].as_str().filter(|s|valid_hex(s,64)).ok_or_else(||io::Error::other("worktree container digest unavailable"))?;
        let before=self.file.metadata()?;if !before.is_file() || before.nlink()!=1 || before.len()!=length || sealed.get("identity")!=Some(&managed_object_identity(&self.file)?) {return Err(io::Error::other("worktree container changed"));}
        let mut hash=Sha256::new();let mut buffer=vec![0u8;CHUNK];let mut offset=0u64;while offset<length {if std::time::Instant::now()>=deadline{return Err(io::Error::other("worktree verification deadline"))}let n=((length-offset).min(CHUNK as u64)) as usize;self.file.read_exact_at(&mut buffer[..n],offset)?;hash.update(&buffer[..n]);offset+=n as u64;}
        let after=self.file.metadata()?;if format!("{:x}",hash.finalize())!=expected || (before.dev(),before.ino(),before.len(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())!=(after.dev(),after.ino(),after.len(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()){return Err(io::Error::other("worktree container changed"))}Ok(())
    }
    /// Recheck the exact recorded tree through a pinned root directory, whether
    /// still private or already published. No mutation or retry is performed.
    pub(crate) fn verify_materialized_tree(&self,root:&File,deadline:std::time::Instant)->io::Result<()> {
        self.verify_history_container(deadline)?;
        verify_materialized_record(&self.record,&self.dirs,&self.complete,root,deadline)
    }
    /// Prepare exactly one absent-folder History action before publication.
    /// The caller supplies only the retained slot and current Files scope; all
    /// action/provenance fields come from the immutable journal and sealed data.
    pub(crate) fn prepare_history(&mut self,private:&PrivateWorktreeDirectory,engine:&FileEngine,scope:&AppScope,deadline:std::time::Instant)->Result<(),EngineError> {
        if self.failed {return Err(EngineError::Conflict)}
        let result=self.prepare_history_inner(private,engine,scope,deadline);if result.is_err(){self.failed=true;}result
    }
    fn prepare_history_inner(&mut self,private:&PrivateWorktreeDirectory,engine:&FileEngine,scope:&AppScope,deadline:std::time::Instant)->Result<(),EngineError> {
        if self.record.phase!=ManagedJournalPhase::Prepared || self.record.history_prepare.is_some() {return Err(EngineError::Conflict)}
        let intent=self.record.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").cloned().ok_or(EngineError::Conflict)?;
        let parent_path=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);let name=intent["destination_name"].as_str().ok_or(EngineError::Conflict)?.to_owned();
        engine.authorize_stage_directory_scoped(scope,&parent_path)?;
        let parent=open_managed_directory(&self.record,&parent_path)?;if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&parent)?) {return Err(EngineError::Conflict)}
        let child=CString::new(name.as_bytes()).map_err(io::Error::other)?;let absent=|parent:&File|->io::Result<()> {let mut state:libc::stat=unsafe{std::mem::zeroed()};if unsafe{libc::fstatat(parent.as_raw_fd(),child.as_ptr(),&mut state,libc::AT_SYMLINK_NOFOLLOW)}==0 || io::Error::last_os_error().raw_os_error()!=Some(libc::ENOENT){return Err(io::Error::other("worktree destination changed"))}Ok(())};
        absent(&parent)?;let target=CaptureTarget{resource_id:parent_path.join(&name),before:None};
        let (digest,_length)=self.seal_history_container(private,deadline)?;
        let hook=engine.capture_hook.as_ref().ok_or(EngineError::Conflict)?;let root_id=self.record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();
        if hook.managed_account_id().map_err(EngineError::HistoryCapture)?!=self.record.plan["history_account_id"].as_str().ok_or(EngineError::Conflict)? || hook.managed_actor_id().map_err(EngineError::HistoryCapture)?!=self.record.plan["history_actor_id"].as_str().ok_or(EngineError::Conflict)? || hook.managed_authorize_root(&parent_path).map_err(EngineError::HistoryCapture)?.0!=root_id {return Err(EngineError::Conflict)}
        let binding=WorktreeCaptureBinding{operation_id:self.record.operation_id.clone(),root_id,commit_oid:self.plan.commit_oid.clone(),tree_oid:self.plan.tree_oid.clone(),target_manifest_sha256:target_digest(&self.dirs,&self.complete)?};
        let timestamp=self.record.plan["history_timestamp_millis"].as_u64().ok_or(EngineError::Conflict)?;let expected_action=self.record.plan["history_action_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();
        let start=self.record.preparation.len();let directories=vec![target.resource_id.clone()];
        let registrations={let mut checkpoint=self.journal.preparation_checkpoint_from(&mut self.record,start);hook.register_checkout_targets(std::slice::from_ref(&target),&binding.root_id,&binding.operation_id,&directories,&mut checkpoint).map_err(EngineError::HistoryCapture)?};
        let registry_digest=self.journal.freeze_registry_plan(&mut self.record,serde_json::Value::Array(registrations.clone()))?;
        let legacy = self.record.legacy_prepare_hook(hook.as_ref());
        let hook = legacy.as_deref().unwrap_or(hook.as_ref());
        let ticket={let mut checkpoint=self.journal.preparation_checkpoint_from(&mut self.record,start);hook.prepare_worktree_action(&target,&binding,&registrations,&registry_digest,timestamp,&mut checkpoint).map_err(EngineError::HistoryCapture)?};
        if ticket.action_id()!=expected_action || self.record.history_prepare.is_none() || std::time::Instant::now()>=deadline {return Err(EngineError::Conflict)}
        engine.authorize_stage_directory_scoped(scope,&parent_path)?;let after=open_managed_directory(&self.record,&parent_path)?;
        if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&after)?) {return Err(EngineError::Conflict)}absent(&after)?;self.verify_private(private)?;
        if !self.record.preparation.iter().any(|v|v["kind"]=="worktree_container_sealed" && v["sha256"]==digest) {return Err(EngineError::Conflict)}
        let mut next=self.record.clone();next.phase=ManagedJournalPhase::HistoryPrepared;self.journal.write(&next)?;self.record=next;Ok(())
    }
    fn checked_destination(&self,engine:&FileEngine,scope:&AppScope)->Result<(File,PathBuf,String),EngineError> {
        let intent=self.record.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;
        let parent_path=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);let name=intent["destination_name"].as_str().ok_or(EngineError::Conflict)?.to_owned();
        engine.authorize_stage_directory_scoped(scope,&parent_path)?;let parent=open_managed_directory(&self.record,&parent_path)?;
        if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&parent)?) {return Err(EngineError::Conflict)}
        let component=CString::new(name.as_bytes()).map_err(io::Error::other)?;let mut item:libc::stat=unsafe{std::mem::zeroed()};
        if unsafe{libc::fstatat(parent.as_raw_fd(),component.as_ptr(),&mut item,libc::AT_SYMLINK_NOFOLLOW)}==0 || io::Error::last_os_error().raw_os_error()!=Some(libc::ENOENT){return Err(EngineError::Conflict)}
        let path=parent_path.join(&name);let text=path.to_str().ok_or(EngineError::Conflict)?;
        if text.len()+6>4096 || text.chars().any(|c|(c as u32)<32 || c=='\u{7f}') {return Err(EngineError::Conflict)}
        Ok((parent,path,name))
    }
    /// Record Git-lock intent before the retained Source owner is invoked.
    pub(crate) fn begin_publication_lock(&mut self,private:&PrivateWorktreeDirectory,engine:&FileEngine,scope:&AppScope,deadline:std::time::Instant)->Result<(),EngineError> {
        if self.failed {return Err(EngineError::Conflict)}let result=(||{
            if self.record.phase!=ManagedJournalPhase::HistoryPrepared || self.record.commit_progress.iter().any(|v|v["kind"]=="worktree_backlink_lock_intent") {return Err(EngineError::Conflict)}
            self.verify_private(private)?;self.verify_materialized_tree(&private.file,deadline)?;let (_,path,_)=self.checked_destination(engine,scope)?;
            let hook=engine.capture_hook.as_ref().ok_or(EngineError::Conflict)?;let root_id=self.record.plan["root_id"].as_str().ok_or(EngineError::Conflict)?;
            if hook.managed_account_id().map_err(EngineError::HistoryCapture)?!=self.record.plan["history_account_id"].as_str().ok_or(EngineError::Conflict)? || hook.managed_actor_id().map_err(EngineError::HistoryCapture)?!=self.record.plan["history_actor_id"].as_str().ok_or(EngineError::Conflict)? || !hook.managed_worktree_capture_ready(root_id,deadline).map_err(EngineError::HistoryCapture)? {return Err(EngineError::Conflict)}
            let candidate=format!("{}/.git\n",path.to_str().ok_or(EngineError::Conflict)?);let sha=format!("{:x}",Sha256::digest(candidate.as_bytes()));
            let mut next=self.record.clone();next.phase=ManagedJournalPhase::Committing;self.journal.write(&next)?;self.record=next;
            let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_backlink_lock_intent","candidate_sha256":sha,"private_identity":private.identity}));self.journal.write(&next)?;self.record=next;Ok(())
        })();if result.is_err(){self.failed=true;}result
    }
    /// The trusted retained Host callback supplies only an observed Source lock;
    /// this is evidence tied to the original registration, not user authority.
    pub(crate) fn publication_lock_ready(&mut self,private:&PrivateWorktreeDirectory,witness:&PublicationLockWitness)->Result<(),EngineError> {
        if self.failed {return Err(EngineError::Conflict)}let result=(||{
            if self.record.phase!=ManagedJournalPhase::Committing || self.record.commit_progress.iter().any(|v|v["kind"]=="worktree_backlink_lock_ready") {return Err(EngineError::Conflict)}
            self.verify_private(private)?;let intent=self.record.commit_progress.iter().find(|v|v["kind"]=="worktree_backlink_lock_intent").ok_or(EngineError::Conflict)?;
            let registration:RegistrationWitness=serde_json::from_value(self.record.preparation.iter().find(|v|v["kind"]=="worktree_registration_accepted").and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
            check_private_identity(&witness.lock_identity)?;check_private_identity(&witness.admin_identity)?;check_private_file(&witness.backlink)?;
            if witness.admin_name!=registration.admin_name || witness.admin_identity!=registration.admin_identity || witness.backlink!=registration.backlink || !valid_hex(&witness.candidate_sha256,64) || intent["candidate_sha256"]!=witness.candidate_sha256 {return Err(EngineError::Conflict)}
            let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_backlink_lock_ready","witness":witness}));self.journal.write(&next)?;self.record=next;Ok(())
        })();if result.is_err(){self.failed=true;}result
    }
    /// Publish once, no-replace, only after History preparation and exact lock
    /// witness. A lost/error result leaves the original journal for inspection.
    pub(crate) fn publish_private(&mut self,private:&PrivateWorktreeDirectory,engine:&FileEngine,scope:&AppScope,deadline:std::time::Instant)->Result<(),EngineError> {
        use std::os::unix::ffi::OsStrExt;
        if self.failed {return Err(EngineError::Conflict)}let result=(||{
            if self.record.phase!=ManagedJournalPhase::Committing || !self.record.commit_progress.iter().any(|v|v["kind"]=="worktree_backlink_lock_ready") || self.record.commit_progress.iter().any(|v|v["kind"]=="worktree_publish_intent") {return Err(EngineError::Conflict)}
            self.verify_private(private)?;self.verify_materialized_tree(&private.file,deadline)?;let (destination,_path,name)=self.checked_destination(engine,scope)?;
            let journal=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&self.journal.directory)?;
            let initial=self.record.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;
            if initial.get("parent_identity")!=Some(&managed_object_identity(&journal)?) || destination.metadata()?.dev()!=journal.metadata()?.dev(){return Err(EngineError::Conflict)}
            let from=CString::new(private.path.file_name().ok_or(EngineError::Conflict)?.as_bytes()).map_err(io::Error::other)?;let to=CString::new(name.as_bytes()).map_err(io::Error::other)?;
            let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_publish_intent","private_identity":private.identity}));self.journal.write(&next)?;self.record=next;
            if std::time::Instant::now()>=deadline{return Err(EngineError::Conflict)}
            if unsafe{libc::renameat2(journal.as_raw_fd(),from.as_ptr(),destination.as_raw_fd(),to.as_ptr(),libc::RENAME_NOREPLACE)}!=0{return Err(io::Error::last_os_error().into())}
            journal.sync_all()?;destination.sync_all()?;let fd=unsafe{libc::openat(destination.as_raw_fd(),to.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
            if fd<0{return Err(io::Error::last_os_error().into())}let published=unsafe{File::from_raw_fd(fd)};if managed_object_identity(&published)?!=private.identity{return Err(EngineError::Conflict)}self.verify_materialized_tree(&published,deadline)?;
            let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_publish_done","identity":private.identity}));self.journal.write(&next)?;self.record=next;Ok(())
        })();if result.is_err(){self.failed=true;}result
    }
    pub(crate) fn check_published(&mut self,private:&PrivateWorktreeDirectory,engine:&FileEngine,scope:&AppScope,deadline:std::time::Instant)->Result<(),EngineError> {
        if self.failed {return Err(EngineError::Conflict)}let result=(||{
            if self.record.phase!=ManagedJournalPhase::Committing || !self.record.commit_progress.iter().any(|v|v["kind"]=="worktree_publish_done") || self.record.commit_progress.iter().any(|v|v["kind"]=="worktree_backlink_committed") {return Err(EngineError::Conflict)}
            let intent=self.record.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").ok_or(EngineError::Conflict)?;
            let path=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);let name=intent["destination_name"].as_str().ok_or(EngineError::Conflict)?;
            engine.authorize_stage_directory_scoped(scope,&path)?;let parent=open_managed_directory(&self.record,&path)?;if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&parent)?) {return Err(EngineError::Conflict)}
            let component=CString::new(name.as_bytes()).map_err(io::Error::other)?;let fd=unsafe{libc::openat(parent.as_raw_fd(),component.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};if fd<0{return Err(io::Error::last_os_error().into())}
            let published=unsafe{File::from_raw_fd(fd)};if managed_object_identity(&published)?!=private.identity{return Err(EngineError::Conflict)}self.verify_materialized_tree(&published,deadline)?;
            let mut named:libc::stat=unsafe{std::mem::zeroed()};let meta=published.metadata()?;if unsafe{libc::fstatat(parent.as_raw_fd(),component.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=meta.dev() || named.st_ino as u64!=meta.ino(){return Err(EngineError::Conflict)}Ok(())
        })();if result.is_err(){self.failed=true;}result
    }
    /// Complete only after the original Source callback positively observed the
    /// exact Git lock commit and the published Files tree still matches.
    pub(crate) fn finish_published_history(&mut self,private:&PrivateWorktreeDirectory,engine:&FileEngine,scope:&AppScope,witness:&BacklinkCommittedWitness,deadline:std::time::Instant)->Result<(),EngineError> {
        if self.failed {return Err(EngineError::Conflict)}let result=(||{
            if self.record.phase!=ManagedJournalPhase::Committing || !self.record.commit_progress.iter().any(|v|v["kind"]=="worktree_publish_done") || self.record.commit_progress.iter().any(|v|v["kind"]=="worktree_backlink_committed") {return Err(EngineError::Conflict)}
            let lock:PublicationLockWitness=serde_json::from_value(self.record.commit_progress.iter().find(|v|v["kind"]=="worktree_backlink_lock_ready").and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
            let registration:RegistrationWitness=serde_json::from_value(self.record.preparation.iter().find(|v|v["kind"]=="worktree_registration_accepted").and_then(|v|v.get("witness")).cloned().ok_or(EngineError::Conflict)?).map_err(io::Error::other)?;
            check_private_identity(&witness.admin_identity)?;check_private_file(&witness.backlink)?;check_private_file(&witness.head)?;
            if witness.admin_name!=lock.admin_name || witness.admin_identity!=lock.admin_identity || witness.head!=registration.head || witness.backlink.device!=lock.lock_identity.device || witness.backlink.inode!=lock.lock_identity.inode || witness.backlink.sha256!=lock.candidate_sha256 {return Err(EngineError::Conflict)}
            let intent=self.record.preparation.iter().find(|v|v["kind"]=="worktree_private_directory_intent").cloned().ok_or(EngineError::Conflict)?;
            let parent_path=PathBuf::from(intent["destination_parent"].as_str().ok_or(EngineError::Conflict)?);let name=intent["destination_name"].as_str().ok_or(EngineError::Conflict)?;
            engine.authorize_stage_directory_scoped(scope,&parent_path)?;let parent=open_managed_directory(&self.record,&parent_path)?;if intent.get("destination_parent_identity")!=Some(&managed_object_identity(&parent)?) {return Err(EngineError::Conflict)}
            let target=parent_path.join(name);let expected=format!("{}/.git\n",target.to_str().ok_or(EngineError::Conflict)?);
            if witness.backlink.size!=expected.len() as u64 || format!("{:x}",Sha256::digest(expected.as_bytes()))!=witness.backlink.sha256 {return Err(EngineError::Conflict)}
            let component=CString::new(name.as_bytes()).map_err(io::Error::other)?;let fd=unsafe{libc::openat(parent.as_raw_fd(),component.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
            if fd<0{return Err(io::Error::last_os_error().into())}let published=unsafe{File::from_raw_fd(fd)};if managed_object_identity(&published)?!=private.identity{return Err(EngineError::Conflict)}self.verify_materialized_tree(&published,deadline)?;
            let mut named:libc::stat=unsafe{std::mem::zeroed()};let pm=published.metadata()?;if unsafe{libc::fstatat(parent.as_raw_fd(),component.as_ptr(),&mut named,libc::AT_SYMLINK_NOFOLLOW)}!=0 || named.st_dev as u64!=pm.dev() || named.st_ino as u64!=pm.ino(){return Err(EngineError::Conflict)}
            let sealed=self.record.preparation.iter().find(|v|v["kind"]=="worktree_container_sealed").cloned().ok_or(EngineError::Conflict)?;
            let sha=sealed["sha256"].as_str().ok_or(EngineError::Conflict)?;let length=sealed["length"].as_u64().ok_or(EngineError::Conflict)?;let count=sealed["entry_count"].as_u64().ok_or(EngineError::Conflict)?;
            let prepared=self.record.history_prepare.as_ref().ok_or(EngineError::Conflict)?;let entries=prepared["entries"].as_array().filter(|v|v.len()==1).ok_or(EngineError::Conflict)?;let entry=&entries[0];
            if entry["existence"]!="Absent" || entry["resource_type"]!="Directory" {return Err(EngineError::Conflict)}
            let key=entry.get("resource_key").cloned().ok_or(EngineError::Conflict)?;let locator=entry.get("new_locator").cloned().ok_or(EngineError::Conflict)?;
            let action=self.record.plan["history_action_id"].as_str().ok_or(EngineError::Conflict)?.to_owned();let fingerprint=format!("sha256:{sha}:{length}");
            let live=serde_json::json!({"action_id":action,"status":"Committed","fingerprint":fingerprint,"committed_resources":[{"resource_key":key,"locator":locator,"existence":"Present","resource_type":"Directory","metadata":{"mode":pm.mode()&0o777,"size":null,"modified_millis":null,"opaque":null},"fingerprint":fingerprint,"coverage":{"byte_len":length,"content_digest":fingerprint,"metadata_digest":null,"metadata":{"source_worktree_tree_v2_after":{"journal_operation_id":self.record.operation_id,"container_sha256":sha,"container_length":length,"entry_count":count}}}}]});
            let mut next=self.record.clone();next.commit_progress.push(serde_json::json!({"kind":"worktree_backlink_committed","witness":witness}));self.journal.write(&next)?;self.record=next;
            let mut next=self.record.clone();next.phase=ManagedJournalPhase::LiveCommitted;next.history_completion=Some(serde_json::json!({"worktree_stage":{"label":LABEL,"identity":self.identity,"sha256":sha,"length":length},"live":live}));self.journal.write(&next)?;self.record=next;
            let mut next=self.record.clone();next.phase=ManagedJournalPhase::HistoryPending;self.journal.write(&next)?;self.record=next;
            let hook=engine.capture_hook.as_ref().ok_or(EngineError::Conflict)?;
            let stage=self.journal.worktree_history_stage(&self.record.owner_id,&action,&self.record.request_digest,deadline)?;
            hook.finish_worktree_history(&stage,&live,deadline).map_err(EngineError::HistoryCapture)?;drop(stage);
            let mut next=self.record.clone();next.phase=ManagedJournalPhase::Complete;self.journal.write(&next)?;self.record=next;Ok(())
        })();if result.is_err(){self.failed=true;}result
    }
    pub(crate) fn private_destination_parent(&self,engine:&FileEngine,scope:&AppScope,parent:&Path,object_tag:&str)->Result<File,EngineError> {
        if !valid_hex(object_tag,64) {return Err(EngineError::Conflict)}
        engine.authorize_stage_directory_scoped(scope,parent)?;
        let file=open_managed_directory(&self.record,parent)?;
        if crate::service::ObjectIdentity::from_file(&file)?.opaque_tag()!=object_tag {return Err(EngineError::Conflict)}
        Ok(file)
    }
    pub(crate) fn cursor(&self)->(usize,u64,bool) {(self.index,self.offset,self.sealed)}
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::FileExt;
    fn record(journal:&ManagedJournal)->ManagedJournalRecord {
        ManagedJournalRecord {version:1,operation_id:journal.operation_id.clone(),owner_id:"owner".into(),request_digest:"a".repeat(64),phase:ManagedJournalPhase::Prepared,
            plan:serde_json::json!({"kind":"source_worktree_create_v2","source":{"commit_oid":"a".repeat(40),"tree_oid":"b".repeat(40),"entries":[{"path":"nested/binary","mode":0o100755,"oid":"c".repeat(40),"size":3},{"path":"zero","mode":0o100644,"oid":"d".repeat(40),"size":0}]}}),
            file_stages:vec![],directory_events:vec![],commit_progress:vec![],cleanup_progress:vec![],preparation:vec![],registry_plan:None,history_prepare:None,history_completion:None,review_outcome:None}
    }
    #[test]
    fn binary_stage_seals_and_interrupted_writer_cannot_be_rearmed() {
        let temp=tempfile::tempdir().unwrap();let journal=ManagedJournal::open(&temp.path().join("complete"),"fixture-worktree-00001").unwrap();journal.write(&record(&journal)).unwrap();
        let mut writer=WorktreeStageWriter::begin(journal,"owner",&"a".repeat(64)).unwrap();
        writer.write_chunk(0,0,&[0,255]).unwrap();writer.write_chunk(0,2,&[1]).unwrap();
        let file_hash=format!("{:x}",Sha256::digest([0,255,1]));writer.finish_blob(0,&"c".repeat(40),&file_hash).unwrap();writer.finish_blob(1,&"d".repeat(40),&format!("{:x}",Sha256::digest([]))).unwrap();
        let target=writer.seal_target().unwrap();assert_eq!(target,"cd8231e8764bbb73920c6ff1327099ef343a8137a45381479ec778a6aba31410");let mut bytes=[0u8;3];writer.file.read_exact_at(&mut bytes,HEADER).unwrap();assert_eq!(bytes,[0,255,1]);
        let target_parent=File::open(temp.path()).unwrap();let private=writer.create_private_directory(&target_parent,temp.path(),"published").unwrap();
        assert!(private.path.is_dir());assert_eq!(managed_object_identity(&private.file).unwrap(),private.identity);
        writer.registration_issued(&private).unwrap();let marker=b"gitdir: /private/admin\n";fs::write(private.path.join(".git"),marker).unwrap();
        let meta=fs::metadata(private.path.join(".git")).unwrap();let pm=private.file.metadata().unwrap();let f=serde_json::json!({"device":meta.dev().to_string(),"inode":meta.ino().to_string(),"size":marker.len(),"sha256":format!("{:x}",Sha256::digest(marker))});
        let mut marker_value=f.clone();marker_value["bytesBase64"]=serde_json::json!(base64::engine::general_purpose::STANDARD.encode(marker));
        let witness:RegistrationWitness=serde_json::from_value(serde_json::json!({"privateIdentity":{"device":pm.dev().to_string(),"inode":pm.ino().to_string()},"marker":marker_value,"adminName":"admin","adminIdentity":{"device":"1","inode":"2"},"backlink":f,"head":f})).unwrap();
        writer.accept_registration(&private,&witness).unwrap();writer.index_issued(&private,&"a".repeat(40)).unwrap();let index:PrivateFileWitness=serde_json::from_value(f.clone()).unwrap();writer.accept_index(&private,&index).unwrap();assert_eq!(writer.materialize_private(&private,std::time::Instant::now()+std::time::Duration::from_secs(30)).unwrap(),target);
        assert_eq!(fs::read(private.path.join("nested/binary")).unwrap(),[0,255,1]);assert_eq!(fs::read(private.path.join("zero")).unwrap(),b"");
        let (container_sha,container_len)=writer.seal_history_container(&private,std::time::Instant::now()+std::time::Duration::from_secs(30)).unwrap();
        let mut container=vec![0;container_len as usize];writer.file.read_exact_at(&mut container,0).unwrap();assert_eq!(&container[..8],b"OCKTREE2");assert_eq!(format!("{:x}",Sha256::digest(&container)),container_sha);
        let header_len=u32::from_be_bytes(container[8..12].try_into().unwrap()) as usize;let manifest:serde_json::Value=serde_json::from_slice(&container[12..12+header_len]).unwrap();assert_eq!(manifest["source"]["target_manifest_sha256"],target);assert_eq!(manifest["entries"].as_array().unwrap().last().unwrap()["owner"],"source_worktree");assert_eq!(manifest["entries"].as_array().unwrap().last().unwrap()["mode"],meta.mode()&0o777);
        writer.verify_materialized_tree(&private.file,std::time::Instant::now()+std::time::Duration::from_secs(30)).unwrap();
        use std::os::unix::fs::PermissionsExt;let changed_mode=if meta.mode()&0o777==0o600 {0o644}else{0o600};fs::set_permissions(private.path.join(".git"),fs::Permissions::from_mode(changed_mode)).unwrap();assert!(writer.verify_materialized_tree(&private.file,std::time::Instant::now()+std::time::Duration::from_secs(30)).is_err());fs::set_permissions(private.path.join(".git"),fs::Permissions::from_mode(meta.mode()&0o777)).unwrap();
        fs::write(private.path.join("nested/binary"),[9,9,9]).unwrap();assert!(writer.verify_materialized_tree(&private.file,std::time::Instant::now()+std::time::Duration::from_secs(30)).is_err());
        assert!(writer.seal_history_container(&private,std::time::Instant::now()+std::time::Duration::from_secs(30)).is_err());
        let durable=writer.journal.read().unwrap().unwrap();assert!(durable.preparation.iter().any(|v|v["kind"]=="worktree_stage_target_sealed" && v["target_manifest_sha256"]==target));
        drop(writer);let journal=ManagedJournal::open_existing(&temp.path().join("complete"),"fixture-worktree-00001",true).unwrap().unwrap();assert!(WorktreeStageWriter::begin(journal,"owner",&"a".repeat(64)).is_err());
        let failed=ManagedJournal::open(&temp.path().join("failed"),"fixture-worktree-00002").unwrap();failed.write(&record(&failed)).unwrap();let mut writer=WorktreeStageWriter::begin(failed,"owner",&"a".repeat(64)).unwrap();
        assert!(writer.write_chunk(0,1,&[7]).is_err());assert!(writer.write_chunk(0,0,&[0,255,1]).is_err());drop(writer);
        let failed=ManagedJournal::open_existing(&temp.path().join("failed"),"fixture-worktree-00002",true).unwrap().unwrap();assert!(WorktreeStageWriter::begin(failed,"owner",&"a".repeat(64)).is_err());
        let drift=ManagedJournal::open(&temp.path().join("drift"),"fixture-worktree-00003").unwrap();drift.write(&record(&drift)).unwrap();let mut writer=WorktreeStageWriter::begin(drift,"owner",&"a".repeat(64)).unwrap();
        writer.write_chunk(0,0,&[0,255,1]).unwrap();writer.finish_blob(0,&"c".repeat(40),&file_hash).unwrap();writer.finish_blob(1,&"d".repeat(40),&format!("{:x}",Sha256::digest([]))).unwrap();writer.file.write_all_at(&[7],HEADER).unwrap();
        assert!(writer.seal_target().is_err());assert!(writer.seal_target().is_err());
        let oversized=SourcePlan{commit_oid:"a".repeat(40),tree_oid:"b".repeat(40),entries:(0..8000).map(|n|SourceEntry{path:format!("{n:05}{}","x".repeat(600)),mode:0o100644,oid:"c".repeat(40),size:0}).collect()};
        assert!(validate_plan(&oversized).is_err(),"oversized manifest must fail before stage allocation");
    }
}
