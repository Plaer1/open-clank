//! Derived mutation invalidation for the optional root document v2 profile.
//! Epochs never grant document or receipt access.
use super::*;
const PREFIX:&str="openclank.document_epoch.v2:";
const MARKER:&str="openclank.document_vector_batch.v2";
fn key(owner:&str)->Result<String,RepositoryError>{
    use std::fmt::Write;
    if owner.is_empty()||owner.len()>512||owner.trim()!=owner||owner.chars().any(char::is_control){return Err(RepositoryError::NotFound)}
    let mut value=String::with_capacity(PREFIX.len()+owner.len()*2);value.push_str(PREFIX);
    for byte in owner.as_bytes(){write!(&mut value,"{byte:02X}").map_err(|_|RepositoryError::NotFound)?;}Ok(value)
}
pub(crate) fn enabled(tx:&Transaction<'_>)->Result<bool,RepositoryError>{
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key=?1",[MARKER],|r|r.get(0)).optional()?;
    match marker.as_deref(){None=>Ok(false),Some("1")=>Ok(true),_=>Err(RepositoryError::NotFound)}
}
pub(crate) fn read(tx:&Transaction<'_>,owner:&str)->Result<Option<i64>,RepositoryError>{
    if !enabled(tx)?{return Err(RepositoryError::NotFound)}
    let raw:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key=?1",[key(owner)?],|r|r.get(0)).optional()?;
    raw.map(|value|{let n=value.parse::<i64>().map_err(|_|RepositoryError::NotFound)?;
        if n<1||n.to_string()!=value{return Err(RepositoryError::NotFound)} Ok(n)}).transpose()
}
fn v2_or_unprovable(tx:&Transaction<'_>,owner:&str)->Result<bool,RepositoryError>{
    Ok(tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_jobs j LEFT JOIN fm_v2_job_manifests m ON m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection' WHERE j.owner_id=?1 AND j.kind='document_vector_build.v1' AND (m.job_id IS NULL OR length(CAST(m.selected_json AS BLOB))>16384 OR CASE WHEN json_valid(m.selected_json) THEN json_extract(m.selected_json,'$.version') ELSE NULL END IS NOT 1)) OR EXISTS(SELECT 1 FROM fm_v2_job_manifests m LEFT JOIN fm_v2_jobs j ON j.owner_id=m.owner_id AND j.job_id=m.job_id WHERE m.owner_id=?1 AND (substr(m.job_id,1,11)='job_vbuild_' OR substr(m.manifest_id,1,11)='job_vbuild_') AND (j.job_id IS NULL OR j.kind<>'document_vector_build.v1'))",[owner],|r|r.get(0))?)
}
/// Existing v2 originals must never cause an absent counter to be initialized again.
pub(crate) fn require_if_v2(tx:&Transaction<'_>,owner:&str)->Result<(),RepositoryError>{
    if v2_or_unprovable(tx,owner)? && read(tx,owner)?.is_none(){return Err(RepositoryError::NotFound)}Ok(())
}
/// Called only inside the Immediate transaction which then validates the full snapshot.
pub(crate) fn initialize_for_original(tx:&Transaction<'_>,owner:&str)->Result<i64,RepositoryError>{
    if let Some(value)=read(tx,owner)?{return Ok(value)}
    // Missing after any v2 original (including malformed/orphan metadata) is
    // unprovable, never an invitation to reset its counter.
    if v2_or_unprovable(tx,owner)? {return Err(RepositoryError::NotFound)}
    let key=key(owner)?;
    crate::writer_fence::with_document_epoch_init_authority(tx,||tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,'1')",[key]))?;
    Ok(1)
}
pub(crate) fn require(tx:&Transaction<'_>,owner:&str,expected:i64)->Result<(),RepositoryError>{
    if expected<1 || read(tx,owner)?!=Some(expected){return Err(RepositoryError::NotFound)}Ok(())
}
/// Guarded purge removes this metadata only after the family is retired.
pub(crate) fn purge(tx:&Transaction<'_>,owner:&str)->Result<(),RepositoryError>{
    if !enabled(tx)?{return Ok(())}require_if_v2(tx,owner)?;let key=key(owner)?;
    crate::writer_fence::with_document_epoch_lifecycle_authority(tx,||tx.execute("DELETE FROM fm_meta WHERE key=?1",[key]))?;Ok(())
}
/// Old originals retain their captured name and are not revived at the target.
pub(crate) fn rename(tx:&Transaction<'_>,old:&str,new:&str)->Result<(),RepositoryError>{
    if !enabled(tx)?{return Ok(())}
    require_if_v2(tx,old)?;let old_key=key(old)?;let new_key=key(new)?;let old_epoch=read(tx,old)?;
    if old_epoch.is_none(){return Ok(())}
    match read(tx,new)? {
        Some(_)=>{tx.execute("UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key=?1",[&new_key])?;},
        None=>{crate::writer_fence::with_document_epoch_init_authority(tx,||tx.execute("INSERT INTO fm_meta(key,value) VALUES(?1,'1')",[&new_key]))?;},
    }
    crate::writer_fence::with_document_epoch_lifecycle_authority(tx,||tx.execute("DELETE FROM fm_meta WHERE key=?1",[old_key]))?;Ok(())
}
