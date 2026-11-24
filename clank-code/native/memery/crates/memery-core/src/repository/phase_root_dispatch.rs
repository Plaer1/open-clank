//! Phase-bound ordinary archive root fence on the shared paired proof owner.
use super::*;
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PhaseRootFenceRequest {
    pub version:u32,pub phase:QueuedTransitionBinding,pub workspace_id:String,pub job_id:String,
    pub source:HostArchiveSourceRef,pub policy_fingerprint:String,
}
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PhaseRootRenewRequest {
    pub version:u32,pub transition:crate::store::sqlite::GuardedOwnerRenameRequest,pub receipt_id:String,pub receipt_hash:String,
    pub workspace_id:String,pub source:HostArchiveSourceRef,pub fence_id:String,pub policy_projection:PolicyPublicationRefs,
}
/// Observation origin is a persisted factual source lineage, not an S05 job
/// or a substitute for the Host's active transition/source authority.
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PhaseRootObservationFenceRequest {
    pub version:u32,pub phase:QueuedTransitionBinding,pub workspace_id:String,
    pub source:HostArchiveSourceRef,pub authority:ObservationOriginAuthority,
    #[serde(default)] pub deleted_chat:bool,
}
#[derive(Debug,Clone,Serialize,Deserialize,JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PhaseRootObservationRenewRequest {
    pub version:u32,pub transition:crate::store::sqlite::GuardedOwnerRenameRequest,pub receipt_id:String,pub receipt_hash:String,
    pub workspace_id:String,pub source:HostArchiveSourceRef,pub fence_id:String,pub policy_projection:PolicyPublicationRefs,
    pub authority:ObservationOriginAuthority,pub current_fingerprint:String,
}
impl<'a> V2Repository<'a> {
    pub(crate) fn phase_root_observation_fence(&mut self,r:&PhaseRootObservationFenceRequest)->Result<Value,RepositoryError> {
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        require_guarded_epoch(&tx)?;r.source.validate_in(&tx)?;
        if r.version!=1 || r.phase.account_id!=r.source.account_id || r.workspace_id.is_empty() || r.workspace_id.trim()!=r.workspace_id || r.workspace_id.len()>512 {return Err(RepositoryError::NotFound);}
        validate_queued_binding(&tx,&r.phase,&r.source.account_id)?;
        let mut budget=ResetFenceBudget::install(&tx);
        let lengths:Option<(i64,i64)>=tx.query_row("SELECT length(CAST(c.operation_metadata_json AS BLOB)),length(CAST(o.payload_json AS BLOB)) FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?3 AND o.kind='s06_observation_origin' WHERE c.owner_id=?1 AND c.change_set_id=?2",params![r.phase.owner,r.authority.origin_id,format!("outbox_{}_s06_observation_origin",r.authority.origin_id)],|row|Ok((row.get(0)?,row.get(1)?))).optional()?;
        let (a,b)=lengths.ok_or(RepositoryError::NotFound)?;if !(0..=16384).contains(&a) || !(0..=16384).contains(&b) {return Err(RepositoryError::NotFound);}budget.check((a+b) as usize).map_err(RepositoryError::Validation)?;
        // Charge the exact origin's rebind history before the existing verifier
        // traverses it. The scoped index and transaction progress guard apply.
        let history:(i64,i64)=tx.query_row("SELECT count(*),coalesce(sum(length(CAST(operation_metadata_json AS BLOB))),0) FROM fm_v2_change_sets INDEXED BY s06_observation_rebind_current WHERE owner_id=?1 AND reason='s06.observation-rebind.v2' AND json_valid(operation_metadata_json) AND json_extract(operation_metadata_json,'$.origin_id')=?2",params![r.phase.owner,r.authority.origin_id],|row|Ok((row.get(0)?,row.get(1)?)))?;
        if history.0<0 || history.1<0 {return Err(RepositoryError::NotFound);}budget.check(usize::try_from(history.1).map_err(|_|RepositoryError::NotFound)?).map_err(RepositoryError::Validation)?;
        let view=owner_custody::verified_or_direct_custody_view(&tx,&r.phase.database_id,&r.phase.account_id,&r.phase.owner,&r.authority.captured_owner,&mut budget)?;
        let prior=match (&r.authority.rebind_id,&r.authority.rebind_hash) {(Some(id),Some(hash))=>Some(read_observation_rebind_custody(&tx,&r.phase.owner,id,hash,&view,&mut budget)?),(None,None)=>None,_=>return Err(RepositoryError::NotFound)};
        let head_owner=prior.as_ref().map(|v|v.owner.as_str()).unwrap_or(&r.authority.captured_owner);
        validate_observation_origin_custody(&tx,&r.phase.owner,head_owner,&r.workspace_id,&r.source,&r.authority,&view,&mut budget)?;
        let _segment=observation_phase_segment(&tx,&view,head_owner,prior.as_ref())?;
        let root = host_archive_source_id(&r.source)?;
        let fence_id = if r.deleted_chat {
            use super::paired_transition as proof;
            if !proof::deleted_chat_phase(&r.phase)
                || proof::source_state(&tx,&r.phase.owner,&r.workspace_id,&root,r.source.revision,&r.source.content_hash,"host_archive_part.v1")? != "forget_requested" {
                return Err(RepositoryError::NotFound);
            }
            if r.phase.action == "reverse" || r.phase.generation > 1 {
                let (previous,key,hash)=view.head_transition(&tx)?.ok_or(RepositoryError::NotFound)?;
                let prior=queued_binding(previous);
                let adjacent=match r.phase.action.as_str() {
                    "reverse" => prior.action=="forward" && r.phase.generation==prior.generation,
                    "forward" => prior.action=="reverse" && prior.generation.checked_add(1)==Some(r.phase.generation),
                    _ => false,
                };
                if !adjacent || !view.is_head_transition(&tx,previous,key,hash)? || prior.operation_id!=r.phase.operation_id
                    || prior.database_id!=r.phase.database_id || prior.account_id!=r.phase.account_id
                    || prior.step!=r.phase.step || prior.owner!=r.phase.new_owner || prior.new_owner!=r.phase.owner
                    || !proof::has_deleted_chat_proof(&tx,&r.phase.owner,&prior,&r.workspace_id,&root,r.source.revision,&r.source.content_hash,&mut budget)? {
                    return Err(RepositoryError::NotFound);
                }
            }
            let blocked:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id=?1 AND (source_id=?2 OR source_id IS NULL)) OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id IN (?3,?4))",
                params![r.phase.owner,root,proof::proof_id(&r.phase,"root",&root,None)?,proof::proof_id(&r.phase,"root_absent",&root,None)?],|row|row.get(0))?;
            if blocked {return Err(RepositoryError::NotFound);}
            let (id,body)=proof::deleted_chat_proof(&r.phase,&r.workspace_id,&root,r.source.revision,&r.source.content_hash)?;
            if !proof::receipt(&tx,&r.phase.owner,&id,"paired_rename_root_deleted_chat",&body,r.source.revision,&mut budget)? {
                proof::insert_receipt(&tx,&r.phase.owner,&id,"paired_rename_root_deleted_chat",&body,r.source.revision,&mut budget)?;
            }
            id
        } else {
            let (id,active)=super::paired_transition::fence_root(&tx,&r.phase,&r.workspace_id,&r.source,&mut budget)?;
            if !active {return Err(RepositoryError::NotFound);}
            id
        };
        drop(view);
        host_visibility_with_account_phase(&tx,&r.phase,&r.workspace_id,&root,&mut budget,true)?;
        drop(budget);tx.commit()?;Ok(json!({"version":1,"state":"fenced","fence_id":fence_id}))
    }

    /// The Host owns fresh current target/source authority. This transaction
    /// binds the original observation lineage to the exact committed head and
    /// the canonical root renewal without inventing an older legacy fence.
    pub(crate) fn phase_root_observation_renew(&mut self,r:&PhaseRootObservationRenewRequest)->Result<Value,RepositoryError> {
        if r.version!=1 || !valid_sha256(&r.current_fingerprint) || r.source.account_id!=r.transition.account_id || r.workspace_id.is_empty() || r.workspace_id.trim()!=r.workspace_id || r.workspace_id.len()>512 {return Err(RepositoryError::NotFound);}
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;require_guarded_epoch(&tx)?;r.source.validate_in(&tx)?;
        let mut budget=ResetFenceBudget::install(&tx);let current=&r.transition.new_owner;
        let view=owner_custody::verified_custody_view(&tx,&r.transition.database_id,&r.transition.account_id,current,&r.authority.captured_owner,&mut budget)?;
        let prior=match (&r.authority.rebind_id,&r.authority.rebind_hash) {(Some(id),Some(hash))=>Some(read_observation_rebind_custody(&tx,current,id,hash,&view,&mut budget)?),(None,None)=>None,_=>return Err(RepositoryError::NotFound)};
        let head_owner=prior.as_ref().map(|v|v.owner.as_str()).unwrap_or(&r.authority.captured_owner);
        let segment=observation_phase_segment(&tx,&view,head_owner,prior.as_ref())?;
        let source_id=host_archive_source_id(&r.source)?;
        let root_renewal_id=paired_transition::renewal_id(&queued_binding(&r.transition),"root",&source_id,None)?;
        if segment.is_empty() {
            // Reversal restored the exact existing origin/head: renew the real
            // root under the exact head but do not fabricate an observation link.
            validate_observation_origin_custody(&tx,current,head_owner,&r.workspace_id,&r.source,&r.authority,&view,&mut budget)?;
            if !view.is_head_transition(&tx,&r.transition,&r.receipt_id,&r.receipt_hash)? {return Err(RepositoryError::NotFound);}
            let (root_id,active)=paired_transition::renew_root(&tx,&r.transition,&r.receipt_id,&r.receipt_hash,&r.workspace_id,&r.source,&r.fence_id,&r.policy_projection,&mut budget)?;
            if !active || root_id!=root_renewal_id {return Err(RepositoryError::NotFound);}
            drop(view);drop(budget);tx.commit()?;return Ok(json!({"version":3,"state":"restored","source_id":source_id,"revision":r.source.revision,"root_renewal_id":root_id,"rebind_id":r.authority.rebind_id,"rebind_sha256":r.authority.rebind_hash}));
        }
        if segment.first().is_none_or(|p|p.phase!=queued_binding(&r.transition) || p.receipt_id!=r.receipt_id || p.receipt_hash!=r.receipt_hash) {return Err(RepositoryError::NotFound);}
        let lineage=ObservationPhaseLineage{transitions:segment,root_renewal_id:root_renewal_id.clone()};
        let receipt=ObservationRebindReceipt{version:"s06.observation-rebind.v3".into(),origin_id:r.authority.origin_id.clone(),origin_hash:r.authority.origin_hash.clone(),captured_owner:r.authority.captured_owner.clone(),prior_receipt_id:r.authority.rebind_id.clone(),prior_receipt_hash:r.authority.rebind_hash.clone(),previous_owner:head_owner.into(),owner:current.clone(),workspace_id:r.workspace_id.clone(),account_id:r.source.account_id.clone(),source_id:source_id.clone(),revision:r.source.revision,content_hash:r.source.content_hash.clone(),operation_id:r.transition.operation_id.clone(),fence_proof_id:r.fence_id.clone(),previous_fingerprint:r.authority.current_fingerprint.clone(),current_fingerprint:r.current_fingerprint.clone(),phase_lineage:Some(lineage)};
        let id=observation_phase_rebind_id(&receipt)?;
        if let Some(hash)=tx.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id=?1 AND change_set_id=?2",params![current,id],|row|row.get::<_,String>(0)).optional()? {
            let existing=read_observation_rebind_custody(&tx,current,&id,&hash,&view,&mut budget)?;if existing!=receipt {return Err(RepositoryError::NotFound);}
            let head=observation_rebind_head_custody(&tx,current,current,&r.authority.origin_id,&r.authority.origin_hash,&r.authority.captured_owner,&r.workspace_id,&r.source,&view,&mut budget)?;
            if head.as_ref()!=Some(&(id.clone(),hash.clone())) {return Err(RepositoryError::NotFound);}
            drop(view);drop(budget);tx.commit()?;return Ok(json!({"version":3,"state":"already_applied","source_id":source_id,"revision":r.source.revision,"receipt_id":id,"receipt_sha256":hash}));
        }
        let outbox_exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE owner_id=?1 AND event_id=?2)",params![current,format!("outbox_{id}_s06_observation_rebound")],|row|row.get(0))?;if outbox_exists {return Err(RepositoryError::NotFound);}
        if !view.is_head_transition(&tx,&r.transition,&r.receipt_id,&r.receipt_hash)? {return Err(RepositoryError::NotFound);}
        validate_observation_origin_custody(&tx,current,head_owner,&r.workspace_id,&r.source,&r.authority,&view,&mut budget)?;
        let (root_renewal,active)=paired_transition::renew_root(&tx,&r.transition,&r.receipt_id,&r.receipt_hash,&r.workspace_id,&r.source,&r.fence_id,&r.policy_projection,&mut budget)?;
        if !active || root_renewal!=root_renewal_id {return Err(RepositoryError::NotFound);}
        let raw=serde_json::to_string(&receipt)?;if raw.len()>65536 {return Err(RepositoryError::NotFound);}budget.check(raw.len()).map_err(RepositoryError::Validation)?;
        let hash=sha256_hex(raw.as_bytes());let now=chrono::Utc::now().to_rfc3339();
        tx.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) VALUES(?1,?2,'automation','account_transition','s06.observation-rebind.v2',?3,?4,?5,?6,?7)",params![current,id,source_id,CONTRACT_VERSION,hash,now,raw])?;
        insert_outbox(&tx,current,&id,"s06_observation_rebound",&serde_json::to_value(&receipt)?,r.source.revision,&now)?;
        let checked=read_observation_rebind_custody(&tx,current,&id,&hash,&view,&mut budget)?;if checked!=receipt {return Err(RepositoryError::NotFound);}
        drop(view);drop(budget);tx.commit()?;Ok(json!({"version":3,"state":"renewed","source_id":source_id,"revision":r.source.revision,"receipt_id":id,"receipt_sha256":hash}))
    }

    pub(crate) fn phase_root_renew(&mut self,r:&PhaseRootRenewRequest)->Result<Value,RepositoryError> {
        if r.version!=1 {return Err(RepositoryError::NotFound);}
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut budget=ResetFenceBudget::install(&tx);
        let (id,active)=super::paired_transition::renew_root(&tx,&r.transition,&r.receipt_id,&r.receipt_hash,&r.workspace_id,&r.source,&r.fence_id,&r.policy_projection,&mut budget)?;
        drop(budget);tx.commit()?;Ok(json!({"version":1,"state":if active {"renewed"} else {"absent"},"receipt_id":id}))
    }

    pub(crate) fn phase_root_fence(&mut self,r:&PhaseRootFenceRequest)->Result<Value,RepositoryError> {
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        require_guarded_epoch(&tx)?;r.source.validate_in(&tx)?;
        if r.version!=1 || r.phase.account_id!=r.source.account_id || r.workspace_id.is_empty() || r.job_id.is_empty() || !valid_sha256(&r.policy_fingerprint) {return Err(RepositoryError::NotFound);}
        let mut budget=ResetFenceBudget::install(&tx);
        let length:Option<i64>=tx.query_row("SELECT length(CAST(r.source_ref_json AS BLOB)) FROM fm_v2_s05_job_refs r JOIN fm_v2_jobs j ON j.owner_id=r.owner_id AND j.job_id=r.job_id WHERE r.owner_id=?1 AND r.job_id=?2 AND j.workspace_key=?3 AND j.project_key='' AND j.kind IN ('s05_host_user_capture','completed_turn_review')",params![r.phase.owner,r.job_id,r.workspace_id],|row|row.get(0)).optional()?;
        let length=length.ok_or(RepositoryError::NotFound)?;if !(0..=8192).contains(&length) {return Err(RepositoryError::NotFound);}budget.check(length as usize).map_err(RepositoryError::Validation)?;
        let (raw,hash,target,account,source_workspace,policy):(String,String,String,String,Option<String>,String)=tx.query_row("SELECT r.source_ref_json,r.source_hash,r.target_workspace_id,r.source_account_id,r.source_workspace_id,r.policy_fingerprint FROM fm_v2_s05_job_refs r JOIN fm_v2_jobs j ON j.owner_id=r.owner_id AND j.job_id=r.job_id WHERE r.owner_id=?1 AND r.job_id=?2 AND j.workspace_key=?3 AND j.project_key='' AND j.kind IN ('s05_host_user_capture','completed_turn_review')",params![r.phase.owner,r.job_id,r.workspace_id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?)))?;
        let stored:HostArchiveSourceRef=serde_json::from_value(crate::field_authority::parse_unique_json(&raw).map_err(RepositoryError::Validation)?)?;
        if stored!=r.source || hash!=r.source.content_hash || target!=r.workspace_id || account!=r.source.account_id || source_workspace!=r.source.workspace_id || policy!=r.policy_fingerprint {return Err(RepositoryError::NotFound);}
        let (fence_id,active)=super::paired_transition::fence_root(&tx,&r.phase,&r.workspace_id,&r.source,&mut budget)?;
        drop(budget);tx.commit()?;Ok(json!({"version":1,"state":if active {"fenced"} else {"absent"},"fence_id":fence_id}))
    }
}
