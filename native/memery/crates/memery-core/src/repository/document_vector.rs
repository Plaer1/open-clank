//! Guarded document-vector generation: scoped canonical inventory foundation.
//!
//! This module is not dispatched or advertised. Publication and embedding
//! production remain unavailable until the transaction owner is joined.
use super::*;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct VectorCanonicalRow {
    pub source_id:String, pub source_uri:String, pub source_revision:i64,
    pub document_id:String, pub chunk_id:String, pub content_hash:String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct VectorCanonicalSnapshot {
    pub source_watermark: String,
    pub rows: Vec<VectorCanonicalRow>,
}

/// Match RAG's current internal-document watermark under one read transaction.
/// The empty owner scope cannot have a registered external adapter; every
/// persisted external or unexplained source is refused rather than adopted.
pub(crate) fn canonical_snapshot(
    tx: &Transaction<'_>, owner: &str, account: &str,
    view: &owner_custody::VerifiedCustodyView<'_, '_>, budget: &mut ResetFenceBudget,
) -> Result<VectorCanonicalSnapshot, RepositoryError> {
    if owner.is_empty() || account.is_empty() || view.current_owner()!=owner || view.account_id()!=account {
        return Err(RepositoryError::NotFound);
    }
    let bad: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND workspace_key='' AND project_key='' AND source_type<>'owner_imported_file')", [owner], |r|r.get(0))?;
    if bad { return Err(RepositoryError::NotFound); }
    let mut statement=tx.prepare("SELECT s.source_id,s.source_revision,s.source_uri,s.content_hash,s.forget_state,COALESCE(d.document_id,''),COALESCE(c.chunk_id,''),COALESCE(c.content_hash,''),COALESCE(d.content_hash,''),c.ordinal,CASE WHEN length(CAST(c.text AS BLOB))<=65536 THEN c.text END,CASE WHEN length(CAST(c.locator_json AS BLOB))<=65536 THEN c.locator_json END FROM fm_v2_sources s LEFT JOIN fm_v2_documents d ON d.owner_id=s.owner_id AND d.source_id=s.source_id AND d.source_revision=s.source_revision AND d.parser_version='frankenmemory-rag-v2' LEFT JOIN fm_v2_chunks c ON c.owner_id=d.owner_id AND c.document_id=d.document_id WHERE s.owner_id=?1 AND s.workspace_key='' AND s.project_key='' AND NOT EXISTS(SELECT 1 FROM fm_v2_sources newer WHERE newer.owner_id=s.owner_id AND newer.source_id=s.source_id AND newer.source_revision>s.source_revision) ORDER BY s.source_id,s.source_revision,d.document_id,c.ordinal,c.chunk_id")?;
    let mut cursor=statement.query([owner])?;
    let mut manifest=Vec::new(); let mut eligible=Vec::new(); let mut ids=std::collections::BTreeSet::new();
    let mut required=std::collections::BTreeMap::<(String,i64),String>::new();
    while let Some(row)=cursor.next()? {
        let source:String=row.get(0)?; let revision:i64=row.get(1)?; let uri:String=row.get(2)?;
        let source_hash:String=row.get(3)?; let state:String=row.get(4)?; let document:String=row.get(5)?;
        let chunk:String=row.get(6)?; let hash:String=row.get(7)?; let document_hash:String=row.get(8)?;
        let ordinal:Option<i64>=row.get(9)?;let text:Option<String>=row.get(10)?;let locator:Option<String>=row.get(11)?;
        budget.check(source.len()+uri.len()+source_hash.len()+state.len()+document.len()+chunk.len()+hash.len()+document_hash.len()+text.as_ref().map_or(0,String::len)+locator.as_ref().map_or(0,String::len)+8).map_err(RepositoryError::Validation)?;
        let expected=format!("src_{}",&sha256_hex(format!("{owner}\0\0\0{uri}").as_bytes())[..32]);
        if source!=expected || uri.is_empty() || uri.len()>1024 || uri.trim()!=uri || uri.contains('\0') || revision<=0 || !valid_sha256(&source_hash)
            || !matches!(state.as_str(),"active"|"forgotten") || (state=="forgotten" && !document.is_empty()) {
            return Err(RepositoryError::NotFound);
        }
        if state=="active" && document.is_empty() {return Err(RepositoryError::NotFound);}
        if !document.is_empty() {
            if state=="active" {required.insert((source.clone(),revision),document.clone());}
            let expected_doc=format!("doc_{}",&sha256_hex(format!("{owner}\0{source}\0{revision}\0{source_hash}\0frankenmemory-rag-v2").as_bytes())[..32]);
            if document!=expected_doc || document_hash!=source_hash {return Err(RepositoryError::NotFound);}
        }
        if !chunk.is_empty() && state=="active" {
            let ordinal=ordinal.filter(|n|*n>=0).ok_or(RepositoryError::NotFound)?;
            let text=text.as_ref().ok_or(RepositoryError::NotFound)?;let locator=locator.as_ref().ok_or(RepositoryError::NotFound)?;
            let expected_chunk=format!("chunk_{}",&sha256_hex(format!("{owner}\0{document}\0frankenmemory-rag-v2\0{ordinal}\0{locator}\0{hash}").as_bytes())[..32]);
            if document.is_empty() || chunk!=expected_chunk || !valid_sha256(&hash) || sha256_hex(text.as_bytes())!=hash || !ids.insert(chunk.clone()) { return Err(RepositoryError::NotFound); }
            eligible.push(VectorCanonicalRow{source_id:source.clone(),source_uri:uri.clone(),source_revision:revision,document_id:document.clone(),chunk_id:chunk.clone(),content_hash:hash.clone()});
        }
        manifest.push(json!([source,revision,uri,source_hash,state,document,chunk,hash]));
    }
    drop(cursor); drop(statement);
    // Source rows alone carry no account approval. Discover and verify the
    // existing paired guarded-import receipt for every active source head.
    let mut statement=tx.prepare("SELECT CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=8192 THEN operation_metadata_json END FROM fm_v2_change_sets WHERE owner_id=?1 AND reason='s01.document-upsert.v1' ORDER BY change_set_id")?;
    let mut cursor=statement.query([owner])?;let mut originals=Vec::new();
    while let Some(row)=cursor.next()? {
        let raw:Option<String>=row.get(0)?;let raw=raw.ok_or(RepositoryError::NotFound)?;
        budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        let source=value.get("source_id").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?;
        let revision=value.get("source_revision").and_then(Value::as_i64).ok_or(RepositoryError::NotFound)?;
        let Some(document)=required.get(&(source.to_owned(),revision)) else {continue};
        if value.get("account_id").and_then(Value::as_str)!=Some(account) || value.get("document_id").and_then(Value::as_str)!=Some(document.as_str()) {return Err(RepositoryError::NotFound);}
        let get=|name:&str|value.get(name).and_then(Value::as_str).map(str::to_owned).ok_or(RepositoryError::NotFound);
        originals.push((source.to_owned(),revision,super::document_upsert::DocumentOriginalRequest{version:1,operation_id:get("operation_id")?,request_sha256:get("request_sha256")?,account_id:account.into(),current_owner:owner.into(),captured_owner:get("captured_owner")?}));
    }
    drop(cursor);drop(statement);
    let mut proved=std::collections::BTreeSet::new();
    for (source,revision,request) in originals {
        let fact=super::document_upsert::original(tx,&request,view,budget)?;
        if fact.get("state").and_then(Value::as_str)!=Some("committed") || fact.get("source_id").and_then(Value::as_str)!=Some(source.as_str()) || fact.get("source_revision").and_then(Value::as_i64)!=Some(revision) {return Err(RepositoryError::NotFound);}
        proved.insert((source,revision));
    }
    if required.keys().any(|key|!proved.contains(key)) {return Err(RepositoryError::NotFound);}
    let raw=super::document_upsert::sorted_json(&Value::Array(manifest),true)?;
    Ok(VectorCanonicalSnapshot{source_watermark:sha256_hex(raw.as_bytes()),rows:eligible})
}


#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentVectorSnapshotRequest {
    pub version:u8, pub owner:String, pub account_id:String,
    pub after:Option<String>, pub expected_watermark:Option<String>, pub limit:u8,
}

/// Metadata only, in a deterministic page. Every page repeats full source and
/// receipt validation in its own transaction; the original watermark fences it.
pub(crate) fn snapshot_page(tx:&Transaction<'_>,r:&DocumentVectorSnapshotRequest,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if r.version!=1 || r.limit==0 || r.limit>64 || r.after.as_ref().is_some_and(|v|v.is_empty()||v.len()>512)
        || (r.after.is_some() && r.expected_watermark.is_none()) || r.expected_watermark.as_ref().is_some_and(|v|!valid_sha256(v)) {
        return Err(RepositoryError::NotFound);
    }
    let mut snapshot=canonical_snapshot(tx,&r.owner,&r.account_id,view,budget)?;
    if r.expected_watermark.as_ref().is_some_and(|v|v!=&snapshot.source_watermark) {return Err(RepositoryError::NotFound);}
    snapshot.rows.sort_by(|a,b|a.chunk_id.cmp(&b.chunk_id));
    let total=snapshot.rows.len();let start=match &r.after {None=>0,Some(after)=>snapshot.rows.binary_search_by(|v|v.chunk_id.cmp(after)).map_err(|_|RepositoryError::NotFound)?+1};
    let end=(start+usize::from(r.limit)).min(total);
    let next=if end<total {Some(snapshot.rows[end-1].chunk_id.clone())} else {None};
    Ok(json!({"version":1,"state":"available","source_watermark":snapshot.source_watermark,"total":total,"items":&snapshot.rows[start..end],"next_cursor":next}))
}

/// Values supplied by the existing configured embedding producer, never by a
/// model-authored request. This preparer grants no publication authority.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct VectorInputRow {
    pub chunk_id:String, pub content_hash:String, pub source_revision:i64, pub values:Vec<f32>,
}

pub(crate) struct PreparedVectorGeneration {
    pub generation_id:String,
    pub config_fingerprint:String,
    pub eligibility_digest:String,
    pub flat_checksum:[u8;32],
    pub rows:Vec<VectorInputRow>,
}

