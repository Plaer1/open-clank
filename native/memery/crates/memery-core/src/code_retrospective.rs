//! Private, bounded current-parser reconstruction of one retained file.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use crate::code_evidence::{extract_evidence, ExtractionBudget, IdentityState};
use crate::code_query::QueryReceipt;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub version: u8,
    pub receipt: QueryReceipt,
    pub file_id: String,
    pub rel_path: String,
    pub source_sha256: String,
    pub source_utf8: String,
}
#[derive(Serialize)]
struct Declaration { id: String, identity_state: &'static str }
#[derive(Serialize)]
pub struct ResultValue {
    version: u8, receipt: QueryReceipt, file_id: String, source_sha256: String,
    extractor_fingerprint: String, complete: bool, declarations: Vec<Declaration>,
}

pub fn extract(request: Request) -> Result<ResultValue, ()> {
    if request.version != 1 || request.source_utf8.len() > 32768 || request.source_sha256.len()!=64 ||
        !request.source_sha256.bytes().all(|c|c.is_ascii_digit() || (b'a'..=b'f').contains(&c)) { return Err(()); }
    let bytes=request.source_utf8.as_bytes();
    if bytes.is_empty() || bytes.len()>32768 || bytes.contains(&0) ||
        format!("{:x}",Sha256::digest(bytes)) != request.source_sha256 ||
        request.receipt.evidence.config_fingerprint != crate::code::producer_fingerprint() { return Err(()); }
    let checkout=&request.receipt.evidence.checkout_id;
    if request.file_id != format!("file:{}",crate::code_query::file_id(checkout,&request.rel_path)) { return Err(()); }
    let budget=ExtractionBudget { max_source_bytes:32768, max_declarations:201, max_serialized_bytes:262144, ..Default::default() };
    let file=extract_evidence(checkout,&request.rel_path,bytes,&budget).map_err(|_|())?;
    if !matches!(file.language.as_str(),"rust"|"python"|"typescript"|"javascript") || !file.capability.available ||
        file.producer_fingerprint!=crate::code::producer_fingerprint() || file.declarations.is_empty() || file.declarations.len()>200 ||
        file.diagnostics.iter().any(|d|!matches!(d.code.as_str(),"conditional_import"|"unsupported_sql_code"|"unsupported_callback_registration")) { return Err(()); }
    let mut ids=BTreeSet::new();let mut declarations=Vec::with_capacity(file.declarations.len());
    for item in file.declarations {
        if item.identity_state!=IdentityState::Stable || !ids.insert(item.id.clone()) { return Err(()); }
        declarations.push(Declaration{id:format!("decl:{}",item.id),identity_state:"stable"});
    }
    Ok(ResultValue {version:1,receipt:request.receipt,file_id:request.file_id,source_sha256:request.source_sha256,
        extractor_fingerprint:file.producer_fingerprint,complete:true,declarations})
}
