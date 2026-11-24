//! Offline C3 validation. A checkpoint is progress, never a readiness marker.
use crate::{catalog::{action_resources,change_scope,ordered_keys,origin_timeline_keys,ActionRecord,ActionState,LiveStatus,ResourceKey,VersionContent,VersionReceipt}, migration_copy::{candidate_path,digest,prepare,validate_candidate,verify_reference,Inputs}, retention::{ExpiryRecord,ExpiryState,PersistedReservation,PolicySet}, restore::{RestoreJournal,RestoreState}};
use redb::{Database,ReadableTableMetadata,TableDefinition,TableHandle};
use serde::{Deserialize,Serialize};
use sha2::{Digest,Sha256};
use std::{collections::{BTreeMap,BTreeSet},fs::{self,File,OpenOptions},io::{self,Read,Write},os::{fd::AsRawFd,unix::fs::{MetadataExt,OpenOptionsExt,PermissionsExt}},path::{Component,Path,PathBuf},time::{Duration,Instant}};
const TABLES:[&str;11]=["history_meta","history_policies","history_reservations","history_expiry","history_restores","history_change_index_v1","history_change_revisions_v1","history_actions","history_recorded_time_v1","history_origin_resource_v1","history_origin_recorded_time_v1"];
fn table_count(schema:Option<u32>)->usize {match schema {Some(5)=>11,Some(3|4)=>10,_=>8}}
fn target_identity() -> String { format!("{}/{}", crate::LORE_SOURCE, crate::LORE_PATCHSET) }
const BYTE_LIMIT:u64=256*1024*1024;
fn err(s:impl std::fmt::Display)->io::Error{io::Error::other(s.to_string())}
#[derive(Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct Checkpoint {
    version: u8,
    state: String,
    baseline_manifest: String,
    baseline_device: u64,
    baseline_inode: u64,
    work_device: u64,
    work_inode: u64,
    snapshot_device: u64,
    snapshot_inode: u64,
    lock_device: u64,
    lock_inode: u64,
    snapshot_hash: String,
    work_catalog_hash: String,
    work_hash: String,
    target: String,
    schema: Option<u32>,
    pages: u32,
    elapsed_ms: u64,
    table: usize,
    after: Option<String>,
    rows: u64,
    refs: u64,
    partitions: BTreeMap<String,String>,
    receipts: BTreeMap<String,String>,
    verified: BTreeMap<String,String>,
}
#[derive(Serialize)]
pub struct Progress {
    pub state: &'static str,
    pub table: Option<String>,
    pub rows: u64,
    pub references: u64,
}
fn normal_root(p:&Path)->io::Result<PathBuf>{if !p.is_absolute(){return Err(err("absolute path required"));
        } let mut q=PathBuf::from("/");
    for c in p.components(){match c{Component::RootDir=>{},Component::Normal(v)=>{q.push(v);
                if fs::symlink_metadata(&q)?.file_type().is_symlink(){return Err(err("symlink refused"));
                    }},_=>return Err(err("non-normal path"))}}q.canonicalize()}
fn raw_digest(p:&Path,deadline:Instant)->io::Result<String>{digest(p,BYTE_LIMIT,Some(deadline)).map(|x|x.0)}
pub(crate) fn hash_tree(root:&Path,deadline:Instant)->io::Result<String>{
 fn walk(p:&Path,root:&Path,h:&mut Sha256,count:&mut usize,total:&mut u64,depth:usize,deadline:Instant)->io::Result<()>{
  if Instant::now()>=deadline||depth>128||*count>=100_000{return Err(err("working tree budget"));
            }*count+=1;
        let m=fs::symlink_metadata(p)?;
        if m.file_type().is_symlink()||(!m.is_file()&&!m.is_dir())||(m.is_file()&&m.nlink()!=1){return Err(err("working tree object refused"));
            }
  let rel=p.strip_prefix(root).map_err(err)?.to_str().ok_or_else(||err("non-UTF8 working path"))?;
        h.update((rel.len() as u64).to_le_bytes());
        h.update(rel.as_bytes());
        h.update([if m.is_dir(){1}else{2}]);
        h.update(m.dev().to_le_bytes());
        h.update(m.ino().to_le_bytes());
  if m.is_file(){*total=total.checked_add(m.len()).filter(|x|*x<=BYTE_LIMIT).ok_or_else(||err("working byte budget"))?;
            let (d,n)=digest(p,BYTE_LIMIT.saturating_sub(*total-m.len()),Some(deadline))?;
            if n!=m.len(){return Err(err("working file changed"));
                }h.update(n.to_le_bytes());
            h.update(d.as_bytes());
            }
  else{let mut children=Vec::new();
            for e in fs::read_dir(p)?{if children.len()>=100_000usize.saturating_sub(*count){return Err(err("working entry budget"));
                    }children.push(e?.path());
                }children.sort();
            for child in children{walk(&child,root,h,count,total,depth+1,deadline)?;
                }} Ok(())
 }
 let mut h=Sha256::new();
    let(mut n,mut size)=(0,0);
    walk(root,root,&mut h,&mut n,&mut size,0,deadline)?;
    Ok(format!("{:x}",h.finalize()))
}
fn manifest_map(root:&Path)->io::Result<BTreeMap<String,String>>{let p=root.join("manifest.json");
    if fs::metadata(&p)?.len()>64*1024*1024{return Err(err("manifest budget"));
        }let v:serde_json::Value=serde_json::from_reader(File::open(p)?)?;
    serde_json::from_value(v.get("components").cloned().ok_or_else(||err("missing components"))?).map_err(err)}
fn save(container:&Path,cp:&Checkpoint)->io::Result<()> {let tmp=container.join("checkpoint.tmp");
    let mut f=OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(&tmp)?;
    let bytes=serde_json::to_vec(cp)?;
    if bytes.len()>16*1024*1024{return Err(err("checkpoint too large"));
        }f.write_all(&bytes)?;
    f.write_all(b"\n")?;
    f.sync_all()?;
    fs::rename(tmp,container.join("checkpoint.json"))?;
    File::open(container)?.sync_all()?;
    Ok(())}