/// Bind the entire exact canonical set and actual float bits to a stable
/// generation identity. This runs before any publication transaction.
pub(crate) fn prepare_generation(
    owner:&str, account:&str, snapshot:&VectorCanonicalSnapshot, provider_ref:&str, model:&str,
    endpoint_class:&str, dimension:usize, mut rows:Vec<VectorInputRow>,
) -> Result<PreparedVectorGeneration, RepositoryError> {
    use sha2::{Digest,Sha256};
    use crate::retrieval::vector_backend::{FlatIndex,Metric,Profile,Row,Vector};
    if [owner,account,provider_ref,model,endpoint_class].iter().any(|s|s.is_empty() || s.trim()!=*s || s.len()>512)
        || dimension==0 || dimension>65536 || rows.len()!=snapshot.rows.len() || rows.len()>1_000_000
        || dimension.checked_mul(rows.len()).is_none_or(|n|n>16_000_000) {
        return Err(RepositoryError::NotFound);
    }
    rows.sort_by(|a,b|a.chunk_id.cmp(&b.chunk_id));
    let mut canonical=snapshot.rows.clone(); canonical.sort_by(|a,b|a.chunk_id.cmp(&b.chunk_id));
    let mut identities=Vec::with_capacity(rows.len());
    let mut digest=Sha256::new(); digest.update(b"openclank.document-vector-generation.v1\0");
    let descriptor=json!({"owner":owner,"account":account,"workspace":"","project":"","provider_ref":provider_ref,"model":model,
        "model_version":"unknown","endpoint_class":endpoint_class,"dimension":dimension,"normalization":"l2",
        "metric":"cosine","chunker_version":"frankenmemory-rag-v2","source_watermark":snapshot.source_watermark});
    let raw=super::document_upsert::sorted_json(&descriptor,true)?;digest.update((raw.len() as u64).to_be_bytes());digest.update(raw.as_bytes());
    for (input,current) in rows.iter().zip(&canonical) {
        if input.chunk_id!=current.chunk_id || input.content_hash!=current.content_hash || input.source_revision!=current.source_revision
            || input.values.len()!=dimension || input.values.iter().any(|x|!x.is_finite()) {return Err(RepositoryError::NotFound)}
        let norm:f64=input.values.iter().map(|x|(*x as f64)*(*x as f64)).sum();
        if !norm.is_finite() || (norm.sqrt()-1.0).abs()>1e-5 {return Err(RepositoryError::NotFound)}
        identities.push(json!([input.chunk_id,input.source_revision,input.content_hash]));
        for value in [&input.chunk_id,&input.content_hash] {digest.update((value.len() as u64).to_be_bytes());digest.update(value.as_bytes());}
        digest.update(input.source_revision.to_be_bytes());
        for value in &input.values {digest.update(value.to_bits().to_le_bytes());}
    }
    let eligibility=super::document_upsert::sorted_json(&Value::Array(identities),true)?;
    let eligibility_digest=sha256_hex(eligibility.as_bytes());
    let generation_id=format!("gen_vec_{}",digest.finalize().iter().map(|b|format!("{b:02x}")).collect::<String>());
    let profile=Profile{owner:owner.into(),workspace:String::new(),project:String::new(),generation_id:generation_id.clone(),
        source_watermark:snapshot.source_watermark.clone(),eligibility_epoch:eligibility_digest.clone(),model:model.into(),
        model_version:"unknown".into(),dimension,metric:Metric::UnitCosine,normalization:"unit_l2".into()};
    let flat_rows=rows.iter().map(|row|Row{id:row.chunk_id.clone(),source_revision:format!("{}:{}",row.source_revision,row.content_hash),vector:Vector::Float(row.values.clone())}).collect();
    let flat=FlatIndex::build(profile,flat_rows).map_err(|_|RepositoryError::NotFound)?;
    let config=json!({"provider_ref":provider_ref,"model":model,"endpoint_class":endpoint_class,"dimension":dimension,
        "normalization":"l2","metric":"cosine","chunker_version":"frankenmemory-rag-v2"});
    let config_fingerprint=sha256_hex(super::document_upsert::sorted_json(&config,true)?.as_bytes());
    Ok(PreparedVectorGeneration{generation_id,config_fingerprint,eligibility_digest,flat_checksum:flat.checksum(),rows})
}


#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExpectedVectorPointer {
    pub generation_id:String, pub publication_id:String, pub publication_watermark:String,
}

/// Called only inside a guarded immediate transaction after the Host's exact
/// producer and source admission have been revalidated. Any error must roll
/// back the caller's transaction; no public dispatch is currently provided.
pub(crate) fn publish_precomputed(
    tx:&Transaction<'_>, owner:&str, account:&str, provider_ref:&str, model:&str,
    endpoint_class:&str, dimension:usize, expected_watermark:&str,
    expected_pointer:Option<&ExpectedVectorPointer>, rows:Vec<VectorInputRow>,
    view:&owner_custody::VerifiedCustodyView<'_, '_>, budget:&mut ResetFenceBudget,
) -> Result<Value,RepositoryError> {
    require_guarded_epoch(tx)?;
    let snapshot=canonical_snapshot(tx,owner,account,view,budget)?;
    if snapshot.source_watermark!=expected_watermark {return Err(RepositoryError::NotFound);}
    let prepared=prepare_generation(owner,account,&snapshot,provider_ref,model,endpoint_class,dimension,rows)?;
    let current:Option<(String,String,String)>=tx.query_row("SELECT generation_id,publication_tx,publication_watermark FROM fm_v2_index_pointers WHERE owner_id=?1 AND logical_space='documents_vector' AND workspace_key='' AND project_key=''",[owner],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
    let matches=match (&current,expected_pointer) {
        (None,None)=>true,
        (Some((generation,publication,watermark)),Some(expected))=>generation==&expected.generation_id && publication==&expected.publication_id && watermark==&expected.publication_watermark,
        _=>false,
    };
    if !matches {return Err(RepositoryError::NotFound);}
    if let Some((generation,publication,watermark))=&current {
        let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_index_publications p JOIN fm_v2_derived_generations g ON g.owner_id=p.owner_id AND g.generation_id=p.generation_id WHERE p.owner_id=?1 AND p.publication_id=?2 AND p.logical_space='documents_vector' AND p.workspace_key='' AND p.project_key='' AND p.generation_id=?3 AND p.publication_watermark=?4 AND p.action IN ('publish','rollback') AND g.state='ready' AND g.retention_state='retained')",params![owner,publication,generation,watermark],|r|r.get(0))?;
        if !valid {return Err(RepositoryError::NotFound);}
    }
    let existing:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_derived_generations WHERE owner_id=?1 AND generation_id=?2)",params![owner,prepared.generation_id],|r|r.get(0))?;
    if existing {return Err(RepositoryError::NotFound);}
    let now=chrono::Utc::now().to_rfc3339();
    tx.execute("INSERT INTO fm_v2_derived_generations(owner_id,generation_id,logical_space,workspace_key,project_key,provider_ref,model,endpoint_class,dimension,normalization,metric,chunker_version,config_fingerprint,source_watermark,row_count,state,retention_state,created_at,updated_at) VALUES(?1,?2,'documents_vector','','',?3,?4,?5,?6,'l2','cosine','frankenmemory-rag-v2',?7,?8,?9,'planned','retained',?10,?10)",params![owner,prepared.generation_id,provider_ref,model,endpoint_class,dimension as i64,prepared.config_fingerprint,snapshot.source_watermark,prepared.rows.len() as i64,now])?;
    if tx.execute("UPDATE fm_v2_derived_generations SET state='building',updated_at=?3 WHERE owner_id=?1 AND generation_id=?2 AND state='planned'",params![owner,prepared.generation_id,now])?!=1 {return Err(RepositoryError::NotFound);}
    for row in &prepared.rows {
        let mut blob=Vec::with_capacity(row.values.len()*4);for value in &row.values {blob.extend_from_slice(&value.to_bits().to_le_bytes());}
        budget.check(row.chunk_id.len()+row.content_hash.len()+blob.len()).map_err(RepositoryError::Validation)?;
        tx.execute("INSERT INTO fm_v2_chunk_embeddings(owner_id,generation_id,chunk_id,dimension,embedding,content_hash,created_at) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![owner,prepared.generation_id,row.chunk_id,dimension as i64,blob,row.content_hash,now])?;
    }
    for (before,after) in [("building","validating"),("validating","ready")] {
        if tx.execute("UPDATE fm_v2_derived_generations SET state=?3,updated_at=?4,validated_at=CASE WHEN ?3='ready' THEN ?4 ELSE validated_at END WHERE owner_id=?1 AND generation_id=?2 AND state=?5",params![owner,prepared.generation_id,after,now,before])?!=1 {return Err(RepositoryError::NotFound);}
    }
    let publication=format!("pub_{}",uuid::Uuid::new_v4().simple());
    if let Some((generation,previous,watermark))=&current {
        if tx.execute("UPDATE fm_v2_index_pointers SET generation_id=?2,publication_tx=?3,publication_watermark=?4,published_at=?5 WHERE owner_id=?1 AND logical_space='documents_vector' AND workspace_key='' AND project_key='' AND generation_id=?6 AND publication_tx=?7 AND publication_watermark=?8",params![owner,prepared.generation_id,publication,snapshot.source_watermark,now,generation,previous,watermark])?!=1 {return Err(RepositoryError::NotFound);}
    } else {
        tx.execute("INSERT INTO fm_v2_index_pointers(owner_id,logical_space,workspace_key,project_key,generation_id,publication_tx,publication_watermark,published_at) VALUES(?1,'documents_vector','','',?2,?3,?4,?5)",params![owner,prepared.generation_id,publication,snapshot.source_watermark,now])?;
    }
    tx.execute("INSERT INTO fm_v2_index_publications(owner_id,publication_id,logical_space,workspace_key,project_key,previous_generation_id,generation_id,action,publication_watermark,created_at) VALUES(?1,?2,'documents_vector','','',?3,?4,'publish',?5,?6)",params![owner,publication,current.as_ref().map(|v|v.0.as_str()),prepared.generation_id,snapshot.source_watermark,now])?;
    Ok(json!({"version":1,"state":"published","generation_id":prepared.generation_id,"publication_id":publication,"source_watermark":snapshot.source_watermark,"eligibility_digest":prepared.eligibility_digest,"flat_checksum":prepared.flat_checksum.iter().map(|b|format!("{b:02x}")).collect::<String>()}))
}


/// An explicit Host utility operation; its key is identity, not authority.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentVectorBuildOriginal {
    pub version:u8, pub owner:String, pub account_id:String, pub operation_id:String,
    pub provider_ref:String, pub model:String, pub endpoint_class:String, pub dimension:usize,
    pub source_watermark:String, pub row_count:usize, pub expected_pointer:Option<ExpectedVectorPointer>,
}
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentVectorAppendRequest { pub original:DocumentVectorBuildOriginal, pub offset:usize, pub rows:Vec<VectorInputRow> }

