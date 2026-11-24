// Draft: bounded, receipt-qualified keyset selection for V2; not registered.
use super::*;

#[derive(Debug, Clone, Serialize)]
pub(crate) struct BatchSourceRow {
    pub chunk_id:String, pub content_hash:String, pub source_id:String, pub source_revision:i64, pub text:String,
}

pub(crate) fn selected_after(
    tx:&Transaction<'_>, owner:&str, account:&str, after:Option<&str>, limit:usize,
    view:&owner_custody::VerifiedCustodyView<'_, '_>, budget:&mut ResetFenceBudget,
)->Result<Vec<BatchSourceRow>,RepositoryError>{
    if view.current_owner()!=owner || view.account_id()!=account || !(1..=16).contains(&limit)
        || after.is_some_and(|s|s.len()>512 || s.is_empty()) {return Err(RepositoryError::NotFound)}
    let mut stmt=tx.prepare("SELECT CASE WHEN length(CAST(c.chunk_id AS BLOB))<=1024 THEN c.chunk_id END,CASE WHEN length(CAST(c.content_hash AS BLOB))<=1024 THEN c.content_hash END,CASE WHEN length(CAST(c.text AS BLOB))<=65536 THEN c.text END,CASE WHEN length(CAST(c.locator_json AS BLOB))<=65536 THEN c.locator_json END,c.ordinal,CASE WHEN length(CAST(d.document_id AS BLOB))<=1024 THEN d.document_id END,CASE WHEN length(CAST(d.content_hash AS BLOB))<=1024 THEN d.content_hash END,CASE WHEN length(CAST(s.source_id AS BLOB))<=1024 THEN s.source_id END,s.source_revision,CASE WHEN length(CAST(s.source_uri AS BLOB))<=1024 THEN s.source_uri END,CASE WHEN length(CAST(s.content_hash AS BLOB))<=1024 THEN s.content_hash END FROM fm_v2_chunks c JOIN fm_v2_documents d ON d.owner_id=c.owner_id AND d.document_id=c.document_id JOIN fm_v2_sources s ON s.owner_id=d.owner_id AND s.source_id=d.source_id AND s.source_revision=d.source_revision WHERE c.owner_id=?1 AND c.chunk_id>?2 AND s.workspace_key='' AND s.project_key='' AND s.source_type='owner_imported_file' AND s.forget_state='active' AND d.parser_version='frankenmemory-rag-v2' AND NOT EXISTS(SELECT 1 FROM fm_v2_sources n WHERE n.owner_id=s.owner_id AND n.source_id=s.source_id AND n.source_revision>s.source_revision) ORDER BY c.chunk_id LIMIT ?3")?;
    let mut cursor=stmt.query(params![owner,after.unwrap_or(""),limit as i64])?;let mut result=Vec::new();
    while let Some(r)=cursor.next()? {
        let chunk:String=r.get(0)?;let hash:String=r.get(1)?;let text:Option<String>=r.get(2)?;let text=text.ok_or(RepositoryError::NotFound)?;let locator:Option<String>=r.get(3)?;let locator=locator.ok_or(RepositoryError::NotFound)?;let ordinal:i64=r.get(4)?;
        let doc:String=r.get(5)?;let doc_hash:String=r.get(6)?;let source:String=r.get(7)?;let revision:i64=r.get(8)?;let uri:String=r.get(9)?;let source_hash:String=r.get(10)?;
        let size=chunk.len()+hash.len()+text.len()+locator.len()+doc.len()+source.len()+uri.len()+source_hash.len();
        budget.check(size).map_err(RepositoryError::Validation)?;
        if text.len()>65536 || locator.len()>65536 || uri.is_empty() || uri.len()>1024 || uri.trim()!=uri || uri.contains('\0')
            || ordinal<0 || revision<=0 || !valid_sha256(&hash) || !valid_sha256(&source_hash) || sha256_hex(text.as_bytes())!=hash || doc_hash!=source_hash
            || source!=format!("src_{}",&sha256_hex(format!("{owner}\0\0\0{uri}").as_bytes())[..32])
            || doc!=format!("doc_{}",&sha256_hex(format!("{owner}\0{source}\0{revision}\0{source_hash}\0frankenmemory-rag-v2").as_bytes())[..32])
            || chunk!=format!("chunk_{}",&sha256_hex(format!("{owner}\0{doc}\0frankenmemory-rag-v2\0{ordinal}\0{locator}\0{hash}").as_bytes())[..32]) {return Err(RepositoryError::NotFound)}
        let mut receipts=tx.prepare("SELECT CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=8192 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND reason='s01.document-upsert.v1' AND json_valid(operation_metadata_json) AND json_extract(operation_metadata_json,'$.source_id')=?2 AND json_extract(operation_metadata_json,'$.source_revision')=?3 ORDER BY change_set_id")?;
        let mut found=receipts.query(params![owner,source,revision])?;let mut count=0usize;
        while let Some(record)=found.next()? {
            count+=1;
            let raw:Option<String>=record.get(0)?;let raw=raw.ok_or(RepositoryError::NotFound)?;
            budget.check(raw.len()).map_err(RepositoryError::Validation)?;
            let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
            let get=|name:&str|value.get(name).and_then(Value::as_str).map(str::to_owned).ok_or(RepositoryError::NotFound);
            if value.get("account_id").and_then(Value::as_str)!=Some(account) || value.get("document_id").and_then(Value::as_str)!=Some(doc.as_str()) {return Err(RepositoryError::NotFound)}
            let req=super::document_upsert::DocumentOriginalRequest{version:1,operation_id:get("operation_id")?,request_sha256:get("request_sha256")?,account_id:account.into(),current_owner:owner.into(),captured_owner:get("captured_owner")?};
            let proved=super::document_upsert::original(tx,&req,view,budget)?;
            if proved.get("state")!=Some(&json!("committed")) || proved.get("source_id")!=Some(&json!(source)) || proved.get("source_revision")!=Some(&json!(revision)) || proved.get("document_id")!=Some(&json!(doc)) {return Err(RepositoryError::NotFound)}
        }
        if count==0{return Err(RepositoryError::NotFound)}
        result.push(BatchSourceRow{chunk_id:chunk,content_hash:hash,source_id:source,source_revision:revision,text});
    }
    Ok(result)
}