/// Create owned, separate working and catalog snapshot copies; never open baseline redb.
pub fn start(baseline:&Path,container:&Path)->io::Result<()> {
 let deadline=Instant::now()+Duration::from_secs(30);
    let baseline=normal_root(baseline)?;
    let(catalog,_)=validate_candidate(&baseline,deadline)?;
    let bm=fs::metadata(&baseline)?;
    let map=manifest_map(&baseline)?;
    let manifest_hash=raw_digest(&baseline.join("manifest.json"),deadline)?;
 let parent=normal_root(container.parent().ok_or_else(||err("missing parent"))?)?;
    let container=parent.join(container.file_name().ok_or_else(||err("missing name"))?);
    if fs::symlink_metadata(&container).is_ok()||container.starts_with(&baseline)||baseline.starts_with(&container){return Err(err("container exists or overlaps baseline"));
        }fs::create_dir(&container)?;
    fs::set_permissions(&container,fs::Permissions::from_mode(0o700))?;
    let lock=OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(container.join("validation.lock"))?;
    lock.sync_all()?;
    let lock_metadata=lock.metadata()?;
 let path=|name:&str|candidate_path(&baseline,map.get(name).ok_or_else(||err("missing component"))?);
 let work=container.join("work");
    prepare(Inputs{catalog:path("catalog")?,lore:path("lore")?,registry:path("registry")?,receipts:path("receipts")?,shell_journals:path("shell_journals")?,files_journals:path("files_journals")?,destination:work.clone()})?;
 let snapshot=container.join("catalog-snapshot.redb");
    let mut src=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(&catalog)?;
    let sm=src.metadata()?;
    if sm.len()>BYTE_LIMIT{return Err(err("catalog copy exceeds budget"));}
    let mut out=OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(&snapshot)?;
    let n=io::copy(&mut (&mut src).take(sm.len()+1),&mut out)?;
    let after=src.metadata()?;
    if n!=sm.len()||after.dev()!=sm.dev()||after.ino()!=sm.ino()||after.len()!=sm.len()||after.mtime()!=sm.mtime()||after.mtime_nsec()!=sm.mtime_nsec(){return Err(err("catalog copy changed"));
        }out.sync_all()?;
    drop(out);
    if raw_digest(&catalog,deadline)?!=raw_digest(&snapshot,deadline)?{return Err(err("catalog snapshot mismatch"));
        }
 validate_candidate(&baseline,deadline)?;
    let work_map=manifest_map(&work)?;
    let work_catalog=candidate_path(&work,work_map.get("catalog").ok_or_else(||err("catalog missing"))?)?;
    // Normalize only the two owned copies before binding their immutable catalog hashes.
    { let db=Database::open(&snapshot).map_err(err)?; let _read=db.begin_read().map_err(err)?; }
    { let db=Database::open(&work_catalog).map_err(err)?; let _read=db.begin_read().map_err(err)?; }
    let wm=fs::metadata(&work)?;
    let snap=fs::metadata(&snapshot)?;
    let cp=Checkpoint{version:1,state:"incomplete".into(),baseline_manifest:manifest_hash,baseline_device:bm.dev(),baseline_inode:bm.ino(),work_device:wm.dev(),work_inode:wm.ino(),snapshot_device:snap.dev(),snapshot_inode:snap.ino(),lock_device:lock_metadata.dev(),lock_inode:lock_metadata.ino(),snapshot_hash:raw_digest(&snapshot,deadline)?,work_catalog_hash:raw_digest(&work_catalog,deadline)?,work_hash:hash_tree(&work,deadline)?,target:target_identity(),schema:None,pages:0,elapsed_ms:0,table:0,after:None,rows:0,refs:0,partitions:BTreeMap::new(),receipts:BTreeMap::new(),verified:BTreeMap::new()};
    save(&container,&cp)
}
fn subset(raw:&serde_json::Value,known:&serde_json::Value)->bool{match(raw,known){(serde_json::Value::Object(a),serde_json::Value::Object(b))=>a.iter().all(|(k,v)|b.get(k).is_some_and(|x|subset(v,x))),(serde_json::Value::Array(a),serde_json::Value::Array(b))=>a.len()==b.len()&&a.iter().zip(b).all(|(x,y)|subset(x,y)),_=>raw==known}}
fn decode<T:serde::de::DeserializeOwned+Serialize>(data:&[u8])->io::Result<T>{let raw:serde_json::Value=serde_json::from_slice(data)?;
    let typed:T=serde_json::from_value(raw.clone())?;
    if !subset(&raw,&serde_json::to_value(&typed)?){return Err(err("unknown or changed record fields"));
        }Ok(typed)}
fn read_cp(p:&Path)->io::Result<Checkpoint>{let f=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(p)?;
    if f.metadata()?.len()>16*1024*1024{return Err(err("checkpoint too large"));
        }serde_json::from_reader(f).map_err(err)}
fn same(m:&fs::Metadata,dev:u64,ino:u64)->bool{m.dev()==dev&&m.ino()==ino}
fn snapshot_aux(root:&Path,deadline:Instant)->io::Result<BTreeMap<String,String>>{let map=manifest_map(root)?;
    let mut out=BTreeMap::new();
    for key in ["registry","receipts","shell_journals","files_journals"]{let p=candidate_path(root,map.get(key).ok_or_else(||err("component missing"))?)?;
        let state = match fs::symlink_metadata(&p) {
            Ok(m) if m.is_dir() => content_tree(&p,deadline)?,
            Ok(m) if m.is_file() => raw_digest(&p,deadline)?,
            Ok(_) => return Err(err("invalid auxiliary authority type")),
            Err(e) if e.kind() == io::ErrorKind::NotFound => "absent".into(),
            Err(e) => return Err(e),
        };
        out.insert(key.into(), state);
        }Ok(out)}
fn content_tree(root:&Path,deadline:Instant)->io::Result<String>{fn rec(p:&Path,root:&Path,h:&mut Sha256,n:&mut usize,total:&mut u64,depth:usize,deadline:Instant)->io::Result<()>{if depth>128||*n>=100_000||Instant::now()>=deadline{return Err(err("aux budget"));
            }*n+=1;
        let m=fs::symlink_metadata(p)?;
        if m.file_type().is_symlink()||(!m.is_dir()&&!m.is_file())||(m.is_file()&&m.nlink()!=1){return Err(err("aux object refused"));
            }let rel=p.strip_prefix(root).map_err(err)?.to_str().ok_or_else(||err("aux path"))?;
        h.update((rel.len() as u64).to_le_bytes());
        h.update(rel.as_bytes());
        h.update([u8::from(m.is_dir())]);
        if m.is_file(){*total=total.checked_add(m.len()).filter(|v|*v<=BYTE_LIMIT).ok_or_else(||err("aux byte budget"))?;
            h.update(raw_digest(p,deadline)?.as_bytes());
            }else{let mut xs=Vec::new();
            for e in fs::read_dir(p)?{if xs.len()>=100_000usize.saturating_sub(*n){return Err(err("aux entry budget"));
                    }xs.push(e?.path());
                }xs.sort();
            for p in xs{rec(&p,root,h,n,total,depth+1,deadline)?;
                }}Ok(())}let mut h=Sha256::new();
    let mut n=0;
    let mut total=0;
    rec(root,root,&mut h,&mut n,&mut total,0,deadline)?;
    Ok(format!("{:x}",h.finalize()))}