pub(crate) fn staged_identity(r:&DocumentVectorBuildOriginal)->Result<(String,String),RepositoryError> {
    if r.version!=1 || [r.owner.as_str(),r.account_id.as_str(),r.operation_id.as_str(),r.provider_ref.as_str(),r.model.as_str(),r.endpoint_class.as_str()].iter().any(|s|s.is_empty()||s.trim()!=*s||s.len()>512)
        || r.operation_id.len()<16 || !r.operation_id.bytes().all(|b|b.is_ascii_alphanumeric()||b==b'_'||b==b'-')
        || r.expected_pointer.as_ref().is_some_and(|p|[&p.generation_id,&p.publication_id,&p.publication_watermark].iter().any(|x|x.is_empty()||x.len()>512))
        || !valid_sha256(&r.source_watermark) || r.dimension==0 || r.dimension>65536 || r.row_count>1_000_000
        || r.dimension.checked_mul(r.row_count).is_none_or(|n|n>16_000_000) {return Err(RepositoryError::NotFound)}
    let identity=json!(["openclank.document-vector-operation.v1",r.account_id,r.owner,r.operation_id]);
    let stage=format!("stage_vec_{}",sha256_hex(super::document_upsert::sorted_json(&identity,true)?.as_bytes()));
    let raw=serde_json::to_value(r).map_err(|_|RepositoryError::NotFound)?;
    Ok((stage,sha256_hex(super::document_upsert::sorted_json(&raw,true)?.as_bytes())))
}
pub(crate) fn stage_current(tx:&Transaction<'_>,r:&DocumentVectorBuildOriginal,stage:&str,original_hash:&str)->Result<bool,RepositoryError>{
    let row:Option<(String,String,String,String,i64,String,String,i64,String,String)>=tx.query_row("SELECT logical_space,provider_ref,model,endpoint_class,dimension,config_fingerprint,source_watermark,row_count,state,retention_state FROM fm_v2_derived_generations WHERE owner_id=?1 AND generation_id=?2 AND workspace_key='' AND project_key='' AND normalization='l2' AND metric='cosine' AND chunker_version='frankenmemory-rag-v2'",params![r.owner,stage],|v|Ok((v.get(0)?,v.get(1)?,v.get(2)?,v.get(3)?,v.get(4)?,v.get(5)?,v.get(6)?,v.get(7)?,v.get(8)?,v.get(9)?))).optional()?;
    match row {None=>Ok(false),Some((space,provider,model,endpoint,dim,hash,watermark,count,state,retention))=>{
        if space!="documents_vector"||provider!=r.provider_ref||model!=r.model||endpoint!=r.endpoint_class||dim!=r.dimension as i64||hash!=original_hash||watermark!=r.source_watermark||count!=r.row_count as i64||state!="building"||retention!="retained" {return Err(RepositoryError::NotFound)} Ok(true)
    }}
}
fn checked_stage_snapshot(tx:&Transaction<'_>,r:&DocumentVectorBuildOriginal,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<VectorCanonicalSnapshot,RepositoryError>{
    let mut snapshot=canonical_snapshot(tx,&r.owner,&r.account_id,view,budget)?;
    if snapshot.source_watermark!=r.source_watermark||snapshot.rows.len()!=r.row_count {return Err(RepositoryError::NotFound)}
    snapshot.rows.sort_by(|a,b|a.chunk_id.cmp(&b.chunk_id));Ok(snapshot)
}
/// Head is advisory; begin and finalize independently compare the complete pointer tuple.
pub(crate) fn vector_head(tx:&Transaction<'_>,r:&DocumentVectorSnapshotRequest,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    if r.version!=1 || r.after.is_some() || r.expected_watermark.is_some(){return Err(RepositoryError::NotFound)}
    let snapshot=canonical_snapshot(tx,&r.owner,&r.account_id,view,budget)?;
    let pointer:Option<(String,String,String)>=tx.query_row("SELECT generation_id,publication_tx,publication_watermark FROM fm_v2_index_pointers WHERE owner_id=?1 AND logical_space='documents_vector' AND workspace_key='' AND project_key=''",[&r.owner],|v|Ok((v.get(0)?,v.get(1)?,v.get(2)?))).optional()?;
    let descriptor=if let Some((generation,_,_))=&pointer {
        let value:Option<(String,String,String,i64)>=tx.query_row("SELECT provider_ref,model,endpoint_class,dimension FROM fm_v2_derived_generations WHERE owner_id=?1 AND generation_id=?2 AND logical_space='documents_vector' AND workspace_key='' AND project_key='' AND state='ready' AND retention_state='retained'",params![r.owner,generation],|v|Ok((v.get(0)?,v.get(1)?,v.get(2)?,v.get(3)?))).optional()?;
        let (provider_ref,model,endpoint_class,dimension)=value.ok_or(RepositoryError::NotFound)?;
        Some(json!({"provider_ref":provider_ref,"model":model,"endpoint_class":endpoint_class,"dimension":dimension}))
    } else {None};
    let expected_pointer=pointer.map(|(generation_id,publication_id,publication_watermark)|ExpectedVectorPointer{generation_id,publication_id,publication_watermark});
    Ok(json!({"version":1,"state":"available","source_watermark":snapshot.source_watermark,"row_count":snapshot.rows.len(),"expected_pointer":expected_pointer,"descriptor":descriptor}))
}

pub(crate) fn stage_begin(tx:&Transaction<'_>,r:&DocumentVectorBuildOriginal,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    require_guarded_epoch(tx)?;let (stage,hash)=staged_identity(r)?;super::document_vector_intent::require_stage_context(tx,r,view,budget)?;checked_stage_snapshot(tx,r,view,budget)?;
    if !stage_current(tx,r,&stage,&hash)? {
        let pointer:Option<(String,String,String)>=tx.query_row("SELECT generation_id,publication_tx,publication_watermark FROM fm_v2_index_pointers WHERE owner_id=?1 AND logical_space='documents_vector' AND workspace_key='' AND project_key=''",[&r.owner],|v|Ok((v.get(0)?,v.get(1)?,v.get(2)?))).optional()?;
        let matches=match (&pointer,&r.expected_pointer){(None,None)=>true,(Some((g,p,w)),Some(e))=>g==&e.generation_id&&p==&e.publication_id&&w==&e.publication_watermark,_=>false};
        if !matches{return Err(RepositoryError::NotFound)}
        let now=chrono::Utc::now().to_rfc3339();
        tx.execute("INSERT INTO fm_v2_derived_generations(owner_id,generation_id,logical_space,workspace_key,project_key,provider_ref,model,endpoint_class,dimension,normalization,metric,chunker_version,config_fingerprint,source_watermark,row_count,state,retention_state,created_at,updated_at) VALUES(?1,?2,'documents_vector','','',?3,?4,?5,?6,'l2','cosine','frankenmemory-rag-v2',?7,?8,?9,'planned','retained',?10,?10)",params![r.owner,stage,r.provider_ref,r.model,r.endpoint_class,r.dimension as i64,hash,r.source_watermark,r.row_count as i64,now])?;
        if tx.execute("UPDATE fm_v2_derived_generations SET state='building' WHERE owner_id=?1 AND generation_id=?2 AND state='planned'",params![r.owner,stage])?!=1{return Err(RepositoryError::NotFound)}
    }
    Ok(json!({"version":1,"state":"building","stage_id":stage}))
}
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentVectorTextRequest {pub original:DocumentVectorBuildOriginal,pub offset:usize}
pub(crate) fn stage_text(tx:&Transaction<'_>,request:&DocumentVectorTextRequest,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    require_guarded_epoch(tx)?;let r=&request.original;let (stage,hash)=staged_identity(r)?;super::document_vector_intent::require_stage_context(tx,r,view,budget)?;let snapshot=checked_stage_snapshot(tx,r,view,budget)?;
    if !stage_current(tx,r,&stage,&hash)? || request.offset>=r.row_count {return Err(RepositoryError::NotFound)}
    let end=request.offset+1;let mut items=Vec::new();
    for row in &snapshot.rows[request.offset..end] {
        let text:Option<String>=tx.query_row("SELECT CASE WHEN length(CAST(text AS BLOB))<=65536 THEN text END FROM fm_v2_chunks WHERE owner_id=?1 AND chunk_id=?2 AND document_id=?3",params![r.owner,row.chunk_id,row.document_id],|v|v.get(0)).optional()?.flatten();
        let text=text.ok_or(RepositoryError::NotFound)?;budget.check(text.len()).map_err(RepositoryError::Validation)?;
        if sha256_hex(text.as_bytes())!=row.content_hash{return Err(RepositoryError::NotFound)}
        items.push(json!({"chunk_id":row.chunk_id,"content_hash":row.content_hash,"source_revision":row.source_revision,"text":text}));
    }
    Ok(json!({"version":1,"state":"available","stage_id":stage,"offset":request.offset,"items":items}))
}

