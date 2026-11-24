//! Bounded checked worktree container v2. No mutation or public grant lives here.
//!
//! Layout is `MAGIC | u32 big-endian manifest length | JSON manifest | bytes`.
//! File offsets are relative to the byte area and must be gapless and ordered.
//! The single `.git` entry is always explicitly delegated to the Git owner.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs::File;
use std::io;
#[cfg(unix)] use std::os::unix::fs::{FileExt, MetadataExt};

pub const MAGIC: &[u8; 8] = b"OCKTREE2";
pub const MAX_ENTRIES: usize = 8192;
pub const MAX_TOTAL: u64 = 512 * 1024 * 1024;
pub const MAX_BLOB: u64 = 32 * 1024 * 1024;
pub const MAX_MANIFEST: usize = 4 * 1024 * 1024;
const CHUNK: usize = 128 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Manifest { pub version: u32, pub source: TreeSource, pub entries: Vec<Entry> }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TreeSource { pub commit_oid: String, pub tree_oid: String, pub target_manifest_sha256: String }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Entry {
    Directory { path: String, mode: u32 },
    File { path: String, mode: u32, offset: u64, length: u64, sha256: String },
    GitMetadata { path: String, mode: u32, offset: u64, length: u64, sha256: String, owner: GitOwner },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum GitOwner { SourceWorktree }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ActionCommitment {
    pub version: u32, pub allowed_after_format: String, pub commit_oid: String, pub tree_oid: String,
    pub target_manifest_sha256: String, pub journal_operation_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct JournalAfterCommitment {
    pub journal_operation_id: String, pub container_sha256: String, pub container_length: u64, pub entry_count: usize,
}

/// A future Native Files owner must construct this from its retained journal.
/// No production constructor exists until that exact custody join is reviewed.
pub(crate) struct VerifiedWorktreeStage {
    file: File, journal_operation_id: String, outcome: crate::catalog::ResourceOutcome,
}
impl VerifiedWorktreeStage {
    pub(crate) fn file(&self) -> &File { &self.file }
    pub(crate) fn journal_operation_id(&self) -> &str { &self.journal_operation_id }
    pub(crate) fn outcome(&self) -> &crate::catalog::ResourceOutcome { &self.outcome }
    pub(crate) fn from_files_stage(file: File, journal_operation_id: String, outcome: crate::catalog::ResourceOutcome) -> Self {
        Self {file,journal_operation_id,outcome}
    }
    #[cfg(test)]
    pub(crate) fn fixture(file: File, journal_operation_id: String, outcome: crate::catalog::ResourceOutcome) -> Self {
        Self {file,journal_operation_id,outcome}
    }
}

/// Capability remains unconstructible in production until Source's authenticated
/// worktree restore owner supplies its journal-bound stage and account proof.
pub(crate) struct VerifiedWorktreeRead {
    action_id: String, account_id: String, version_id: String, file: File,
}
impl VerifiedWorktreeRead {
    pub(crate) fn action_id(&self) -> &str { &self.action_id }
    pub(crate) fn account_id(&self) -> &str { &self.account_id }
    pub(crate) fn version_id(&self) -> &str { &self.version_id }
    pub(crate) fn file(&self) -> &File { &self.file }
    pub(crate) fn from_files_stage(action_id: String, account_id: String, version_id: String, file: File) -> Self {
        Self { action_id, account_id, version_id, file }
    }
    #[cfg(test)]
    pub(crate) fn fixture(action_id: String, account_id: String, version_id: String, file: File) -> Self {
        Self { action_id, account_id, version_id, file }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Summary { pub entries: usize, pub bytes: u64, pub container_sha256: String }

/// Reserved durable provenance; generic readers must fail closed on presence,
/// including malformed values, before materializing any candidate blob.
pub(crate) fn action_marks_tree(action: &crate::catalog::ActionRecord) -> bool {
    action.coverage.as_ref().and_then(|coverage|coverage.metadata.as_ref())
        .and_then(serde_json::Value::as_object).is_some_and(|metadata|metadata.contains_key("source_worktree_tree_v2"))
}

fn invalid() -> io::Error { io::Error::new(io::ErrorKind::InvalidData, "tree container unavailable") }
fn hex_digest(value: &str) -> bool { value.len() == 64 && value.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) }
fn oid(value: &str) -> bool { matches!(value.len(), 40 | 64) && value.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) }

/// The fixed Git-tree commitment excludes only the separately bound Git marker.
pub fn target_manifest_sha256(entries: &[Entry]) -> io::Result<String> {
    let mut digest=Sha256::new();digest.update(b"openclank.worktree.target.v2\0");
    for entry in entries {
        let (kind,path,mode,part)=match entry {
            Entry::Directory{path,mode} => (b'd',path,*mode,None),
            Entry::File{path,mode,length,sha256,..} => (b'f',path,*mode,Some((*length,sha256))),
            Entry::GitMetadata{..} => continue,
        };
        let size=u32::try_from(path.len()).map_err(|_|invalid())?;
        digest.update([kind]);digest.update(size.to_be_bytes());digest.update(path.as_bytes());digest.update(mode.to_be_bytes());
        if let Some((length,sha))=part {if !hex_digest(sha) {return Err(invalid());}digest.update(length.to_be_bytes());digest.update(sha.as_bytes());}
    }
    Ok(format!("{:x}",digest.finalize()))
}

fn safe_path(value: &str, marker: bool) -> bool {
    if value.is_empty() || value.len() > 4096 || value.starts_with('/') || value.contains('\\') || value.contains('\0') { return false; }
    let parts: Vec<_> = value.split('/').collect();
    if parts.iter().any(|p| p.is_empty() || *p == "." || *p == ".." || matches!(*p, ".clanker" | ".clankers" | ".references")) { return false; }
    if marker { value == ".git" } else { !parts.contains(&".git") }
}

#[cfg(unix)]
fn read_exact_at(file: &File, mut offset: u64, out: &mut [u8]) -> io::Result<()> {
    let mut done=0;
    while done<out.len() {
        let n=file.read_at(&mut out[done..],offset)?;
        if n==0 { return Err(invalid()); }
        done+=n;offset=offset.checked_add(n as u64).ok_or_else(invalid)?;
    }
    Ok(())
}

/// Validate the whole pinned file, including every binary blob and marker byte.
/// The caller must hold its original staged-file custody through publication.
#[cfg(unix)]
pub fn validate_pinned(file: &File) -> io::Result<(Manifest, Summary)> {
    let before=file.metadata()?;
    if !before.is_file() || before.nlink()!=1 || before.len()<12 || before.len()>MAX_TOTAL+MAX_MANIFEST as u64+12 { return Err(invalid()); }
    let mut prefix=[0u8;12];read_exact_at(file,0,&mut prefix)?;
    if &prefix[..8]!=MAGIC {return Err(invalid());}
    let header_len=u32::from_be_bytes(prefix[8..12].try_into().map_err(|_|invalid())?) as usize;
    if header_len==0 || header_len>MAX_MANIFEST {return Err(invalid());}
    let mut header=vec![0u8;header_len];read_exact_at(file,12,&mut header)?;
    let manifest:Manifest=serde_json::from_slice(&header).map_err(|_|invalid())?;
    if manifest.version!=2 || manifest.entries.is_empty() || manifest.entries.len()>MAX_ENTRIES
        || !oid(&manifest.source.commit_oid) || !oid(&manifest.source.tree_oid)
        || manifest.source.commit_oid.len()!=manifest.source.tree_oid.len()
        || !hex_digest(&manifest.source.target_manifest_sha256)
        || target_manifest_sha256(&manifest.entries)?!=manifest.source.target_manifest_sha256 {return Err(invalid());}
    let mut seen=BTreeSet::new();let mut directories=BTreeSet::new();let mut marker_count=0usize;let mut total=0u64;
    let mut full=Sha256::new();full.update(prefix);full.update(&header);
    let data_start=12u64.checked_add(header_len as u64).ok_or_else(invalid)?;
    let mut buffer=vec![0u8;CHUNK];
    for entry in &manifest.entries {
        let (path,mode,part,marker)=match entry {
            Entry::Directory{path,mode} => (path,*mode,None,false),
            Entry::File{path,mode,offset,length,sha256} => (path,*mode,Some((*offset,*length,sha256)),false),
            Entry::GitMetadata{path,mode,offset,length,sha256,owner:GitOwner::SourceWorktree} => (path,*mode,Some((*offset,*length,sha256)),true),
        };
        if !safe_path(path,marker) || !seen.insert(path.clone()) || (if marker {!matches!(mode,0o600|0o644)} else {!matches!(mode,0o644|0o755)}) {return Err(invalid());}
        if let Some((parent,_))=path.rsplit_once('/') {if !directories.contains(parent) {return Err(invalid());}}
        if marker {marker_count+=1;}
        if let Some((offset,length,sha))=part {
            if offset!=total || length>MAX_BLOB || !hex_digest(sha) {return Err(invalid());}
            total=total.checked_add(length).filter(|n|*n<=MAX_TOTAL).ok_or_else(invalid)?;
            let mut hash=Sha256::new();let mut read=0u64;
            while read<length {
                let count=((length-read).min(CHUNK as u64)) as usize;
                read_exact_at(file,data_start.checked_add(offset).and_then(|n|n.checked_add(read)).ok_or_else(invalid)?,&mut buffer[..count])?;
                hash.update(&buffer[..count]);full.update(&buffer[..count]);read+=count as u64;
            }
            if format!("{:x}",hash.finalize())!=*sha {return Err(invalid());}
        } else {
            if mode!=0o755 {return Err(invalid());}
            directories.insert(path.clone());
        }
    }
    if marker_count!=1 || data_start.checked_add(total)!=Some(before.len()) {return Err(invalid());}
    let after=file.metadata()?;
    if (before.dev(),before.ino(),before.len(),before.nlink(),before.mtime(),before.mtime_nsec(),before.ctime(),before.ctime_nsec())
        !=(after.dev(),after.ino(),after.len(),after.nlink(),after.mtime(),after.mtime_nsec(),after.ctime(),after.ctime_nsec()) {return Err(invalid());}
    Ok((manifest,Summary{entries:seen.len(),bytes:before.len(),container_sha256:format!("{:x}",full.finalize())}))
}

/// Preserve the existing action idempotency digest without loading the tree.
#[cfg(unix)]
pub fn capture_digest_pinned(file: &File, fingerprint: &str, expected: &Summary) -> io::Result<String> {
    if fingerprint.len()>4096 || validate_pinned(file)?.1!=*expected {return Err(invalid());}
    let mut action=blake3::Hasher::new();action.update(fingerprint.as_bytes());action.update(&[0]);
    let mut sha=Sha256::new();let mut offset=0u64;let mut buffer=vec![0u8;CHUNK];
    while offset<expected.bytes {
        let count=((expected.bytes-offset).min(CHUNK as u64)) as usize;
        read_exact_at(file,offset,&mut buffer[..count])?;action.update(&buffer[..count]);sha.update(&buffer[..count]);offset+=count as u64;
    }
    if format!("{:x}",sha.finalize())!=expected.container_sha256 || file.metadata()?.len()!=expected.bytes {return Err(invalid());}
    Ok(action.finalize().to_hex().to_string())
}

#[cfg(not(unix))]
pub fn capture_digest_pinned(_file: &File, _fingerprint: &str, _expected: &Summary) -> io::Result<String> {
    Err(io::Error::new(io::ErrorKind::Unsupported,"tree container unavailable"))
}

#[cfg(not(unix))]
pub fn validate_pinned(_file: &File) -> io::Result<(Manifest, Summary)> { Err(io::Error::new(io::ErrorKind::Unsupported,"tree container unavailable")) }


#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use crate::restore::{FilesystemRestoreProvider, HostMetadata, RestoreProvider};
    use crate::HistoryStore;
    use lore_storage::{Context, Partition};
    use std::io::Write;

    fn container() -> Vec<u8> {
        let blob:Vec<u8>=(0..700_000).map(|n|(n%251) as u8).collect();
        let marker=b"gitdir: private-fixture\n";
        let entries=vec![
            Entry::Directory{path:"nested".into(),mode:0o755},
            Entry::File{path:"nested/binary".into(),mode:0o644,offset:0,length:blob.len() as u64,sha256:format!("{:x}",Sha256::digest(&blob))},
            Entry::GitMetadata{path:".git".into(),mode:0o644,offset:blob.len() as u64,length:marker.len() as u64,sha256:format!("{:x}",Sha256::digest(marker)),owner:GitOwner::SourceWorktree},
        ];
        let source=TreeSource{commit_oid:"a".repeat(40),tree_oid:"b".repeat(40),target_manifest_sha256:target_manifest_sha256(&entries).unwrap()};
        let manifest=Manifest{version:2,source,entries};
        let header=serde_json::to_vec(&manifest).unwrap();
        let mut out=MAGIC.to_vec();out.extend_from_slice(&(header.len() as u32).to_be_bytes());out.extend_from_slice(&header);out.extend_from_slice(&blob);out.extend_from_slice(marker);out
    }

    #[tokio::test]
    async fn pinned_stream_roundtrip_and_generic_refusal() {
        let temp=tempfile::tempdir().unwrap();let stage=temp.path().join("stage");let moved=temp.path().join("held");
        let expected=container();std::fs::write(&stage,&expected).unwrap();
        let input=File::open(&stage).unwrap();let (_,summary)=validate_pinned(&input).unwrap();
        assert_eq!(capture_digest_pinned(&input,"fixture",&summary).unwrap(),crate::operations::capture_input_digest(Some(&expected),"fixture"));
        // Replacing the source name cannot redirect the held descriptor.
        std::fs::rename(&stage,&moved).unwrap();std::fs::write(&stage,b"replacement").unwrap();
        let store=HistoryStore::open(temp.path().join("lore")).await.unwrap();
        let partition=Partition::from([7;16]);let (address,written)=store.write_tree_pinned(partition,Context::from([8;16]),&input).await.unwrap();
        assert_eq!(written,summary);
        let output_path=temp.path().join("output");let output=std::fs::OpenOptions::new().read(true).write(true).create_new(true).open(&output_path).unwrap();
        store.read_tree_to_pinned_stage(partition,address,&summary,&output).await.unwrap();
        assert_eq!(std::fs::read(&output_path).unwrap(),expected);
        let bad_output=temp.path().join("bad-output");let bad_stage=std::fs::OpenOptions::new().read(true).write(true).create_new(true).open(&bad_output).unwrap();
        let mut forged=summary.clone();forged.container_sha256="0".repeat(64);
        assert!(store.read_tree_to_pinned_stage(partition,address,&forged,&bad_stage).await.is_err());
        let header_len=u32::from_be_bytes(expected[8..12].try_into().unwrap()) as usize;
        let mut manifest:serde_json::Value=serde_json::from_slice(&expected[12..12+header_len]).unwrap();
        manifest["entries"][2]["mode"]=serde_json::json!(0o600);
        let header=serde_json::to_vec(&manifest).unwrap();let mut private_marker=MAGIC.to_vec();private_marker.extend_from_slice(&(header.len() as u32).to_be_bytes());private_marker.extend_from_slice(&header);private_marker.extend_from_slice(&expected[12+header_len..]);
        let private_marker_path=temp.path().join("private-marker");std::fs::write(&private_marker_path,&private_marker).unwrap();assert!(validate_pinned(&File::open(&private_marker_path).unwrap()).is_ok());
        manifest["entries"][1]["mode"]=serde_json::json!(0o600);let header=serde_json::to_vec(&manifest).unwrap();let mut private_file=MAGIC.to_vec();private_file.extend_from_slice(&(header.len() as u32).to_be_bytes());private_file.extend_from_slice(&header);private_file.extend_from_slice(&expected[12+header_len..]);
        std::fs::write(&private_marker_path,&private_file).unwrap();assert!(validate_pinned(&File::open(&private_marker_path).unwrap()).is_err());
        manifest["entries"][1]["mode"]=serde_json::json!(0o644);
        manifest["entries"][1]["path"]=serde_json::json!("../escape");
        let header=serde_json::to_vec(&manifest).unwrap();let mut unsafe_tree=MAGIC.to_vec();
        unsafe_tree.extend_from_slice(&(header.len() as u32).to_be_bytes());unsafe_tree.extend_from_slice(&header);unsafe_tree.extend_from_slice(&expected[12+header_len..]);
        let unsafe_path=temp.path().join("unsafe");std::fs::write(&unsafe_path,&unsafe_tree).unwrap();
        assert!(validate_pinned(&File::open(unsafe_path).unwrap()).is_err());
        let protected=temp.path().join("protected");std::fs::create_dir(&protected).unwrap();std::fs::write(protected.join("keep"),b"keep").unwrap();
        let mut provider=FilesystemRestoreProvider::new(&protected);
        let metadata=HostMetadata{resource_type:Some("Directory".into()),..Default::default()};
        assert!(provider.apply_if_revision_idempotent_with_metadata("fixture",None,&expected,Some(&metadata)).is_err());
        assert_eq!(std::fs::read(protected.join("keep")).unwrap(),b"keep");
        let bad=temp.path().join("bad");let mut bytes=expected.clone();*bytes.last_mut().unwrap()^=1;let mut handle=File::create(&bad).unwrap();handle.write_all(&bytes).unwrap();drop(handle);
        assert!(validate_pinned(&File::open(bad).unwrap()).is_err());
    }
    #[tokio::test]
    async fn action_capture_expiry_and_restore_refusal() {
        use crate::catalog::{ActionState, BeginResult, CaptureManifest, CommittedResource, LiveReceipt, LiveStatus,
            ResourceExistence, ResourceKey, ResourceMetadata, ResourceOutcome, ResourceType, Revision};
        use crate::operations::{begin, ActionRequest, BatchCaptureInput, HistoryCoordinator};
        use crate::restore::{prepare_restore_authorized, RestoreOutcome, RestoreRequest};
        let temp=tempfile::tempdir().unwrap(); let stage_path=temp.path().join("stage");
        let bytes=container(); std::fs::write(&stage_path,&bytes).unwrap();
        let file=File::open(&stage_path).unwrap();let (manifest,summary)=validate_pinned(&file).unwrap();
        let key=ResourceKey{account_id:"account".into(),workspace_id:"workspace".into(),provider:"files".into(),resource_id:"tree".into()};
        let operation="fixture-operation-0001";
        let commitment=serde_json::json!({"source_worktree_tree_v2":{"version":2,"allowed_after_format":"indexed_tree_v2", "commit_oid":manifest.source.commit_oid,"tree_oid":manifest.source.tree_oid,"target_manifest_sha256":manifest.source.target_manifest_sha256,"journal_operation_id":operation}});
        let c=HistoryCoordinator::open(temp.path().join("catalog"),temp.path().join("lore"),"account").await.unwrap();
        let req=ActionRequest{schema_version:1,action_id:"worktree-action".into(),actor_account_id:"account".into(),resource_key:key.clone(),physical_lease_keys:vec![],guard_resource_ids:vec![],modified_resource_ids:vec![],operation:"source_worktree_create_v2".into(),expected_revision:None,actor_id:"actor".into(),actor_kind:"user".into(),managed_tool_origin_v1:None,session_id:None,run_id:None,task_id:None,tool_id:None,before_revision:None,expected_after_revision:None,original_locator:None,destination_locator:None,timestamp_millis:None,coverage:Some(CaptureManifest{metadata:Some(commitment),..Default::default()}),per_resource_outcomes:None};
        let mut inverse_req = req.clone();
        assert!(matches!(begin(c.catalog(),req).unwrap(),BeginResult::New(_)));
        c.capture_batch_before("worktree-action",vec![BatchCaptureInput{resource_key:key.clone(),old_locator:None,new_locator:None,expected_revision:None,existence:ResourceExistence::Absent,resource_type:ResourceType::Directory,metadata:ResourceMetadata::default(),content:None,fingerprint:"missing".into(),coverage:CaptureManifest::default()}]).await.unwrap();
        c.begin_apply("worktree-action").unwrap();
        let fp=format!("sha256:{}:{}",summary.container_sha256,summary.bytes);
        let coverage=CaptureManifest{content_digest:Some(fp.clone()),byte_len:Some(summary.bytes),metadata:Some(serde_json::json!({"source_worktree_tree_v2_after":{"journal_operation_id":operation,"container_sha256":summary.container_sha256,"container_length":summary.bytes,"entry_count":summary.entries}})),..Default::default()};
        c.record_live("worktree-action",LiveReceipt{action_id:"worktree-action".into(),status:LiveStatus::Committed,fingerprint:Some(fp.clone()),committed_resources:vec![CommittedResource{resource_key:key.clone(),locator:None,existence:ResourceExistence::Present,resource_type:ResourceType::Directory,metadata:ResourceMetadata::default(),fingerprint:fp.clone(),coverage}]}).unwrap();
        let outcome=ResourceOutcome{resource_id:key.resource_id.clone(),status:"Committed".into(),revision:Some(Revision::Opaque{kind:"fingerprint".into(),value:fp})};
        let wrong=VerifiedWorktreeStage::fixture(file.try_clone().unwrap(),"another-operation-0001".into(),outcome.clone());
        assert!(c.capture_after_worktree_tree("worktree-action",&wrong).await.is_err());
        let stage=VerifiedWorktreeStage::fixture(file,operation.into(),outcome);
        let captured=c.capture_after_worktree_tree("worktree-action",&stage).await.unwrap();
        assert_eq!(captured.state,ActionState::AfterDurable); assert_eq!(captured.after_resources.len(),1);
        assert!(c.capture_after_worktree_tree("worktree-action",&stage).await.is_ok());
        let complete=c.complete("worktree-action").unwrap();let receipt=complete.after.unwrap();
        assert!(c.read_version("worktree-action",&receipt).await.is_err());
        let request=RestoreRequest{restore_id:"restore".into(),account_id:"account".into(),source_action_id:"worktree-action".into(),source_version_id:receipt.version_id.clone(),destination:key.clone(),expected_destination_fingerprint:None,require_current_capture:false};
        assert!(matches!(prepare_restore_authorized(&c,&request,&receipt,None).await,Err(RestoreOutcome::Unauthorized)));
        let output_path=temp.path().join("out");let output=std::fs::OpenOptions::new().read(true).write(true).create_new(true).open(&output_path).unwrap();
        let authority=VerifiedWorktreeRead::fixture("worktree-action".into(),"account".into(),receipt.version_id.clone(),output);
        c.read_worktree_tree_to_stage(&authority).await.unwrap();assert_eq!(std::fs::read(output_path).unwrap(),bytes);
        inverse_req.action_id = "worktree-inverse".into();
        inverse_req.operation = "source_worktree_inverse_v2".into();
        inverse_req.actor_id = "current-human".into();
        inverse_req.actor_kind = "human".into();
        inverse_req.coverage = Some(CaptureManifest { metadata: Some(serde_json::json!({"source_worktree_tree_v2": {
            "journal_operation_id":"inverse-operation-0001", "original_action_id":"worktree-action", "original_version_id":receipt.version_id,
            "original_journal_operation_id":operation, "original_container_sha256":summary.container_sha256,
            "original_container_length":summary.bytes
        }})), ..Default::default() });
        assert!(matches!(begin(c.catalog(),inverse_req).unwrap(),BeginResult::New(_)));
        let inverse_path=temp.path().join("inverse-stage");
        let inverse_stage=std::fs::OpenOptions::new().read(true).write(true).create_new(true).open(&inverse_path).unwrap();
        let inverse=c.prepare_worktree_inverse_before("worktree-action",&receipt.version_id,"worktree-inverse",&inverse_stage).await.unwrap();
        assert_eq!(inverse.state,ActionState::BeforeDurable);
        assert_eq!(inverse.before_resources.len(),1);
        let inverse_receipt=inverse.before.unwrap();
        assert_ne!(inverse_receipt.content,receipt.content);
        assert!(c.reclaim_checked("worktree-action",1).await.is_err());
        c.begin_apply("worktree-inverse").unwrap();
        c.record_live("worktree-inverse",LiveReceipt{action_id:"worktree-inverse".into(),status:LiveStatus::Committed,
            fingerprint:Some("missing".into()),committed_resources:vec![CommittedResource{resource_key:key.clone(),locator:None,
                existence:ResourceExistence::Absent,resource_type:ResourceType::Directory,metadata:ResourceMetadata::default(),
                fingerprint:"missing".into(),coverage:CaptureManifest{metadata:Some(serde_json::json!({"source_worktree_inverse_v2":{"journal_operation_id":"inverse-operation-0001"}})),..Default::default()}}]}).unwrap();
        assert!(c.capture_inverse_absence("worktree-inverse","wrong-operation").await.is_err());
        let inverse_after=c.capture_inverse_absence("worktree-inverse","inverse-operation-0001").await.unwrap();
        assert_eq!(inverse_after.state,ActionState::AfterDurable);
        assert_eq!(inverse_after.after.unwrap().content,crate::catalog::VersionContent::Tombstone);
        assert_eq!(c.complete("worktree-inverse").unwrap().state,ActionState::Complete);
        c.reclaim_checked("worktree-action",1).await.unwrap();
        let after_expiry=temp.path().join("expired");let output=std::fs::OpenOptions::new().read(true).write(true).create_new(true).open(after_expiry).unwrap();
        let authority=VerifiedWorktreeRead::fixture("worktree-action".into(),"account".into(),receipt.version_id,output);
        assert!(c.read_worktree_tree_to_stage(&authority).await.is_err());
        let partition=Partition::from(c.catalog().owner_partition("account").unwrap());
        drop(c);
        let store=HistoryStore::open(temp.path().join("lore")).await.unwrap();
        let crate::catalog::VersionContent::Bytes(reference)=inverse_receipt.content else {panic!("missing inverse version")};
        let address=lore_storage::Address{context:Context::from(reference.context),hash:reference.hash_hex.parse().unwrap()};
        let retained_path=temp.path().join("inverse-retained");
        let retained=std::fs::OpenOptions::new().read(true).write(true).create_new(true).open(&retained_path).unwrap();
        store.read_tree_to_pinned_stage(partition,address,&summary,&retained).await.unwrap();
        assert_eq!(std::fs::read(retained_path).unwrap(),bytes);
    }

}