/// Compare the complete publication pointer on every batch. Epoch equality
/// alone does not establish pointer or account authority.
pub(crate) fn require_pointer(tx:&Transaction<'_>,r:&super::document_vector::DocumentVectorBuildOriginal)->Result<(),RepositoryError>{
    let actual:Option<(String,String,String)>=tx.query_row("SELECT generation_id,publication_tx,publication_watermark FROM fm_v2_index_pointers WHERE owner_id=?1 AND logical_space='documents_vector' AND workspace_key='' AND project_key=''",[&r.owner],|v|Ok((v.get(0)?,v.get(1)?,v.get(2)?))).optional()?;
    let equal=match (&actual,&r.expected_pointer){(None,None)=>true,(Some((g,p,w)),Some(e))=>g==&e.generation_id&&p==&e.publication_id&&w==&e.publication_watermark,_=>false};
    if !equal{return Err(RepositoryError::NotFound)}
    if let Some((generation,publication,watermark))=actual {
        let proof:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_index_publications p JOIN fm_v2_derived_generations g ON g.owner_id=p.owner_id AND g.generation_id=p.generation_id WHERE p.owner_id=?1 AND p.publication_id=?2 AND p.logical_space='documents_vector' AND p.workspace_key='' AND p.project_key='' AND p.generation_id=?3 AND p.publication_watermark=?4 AND p.action IN ('publish','rollback') AND g.state='ready' AND g.retention_state='retained')",params![r.owner,publication,generation,watermark],|v|v.get(0))?;
        if !proof{return Err(RepositoryError::NotFound)}
    }Ok(())
}
/// The caller has already proved the active claim and freshly re-read these
/// canonical rows in this transaction. No partial or conflicting batch lands.
pub(crate) fn append_rows(tx:&Transaction<'_>,original:&super::document_vector::DocumentVectorBuildOriginal,stage:&str,
    selected:&[BatchSourceRow],rows:&[super::document_vector::VectorInputRow],budget:&mut ResetFenceBudget)->Result<(),RepositoryError>{
    if selected.is_empty()||selected.len()!=rows.len()||selected.len()>16{return Err(RepositoryError::NotFound)}
    let mut blobs=Vec::with_capacity(rows.len());
    for (source,row) in selected.iter().zip(rows){
        if row.chunk_id!=source.chunk_id||row.content_hash!=source.content_hash||row.source_revision!=source.source_revision
            ||row.values.len()!=original.dimension||row.values.iter().any(|v|!v.is_finite()){return Err(RepositoryError::NotFound)}
        let norm:f64=row.values.iter().map(|v|(*v as f64)*(*v as f64)).sum();
        if !norm.is_finite()||(norm.sqrt()-1.).abs()>1e-5{return Err(RepositoryError::NotFound)}
        let blob:Vec<u8>=row.values.iter().flat_map(|v|v.to_bits().to_le_bytes()).collect();
        budget.check(blob.len()+row.chunk_id.len()+row.content_hash.len()).map_err(RepositoryError::Validation)?;
        let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_chunk_embeddings WHERE owner_id=?1 AND generation_id=?2 AND chunk_id=?3)",params![original.owner,stage,row.chunk_id],|v|v.get(0))?;
        if exists{return Err(RepositoryError::NotFound)}blobs.push(blob);
    }
    let now=chrono::Utc::now().to_rfc3339();
    for (row,blob) in rows.iter().zip(blobs){tx.execute("INSERT INTO fm_v2_chunk_embeddings(owner_id,generation_id,chunk_id,dimension,embedding,content_hash,created_at) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![original.owner,stage,row.chunk_id,original.dimension as i64,blob,row.content_hash,now])?;}
    Ok(())
}

// Draft only: append-only V2 claim and terminal facts.
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct Item {chunk_id:String,content_hash:String,source_id:String,source_revision:i64}
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct BatchFact {
    version:u8, phase:String, job_id:String, operation_id:String, account_id:String,
    original_sha256:String, stage_id:String, attempt_id:String, ordinal:i64,
    offset:usize, items:Vec<Item>, request_sha256:String, previous_terminal_sha256:Option<String>,
    claim_sha256:Option<String>, vector_sha256:Option<String>, row_vector_sha256:Option<Vec<String>>,
}
fn fact_raw(f:&BatchFact)->Result<String,RepositoryError>{
    let value=serde_json::to_value(f).map_err(|_|RepositoryError::NotFound)?;
    let raw=super::document_upsert::sorted_json(&value,false)?;
    if raw.len()>16_384 {return Err(RepositoryError::NotFound)}Ok(raw)
}
fn receipt_id(job:&str,seq:i64)->String {format!("cs_dvbatch_{}",sha256_hex(format!("openclank.document-vector-batch.v2\0{job}\0{seq}").as_bytes()))}
fn write_fact(tx:&Transaction<'_>,owner:&str,job:&str,seq:i64,state:&str,attempt:&str,fact:&BatchFact)->Result<String,RepositoryError>{
    let raw=fact_raw(fact)?;let digest=sha256_hex(raw.as_bytes());let id=receipt_id(job,seq);
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?1,'document_vector.batch.v2',NULL,?3,?4,?5,?6)",params![owner,id,crate::ontology::CONTRACT_VERSION,digest,now,raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,state,created_at) VALUES(?1,?2,?3,'document_vector_batch_v2',?4,?5,'pending',?6)",params![owner,format!("outbox_{id}_document_vector_batch_v2"),id,raw,seq,now])?;
    tx.execute("INSERT INTO fm_v2_job_events(owner_id,job_id,sequence,state,attempt_id,lease_epoch,result_json,error_json,change_set_id,created_at) VALUES(?1,?2,?3,?4,?5,1,?6,NULL,?7,?8)",params![owner,job,seq,state,attempt,raw,id,now])?;
    Ok(digest)
}
fn read_fact(tx:&Transaction<'_>,owner:&str,job:&str,seq:i64,expected_actor:&str,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<(BatchFact,String,String),RepositoryError>{
    let id=receipt_id(job,seq);
    let (state,attempt,raw,hash,payload,revision,actor,contract):(String,String,String,String,String,i64,String,String)=tx.query_row("SELECT e.state,e.attempt_id,CASE WHEN length(CAST(e.result_json AS BLOB))<=16384 THEN e.result_json END,c.content_hash,CASE WHEN length(CAST(o.payload_json AS BLOB))<=16384 THEN o.payload_json END,o.source_revision,c.actor_id,c.contract_version FROM fm_v2_job_events e JOIN fm_v2_change_sets c ON c.owner_id=e.owner_id AND c.change_set_id=e.change_set_id JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id='outbox_'||c.change_set_id||'_document_vector_batch_v2' WHERE e.owner_id=?1 AND e.job_id=?2 AND e.sequence=?3 AND e.change_set_id=?4 AND e.lease_epoch=1 AND e.error_json IS NULL AND c.actor_type='user' AND c.source_event_id IS NULL AND c.reason='document_vector.batch.v2' AND c.operation_metadata_json=e.result_json AND o.kind='document_vector_batch_v2'",params![owner,job,seq,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?)))?;
    budget.check(raw.len()+payload.len()).map_err(RepositoryError::Validation)?;
    if raw.len()>16_384||raw!=payload||hash!=sha256_hex(raw.as_bytes())||revision!=seq||actor!=expected_actor||contract!=crate::ontology::CONTRACT_VERSION{return Err(RepositoryError::NotFound)}
    view.for_captured(tx,&actor)?;
    let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
    let fact:BatchFact=serde_json::from_value(value).map_err(|_|RepositoryError::NotFound)?;
    if fact_raw(&fact)?!=raw||fact.version!=2||fact.job_id!=job||fact.attempt_id!=attempt||!matches!((fact.phase.as_str(),state.as_str()),("claim","active")|("terminal","succeeded")|("cancelled","cancelled")) {return Err(RepositoryError::NotFound)}
    Ok((fact,hash,state))
}
/// Only this terminal cursor can authorize selecting the next batch. Earlier
/// records are immutable under the optional schema; each accepted transition
/// links the previously verified terminal hash, so this is an induction, not
/// a count-based reconstruction of missing receipts.
struct Tail { ordinal:i64, offset:usize, after:Option<String>, terminal_hash:Option<String>, active:Option<(BatchFact,String)> }
fn tail(tx:&Transaction<'_>,owner:&str,account:&str,operation:&str,job:&str,original_hash:&str,stage:&str,
    count:usize,width:usize,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Tail,RepositoryError>{
    let (state,n,current):(String,i64,Option<String>)=tx.query_row("SELECT state,attempt_count,current_attempt_id FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND kind='document_vector_build.v1'",params![owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
    if state!="active" || n<0 || !(1..=16).contains(&width) || (n as usize)>count.div_ceil(width) {return Err(RepositoryError::NotFound)}
    let extra:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2 AND attempt_number>?3) OR EXISTS(SELECT 1 FROM fm_v2_job_events WHERE owner_id=?1 AND job_id=?2 AND sequence>?4)",params![owner,job,n,2*n],|r|r.get(0))?;
    if extra{return Err(RepositoryError::NotFound)}
    if n==0 {
        if current.is_some() {return Err(RepositoryError::NotFound)}
        return Ok(Tail{ordinal:0,offset:0,after:None,terminal_hash:None,active:None})
    }
    let claim_seq=n.checked_mul(2).and_then(|v|v.checked_sub(1)).ok_or(RepositoryError::NotFound)?;
    let (claim,claim_hash,_)=read_fact(tx,owner,job,claim_seq,owner,view,budget)?;
    let offset=(n as usize-1).checked_mul(width).ok_or(RepositoryError::NotFound)?;
    let expected_count=(count-offset).min(width);
    let expected_attempt=format!("attempt_dv_{}",sha256_hex(format!("openclank.document-vector-attempt.v2\0{account}\0{operation}\0{offset}").as_bytes()));
    if claim.phase!="claim" || claim.account_id!=account || claim.operation_id!=operation || claim.original_sha256!=original_hash || claim.stage_id!=stage
        || claim.ordinal!=n || claim.offset!=offset || claim.attempt_id!=expected_attempt || claim.items.len()!=expected_count
        || claim.claim_sha256.is_some() || claim.vector_sha256.is_some() || claim.row_vector_sha256.is_some() || !valid_sha256(&claim.request_sha256)
        || (n==1)!=claim.previous_terminal_sha256.is_none() || claim.previous_terminal_sha256.as_ref().is_some_and(|v|!valid_sha256(v))
        || claim.items.windows(2).any(|w|w[0].chunk_id>=w[1].chunk_id)
        || claim.items.iter().any(|i|i.chunk_id.is_empty()||i.chunk_id.len()>512||i.source_id.len()!=36||!i.source_id.starts_with("src_")||!i.source_id[4..].bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c))||!valid_sha256(&i.content_hash)||i.source_revision<=0) {return Err(RepositoryError::NotFound)}
    let attempt:(String,i64,String)=tx.query_row("SELECT attempt_id,lease_epoch,state FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2 AND attempt_number=?3",params![owner,job,n],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
    if attempt.0!=expected_attempt||attempt.1!=1{return Err(RepositoryError::NotFound)}
    if let Some(active)=current {
        if active!=expected_attempt || attempt.2!="running" {return Err(RepositoryError::NotFound)}
        let terminal_exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_job_events WHERE owner_id=?1 AND job_id=?2 AND sequence=?3)",params![owner,job,2*n],|r|r.get(0))?;
        if terminal_exists{return Err(RepositoryError::NotFound)}
        return Ok(Tail{ordinal:n,offset,after:None,terminal_hash:None,active:Some((claim,claim_hash))})
    }
    if attempt.2!="succeeded" {return Err(RepositoryError::NotFound)}
    let (terminal,terminal_hash,_)=read_fact(tx,owner,job,2*n,owner,view,budget)?;
    if terminal.phase!="terminal" || terminal.account_id!=claim.account_id || terminal.operation_id!=claim.operation_id || terminal.original_sha256!=claim.original_sha256
        || terminal.stage_id!=claim.stage_id || terminal.attempt_id!=claim.attempt_id || terminal.ordinal!=claim.ordinal || terminal.offset!=claim.offset
        || serde_json::to_value(&terminal.items).ok()!=serde_json::to_value(&claim.items).ok() || terminal.request_sha256!=claim.request_sha256
        || terminal.previous_terminal_sha256!=claim.previous_terminal_sha256 || terminal.claim_sha256.as_deref()!=Some(claim_hash.as_str())
        || terminal.vector_sha256.as_ref().is_none_or(|v|!valid_sha256(v))
        || terminal.row_vector_sha256.as_ref().is_none_or(|v|v.len()!=terminal.items.len()||v.iter().any(|h|!valid_sha256(h))) {return Err(RepositoryError::NotFound)}
    let after=terminal.items.last().map(|v|v.chunk_id.clone()).ok_or(RepositoryError::NotFound)?;
    Ok(Tail{ordinal:n,offset:offset+expected_count,after:Some(after),terminal_hash:Some(terminal_hash),active:None})
}
fn batch_payload(intent:&super::document_vector_intent::BuildIntent,offset:usize,selected:&[BatchSourceRow])->Result<Value,RepositoryError>{
    let mut payload=intent.probe.as_object().cloned().ok_or(RepositoryError::NotFound)?;
    let key=format!("dv_{}",sha256_hex(format!("{}\0batch:{offset}",intent.operation_id).as_bytes()));
    payload.insert("idempotencyKey".into(),json!(key));
    payload.insert("input".into(),json!({"texts":selected.iter().map(|r|r.text.as_str()).collect::<Vec<_>>(),"purpose":"index"}));
    Ok(Value::Object(payload))
}
fn batch_request_hash(intent:&super::document_vector_intent::BuildIntent,offset:usize,selected:&[BatchSourceRow])->Result<String,RepositoryError>{
    let payload=batch_payload(intent,offset,selected)?;
    let material=json!({"operation":payload["operation"],"routes":payload["routes"],"input":payload["input"],"artifactInputs":payload["artifactInputs"],"options":payload["options"]});
    let raw=super::document_upsert::sorted_json(&material,false)?;
    Ok(sha256_hex(raw.as_bytes()))
}
fn vector_hash(rows:&[super::document_vector::VectorInputRow])->Result<String,RepositoryError>{
    use sha2::{Digest,Sha256};
    let mut digest=Sha256::new();digest.update(b"openclank.document-vector-batch-values.v2\0");
    for row in rows {
        digest.update((row.chunk_id.len() as u64).to_be_bytes());digest.update(row.chunk_id.as_bytes());
        digest.update((row.content_hash.len() as u64).to_be_bytes());digest.update(row.content_hash.as_bytes());
        digest.update(row.source_revision.to_be_bytes());
        digest.update((row.values.len() as u64).to_be_bytes());
        for value in &row.values {digest.update(value.to_bits().to_le_bytes())}
    }
    Ok(digest.finalize().iter().map(|v|format!("{v:02x}")).collect())
}