pub(crate) fn stage_append(tx:&Transaction<'_>,request:&DocumentVectorAppendRequest,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    require_guarded_epoch(tx)?;let r=&request.original;let (stage,hash)=staged_identity(r)?;super::document_vector_intent::require_stage_context(tx,r,view,budget)?;let snapshot=checked_stage_snapshot(tx,r,view,budget)?;
    let batch=(57000/(r.dimension*24+512)).clamp(1,16);
    if !stage_current(tx,r,&stage,&hash)? || request.offset%batch!=0 || request.offset>=r.row_count || request.rows.len()!=(r.row_count-request.offset).min(batch) {return Err(RepositoryError::NotFound)}
    let mut existing=0;let mut blobs=Vec::with_capacity(request.rows.len());
    for (row,current) in request.rows.iter().zip(&snapshot.rows[request.offset..request.offset+request.rows.len()]) {
        if row.chunk_id!=current.chunk_id||row.content_hash!=current.content_hash||row.source_revision!=current.source_revision||row.values.len()!=r.dimension||row.values.iter().any(|v|!v.is_finite()) {return Err(RepositoryError::NotFound)}
        let norm:f64=row.values.iter().map(|v|(*v as f64)*(*v as f64)).sum();if !norm.is_finite()||(norm.sqrt()-1.).abs()>1e-5{return Err(RepositoryError::NotFound)}
        let blob:Vec<u8>=row.values.iter().flat_map(|v|v.to_bits().to_le_bytes()).collect();budget.check(blob.len()+row.chunk_id.len()+row.content_hash.len()).map_err(RepositoryError::Validation)?;
        let old:Option<(i64,Vec<u8>,String)>=tx.query_row("SELECT dimension,embedding,content_hash FROM fm_v2_chunk_embeddings WHERE owner_id=?1 AND generation_id=?2 AND chunk_id=?3",params![r.owner,stage,row.chunk_id],|v|Ok((v.get(0)?,v.get(1)?,v.get(2)?))).optional()?;
        if let Some((dim,old_blob,old_hash))=old {if dim!=r.dimension as i64||old_blob!=blob||old_hash!=row.content_hash{return Err(RepositoryError::NotFound)}existing+=1;}
        blobs.push(blob);
    }
    if existing!=0&&existing!=request.rows.len(){return Err(RepositoryError::NotFound)}
    if existing==0 {let now=chrono::Utc::now().to_rfc3339();for (row,blob) in request.rows.iter().zip(blobs){tx.execute("INSERT INTO fm_v2_chunk_embeddings(owner_id,generation_id,chunk_id,dimension,embedding,content_hash,created_at) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![r.owner,stage,row.chunk_id,r.dimension as i64,blob,row.content_hash,now])?;}}
    Ok(json!({"version":1,"state":"building","stage_id":stage,"offset":request.offset,"count":request.rows.len()}))
}
pub(crate) fn stage_finalize(tx:&Transaction<'_>,r:&DocumentVectorBuildOriginal,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    require_guarded_epoch(tx)?;let (stage,hash)=staged_identity(r)?;super::document_vector_intent::require_stage_context(tx,r,view,budget)?;let snapshot=checked_stage_snapshot(tx,r,view,budget)?;
    if !stage_current(tx,r,&stage,&hash)? {return Err(RepositoryError::NotFound)}
    let revisions:std::collections::BTreeMap<_,_>=snapshot.rows.iter().map(|x|(x.chunk_id.clone(),x.source_revision)).collect();
    let mut statement=tx.prepare("SELECT chunk_id,dimension,embedding,content_hash FROM fm_v2_chunk_embeddings WHERE owner_id=?1 AND generation_id=?2 ORDER BY chunk_id")?;
    let mut cursor=statement.query(params![r.owner,stage])?;let mut rows=Vec::new();let mut total=0usize;
    while let Some(row)=cursor.next()? {
        budget.check(0).map_err(RepositoryError::Validation)?;
        let id:String=row.get(0)?;let dim:i64=row.get(1)?;let blob:Vec<u8>=row.get(2)?;let content_hash:String=row.get(3)?;
        total=total.checked_add(blob.len()).ok_or(RepositoryError::NotFound)?;
        if total>64_000_000 || dim!=r.dimension as i64 || blob.len()!=r.dimension*4 {return Err(RepositoryError::NotFound)}
        let source_revision=*revisions.get(&id).ok_or(RepositoryError::NotFound)?;
        let values=blob.chunks_exact(4).map(|b|f32::from_le_bytes([b[0],b[1],b[2],b[3]])).collect();
        rows.push(VectorInputRow{chunk_id:id,content_hash,source_revision,values});
    }
    drop(cursor);drop(statement);
    let prepared=prepare_generation(&r.owner,&r.account_id,&snapshot,&r.provider_ref,&r.model,&r.endpoint_class,r.dimension,rows)?;
    let publication=format!("pub_vec_{}",sha256_hex(format!("openclank.document-vector-publication.v1\0{stage}\0{hash}").as_bytes()));
    let previous:Option<(String,String,String)>=tx.query_row("SELECT generation_id,publication_tx,publication_watermark FROM fm_v2_index_pointers WHERE owner_id=?1 AND logical_space='documents_vector' AND workspace_key='' AND project_key=''",[&r.owner],|v|Ok((v.get(0)?,v.get(1)?,v.get(2)?))).optional()?;
    let prior_publication:Option<(String,String)>=tx.query_row("SELECT generation_id,publication_watermark FROM fm_v2_index_publications WHERE owner_id=?1 AND publication_id=?2 AND logical_space='documents_vector' AND workspace_key='' AND project_key='' AND action='publish'",params![r.owner,publication],|v|Ok((v.get(0)?,v.get(1)?))).optional()?;
    if let Some((generation,watermark))=prior_publication {
        if generation!=prepared.generation_id||watermark!=snapshot.source_watermark{return Err(RepositoryError::NotFound)}
        let current=previous.as_ref().is_some_and(|p|p.0==generation&&p.1==publication&&p.2==watermark);
        return Ok(json!({"version":1,"state":"published","generation_id":generation,"publication_id":publication,"source_watermark":watermark,"current":current}));
    }
    let matches=match (&previous,&r.expected_pointer){(None,None)=>true,(Some((g,p,w)),Some(e))=>g==&e.generation_id&&p==&e.publication_id&&w==&e.publication_watermark,_=>false};
    if !matches{return Err(RepositoryError::NotFound)}
    if let Some((g,p,w))=&previous {
        let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_index_publications p JOIN fm_v2_derived_generations g ON g.owner_id=p.owner_id AND g.generation_id=p.generation_id WHERE p.owner_id=?1 AND p.publication_id=?2 AND p.logical_space='documents_vector' AND p.workspace_key='' AND p.project_key='' AND p.generation_id=?3 AND p.publication_watermark=?4 AND p.action IN ('publish','rollback') AND g.state='ready' AND g.retention_state='retained')",params![r.owner,p,g,w],|v|v.get(0))?;
        if !valid{return Err(RepositoryError::NotFound)}
    }
    let existing:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_derived_generations WHERE owner_id=?1 AND generation_id=?2)",params![r.owner,prepared.generation_id],|v|v.get(0))?;
    if existing {
        let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_derived_generations WHERE owner_id=?1 AND generation_id=?2 AND logical_space='documents_vector' AND workspace_key='' AND project_key='' AND provider_ref=?3 AND model=?4 AND endpoint_class=?5 AND dimension=?6 AND config_fingerprint=?7 AND source_watermark=?8 AND row_count=?9 AND state='ready' AND retention_state='retained' AND validated_at IS NOT NULL) AND NOT EXISTS(SELECT 1 FROM fm_v2_chunk_embeddings a LEFT JOIN fm_v2_chunk_embeddings b ON b.owner_id=a.owner_id AND b.generation_id=?2 AND b.chunk_id=a.chunk_id WHERE a.owner_id=?1 AND a.generation_id=?10 AND (b.chunk_id IS NULL OR b.dimension<>a.dimension OR b.content_hash<>a.content_hash OR b.embedding<>a.embedding)) AND (SELECT count(*) FROM fm_v2_chunk_embeddings WHERE owner_id=?1 AND generation_id=?2)=?9",params![r.owner,prepared.generation_id,r.provider_ref,r.model,r.endpoint_class,r.dimension as i64,prepared.config_fingerprint,r.source_watermark,r.row_count as i64,stage],|v|v.get(0))?;
        if !valid{return Err(RepositoryError::NotFound)}
    } else {
        let now=chrono::Utc::now().to_rfc3339();
        tx.execute("INSERT INTO fm_v2_derived_generations(owner_id,generation_id,logical_space,workspace_key,project_key,provider_ref,model,endpoint_class,dimension,normalization,metric,chunker_version,config_fingerprint,source_watermark,row_count,state,retention_state,created_at,updated_at) VALUES(?1,?2,'documents_vector','','',?3,?4,?5,?6,'l2','cosine','frankenmemory-rag-v2',?7,?8,?9,'planned','retained',?10,?10)",params![r.owner,prepared.generation_id,r.provider_ref,r.model,r.endpoint_class,r.dimension as i64,prepared.config_fingerprint,r.source_watermark,r.row_count as i64,now])?;
        tx.execute("UPDATE fm_v2_derived_generations SET state='building' WHERE owner_id=?1 AND generation_id=?2 AND state='planned'",params![r.owner,prepared.generation_id])?;
        let count=tx.execute("INSERT INTO fm_v2_chunk_embeddings(owner_id,generation_id,chunk_id,dimension,embedding,content_hash,created_at) SELECT owner_id,?3,chunk_id,dimension,embedding,content_hash,created_at FROM fm_v2_chunk_embeddings WHERE owner_id=?1 AND generation_id=?2",params![r.owner,stage,prepared.generation_id])?;
        if count!=r.row_count{return Err(RepositoryError::NotFound)}
        tx.execute("UPDATE fm_v2_derived_generations SET state='validating' WHERE owner_id=?1 AND generation_id=?2 AND state='building'",params![r.owner,prepared.generation_id])?;
        tx.execute("UPDATE fm_v2_derived_generations SET state='ready',validated_at=?3,updated_at=?3 WHERE owner_id=?1 AND generation_id=?2 AND state='validating'",params![r.owner,prepared.generation_id,now])?;
    }
    let now=chrono::Utc::now().to_rfc3339();
    if let Some((g,p,w))=&previous {if tx.execute("UPDATE fm_v2_index_pointers SET generation_id=?2,publication_tx=?3,publication_watermark=?4,published_at=?5 WHERE owner_id=?1 AND logical_space='documents_vector' AND workspace_key='' AND project_key='' AND generation_id=?6 AND publication_tx=?7 AND publication_watermark=?8",params![r.owner,prepared.generation_id,publication,snapshot.source_watermark,now,g,p,w])?!=1{return Err(RepositoryError::NotFound)}}
    else {tx.execute("INSERT INTO fm_v2_index_pointers(owner_id,logical_space,workspace_key,project_key,generation_id,publication_tx,publication_watermark,published_at) VALUES(?1,'documents_vector','','',?2,?3,?4,?5)",params![r.owner,prepared.generation_id,publication,snapshot.source_watermark,now])?;}
    tx.execute("INSERT INTO fm_v2_index_publications(owner_id,publication_id,logical_space,workspace_key,project_key,previous_generation_id,generation_id,action,publication_watermark,created_at) VALUES(?1,?2,'documents_vector','','',?3,?4,'publish',?5,?6)",params![r.owner,publication,previous.as_ref().map(|p|p.0.as_str()),prepared.generation_id,snapshot.source_watermark,now])?;
    Ok(json!({"version":1,"state":"published","generation_id":prepared.generation_id,"publication_id":publication,"source_watermark":snapshot.source_watermark,"current":true}))
}


#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentVectorSearchRequest {
    pub version:u8, pub owner:String, pub account_id:String, pub expected_pointer:ExpectedVectorPointer,
    pub provider_ref:String, pub model:String, pub endpoint_class:String, pub dimension:usize,
    pub query:Vec<f32>, pub limit:usize,
}
pub(crate) fn stage_search(tx:&Transaction<'_>,r:&DocumentVectorSearchRequest,view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    if r.version!=1 || r.limit==0 || r.limit>10000 || r.dimension==0 || r.dimension>65536 || r.query.len()!=r.dimension{return Err(RepositoryError::NotFound)}
    search_precomputed(tx,&r.owner,&r.account_id,&r.expected_pointer,&r.provider_ref,&r.model,&r.endpoint_class,r.dimension,r.query.clone(),r.limit,view,budget)
}

/// Rebuild and verify the exact active generation under one read snapshot.
/// The caller must authenticate the query-vector producer and current scope.
pub(crate) fn search_precomputed(
    tx:&Transaction<'_>,owner:&str,account:&str,expected:&ExpectedVectorPointer,
    provider_ref:&str,model:&str,endpoint_class:&str,dimension:usize,query:Vec<f32>,limit:usize,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget,
) -> Result<Value,RepositoryError> {
    use crate::retrieval::vector_backend::{FlatIndex,Metric,Profile,Row,Vector};
    require_guarded_epoch(tx)?;
    let snapshot=canonical_snapshot(tx,owner,account,view,budget)?;
    if snapshot.source_watermark!=expected.publication_watermark {return Err(RepositoryError::NotFound);}
    let present:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_index_pointers p JOIN fm_v2_index_publications u ON u.owner_id=p.owner_id AND u.publication_id=p.publication_tx AND u.generation_id=p.generation_id AND u.publication_watermark=p.publication_watermark AND u.logical_space=p.logical_space AND u.workspace_key=p.workspace_key AND u.project_key=p.project_key JOIN fm_v2_derived_generations g ON g.owner_id=p.owner_id AND g.generation_id=p.generation_id WHERE p.owner_id=?1 AND p.logical_space='documents_vector' AND p.workspace_key='' AND p.project_key='' AND p.generation_id=?2 AND p.publication_tx=?3 AND p.publication_watermark=?4 AND u.action IN ('publish','rollback') AND g.logical_space=p.logical_space AND g.workspace_key=p.workspace_key AND g.project_key=p.project_key AND g.provider_ref=?5 AND g.model=?6 AND g.endpoint_class=?7 AND g.dimension=?8 AND g.normalization='l2' AND g.metric='cosine' AND g.chunker_version='frankenmemory-rag-v2' AND g.source_watermark=p.publication_watermark AND g.state='ready' AND g.retention_state='retained' AND g.validated_at IS NOT NULL)",params![owner,expected.generation_id,expected.publication_id,expected.publication_watermark,provider_ref,model,endpoint_class,dimension as i64],|r|r.get(0))?;
    if !present {return Err(RepositoryError::NotFound);}
    let revisions:std::collections::BTreeMap<_,_>=snapshot.rows.iter().map(|r|(r.chunk_id.clone(),r.source_revision)).collect();
    let mut statement=tx.prepare("SELECT chunk_id,dimension,embedding,content_hash FROM fm_v2_chunk_embeddings WHERE owner_id=?1 AND generation_id=?2 ORDER BY chunk_id")?;
    let mut cursor=statement.query(params![owner,expected.generation_id])?;let mut rows=Vec::new();
    while let Some(row)=cursor.next()? {
        let id:String=row.get(0)?;let dim:i64=row.get(1)?;let blob:Vec<u8>=row.get(2)?;let hash:String=row.get(3)?;
        budget.check(id.len()+hash.len()+blob.len()).map_err(RepositoryError::Validation)?;
        if dim!=dimension as i64 || blob.len()!=dimension.checked_mul(4).ok_or(RepositoryError::NotFound)? {return Err(RepositoryError::NotFound);}
        let revision=*revisions.get(&id).ok_or(RepositoryError::NotFound)?;
        let values=blob.chunks_exact(4).map(|b|f32::from_le_bytes([b[0],b[1],b[2],b[3]])).collect();
        rows.push(VectorInputRow{chunk_id:id,content_hash:hash,source_revision:revision,values});
    }
    drop(cursor);drop(statement);
    let prepared=prepare_generation(owner,account,&snapshot,provider_ref,model,endpoint_class,dimension,rows)?;
    let (config,count):(String,i64)=tx.query_row("SELECT config_fingerprint,row_count FROM fm_v2_derived_generations WHERE owner_id=?1 AND generation_id=?2",params![owner,expected.generation_id],|r|Ok((r.get(0)?,r.get(1)?)))?;
    if prepared.generation_id!=expected.generation_id || prepared.config_fingerprint!=config || count!=prepared.rows.len() as i64 {return Err(RepositoryError::NotFound);}
    let profile=Profile{owner:owner.into(),workspace:String::new(),project:String::new(),generation_id:prepared.generation_id.clone(),source_watermark:snapshot.source_watermark,
        eligibility_epoch:prepared.eligibility_digest.clone(),model:model.into(),model_version:"unknown".into(),dimension,metric:Metric::UnitCosine,normalization:"unit_l2".into()};
    let eligible=prepared.rows.iter().map(|r|r.chunk_id.clone()).collect();
    let flat_rows=prepared.rows.iter().map(|r|Row{id:r.chunk_id.clone(),source_revision:format!("{}:{}",r.source_revision,r.content_hash),vector:Vector::Float(r.values.clone())}).collect();
    let flat=FlatIndex::build(profile.clone(),flat_rows).map_err(|_|RepositoryError::NotFound)?;
    if flat.checksum()!=prepared.flat_checksum {return Err(RepositoryError::NotFound);}
    let result=flat.search(&profile,&eligible,None,&Vector::Float(query),limit,None).map_err(|_|RepositoryError::NotFound)?;
    let hits=result.hits.into_iter().filter(|h|h.score>0.0).map(|h|json!({"chunk_id":h.id,"score":h.score})).collect::<Vec<_>>();
    Ok(json!({"version":1,"state":"ranked","hits":hits,"truncated":result.truncated}))
}

