//! Bounded, account-custody-qualified S06 evidence for final ranking.
use super::*;
use crate::retrieval::rank_learning::{Effects,RankEvents};
use std::collections::BTreeMap;

struct FoundProof {index:usize,observation:FoundObservation,authority:ObservationOriginAuthority}

fn unavailable()->RepositoryError {RepositoryError::NotFound}
fn rows(tx:&Transaction<'_>,owner:&str,reason:&str,kind:&str,budget:&mut ResetFenceBudget)->Result<Vec<(String,Value)>,RepositoryError> {
    let orphan:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox o WHERE o.owner_id=?1 AND o.kind=?2 AND NOT EXISTS(SELECT 1 FROM fm_v2_change_sets c WHERE c.owner_id=o.owner_id AND c.change_set_id=o.change_set_id AND c.reason=?3 AND o.event_id='outbox_'||c.change_set_id||'_'||?2))",params![owner,kind,reason],|r|r.get(0))?;
    if orphan {return Err(unavailable());}
    let mut stmt=tx.prepare("SELECT change_set_id,content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=?1 AND reason=?2 ORDER BY change_set_id")?;
    let mut cursor=stmt.query(params![owner,reason])?;let mut result=Vec::new();
    while let Some(row)=cursor.next()? {
        let id:String=row.get(0)?;let hash:String=row.get(1)?;let raw:Option<String>=row.get(2)?;let actor:String=row.get(3)?;let actor_id:String=row.get(4)?;let contract:String=row.get(5)?;let source:Option<String>=row.get(6)?;
        let raw=raw.ok_or_else(unavailable)?;budget.check(id.len()+raw.len()).map_err(RepositoryError::Validation)?;
        let value=crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?;
        let source_id=value.get("source_id").and_then(Value::as_str).ok_or_else(unavailable)?;
        let expected_actor=if kind=="s06_helped" {"user"} else {"automation"};
        if sha256_hex(raw.as_bytes())!=hash || actor!=expected_actor || (kind!="s06_helped" && actor_id!="memory_observation") || contract!=CONTRACT_VERSION || source.as_deref()!=Some(source_id) {return Err(unavailable());}
        let paired:Option<String>=tx.query_row("SELECT payload_json FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2 AND change_set_id=?3 AND kind=?4",params![owner,format!("outbox_{id}_{kind}"),id,kind],|r|r.get(0)).optional()?;
        if paired.as_deref()!=Some(raw.as_str()) {return Err(unavailable());}
        result.push((id,value));
    }
    Ok(result)
}

fn authority(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,found:&FoundObservation,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,fingerprint:&str)->Result<ObservationOriginAuthority,RepositoryError> {
    if found.source.account_id!=account || found.workspace_id!=workspace {return Err(unavailable());}
    view.for_captured(tx,&found.owner)?;
    let id=format!("cs_s06origin_{}",sha256_hex(&serde_json::to_vec(&(&found.owner,workspace,&found.source))?));
    let hash:Option<String>=tx.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.observation-origin.v1'",params![owner,id],|r|r.get(0)).optional()?;
    let hash=hash.filter(|v|valid_sha256(v)).ok_or_else(unavailable)?;
    let mut a=ObservationOriginAuthority{origin_id:id,origin_hash:hash,captured_owner:found.owner.clone(),current_fingerprint:fingerprint.into(),rebind_id:None,rebind_hash:None};
    if let Some((id,hash))=observation_rebind_head_custody(tx,owner,owner,&a.origin_id,&a.origin_hash,&a.captured_owner,workspace,&found.source,view,budget)? {
        a.current_fingerprint=read_observation_rebind_custody(tx,owner,&id,&hash,view,budget)?.current_fingerprint;a.rebind_id=Some(id);a.rebind_hash=Some(hash);
    }
    validate_observation_origin_custody(tx,owner,owner,workspace,&found.source,&a,view,budget)?;Ok(a)
}