fn check_receipt_identity(
    cp: &mut Checkpoint,
    action: &ActionRecord,
    resource: &ResourceKey,
    receipt: &VersionReceipt,
) -> io::Result<Option<String>> {
    if receipt.version_id.is_empty() || receipt.version_id.len() > 512 {
        return Err(err("invalid version identity"));
    }
    let key = serde_json::to_string(&(&action.resource_key.account_id, &receipt.version_id))?;
    let value = serde_json::to_string(&(&action.action_id, resource, receipt))?;
    if key.len() > 2048 || value.len() > 65536 {
        return Err(err("receipt checkpoint budget"));
    }
    if let Some(old) = cp.receipts.get(&key) {
        if old != &value {
            return Err(err("ambiguous version receipt"));
        }
        return Ok(None);
    }
    if cp.receipts.len() >= 4096 {
        return Err(err("receipt coverage budget"));
    }
    cp.receipts.insert(key.clone(), value);
    Ok(Some(key))
}

async fn verify_action(
    action: &ActionRecord,
    expiry: Option<ExpiryRecord>,
    partition: [u8; 16],
    lore: &Path,
    cp: &mut Checkpoint,
    deadline: Instant,
) -> io::Result<()> {
    let pending = !matches!(action.state, ActionState::Complete | ActionState::Aborted | ActionState::Conflict)
        || action.live.as_ref().is_some_and(|x| matches!(x.status, LiveStatus::Unknown | LiveStatus::Partial))
        || (action.state == ActionState::Complete
            && !action.live.as_ref().is_some_and(|x| x.status == LiveStatus::Committed));
    let account = &action.resource_key.account_id;
    if account.is_empty() || action.actor_account_id != *account {
        return Err(err("ambiguous account"));
    }
    if action.guard_resource_ids.iter().chain(&action.modified_resource_ids).any(|r| r.account_id != *account)
        || action.live.as_ref().is_some_and(|live| live.committed_resources.iter().any(|r| r.resource_key.account_id != *account))
    {
        return Err(err("cross-account action participant"));
    }
    if expiry.as_ref().is_some_and(|e| e.state == ExpiryState::Expiring) {
        return Err(err("pending expiry"));
    }
    let expired = expiry.as_ref().is_some_and(|e| e.state == ExpiryState::Expired);
    let mut values: Vec<(&ResourceKey, &VersionReceipt)> = Vec::new();
    if let Some(first) = action.before_resources.first() {
        if action.before.as_ref() != first.before.as_ref() {
            return Err(err("inconsistent before projection"));
        }
    }
    if let Some(first) = action.after_resources.first() {
        if action.after.as_ref() != Some(&first.after) {
            return Err(err("inconsistent after projection"));
        }
    }
    let before_key = action.before_resources.first().map_or(&action.resource_key, |r| &r.resource_key);
    let after_key = action.after_resources.first().map_or(&action.resource_key, |r| &r.resource_key);
    values.extend(action.before.iter().map(|r| (before_key, r)));
    values.extend(action.after.iter().map(|r| (after_key, r)));
    for entry in &action.before_resources {
        if entry.resource_key.account_id != *account {
            return Err(err("cross-account before"));
        }
        let receipt = entry.before.as_ref().ok_or_else(|| err("missing before receipt"))?;
        values.push((&entry.resource_key, receipt));
    }
    for entry in &action.after_resources {
        if entry.resource_key.account_id != *account {
            return Err(err("cross-account after"));
        }
        values.push((&entry.resource_key, &entry.after));
    }
    if let Some(batch) = &action.mutation_batch {
        if batch.parent_action_id != action.action_id || batch.schema_version != 1
            || batch.prepare.parent_action_id != action.action_id || !batch.prepare.acknowledged
            || batch.prepare.batch_version != 1
            || batch.prepare.resource_count as usize != batch.resources.len()
            || batch.prepare.logical_bytes != action.before_logical_bytes
            || batch.resources != action.before_resources
        {
            return Err(err("invalid batch"));
        }
        for entry in &batch.resources {
            if entry.resource_key.account_id != *account {
                return Err(err("cross-account batch"));
            }
            let receipt = entry.before.as_ref().ok_or_else(|| err("missing batch receipt"))?;
            values.push((&entry.resource_key, receipt));
        }
    }
    if values.len() > 256 {
        return Err(err("action reference budget"));
    }
    for (resource, receipt) in values {
        let Some(coverage_key) = check_receipt_identity(cp, action, resource, receipt)? else { continue };
        cp.refs = cp.refs.checked_add(1).filter(|n| *n <= 64).ok_or_else(|| err("reference budget"))?;
        let proof = match &receipt.content {
            VersionContent::Bytes(reference) => {
                let hash = reference.hash_hex.parse().map_err(|_| err("invalid reference hash"))?;
                if expired {
                    "expired".into()
                } else {
                    let address = lore_storage::Address { context: lore_storage::Context::from(reference.context), hash };
                    verify_reference(lore.to_path_buf(), lore_storage::Partition::from(partition), address, None, deadline).await?
                }
            }
            VersionContent::Empty => "empty".into(),
            VersionContent::Tombstone => "tombstone".into(),
        };
        cp.verified.insert(coverage_key, proof);
    }
    if pending {
        return Err(err("pending or ambiguous action"));
    }
    Ok(())
}