/// Private selected-profile request. The caller must supply an authenticated
/// settings/owner admission; the choice itself conveys no authority.
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(tag="kind",rename_all="snake_case",deny_unknown_fields)]
pub enum VectorProfileTransform {Identity,CenteredSignedPermutation{seed:u64}}
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(tag="family",rename_all="snake_case",deny_unknown_fields)]
pub enum VectorProfileChoice {
    IvfFlat{seed:u64,lists:usize,iterations:usize},
    IvfMinibatch{seed:u64,lists:usize,iterations:usize},
    Hnsw{seed:u64,m:usize,ef_construction:usize},
    Scalar8{transform:VectorProfileTransform},
    Pq{seed:u64,transform:VectorProfileTransform,parts:usize,centroids:usize,iterations:usize},
}
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct VectorProfileBuildRequest {pub version:u8,pub owner:String,pub account_id:String,pub expected_pointer:ExpectedVectorPointer,pub choice:VectorProfileChoice}
pub(crate) struct VectorProfileSource {
    pub descriptor:String,pub descriptor_sha:[u8;32],pub profile_id:String,pub family:&'static str,
    pub flat_checksum:[u8;32],pub profile:crate::retrieval::vector_backend::Profile,
    pub rows:Vec<crate::retrieval::vector_backend::Row>,pub choice:VectorProfileChoice,
}
pub(crate) struct VectorProfileArtifact {pub source:VectorProfileSource,pub bytes:Vec<u8>,pub representation_sha:[u8;32]}

/// Read the actual current generation, canonical rows, receipts and pointer in
/// one snapshot. The returned data is a build input, not a publication grant.
pub(crate) fn profile_source(tx:&Transaction<'_>,r:&VectorProfileBuildRequest,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<VectorProfileSource,RepositoryError>{
    use crate::retrieval::vector_backend::{FlatIndex,Metric,Profile,Row,Vector};
    use sha2::{Digest,Sha256};
    require_guarded_epoch(tx)?;if r.version!=1{return Err(RepositoryError::NotFound)}
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.vector_profiles.v1'",[],|v|v.get(0)).optional()?;
    if marker.as_deref()!=Some("1"){return Err(RepositoryError::NotFound)}
    let snapshot=canonical_snapshot(tx,&r.owner,&r.account_id,view,budget)?;
    let e=&r.expected_pointer;if snapshot.source_watermark!=e.publication_watermark{return Err(RepositoryError::NotFound)}
    let meta:Option<(String,String,String,i64,String,i64)>=tx.query_row("SELECT g.provider_ref,g.model,g.endpoint_class,g.dimension,g.config_fingerprint,g.row_count FROM fm_v2_index_pointers p JOIN fm_v2_index_publications u ON u.owner_id=p.owner_id AND u.publication_id=p.publication_tx AND u.generation_id=p.generation_id AND u.publication_watermark=p.publication_watermark AND u.logical_space=p.logical_space AND u.workspace_key=p.workspace_key AND u.project_key=p.project_key JOIN fm_v2_derived_generations g ON g.owner_id=p.owner_id AND g.generation_id=p.generation_id WHERE p.owner_id=?1 AND p.logical_space='documents_vector' AND p.workspace_key='' AND p.project_key='' AND p.generation_id=?2 AND p.publication_tx=?3 AND p.publication_watermark=?4 AND u.action IN ('publish','rollback') AND g.logical_space=p.logical_space AND g.workspace_key=p.workspace_key AND g.project_key=p.project_key AND g.normalization='l2' AND g.metric='cosine' AND g.chunker_version='frankenmemory-rag-v2' AND g.source_watermark=p.publication_watermark AND g.state='ready' AND g.retention_state='retained' AND g.validated_at IS NOT NULL",params![r.owner,e.generation_id,e.publication_id,e.publication_watermark],|v|Ok((v.get(0)?,v.get(1)?,v.get(2)?,v.get(3)?,v.get(4)?,v.get(5)?))).optional()?;
    let (provider_ref,model,endpoint,dim,config,count)=meta.ok_or(RepositoryError::NotFound)?;
    if dim<=0||dim>1024||count<=0||count>4096||count as usize!=snapshot.rows.len(){return Err(RepositoryError::NotFound)}
    let revisions:std::collections::BTreeMap<_,_>=snapshot.rows.iter().map(|v|(v.chunk_id.clone(),v.source_revision)).collect();
    let mut statement=tx.prepare("SELECT chunk_id,dimension,embedding,content_hash FROM fm_v2_chunk_embeddings WHERE owner_id=?1 AND generation_id=?2 ORDER BY chunk_id")?;
    let mut cursor=statement.query(params![r.owner,e.generation_id])?;let mut input=Vec::new();
    while let Some(row)=cursor.next()? {
        let id:String=row.get(0)?;let actual_dim:i64=row.get(1)?;let blob:Vec<u8>=row.get(2)?;let content_hash:String=row.get(3)?;
        budget.check(id.len()+blob.len()+content_hash.len()).map_err(RepositoryError::Validation)?;
        if actual_dim!=dim||blob.len()!=dim as usize*4{return Err(RepositoryError::NotFound)}
        let source_revision=*revisions.get(&id).ok_or(RepositoryError::NotFound)?;
        let values=blob.chunks_exact(4).map(|v|f32::from_le_bytes([v[0],v[1],v[2],v[3]])).collect();
        input.push(VectorInputRow{chunk_id:id,content_hash,source_revision,values});
    }
    drop(cursor);drop(statement);
    let prepared=prepare_generation(&r.owner,&r.account_id,&snapshot,&provider_ref,&model,&endpoint,dim as usize,input)?;
    if prepared.generation_id!=e.generation_id||prepared.config_fingerprint!=config||prepared.rows.len()!=count as usize{return Err(RepositoryError::NotFound)}
    let profile=Profile{owner:r.owner.clone(),workspace:String::new(),project:String::new(),generation_id:e.generation_id.clone(),source_watermark:snapshot.source_watermark.clone(),eligibility_epoch:prepared.eligibility_digest.clone(),model:model.clone(),model_version:"unknown".into(),dimension:dim as usize,metric:Metric::UnitCosine,normalization:"unit_l2".into()};
    let rows:Vec<Row>=prepared.rows.iter().map(|v|Row{id:v.chunk_id.clone(),source_revision:format!("{}:{}",v.source_revision,v.content_hash),vector:Vector::Float(v.values.clone())}).collect();
    let flat=FlatIndex::build(profile.clone(),rows.clone()).map_err(|_|RepositoryError::NotFound)?;let flat_checksum=flat.checksum();if flat_checksum!=prepared.flat_checksum{return Err(RepositoryError::NotFound)}
    let hex=|v:[u8;32]|v.iter().map(|x|format!("{x:02x}")).collect::<String>();
    let (family,seed,parameters)=match &r.choice {
        VectorProfileChoice::IvfMinibatch{seed,lists,iterations}=>("ivf_minibatch",*seed,json!({"lists":lists,"iterations":iterations})),
        VectorProfileChoice::IvfFlat{seed,lists,iterations}=>("ivf_flat",*seed,json!({"lists":lists,"iterations":iterations})),
        VectorProfileChoice::Hnsw{seed,m,ef_construction}=>("hnsw",*seed,json!({"m":m,"ef_construction":ef_construction})),
        VectorProfileChoice::Scalar8{transform}=>("scalar8",0,json!({"transform":transform})),
        VectorProfileChoice::Pq{seed,transform,parts,centroids,iterations}=>("pq",*seed,json!({"transform":transform,"parts":parts,"centroids":centroids,"iterations":iterations,"seed":seed})),
    };
    let descriptor=json!({"version":1,"family":family,"owner":r.owner,"account_id":r.account_id,"workspace_id":"","project_id":"","generation_id":e.generation_id,"source_watermark":snapshot.source_watermark,"eligibility_digest":prepared.eligibility_digest,"provider_ref":provider_ref,"model":model,"model_version":"unknown","dimension":dim,"metric":"unit_cosine","normalization":"unit_l2","flat_sha256":hex(flat_checksum),"training_sha256":hex(flat_checksum),"seed":seed,"parameters":parameters});
    let descriptor=super::document_upsert::sorted_json(&descriptor,true)?;if descriptor.len()>8192{return Err(RepositoryError::NotFound)}
    let descriptor_sha:[u8;32]=Sha256::digest(descriptor.as_bytes()).into();let profile_id=hex(descriptor_sha);
    Ok(VectorProfileSource{descriptor,descriptor_sha,profile_id,family,flat_checksum,profile,rows,choice:r.choice.clone()})
}

/// Runs after the read transaction is closed; no SQLite lock or pointer change.
pub(crate) fn build_profile(source:VectorProfileSource)->Result<VectorProfileArtifact,RepositoryError>{
    use crate::retrieval::vector_backend::{profiles,quantized};use sha2::{Digest,Sha256};
    let transform=|value:&VectorProfileTransform|match value {VectorProfileTransform::Identity=>quantized::TransformSpec::Identity,VectorProfileTransform::CenteredSignedPermutation{seed}=>quantized::TransformSpec::CenteredSignedPermutation{seed:*seed}};
    let bytes=match &source.choice {
        VectorProfileChoice::IvfFlat{seed,lists,iterations} | VectorProfileChoice::IvfMinibatch{seed,lists,iterations}=>{let minibatch=matches!(&source.choice,VectorProfileChoice::IvfMinibatch{..});let spec=profiles::IndexSpec{version:1,profile:source.profile.clone(),flat_checksum:source.flat_checksum,training_checksum:source.flat_checksum,seed:*seed,family:if minibatch {profiles::Family::IvfMinibatch{lists:*lists,iterations:*iterations}} else {profiles::Family::IvfFlat{lists:*lists,iterations:*iterations}}};profiles::Index::build(spec,source.rows.clone(),source.rows.clone()).and_then(|i|i.to_ocvp(source.descriptor_sha))},
        VectorProfileChoice::Hnsw{seed,m,ef_construction}=>{let spec=profiles::IndexSpec{version:1,profile:source.profile.clone(),flat_checksum:source.flat_checksum,training_checksum:source.flat_checksum,seed:*seed,family:profiles::Family::Hnsw{m:*m,ef_construction:*ef_construction}};profiles::Index::build(spec,source.rows.clone(),source.rows.clone()).and_then(|i|i.to_ocvp(source.descriptor_sha))},
        VectorProfileChoice::Scalar8{transform:t}=>{let spec=quantized::Spec{version:1,profile:source.profile.clone(),flat_checksum:source.flat_checksum,training_checksum:source.flat_checksum,transform:transform(t),quantizer:quantized::QuantizerSpec::Scalar8};quantized::Index::build(spec,source.rows.clone(),source.rows.clone()).and_then(|i|i.to_ocvp(source.descriptor_sha))},
        VectorProfileChoice::Pq{seed,transform:t,parts,centroids,iterations}=>{let spec=quantized::Spec{version:1,profile:source.profile.clone(),flat_checksum:source.flat_checksum,training_checksum:source.flat_checksum,transform:transform(t),quantizer:quantized::QuantizerSpec::Product{parts:*parts,centroids:*centroids,iterations:*iterations,seed:*seed}};quantized::Index::build(spec,source.rows.clone(),source.rows.clone()).and_then(|i|i.to_ocvp(source.descriptor_sha))},
    }.map_err(|_|RepositoryError::NotFound)?;
    if bytes.is_empty()||bytes.len()>16*1024*1024{return Err(RepositoryError::NotFound)}
    let representation_sha=Sha256::digest(&bytes).into();Ok(VectorProfileArtifact{source,bytes,representation_sha})
}

