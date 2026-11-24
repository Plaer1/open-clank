//! Exact personal-document removal and its content-free original fact.
use rusqlite::{params, OptionalExtension, Transaction};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use super::{sha256_hex, RepositoryError, ResetFenceBudget};
use super::owner_custody::VerifiedCustodyView;
use super::document_upsert::{sorted_json, source_lookup, DocumentOriginalRequest, DocumentSourceLookupRequest};

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentRemoveHead { pub revision: u64, pub content_sha256: String }
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentRemoveRequest {
    pub version: u8, pub operation_id: String, pub account_id: String,
    pub owner: String, pub captured_owner: String, pub source_id: String,
    pub source_uri: String, pub expected_head: DocumentRemoveHead,
}
fn unavailable()->RepositoryError { RepositoryError::Validation("document removal unavailable".into()) }
fn text(v:&str)->bool {!v.is_empty() && v.len()<=1024 && v.trim()==v && !v.contains('\0')}
fn hash(v:&str)->bool {v.len()==64 && v.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))}
fn key(account:&str,operation:&str)->Result<String,RepositoryError> {
    let mut bytes=b"openclank.document-remove.receipt.v1\n".to_vec();
    bytes.extend_from_slice(sorted_json(&json!([account,operation]),false)?.as_bytes());
    Ok(format!("cs_s01_document_remove_{}",sha256_hex(&bytes)))
}
impl DocumentRemoveRequest {
    fn validate(&self)->Result<(),RepositoryError> {
        if self.version!=1 || ![&self.operation_id,&self.account_id,&self.owner,&self.captured_owner,&self.source_id,&self.source_uri].iter().all(|v|text(v))
            || self.expected_head.revision==0 || self.expected_head.revision>i64::MAX as u64 || !hash(&self.expected_head.content_sha256) {return Err(unavailable());}
        Ok(())
    }
    fn digest(&self)->Result<String,RepositoryError> {
        self.validate()?; let mut bytes=b"openclank.document-remove.request.v1\n".to_vec();
        bytes.extend_from_slice(sorted_json(&serde_json::to_value(self)?,false)?.as_bytes()); Ok(sha256_hex(&bytes))
    }
}

pub(crate) fn original(tx:&Transaction<'_>,r:&DocumentOriginalRequest,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    r.validate()?; let captured=view.for_captured(tx,&r.captured_owner)?;
    if captured.account_id()!=r.account_id || captured.current_owner()!=r.current_owner {return Err(unavailable());}
    let id=key(&r.account_id,&r.operation_id)?;let event=format!("outbox_{id}_s01_document_remove");
    let lengths:Option<(i64,i64)>=tx.query_row("SELECT length(CAST(c.operation_metadata_json AS BLOB)),length(CAST(o.payload_json AS BLOB)) FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 AND o.kind='s01_document_remove' WHERE c.owner_id=?1 AND c.change_set_id=?2",params![r.current_owner,id,event],|row|Ok((row.get(0)?,row.get(1)?))).optional()?;
    let Some((a,b))=lengths else{return Ok(json!({"version":1,"state":"unknown"}));};
    if !(1..=8192).contains(&a)||!(1..=8192).contains(&b){return Err(unavailable());}budget.check((a+b) as usize).map_err(RepositoryError::Validation)?;
    let (digest,raw,payload,revision,actor):(String,String,String,i64,String)=tx.query_row("SELECT c.content_hash,c.operation_metadata_json,o.payload_json,o.source_revision,c.actor_id FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 AND o.kind='s01_document_remove' WHERE c.owner_id=?1 AND c.change_set_id=?2 AND c.reason='s01.document-remove.v1' AND c.actor_type='user' AND c.source_event_id IS NULL AND c.contract_version=?4",params![r.current_owner,id,event,crate::ontology::CONTRACT_VERSION],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?)))?;
    view.for_captured(tx,&actor)?;if actor!=r.captured_owner || raw!=payload || digest!=sha256_hex(raw.as_bytes()){return Err(unavailable());}
    let value:Value=serde_json::from_str(&raw).map_err(|_|unavailable())?;if sorted_json(&value,false)?!=raw{return Err(unavailable());}
    let obj=value.as_object().ok_or_else(unavailable)?;let keys=["version","state","operation_id","request_sha256","account_id","captured_owner","source_id","source_revision","documents_removed","chunks_removed"];
    if obj.len()!=keys.len() || keys.iter().any(|k|!obj.contains_key(*k)) || value["version"]!=1 || value["state"]!="removed" || value["operation_id"]!=r.operation_id || value["request_sha256"]!=r.request_sha256 || value["account_id"]!=r.account_id || value["captured_owner"]!=r.captured_owner || value["source_revision"].as_i64()!=Some(revision) || revision<=0 || value["source_id"].as_str().is_none_or(|v|!text(v)) || value["documents_removed"].as_u64().is_none_or(|v|v==0) || value["chunks_removed"].as_u64().is_none_or(|v|v==0) {return Err(unavailable());}
    let mut result=obj.clone();result.remove("account_id");result.remove("captured_owner");Ok(Value::Object(result))
}

