//! Transaction owners for the optional offline-prepared pending-pair profile.
use super::*;
use super::paired_pending::{self,PendingPairFenceRequest,PendingPairRebindRequest};

fn require_pending_profile(tx:&Transaction<'_>)->Result<(),RepositoryError> {
    require_guarded_epoch(tx)?;
    let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.assistant_interpretation.pending_rebind.v1'",[],|r|r.get(0)).optional()?;
    if marker.as_deref()!=Some("1") {return Err(RepositoryError::NotFound);}
    Ok(())
}
impl<'a> V2Repository<'a> {
    pub(crate) fn paired_erased_fact(&mut self,r:&super::PairedErasedFactRequest)->Result<Value,RepositoryError> {
        if r.version!=1 || [&r.owner,&r.account_id,&r.workspace_id,&r.job_id].iter().any(|v|v.is_empty() || v.trim()!=v.as_str() || v.len()>512) {return Err(RepositoryError::NotFound)}
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;require_pending_profile(&tx)?;
        let mut budget=ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
        let view=owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,&r.owner,&r.owner,&mut budget)?;
        let value=crate::store::sqlite::SqliteStore::paired_erased_fact_in(&tx,&r.owner,&r.account_id,&r.workspace_id,&r.job_id,&view,&mut budget).map_err(RepositoryError::Validation)?;
        drop(view);drop(budget);tx.commit()?;Ok(value)
    }
    pub(crate) fn erased_pair_fence(&mut self,r:&PendingPairFenceRequest)->Result<Value,RepositoryError> {
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;require_pending_profile(&tx)?;
        let mut budget=ResetFenceBudget::install(&tx);let value=paired_pending::fence_erased_pair(&tx,r,&mut budget)?;
        drop(budget);tx.commit()?;Ok(value)
    }

    pub(crate) fn paired_publication_original(&mut self,request:&super::paired_publication::PairedPublicationStatusRequest)->Result<Value,RepositoryError> {
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;
        let mut budget=ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
        let keys=owner_custody::account_custody_keys(&tx,&database,&request.source.account_id,&mut budget)?;
        let result=if keys.is_empty() {super::paired_publication::original_status(&tx,request,None,&mut budget)?} else {
            let view=owner_custody::verified_custody_view(&tx,&database,&request.source.account_id,&request.owner,&request.owner,&mut budget)?;
            super::paired_publication::original_status(&tx,request,Some(&view),&mut budget)?
        };
        drop(budget);tx.commit()?;Ok(result)
    }

    pub(crate) fn terminal_pair_fence(&mut self,request:&PendingPairFenceRequest)->Result<Value,RepositoryError> {
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        require_pending_profile(&tx)?;
        let mut budget=ResetFenceBudget::install(&tx);
        let result=paired_pending::fence_terminal_pair(&tx,request,&mut budget)?;
        drop(budget);tx.commit()?;Ok(result)
    }
    pub(crate) fn pending_pair_fence(&mut self,request:&PendingPairFenceRequest)->Result<Value,RepositoryError> {
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        require_pending_profile(&tx)?;
        let mut budget=ResetFenceBudget::install(&tx);
        let result=paired_pending::fence_pending_pair(&tx,request,&mut budget)?;
        drop(budget);tx.commit()?;Ok(result)
    }
    pub(crate) fn pending_pair_original(&mut self,request:&PendingPairRebindRequest)->Result<Value,RepositoryError> {
        let tx=self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;
        require_pending_profile(&tx)?;
        let mut budget=ResetFenceBudget::install(&tx);
        let found={
            let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
            let view=owner_custody::verified_or_direct_custody_view(&tx,&database,&request.account_id,&request.owner,&request.owner,&mut budget)?;
            paired_pending::pending_pair_original(&tx,request,Some(&view),&mut budget)?
        };
        drop(budget);tx.commit()?;
        Ok(found.unwrap_or_else(||json!({"version":1,"state":"absent"})))
    }
    pub(crate) fn pending_pair_rebind(&mut self,request:&PendingPairRebindRequest)->Result<Value,RepositoryError> {
        crate::writer_fence::with_pair_pending_rebind_authority(self.conn,|conn| {
            let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
            require_pending_profile(&tx)?;
            let mut budget=ResetFenceBudget::install(&tx);
            let result={
                let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
                let view=owner_custody::verified_or_direct_custody_view(&tx,&database,&request.account_id,&request.owner,&request.owner,&mut budget)?;
                paired_pending::rebind_pending_pair(&tx,request,Some(&view),&mut budget)?
            };
            drop(budget);tx.commit()?;Ok(result)
        })
    }
}