fn verify_profile_artifact(source:&VectorProfileSource,bytes:&[u8],sha:[u8;32])->Result<(),RepositoryError>{
    use crate::retrieval::vector_backend::{profiles,quantized};
    let transform=|value:&VectorProfileTransform|match value {VectorProfileTransform::Identity=>quantized::TransformSpec::Identity,VectorProfileTransform::CenteredSignedPermutation{seed}=>quantized::TransformSpec::CenteredSignedPermutation{seed:*seed}};
    match &source.choice {
        VectorProfileChoice::IvfFlat{seed,lists,iterations} | VectorProfileChoice::IvfMinibatch{seed,lists,iterations}=>{let minibatch=matches!(&source.choice,VectorProfileChoice::IvfMinibatch{..});let spec=profiles::IndexSpec{version:1,profile:source.profile.clone(),flat_checksum:source.flat_checksum,training_checksum:source.flat_checksum,seed:*seed,family:if minibatch {profiles::Family::IvfMinibatch{lists:*lists,iterations:*iterations}} else {profiles::Family::IvfFlat{lists:*lists,iterations:*iterations}}};profiles::Index::from_ocvp(spec,source.rows.clone(),source.descriptor_sha,bytes,sha).map(|_|())},
        VectorProfileChoice::Hnsw{seed,m,ef_construction}=>{let spec=profiles::IndexSpec{version:1,profile:source.profile.clone(),flat_checksum:source.flat_checksum,training_checksum:source.flat_checksum,seed:*seed,family:profiles::Family::Hnsw{m:*m,ef_construction:*ef_construction}};profiles::Index::from_ocvp(spec,source.rows.clone(),source.descriptor_sha,bytes,sha).map(|_|())},
        VectorProfileChoice::Scalar8{transform:t}=>{let spec=quantized::Spec{version:1,profile:source.profile.clone(),flat_checksum:source.flat_checksum,training_checksum:source.flat_checksum,transform:transform(t),quantizer:quantized::QuantizerSpec::Scalar8};quantized::Index::from_ocvp(spec,source.rows.clone(),source.descriptor_sha,bytes,sha).map(|_|())},
        VectorProfileChoice::Pq{seed,transform:t,parts,centroids,iterations}=>{let spec=quantized::Spec{version:1,profile:source.profile.clone(),flat_checksum:source.flat_checksum,training_checksum:source.flat_checksum,transform:transform(t),quantizer:quantized::QuantizerSpec::Product{parts:*parts,centroids:*centroids,iterations:*iterations,seed:*seed}};quantized::Index::from_ocvp(spec,source.rows.clone(),source.descriptor_sha,bytes,sha).map(|_|())},
    }.map_err(|_|RepositoryError::NotFound)
}

/// Install only the complete derived representation under current custody.
/// The caller owns an Immediate transaction and rolls back on every error.
pub(crate) fn commit_profile(tx:&Transaction<'_>,r:&VectorProfileBuildRequest,artifact:&VectorProfileArtifact,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    use sha2::{Digest,Sha256};
    let current=profile_source(tx,r,view,budget)?;
    if current.descriptor!=artifact.source.descriptor||current.profile_id!=artifact.source.profile_id||current.flat_checksum!=artifact.source.flat_checksum
        || artifact.bytes.is_empty()||artifact.bytes.len()>16*1024*1024 || <[u8;32]>::from(Sha256::digest(&artifact.bytes))!=artifact.representation_sha {return Err(RepositoryError::NotFound)}
    // Work is charged before the first mutation. The existing maintenance cap
    // may conservatively reject a large profile; rejection leaves no partial row.
    budget.check(artifact.bytes.len()).map_err(RepositoryError::Validation)?;
    verify_profile_artifact(&current,&artifact.bytes,artifact.representation_sha)?;
    let hex=|v:[u8;32]|v.iter().map(|x|format!("{x:02x}")).collect::<String>();
    let count=artifact.bytes.len().div_ceil(262144);let flat=hex(current.flat_checksum);let rep=hex(artifact.representation_sha);
    let existing:Option<(i64,String,String,String,String,i64,i64)>=tx.query_row("SELECT version,family,descriptor_json,flat_sha256,representation_sha256,byte_count,chunk_count FROM fm_v2_vector_profiles WHERE owner_id=?1 AND generation_id=?2 AND profile_id=?3",params![r.owner,r.expected_pointer.generation_id,current.profile_id],|v|Ok((v.get(0)?,v.get(1)?,v.get(2)?,v.get(3)?,v.get(4)?,v.get(5)?,v.get(6)?))).optional()?;
    if let Some((version,family,descriptor,flat_sha,representation,bytes,chunks))=existing {
        let training:String=tx.query_row("SELECT training_sha256 FROM fm_v2_vector_profiles WHERE owner_id=?1 AND generation_id=?2 AND profile_id=?3",params![r.owner,r.expected_pointer.generation_id,current.profile_id],|v|v.get(0))?;
        if version!=1||family!=current.storage_family()||descriptor!=current.descriptor||flat_sha!=flat||training!=flat||representation!=rep||bytes!=artifact.bytes.len() as i64||chunks!=count as i64{return Err(RepositoryError::NotFound)}
        let mut statement=tx.prepare("SELECT ordinal,bytes FROM fm_v2_vector_profile_chunks WHERE owner_id=?1 AND generation_id=?2 AND profile_id=?3 ORDER BY ordinal")?;
        let mut cursor=statement.query(params![r.owner,r.expected_pointer.generation_id,current.profile_id])?;let mut i=0usize;
        while let Some(row)=cursor.next()? {let ordinal:i64=row.get(0)?;let bytes:Vec<u8>=row.get(1)?;let expected=artifact.bytes.chunks(262144).nth(i).ok_or(RepositoryError::NotFound)?;
            if ordinal!=i as i64||bytes!=expected{return Err(RepositoryError::NotFound)}i+=1;}
        if i!=count{return Err(RepositoryError::NotFound)}
    } else {
        tx.execute("INSERT INTO fm_v2_vector_profiles(owner_id,generation_id,profile_id,version,family,descriptor_json,flat_sha256,training_sha256,representation_sha256,byte_count,chunk_count) VALUES(?1,?2,?3,1,?4,?5,?6,?6,?7,?8,?9)",params![r.owner,r.expected_pointer.generation_id,current.profile_id,current.storage_family(),current.descriptor,flat,rep,artifact.bytes.len() as i64,count as i64])?;
        for (ordinal,bytes) in artifact.bytes.chunks(262144).enumerate(){tx.execute("INSERT INTO fm_v2_vector_profile_chunks(owner_id,generation_id,profile_id,ordinal,bytes) VALUES(?1,?2,?3,?4,?5)",params![r.owner,r.expected_pointer.generation_id,current.profile_id,ordinal as i64,bytes])?;}
    }
    // profile_source verified every original stored BLOB is exactly one f32
    // row of this dimension, and verified row count against the same pointer.
    let original_vectors = current.rows.len() * current.profile.dimension * std::mem::size_of::<f32>();
    let payload_bytes = json!({"original_vectors":original_vectors,
        "derived_profile":artifact.bytes.len(),"profile_descriptor":current.descriptor.len()});
    Ok(json!({"version":1,"state":"available","profile_id":current.profile_id,
        "generation_id":r.expected_pointer.generation_id,"family":current.family,
        "representation_sha256":rep,"payload_bytes":payload_bytes}))
}


