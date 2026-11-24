//! Private, content-free view of an already classified selective-ingest
//! partition. This is a projection, not a selection or mutation authority.
use super::*;
use std::collections::{BTreeMap, BTreeSet};

fn unavailable() -> RepositoryError { RepositoryError::NotFound }

fn exact_keys(value:&Value, keys:&[&str]) -> bool {
    value.as_object().is_some_and(|o| o.len()==keys.len() && keys.iter().all(|k|o.contains_key(*k)))
}
fn string(value:&Value) -> bool { value.as_str().is_some() }
fn string_fields(value:&Value, fields:&[&str]) -> bool {fields.iter().all(|key|string(&value[*key]))}
fn hash_fields(value:&Value, fields:&[&str]) -> bool {fields.iter().all(|key|value[*key].as_str().is_some_and(valid_sha256))}
fn hash_array(value:&Value) -> bool {value.as_array().is_some_and(|values|values.iter().all(|v|v.as_str().is_some_and(valid_sha256)))}

/// `imports` is the separately owned, already projected v2 import object.
/// Enforce its closed shape before preserving the Value without alteration.
fn checked_imports(imports:&Value, owner:&str, account:&str) -> Result<(),RepositoryError> {
    let keys=["version","owner","account_id","retained_job_selectors","retained_content_job_selectors","erasable_job_selectors","job_bindings","retained_staging","erasable_staging","approved_refs","snapshot_claims","retained_candidate_selectors","erasable_candidate_selectors","candidate_bindings","immutable_event_job_selectors","digest"];
    if !exact_keys(imports,&keys) || imports["version"]!=2 || imports["owner"]!=owner || imports["account_id"]!=account
        || !hash_fields(imports,&["digest"]) || !["retained_job_selectors","retained_content_job_selectors","erasable_job_selectors","retained_candidate_selectors","erasable_candidate_selectors","immutable_event_job_selectors"].iter().all(|k|hash_array(&imports[*k])) {return Err(unavailable());}
    let retained=imports["retained_job_selectors"].as_array().ok_or_else(unavailable)?;
    let content=imports["retained_content_job_selectors"].as_array().ok_or_else(unavailable)?;
    if content.windows(2).any(|p|p[0].as_str()>=p[1].as_str()) || content.iter().any(|item|!retained.contains(item)) {return Err(unavailable());}
    let kept=imports["retained_candidate_selectors"].as_array().ok_or_else(unavailable)?;
    let erased=imports["erasable_candidate_selectors"].as_array().ok_or_else(unavailable)?;
    if kept.windows(2).any(|p|p[0].as_str()>=p[1].as_str()) || erased.windows(2).any(|p|p[0].as_str()>=p[1].as_str()) || kept.iter().any(|v|erased.contains(v)) {return Err(unavailable());}
    let candidates=imports["candidate_bindings"].as_array().ok_or_else(unavailable)?;
    if candidates.len()!=kept.len()+erased.len() {return Err(unavailable());}
    let mut previous=None;
    for v in candidates {
        if !exact_keys(v,&["candidate_selector","job_selector","state","proof_sha256"]) || !hash_fields(v,&["candidate_selector","job_selector","proof_sha256"]) || !matches!(v["state"].as_str(),Some("pending"|"rejected"|"accepted")) {return Err(unavailable());}
        let selector=v["candidate_selector"].as_str().ok_or_else(unavailable)?;
        if previous.is_some_and(|p:&str|p>=selector) || !(kept.contains(&v["candidate_selector"]) || erased.contains(&v["candidate_selector"])) || (erased.contains(&v["candidate_selector"]) && v["state"]=="accepted") {return Err(unavailable());}
        if !(imports["retained_job_selectors"].as_array().ok_or_else(unavailable)?.contains(&v["job_selector"]) || imports["erasable_job_selectors"].as_array().ok_or_else(unavailable)?.contains(&v["job_selector"])) {return Err(unavailable());}
        previous=Some(selector);
    }
    let bindings=imports["job_bindings"].as_array().ok_or_else(unavailable)?;
    for v in bindings {
        if !exact_keys(v,&["job_selector","workspace_id","kind","idempotency_sha256","row_sha256","relation_sha256"])
            || !string_fields(v,&["workspace_id","kind"]) || !hash_fields(v,&["job_selector","idempotency_sha256","row_sha256","relation_sha256"]) {return Err(unavailable());}
    }
    for field in ["retained_staging","erasable_staging"] {
        for v in imports[field].as_array().ok_or_else(unavailable)? {
            if !exact_keys(v,&["workspace_id","batch_id","staged_item_id","source_id_sha256","content_sha256","byte_size","job_selectors"])
                || !string_fields(v,&["workspace_id","batch_id","staged_item_id"]) || !hash_fields(v,&["source_id_sha256","content_sha256"])
                || v["byte_size"].as_u64().is_none() || !hash_array(&v["job_selectors"]) {return Err(unavailable());}
        }
    }
    for v in imports["approved_refs"].as_array().ok_or_else(unavailable)? {
        if !exact_keys(v,&["workspace_id","job_selector","suggestion_sha256","publication_sha256","publication_kind","review_key_hash","receipt_id_sha256","receipt_sha256"])
            || !string_fields(v,&["workspace_id","publication_kind"]) || !hash_fields(v,&["job_selector","suggestion_sha256","publication_sha256","review_key_hash","receipt_id_sha256","receipt_sha256"]) {return Err(unavailable());}
    }
    for v in imports["snapshot_claims"].as_array().ok_or_else(unavailable)? {
        if !exact_keys(v,&["workspace_id","job_selector","item_id","account","upload_id","scope"])
            || !string_fields(v,&["workspace_id","item_id","account","upload_id","scope"]) || !hash_fields(v,&["job_selector"]) {return Err(unavailable());}
    }
    Ok(())
}

