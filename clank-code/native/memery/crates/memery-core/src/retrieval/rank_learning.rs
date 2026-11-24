//! Bounded M3 rank-only transforms. Callers provide verified scoped facts.
use serde::{Deserialize,Serialize};

#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RankLearning {pub enabled:bool,pub salience:bool,pub age_decay:bool,pub retrieval_boost:bool}
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Effect {pub enabled:bool,pub weight:f64}
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Effects {pub found:Effect,pub admitted:Effect,pub helped:Effect}
#[derive(Clone,Debug)]
pub(crate) struct RankFacts {pub id:String,pub importance:f64,pub created_at:String,pub events:Option<RankEvents>}
#[derive(Clone,Debug)]
pub(crate) struct RankEvents {pub found:Option<u32>,pub admitted:Option<u32>,pub helped_positive:Option<u32>,pub helped_negative:Option<u32>}

/// Exact Host policy input is accepted only when its bytes match the live
/// account projection; caller-provided switches are never authoritative.
pub(crate) fn projected_policy(raw:&str,account_fingerprint:&str)->Result<(RankLearning,Effects),String> {
    if raw.len()>65536 || crate::repository::sha256_hex(raw.as_bytes())!=account_fingerprint {return Err("ranking unavailable".into());}
    let value=crate::field_authority::parse_unique_json(raw).map_err(|_|"ranking unavailable")?;
    let root=value.as_object().ok_or("ranking unavailable")?;
    if root.len()!=2 || !root.get("can_manage_memory").is_some_and(serde_json::Value::is_boolean) {return Err("ranking unavailable".into());}
    let preferences=root.get("preferences").and_then(serde_json::Value::as_object).ok_or("ranking unavailable")?;
    let entry=preferences.get("memory_policy").and_then(serde_json::Value::as_array).filter(|a|a.len()==2 && a[0].is_boolean()).ok_or("ranking unavailable")?;
    let disabled=RankLearning{enabled:false,salience:false,age_decay:false,retrieval_boost:false};
    let no_effect=||Effect{enabled:false,weight:0.0};
    let empty=||Effects{found:no_effect(),admitted:no_effect(),helped:no_effect()};
    if entry[0]==false {return if entry[1].is_null() {Ok((disabled,empty()))} else {Err("ranking unavailable".into())};}
    if entry[1].is_null() {return Ok((disabled,empty()));}
    let doc=entry[1].as_object().ok_or("ranking unavailable")?;
    let accepted=match doc.get("schema").and_then(serde_json::Value::as_str) {
        Some("openclank.memory-policy.v1")=>&entry[1],
        Some("openclank.memory-policy.v2"|"openclank.memory-policy.v3")=>doc.get("accepted_policy").ok_or("ranking unavailable")?,
        _=>return Err("ranking unavailable".into()),
    };
    if accepted.is_null() {return Ok((disabled,empty()));}
    let policy=accepted.get("policy").and_then(serde_json::Value::as_object).ok_or("ranking unavailable")?;
    let ranking=match policy.get("rank_learning") {None=>disabled,Some(v)=>serde_json::from_value::<RankLearning>(v.clone()).map_err(|_|"ranking unavailable")?};
    let effects:Effects=serde_json::from_value(policy.get("effects").cloned().ok_or("ranking unavailable")?).map_err(|_|"ranking unavailable")?;
    if [&effects.found,&effects.admitted,&effects.helped].iter().any(|v|!weight_ok(v)) {return Err("ranking unavailable".into());}
    Ok((ranking,effects))
}