/// Process a bounded page and persist only an incomplete, identity-bound checkpoint.
/// A refusal never executes pending operations or marks the candidate ready.
pub async fn advance(baseline:&Path,container:&Path,page_size:usize)->io::Result<Progress>{
 if !(1..=64).contains(&page_size){return Err(err("invalid page size"));
        }let run_started=Instant::now();
    let deadline=run_started+Duration::from_secs(30);
    let baseline=normal_root(baseline)?;
    let container=normal_root(container)?;
    let work=normal_root(&container.join("work"))?;
    let snapshot=container.join("catalog-snapshot.redb");
    let lock=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(container.join("validation.lock"))?;
    if !lock.metadata()?.is_file() || unsafe{libc::flock(lock.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB)}!=0{return Err(err("validation already active"));}
    let mut cp=read_cp(&container.join("checkpoint.json"))?;
 if cp.receipts.len()!=cp.verified.len() || cp.receipts.len() as u64!=cp.refs || cp.receipts.keys().ne(cp.verified.keys()) {return Err(err("incomplete receipt checkpoint"));
        }
 if cp.version!=1||cp.state!="incomplete"||cp.target!=target_identity()||!same(&lock.metadata()?,cp.lock_device,cp.lock_inode)||cp.pages>=4096||cp.elapsed_ms>=600_000||cp.table>TABLES.len()||!same(&fs::metadata(&baseline)?,cp.baseline_device,cp.baseline_inode)||!same(&fs::metadata(&work)?,cp.work_device,cp.work_inode)||!same(&fs::symlink_metadata(&snapshot)?,cp.snapshot_device,cp.snapshot_inode){return Err(err("checkpoint identity changed"));
        }
 validate_candidate(&baseline,deadline)?;
    if raw_digest(&baseline.join("manifest.json"),deadline)?!=cp.baseline_manifest || raw_digest(&snapshot,deadline)?!=cp.snapshot_hash {
        return Err(err("baseline or catalog snapshot changed; validation cannot resume"));
    }
    if hash_tree(&work,deadline)?!=cp.work_hash {
        return Err(err("working copy changed since checkpoint; preserve it and start a fresh validation from the unchanged baseline"));
    }
 let before_aux=snapshot_aux(&baseline,deadline)?;
    if snapshot_aux(&work,deadline)?!=before_aux{return Err(err("auxiliary authority changed"));
        }
 if cp.table==table_count(cp.schema){return Ok(Progress{state:"coverage_complete_no_readiness",table:None,rows:cp.rows,references:cp.refs});
        }
 let work_map=manifest_map(&work)?;
    let work_catalog=candidate_path(&work,work_map.get("catalog").ok_or_else(||err("catalog missing"))?)?;
    let lore=candidate_path(&work,work_map.get("lore").ok_or_else(||err("lore missing"))?)?;
 if raw_digest(&work_catalog,deadline)?!=cp.work_catalog_hash{return Err(err("working catalog changed"));}
 let db=Database::open(&snapshot).map_err(err)?;
    let target=Database::open(&work_catalog).map_err(err)?;
    let read=db.begin_read().map_err(err)?;
    let working=target.begin_read().map_err(err)?;
 let names:BTreeSet<_>=read.list_tables().map_err(err)?.map(|x|x.name().to_string()).collect();
    let other:BTreeSet<_>=working.list_tables().map_err(err)?.map(|x|x.name().to_string()).collect();
    if names!=other||names.iter().any(|x|!TABLES.contains(&x.as_str()))||read.list_multimap_tables().map_err(err)?.next().is_some()||working.list_multimap_tables().map_err(err)?.next().is_some(){return Err(err("unknown or changed catalog tables"));
        }
 for required in ["history_meta","history_actions","history_policies","history_reservations","history_expiry","history_restores"]{if !names.contains(required){return Err(err("required catalog table missing"));
            }}
 let meta=read.open_table(TableDefinition::<&str,&[u8]>::new("history_meta")).map_err(err)?;
    let schema:u32=serde_json::from_slice(meta.get("schema").map_err(err)?.ok_or_else(||err("schema missing"))?.value())?;
    if !(1..=5).contains(&schema){return Err(err("unsupported catalog schema"));
        }if cp.schema.is_some_and(|old|old!=schema){return Err(err("catalog schema changed"));
        }cp.schema=Some(schema);
    if schema>=2 && (!names.contains(TABLES[5])||!names.contains(TABLES[6]) || !meta.get("change_index_v1_complete").map_err(err)?.is_some_and(|x|x.value()==b"true") || !meta.get("change_index_v1_policy").map_err(err)?.is_some_and(|x|x.value()==b"resource_actions_v1")){return Err(err("incomplete catalog index"));
        }
    if schema>=3 {if !names.contains(TABLES[8])||!names.contains(TABLES[9])||!meta.get("history_ordered_policy_v1").map_err(err)?.is_some_and(|v|v.value()==b"recorded_time_origin_resource_v1"){return Err(err("incomplete ordered index"));}}
    else if names.contains(TABLES[8])||names.contains(TABLES[9]) {return Err(err("unexpected ordered index"));}
    if names.contains(TABLES[10]) != (schema == 5) {
        return Err(err("origin timeline profile mismatch"));
    }
    if schema == 5 && !meta.get("history_origin_catalog_incarnation_v1").map_err(err)?
        .is_some_and(|v| v.value().len() == 64 && v.value().iter().all(u8::is_ascii_hexdigit)) {
        return Err(err("origin catalog incarnation unavailable"));
    }
 let name=TABLES[cp.table];
    if !names.contains(name){cp.table+=1;
        cp.after=None;
        }else{
 let def=TableDefinition::<&str,&[u8]>::new(name);
        let source=read.open_table(def).map_err(err)?;
        let dest=working.open_table(def).map_err(err)?;
        if source.len().map_err(err)?!=dest.len().map_err(err)?{return Err(err("working catalog row count changed"));
            }
 let mut rows=Vec::new();
        let mut page_bytes=0usize;
        let start=cp.after.as_deref().unwrap_or("");
        let mut has_more=false;
        for item in source.range(start..).map_err(err)? {let(k,v)=item.map_err(err)?;
            if cp.after.as_deref()==Some(k.value()){continue;
                } if rows.len()==page_size{has_more=true;
                break;
                }let key=k.value().to_owned();
            let val=v.value();
            if key.is_empty()||key.len()>if schema>=3 {32768}else{1024}||val.len()>1_000_000{return Err(err("catalog row budget or identity"));
                }page_bytes=page_bytes.checked_add(val.len()).filter(|n|*n<=4_000_000).ok_or_else(||err("catalog page byte budget"))?;
            let other=dest.get(key.as_str()).map_err(err)?.ok_or_else(||err("working row missing"))?;
            if other.value()!=val{return Err(err("working catalog row changed"));
                }rows.push((key,val.to_vec()));
            }
 for (key,value) in &rows {if Instant::now()>=deadline{return Err(err("verification deadline"));
                }match name {
 "history_meta"=>{if key=="schema"{let _:u32=serde_json::from_slice(value)?;
                        }else if key.starts_with("partition:"){if key.len()<=10||value.len()!=16{return Err(err("invalid account partition"));
                            }let account=&key[10..];
                        if account.len()>512||cp.partitions.len()>=4096{return Err(err("account partition budget"));
                            }let id=value.iter().map(|b|format!("{b:02x}")).collect::<String>();
                        if let Some(old)=cp.partitions.insert(id,account.to_owned()){if old!=account{return Err(err("account partition collision"));
                                }}}else if key=="change_index_v1_complete"{if value!=b"true"{return Err(err("incomplete index"));
                            }}else if key=="change_index_v1_policy"{if value!=b"resource_actions_v1"{return Err(err("unknown index policy"));
                            }}else if key=="history_ordered_policy_v1"&&schema>=3 {if value!=b"recorded_time_origin_resource_v1"{return Err(err("unknown ordered policy"));
                            }}else if schema == 5 && key == "history_origin_catalog_incarnation_v1" {
                            if value.len() != 64 || !value.iter().all(u8::is_ascii_hexdigit) { return Err(err("invalid origin incarnation")); }
                        }else if schema == 5 && key.starts_with("history_origin_revision_v1:") {
                            let scope = key.trim_start_matches("history_origin_revision_v1:");
                            let revision: u64 = serde_json::from_slice(value)?;
                            if scope.len() != 64 || !scope.bytes().all(|b| b.is_ascii_hexdigit()) || !(1..=9_007_199_254_740_991).contains(&revision) {
                                return Err(err("invalid origin revision"));
                            }
                        }else{return Err(err("unknown catalog metadata"));
                        }},
 "history_policies"=>{if key!="policy"{return Err(err("unknown policy"));
                        }let _:PolicySet=decode(value)?;
                    },
 "history_reservations"=>{let _:PersistedReservation=decode(value)?;
                    return Err(err("pending reservation"));
                    },
 "history_expiry"=>{let x:ExpiryRecord=decode(value)?;
                    if x.action_id!=*key||x.state==ExpiryState::Expiring || x.finished_millis.is_none_or(|v|v<x.started_millis){return Err(err("pending or invalid expiry"));
                        } let a=read.open_table(TableDefinition::<&str,&[u8]>::new("history_actions")).map_err(err)?;
                    if a.get(key.as_str()).map_err(err)?.is_none(){return Err(err("orphan expiry"));
                        }},
 "history_restores"=>{let x:RestoreJournal=decode(value)?;
                    if x.restore_id!=*key||!matches!(x.state,RestoreState::Complete|RestoreState::Conflict|RestoreState::Failed){return Err(err("pending or invalid restore"));
                        }if x.current_capture.as_ref().is_some_and(|v|!v.durable||v.version_id.is_empty()||v.fingerprint.is_empty()){return Err(err("unproven restore capture"));
                        }
                    if x.verification.as_ref().is_some_and(|v|v.status!="Verified"||v.restore_id!=x.restore_id||v.resource_id!=x.destination.resource_id||v.version_id!=x.source_version_id||v.restored_content_hash.as_deref()!=Some(v.content_hash.as_str())) || (x.state==RestoreState::Complete && x.verification.is_none()){return Err(err("pending or unverified restore"));}
 let a=read.open_table(TableDefinition::<&str,&[u8]>::new("history_actions")).map_err(err)?;
                    let row=a.get(x.source_action_id.as_str()).map_err(err)?.ok_or_else(||err("orphan restore source"))?;
                    let source_action:ActionRecord=decode(row.value())?;
                    if source_action.resource_key.account_id!=x.destination.account_id{return Err(err("restore account mismatch"));
                        }
 let exists=source_action.before.iter().chain(source_action.after.iter()).chain(source_action.before_resources.iter().filter_map(|r|r.before.as_ref())).chain(source_action.after_resources.iter().map(|r|&r.after)).chain(source_action.mutation_batch.iter().flat_map(|b|b.resources.iter().filter_map(|r|r.before.as_ref()))).any(|v|v.version_id==x.source_version_id);
                    if !exists{return Err(err("orphan restore version"));
                        }},
 "history_change_index_v1"=>{let id=std::str::from_utf8(value).map_err(err)?;
                    if id.is_empty()||!key.ends_with(&format!(":{id}")){return Err(err("invalid change index"));
                        }let a=read.open_table(TableDefinition::<&str,&[u8]>::new("history_actions")).map_err(err)?;
                    let row=a.get(id).map_err(err)?.ok_or_else(||err("orphan change index"))?;
                    let action:ActionRecord=decode(row.value())?;
                    if !action_resources(&action).iter().any(|r|change_scope(r).is_ok_and(|scope|format!("{scope}:{id}")==*key)){return Err(err("change index scope mismatch"));
                        }},
 "history_change_revisions_v1"=>{let revision:u64=serde_json::from_slice(value)?;
                    if (schema>=3&&revision==0)||revision>9_007_199_254_740_991||key.len()%2!=0||key.len()>if schema>=3 {32768}else{8192}||!key.bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c)){return Err(err("invalid change revision"));
                        }let bytes=(0..key.len()).step_by(2).map(|i|u8::from_str_radix(&key[i..i+2],16)).collect::<Result<Vec<_>,_>>().map_err(err)?;
                    let resource:ResourceKey=decode(&bytes)?;
                    if change_scope(&resource).map_err(err)?!=*key{return Err(err("invalid revision scope"));
                        }},
 "history_recorded_time_v1"|"history_origin_resource_v1"=>{if schema<3{return Err(err("unexpected ordered index"));}let id=std::str::from_utf8(value).map_err(err)?;let actions=read.open_table(TableDefinition::<&str,&[u8]>::new("history_actions")).map_err(err)?;let row=actions.get(id).map_err(err)?.ok_or_else(||err("orphan ordered index"))?;let action:ActionRecord=decode(row.value())?;let (times,origins)=ordered_keys(&action).map_err(err)?;if action.action_id!=id||!(if name=="history_recorded_time_v1" {times.contains(key)}else{origins.contains(key)}){return Err(err("ordered index scope mismatch"));}},
 "history_origin_recorded_time_v1" => {
                    let id = std::str::from_utf8(value).map_err(err)?;
                    let actions = read.open_table(TableDefinition::<&str,&[u8]>::new("history_actions")).map_err(err)?;
                    let row = actions.get(id).map_err(err)?.ok_or_else(||err("orphan origin timeline entry"))?;
                    let action: ActionRecord = decode(row.value())?;
                    if action.action_id != id || !origin_timeline_keys(&action).map_err(err)?.contains_key(key) {
                        return Err(err("origin timeline scope mismatch"));
                    }
                },
 "history_actions"=>{let x:ActionRecord=decode(value)?;
                    if x.action_id!=*key||x.schema_version!=1{return Err(err("invalid action"));
                        }let partition_key=format!("partition:{}",x.resource_key.account_id);
                    let p=meta.get(partition_key.as_str()).map_err(err)?.ok_or_else(||err("missing account partition"))?;
                    let partition:[u8;16]=p.value().try_into().map_err(|_|err("invalid partition"))?;
                    let exp=read.open_table(TableDefinition::<&str,&[u8]>::new("history_expiry")).map_err(err)?;
                    let expiry=exp.get(key.as_str()).map_err(err)?.map(|v|decode::<ExpiryRecord>(v.value())).transpose()?;
                    if schema>=2{let index=read.open_table(TableDefinition::<&str,&[u8]>::new("history_change_index_v1")).map_err(err)?;
                        for resource in action_resources(&x){let index_key=format!("{}:{}",change_scope(&resource).map_err(err)?,x.action_id);
                            if !index.get(index_key.as_str()).map_err(err)?.is_some_and(|v|v.value()==x.action_id.as_bytes()){return Err(err("missing action index"));
                                }}}
                    if schema>=3 {let (times,origins)=ordered_keys(&x).map_err(err)?;let ti=read.open_table(TableDefinition::<&str,&[u8]>::new("history_recorded_time_v1")).map_err(err)?;let oi=read.open_table(TableDefinition::<&str,&[u8]>::new("history_origin_resource_v1")).map_err(err)?;let revisions=read.open_table(TableDefinition::<&str,&[u8]>::new("history_change_revisions_v1")).map_err(err)?;for resource in action_resources(&x){let scope=change_scope(&resource).map_err(err)?;if !revisions.get(scope.as_str()).map_err(err)?.is_some_and(|v|serde_json::from_slice::<u64>(v.value()).is_ok_and(|n|n>0&&n<=9_007_199_254_740_991)){return Err(err("missing ordered revision"));}}for k in times {if !ti.get(k.as_str()).map_err(err)?.is_some_and(|v|v.value()==x.action_id.as_bytes()){return Err(err("missing time index"));}}for k in origins {if !oi.get(k.as_str()).map_err(err)?.is_some_and(|v|v.value()==x.action_id.as_bytes()){return Err(err("missing origin index"));}}}
                    if schema == 5 {
                        let index = read.open_table(TableDefinition::<&str,&[u8]>::new("history_origin_recorded_time_v1")).map_err(err)?;
                        for key in origin_timeline_keys(&x).map_err(err)?.keys() {
                            if !index.get(key.as_str()).map_err(err)?.is_some_and(|v| v.value() == x.action_id.as_bytes()) {
                                return Err(err("missing origin timeline entry"));
                            }
                        }
                    }
                    verify_action(&x,expiry,partition,&lore,&mut cp,deadline).await?;
                    },
 _=>return Err(err("unknown table"))};
            cp.rows=cp.rows.checked_add(1).filter(|x|*x<=100_000).ok_or_else(||err("row budget"))?;
            cp.after=Some(key.clone());
            }
 if !has_more {cp.table+=1;
            cp.after=None;
            }
 }
 drop(meta);
    drop(working);
    drop(read);
    drop(target);
    drop(db);
 validate_candidate(&baseline,deadline)?;
    if raw_digest(&baseline.join("manifest.json"),deadline)?!=cp.baseline_manifest||snapshot_aux(&baseline,deadline)?!=before_aux||snapshot_aux(&work,deadline)?!=before_aux{return Err(err("authority changed during page"));
        }
 if raw_digest(&snapshot,deadline)?!=cp.snapshot_hash || raw_digest(&work_catalog,deadline)?!=cp.work_catalog_hash{return Err(err("stable catalog snapshot changed"));}
    cp.work_hash=hash_tree(&work,deadline)?;
    cp.pages=cp.pages.checked_add(1).ok_or_else(||err("page budget"))?;
    cp.elapsed_ms=cp.elapsed_ms.checked_add(run_started.elapsed().as_millis() as u64).filter(|x|*x<=600_000).ok_or_else(||err("total time budget"))?;
    let progress=Progress{state:if cp.table==table_count(cp.schema){"coverage_complete_no_readiness"}else{"incomplete"},table:if cp.table==table_count(cp.schema){None}else{TABLES.get(cp.table).map(|x|x.to_string())},rows:cp.rows,references:cp.refs};
    save(&container,&cp)?;
    Ok(progress)
}

