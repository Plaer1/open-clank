//! Guarded personal document publication and exact source receipts.
#[path = "document_segments.rs"]
mod document_segments;

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{sha256_hex, RepositoryError};

const MAX_TEXT: usize = 256 * 1024;
const MAX_CHUNKS: usize = 512;
const MAX_LOCATOR: usize = 8 * 1024;
const REQUEST_PREFIX: &[u8] = b"openclank.document-upsert.request.v1\n";

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentUpsertRequest {
    pub version: u8,
    pub operation_id: String,
    pub account_id: String,
    pub owner: String,
    pub captured_owner: String,
    pub workspace_id: String,
    pub project_id: String,
    pub source_uri: String,
    pub expected_head: DocumentExpectedHead,
    pub raw: DocumentRawCommitment,
    pub extracted_text: String,
    pub extracted_sha256: String,
    pub extractor_version: String,
    pub chunker_version: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub chunks: Vec<DocumentChunk>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub page_ends: Vec<usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum DocumentExpectedHead {
    Absent,
    Revision { source_id: String, revision: u64, content_sha256: String, forget_state: String },
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentRawCommitment { pub byte_length: u64, pub sha256: String }

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentChunk {
    pub ordinal: u64,
    pub text: String,
    pub text_sha256: String,
    pub locator_json: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentOriginalRequest {
    pub version: u8,
    pub operation_id: String,
    pub request_sha256: String,
    pub account_id: String,
    pub current_owner: String,
    pub captured_owner: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Locator {
    #[serde(rename = "type")]
    kind: String,
    start: usize,
    end: usize,
    line_start: usize,
    line_end: usize,
    headings: Vec<String>,
    block_type: String,
}

fn unavailable() -> RepositoryError {
    RepositoryError::Validation("document upsert unavailable".into())
}
fn valid_text(value: &str, empty: bool) -> bool {
    (empty || !value.is_empty()) && value.len() <= 1024 && value.trim() == value && !value.contains('\0')
}
fn valid_hash(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Python str.isspace() for the Unicode version used by the source chunker.
fn python_space(ch: char) -> bool {
    matches!(ch as u32, 0x09..=0x0d | 0x1c..=0x20 | 0x85 | 0xa0 | 0x1680 |
        0x2000..=0x200a | 0x2028 | 0x2029 | 0x202f | 0x205f | 0x3000)
}

/// Pure, Host-delegated projection of selected canonical source text.
pub(crate) fn structural_chunks(text: &str, page_ends: &[usize]) -> Result<Vec<DocumentChunk>, RepositoryError> {
    document_segments::chunks(text, page_ends)
}

pub(crate) fn structural_mentions(text: &str) -> Value {
    document_segments::mentions(text)
}

pub fn document_code_mentions(texts: &[String]) -> Result<Value, RepositoryError> {
    if texts.is_empty() || texts.len() > 16 || texts.iter().map(String::len).sum::<usize>() > MAX_TEXT {
        return Err(unavailable());
    }
    let documents: Vec<Value> = texts.iter().map(|text| structural_mentions(text)).collect();
    Ok(serde_json::json!({"documents":documents}))
}

impl DocumentUpsertRequest {
    /// Validates source-independent facts. Raw file ownership and currentness
    /// remain the responsibility of the existing Host issue guard.
    pub(crate) fn validate(&self) -> Result<(), RepositoryError> {
        if !matches!(self.version, 1 | 2) || !valid_text(&self.operation_id, false)
            || !valid_text(&self.account_id, false) || !valid_text(&self.owner, false)
            || !valid_text(&self.captured_owner, false) || !valid_text(&self.workspace_id, true)
            || !valid_text(&self.project_id, true) || !valid_text(&self.source_uri, false)
            || (!self.project_id.is_empty() && self.workspace_id.is_empty())
            || !valid_text(&self.extractor_version, false)
            || self.chunker_version != "frankenmemory-rag-v2"
            || self.raw.byte_length > 50 * 1024 * 1024 || !valid_hash(&self.raw.sha256)
            || self.extracted_text.is_empty() || self.extracted_text.len() > MAX_TEXT
            || !valid_hash(&self.extracted_sha256)
            || sha256_hex(self.extracted_text.as_bytes()) != self.extracted_sha256
            || (self.version == 1 && (self.chunks.is_empty() || self.chunks.len() > MAX_CHUNKS || !self.page_ends.is_empty()))
            || (self.version == 2 && (!self.chunks.is_empty() || self.page_ends.len() > 1024
                || (self.extractor_version == "pypdf-text.v1") == self.page_ends.is_empty())) {
            return Err(unavailable());
        }
        if let DocumentExpectedHead::Revision { source_id, revision, content_sha256, forget_state } = &self.expected_head {
            if !valid_text(source_id,false) || *revision == 0 || *revision > i64::MAX as u64 || !valid_hash(content_sha256) || !matches!(forget_state.as_str(), "active" | "forgotten") { return Err(unavailable()); }
        }
        if self.version == 2 { return Ok(()); }
        let chars: Vec<char> = self.extracted_text.chars().collect();
        let mut start = 0;
        let mut ordinal = 0;
        while start < chars.len() {
            let end = (start + 1200).min(chars.len());
            if !chars[start..end].iter().all(|c| python_space(*c)) {
                let chunk = self.chunks.get(ordinal).ok_or_else(unavailable)?;
                let text: String = chars[start..end].iter().collect();
                if chunk.ordinal != ordinal as u64 || chunk.text != text || chunk.text.len() > 4800
                    || !valid_hash(&chunk.text_sha256) || sha256_hex(chunk.text.as_bytes()) != chunk.text_sha256
                    || chunk.locator_json.len() > MAX_LOCATOR { return Err(unavailable()); }
                let locator: Locator = serde_json::from_str(&chunk.locator_json).map_err(|_| unavailable())?;
                if locator.kind != "text" || locator.start != start || locator.end != end
                    || locator.line_start == 0 || locator.line_end < locator.line_start
                    || locator.headings.len() > 512 || locator.headings.iter().any(|h| h.len() > MAX_LOCATOR)
                    || !matches!(locator.block_type.as_str(), "text" | "table") { return Err(unavailable()); }
                let headings = sorted_json(&serde_json::to_value(&locator.headings)?, true)?;
                let canonical = format!("{{\"type\":\"text\",\"start\":{},\"end\":{},\"line_start\":{},\"line_end\":{},\"headings\":{},\"block_type\":\"{}\"}}", locator.start, locator.end, locator.line_start, locator.line_end, headings, locator.block_type);
                if canonical != chunk.locator_json { return Err(unavailable()); }
                ordinal += 1;
            }
            if end == chars.len() { break; }
            start = (end - 120).max(start + 1);
        }
        if ordinal != self.chunks.len() { return Err(unavailable()); }
        let encoded = sorted_json(&serde_json::to_value(self)?, false)?;
        if encoded.len() > 4 * 1024 * 1024 { return Err(unavailable()); }
        Ok(())
    }
    pub(crate) fn request_sha256(&self) -> Result<String, RepositoryError> {
        self.validate()?;
        let value = serde_json::to_value(self)?;
        let mut bytes = REQUEST_PREFIX.to_vec();
        bytes.extend_from_slice(sorted_json(&value, false)?.as_bytes());
        Ok(sha256_hex(&bytes))
    }
}

impl DocumentOriginalRequest {
    pub(crate) fn validate(&self) -> Result<(), RepositoryError> {
        if self.version != 1 || !valid_text(&self.operation_id, false) || !valid_hash(&self.request_sha256)
            || !valid_text(&self.account_id, false) || !valid_text(&self.current_owner, false)
            || !valid_text(&self.captured_owner, false) { return Err(unavailable()); }
        Ok(())
    }
}

/// Compact Python-compatible JSON with recursively sorted object keys. The
/// ASCII form is for existing Python IDs/watermarks, not the request digest.
pub(crate) fn sorted_json(value: &Value, ascii: bool) -> Result<String, RepositoryError> {
    fn string(value: &str, ascii: bool, out: &mut String) {
        use std::fmt::Write;
        out.push('"');
        for ch in value.chars() {
            match ch {
                '"' => out.push_str("\\\""), '\\' => out.push_str("\\\\"),
                '\u{08}' => out.push_str("\\b"), '\u{0c}' => out.push_str("\\f"),
                '\n' => out.push_str("\\n"), '\r' => out.push_str("\\r"), '\t' => out.push_str("\\t"),
                c if (c as u32) < 0x20 || (ascii && (c as u32) >= 0x7f) => {
                    let mut units = [0u16; 2];
                    for unit in c.encode_utf16(&mut units).iter() { write!(out, "\\u{unit:04x}").unwrap(); }
                }
                c => out.push(c),
            }
        }
        out.push('"');
    }
    fn emit(value: &Value, ascii: bool, out: &mut String) -> Result<(), RepositoryError> {
        match value {
            Value::Null => out.push_str("null"),
            Value::Bool(v) => out.push_str(if *v { "true" } else { "false" }),
            Value::Number(n) if n.is_i64() || n.is_u64() => out.push_str(&n.to_string()),
            Value::Number(_) => return Err(unavailable()),
            Value::String(v) => string(v, ascii, out),
            Value::Array(items) => { out.push('['); for (i, item) in items.iter().enumerate() { if i > 0 { out.push(','); } emit(item, ascii, out)?; } out.push(']'); },
            Value::Object(items) => {
                out.push('{'); let mut keys: Vec<_> = items.keys().collect(); keys.sort();
                for (i, key) in keys.iter().enumerate() { if i > 0 { out.push(','); } string(key, ascii, out); out.push(':'); emit(&items[*key], ascii, out)?; }
                out.push('}');
            }
        }
        Ok(())
    }
    let mut out = String::new(); emit(value, ascii, &mut out)?; Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn python_encoding_vectors() {
        let value = json!({"z":[1,{"kind":"revision","revision":7}],"a":"é😀\n\u{001c}"});
        assert_eq!(sorted_json(&value, true).unwrap(), "{\"a\":\"\\u00e9\\ud83d\\ude00\\n\\u001c\",\"z\":[1,{\"kind\":\"revision\",\"revision\":7}]}");
        let mut bytes = REQUEST_PREFIX.to_vec(); bytes.extend_from_slice(sorted_json(&value, false).unwrap().as_bytes());
        assert_eq!(sha256_hex(&bytes), "6f1d3bed7c2ee3cc68109acaae59d725614e804c6e46bdc6b0ec45e8385ecb1a");
        assert!(python_space('\u{001c}')); assert!(!python_space('\u{200b}'));
    }
}

fn receipt_key(account: &str, operation: &str) -> Result<String, RepositoryError> {
    let value = serde_json::json!([account, operation]);
    let mut bytes = b"openclank.document-upsert.receipt.v1\n".to_vec();
    bytes.extend_from_slice(sorted_json(&value, false)?.as_bytes());
    Ok(format!("cs_s01_document_{}", sha256_hex(&bytes)))
}

/// Exact factual receipt only. Absence is unknown, never permission to reissue.
pub(crate) fn original(
    tx: &rusqlite::Transaction<'_>, request: &DocumentOriginalRequest,
    view: &super::owner_custody::VerifiedCustodyView<'_, '_>,
    budget: &mut super::ResetFenceBudget,
) -> Result<Value, RepositoryError> {
    use rusqlite::{params, OptionalExtension};
    request.validate()?;
    let captured = view.for_captured(tx, &request.captured_owner)?;
    if captured.account_id() != request.account_id || captured.current_owner() != request.current_owner { return Err(unavailable()); }
    let id = receipt_key(&request.account_id, &request.operation_id)?;
    let event = format!("outbox_{id}_s01_document_upsert");
    let lengths: Option<(i64,i64)> = tx.query_row(
        "SELECT length(CAST(c.operation_metadata_json AS BLOB)),length(CAST(o.payload_json AS BLOB)) FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 AND o.kind='s01_document_upsert' WHERE c.owner_id=?1 AND c.change_set_id=?2",
        params![request.current_owner,id,event], |r| Ok((r.get(0)?,r.get(1)?))).optional()?;
    let Some((a,b)) = lengths else { return Ok(serde_json::json!({"version":1,"state":"unknown"})); };
    if !(1..=8192).contains(&a) || !(1..=8192).contains(&b) { return Err(unavailable()); }
    budget.check((a+b) as usize).map_err(RepositoryError::Validation)?;
    let (hash, raw, payload, revision, actor): (String,String,String,i64,String) = tx.query_row(
        "SELECT c.content_hash,c.operation_metadata_json,o.payload_json,o.source_revision,c.actor_id FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 AND o.kind='s01_document_upsert' WHERE c.owner_id=?1 AND c.change_set_id=?2 AND c.reason='s01.document-upsert.v1' AND c.actor_type='user' AND c.source_event_id IS NULL AND c.contract_version=?4",
        params![request.current_owner,id,event,crate::ontology::CONTRACT_VERSION],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?;
    view.for_captured(tx, &actor)?;
    if actor != request.captured_owner || raw != payload || hash != sha256_hex(raw.as_bytes()) { return Err(unavailable()); }
    let receipt: Value = serde_json::from_str(&raw).map_err(|_| unavailable())?;
    if sorted_json(&receipt,false)? != raw { return Err(unavailable()); }
    let obj = receipt.as_object().ok_or_else(unavailable)?;
    let keys = ["version","state","operation_id","request_sha256","account_id","captured_owner","source_id","source_revision","document_id","generation_id","publication_id"];
    let has_count = obj.contains_key("chunk_count");
    if obj.len()!=keys.len()+usize::from(has_count) || keys.iter().any(|key| !obj.contains_key(*key))
        || (has_count && receipt["chunk_count"].as_u64().is_none_or(|n| !(1..=MAX_CHUNKS as u64).contains(&n)))
        || receipt["version"] != 1 || receipt["state"] != "committed"
        || receipt["operation_id"] != request.operation_id || receipt["request_sha256"] != request.request_sha256
        || receipt["account_id"] != request.account_id || receipt["captured_owner"] != request.captured_owner
        || receipt["source_revision"].as_i64()!=Some(revision) || revision<=0
        || ["source_id","document_id","generation_id","publication_id"].iter().any(|k| receipt[*k].as_str().is_none_or(|s| !valid_text(s,false))) { return Err(unavailable()); }
    let mut result = obj.clone(); result.remove("account_id"); result.remove("captured_owner");
    Ok(Value::Object(result))
}

fn scoped_generation(tx: &rusqlite::Transaction<'_>, owner: &str, budget: &mut super::ResetFenceBudget) -> Result<(String,String), RepositoryError> {
    use rusqlite::{params, OptionalExtension};
    let mut statement = tx.prepare("SELECT CASE WHEN length(CAST(s.source_id AS BLOB))<=1024 THEN s.source_id END,s.source_revision,CASE WHEN length(CAST(s.source_uri AS BLOB))<=1024 THEN s.source_uri END,CASE WHEN length(CAST(s.content_hash AS BLOB))<=1024 THEN s.content_hash END,CASE WHEN length(CAST(s.forget_state AS BLOB))<=1024 THEN s.forget_state END,CASE WHEN length(CAST(COALESCE(d.document_id,'') AS BLOB))<=1024 THEN COALESCE(d.document_id,'') END,CASE WHEN length(CAST(COALESCE(c.chunk_id,'') AS BLOB))<=1024 THEN COALESCE(c.chunk_id,'') END,CASE WHEN length(CAST(COALESCE(c.content_hash,'') AS BLOB))<=1024 THEN COALESCE(c.content_hash,'') END FROM fm_v2_sources s LEFT JOIN fm_v2_documents d ON d.owner_id=s.owner_id AND d.source_id=s.source_id AND d.source_revision=s.source_revision AND d.parser_version='frankenmemory-rag-v2' LEFT JOIN fm_v2_chunks c ON c.owner_id=d.owner_id AND c.document_id=d.document_id WHERE s.owner_id=?1 AND s.workspace_key='' AND s.project_key='' AND NOT EXISTS (SELECT 1 FROM fm_v2_sources newer WHERE newer.owner_id=s.owner_id AND newer.source_id=s.source_id AND newer.source_revision>s.source_revision) ORDER BY s.source_id,s.source_revision,d.document_id,c.ordinal,c.chunk_id")?;
    let mut rows=statement.query([owner])?; let mut manifest=Vec::new();
    while let Some(row)=rows.next()? {
        let id:Option<String>=row.get(0)?; let revision:i64=row.get(1)?; let uri:Option<String>=row.get(2)?; let hash:Option<String>=row.get(3)?; let state:Option<String>=row.get(4)?; let doc:Option<String>=row.get(5)?; let chunk:Option<String>=row.get(6)?; let chunk_hash:Option<String>=row.get(7)?;
        let (id,uri,hash,state,doc,chunk,chunk_hash)=match (id,uri,hash,state,doc,chunk,chunk_hash) {(Some(a),Some(b),Some(c),Some(d),Some(e),Some(f),Some(g))=>(a,b,c,d,e,f,g),_=>return Err(unavailable())};
        budget.check(id.len()+uri.len()+hash.len()+state.len()+doc.len()+chunk.len()+chunk_hash.len()+8).map_err(RepositoryError::Validation)?;
        manifest.push(serde_json::json!([id,revision,uri,hash,state,doc,chunk,chunk_hash]));
    }
    drop(rows); drop(statement);
    let watermark=sha256_hex(sorted_json(&Value::Array(manifest),true)?.as_bytes());
    let config=serde_json::json!({"provider_ref":"sqlite-fts5","model":"fts5","endpoint_class":"in_process","dimension":0,"normalization":"bm25","metric":"bm25","chunker_version":"frankenmemory-rag-v2"});
    let fingerprint=sha256_hex(sorted_json(&config,true)?.as_bytes());
    let generation=format!("gen_fts_{}",&sha256_hex(format!("{owner}\0\0\0{watermark}\0{fingerprint}").as_bytes())[..32]);
    // Scan FTS once, using the canonical chunk key for indexed scope lookup.
    // Both streams include historical chunks and reject missing/duplicate/text
    // differences; unrelated valid workspace chunks are excluded from the proof.
    let mut canonical=tx.prepare("SELECT CASE WHEN length(CAST(c.chunk_id AS BLOB))<=1024 THEN c.chunk_id END, CASE WHEN length(CAST(c.text AS BLOB))<=262144 THEN c.text END FROM fm_v2_chunks c JOIN fm_v2_documents d ON d.owner_id=c.owner_id AND d.document_id=c.document_id JOIN fm_v2_sources s ON s.owner_id=d.owner_id AND s.source_id=d.source_id AND s.source_revision=d.source_revision WHERE c.owner_id=?1 AND s.workspace_key='' AND s.project_key='' ORDER BY c.chunk_id")?;
    let mut projected=tx.prepare("SELECT CASE WHEN length(CAST(f.chunk_id AS BLOB))<=1024 THEN f.chunk_id END, CASE WHEN length(CAST(f.text AS BLOB))<=262144 THEN f.text END FROM fm_v2_chunks_fts f CROSS JOIN fm_v2_chunks c CROSS JOIN fm_v2_documents d CROSS JOIN fm_v2_sources s WHERE f.owner_id=?1 AND c.owner_id=f.owner_id AND c.chunk_id=f.chunk_id AND d.owner_id=c.owner_id AND d.document_id=c.document_id AND s.owner_id=d.owner_id AND s.source_id=d.source_id AND s.source_revision=d.source_revision AND s.workspace_key='' AND s.project_key='' ORDER BY f.chunk_id")?;
    let mut canonical_rows=canonical.query([owner])?;
    let mut projected_rows=projected.query([owner])?;
    loop {
        match (canonical_rows.next()?,projected_rows.next()?) {
            (None,None)=>break,
            (Some(a),Some(b))=>{
                let aid:Option<String>=a.get(0)?;let at:Option<String>=a.get(1)?;
                let bid:Option<String>=b.get(0)?;let bt:Option<String>=b.get(1)?;
                let (aid,at,bid,bt)=match (aid,at,bid,bt) {(Some(a),Some(b),Some(c),Some(d))=>(a,b,c,d),_=>return Err(unavailable())};
                budget.check(aid.len()+at.len()+bid.len()+bt.len()).map_err(RepositoryError::Validation)?;
                if aid!=bid || at!=bt {return Err(unavailable());}
            },
            _=>return Err(unavailable()),
        }
    }
    drop(canonical_rows);drop(projected_rows);drop(canonical);drop(projected);
    let extra:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_chunks_fts f WHERE f.owner_id=?1 AND NOT EXISTS(SELECT 1 FROM fm_v2_chunks c WHERE c.owner_id=f.owner_id AND c.chunk_id=f.chunk_id))",[owner],|r|r.get(0))?;
    if extra {return Err(unavailable());}
    let count:i64=tx.query_row("SELECT count(*) FROM fm_v2_chunks c JOIN fm_v2_documents d ON d.owner_id=c.owner_id AND d.document_id=c.document_id JOIN fm_v2_sources s ON s.owner_id=d.owner_id AND s.source_id=d.source_id AND s.source_revision=d.source_revision WHERE c.owner_id=?1 AND s.workspace_key='' AND s.project_key='' AND d.parser_version='frankenmemory-rag-v2' AND s.forget_state='active' AND NOT EXISTS(SELECT 1 FROM fm_v2_sources n WHERE n.owner_id=s.owner_id AND n.source_id=s.source_id AND n.source_revision>s.source_revision)",[owner],|r|r.get(0))?;
    let now=chrono::Utc::now().to_rfc3339();
    let existing:Option<(String,String,i64,String,String,String,String,String,String,i64,String,String,String)>=tx.query_row("SELECT state,retention_state,row_count,config_fingerprint,source_watermark,logical_space,workspace_key,project_key,provider_ref,dimension,model,endpoint_class,chunker_version FROM fm_v2_derived_generations WHERE owner_id=?1 AND generation_id=?2",params![owner,generation],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?,r.get(9)?,r.get(10)?,r.get(11)?,r.get(12)?))).optional()?;
    if let Some((state,retention,rows,config,water,space,workspace,project,provider,dimension,model,endpoint,chunker))=existing {
        if state!="ready" || retention!="retained" || rows!=count || config!=fingerprint || water!=watermark || space!="documents_fts" || !workspace.is_empty() || !project.is_empty() || provider!="sqlite-fts5" || dimension!=0 || model!="fts5" || endpoint!="in_process" || chunker!="frankenmemory-rag-v2" {return Err(unavailable());}
        let metric:(String,String)=tx.query_row("SELECT normalization,metric FROM fm_v2_derived_generations WHERE owner_id=?1 AND generation_id=?2",params![owner,generation],|r|Ok((r.get(0)?,r.get(1)?)))?;
        if metric!=("bm25".into(),"bm25".into()) {return Err(unavailable());}
        let validated:Option<String>=tx.query_row("SELECT validated_at FROM fm_v2_derived_generations WHERE owner_id=?1 AND generation_id=?2",params![owner,generation],|r|r.get(0))?;
        if validated.as_deref().is_none_or(str::is_empty) {return Err(unavailable());}
    } else {
        tx.execute("INSERT INTO fm_v2_derived_generations(owner_id,generation_id,logical_space,workspace_key,project_key,provider_ref,model,endpoint_class,dimension,normalization,metric,chunker_version,config_fingerprint,source_watermark,row_count,state,retention_state,created_at,updated_at) VALUES(?1,?2,'documents_fts','','','sqlite-fts5','fts5','in_process',0,'bm25','bm25','frankenmemory-rag-v2',?3,?4,?5,'planned','retained',?6,?6)",params![owner,generation,fingerprint,watermark,count,now])?;
        for (old,new) in [("planned","building"),("building","validating"),("validating","ready")] {if tx.execute("UPDATE fm_v2_derived_generations SET state=?3,validated_at=CASE WHEN ?3='ready' THEN ?4 ELSE validated_at END WHERE owner_id=?1 AND generation_id=?2 AND state=?5",params![owner,generation,new,now,old])?!=1{return Err(unavailable());}}
    }
    let prior:Option<(String,String,String)>=tx.query_row("SELECT generation_id,publication_watermark,publication_tx FROM fm_v2_index_pointers WHERE owner_id=?1 AND logical_space='documents_fts' AND workspace_key='' AND project_key=''",[owner],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
    if let Some((ref id,ref water,ref publication))=prior { if id==&generation && water==&watermark && !publication.is_empty() {
        let exact:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_index_publications WHERE owner_id=?1 AND publication_id=?2 AND logical_space='documents_fts' AND workspace_key='' AND project_key='' AND generation_id=?3 AND publication_watermark=?4 AND action IN ('publish','rollback'))",params![owner,publication,generation,watermark],|r|r.get(0))?;
        if !exact {return Err(unavailable());} return Ok((generation,publication.clone()));
    } }
    let publication=format!("pub_{}",uuid::Uuid::new_v4().simple());
    tx.execute("INSERT INTO fm_v2_index_pointers(owner_id,logical_space,workspace_key,project_key,generation_id,publication_tx,publication_watermark,published_at) VALUES(?1,'documents_fts','','',?2,?3,?4,?5) ON CONFLICT(owner_id,logical_space,workspace_key,project_key) DO UPDATE SET generation_id=excluded.generation_id,publication_tx=excluded.publication_tx,publication_watermark=excluded.publication_watermark,published_at=excluded.published_at",params![owner,generation,publication,watermark,now])?;
    tx.execute("INSERT INTO fm_v2_index_publications(owner_id,publication_id,logical_space,workspace_key,project_key,previous_generation_id,generation_id,action,publication_watermark,created_at) VALUES(?1,?2,'documents_fts','','',?3,?4,'publish',?5,?6)",params![owner,publication,prior.map(|v|v.0),generation,watermark,now])?;
    Ok((generation,publication))
}

/// Called only by the existing authenticated Host-owned immediate transaction.
pub(crate) fn upsert(
    tx: &rusqlite::Transaction<'_>, request: &DocumentUpsertRequest,
    view: &super::owner_custody::VerifiedCustodyView<'_, '_>,
    budget: &mut super::ResetFenceBudget,
) -> Result<Value, RepositoryError> {
    use rusqlite::{params, OptionalExtension};
    request.validate()?;
    let captured=view.for_captured(tx,&request.captured_owner)?;
    if captured.account_id()!=request.account_id || captured.current_owner()!=request.owner
        || request.owner!=request.captured_owner || !request.workspace_id.is_empty() || !request.project_id.is_empty() { return Err(unavailable()); }
    super::require_guarded_epoch(tx)?;
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.document_fts.v1'",[],|r|r.get(0)).optional()?;
    if marker.as_deref()!=Some("1") {return Err(unavailable());}
    let digest=request.request_sha256()?;
    let selector=DocumentOriginalRequest{version:1,operation_id:request.operation_id.clone(),request_sha256:digest.clone(),account_id:request.account_id.clone(),current_owner:request.owner.clone(),captured_owner:request.captured_owner.clone()};
    let receipt_id=receipt_key(&request.account_id,&request.operation_id)?;
    let event_id=format!("outbox_{receipt_id}_s01_document_upsert");
    let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?3)",params![request.owner,receipt_id,event_id],|r|r.get(0))?;
    if collision {return original(tx,&selector,view,budget);}
    let generated;
    let chunks = if request.version == 2 {
        generated = structural_chunks(&request.extracted_text, &request.page_ends)?;
        &generated
    } else {
        &request.chunks
    };
    let lookup=DocumentSourceLookupRequest{version:1,account_id:request.account_id.clone(),current_owner:request.owner.clone(),captured_owner:request.captured_owner.clone(),source_uri:request.source_uri.clone()};
    let fact=source_lookup(tx,&lookup,view,budget)?;
    let canonical=format!("src_{}",&sha256_hex(format!("{}\0\0\0{}",request.owner,request.source_uri).as_bytes())[..32]);
    let (source_id,head):(String,Option<(i64,String,String)>)=match (&request.expected_head,fact["state"].as_str()) {
        (DocumentExpectedHead::Absent,Some("absent"))=>(canonical.clone(),None),
        (DocumentExpectedHead::Revision{source_id,revision,content_sha256,forget_state},Some("present")) if fact["source_id"].as_str()==Some(source_id.as_str()) && fact["source_revision"].as_u64()==Some(*revision) && fact["content_sha256"].as_str()==Some(content_sha256.as_str()) && fact["forget_state"].as_str()==Some(forget_state.as_str()) => (source_id.clone(),Some((*revision as i64,content_sha256.clone(),forget_state.clone()))),
        _=>return Err(unavailable()),
    };
    if head.as_ref().is_some_and(|(_,_,state)|state=="forgotten") {
        let documents:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_documents WHERE owner_id=?1 AND source_id=?2)",params![request.owner,source_id],|r|r.get(0))?;
        if documents {return Err(unavailable());}
    }
    let mut same=head.as_ref().is_some_and(|(_,hash,state)|state=="active" && hash==&request.extracted_sha256);
    if same {
        let (n,_,_)=head.as_ref().ok_or_else(unavailable)?;
        let mut stmt=tx.prepare("SELECT CASE WHEN length(CAST(document_id AS BLOB))<=1024 THEN document_id END,CASE WHEN length(CAST(parser_version AS BLOB))<=1024 THEN parser_version END,CASE WHEN length(CAST(content_hash AS BLOB))<=1024 THEN content_hash END FROM fm_v2_documents WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3 LIMIT 2")?;
        let rows=stmt.query_map(params![request.owner,source_id,n],|r|Ok((r.get::<_,Option<String>>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,Option<String>>(2)?)))?.collect::<Result<Vec<_>,_>>()?;
        if rows.len()!=1 {return Err(unavailable());}
        let (old_id,parser,hash)=match &rows[0] {(Some(a),Some(b),Some(c))=>(a,b,c),_=>return Err(unavailable())};
        budget.check(old_id.len()+parser.len()+hash.len()).map_err(RepositoryError::Validation)?;
        if parser!="frankenmemory-rag-v2" || hash!=&request.extracted_sha256 {return Err(unavailable());}
        let mut stmt=tx.prepare("SELECT ordinal,CASE WHEN length(CAST(text AS BLOB))<=4800 THEN text END,CASE WHEN length(CAST(content_hash AS BLOB))<=1024 THEN content_hash END,CASE WHEN length(CAST(locator_json AS BLOB))<=8192 THEN locator_json END FROM fm_v2_chunks WHERE owner_id=?1 AND document_id=?2 ORDER BY ordinal LIMIT 513")?;
        let mut rows=stmt.query(params![request.owner,old_id])?;
        let mut index=0usize;
        let mut locator_changed=false;
        while let Some(row)=rows.next()? {
            let ordinal:i64=row.get(0)?;let text:Option<String>=row.get(1)?;let hash:Option<String>=row.get(2)?;let locator:Option<String>=row.get(3)?;
            let (text,hash,locator)=match(text,hash,locator){(Some(a),Some(b),Some(c))=>(a,b,c),_=>return Err(unavailable())};
            budget.check(text.len()+hash.len()+locator.len()+8).map_err(RepositoryError::Validation)?;
            let expected=chunks.get(index).ok_or_else(unavailable)?;
            if ordinal!=expected.ordinal as i64 || text!=expected.text || hash!=expected.text_sha256 {return Err(unavailable());}
            if locator!=expected.locator_json {
                if request.version!=2 {return Err(unavailable());}
                locator_changed = true;
            }
            index+=1;
        }
        if index!=chunks.len(){return Err(unavailable());}
        let current_id=format!("doc_{}",&sha256_hex(format!("{}\0{}\0{}\0{}\0frankenmemory-rag-v2",request.owner,source_id,n,request.extracted_sha256).as_bytes())[..32]);
        if old_id!=&current_id || source_id!=canonical || locator_changed {same=false;}
    }
    let revision=match &head {Some((n,_,_)) if same=>*n,Some((n,_,_))=>n.checked_add(1).ok_or_else(unavailable)?,None=>1};
    let doc_hash=sha256_hex(format!("{}\0{}\0{}\0{}\0frankenmemory-rag-v2",request.owner,source_id,revision,request.extracted_sha256).as_bytes());
    let document_id=format!("doc_{}",&doc_hash[..32]);
    let title=std::path::Path::new(&request.source_uri).file_name().and_then(|v|v.to_str()).filter(|v|!v.is_empty()).unwrap_or(&document_id);
    let now=chrono::Utc::now().to_rfc3339();
    if same {
        let document_count:i64=tx.query_row("SELECT count(*) FROM fm_v2_documents WHERE owner_id=?1 AND source_id=?2 AND source_revision=?3",params![request.owner,source_id,revision],|r|r.get(0))?;
        if document_count!=1 {return Err(unavailable());}
        let document:Option<(String,i64,String,String,String)>=tx.query_row("SELECT source_id,source_revision,title,parser_version,content_hash FROM fm_v2_documents WHERE owner_id=?1 AND document_id=?2",params![request.owner,document_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
        if document!=Some((source_id.clone(),revision,title.to_string(),request.chunker_version.clone(),request.extracted_sha256.clone())) {return Err(unavailable());}
        let count:i64=tx.query_row("SELECT count(*) FROM fm_v2_chunks WHERE owner_id=?1 AND document_id=?2",params![request.owner,document_id],|r|r.get(0))?;
        if count!=chunks.len() as i64 {return Err(unavailable());}
    } else {
        tx.execute("INSERT INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,source_uri,source_revision,content_hash,source_type,created_at) VALUES(?1,?2,'','',?3,?4,?5,'owner_imported_file',?6)",params![request.owner,source_id,request.source_uri,revision,request.extracted_sha256,now])?;
        tx.execute("INSERT INTO fm_v2_documents(owner_id,document_id,source_id,source_revision,title,parser_version,content_hash,created_at) VALUES(?1,?2,?3,?4,?5,'frankenmemory-rag-v2',?6,?7)",params![request.owner,document_id,source_id,revision,title,request.extracted_sha256,now])?;
    }
    for chunk in chunks {
        budget.check(chunk.text.len()+chunk.locator_json.len()).map_err(RepositoryError::Validation)?;
        let hash=sha256_hex(format!("{}\0{}\0frankenmemory-rag-v2\0{}\0{}\0{}",request.owner,document_id,chunk.ordinal,chunk.locator_json,chunk.text_sha256).as_bytes());
        let chunk_id=format!("chunk_{}",&hash[..32]);
        if same {
            let stored:Option<(String,i64,String,String,String)>=tx.query_row("SELECT document_id,ordinal,text,content_hash,locator_json FROM fm_v2_chunks WHERE owner_id=?1 AND chunk_id=?2",params![request.owner,chunk_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
            if stored!=Some((document_id.clone(),chunk.ordinal as i64,chunk.text.clone(),chunk.text_sha256.clone(),chunk.locator_json.clone())) {return Err(unavailable());}
        } else {
            tx.execute("INSERT INTO fm_v2_chunks(owner_id,chunk_id,document_id,ordinal,text,content_hash,locator_json,created_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",params![request.owner,chunk_id,document_id,chunk.ordinal as i64,chunk.text,chunk.text_sha256,chunk.locator_json,now])?;
            tx.execute("INSERT INTO fm_v2_chunks_fts(owner_id,chunk_id,text) VALUES(?1,?2,?3)",params![request.owner,chunk_id,chunk.text])?;
        }
    }
    let (generation_id,publication_id)=scoped_generation(tx,&request.owner,budget)?;
    let mut receipt=serde_json::json!({"version":1,"state":"committed","operation_id":request.operation_id,"request_sha256":digest,"account_id":request.account_id,"captured_owner":request.captured_owner,"source_id":source_id,"source_revision":revision,"document_id":document_id,"generation_id":generation_id,"publication_id":publication_id});
    if request.version == 2 {
        receipt["chunk_count"] = serde_json::json!(chunks.len());
    }
    let raw=sorted_json(&receipt,false)?; if raw.len()>8192 {return Err(unavailable());}
    tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'user',?3,'s01.document-upsert.v1',NULL,?4,?5,?6,?7)",params![request.owner,receipt_id,request.captured_owner,crate::ontology::CONTRACT_VERSION,sha256_hex(raw.as_bytes()),now,raw])?;
    tx.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?1,?2,?3,'s01_document_upsert',?4,?5,?6)",params![request.owner,event_id,receipt_id,raw,revision,now])?;
    original(tx,&selector,view,budget)
}


/// A snapshot of one personal source head, not an issuance or file grant.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentSourceLookupRequest {
    pub version: u8,
    pub account_id: String,
    pub current_owner: String,
    pub captured_owner: String,
    pub source_uri: String,
}

pub(crate) fn source_lookup(
    tx: &rusqlite::Transaction<'_>, request: &DocumentSourceLookupRequest,
    view: &super::owner_custody::VerifiedCustodyView<'_, '_>,
    budget: &mut super::ResetFenceBudget,
) -> Result<Value, RepositoryError> {
    use rusqlite::{params, OptionalExtension};
    if request.version!=1 || !valid_text(&request.account_id,false)
        || !valid_text(&request.current_owner,false) || !valid_text(&request.captured_owner,false)
        || !valid_text(&request.source_uri,false) {return Err(unavailable());}
    let captured=view.for_captured(tx,&request.captured_owner)?;
    if captured.account_id()!=request.account_id || captured.current_owner()!=request.current_owner
        || request.captured_owner!=request.current_owner {return Err(unavailable());}
    super::require_guarded_epoch(tx)?;
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.document_fts.v1'",[],|r|r.get(0)).optional()?;
    if marker.as_deref()!=Some("1") {return Err(unavailable());}
    let hash=sha256_hex(format!("{}\0\0\0{}",request.current_owner,request.source_uri).as_bytes());
    let expected=format!("src_{}",&hash[..32]);
    let mut after_id=String::new(); let mut after_revision=0_i64; let mut first=true;
    let mut selected:Option<String>=None; let mut head:Option<(i64,String,String)>=None;
    loop {
        let page={
            let mut stmt=tx.prepare("SELECT CASE WHEN length(CAST(source_id AS BLOB))<=1024 THEN source_id END,source_revision,CASE WHEN length(CAST(source_uri AS BLOB))<=1024 THEN source_uri END,CASE WHEN length(CAST(workspace_key AS BLOB))<=1024 THEN workspace_key END,CASE WHEN length(CAST(project_key AS BLOB))<=1024 THEN project_key END,CASE WHEN length(CAST(source_type AS BLOB))<=1024 THEN source_type END,CASE WHEN length(CAST(forget_state AS BLOB))<=1024 THEN forget_state END,CASE WHEN length(CAST(content_hash AS BLOB))<=1024 THEN content_hash END FROM fm_v2_sources WHERE owner_id=?1 AND (?4=0 OR source_id>?2 OR (source_id=?2 AND source_revision>?3)) ORDER BY source_id,source_revision LIMIT 128")?;
            let mut rows=stmt.query(params![request.current_owner,after_id,after_revision,if first {0} else {1}])?;
            let mut page=Vec::new();
            while let Some(row)=rows.next()? {
                let id:Option<String>=row.get(0)?;let revision:i64=row.get(1)?;let uri:Option<String>=row.get(2)?;
                let workspace:Option<String>=row.get(3)?;let project:Option<String>=row.get(4)?;let kind:Option<String>=row.get(5)?;
                let state:Option<String>=row.get(6)?;let digest:Option<String>=row.get(7)?;
                let (id,uri,workspace,project,kind,state,digest)=match(id,uri,workspace,project,kind,state,digest) {
                    (Some(a),Some(b),Some(c),Some(d),Some(e),Some(f),Some(g))=>(a,b,c,d,e,f,g),_=>return Err(unavailable())};
                budget.check(id.len()+uri.len()+workspace.len()+project.len()+kind.len()+state.len()+digest.len()+8).map_err(RepositoryError::Validation)?;
                if id.is_empty() || revision<=0 {return Err(unavailable());}
                page.push((id,revision,uri,workspace,project,kind,state,digest));
            }
            page
        };
        if page.is_empty() {break;}
        let page_len=page.len(); first=false;
        for (id,revision,uri,workspace,project,kind,state,digest) in page {
            after_id=id.clone();after_revision=revision;
            let matches=uri==request.source_uri && workspace.is_empty() && project.is_empty();
            if id==expected && !matches {return Err(unavailable());}
            if matches {
                if kind!="owner_imported_file" || !matches!(state.as_str(),"active"|"forgotten") || !valid_hash(&digest) {return Err(unavailable());}
                if selected.as_ref().is_some_and(|prior| prior!=&id) {return Err(unavailable());}
                selected=Some(id);head=Some((revision,digest,state));
            }
        }
        if page_len<128 {break;}
    }
    match (selected,head) {
        (None,None)=>Ok(serde_json::json!({"version":1,"state":"absent"})),
        (Some(source_id),Some((revision,digest,state)))=>{
            let mixed:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_sources WHERE owner_id=?1 AND source_id=?2 AND (source_uri<>?3 OR workspace_key<>'' OR project_key<>'' OR source_type<>'owner_imported_file' OR forget_state NOT IN ('active','forgotten')))",params![request.current_owner,source_id,request.source_uri],|r|r.get(0))?;
            if mixed {return Err(unavailable());}
            if state=="forgotten" {
                let documents:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_documents WHERE owner_id=?1 AND source_id=?2)",params![request.current_owner,source_id],|r|r.get(0))?;
                if documents {return Err(unavailable());}
            }
            Ok(serde_json::json!({"version":1,"state":"present","source_id":source_id,"source_revision":revision,"content_sha256":digest,"forget_state":state}))
        },
        _=>Err(unavailable()),
    }
}