/// Validate and remove only the selected source's FTS rows, then invalidate
/// that exact scope's lexical and vector pointers. Caller owns source authority.
pub(crate) fn clear_source_derived(tx:&Transaction<'_>,owner:&str,source:&str,workspace:&str,project:&str,budget:&mut ResetFenceBudget)->Result<u64,RepositoryError> {
    let mixed:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2 AND (workspace_key<>?3 OR project_key<>?4))",params![owner,source,workspace,project],|row|row.get(0))?;
    if mixed {return Err(unavailable());}
    if workspace.is_empty() && project.is_empty() {
        super::document_structure::revoke_file(tx, owner, source)?;
    }
    let mut base=tx.prepare("SELECT CASE WHEN length(CAST(c.chunk_id AS BLOB))<=1024 THEN c.chunk_id END,CASE WHEN length(CAST(c.text AS BLOB))<=262144 THEN c.text END FROM fm_v2_chunks c JOIN fm_v2_documents d ON d.owner_id=c.owner_id AND d.document_id=c.document_id WHERE c.owner_id=?1 AND d.source_id=?2 ORDER BY c.chunk_id")?;
    let mut fts=tx.prepare("SELECT CASE WHEN length(CAST(f.chunk_id AS BLOB))<=1024 THEN f.chunk_id END,CASE WHEN length(CAST(f.text AS BLOB))<=262144 THEN f.text END FROM fm_v2_chunks_fts f CROSS JOIN fm_v2_chunks c CROSS JOIN fm_v2_documents d WHERE f.owner_id=?1 AND c.owner_id=f.owner_id AND c.chunk_id=f.chunk_id AND d.owner_id=c.owner_id AND d.document_id=c.document_id AND d.source_id=?2 ORDER BY f.chunk_id")?;
    let mut a=base.query(params![owner,source])?;let mut b=fts.query(params![owner,source])?;let mut chunk_count=0_u64;
    loop {match(a.next()?,b.next()?){(None,None)=>break,(Some(x),Some(y))=>{let ai:Option<String>=x.get(0)?;let at:Option<String>=x.get(1)?;let bi:Option<String>=y.get(0)?;let bt:Option<String>=y.get(1)?;let (ai,at,bi,bt)=match(ai,at,bi,bt){(Some(a),Some(b),Some(c),Some(d))=>(a,b,c,d),_=>return Err(unavailable())};budget.check(ai.len()+at.len()+bi.len()+bt.len()).map_err(RepositoryError::Validation)?;if ai!=bi||at!=bt{return Err(unavailable());}chunk_count+=1;},_=>return Err(unavailable())}}
    drop(a);drop(b);drop(base);drop(fts);
    let removed=tx.execute("DELETE FROM fm_v2_chunks_fts WHERE owner_id=?1 AND chunk_id IN (SELECT c.chunk_id FROM fm_v2_chunks c JOIN fm_v2_documents d ON d.owner_id=c.owner_id AND d.document_id=c.document_id WHERE c.owner_id=?1 AND d.source_id=?2)",params![owner,source])?;
    if removed as u64!=chunk_count{return Err(unavailable());}
    tx.execute("DELETE FROM fm_v2_index_pointers WHERE owner_id=?1 AND workspace_key=?2 AND project_key=?3 AND logical_space IN ('documents_fts','documents_vector')",params![owner,workspace,project])?;
    Ok(chunk_count)
}