/// Closed receipt for a prepared private candidate. It grants no runtime authority.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PreparationReceipt {
    pub version: u8,
    pub state: String,
    pub baseline_manifest: String,
    pub target: String,
    pub catalog_schema: u32,
    pub lore_format: u32,
    pub checkpoint_hash: String,
    pub snapshot_hash: String,
    pub work_catalog_hash: String,
    pub work_device: u64,
    pub work_inode: u64,
    pub work_hash: String,
    pub marker_hash: String,
    pub auxiliary: BTreeMap<String, String>,
    pub coverage_hash: String,
    pub rows: u64,
    pub references: u64,
}

#[derive(Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct PreparationIntent {
    version: u8,
    checkpoint_hash: String,
    initial_work_hash: String,
    marker_hash: String,
}

fn sync_owned_tree(path: &Path, deadline: Instant) -> io::Result<()> {
    fn visit(path: &Path, depth: usize, count: &mut usize, deadline: Instant) -> io::Result<()> {
        if depth > 128 || *count >= 100_000 || Instant::now() >= deadline {
            return Err(err("sync budget exceeded"));
        }
        *count += 1;
        let metadata = fs::symlink_metadata(path)?;
        if metadata.file_type().is_symlink() || (!metadata.is_file() && !metadata.is_dir())
            || (metadata.is_file() && metadata.nlink() != 1) {
            return Err(err("unsupported candidate sync object"));
        }
        if metadata.is_dir() {
            let mut entries = Vec::new();
            for entry in fs::read_dir(path)? {
                if entries.len() >= 100_000usize.saturating_sub(*count) {
                    return Err(err("sync entry budget exceeded"));
                }
                entries.push(entry?.path());
            }
            entries.sort();
            for entry in entries { visit(&entry, depth + 1, count, deadline)?; }
        }
        OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(path)?.sync_all()
    }
    visit(path, 0, &mut 0, deadline)
}