// V2 internal owner API sketch. The eventual provider dispatch must execute
// these in an Immediate transaction under the connection-bound batch guard.
fn claim_batch(tx:&Transaction<'_>,intent:&super::document_vector_intent::BuildIntent,job:&str,original_hash:&str,
    original:&super::document_vector::DocumentVectorBuildOriginal,stage:&str,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    let width=(57000/(original.dimension*24+512)).clamp(1,16);
    let current=tail(tx,&original.owner,&original.account_id,&original.operation_id,job,original_hash,stage,original.row_count,width,view,budget)?;
    if current.active.is_some()||current.offset>=original.row_count{return Err(RepositoryError::NotFound)}
    let expected=(original.row_count-current.offset).min(width);
    let rows=selected_after(tx,&original.owner,&original.account_id,current.after.as_deref(),expected,view,budget)?;
    if rows.len()!=expected{return Err(RepositoryError::NotFound)}
    let ordinal=current.ordinal.checked_add(1).ok_or(RepositoryError::NotFound)?;
    let attempt=format!("attempt_dv_{}",sha256_hex(format!("openclank.document-vector-attempt.v2\0{}\0{}\0{}",original.account_id,original.operation_id,current.offset).as_bytes()));
    let request_sha256=batch_request_hash(intent,current.offset,&rows)?;
    let fact=BatchFact{version:2,phase:"claim".into(),job_id:job.into(),operation_id:original.operation_id.clone(),account_id:original.account_id.clone(),
        original_sha256:original_hash.into(),stage_id:stage.into(),attempt_id:attempt.clone(),ordinal,offset:current.offset,
        items:rows.iter().map(|r|Item{chunk_id:r.chunk_id.clone(),content_hash:r.content_hash.clone(),source_id:r.source_id.clone(),source_revision:r.source_revision}).collect(),
        request_sha256:request_sha256.clone(),previous_terminal_sha256:current.terminal_hash,claim_sha256:None,vector_sha256:None,row_vector_sha256:None};
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_attempts(owner_id,attempt_id,job_id,attempt_number,state,lease_epoch,created_at,updated_at) VALUES(?1,?2,?3,?4,'running',1,?5,?5)",params![original.owner,attempt,job,ordinal,now])?;
    let claim_hash=write_fact(tx,&original.owner,job,2*ordinal-1,"active",&attempt,&fact)?;
    if tx.execute("UPDATE fm_v2_jobs SET attempt_count=?3,current_attempt_id=?4,updated_at=?5 WHERE owner_id=?1 AND job_id=?2 AND state='active' AND current_attempt_id IS NULL AND attempt_count=?6",params![original.owner,job,ordinal,attempt,now,ordinal-1])?!=1{return Err(RepositoryError::NotFound)}
    Ok(json!({"version":2,"state":"claimed","stage_id":stage,"offset":current.offset,"claim_sha256":claim_hash,"request_sha256":request_sha256,"items":rows}))
}
fn active_rows(tx:&Transaction<'_>,intent:&super::document_vector_intent::BuildIntent,original:&super::document_vector::DocumentVectorBuildOriginal,
    claim:&BatchFact,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Vec<BatchSourceRow>,RepositoryError>{
    let after=if claim.ordinal==1 {None} else {
        let (previous,previous_hash,_)=read_fact(tx,&original.owner,&claim.job_id,2*(claim.ordinal-1),&intent.captured_owner,view,budget)?;
        if previous.phase!="terminal"||previous.account_id!=claim.account_id||previous.operation_id!=claim.operation_id
            || previous.original_sha256!=claim.original_sha256||previous.stage_id!=claim.stage_id||previous.ordinal!=claim.ordinal-1
            || claim.previous_terminal_sha256.as_deref()!=Some(previous_hash.as_str()) {return Err(RepositoryError::NotFound)}
        Some(previous.items.last().ok_or(RepositoryError::NotFound)?.chunk_id.clone())
    };
    let rows=selected_after(tx,&original.owner,&original.account_id,after.as_deref(),claim.items.len(),view,budget)?;
    if rows.len()!=claim.items.len()||rows.iter().zip(&claim.items).any(|(r,i)|r.chunk_id!=i.chunk_id||r.content_hash!=i.content_hash||r.source_id!=i.source_id||r.source_revision!=i.source_revision)
        ||batch_request_hash(intent,claim.offset,&rows)?!=claim.request_sha256 {return Err(RepositoryError::NotFound)}
    Ok(rows)
}
fn complete_batch(tx:&Transaction<'_>,intent:&super::document_vector_intent::BuildIntent,job:&str,original_hash:&str,
    original:&super::document_vector::DocumentVectorBuildOriginal,stage:&str,rows:&[super::document_vector::VectorInputRow],
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    let width=(57000/(original.dimension*24+512)).clamp(1,16);
    let current=tail(tx,&original.owner,&original.account_id,&original.operation_id,job,original_hash,stage,original.row_count,width,view,budget)?;
    let (claim,claim_hash)=current.active.ok_or(RepositoryError::NotFound)?;
    let selected=active_rows(tx,intent,original,&claim,view,budget)?;
    append_rows(tx,original,stage,&selected,rows,budget)?;
    let mut terminal=claim.clone();terminal.phase="terminal".into();terminal.claim_sha256=Some(claim_hash);
    terminal.vector_sha256=Some(vector_hash(rows)?);
    terminal.row_vector_sha256=Some(rows.iter().map(|row|vector_hash(std::slice::from_ref(row))).collect::<Result<Vec<_>,_>>()?);
    let now=chrono::Utc::now().to_rfc3339();
    let terminal_hash=write_fact(tx,&original.owner,job,2*claim.ordinal,"succeeded",&claim.attempt_id,&terminal)?;
    if tx.execute("UPDATE fm_v2_attempts SET state='succeeded',updated_at=?3 WHERE owner_id=?1 AND attempt_id=?2 AND state='running'",params![original.owner,claim.attempt_id,now])?!=1{return Err(RepositoryError::NotFound)}
    if tx.execute("UPDATE fm_v2_jobs SET current_attempt_id=NULL,updated_at=?4 WHERE owner_id=?1 AND job_id=?2 AND state='active' AND current_attempt_id=?3",params![original.owner,job,claim.attempt_id,now])?!=1{return Err(RepositoryError::NotFound)}
    Ok(json!({"version":2,"state":"complete","stage_id":stage,"offset":claim.offset,"count":rows.len(),"terminal_sha256":terminal_hash}))
}
fn batch_status(tx:&Transaction<'_>,intent:&super::document_vector_intent::BuildIntent,job:&str,original_hash:&str,
    original:&super::document_vector::DocumentVectorBuildOriginal,stage:&str,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    let width=(57000/(original.dimension*24+512)).clamp(1,16);
    let current=tail(tx,&original.owner,&original.account_id,&original.operation_id,job,original_hash,stage,original.row_count,width,view,budget)?;
    match current.active {
        Some((claim,claim_hash))=>{
            let rows=active_rows(tx,intent,original,&claim,view,budget)?;
            Ok(json!({"version":2,"state":"unknown","stage_id":stage,"offset":claim.offset,"claim_sha256":claim_hash,"request_sha256":claim.request_sha256,"items":rows}))
        },
        None if current.offset==original.row_count=>Ok(json!({"version":2,"state":"ready_to_finalize","stage_id":stage,"offset":current.offset})),
        None=>Ok(json!({"version":2,"state":"ready_to_claim","stage_id":stage,"offset":current.offset})),
    }
}
/// Full bounded lifecycle audit. It is used by reset/rename/purge inventory,
/// never by the per-batch fast path. The caller has verified immutable intent
/// and its exact staged descriptor, including captured-owner custody.
fn lifecycle_digest(tx:&Transaction<'_>,sql_owner:&str,intent:&super::document_vector_intent::BuildIntent,job:&str,
    original_hash:&str,stage:&str,row_count:usize,dimension:usize,width:usize,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<String,RepositoryError>{
    use sha2::{Digest,Sha256};
    let (state,n,current):(String,i64,Option<String>)=tx.query_row("SELECT state,attempt_count,current_attempt_id FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2 AND kind='document_vector_build.v1'",params![sql_owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
    if n<0 || !(1..=16).contains(&width) || (n as usize)>row_count.div_ceil(width) || !matches!(state.as_str(),"queued"|"active"|"succeeded"|"cancelled") || (n==0 && current.is_some()) || (state=="queued" && n!=0) {return Err(RepositoryError::NotFound)}
    let extra:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2 AND attempt_number>?3) OR EXISTS(SELECT 1 FROM fm_v2_job_events WHERE owner_id=?1 AND job_id=?2 AND sequence>?4)",params![sql_owner,job,n,2*n],|r|r.get(0))?;
    if extra{return Err(RepositoryError::NotFound)}
    let mut digest=Sha256::new();digest.update(b"openclank.document-vector-lifecycle.v2\0");
    let mut previous:Option<String>=None;let mut last_chunk:Option<String>=None;let mut completed_rows=0usize;
    for ordinal in 1..=n {
        budget.check(0).map_err(RepositoryError::Validation)?;
        let offset=(ordinal as usize-1).checked_mul(width).ok_or(RepositoryError::NotFound)?;
        let amount=(row_count-offset).min(width);
        let (claim,claim_hash,_)=read_fact(tx,sql_owner,job,2*ordinal-1,&intent.captured_owner,view,budget)?;
        let expected_attempt=format!("attempt_dv_{}",sha256_hex(format!("openclank.document-vector-attempt.v2\0{}\0{}\0{offset}",intent.account_id,intent.operation_id).as_bytes()));
        if claim.phase!="claim"||claim.account_id!=intent.account_id||claim.operation_id!=intent.operation_id||claim.original_sha256!=original_hash
            ||claim.stage_id!=stage||claim.attempt_id!=expected_attempt||claim.ordinal!=ordinal||claim.offset!=offset||claim.items.len()!=amount
            ||claim.previous_terminal_sha256!=previous||claim.claim_sha256.is_some()||claim.vector_sha256.is_some()||claim.row_vector_sha256.is_some()||!valid_sha256(&claim.request_sha256)
            ||claim.items.windows(2).any(|w|w[0].chunk_id>=w[1].chunk_id)
            ||claim.items.first().is_some_and(|i|last_chunk.as_ref().is_some_and(|last|i.chunk_id<=*last))
            ||claim.items.iter().any(|i|i.chunk_id.is_empty()||i.chunk_id.len()>512||i.source_id.len()!=36||!i.source_id.starts_with("src_")||!i.source_id[4..].bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c))||!valid_sha256(&i.content_hash)||i.source_revision<=0) {return Err(RepositoryError::NotFound)}
        let (attempt,lease,attempt_state):(String,i64,String)=tx.query_row("SELECT attempt_id,lease_epoch,state FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2 AND attempt_number=?3",params![sql_owner,job,ordinal],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
        if attempt!=expected_attempt||lease!=1{return Err(RepositoryError::NotFound)}
        digest.update(claim_hash.as_bytes());
        let terminal_exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_job_events WHERE owner_id=?1 AND job_id=?2 AND sequence=?3)",params![sql_owner,job,2*ordinal],|r|r.get(0))?;
        if !terminal_exists {
            if ordinal!=n||state!="active"||current.as_deref()!=Some(attempt.as_str())||attempt_state!="running" {return Err(RepositoryError::NotFound)}
            digest.update(b"active");continue
        }
        if ordinal==n && current.is_some(){return Err(RepositoryError::NotFound)}
        let (terminal,hash,_)=read_fact(tx,sql_owner,job,2*ordinal,&intent.captured_owner,view,budget)?;
        if terminal.account_id!=claim.account_id||terminal.operation_id!=claim.operation_id||terminal.original_sha256!=claim.original_sha256||terminal.stage_id!=claim.stage_id
            ||terminal.attempt_id!=claim.attempt_id||terminal.ordinal!=claim.ordinal||terminal.offset!=claim.offset
            ||serde_json::to_value(&terminal.items).ok()!=serde_json::to_value(&claim.items).ok()||terminal.request_sha256!=claim.request_sha256
            ||terminal.previous_terminal_sha256!=claim.previous_terminal_sha256||terminal.claim_sha256.as_deref()!=Some(claim_hash.as_str()) {return Err(RepositoryError::NotFound)}
        match terminal.phase.as_str(){
            "terminal" if attempt_state=="succeeded" && terminal.vector_sha256.as_ref().is_some_and(|x|valid_sha256(x))=>{
                let retained=verify_terminal_rows(tx,sql_owner,&intent.account_id,stage,dimension,&terminal,view,budget)?;
                completed_rows=completed_rows.checked_add(retained).ok_or(RepositoryError::NotFound)?;
            },
            "cancelled" if ordinal==n && state=="cancelled" && attempt_state=="fenced" && terminal.vector_sha256.is_none() && terminal.row_vector_sha256.is_none()=>{},
            _=>return Err(RepositoryError::NotFound),
        }
        previous=Some(hash.clone());last_chunk=terminal.items.last().map(|i|i.chunk_id.clone());digest.update(hash.as_bytes());
    }
    let stored:i64=tx.query_row("SELECT count(*) FROM fm_v2_chunk_embeddings WHERE owner_id=?1 AND generation_id=?2",params![sql_owner,stage],|r|r.get(0))?;
    if stored!=completed_rows as i64{return Err(RepositoryError::NotFound)}
    if current.is_some() && state!="active" {return Err(RepositoryError::NotFound)}
    if state=="succeeded" && (n as usize)!=row_count.div_ceil(width) {return Err(RepositoryError::NotFound)}
    Ok(digest.finalize().iter().map(|v|format!("{v:02x}")).collect())
}


/// Run once per owner inventory under the existing progress budget, not once
/// for every job. Unknown or disconnected batch receipts make custody unavailable.
pub(crate) fn require_no_orphan_receipts(tx:&Transaction<'_>,owner:&str)->Result<(),RepositoryError>{
    let bad:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets c WHERE c.owner_id=?1 AND c.reason='document_vector.batch.v2' AND (NOT json_valid(c.operation_metadata_json) OR NOT EXISTS(SELECT 1 FROM fm_v2_job_events e JOIN fm_v2_jobs j ON j.owner_id=e.owner_id AND j.job_id=e.job_id WHERE e.owner_id=c.owner_id AND e.change_set_id=c.change_set_id AND e.result_json=c.operation_metadata_json AND j.kind='document_vector_build.v1'))) OR EXISTS(SELECT 1 FROM fm_v2_outbox o LEFT JOIN fm_v2_change_sets c ON c.owner_id=o.owner_id AND c.change_set_id=o.change_set_id WHERE o.owner_id=?1 AND o.kind='document_vector_batch_v2' AND (c.change_set_id IS NULL OR c.reason<>'document_vector.batch.v2' OR o.event_id<>'outbox_'||o.change_set_id||'_document_vector_batch_v2' OR o.payload_json<>c.operation_metadata_json))",[owner],|r|r.get(0))?;
    if bad{return Err(RepositoryError::NotFound)}Ok(())
}