/// Select only an already persisted matching profile. Missing representation
/// is an exact fallback; an incompatible or corrupt representation is refused.
pub(crate) fn search_profile(tx:&Transaction<'_>,r:&VectorProfileBuildRequest,profile_id:&str,query:Vec<f32>,limit:usize,
    effort:usize,visit_budget:usize,candidate_limit:usize,selector:Option<Vec<String>>,
    view:&owner_custody::VerifiedCustodyView<'_, '_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError>{
    use crate::retrieval::vector_backend::{FlatIndex,Vector,profiles,quantized};
    use sha2::{Digest,Sha256};
    let source=profile_source(tx,r,view,budget)?;
    if (!profile_id.is_empty()&&profile_id!=source.profile_id)||limit==0||limit>10000||query.len()!=source.profile.dimension{return Err(RepositoryError::NotFound)}
    let profile_id=source.profile_id.as_str();
    let flat=FlatIndex::build(source.profile.clone(),source.rows.clone()).map_err(|_|RepositoryError::NotFound)?;
    let eligible:std::collections::BTreeSet<String>=source.rows.iter().map(|v|v.id.clone()).collect();
    let selected=if let Some(values)=selector {let set:std::collections::BTreeSet<String>=values.iter().cloned().collect();if values.len()!=set.len()||!set.is_subset(&eligible){return Err(RepositoryError::NotFound)}Some(set)}else{None};
    let meta:Option<(i64,String,String,String,String,String,i64,i64)>=tx.query_row("SELECT version,family,descriptor_json,flat_sha256,training_sha256,representation_sha256,byte_count,chunk_count FROM fm_v2_vector_profiles WHERE owner_id=?1 AND generation_id=?2 AND profile_id=?3",params![r.owner,r.expected_pointer.generation_id,profile_id],|v|Ok((v.get(0)?,v.get(1)?,v.get(2)?,v.get(3)?,v.get(4)?,v.get(5)?,v.get(6)?,v.get(7)?))).optional()?;
    let query=Vector::Float(query);
    let eligible_vectors = selected.as_ref().map_or(eligible.len(), |ids| ids.len());
    let Some((version,family,descriptor,flat_sha,training_sha,representation,byte_count,chunk_count))=meta else {
        let found=flat.search(&source.profile,&eligible,selected.as_ref(),&query,limit,None).map_err(|_|RepositoryError::NotFound)?;
        let hits=found.hits.into_iter().filter(|v|v.score>0.).map(|v|json!({"chunk_id":v.id,"score":v.score})).collect::<Vec<_>>();
        return Ok(json!({"version":1,"state":"ranked","backend":"exact_fallback","approximate":false,"truncated":found.truncated,"hits":hits,
            "strategy":{"reason":"representation_missing","eligible_vectors":eligible_vectors,
              "candidate_scoring":"original","candidate_count":eligible_vectors}}));
    };
    let hex=|v:[u8;32]|v.iter().map(|x|format!("{x:02x}")).collect::<String>();
    if version!=1||family!=source.storage_family()||descriptor!=source.descriptor||flat_sha!=hex(source.flat_checksum)||training_sha!=flat_sha
        || !valid_sha256(&representation)||byte_count<=0||byte_count>16*1024*1024||chunk_count<=0||chunk_count>64||chunk_count as usize!=(byte_count as usize).div_ceil(262144){return Err(RepositoryError::NotFound)}
    budget.check(byte_count as usize).map_err(RepositoryError::Validation)?;
    let mut statement=tx.prepare("SELECT ordinal,CASE WHEN length(bytes)<=262144 THEN bytes END FROM fm_v2_vector_profile_chunks WHERE owner_id=?1 AND generation_id=?2 AND profile_id=?3 ORDER BY ordinal")?;
    let mut cursor=statement.query(params![r.owner,r.expected_pointer.generation_id,profile_id])?;let mut bytes=Vec::with_capacity(byte_count as usize);let mut ordinal=0usize;
    while let Some(row)=cursor.next()? {let index:i64=row.get(0)?;let chunk:Option<Vec<u8>>=row.get(1)?;let chunk=chunk.ok_or(RepositoryError::NotFound)?;
        let expected=(byte_count as usize-bytes.len()).min(262144);if index!=ordinal as i64||chunk.len()!=expected||chunk.is_empty(){return Err(RepositoryError::NotFound)}bytes.extend_from_slice(&chunk);ordinal+=1;}
    if ordinal!=chunk_count as usize||bytes.len()!=byte_count as usize||hex(Sha256::digest(&bytes).into())!=representation{return Err(RepositoryError::NotFound)}
    drop(cursor);drop(statement);let digest:[u8;32]=Sha256::digest(&bytes).into();
    let transform=|value:&VectorProfileTransform|match value {VectorProfileTransform::Identity=>quantized::TransformSpec::Identity,VectorProfileTransform::CenteredSignedPermutation{seed}=>quantized::TransformSpec::CenteredSignedPermutation{seed:*seed}};
    // Compare against the caller's existing ANN visit budget. A complete exact
    // scan is no more scoring work when every eligible vector fits inside it.
    // Always verify selected artifact integrity before returning a fallback.
    let ann_profile = matches!(&source.choice,
        VectorProfileChoice::IvfFlat { .. } | VectorProfileChoice::IvfMinibatch { .. } | VectorProfileChoice::Hnsw { .. });
    let exact_within_visit_budget = ann_profile && eligible_vectors <= visit_budget;
    let strategy_selector = selected.as_ref().or_else(|| exact_within_visit_budget.then_some(&eligible));
    let mut reranked = None;
    let (found,backend,approximate,audit)=match &source.choice {
        VectorProfileChoice::IvfFlat{seed,lists,iterations} | VectorProfileChoice::IvfMinibatch{seed,lists,iterations}=>{let minibatch=matches!(&source.choice,VectorProfileChoice::IvfMinibatch{..});let spec=profiles::IndexSpec{version:1,profile:source.profile.clone(),flat_checksum:source.flat_checksum,training_checksum:source.flat_checksum,seed:*seed,family:if minibatch {profiles::Family::IvfMinibatch{lists:*lists,iterations:*iterations}} else {profiles::Family::IvfFlat{lists:*lists,iterations:*iterations}}};let index=profiles::Index::from_ocvp(spec,source.rows.clone(),source.descriptor_sha,&bytes,digest).map_err(|_|RepositoryError::NotFound)?;let result=index.search(&source.profile,&eligible,strategy_selector,&query,limit,effort,visit_budget).map_err(|_|RepositoryError::NotFound)?;(result.results,if result.approximate {if minibatch {"ivf_minibatch"} else {"ivf_flat"}}else{"exact_fallback"},result.approximate,Some((result.visited,result.budget_exhausted)))},
        VectorProfileChoice::Hnsw{seed,m,ef_construction}=>{let spec=profiles::IndexSpec{version:1,profile:source.profile.clone(),flat_checksum:source.flat_checksum,training_checksum:source.flat_checksum,seed:*seed,family:profiles::Family::Hnsw{m:*m,ef_construction:*ef_construction}};let index=profiles::Index::from_ocvp(spec,source.rows.clone(),source.descriptor_sha,&bytes,digest).map_err(|_|RepositoryError::NotFound)?;let result=index.search(&source.profile,&eligible,strategy_selector,&query,limit,effort,visit_budget).map_err(|_|RepositoryError::NotFound)?;(result.results,if result.approximate {"hnsw"}else{"exact_fallback"},result.approximate,Some((result.visited,result.budget_exhausted)))},
        VectorProfileChoice::Scalar8{transform:t}=>{let spec=quantized::Spec{version:1,profile:source.profile.clone(),flat_checksum:source.flat_checksum,training_checksum:source.flat_checksum,transform:transform(t),quantizer:quantized::QuantizerSpec::Scalar8};let index=quantized::Index::from_ocvp(spec,source.rows.clone(),source.descriptor_sha,&bytes,digest).map_err(|_|RepositoryError::NotFound)?;let result=index.search(&source.profile,&eligible,selected.as_ref(),&query,limit,candidate_limit).map_err(|_|RepositoryError::NotFound)?;
            if result.approximate { reranked = Some(result.candidate_scores.len()); }(result.results,if result.approximate {"scalar8"}else{"exact_fallback"},result.approximate,None)},
        VectorProfileChoice::Pq{seed,transform:t,parts,centroids,iterations}=>{let spec=quantized::Spec{version:1,profile:source.profile.clone(),flat_checksum:source.flat_checksum,training_checksum:source.flat_checksum,transform:transform(t),quantizer:quantized::QuantizerSpec::Product{parts:*parts,centroids:*centroids,iterations:*iterations,seed:*seed}};let index=quantized::Index::from_ocvp(spec,source.rows.clone(),source.descriptor_sha,&bytes,digest).map_err(|_|RepositoryError::NotFound)?;let result=index.search(&source.profile,&eligible,selected.as_ref(),&query,limit,candidate_limit).map_err(|_|RepositoryError::NotFound)?;
            if result.approximate { reranked = Some(result.candidate_scores.len()); }(result.results,if result.approximate {"pq"}else{"exact_fallback"},result.approximate,None)},
    };
    let result_limit=found.truncated;
    let hits=found.hits.into_iter().filter(|v|v.score>0.).map(|v|json!({"chunk_id":v.id,"score":v.score})).collect::<Vec<_>>();
    let mut result=json!({"version":1,"state":"ranked","backend":backend,"approximate":approximate,"truncated":result_limit || audit.is_some_and(|(_,exhausted)|exhausted),"hits":hits});
    if let Some((visited,budget_exhausted))=audit {result["audit"]=json!({"visited":visited,"visit_budget_exhausted":budget_exhausted,"result_limit":result_limit});}
    let reason = if approximate { "profile_approximate" }
        else if selected.is_some() { "selector_exact" }
        else { "exact_within_visit_budget" };
    let candidate_count = if approximate { reranked } else { Some(eligible_vectors) };
    result["strategy"] = json!({"reason":reason,"eligible_vectors":eligible_vectors,
        "candidate_scoring":if reranked.is_some() {"original_rerank"} else {"original"},
        "candidate_count":candidate_count});
    Ok(result)
}


#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct VectorProfileSelectionPolicy {pub workspace_id:String,pub policy_projection:PolicyPublicationRefs,pub policy_input_json:String}
/// Only a live accepted account projection can select an ANN/quantized family.
/// The actual memory privilege bit is hashed as supplied; it is not elevated.
pub(crate) fn require_profile_selection(tx:&Transaction<'_>,account:&str,policy:&VectorProfileSelectionPolicy,
    choice:&VectorProfileChoice,query:Option<(usize,usize,usize,usize)>,budget:&mut ResetFenceBudget)->Result<(),RepositoryError>{
    if policy.workspace_id.is_empty()||policy.workspace_id.trim()!=policy.workspace_id||policy.workspace_id.len()>512||policy.policy_input_json.len()>65536{return Err(RepositoryError::NotFound)}
    require_policy_projection(tx,account,&policy.workspace_id,Some(&policy.policy_projection))?;
    budget.check(policy.policy_input_json.len()).map_err(RepositoryError::Validation)?;
    if sha256_hex(policy.policy_input_json.as_bytes())!=policy.policy_projection.account.fingerprint{return Err(RepositoryError::NotFound)}
    let raw=crate::field_authority::parse_unique_json(&policy.policy_input_json).map_err(RepositoryError::Validation)?;
    let root=raw.as_object().ok_or(RepositoryError::NotFound)?;
    if root.len()!=2||!root.get("can_manage_memory").is_some_and(Value::is_boolean){return Err(RepositoryError::NotFound)}
    let prefs=root.get("preferences").and_then(Value::as_object).ok_or(RepositoryError::NotFound)?;
    if prefs.get("document_vector_profile_version")!=Some(&json!([true,1])){return Err(RepositoryError::NotFound)}
    let entry=prefs.get("document_vector_profile").and_then(Value::as_array).filter(|v|v.len()==2&&v[0]==true).ok_or(RepositoryError::NotFound)?;
    let mut selected=entry[1].as_object().cloned().ok_or(RepositoryError::NotFound)?;
    if selected.remove("version")!=Some(json!(1)){return Err(RepositoryError::NotFound)}
    let family=selected.get("family").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?.to_owned();
    let number=|v:Option<Value>,low:u64,high:u64|->Result<usize,RepositoryError>{let n=v.and_then(|x|x.as_u64()).filter(|x|*x>=low&&*x<=high).ok_or(RepositoryError::NotFound)?;usize::try_from(n).map_err(|_|RepositoryError::NotFound)};
    let (effort,visits,candidates)=match family.as_str(){
        "ivf_flat"|"ivf_minibatch"=>{let e=number(selected.remove("effort"),1,128)?;let v=number(selected.remove("visit_budget"),1,4096)?;let lists=selected.get("lists").and_then(Value::as_u64).ok_or(RepositoryError::NotFound)?;if e as u64>lists{return Err(RepositoryError::NotFound)}(e,v,1)},
        "hnsw"=>{let e=number(selected.remove("effort"),1,256)?;let v=number(selected.remove("visit_budget"),1,4096)?;(e,v,1)},
        "scalar8"|"pq"=>{let n=number(selected.remove("candidate_limit"),1,1024)?;(1,1,n)},
        _=>return Err(RepositoryError::NotFound),
    };
    let selected_choice:VectorProfileChoice=serde_json::from_value(Value::Object(selected)).map_err(|_|RepositoryError::NotFound)?;
    if serde_json::to_value(&selected_choice).map_err(|_|RepositoryError::NotFound)?!=serde_json::to_value(choice).map_err(|_|RepositoryError::NotFound)? {return Err(RepositoryError::NotFound)}
    let valid=match choice {
        VectorProfileChoice::IvfFlat{seed,lists,iterations}|VectorProfileChoice::IvfMinibatch{seed,lists,iterations}=>*seed<=u32::MAX as u64 && (1..=128).contains(lists) && (1..=32).contains(iterations),
        VectorProfileChoice::Hnsw{seed,m,ef_construction}=>*seed<=u32::MAX as u64 && (2..=32).contains(m) && *ef_construction>=*m && *ef_construction<=256,
        VectorProfileChoice::Scalar8{transform}=>match transform {VectorProfileTransform::Identity=>true,VectorProfileTransform::CenteredSignedPermutation{seed}=>*seed<=u32::MAX as u64},
        VectorProfileChoice::Pq{seed,transform,parts,centroids,iterations}=>*seed<=u32::MAX as u64 && (1..=1024).contains(parts) && (2..=256).contains(centroids) && (1..=32).contains(iterations) && match transform {VectorProfileTransform::Identity=>true,VectorProfileTransform::CenteredSignedPermutation{seed}=>*seed<=u32::MAX as u64},
    };
    if !valid{return Err(RepositoryError::NotFound)}
    if let Some((limit,requested_effort,requested_visits,requested_candidates))=query {
        if limit==0||limit>10000||requested_effort!=effort||requested_visits!=visits||requested_candidates!=candidates
            || (matches!(choice,VectorProfileChoice::Scalar8{..}|VectorProfileChoice::Pq{..})&&limit>candidates){return Err(RepositoryError::NotFound)}
    }
    Ok(())
}


#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn canonical_watermark_matches_host_and_refuses_external_scope() {
        let mut conn=rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch(include_str!("../store/current-schema.sql")).unwrap();
        conn.execute_batch(include_str!("../store/schema-v17-authority.sql")).unwrap();
        conn.pragma_update(None,"foreign_keys",true).unwrap();
        conn.execute_batch(include_str!("../store/schema-v18-vector-profiles.sql")).unwrap();
        conn.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.vector_profiles.v1','1')",[]).unwrap();
        crate::writer_fence::register_guarded_writer_capability_for_epoch(&conn,18).unwrap();
        conn.pragma_update(None,"user_version",18).unwrap();
        let a="a".repeat(64); let b=sha256_hex(b"x");
        conn.execute("INSERT INTO fm_v2_sources(owner_id,source_id,source_uri,source_revision,content_hash,source_type,created_at) VALUES('alice','src_3fbc489d0abc741befd73d6a421cf216','café',1,?1,'owner_imported_file','now')",[&a]).unwrap();
        conn.execute("INSERT INTO fm_v2_documents(owner_id,document_id,source_id,source_revision,parser_version,content_hash,created_at) VALUES('alice','doc_73449179a945f69f249953f7db1252b2','src_3fbc489d0abc741befd73d6a421cf216',1,'frankenmemory-rag-v2',?1,'now')",[&a]).unwrap();
        conn.execute("INSERT INTO fm_v2_chunks(owner_id,chunk_id,document_id,ordinal,text,content_hash,locator_json,created_at) VALUES('alice','chunk_7ad8fe10f001b5d7699ddc41b4fdcaff','doc_73449179a945f69f249953f7db1252b2',0,'x',?1,'{}','now')",[&b]).unwrap();
        // Synthetic paired receipt models the immutable guarded import envelope;
        // a separate Host/Native case is still required for actual source custody.
        let operation="operation-test";let request_sha="c".repeat(64);
        let key=super::super::document_upsert::sorted_json(&json!(["account",operation]),false).unwrap();
        let receipt_id=format!("cs_s01_document_{}",sha256_hex(format!("openclank.document-upsert.receipt.v1\n{key}").as_bytes()));
        let receipt=json!({"version":1,"state":"committed","operation_id":operation,"request_sha256":request_sha,"account_id":"account","captured_owner":"alice","source_id":"src_3fbc489d0abc741befd73d6a421cf216","source_revision":1,"document_id":"doc_73449179a945f69f249953f7db1252b2","generation_id":"gen_fts_test","publication_id":"pub_test"});
        let raw=super::super::document_upsert::sorted_json(&receipt,false).unwrap();let digest=sha256_hex(raw.as_bytes());
        conn.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,contract_version,content_hash,created_at,operation_metadata_json) VALUES('alice',?1,'user','alice','s01.document-upsert.v1',?2,?3,'now',?4)",params![receipt_id,crate::ontology::CONTRACT_VERSION,digest,raw]).unwrap();
        conn.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES('alice',?1,?2,'s01_document_upsert',?3,1,'now')",params![format!("outbox_{receipt_id}_s01_document_upsert"),receipt_id,raw]).unwrap();
        let database:String=conn.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).unwrap();
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).unwrap();let mut budget=ResetFenceBudget::install(&tx);
        let view=owner_custody::verified_or_direct_custody_view(&tx,&database,"account","alice","alice",&mut budget).unwrap();
        let snapshot=canonical_snapshot(&tx,"alice","account",&view,&mut budget).unwrap();
        assert_eq!(snapshot.source_watermark,"f1899ec6bc840ab17f700ccae5ff862c5407d3a41c949e797aa1eb0a80c9e427");
        let page_request=DocumentVectorSnapshotRequest{version:1,owner:"alice".into(),account_id:"account".into(),after:None,expected_watermark:None,limit:1};
        let page=snapshot_page(&tx,&page_request,&view,&mut budget).unwrap();
        assert_eq!(page["total"],1);assert_eq!(page["items"][0]["source_uri"],"café");assert!(page["next_cursor"].is_null());
        let stale=DocumentVectorSnapshotRequest{expected_watermark:Some("d".repeat(64)),..page_request};
        assert!(snapshot_page(&tx,&stale,&view,&mut budget).is_err());
        assert_eq!(snapshot.rows,vec![VectorCanonicalRow{source_id:"src_3fbc489d0abc741befd73d6a421cf216".into(),source_uri:"café".into(),source_revision:1,document_id:"doc_73449179a945f69f249953f7db1252b2".into(),chunk_id:"chunk_7ad8fe10f001b5d7699ddc41b4fdcaff".into(),content_hash:b.clone()}]);
        let row=VectorInputRow{chunk_id:"chunk_7ad8fe10f001b5d7699ddc41b4fdcaff".into(),content_hash:b,source_revision:1,values:vec![1.0,0.0]};
        let first=prepare_generation("alice","account",&snapshot,"route","model","host",2,vec![row.clone()]).unwrap();
        let repeat=prepare_generation("alice","account",&snapshot,"route","model","host",2,vec![row.clone()]).unwrap();
        assert_eq!(first.generation_id,repeat.generation_id);assert_eq!(first.flat_checksum,repeat.flat_checksum);
        let foreign=prepare_generation("alice","other-account",&snapshot,"route","model","host",2,vec![row.clone()]).unwrap();
        assert_ne!(first.generation_id,foreign.generation_id);
        let changed=prepare_generation("alice","account",&snapshot,"route","model","host",2,vec![VectorInputRow{values:vec![0.0,1.0],..row.clone()}]).unwrap();
        assert_ne!(first.generation_id,changed.generation_id);assert_ne!(first.flat_checksum,changed.flat_checksum);
        assert!(prepare_generation("alice","account",&snapshot,"route","model","host",2,vec![VectorInputRow{source_revision:2,..row.clone()}]).is_err());
        assert!(publish_precomputed(&tx,"alice","account","route","model","host",2,"stale",None,vec![row.clone()],&view,&mut budget).is_err());
        let published=publish_precomputed(&tx,"alice","account","route","model","host",2,&snapshot.source_watermark,None,vec![row.clone()],&view,&mut budget).unwrap();
        assert_eq!(published["generation_id"],first.generation_id);
        let blob:Vec<u8>=tx.query_row("SELECT embedding FROM fm_v2_chunk_embeddings WHERE owner_id='alice'",[],|r|r.get(0)).unwrap();
        assert_eq!(blob,[1.0f32.to_le_bytes(),0.0f32.to_le_bytes()].concat());
        let current=ExpectedVectorPointer{generation_id:first.generation_id.clone(),publication_id:published["publication_id"].as_str().unwrap().into(),publication_watermark:snapshot.source_watermark.clone()};
        let found=search_precomputed(&tx,"alice","account",&current,"route","model","host",2,vec![1.0,0.0],1,&view,&mut budget).unwrap();
        assert_eq!(found["hits"][0]["chunk_id"],snapshot.rows[0].chunk_id);assert_eq!(found["hits"][0]["score"],1.0);
        let profile_request=VectorProfileBuildRequest{version:1,owner:"alice".into(),account_id:"account".into(),expected_pointer:current.clone(),choice:VectorProfileChoice::Scalar8{transform:VectorProfileTransform::Identity}};
        let profile=build_profile(profile_source(&tx,&profile_request,&view,&mut budget).unwrap()).unwrap();
        let installed=commit_profile(&tx,&profile_request,&profile,&view,&mut budget).unwrap();assert_eq!(installed["family"],"scalar8");
        assert_eq!(commit_profile(&tx,&profile_request,&profile,&view,&mut budget).unwrap(),installed);
        let id=installed["profile_id"].as_str().unwrap();
        let selected=search_profile(&tx,&profile_request,id,vec![1.0,0.0],1,1,1,1,None,&view,&mut budget).unwrap();assert_eq!(selected["backend"],"scalar8");assert_eq!(selected["hits"][0]["score"],1.0);
        let restricted=search_profile(&tx,&profile_request,id,vec![1.0,0.0],1,1,1,1,Some(vec![snapshot.rows[0].chunk_id.clone()]),&view,&mut budget).unwrap();assert_eq!(restricted["backend"],"exact_fallback");
        let missing=VectorProfileBuildRequest{choice:VectorProfileChoice::Hnsw{seed:1,m:2,ef_construction:2},..profile_request.clone()};
        let missing_id=profile_source(&tx,&missing,&view,&mut budget).unwrap().profile_id;let fallback=search_profile(&tx,&missing,&missing_id,vec![1.0,0.0],1,1,1,1,None,&view,&mut budget).unwrap();assert_eq!(fallback["backend"],"exact_fallback");
        tx.execute("UPDATE fm_v2_vector_profile_chunks SET bytes=x'00' WHERE owner_id='alice'",[]).unwrap();
        assert!(search_profile(&tx,&profile_request,id,vec![1.0,0.0],1,1,1,1,None,&view,&mut budget).is_err());
        let stale_profile=VectorProfileBuildRequest{expected_pointer:ExpectedVectorPointer{publication_id:"wrong".into(),..current.clone()},..profile_request.clone()};
        assert!(commit_profile(&tx,&stale_profile,&profile,&view,&mut budget).is_err());
        tx.execute("UPDATE fm_v2_chunk_embeddings SET content_hash=content_hash WHERE owner_id='alice' AND generation_id=?1",[&current.generation_id]).unwrap();
        assert_eq!(tx.query_row("SELECT count(*) FROM fm_v2_vector_profiles",[],|v|v.get::<_,i64>(0)).unwrap(),0);
        assert_eq!(tx.query_row("SELECT count(*) FROM fm_v2_vector_profile_chunks",[],|v|v.get::<_,i64>(0)).unwrap(),0);

        assert!(search_precomputed(&tx,"alice","account",&current,"route","other-model","host",2,vec![1.0,0.0],1,&view,&mut budget).is_err());
        assert!(publish_precomputed(&tx,"alice","account","route","model","host",2,&snapshot.source_watermark,None,vec![VectorInputRow{values:vec![0.0,1.0],..row}],&view,&mut budget).is_err());
        let pointer:String=tx.query_row("SELECT generation_id FROM fm_v2_index_pointers WHERE owner_id='alice' AND logical_space='documents_vector'",[],|r|r.get(0)).unwrap();
        assert_eq!(pointer,first.generation_id);
        let stale=ExpectedVectorPointer{generation_id:pointer.clone(),publication_id:"old-publication".into(),publication_watermark:snapshot.source_watermark.clone()};
        assert!(publish_precomputed(&tx,"alice","account","route","model","host",2,&snapshot.source_watermark,Some(&stale),vec![VectorInputRow{chunk_id:snapshot.rows[0].chunk_id.clone(),content_hash:snapshot.rows[0].content_hash.clone(),source_revision:1,values:vec![0.0,1.0]}],&view,&mut budget).is_err());
        tx.execute("UPDATE fm_v2_sources SET source_type='external_reference'",[]).unwrap();
        assert!(canonical_snapshot(&tx,"alice","account",&view,&mut budget).is_err());
        assert!(search_precomputed(&tx,"alice","account",&current,"route","model","host",2,vec![1.0,0.0],1,&view,&mut budget).is_err());
    }
}

// Persist the index layout in the existing schema family column. The immutable
// descriptor and its checksum distinguish the trainer within the IVF layout.
impl VectorProfileSource {
    fn storage_family(&self) -> &'static str {
        match self.choice {
            VectorProfileChoice::IvfMinibatch { .. } => "ivf_flat",
            _ => self.family,
        }
    }
}