async fn flush_candidate(lore: &Path, deadline: Instant) -> io::Result<()> {
    use lore_storage::{ImmutableStore, ImmutableStoreSettings, LocalImmutableStore};
    use std::sync::Arc;
    let settings = ImmutableStoreSettings {
        protect_local_fragment: true,
        implicit_durable_stored: false,
        isolate_partitions: true,
        flush_background: false,
        ..Default::default()
    };
    let opened = tokio::time::timeout_at(tokio::time::Instant::from_std(deadline),
        LocalImmutableStore::new(Some(lore.to_path_buf()), settings)).await
        .map_err(|_| err("candidate flush deadline"))?.map_err(err)?;
    let store: Arc<dyn ImmutableStore> = opened;
    tokio::time::timeout_at(tokio::time::Instant::from_std(deadline), store.clone().flush(lore_storage::FlushOptions::SYNC_DATA)).await
        .map_err(|_| err("candidate flush deadline; outcome unknown"))?.map_err(err)?;
    drop(store);
    Ok(())
}

fn read_exact_file(path: &Path, limit: u64) -> io::Result<Vec<u8>> {
    let f = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(path)?;
    if !f.metadata()?.is_file() || f.metadata()?.len() > limit { return Err(err("invalid receipt or marker")); }
    let mut bytes = Vec::new();
    f.take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit { return Err(err("receipt or marker budget")); }
    Ok(bytes)
}

fn write_new_synced(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut file = OpenOptions::new().write(true).create_new(true).mode(0o600)
        .custom_flags(libc::O_NOFOLLOW).open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}