pub(super) fn counts(tx:&Transaction<'_>,owner:&str,workspace:&str,account:&str,targets:&BTreeMap<(String,i64),usize>,effects:&Effects,view:&owner_custody::VerifiedCustodyView<'_,'_>,budget:&mut ResetFenceBudget,fingerprint:&str)->Result<Vec<RankEvents>,RepositoryError> {
    let want_found=effects.found.enabled && effects.found.weight>0.0;
    let want_admitted=effects.admitted.enabled && effects.admitted.weight>0.0;
    let want_helped=effects.helped.enabled && effects.helped.weight>0.0;
    let mut result=(0..targets.len()).map(|_|RankEvents{found:want_found.then_some(0),admitted:want_admitted.then_some(0),helped_positive:want_helped.then_some(0),helped_negative:want_helped.then_some(0)}).collect::<Vec<_>>();
    if !(want_found||want_admitted||want_helped) {return Ok(result);}
    if !V2Repository::s06_indexes_present(tx)? || !V2Repository::s06_source_erase_present(tx)? || ((want_admitted||want_helped) && !V2Repository::admitted_indexes_present(tx,owner)?) || (want_helped && !V2Repository::helped_indexes_present(tx,owner)?) {return Err(unavailable());}
    let mut found_map=BTreeMap::<String,FoundProof>::new();
    for (id,value) in rows(tx,owner,"s06.found.v1","s06_found",budget)? {
        let observation:FoundObservation=serde_json::from_value(value.get("observation").cloned().ok_or_else(unavailable)?)?;
        if observation.source.account_id!=account || observation.workspace_id!=workspace {continue;}
        let Some(&index)=targets.get(&(observation.memory.block_id.clone(),observation.memory.revision)) else {continue};
        let a=authority(tx,owner,workspace,account,&observation,view,budget,fingerprint)?;
        let mut check=|captured:&str,source:Option<(&HostArchiveSourceRef,&mut ObservationOriginAuthority)>| {
            view.for_captured(tx,captured)?;
            if let Some((source,provided))=source {if source!=&observation.source {return Err(unavailable());}*provided=a.clone();validate_observation_origin_custody(tx,owner,owner,workspace,source,provided,view,budget)?;}
            Ok(())
        };
        let verified=V2Repository::read_found_in_transaction_inner(tx,owner,workspace,&id,&a,Some(&mut check))?;drop(check);
        if verified!=observation || found_map.insert(id,FoundProof{index,observation,authority:a}).is_some() {return Err(unavailable());}
        if want_found {let n=result[index].found.as_mut().ok_or_else(unavailable)?;*n=n.saturating_add(1).min(3);}
    }
    if !(want_admitted||want_helped) {return Ok(result);}
    let mut admitted_map=BTreeMap::<String,(String,String)>::new();
    for (id,value) in rows(tx,owner,"s06.admitted.v1","s06_admitted",budget)? {
        let version=value.get("version").and_then(Value::as_str).ok_or_else(unavailable)?;
        let request=value.get("request").cloned().ok_or_else(unavailable)?;
        let found_id=match version {
            "s06.admitted.v1"=>serde_json::from_value::<AdmittedObservation>(request)?.found_event_id,
            "s06.context-admitted.v1"=>serde_json::from_value::<ContextAdmittedObservation>(request)?.found_event_id,
            _=>return Err(unavailable()),
        };
        let Some(proof)=found_map.get(&found_id) else {
            let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2 AND reason='s06.found.v1')",params![owner,found_id],|r|r.get(0))?;
            if !exists {return Err(unavailable());}continue;
        };
        let mut check=|captured:&str,source:Option<(&HostArchiveSourceRef,&mut ObservationOriginAuthority)>| {
            view.for_captured(tx,captured)?;
            if let Some((source,provided))=source {if source!=&proof.observation.source {return Err(unavailable());}*provided=proof.authority.clone();validate_observation_origin_custody(tx,owner,owner,workspace,source,provided,view,budget)?;}
            Ok(())
        };
        if version=="s06.admitted.v1" {V2Repository::read_admitted_in_transaction_inner(tx,owner,workspace,&id,&proof.authority,Some(&mut check))?;}
        else {V2Repository::read_context_admitted_in_transaction_inner(tx,owner,workspace,&id,&proof.authority,Some(&mut check))?;}
        drop(check);
        if admitted_map.insert(id,(found_id,if version=="s06.admitted.v1" {"tool"} else {"context"}.into())).is_some() {return Err(unavailable());}
        if want_admitted {let n=result[proof.index].admitted.as_mut().ok_or_else(unavailable)?;*n=n.saturating_add(1).min(3);}
    }
    if !want_helped {return Ok(result);}
    for (id,value) in rows(tx,owner,"s06.helped.v1","s06_helped",budget)? {
        let request:HelpedObservation=serde_json::from_value(value.get("request").cloned().ok_or_else(unavailable)?)?;
        if request.account_id!=account || request.workspace_id!=workspace {continue;}
        let Some((found_id,admitted_kind))=admitted_map.get(&request.admitted_event_id) else {
            if targets.contains_key(&(request.memory.block_id.clone(),request.memory.revision)) {return Err(unavailable());}continue;
        };
        let proof=found_map.get(found_id).ok_or_else(unavailable)?;
        view.for_captured(tx,&request.owner)?;
        let ids=[&request.owner,&request.workspace_id,&request.account_id,&request.admitted_event_id,&request.request_key];
        if value.as_object().map(|v|v.len())!=Some(3) || ids.iter().any(|v|v.is_empty()||v.trim()!=v.as_str()||v.len()>512) || request.version!=1 || request.producer!="host_human_review.v1" || request.admitted_kind!=*admitted_kind || request.memory!=proof.observation.memory || request.authority.origin_id!=proof.authority.origin_id || request.authority.origin_hash!=proof.authority.origin_hash || request.authority.captured_owner!=proof.authority.captured_owner || !matches!(request.polarity.as_str(),"positive"|"negative") || request.request_key!=V2Repository::helped_key(&request)? || id!=V2Repository::helped_id(&request)? || value.get("source_id")!=Some(&json!(host_archive_source_id(&proof.observation.source)?)) || value.get("version")!=Some(&json!("s06.helped.v1")) {return Err(unavailable());}
        let (actor,revision):(String,i64)=tx.query_row("SELECT c.actor_id,o.source_revision FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.kind='s06_helped' WHERE c.owner_id=?1 AND c.change_set_id=?2",params![owner,id],|r|Ok((r.get(0)?,r.get(1)?)))?;
        if actor!=account || revision!=proof.observation.memory.revision {return Err(unavailable());}
        let n=if request.polarity=="positive" {result[proof.index].helped_positive.as_mut()} else {result[proof.index].helped_negative.as_mut()}.ok_or_else(unavailable)?;*n=n.saturating_add(1).min(3);
    }
    Ok(result)
}