/// Resolve only the exact active v2 original and same retained stage. The
/// source epoch and publication pointer are invalidation fences, not grants.
fn context(tx:&Transaction<'_>,r:&super::document_vector::DocumentVectorBuildOriginal,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)
    ->Result<(super::document_vector_intent::BuildIntent,String,String,String),RepositoryError>{
    require_guarded_epoch(tx)?;
    let id=super::document_vector_intent::BuildIdentity{version:1,account_id:r.account_id.clone(),owner:r.owner.clone(),operation_id:r.operation_id.clone()};
    let (intent,state,job)=super::document_vector_intent::load(tx,&id,view,budget)?;
    if intent.version!=2 || state!="active" || intent.captured_owner!=r.owner || intent.head.source_watermark!=r.source_watermark
        || intent.head.row_count!=r.row_count || json!(intent.head.expected_pointer)!=serde_json::to_value(&r.expected_pointer).map_err(|_|RepositoryError::NotFound)? {return Err(RepositoryError::NotFound)}
    let epoch=intent.source_epoch.ok_or(RepositoryError::NotFound)?;
    super::document_vector_epoch::require(tx,&r.owner,epoch)?;
    let (stage,hash)=super::document_vector::staged_identity(r)?;
    if !super::document_vector::stage_current(tx,r,&stage,&hash)? {return Err(RepositoryError::NotFound)}
    require_pointer(tx,r)?;
    let original_hash=sha256_hex(super::document_vector_intent::validate(&intent)?.as_bytes());
    Ok((intent,job,original_hash,stage))
}
pub(crate) fn begin(tx:&Transaction<'_>,r:&super::document_vector::DocumentVectorBuildOriginal,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    let id=super::document_vector_intent::BuildIdentity{version:1,account_id:r.account_id.clone(),owner:r.owner.clone(),operation_id:r.operation_id.clone()};
    let (intent,state,_)=super::document_vector_intent::load(tx,&id,view,budget)?;
    require_guarded_epoch(tx)?;
    if intent.version!=2||state!="active"||intent.captured_owner!=r.owner||intent.head.source_watermark!=r.source_watermark
        ||intent.head.row_count!=r.row_count||json!(intent.head.expected_pointer)!=serde_json::to_value(&r.expected_pointer).map_err(|_|RepositoryError::NotFound)? {return Err(RepositoryError::NotFound)}
    super::document_vector_epoch::require(tx,&r.owner,intent.source_epoch.ok_or(RepositoryError::NotFound)?)?;
    require_pointer(tx,r)?;
    let (stage,hash)=super::document_vector::staged_identity(r)?;
    if super::document_vector::stage_current(tx,r,&stage,&hash)? {
        return Ok(json!({"version":1,"state":"building","stage_id":stage}))
    }
    crate::writer_fence::with_document_vector_batch_authority(tx,||{
        let now=chrono::Utc::now().to_rfc3339();
        tx.execute("INSERT INTO fm_v2_derived_generations(owner_id,generation_id,logical_space,workspace_key,project_key,provider_ref,model,endpoint_class,dimension,normalization,metric,chunker_version,config_fingerprint,source_watermark,row_count,state,retention_state,created_at,updated_at) VALUES(?1,?2,'documents_vector','','',?3,?4,?5,?6,'l2','cosine','frankenmemory-rag-v2',?7,?8,?9,'planned','retained',?10,?10)",params![r.owner,stage,r.provider_ref,r.model,r.endpoint_class,r.dimension as i64,hash,r.source_watermark,r.row_count as i64,now])?;
        if tx.execute("UPDATE fm_v2_derived_generations SET state='building' WHERE owner_id=?1 AND generation_id=?2 AND state='planned'",params![r.owner,stage])?!=1{return Err(RepositoryError::NotFound)}
        Ok(json!({"version":1,"state":"building","stage_id":stage}))
    })
}
pub(crate) fn claim(tx:&Transaction<'_>,r:&super::document_vector::DocumentVectorBuildOriginal,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    let (intent,job,hash,stage)=context(tx,r,view,budget)?;
    crate::writer_fence::with_document_vector_batch_authority(tx,||claim_batch(tx,&intent,&job,&hash,r,&stage,view,budget))
}
pub(crate) fn status(tx:&Transaction<'_>,r:&super::document_vector::DocumentVectorBuildOriginal,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    let (intent,job,hash,stage)=context(tx,r,view,budget)?;
    batch_status(tx,&intent,&job,&hash,r,&stage,view,budget)
}
pub(crate) fn complete(tx:&Transaction<'_>,r:&super::document_vector::DocumentVectorAppendRequest,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    let (intent,job,hash,stage)=context(tx,&r.original,view,budget)?;
    let width=(57000/(r.original.dimension*24+512)).clamp(1,16);
    let current=tail(tx,&r.original.owner,&r.original.account_id,&r.original.operation_id,&job,&hash,&stage,r.original.row_count,width,view,budget)?;
    if current.active.as_ref().is_none_or(|(claim,_)|claim.offset!=r.offset){return Err(RepositoryError::NotFound)}
    crate::writer_fence::with_document_vector_batch_authority(tx,||complete_batch(tx,&intent,&job,&hash,&r.original,&stage,&r.rows,view,budget))
}
pub(crate) fn finalize(tx:&Transaction<'_>,r:&super::document_vector::DocumentVectorBuildOriginal,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    let (_intent,job,hash,stage)=context(tx,r,view,budget)?;
    let width=(57000/(r.dimension*24+512)).clamp(1,16);
    let current=tail(tx,&r.owner,&r.account_id,&r.operation_id,&job,&hash,&stage,r.row_count,width,view,budget)?;
    if current.active.is_some()||current.offset!=r.row_count{return Err(RepositoryError::NotFound)}
    crate::writer_fence::with_document_vector_batch_authority(tx,||{
        let result=super::document_vector::stage_finalize(tx,r,view,budget)?;
        super::document_vector_intent::finish(tx,r,&result,view,budget)?;Ok(result)
    })
}

/// Resolve the immutable stage descriptor even after a verified owner rename.
/// SQL owner may change; the captured-owner bytes and stage identity do not.
pub(crate) fn audit_intent(tx:&Transaction<'_>,sql_owner:&str,intent:&super::document_vector_intent::BuildIntent,job:&str,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<String,RepositoryError>{
    if intent.version!=2||intent.source_epoch.is_none()||view.current_owner()!=sql_owner||view.account_id()!=intent.account_id{return Err(RepositoryError::NotFound)}
    view.for_captured(tx,&intent.captured_owner)?;
    let identity=json!(["openclank.document-vector-operation.v1",intent.account_id,intent.captured_owner,intent.operation_id]);
    let stage=format!("stage_vec_{}",sha256_hex(super::document_upsert::sorted_json(&identity,true)?.as_bytes()));
    let row:Option<(String,String,String,i64,String,String,i64,String,String)>=tx.query_row("SELECT provider_ref,model,endpoint_class,dimension,config_fingerprint,source_watermark,row_count,state,retention_state FROM fm_v2_derived_generations WHERE owner_id=?1 AND generation_id=?2 AND logical_space='documents_vector' AND workspace_key='' AND project_key='' AND normalization='l2' AND metric='cosine' AND chunker_version='frankenmemory-rag-v2'",params![sql_owner,stage],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?))).optional()?;
    let cursor:(i64,Option<String>,String)=tx.query_row("SELECT attempt_count,current_attempt_id,state FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![sql_owner,job],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
    let Some((provider_ref,model,endpoint_class,dimension,config,watermark,count,state,retention))=row else {
        let extra:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_job_events WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_chunk_embeddings WHERE owner_id=?1 AND generation_id=?3)",params![sql_owner,job,stage],|r|r.get(0))?;
        if cursor.0!=0||cursor.1.is_some()||extra||!matches!(cursor.2.as_str(),"queued"|"active"|"cancelled"){return Err(RepositoryError::NotFound)}
        return Ok(sha256_hex(b"openclank.document-vector-no-stage.v2"))
    };
    if cursor.2=="queued"{return Err(RepositoryError::NotFound)}
    let expected_pointer:Option<super::document_vector::ExpectedVectorPointer>=serde_json::from_value(json!(intent.head.expected_pointer)).map_err(|_|RepositoryError::NotFound)?;
    let original=super::document_vector::DocumentVectorBuildOriginal{version:1,owner:intent.captured_owner.clone(),account_id:intent.account_id.clone(),operation_id:intent.operation_id.clone(),provider_ref,model,endpoint_class,
        dimension:usize::try_from(dimension).map_err(|_|RepositoryError::NotFound)?,source_watermark:watermark,row_count:usize::try_from(count).map_err(|_|RepositoryError::NotFound)?,expected_pointer};
    let (expected_stage,expected_config)=super::document_vector::staged_identity(&original)?;
    if stage!=expected_stage||config!=expected_config||original.source_watermark!=intent.head.source_watermark||original.row_count!=intent.head.row_count||state!="building"||retention!="retained" {return Err(RepositoryError::NotFound)}
    let hash=sha256_hex(super::document_vector_intent::validate(intent)?.as_bytes());
    let width=(57000/(original.dimension*24+512)).clamp(1,16);
    lifecycle_digest(tx,sql_owner,intent,job,&hash,&stage,original.row_count,original.dimension,width,view,budget)
}
/// Retire an uncompleted v2 original; a claimed paid frame remains unknown.
/// The owner lifecycle drains the actual frame before invoking this transaction.
pub(crate) fn retire(tx:&Transaction<'_>,id:&super::document_vector_intent::BuildIdentity,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    let (intent,state,job)=super::document_vector_intent::load(tx,id,view,budget)?;
    if intent.version!=2||!matches!(state.as_str(),"queued"|"active"){return Err(RepositoryError::NotFound)}
    audit_intent(tx,&id.owner,&intent,&job,view,budget)?;
    let active:Option<String>=tx.query_row("SELECT current_attempt_id FROM fm_v2_jobs WHERE owner_id=?1 AND job_id=?2",params![id.owner,job],|r|r.get(0))?;
    crate::writer_fence::with_document_vector_batch_authority(tx,||{
        if let Some(attempt)=active {
            let ordinal:i64=tx.query_row("SELECT attempt_number FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2 AND attempt_id=?3 AND state='running'",params![id.owner,job,attempt],|r|r.get(0))?;
            let (mut claim,hash,_)=read_fact(tx,&id.owner,&job,2*ordinal-1,&intent.captured_owner,view,budget)?;
            claim.phase="cancelled".into();claim.claim_sha256=Some(hash);claim.vector_sha256=None;claim.row_vector_sha256=None;
            write_fact(tx,&id.owner,&job,2*ordinal,"cancelled",&attempt,&claim)?;
            let now=chrono::Utc::now().to_rfc3339();
            if tx.execute("UPDATE fm_v2_attempts SET state='fenced',updated_at=?3 WHERE owner_id=?1 AND attempt_id=?2 AND state='running'",params![id.owner,attempt,now])?!=1{return Err(RepositoryError::NotFound)}
            if tx.execute("UPDATE fm_v2_jobs SET current_attempt_id=NULL,updated_at=?4 WHERE owner_id=?1 AND job_id=?2 AND current_attempt_id=?3 AND state='active'",params![id.owner,job,attempt,now])?!=1{return Err(RepositoryError::NotFound)}
        }
        super::document_vector_intent::cancel(tx,id,view,budget)
    })
}

