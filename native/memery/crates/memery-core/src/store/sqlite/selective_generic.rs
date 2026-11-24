//! Private, same-transaction association for historical unreviewed candidates.
//! The reset owner supplies current authority and excludes its typed families.
use std::collections::BTreeSet;
use rusqlite::{params, OptionalExtension, Transaction};
use serde::Serialize;

#[derive(Serialize)]
pub(super) struct GenericPartition {
    pub candidate_ids: Vec<String>,
    pub binding_sha256: String,
}

/// Identifiers are transaction-private locators, not a public reset projection.
/// The caller must reclassify in its original transaction before any mutation,
/// then apply the existing raw/recovery and replay-denial owners.
pub(super) fn classify_selective_generic(
    tx: &Transaction<'_>, owner: &str, excluded: &BTreeSet<String>,
    budget: &mut crate::repository::ResetFenceBudget,
) -> Result<GenericPartition, String> {
    if owner.is_empty() || owner.trim() != owner { return Err("generic association unavailable".into()); }
    let mut stmt=tx.prepare("SELECT id,workspace_id,raw_evidence_ids,accepted_curated_id,json_array(id,content,kind,confidence_score,importance_score,owner,workspace_id,workspace_path,session_id,turn_id,raw_evidence_ids,evidence_role,source,source_event_id,dedup_key,status,reason,accepted_curated_id,created_at,updated_at,source_uri,source_revision,content_hash,source_message_ids) FROM candidates WHERE owner=?1 AND status IN ('pending','rejected') ORDER BY id").map_err(|_|"generic association unavailable")?;
    let mut rows=stmt.query([owner]).map_err(|_|"generic association unavailable")?;
    let mut ids=Vec::new(); let mut bindings=Vec::new();
    while let Some(row)=rows.next().map_err(|_|"generic association unavailable")? {
        let mut size=0usize;
        for i in 0..5 { size=size.saturating_add(match row.get_ref(i).map_err(|_|"generic association unavailable")? { rusqlite::types::ValueRef::Text(v)|rusqlite::types::ValueRef::Blob(v)=>v.len(), _=>8 }); }
        budget.check(size)?;
        let (id,workspace,raw_ids,accepted,raw):(String,String,String,Option<String>,String)=(row.get(0).map_err(|_|"generic association unavailable")?,row.get(1).map_err(|_|"generic association unavailable")?,row.get(2).map_err(|_|"generic association unavailable")?,row.get(3).map_err(|_|"generic association unavailable")?,row.get(4).map_err(|_|"generic association unavailable")?);
        if excluded.contains(&id) { continue; }
        if id.is_empty() || id.trim()!=id || workspace.is_empty() || workspace.trim()!=workspace || accepted.is_some() {return Err("generic association unavailable".into());}
        let linked:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_candidates WHERE candidate_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_s05_job_refs WHERE candidate_id=?2) OR EXISTS(SELECT 1 FROM curated WHERE id=?2) OR EXISTS(SELECT 1 FROM facts WHERE candidate_id=?2) OR EXISTS(SELECT 1 FROM graph_nodes WHERE candidate_id=?2) OR EXISTS(SELECT 1 FROM graph_edges WHERE candidate_id=?2) OR EXISTS(SELECT 1 FROM graph_cues WHERE candidate_id=?2)",params![owner,id],|r|r.get(0)).map_err(|_|"generic association unavailable")?;
        if linked {return Err("generic association unavailable".into());}
        let value=crate::field_authority::parse_unique_json(&raw_ids).map_err(|_|"generic association unavailable")?;
        let refs:Vec<String>=serde_json::from_value(value).map_err(|_|"generic association unavailable")?;
        let mut seen=BTreeSet::new();
        for raw_id in &refs {
            if raw_id.is_empty() || raw_id.trim()!=raw_id || !seen.insert(raw_id) {return Err("generic association unavailable".into());}
            let scope:Option<(Option<String>,String)>=tx.query_row("SELECT owner,workspace_id FROM raw WHERE id=?1",[raw_id],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|_|"generic association unavailable")?;
            if scope.as_ref().is_none_or(|(actual,w)|actual.as_deref()!=Some(owner) || w!=&workspace) {return Err("generic association unavailable".into());}
        }
        bindings.push((id.clone(),workspace,crate::repository::sha256_hex(raw.as_bytes()))); ids.push(id);
    }
    drop(rows); drop(stmt);
    // Canonical curated mirrors name the candidate in metadata. Examine only
    // decoded candidate_id edges into the selected set: unrelated malformed
    // metadata has no canonical edge and remains untouched. json_each catches
    // duplicate candidate_id keys too, which conservatively refuses a match.
    // A graph node referring to such a curated mirror cannot survive candidate
    // removal because the mirror itself makes the association unavailable.
    if !ids.is_empty() {
        let selected=serde_json::to_string(&ids).map_err(|_|"generic association unavailable")?;
        budget.check(selected.len())?;
        let linked:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM curated c JOIN json_each(CASE WHEN json_valid(c.metadata) THEN c.metadata ELSE '{}' END) m ON m.key='candidate_id' AND m.type='text' JOIN json_each(?1) selected ON selected.value=m.value)",[selected],|r|r.get(0)).map_err(|_|"generic association unavailable")?;
        if linked {return Err("generic association unavailable".into());}
    }
    let encoded=serde_json::to_vec(&("s04.generic-candidate-association.v1",owner,&bindings)).map_err(|_|"generic association unavailable")?;
    Ok(GenericPartition{candidate_ids:ids,binding_sha256:crate::repository::sha256_hex(&encoded)})
}