struct Selectors<'a> {
    owner:&'a str,
    account:&'a str,
    // Detect collisions across all kinds while permitting the same ID in
    // multiple fields of its own kind for cross-field comparison.
    seen:BTreeMap<String,(String,String)>,
}
impl Selectors<'_> {
    fn one(&mut self,kind:&str,id:&str)->Result<String,RepositoryError> {
        if id.is_empty() {return Err(unavailable());}
        let value=sha256_hex(&serde_json::to_vec(&("s04.ingest-selector.v2",self.owner,self.account,kind,id))?);
        if self.seen.get(&value).is_some_and(|(old_kind,old_id)|old_kind!=kind || old_id!=id) {return Err(unavailable());}
        self.seen.insert(value.clone(),(kind.to_owned(),id.to_owned()));
        Ok(value)
    }
    fn list(&mut self,kind:&str,values:&[String])->Result<Vec<String>,RepositoryError> {
        let mut result=BTreeSet::new();
        for value in values {if !result.insert(self.one(kind,value)?) {return Err(unavailable());}}
        Ok(result.into_iter().collect())
    }
    fn s08(&mut self,values:&[(String,String,String)])->Result<Vec<Value>,RepositoryError> {
        let mut tuples=BTreeSet::new();
        for (job,candidate,change) in values {
            let entry=(self.one("job",job)?,self.one("candidate",candidate)?,self.one("change",change)?);
            if !tuples.insert(entry) {return Err(unavailable());}
        }
        Ok(tuples.into_iter().map(|(job,candidate,change)|json!({"job_selector":job,"candidate_selector":candidate,"change_selector":change})).collect())
    }
}

pub(crate) fn selective_ingest_private_view(owner:&str,account:&str,partition:&S05IngestPartition)->Result<Value,RepositoryError> {
    if owner.is_empty() || owner.trim()!=owner || account.is_empty() || account.trim()!=account
        || partition.version!=1 || partition.scope!="selective_ingest" || !valid_sha256(&partition.binding_sha256) {return Err(unavailable());}
    checked_imports(&partition.imports,owner,account)?;
    let mut hash=Selectors{owner,account,seen:BTreeMap::new()};
    let selectors=json!({
        "retained_jobs":hash.list("job",&partition.retained_jobs)?,
        "erasable_jobs":hash.list("job",&partition.erasable_jobs)?,
        "terminal_erased_jobs":hash.list("job",&partition.terminal_erased_jobs)?,
        "retained_candidates":hash.list("candidate",&partition.retained_candidates)?,
        "erasable_candidates":hash.list("candidate",&partition.erasable_candidates)?,
        "generic_retained_candidates":hash.list("candidate",&partition.generic_retained_candidates)?,
        "generic_erasable_candidates":hash.list("candidate",&partition.generic_erasable_candidates)?,
        "s08_retained_candidates":hash.list("candidate",&partition.s08_retained_candidates)?,
        "s08_erasable":hash.s08(&partition.s08_erasable)?,
        "retained_sources":hash.list("source",&partition.retained_sources)?,
        "erasable_sources":hash.list("source",&partition.erasable_sources)?,
        "retained_raw":hash.list("raw",&partition.retained_raw_ids)?,
        "erasable_raw":hash.list("raw",&partition.erasable_raw_ids)?,
        "retained_recovery":hash.list("recovery",&partition.retained_recovery_ids)?,
        "erasable_recovery":hash.list("recovery",&partition.erasable_recovery_ids)?,
        "retained_quarantine":hash.list("quarantine",&partition.retained_quarantine_ids)?,
        "erasable_quarantine":hash.list("quarantine",&partition.erasable_quarantine_ids)?,
    });
    Ok(json!({"version":2,"scope":"selective_ingest","all_s05_sources_retained":partition.all_s05_sources_retained,
        "binding_sha256":partition.binding_sha256,"imports":partition.imports,"selectors":selectors}))
}
