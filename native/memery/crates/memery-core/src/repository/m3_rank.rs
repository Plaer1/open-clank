//! M3 scoped final-position ranking. No memory content or event is written.
use super::*;
use crate::retrieval::rank_learning::{self,RankFacts};

#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct M3RankRequest {
    pub version:u32,pub owner:String,pub workspace_id:String,pub account_id:String,
    pub policy_projection:PolicyPublicationRefs,pub policy_input_json:String,pub candidate_ids:Vec<String>,
}

/// The caller has already opened the guarded read transaction and verified
/// the account custody view. Noncanonical candidate slots are kept in place.
pub(crate) fn rank_in_tx(tx:&Transaction<'_>,r:&M3RankRequest,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget)->Result<Value,RepositoryError> {
    if r.version!=1 || [&r.owner,&r.workspace_id,&r.account_id].iter().any(|v|v.is_empty()||v.trim()!=v.as_str()||v.len()>512)
        || view.current_owner()!=r.owner || view.account_id()!=r.account_id || r.candidate_ids.len()>100 {return Err(RepositoryError::NotFound);}
    let mut seen=std::collections::BTreeSet::new();
    if r.candidate_ids.iter().any(|id|id.is_empty() || id.trim()!=id || id.len()>512 || !seen.insert(id)) {return Err(RepositoryError::NotFound);}
    require_guarded_epoch(tx)?;require_policy_projection(tx,&r.account_id,&r.workspace_id,Some(&r.policy_projection))?;
    budget.check(r.policy_input_json.len()).map_err(RepositoryError::Validation)?;
    let (settings,effects)=rank_learning::projected_policy(&r.policy_input_json,&r.policy_projection.account.fingerprint).map_err(RepositoryError::Validation)?;
    let mut result=r.candidate_ids.clone();
    if !settings.enabled || !(settings.salience||settings.age_decay||settings.retrieval_boost) {
        return Ok(json!({"version":1,"state":"off","ordered_ids":result}));
    }
    let mut slots=Vec::new();let mut facts=Vec::new();let mut targets=std::collections::BTreeMap::new();
    for (index,id) in r.candidate_ids.iter().enumerate() {
        budget.check(id.len()).map_err(RepositoryError::Validation)?;
        let row:Option<(String,f64,String)>=tx.query_row("SELECT source,importance_score,created_at FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3 AND archived=0",params![r.owner,r.workspace_id,id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?))).optional()?;
        let Some((source,importance,created_at))=row else {continue};
        if source=="s08_policy" {
            let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s02.importance.v1'",[],|row|row.get(0)).optional()?;
            if marker.as_deref()==Some("1") {
                // The curated original names its root candidate; later accepted
                // target-score proposals may legitimately reference this same
                // curated ID and must not be mistaken for a second root.
                let size:Option<i64>=tx.query_row("SELECT length(CAST(metadata AS BLOB)) FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![r.owner,r.workspace_id,id],|row|row.get(0)).optional()?;
                let size=size.filter(|v|(1..=16384).contains(v)).ok_or(RepositoryError::NotFound)?;budget.check(size as usize).map_err(RepositoryError::Validation)?;
                let raw:String=tx.query_row("SELECT metadata FROM curated WHERE owner=?1 AND workspace_id=?2 AND id=?3",params![r.owner,r.workspace_id,id],|row|row.get(0))?;
                let metadata=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
                if let Some(candidate)=metadata.get("candidate_id").and_then(Value::as_str) {
                    let root=super::s08_policy_review::summary_accepted_root(tx,&r.owner,&r.account_id,&r.workspace_id,candidate,view,budget)?;
                    if root["curated_id"]!=*id {return Err(RepositoryError::NotFound)}
                    let block=root["block_id"].as_str().ok_or(RepositoryError::NotFound)?;
                    let size:Option<i64>=tx.query_row("SELECT length(CAST(metadata_json AS BLOB)) FROM fm_v2_revision_metadata WHERE owner_id=?1 AND block_id=?2 AND revision=1",params![r.owner,block],|row|row.get(0)).optional()?;
                    if let Some(size)=size {
                        if !(1..=16384).contains(&size) {return Err(RepositoryError::NotFound)}budget.check(size as usize).map_err(RepositoryError::Validation)?;
                        let metadata=value_metadata_carry(tx,&r.owner,block,1)?.ok_or(RepositoryError::NotFound)?;
                        let value=crate::field_authority::parse_unique_json(&metadata.json).map_err(RepositoryError::Validation)?;
                        if value.get("importance_score").is_some() {
                            let context=super::s08_policy_review::current_importance_context(tx,&r.owner,&r.account_id,&r.workspace_id,candidate,view,budget)?;
                            let revision=context["revision"].as_i64().ok_or(RepositoryError::NotFound)?;
                            let score=context["importance"]["value"].as_f64().ok_or(RepositoryError::NotFound)?;
                            if score!=importance || revision<1 || targets.insert((block.to_owned(),revision),facts.len()).is_some() {return Err(RepositoryError::NotFound)}
                            slots.push(index);facts.push(RankFacts{id:id.clone(),importance:score,created_at,events:None});continue;
                        }
                    }
                }
            } else if marker.is_some() {return Err(RepositoryError::NotFound)}
        }
        let blocks:i64=tx.query_row("SELECT count(*) FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND workspace_key=?2 AND project_key='' AND claim_slot=?3",params![r.owner,r.workspace_id,id],|row|row.get(0))?;
        if blocks==0 {continue;} if blocks!=1 {return Err(RepositoryError::NotFound);}
        let block=if source=="preextracted" {
            let mut stmt=tx.prepare("SELECT id FROM candidates WHERE owner=?1 AND workspace_id=?2 AND source='preextracted' AND accepted_curated_id=?3 ORDER BY id LIMIT 2")?;
            let candidates=stmt.query_map(params![r.owner,r.workspace_id,id],|row|row.get::<_,String>(0))?.collect::<Result<Vec<_>,_>>()?;drop(stmt);
            if candidates.len()!=1 {return Err(RepositoryError::NotFound);}
            let context=crate::store::sqlite::preextracted::selective_retained_context(tx,&r.owner,&r.workspace_id,&r.account_id,&candidates[0],id,view,budget).map_err(RepositoryError::Validation)?;
            context.get("block_id").and_then(Value::as_str).ok_or(RepositoryError::NotFound)?.to_owned()
        } else {
            // An existing canonical block must have its exact account-owned
            // mirror proof; an unexplained canonical row never becomes legacy.
            let context=current_import_mirror_context_custody(tx,&r.workspace_id,id,view)?;
            context.get("binding").and_then(|v|v.get("block_id")).and_then(Value::as_str).ok_or(RepositoryError::NotFound)?.to_owned()
        };
        let revision:i64=tx.query_row("SELECT current_revision FROM fm_v2_knowledge_blocks WHERE owner_id=?1 AND workspace_key=?2 AND project_key='' AND block_id=?3 AND claim_slot=?4",params![r.owner,r.workspace_id,block,id],|row|row.get(0))?;
        if revision<1 || targets.insert((block,revision),facts.len()).is_some() {return Err(RepositoryError::NotFound);}
        slots.push(index);facts.push(RankFacts{id:id.clone(),importance,created_at,events:None});
    }
    if settings.retrieval_boost && [&effects.found,&effects.admitted,&effects.helped].iter().any(|v|v.enabled && v.weight>0.0) {
        let counts=super::m3_events::counts(tx,&r.owner,&r.workspace_id,&r.account_id,&targets,&effects,view,budget,&r.policy_projection.account.fingerprint)?;
        if counts.len()!=facts.len() {return Err(RepositoryError::NotFound);}
        for (fact,events) in facts.iter_mut().zip(counts) {fact.events=Some(events);}
    }
    let order=rank_learning::permutation(&settings,&effects,&facts,chrono::Utc::now()).map_err(RepositoryError::Validation)?;
    for (target,source) in slots.iter().zip(order) {result[*target]=facts[source].id.clone();}
    Ok(json!({"version":1,"state":"ranked","ordered_ids":result}))
}