fn weight_ok(e:&Effect)->bool {e.weight.is_finite() && (0.0..=1.0).contains(&e.weight) && ((e.weight*20.0).round()-e.weight*20.0).abs()<1e-8}
fn count(n:u32)->f64 {f64::from(n.min(3))/3.0}
/// Return input indexes in ranked order; off bypasses every optional fact.
/// Two positions is the maximum contribution of each independent component.
pub(crate) fn permutation(settings:&RankLearning,effects:&Effects,facts:&[RankFacts],now:chrono::DateTime<chrono::Utc>)->Result<Vec<usize>,String> {
    let mut order:Vec<usize>=(0..facts.len()).collect();
    if !settings.enabled || !(settings.salience || settings.age_decay || settings.retrieval_boost) {return Ok(order);}
    if facts.len()>100 || [&effects.found,&effects.admitted,&effects.helped].iter().any(|e|!weight_ok(e)) {return Err("ranking unavailable".into());}
    let mut values=Vec::with_capacity(facts.len());
    let mut seen=std::collections::BTreeSet::new();
    for (i,fact) in facts.iter().enumerate() {
        if fact.id.is_empty() || !seen.insert(&fact.id) {return Err("ranking unavailable".into());}
        let mut rank=i as f64;
        if settings.salience {
            if !fact.importance.is_finite() || !(0.0..=1.0).contains(&fact.importance) {return Err("ranking unavailable".into());}
            rank-=2.0*fact.importance;
        }
        if settings.age_decay {
            let created=chrono::DateTime::parse_from_rfc3339(&fact.created_at).map_err(|_|"ranking unavailable")?.with_timezone(&chrono::Utc);
            if created>now {return Err("ranking unavailable".into());}
            let age=(now-created).num_seconds() as f64 / 86400.0;
            rank+=2.0*(1.0-2_f64.powf(-age/365.0));
        }
        if settings.retrieval_boost {
            let needs_events=[&effects.found,&effects.admitted,&effects.helped].iter().any(|e|e.enabled && e.weight>0.0);
            if needs_events {
                let events=fact.events.as_ref().ok_or("ranking unavailable")?;
                let mut signal=0.0;
                if effects.found.enabled && effects.found.weight>0.0 {signal+=effects.found.weight*count(events.found.ok_or("ranking unavailable")?);}
                if effects.admitted.enabled && effects.admitted.weight>0.0 {signal+=effects.admitted.weight*count(events.admitted.ok_or("ranking unavailable")?);}
                if effects.helped.enabled && effects.helped.weight>0.0 {signal+=effects.helped.weight*(count(events.helped_positive.ok_or("ranking unavailable")?)-count(events.helped_negative.ok_or("ranking unavailable")?));}
                rank-=2.0*signal.clamp(-1.0,1.0);
            }
        }
        values.push(rank);
    }
    order.sort_by(|a,b|values[*a].total_cmp(&values[*b]).then_with(||a.cmp(b)).then_with(||facts[*a].id.cmp(&facts[*b].id)));
    Ok(order)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn off_and_independent_rank_terms() {
        let off=Effect{enabled:false,weight:0.0};let mut effects=Effects{found:off.clone(),admitted:off.clone(),helped:off};
        let mut cfg=RankLearning{enabled:false,salience:true,age_decay:false,retrieval_boost:false};
        let now=chrono::DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z").unwrap().with_timezone(&chrono::Utc);
        let facts=vec![RankFacts{id:"a".into(),importance:0.0,created_at:"2020-01-01T00:00:00Z".into(),events:Some(RankEvents{found:Some(0),admitted:Some(0),helped_positive:Some(0),helped_negative:Some(0)})},RankFacts{id:"b".into(),importance:1.0,created_at:"2025-12-31T00:00:00Z".into(),events:Some(RankEvents{found:Some(3),admitted:Some(0),helped_positive:Some(0),helped_negative:Some(0)})}];
        assert_eq!(permutation(&cfg,&effects,&facts,now).unwrap(),vec![0,1]);
        cfg.enabled=true;assert_eq!(permutation(&cfg,&effects,&facts,now).unwrap(),vec![1,0]);
        cfg.salience=false;cfg.age_decay=true;assert_eq!(permutation(&cfg,&effects,&facts,now).unwrap(),vec![1,0]);
        cfg.age_decay=false;cfg.retrieval_boost=true;assert_eq!(permutation(&cfg,&effects,&facts,now).unwrap(),vec![0,1]);
        effects.found=Effect{enabled:true,weight:1.0};assert_eq!(permutation(&cfg,&effects,&facts,now).unwrap(),vec![1,0]);
        cfg.retrieval_boost=false;cfg.salience=true;let mut invalid=facts;invalid[0].importance=f64::NAN;assert!(permutation(&cfg,&effects,&invalid,now).is_err());
    }
}