pub(crate) fn remove(tx:&Transaction<'_>,r:&DocumentRemoveRequest,view:&VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    r.validate()?;let captured=view.for_captured(tx,&r.captured_owner)?;
    if captured.account_id()!=r.account_id || captured.current_owner()!=r.owner || r.owner!=r.captured_owner {return Err(unavailable());}
    super::require_guarded_epoch(tx)?;
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.document_fts.v1'",[],|row|row.get(0)).optional()?;
    if marker.as_deref()!=Some("1"){return Err(unavailable());}
    let digest=r.digest()?;let selector=DocumentOriginalRequest{version:1,operation_id:r.operation_id.clone(),request_sha256:digest.clone(),account_id:r.account_id.clone(),current_owner:r.owner.clone(),captured_owner:r.captured_owner.clone()};
    let id=key(&r.account_id,&r.operation_id)?;let event=format!("outbox_{id}_s01_document_remove");
    let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![r.owner,id,event],|row|row.get(0))?;
    if exists{return original(tx,&selector,view,budget);}
    let lookup=DocumentSourceLookupRequest{version:1,account_id:r.account_id.clone(),current_owner:r.owner.clone(),captured_owner:r.captured_owner.clone(),source_uri:r.source_uri.clone()};
    let fact=source_lookup(tx,&lookup,view,budget)?;
    if fact["state"]!="present" || fact["source_id"].as_str()!=Some(r.source_id.as_str()) || fact["source_revision"].as_u64()!=Some(r.expected_head.revision) || fact["content_sha256"].as_str()!=Some(r.expected_head.content_sha256.as_str()) || fact["forget_state"]!="active" {return Err(unavailable());}
    let chunk_count=clear_source_derived(tx,&r.owner,&r.source_id,"","",budget)?;
    if chunk_count==0{return Err(unavailable());}
    let docs:i64=tx.query_row("SELECT count(*) FROM fm_v2_documents WHERE owner_id=?1 AND source_id=?2",params![r.owner,r.source_id],|row|row.get(0))?;if docs<=0{return Err(unavailable());}
    tx.execute("DELETE FROM fm_v2_chunk_embeddings WHERE owner_id=?1 AND chunk_id IN (SELECT c.chunk_id FROM fm_v2_chunks c JOIN fm_v2_documents d ON d.owner_id=c.owner_id AND d.document_id=c.document_id WHERE c.owner_id=?1 AND d.source_id=?2)",params![r.owner,r.source_id])?;
    if tx.execute("DELETE FROM fm_v2_chunks WHERE owner_id=?1 AND document_id IN (SELECT document_id FROM fm_v2_documents WHERE owner_id=?1 AND source_id=?2)",params![r.owner,r.source_id])? as u64!=chunk_count{return Err(unavailable());}
    if tx.execute("DELETE FROM fm_v2_documents WHERE owner_id=?1 AND source_id=?2",params![r.owner,r.source_id])? as i64!=docs{return Err(unavailable());}
    if tx.execute("UPDATE fm_v2_sources SET forget_state='forgotten' WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3 AND content_hash=?4 AND forget_state='active'",params![r.owner,r.source_id,r.expected_head.revision as i64,r.expected_head.content_sha256])?!=1{return Err(unavailable());}
    tx.execute("DELETE FROM fm_v2_sources AS s WHERE owner_id=?1 AND source_id=?2 AND source_revision<>?3 AND NOT EXISTS(SELECT 1 FROM fm_v2_evidence e WHERE e.owner_id=s.owner_id AND e.source_id=s.source_id AND e.source_revision=s.source_revision) AND NOT EXISTS(SELECT 1 FROM fm_v2_episodes e WHERE e.owner_id=s.owner_id AND e.source_id=s.source_id AND e.source_revision=s.source_revision) AND NOT EXISTS(SELECT 1 FROM fm_v2_candidate_revisions c WHERE c.owner_id=s.owner_id AND c.source_id=s.source_id AND c.source_revision=s.source_revision)",params![r.owner,r.source_id,r.expected_head.revision as i64])?;
    let receipt=json!({"version":1,"state":"removed","operation_id":r.operation_id,"request_sha256":digest,"account_id":r.account_id,"captured_owner":r.captured_owner,"source_id":r.source_id,"source_revision":r.expected_head.revision,"documents_removed":docs,"chunks_removed":chunk_count});
    let raw=sorted_json(&receipt,false)?;if raw.len()>8192{return Err(unavailable());}let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?3,'s01.document-remove.v1',NULL,?4,?5,?6,?7)",params![r.owner,id,r.captured_owner,crate::ontology::CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,'s01_document_remove',?4,?5,?6)",params![r.owner,event,id,raw,r.expected_head.revision as i64,now])?;
    original(tx,&selector,view,budget)
}


/// Prove all owner-keyed document FTS rows against canonical chunks before
/// and after owner lifecycle operations. No inferred source authority.
pub(crate) fn verify_owner_projection(tx:&Transaction<'_>,owner:&str,budget:&mut ResetFenceBudget)->Result<(),RepositoryError> {
    let mut base=tx.prepare("SELECT CASE WHEN length(CAST(chunk_id AS BLOB))<=1024 THEN chunk_id END,CASE WHEN length(CAST(text AS BLOB))<=262144 THEN text END FROM fm_v2_chunks WHERE owner_id=?1 ORDER BY chunk_id")?;
    let mut fts=tx.prepare("SELECT CASE WHEN length(CAST(chunk_id AS BLOB))<=1024 THEN chunk_id END,CASE WHEN length(CAST(text AS BLOB))<=262144 THEN text END FROM fm_v2_chunks_fts WHERE owner_id=?1 ORDER BY chunk_id")?;
    let mut a=base.query([owner])?;let mut b=fts.query([owner])?;
    loop {match(a.next()?,b.next()?){(None,None)=>break,(Some(x),Some(y))=>{
        let ai:Option<String>=x.get(0)?;let at:Option<String>=x.get(1)?;let bi:Option<String>=y.get(0)?;let bt:Option<String>=y.get(1)?;
        let (ai,at,bi,bt)=match(ai,at,bi,bt){(Some(a),Some(b),Some(c),Some(d))=>(a,b,c,d),_=>return Err(unavailable())};
        budget.check(ai.len()+at.len()+bi.len()+bt.len()).map_err(RepositoryError::Validation)?;
        if ai!=bi || at!=bt {return Err(unavailable());}
    },_=>return Err(unavailable())}}
    Ok(())
}