/// Prepare only this owned candidate; never open or mutate the baseline.
/// An interrupted marker step without a matching receipt remains unknown.
pub async fn prepare_final(baseline: &Path, container: &Path) -> io::Result<PreparationReceipt> {
    use crate::catalog::VersionReceipt;
    let deadline = Instant::now() + Duration::from_secs(30);
    let baseline = normal_root(baseline)?;
    let container = normal_root(container)?;
    let work = normal_root(&container.join("work"))?;
    let snapshot = container.join("catalog-snapshot.redb");
    let lock = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW)
        .open(container.join("validation.lock"))?;
    if !lock.metadata()?.is_file() || unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(err("validation already active"));
    }
    let cp = read_cp(&container.join("checkpoint.json"))?;
    if cp.version != 1 || cp.state != "incomplete" || cp.target != target_identity()
        || cp.table != table_count(cp.schema) || cp.after.is_some() || !matches!(cp.schema, Some(1..=5))
        || cp.pages == 0 || cp.pages > 4096 || cp.elapsed_ms > 600_000 || cp.rows > 100_000 || cp.refs > 64
        || cp.receipts.len() != cp.verified.len() || cp.receipts.len() as u64 != cp.refs
        || cp.receipts.keys().ne(cp.verified.keys())
        || !same(&lock.metadata()?, cp.lock_device, cp.lock_inode)
        || !same(&fs::metadata(&baseline)?, cp.baseline_device, cp.baseline_inode)
        || !same(&fs::metadata(&work)?, cp.work_device, cp.work_inode)
        || !same(&fs::symlink_metadata(&snapshot)?, cp.snapshot_device, cp.snapshot_inode) {
        return Err(err("completed checkpoint unavailable or identity changed"));
    }
    validate_candidate(&baseline, deadline)?;
    let cp_hash = raw_digest(&container.join("checkpoint.json"), deadline)?;
    if raw_digest(&baseline.join("manifest.json"), deadline)? != cp.baseline_manifest
        || raw_digest(&snapshot, deadline)? != cp.snapshot_hash {
        return Err(err("baseline or catalog snapshot changed"));
    }
    let map = manifest_map(&work)?;
    let lore = candidate_path(&work, map.get("lore").ok_or_else(|| err("lore missing"))?)?;
    let catalog = candidate_path(&work, map.get("catalog").ok_or_else(|| err("catalog missing"))?)?;
    let auxiliary = snapshot_aux(&baseline, deadline)?;
    if snapshot_aux(&work, deadline)? != auxiliary || raw_digest(&catalog, deadline)? != cp.work_catalog_hash {
        return Err(err("working catalog or auxiliary authority changed"));
    }
    let marker = lore.join(crate::FORMAT_MARKER);
    let marker_bytes = crate::canonical_format_marker();
    let marker_hash = format!("{:x}", Sha256::digest(&marker_bytes));
    let coverage_hash = format!("{:x}", Sha256::digest(serde_json::to_vec(&(&cp.receipts, &cp.verified))?));
    let receipt_path = container.join("preparation-receipt.json");
    let intent = container.join("preparation-intent.json");
    let expected_intent = PreparationIntent {
        version: 1, checkpoint_hash: cp_hash.clone(), initial_work_hash: cp.work_hash.clone(), marker_hash: marker_hash.clone(),
    };
    let build_receipt = |work_hash: String| PreparationReceipt {
        version: 1, state: "prepared_candidate_only".into(), baseline_manifest: cp.baseline_manifest.clone(),
        target: target_identity(), catalog_schema: cp.schema.unwrap(), lore_format: crate::FORMAT_REVISION,
        checkpoint_hash: cp_hash.clone(), snapshot_hash: cp.snapshot_hash.clone(),
        work_catalog_hash: cp.work_catalog_hash.clone(), work_device: cp.work_device, work_inode: cp.work_inode,
        work_hash, marker_hash: marker_hash.clone(), auxiliary: auxiliary.clone(),
        coverage_hash: coverage_hash.clone(), rows: cp.rows, references: cp.refs,
    };
    if fs::symlink_metadata(&receipt_path).is_ok() {
        let original_intent: PreparationIntent = serde_json::from_slice(&read_exact_file(&intent, 4096)?)?;
        if original_intent != expected_intent { return Err(err("preparation intent does not match checkpoint")); }
        let existing: PreparationReceipt = serde_json::from_slice(&read_exact_file(&receipt_path, 1_000_000)?)?;
        if read_exact_file(&marker, 4096)? != marker_bytes || existing != build_receipt(hash_tree(&work, deadline)?) {
            return Err(err("existing preparation receipt does not match current candidate"));
        }
        crate::validate_physical_layout(&lore)?;
        File::open(&lore)?.sync_all()?;
        File::open(&container)?.sync_all()?;
        validate_candidate(&baseline, deadline)?;
        if raw_digest(&baseline.join("manifest.json"), deadline)? != cp.baseline_manifest
            || raw_digest(&snapshot, deadline)? != cp.snapshot_hash || raw_digest(&catalog, deadline)? != cp.work_catalog_hash
            || snapshot_aux(&baseline, deadline)? != auxiliary || snapshot_aux(&work, deadline)? != auxiliary
            || existing != build_receipt(hash_tree(&work, deadline)?) {
            return Err(err("authority changed during receipt reconciliation"));
        }
        return Ok(existing);
    }
    if fs::symlink_metadata(&intent).is_ok() { return Err(err("preparation outcome unknown; preserve incomplete candidate for review")); }
    let source_marker = read_exact_file(&marker, 4096)?;
    if hash_tree(&work, deadline)? != cp.work_hash || !crate::supported_copy_source(&source_marker) {
        return Err(err("working candidate changed or legacy marker differs"));
    }
    // Fence every attempt before the target opener: a cancelled/failed flush may
    // leave target-owned work in flight even when the initial tree is unchanged.
    let intent_bytes = serde_json::to_vec(&expected_intent)?;
    write_new_synced(&intent, &intent_bytes)?;
    File::open(&container)?.sync_all()?;
    flush_candidate(&lore, deadline).await?;
    // Reverify every retained tree against its checkpointed computed digest after recovery.
    for (key, encoded) in &cp.receipts {
        let (_, resource, receipt): (String, crate::catalog::ResourceKey, VersionReceipt) = serde_json::from_str(encoded)?;
        let proof = cp.verified.get(key).ok_or_else(|| err("missing proof"))?;
        match &receipt.content {
            VersionContent::Bytes(reference) if proof != "expired" => {
                let candidates: Vec<_> = cp.partitions.iter().filter(|(_, account)| **account == resource.account_id).collect();
                if candidates.len() != 1 { return Err(err("account partition ambiguous")); }
                let hex = candidates[0].0;
                if hex.len() != 32 { return Err(err("invalid partition")); }
                let mut partition = [0u8; 16];
                for (i, byte) in partition.iter_mut().enumerate() {
                    *byte = u8::from_str_radix(&hex[i*2..i*2+2], 16).map_err(err)?;
                }
                let address = lore_storage::Address { context: lore_storage::Context::from(reference.context),
                    hash: reference.hash_hex.parse().map_err(|_| err("invalid reference"))? };
                verify_reference(lore.clone(), lore_storage::Partition::from(partition), address, Some(proof), deadline).await?;
            }
            VersionContent::Bytes(_) if proof == "expired" => {},
            VersionContent::Empty if proof == "empty" => {},
            VersionContent::Tombstone if proof == "tombstone" => {},
            _ => return Err(err("receipt proof mismatch")),
        }
    }
    let verified_hash = hash_tree(&work, deadline)?;
    flush_candidate(&lore, deadline).await?;
    if hash_tree(&work, deadline)? != verified_hash { return Err(err("candidate changed during final flush; preserve for review")); }
    crate::validate_physical_layout(&lore)?;
    sync_owned_tree(&work, deadline)?;
    if hash_tree(&work, deadline)? != verified_hash { return Err(err("candidate changed during sync")); }
    validate_candidate(&baseline, deadline)?;
    if raw_digest(&baseline.join("manifest.json"), deadline)? != cp.baseline_manifest
        || raw_digest(&snapshot, deadline)? != cp.snapshot_hash
        || raw_digest(&catalog, deadline)? != cp.work_catalog_hash
        || snapshot_aux(&baseline, deadline)? != auxiliary || snapshot_aux(&work, deadline)? != auxiliary {
        return Err(err("authority changed before marker publication"));
    }
    let marker_metadata = fs::symlink_metadata(&marker)?;
    if !marker_metadata.is_file() || marker_metadata.nlink() != 1 || read_exact_file(&marker, 4096)? != source_marker {
        return Err(err("legacy marker changed"));
    }
    let marker_tmp = container.join("preparation-marker.tmp");
    write_new_synced(&marker_tmp, &marker_bytes)?;
    let now = fs::symlink_metadata(&marker)?;
    if !same(&now, marker_metadata.dev(), marker_metadata.ino()) || read_exact_file(&marker, 4096)? != source_marker {
        return Err(err("legacy marker changed; preparation unknown"));
    }
    fs::rename(&marker_tmp, &marker)?;
    File::open(&lore)?.sync_all()?;
    crate::validate_physical_layout(&lore)?;
    validate_candidate(&baseline, deadline)?;
    if raw_digest(&baseline.join("manifest.json"), deadline)? != cp.baseline_manifest
        || raw_digest(&snapshot, deadline)? != cp.snapshot_hash || raw_digest(&catalog, deadline)? != cp.work_catalog_hash
        || snapshot_aux(&baseline, deadline)? != auxiliary || snapshot_aux(&work, deadline)? != auxiliary {
        return Err(err("authority changed after marker; preparation unknown"));
    }
    let receipt = build_receipt(hash_tree(&work, deadline)?);
    let receipt_tmp = container.join("preparation-receipt.tmp");
    write_new_synced(&receipt_tmp, &serde_json::to_vec(&receipt)?)?;
    fs::hard_link(&receipt_tmp, &receipt_path).map_err(|_| err("receipt publication refused or unknown"))?;
    File::open(&container)?.sync_all()?;
    Ok(receipt)
}