/// A missing vector is explained only by the exact retained S01 removal
/// owner, not by row absence. Reachable document removal retains its forgotten
/// head and reimport increments that head; account purge removes both this
/// family and the removal receipts together.
fn removed_source_proved(tx:&Transaction<'_>,owner:&str,account:&str,item:&Item,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<bool,RepositoryError>{
    let head:Option<(i64,String,String,String,String)>=tx.query_row("SELECT source_revision,source_type,workspace_key,project_key,forget_state FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2 ORDER BY source_revision DESC LIMIT 1",params![owner,item.source_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
    let (head_revision,kind,workspace,project,head_state)=head.ok_or(RepositoryError::NotFound)?;
    if kind!="owner_imported_file"||!workspace.is_empty()||!project.is_empty()||head_revision<item.source_revision||!matches!(head_state.as_str(),"active"|"forgotten"){return Err(RepositoryError::NotFound)}
    let still_present:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_chunks WHERE owner_id=?1 AND chunk_id=?2)",params![owner,item.chunk_id],|r|r.get(0))?;
    if still_present{return Err(RepositoryError::NotFound)}
    let mut stmt=tx.prepare("SELECT CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=8192 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND reason='s01.document-remove.v1' AND json_valid(operation_metadata_json) AND json_extract(operation_metadata_json,'$.source_id')=?2 AND json_extract(operation_metadata_json,'$.source_revision')>=?3 ORDER BY change_set_id")?;
    let mut rows=stmt.query(params![owner,item.source_id,item.source_revision])?;let mut proved=false;
    while let Some(row)=rows.next()? {
        let raw:Option<String>=row.get(0)?;let raw=raw.ok_or(RepositoryError::NotFound)?;budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        let get=|key:&str|value.get(key).and_then(Value::as_str).map(str::to_owned).ok_or(RepositoryError::NotFound);
        let revision=value.get("source_revision").and_then(Value::as_i64).ok_or(RepositoryError::NotFound)?;
        if value.get("account_id").and_then(Value::as_str)!=Some(account)||revision<item.source_revision||revision>head_revision||(head_state=="active" && revision>=head_revision){return Err(RepositoryError::NotFound)}
        let request=super::document_upsert::DocumentOriginalRequest{version:1,operation_id:get("operation_id")?,request_sha256:get("request_sha256")?,account_id:account.into(),current_owner:owner.into(),captured_owner:get("captured_owner")?};
        let fact=super::document_remove::original(tx,&request,view,budget)?;
        if fact.get("state")!=Some(&json!("removed"))||fact.get("source_id")!=Some(&json!(item.source_id))||fact.get("source_revision")!=Some(&json!(revision)){return Err(RepositoryError::NotFound)}
        proved=true;
    }
    Ok(proved)
}
fn verify_terminal_rows(tx:&Transaction<'_>,owner:&str,account:&str,stage:&str,dimension:usize,terminal:&BatchFact,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<usize,RepositoryError>{
    if dimension==0||dimension>65536||terminal.items.is_empty()||terminal.items.len()>16{return Err(RepositoryError::NotFound)}
    let hashes=terminal.row_vector_sha256.as_ref().filter(|v|v.len()==terminal.items.len()).ok_or(RepositoryError::NotFound)?;
    let mut rows=Vec::new();let mut retained=0usize;
    for (item,expected) in terminal.items.iter().zip(hashes){
        if !valid_sha256(expected){return Err(RepositoryError::NotFound)}
        let stored:Option<(i64,Option<Vec<u8>>,String)>=tx.query_row("SELECT dimension,CASE WHEN length(embedding)<=262144 THEN embedding END,content_hash FROM fm_v2_chunk_embeddings WHERE owner_id=?1 AND generation_id=?2 AND chunk_id=?3",params![owner,stage,item.chunk_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
        let Some((dim,blob,content))=stored else {if !removed_source_proved(tx,owner,account,item,view,budget)?{return Err(RepositoryError::NotFound)}continue};
        let blob=blob.ok_or(RepositoryError::NotFound)?;budget.check(blob.len()+content.len()).map_err(RepositoryError::Validation)?;
        if dim!=dimension as i64||blob.len()!=dimension*4||content!=item.content_hash{return Err(RepositoryError::NotFound)}
        let canonical:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_chunks c JOIN fm_v2_documents d ON d.owner_id=c.owner_id AND d.document_id=c.document_id WHERE c.owner_id=?1 AND c.chunk_id=?2 AND c.content_hash=?3 AND d.source_id=?4 AND d.source_revision=?5)",params![owner,item.chunk_id,item.content_hash,item.source_id,item.source_revision],|r|r.get(0))?;
        if !canonical{return Err(RepositoryError::NotFound)}
        let values:Vec<f32>=blob.chunks_exact(4).map(|b|f32::from_le_bytes([b[0],b[1],b[2],b[3]])).collect();
        let norm:f64=values.iter().map(|v|(*v as f64)*(*v as f64)).sum();
        if values.iter().any(|v|!v.is_finite())||!norm.is_finite()||(norm.sqrt()-1.).abs()>1e-5{return Err(RepositoryError::NotFound)}
        let row=super::document_vector::VectorInputRow{chunk_id:item.chunk_id.clone(),content_hash:item.content_hash.clone(),source_revision:item.source_revision,values};
        if vector_hash(std::slice::from_ref(&row))?!=*expected{return Err(RepositoryError::NotFound)}
        retained+=1;rows.push(row);
    }
    if retained==terminal.items.len()&&terminal.vector_sha256.as_deref()!=Some(vector_hash(&rows)?.as_str()){return Err(RepositoryError::NotFound)}
    Ok(retained)
}