#[cfg(test)]
mod qualification {
    use super::*;
    use crate::catalog::Catalog;

    #[tokio::test]
    #[ignore = "requires an explicitly supplied disposable old-format fixture"]
    async fn synthetic_terminal_and_pending_resume() {
        let old = PathBuf::from(std::env::var_os("OPENCLANK_HISTORY_OLD_FIXTURE").unwrap());
        assert!(old.is_absolute());
        let old = old.canonicalize().unwrap();
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        fs::write(root.join("registry"), b"{}").unwrap();
        for name in ["receipts", "shell", "files"] {
            fs::create_dir(root.join(name)).unwrap();
        }
        let inputs = |catalog: PathBuf, lore: PathBuf, destination: PathBuf| Inputs {
            catalog,
            lore,
            registry: root.join("registry"),
            receipts: root.join("receipts"),
            shell_journals: root.join("shell"),
            files_journals: root.join("files"),
            destination,
        };
        let seed = root.join("seed");
        prepare(inputs(old.join("catalog.redb"), old.join("lore"), seed.clone())).unwrap();
        // Actual current Catalog lifecycle on a private copy; old fixture is never changed.
        let catalog = Catalog::open(seed.join("catalog"), "fixture-account").unwrap();
        assert_eq!(catalog.abort("fixture-batch").unwrap().state, ActionState::Aborted);
        drop(catalog);
        let baseline = root.join("terminal-baseline");
        prepare(inputs(seed.join("catalog"), seed.join("lore"), baseline.clone())).unwrap();
        let container = root.join("validation");
        start(&baseline, &container).unwrap();
        let mut pages = 0;
        loop {
            let progress = advance(&baseline, &container, 1).await.unwrap();
            pages += 1;
            assert!(pages < 100);
            if progress.state == "coverage_complete_no_readiness" {
                assert_eq!(progress.references, 4);
                break;
            }
        }
        assert!(pages > 1);
        let cp = read_cp(&container.join("checkpoint.json")).unwrap();
        assert_eq!(cp.state, "incomplete");
        assert!(cp.verified.values().any(|x| x == "7f9907c250056c2d7fa7aed445377eb2d3f06d111e5e4ef52f37dab32fd87d30"));
        assert!(cp.verified.values().any(|x| x == "e2306c6367dab8e0f580a56d0023a55239547d84a3863de55d9c6d32d68ab7ca"));
        assert!(cp.verified.values().any(|x| x == "empty"));
        assert!(cp.verified.values().any(|x| x == "tombstone"));
        assert!(!container.join("ready").exists());
        let prepared = prepare_final(&baseline, &container).await.unwrap();
        assert_eq!(prepared.state, "prepared_candidate_only");
        assert_eq!(prepared.lore_format, crate::FORMAT_REVISION);
        assert_eq!(prepare_final(&baseline, &container).await.unwrap(), prepared);
        let receipt_path = container.join("preparation-receipt.json");
        fs::write(&receipt_path, b"{\"version\":99}").unwrap();
        assert!(prepare_final(&baseline, &container).await.is_err());

        // The original old-written batch remains pending and must not qualify.
        let pending_baseline = root.join("pending-baseline");
        prepare(inputs(old.join("catalog.redb"), old.join("lore"), pending_baseline.clone())).unwrap();
        let pending_container = root.join("pending-validation");
        start(&pending_baseline, &pending_container).unwrap();
        let mut pending_pages = 0;
        loop {
            match advance(&pending_baseline, &pending_container, 64).await {
                Ok(progress) => {
                    assert_ne!(progress.state, "coverage_complete_no_readiness");
                    pending_pages += 1;
                    assert!(pending_pages < 100);
                }
                Err(error) => {
                    assert!(error.to_string().contains("pending"), "{error}");
                    break;
                }
            }
        }
        assert!(pending_pages > 1);
        assert_eq!(read_cp(&pending_container.join("checkpoint.json")).unwrap().state, "incomplete");
        assert!(!pending_container.join("ready").exists());
    }
}
