use async_trait::async_trait;
use tracing::{info, warn};

use crate::config::FmConfig;
use crate::curate;
use crate::embed::EmbeddingClient;
use crate::privacy::{content_hash, filter_json, filter_text};
use crate::provider::*;
use crate::record::*;
use crate::retrieval;
use crate::store::{sqlite::SqliteStore, MemoryStore};
use std::{sync::Arc, time::Duration};

#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct VectorProfileHandleRequest {original:crate::repository::document_vector::VectorProfileBuildRequest,handle:String}
#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct VectorProfileSearchRequest {original:crate::repository::document_vector::VectorProfileBuildRequest,profile_id:String,query:Vec<f32>,limit:usize,effort:usize,visit_budget:usize,candidate_limit:usize,selector:Option<Vec<String>>}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct SelectedVectorStart {original:crate::repository::document_vector::VectorProfileBuildRequest,policy:crate::repository::document_vector::VectorProfileSelectionPolicy}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct SelectedVectorHandle {original:crate::repository::document_vector::VectorProfileBuildRequest,policy:crate::repository::document_vector::VectorProfileSelectionPolicy,handle:String}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct SelectedVectorSearch {original:crate::repository::document_vector::VectorProfileBuildRequest,policy:crate::repository::document_vector::VectorProfileSelectionPolicy,profile_id:String,query:Vec<f32>,limit:usize,effort:usize,visit_budget:usize,candidate_limit:usize,selector:Option<Vec<String>>}
enum VectorProfileBuildState {Building,Ready(crate::repository::document_vector::VectorProfileArtifact),Failed}
struct VectorProfileBuildSlot {handle:String,original:String,profile_id:String,owner:String,account:String,expires:std::time::Instant,state:VectorProfileBuildState}

pub struct NativeProvider {
    store: Arc<dyn MemoryStore>,
    embed: Arc<dyn EmbeddingClient>,
    config: FmConfig,
    vector_profile_build: Arc<std::sync::Mutex<Option<VectorProfileBuildSlot>>>,
}

impl NativeProvider {
    pub fn s02_private(&self,phase:&str,request:&serde_json::Value,current:Option<&serde_json::Value>)->Result<serde_json::Value,String>{
        use crate::repository::s02_policy::{S02ApplyRequest,S02SnapshotRequest,S02ConfigRequest,S02OverrideRequest};
        let sqlite=self.store.as_sqlite().ok_or("S02 unavailable")?;let mut conn=sqlite.conn.lock().map_err(|_|"S02 unavailable")?;
        let mut repo=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        match phase {
            "capability"=>repo.s02_capability(),
            "override_capability"=>repo.s02_override_capability(),
            "override_apply"=>repo.s02_override_apply(&serde_json::from_value::<S02OverrideRequest>(request.clone()).map_err(|_|"S02 unavailable")?),
            "config"=>repo.s02_config(&serde_json::from_value::<S02ConfigRequest>(request.clone()).map_err(|_|"S02 unavailable")?),
            "selector"=>repo.s02_selector(&serde_json::from_value::<S02SnapshotRequest>(request.clone()).map_err(|_|"S02 unavailable")?),
            "snapshot"=>repo.read_s02_policy_snapshot(&serde_json::from_value::<S02SnapshotRequest>(request.clone()).map_err(|_|"S02 unavailable")?),
            "apply"=>repo.apply_s02_policy(&serde_json::from_value::<S02ApplyRequest>(request.clone()).map_err(|_|"S02 unavailable")?),
            _=>return Err("S02 unavailable".into()),
        }.map_err(|e|e.to_string())
    }
    pub fn s02_original_status(&self,phase:&str,request:&serde_json::Value,owner:&str,account:&str,current:&serde_json::Value)->Result<serde_json::Value,String>{
        use crate::repository::s02_policy::{S02ApplyRequest,S02OverrideRequest};
        let sqlite=self.store.as_sqlite().ok_or("S02 unavailable")?;let mut conn=sqlite.conn.lock().map_err(|_|"S02 unavailable")?;
        let mut repo=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        let refs=serde_json::from_value(current.clone()).map_err(|_|"S02 unavailable")?;
        match phase {
            "status"=>repo.s02_status(&serde_json::from_value::<S02ApplyRequest>(request.clone()).map_err(|_|"S02 unavailable")?,owner,account,&refs),
            "override_status"=>repo.s02_override_status(&serde_json::from_value::<S02OverrideRequest>(request.clone()).map_err(|_|"S02 unavailable")?,owner,account,&refs),
            _=>return Err("S02 unavailable".into()),
        }.map_err(|e|e.to_string())
    }
    pub fn s02_rename_assessment(&self,r:&crate::store::sqlite::GuardedS02RenameAssessment)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("S02 assessment unavailable")?;
        sqlite.guarded_pair_profile().map_err(|e|e.to_string())?;
        sqlite.guarded_s02_rename_assessment(r)
    }
    pub fn metadata_status(&self)->Result<serde_json::Value,String>{let sqlite=self.store.as_sqlite().ok_or("metadata unavailable")?;let mut conn=sqlite.conn.lock().map_err(|_|"metadata unavailable")?;crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.metadata_status().map_err(|e|e.to_string())}
    pub fn metadata_read(&self,r:&crate::repository::MetadataReadRequest,domains:bool)->Result<serde_json::Value,String>{let sqlite=self.store.as_sqlite().ok_or("metadata unavailable")?;let mut conn=sqlite.conn.lock().map_err(|_|"metadata unavailable")?;crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.metadata_read(r,domains).map_err(|e|e.to_string())}
    pub fn apply_manual_metadata(&self,r:&crate::repository::MetadataAssignmentApply)->Result<serde_json::Value,String>{let sqlite=self.store.as_sqlite().ok_or("metadata unavailable")?;let mut conn=sqlite.conn.lock().map_err(|_|"metadata unavailable")?;crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.apply_manual_metadata(r).map_err(|e|e.to_string())}
    pub fn domain_definition_status(&self)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("domain review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"domain review unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.domain_definition_status().map_err(|e|e.to_string())
    }
    pub fn review_domain_definition(&self, request:&crate::repository::DomainDefinitionReviewRequest)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("domain review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"domain review unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.review_domain_definition(request).map_err(|e|e.to_string())
    }
    pub fn prepared_knowledge_read(&self,request:&crate::repository::PreparedKnowledgeRead)->Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("prepared knowledge requires SQLite")?.prepared_knowledge_read(request)
    }
    /// Distinct source-free account-bound review; no model or background work.
    pub fn apply_account_candidate_review(&self,request:&crate::repository::AccountCandidateReviewRequest)->Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("candidate review requires SQLite")?.accept_account_candidate_transaction(request)
    }
    pub fn append_external_history_claim(&self, owner:&str,workspace:&str,claim:&crate::repository::ExternalHistoryClaim) -> Result<String,String> {
        let sqlite=self.store.as_sqlite().ok_or("external history requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"external history unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.append_external_history_claim(owner,workspace,claim).map_err(|e|e.to_string())
    }
    pub fn external_history_claim_page(&self, owner:&str,workspace:&str,after:Option<&str>,limit:usize) -> Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("external history requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"external history unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.external_history_claim_page(owner,workspace,after,limit).map_err(|e|e.to_string())
    }
    pub fn guarded_owner_purge(&self, request: &crate::store::sqlite::GuardedOwnerPurgeRequest) -> Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("guarded purge requires SQLite")?.guarded_purge_owner(request)
    }
    pub fn guarded_owner_purge_stamp(&self,request:&crate::store::sqlite::GuardedOwnerPurgeRequest)->Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("guarded purge requires SQLite")?.guarded_purge_stamp(request)
    }
    pub fn guarded_reset_stamp(&self,owner:&str,workspace:&str,account:&str,operation:&str,components:&[String],expected:&serde_json::Value,queued:bool,manifest:Option<&str>)->Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("guarded reset requires SQLite")?.guarded_reset_stamp(owner,workspace,account,operation,components,expected,queued,manifest)
    }
    pub fn guarded_owner_purge_status(&self, request: &crate::store::sqlite::GuardedOwnerPurgeRequest) -> Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("guarded purge requires SQLite")?.guarded_purge_status(request)
    }
    pub fn guarded_owner_rename(&self, request: &crate::store::sqlite::GuardedOwnerRenameRequest) -> Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("guarded rename requires SQLite")?.guarded_rename_owner(request)
    }
    pub fn guarded_owner_rename_cache_cleanup(&self,request:&crate::store::sqlite::GuardedOwnerRenameRequest)->Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("guarded cache cleanup unavailable")?.guarded_rename_cache_cleanup(request)
    }
    pub fn guarded_owner_purge_cache_cleanup(&self,request:&crate::store::sqlite::GuardedOwnerPurgeRequest)->Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("guarded cache cleanup unavailable")?.guarded_purge_cache_cleanup(request)
    }
    pub fn guarded_owner_rename_stamp(&self, request:&crate::store::sqlite::GuardedOwnerRenameRequest)->Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("guarded rename requires SQLite")?.guarded_rename_stamp(request)
    }
    pub fn guarded_owner_rename_status(&self, request: &crate::store::sqlite::GuardedOwnerRenameRequest) -> Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("guarded rename requires SQLite")?.guarded_rename_status(request)
    }
    pub fn guarded_reset_owner(&self, owner:&str, workspace:&str, account:&str, operation:&str, components:&[String], expected_counts:&serde_json::Value, queued_retired:bool) -> Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("guarded reset requires SQLite")?.guarded_reset_owner(owner,workspace,account,operation,components,expected_counts,queued_retired)
    }
    pub fn guarded_selective_ingest_reset(&self,owner:&str,workspace:&str,account:&str,operation:&str,components:&[String],expected_counts:&serde_json::Value,manifest:&str)->Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("guarded reset requires SQLite")?.guarded_selective_ingest_reset(owner,workspace,account,operation,components,expected_counts,manifest)
    }
    pub fn reconcile_guarded_reset_cache(&self, owner:&str, workspace:&str, account:&str, operation:&str, components:&[String], expected_counts:&serde_json::Value, queued_retired:bool, path:&str, device:u64, inode:u64, database_id:&str, selective_manifest:Option<&str>) -> Result<(),String> {
        self.store.as_sqlite().ok_or("guarded reset cleanup requires SQLite")?.reconcile_guarded_reset_cache(path,device,inode,database_id,owner,workspace,account,operation,components,expected_counts,queued_retired,selective_manifest)
    }
    #[allow(clippy::too_many_arguments)]
    pub fn lookup_host_candidate_receipt_status(&self,owner:&str,workspace:&str,candidate:&str,key:&str,digest:&str,head:i64,paths:&[String],reason:&str)->Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or("candidate receipt requires SQLite")?.lookup_host_candidate_receipt_status(owner,workspace,candidate,key,digest,head,paths,reason)
    }
    pub fn renew_observation_source(&self, request:&crate::repository::HostObservationRenewRequest) -> Result<crate::repository::HostObservationRenewReceipt,String> {
        let sqlite=self.store.as_sqlite().ok_or("host observation renewal requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"host source store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.renew_observation_source(request).map_err(|e|e.to_string())
    }
    pub fn qualified_curated_search(&self, owner:&str, workspace:&str, query:&str, limit:usize) -> Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("qualified search requires SQLite")?;
        let (results,refs)=sqlite.qualified_curated_fts(query,limit,owner,workspace)?;
        let total=results.len();
        Ok(serde_json::json!({"result":crate::record::SearchResult {results,total},"refs":refs}))
    }
    pub fn read_found_observation(&self, owner:&str, workspace:&str, event_id:&str, authority:&crate::repository::ObservationOriginAuthority) -> Result<crate::repository::FoundObservation,String> {
        let sqlite=self.store.as_sqlite().ok_or("found observation requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"found observation store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.read_found_observation(owner,workspace,event_id,authority).map_err(|e|e.to_string())
    }
    /// Execute the existing scoped physical erasure transaction on this store.
    /// The host caller remains responsible for its authenticated lifecycle guard.
    pub fn erase_source(&self, scope:&crate::ontology::ScopeRef, source_id:&str, actor:&crate::repository::RevisionActor) -> Result<crate::repository::PrivacyErasureResult,String> {
        let sqlite=self.store.as_sqlite().ok_or("source erasure requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"source erasure store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.erase_source(scope,source_id,actor).map_err(|e|e.to_string())
    }
    pub fn registered_connection_predicates(&self,account:&str,workspace:&str,policy:&crate::repository::PolicyPublicationRefs)->Result<serde_json::Value,String> {
        if account.is_empty() || account.trim()!=account || account.len()>512 || workspace.is_empty() || workspace.trim()!=workspace || workspace.len()>512 {return Err("connection registry unavailable".into());}
        let sqlite=self.store.as_sqlite().ok_or("connection registry unavailable")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"connection registry unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"connection registry unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|e|e.to_string())?;
        if cookie!=profile.0 {return Err("connection registry unavailable".into());}
        crate::repository::require_guarded_epoch(&tx).map_err(|_|"connection registry unavailable")?;
        crate::repository::require_policy_projection(&tx,account,workspace,Some(policy)).map_err(|_|"connection registry unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        let predicates=crate::repository::s02_connection::registered_connection_predicates(&tx,&mut budget).map_err(|_|"connection registry unavailable")?;
        drop(budget);tx.commit().map_err(|e|e.to_string())?;
        Ok(serde_json::json!({"version":1,"predicates":predicates}))
    }
    pub fn connection_review_manifest(&self,owner:&str,account:&str,workspace:&str,candidate:&str,policy:&crate::repository::PolicyPublicationRefs)->Result<serde_json::Value,String> {
        if [owner,account,workspace,candidate].iter().any(|s|s.is_empty() || s.trim()!=*s || s.len()>512) {return Err("connection review unavailable".into());}
        let sqlite=self.store.as_sqlite().ok_or("connection review unavailable")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"connection review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"connection review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|e|e.to_string())?;
        if cookie!=profile.0 {return Err("connection review unavailable".into());}
        crate::repository::require_guarded_epoch(&tx).map_err(|_|"connection review unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"connection review unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,account,owner,owner,&mut budget).map_err(|_|"connection review unavailable")?;
        let result=crate::repository::s02_connection::connection_review_manifest(&tx,owner,account,workspace,candidate,policy,&view,&mut budget).map_err(|_|"connection review unavailable")?;
        drop(view);drop(budget);tx.commit().map_err(|e|e.to_string())?;Ok(result)
    }
    pub fn s08_policy_review_manifest(&self,output_kind:&str,owner:&str,account:&str,workspace:&str,candidate:&str,policy:&crate::repository::PolicyPublicationRefs)->Result<serde_json::Value,String> {
        if [owner,account,workspace,candidate].iter().any(|s|s.is_empty() || s.trim()!=*s || s.len()>512) {return Err("connection review unavailable".into());}
        let sqlite=self.store.as_sqlite().ok_or("connection review unavailable")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"connection review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"connection review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|e|e.to_string())?;
        if cookie!=profile.0 {return Err("connection review unavailable".into());}
        crate::repository::require_guarded_epoch(&tx).map_err(|_|"connection review unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"connection review unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,account,owner,owner,&mut budget).map_err(|_|"connection review unavailable")?;
        let result=match output_kind {"connection"=>crate::repository::s02_connection::connection_review_manifest(&tx,owner,account,workspace,candidate,policy,&view,&mut budget),"summary"=>crate::repository::s08_policy_review::summary_manifest(&tx,owner,account,workspace,candidate,policy,&view,&mut budget),"journal_synthesis"=>crate::repository::s08_journal_review::journal_manifest(&tx,owner,account,workspace,candidate,policy,&view,&mut budget),_=>return Err("S08 review unavailable".into())}.map_err(|_|"S08 review unavailable")?;
        drop(view);drop(budget);tx.commit().map_err(|e|e.to_string())?;Ok(result)
    }
    pub fn s08_policy_review_reject_preview(&self,output_kind:&str,owner:&str,account:&str,workspace:&str,candidate:&str,policy:&crate::repository::PolicyPublicationRefs)->Result<serde_json::Value,String> {
        if [owner,account,workspace,candidate].iter().any(|s|s.is_empty() || s.trim()!=*s || s.len()>512) {return Err("connection review unavailable".into());}
        let sqlite=self.store.as_sqlite().ok_or("connection review unavailable")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"connection review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"connection review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|e|e.to_string())?;
        if cookie!=profile.0 {return Err("connection review unavailable".into());}
        crate::repository::require_guarded_epoch(&tx).map_err(|_|"connection review unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"connection review unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,account,owner,owner,&mut budget).map_err(|_|"connection review unavailable")?;
        let result=crate::repository::s08_policy_review::reject_preview(&tx,owner,account,workspace,candidate,output_kind,policy,&view,&mut budget).map_err(|_|"S08 review unavailable")?;
        drop(view);drop(budget);tx.commit().map_err(|e|e.to_string())?;Ok(result)
    }
    pub fn connection_review_original(&self,owner:&str,account:&str,workspace:&str,candidate:&str,candidate_sha256:Option<&str>,policy:&crate::repository::PolicyPublicationRefs)->Result<serde_json::Value,String> {
        if [owner,account,workspace,candidate].iter().any(|s|s.is_empty() || s.trim()!=*s || s.len()>512) {return Err("connection review unavailable".into());}
        let sqlite=self.store.as_sqlite().ok_or("connection review unavailable")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"connection review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"connection review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|e|e.to_string())?;
        if cookie!=profile.0 {return Err("connection review unavailable".into());}
        crate::repository::require_guarded_epoch(&tx).map_err(|_|"connection review unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"connection review unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,account,owner,owner,&mut budget).map_err(|_|"connection review unavailable")?;
        let result=crate::repository::s02_connection::connection_review_original(&tx,owner,account,workspace,candidate,candidate_sha256,policy,&view,&mut budget).map_err(|_|"connection review unavailable")?;
        drop(view);drop(budget);tx.commit().map_err(|e|e.to_string())?;Ok(result)
    }
    pub fn s08_policy_review_original(&self,output_kind:&str,owner:&str,account:&str,workspace:&str,candidate:&str,candidate_sha256:Option<&str>,policy:&crate::repository::PolicyPublicationRefs)->Result<serde_json::Value,String> {
        if [owner,account,workspace,candidate].iter().any(|s|s.is_empty() || s.trim()!=*s || s.len()>512) {return Err("connection review unavailable".into());}
        let sqlite=self.store.as_sqlite().ok_or("connection review unavailable")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"connection review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"connection review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|e|e.to_string())?;
        if cookie!=profile.0 {return Err("connection review unavailable".into());}
        crate::repository::require_guarded_epoch(&tx).map_err(|_|"connection review unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"connection review unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,account,owner,owner,&mut budget).map_err(|_|"connection review unavailable")?;
        let result=match output_kind {"connection"=>crate::repository::s02_connection::connection_review_original(&tx,owner,account,workspace,candidate,candidate_sha256,policy,&view,&mut budget),"summary"=>crate::repository::s08_policy_review::summary_original(&tx,owner,account,workspace,candidate,candidate_sha256,policy,&view,&mut budget),"journal_synthesis"=>crate::repository::s08_journal_review::journal_original(&tx,owner,account,workspace,candidate,candidate_sha256,policy,&view,&mut budget),_=>return Err("S08 review unavailable".into())}.map_err(|_|"S08 review unavailable")?;
        drop(view);drop(budget);tx.commit().map_err(|e|e.to_string())?;Ok(result)
    }
    pub fn s08_review_kind(&self,owner:&str,account:&str,workspace:&str,candidate:&str,policy:&crate::repository::PolicyPublicationRefs)->Result<serde_json::Value,String> {
        if [owner,account,workspace,candidate].iter().any(|s|s.is_empty() || s.trim()!=*s || s.len()>512) {return Err("connection review unavailable".into());}
        let sqlite=self.store.as_sqlite().ok_or("connection review unavailable")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"connection review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"connection review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|e|e.to_string())?;
        if cookie!=profile.0 {return Err("connection review unavailable".into());}
        crate::repository::require_guarded_epoch(&tx).map_err(|_|"connection review unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"connection review unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,account,owner,owner,&mut budget).map_err(|_|"connection review unavailable")?;
        let result=crate::repository::s02_connection::s08_review_kind(&tx,owner,account,workspace,candidate,policy,&view,&mut budget).map_err(|_|"connection review unavailable")?;
        drop(view);drop(budget);tx.commit().map_err(|e|e.to_string())?;Ok(result)
    }
    pub fn s08_original_scoped(&self,owner:&str,account:&str,workspace:&str,policy:&crate::repository::PolicyPublicationRefs,kind:&str,request:&serde_json::Value)->Result<serde_json::Value,String> {
        use crate::repository::{S08ModelPrepareRequest,S08ModelReserveRequest,S08ModelAttemptRequest,S08PolicyCommitRequest,S08PolicyTerminalRequest,V2Repository};
        if [owner,account,workspace].iter().any(|s|s.is_empty() || s.trim()!=*s || s.len()>512) {return Err("S08 original unavailable".into());}
        let sqlite=self.store.as_sqlite().ok_or("S08 original unavailable")?;let profile=sqlite.guarded_pair_profile().map_err(|_|"S08 original unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 original unavailable")?;let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|e|e.to_string())?;if cookie!=profile.0 {return Err("S08 original unavailable".into());}
        crate::repository::require_guarded_epoch(&tx).map_err(|_|"S08 original unavailable")?;
        crate::repository::require_policy_projection(&tx,account,workspace,Some(policy)).map_err(|_|"S08 original unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"S08 original unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,account,owner,owner,&mut budget).map_err(|_|"S08 original unavailable")?;
        let unavailable=||"S08 original unavailable".to_string();
        let result=match kind {
            "prepare"=>{let r:S08ModelPrepareRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;if r.selection.workspace_id!=workspace {return Err(unavailable());}V2Repository::s08_model_prepare_original_scoped(&tx,owner,account,&r,&view,&mut budget)},
            "reserve"=>{let r:S08ModelReserveRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;if r.original.selection.workspace_id!=workspace {return Err(unavailable());}V2Repository::s08_model_reserve_original_scoped(&tx,owner,account,&r,&view,&mut budget)},
            "attempt"=>{let r:S08ModelAttemptRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;if r.original.selection.workspace_id!=workspace {return Err(unavailable());}V2Repository::s08_model_attempt_original_scoped(&tx,owner,account,&r,&view,&mut budget)},
            "prepared_cancel"=>{let r:S08ModelPrepareRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;if r.selection.workspace_id!=workspace {return Err(unavailable());}if crate::repository::s08_policy_lifecycle::read_erased_prepared(&tx,owner,account,&r,&view,&mut budget).map_err(|_|unavailable())?.is_some() {Ok(serde_json::json!({"version":1,"state":"erased"}))} else {crate::repository::s08_policy_lifecycle::original_prepared_cancel(&tx,owner,account,&r,&view,&mut budget).map(|v|v.unwrap_or_else(||serde_json::json!({"version":1,"state":"unknown"}))) }},
            "terminal"=>{let r:S08PolicyTerminalRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;if r.attempt.original.selection.workspace_id!=workspace {return Err(unavailable());}if crate::repository::s08_policy_lifecycle::read_erased_known_terminal(&tx,owner,account,&r,&view,&mut budget).map_err(|_|unavailable())?.is_some() {Ok(serde_json::json!({"version":1,"state":"erased"}))} else {crate::repository::s08_policy_lifecycle::original_terminal(&tx,owner,account,&r,&view,&mut budget).map(|v|v.unwrap_or_else(||serde_json::json!({"version":1,"state":"unknown"}))) }},
            "result"=>{let r:S08PolicyCommitRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;if r.attempt.original.selection.workspace_id!=workspace {return Err(unavailable());}crate::repository::s08_policy_lifecycle::original_result(&tx,owner,account,&r,&view,&mut budget).map(|v|v.unwrap_or_else(||serde_json::json!({"version":1,"state":"unknown"})))},
            _=>return Err(unavailable()),
        }.map_err(|_|unavailable())?;
        drop(view);drop(budget);tx.commit().map_err(|e|e.to_string())?;Ok(result)
    }
    pub fn connection_review_apply(&self,owner:&str,workspace:&str,request:&serde_json::Value)->Result<serde_json::Value,String> {
        let r:crate::repository::s02_connection::ConnectionReviewRequest=serde_json::from_value(request.clone()).map_err(|_|"connection review unavailable")?;
        if r.owner!=owner || r.workspace_id!=workspace {return Err("connection review unavailable".into());}
        let sqlite=self.store.as_sqlite().ok_or("connection review unavailable")?;let profile=sqlite.guarded_pair_profile().map_err(|_|"connection review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"connection review unavailable")?;
        crate::writer_fence::with_domain_metadata_authority(&mut conn,|conn| {
            let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).map_err(|e|e.to_string())?;
            let cookie:i64=tx.query_row("PRAGMA schema_version",[],|row|row.get(0)).map_err(|e|e.to_string())?;if cookie!=profile.0 {return Err("connection review unavailable".into());}
            crate::repository::require_guarded_epoch(&tx).map_err(|_|"connection review unavailable")?;
            let mut budget=crate::repository::ResetFenceBudget::install(&tx);let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|"connection review unavailable")?;
            let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|"connection review unavailable")?;
            let result=crate::repository::s02_connection::accept_connection_review(&tx,&r,&view,&mut budget).map_err(|_|"connection review unavailable")?;
            drop(view);drop(budget);tx.commit().map_err(|e|e.to_string())?;Ok(result)
        })
    }
    pub fn s08_policy_review_apply(&self,output_kind:&str,owner:&str,workspace:&str,request:&serde_json::Value)->Result<serde_json::Value,String> {
        let r:crate::repository::s02_connection::ConnectionReviewRequest=serde_json::from_value(request.clone()).map_err(|_|"connection review unavailable")?;
        if r.owner!=owner || r.workspace_id!=workspace {return Err("connection review unavailable".into());}
        let sqlite=self.store.as_sqlite().ok_or("connection review unavailable")?;let profile=sqlite.guarded_pair_profile().map_err(|_|"connection review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"connection review unavailable")?;
        crate::writer_fence::with_domain_metadata_authority(&mut conn,|conn| crate::writer_fence::with_s02_policy_authority(conn,|conn| {
            let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).map_err(|e|e.to_string())?;
            let cookie:i64=tx.query_row("PRAGMA schema_version",[],|row|row.get(0)).map_err(|e|e.to_string())?;if cookie!=profile.0 {return Err("connection review unavailable".into());}
            crate::repository::require_guarded_epoch(&tx).map_err(|_|"connection review unavailable")?;
            let mut budget=crate::repository::ResetFenceBudget::install(&tx);let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|"connection review unavailable")?;
            let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|"connection review unavailable")?;
            let result=match (output_kind,r.action.as_str()) {("connection","accept")=>crate::repository::s02_connection::accept_connection_review(&tx,&r,&view,&mut budget),("summary","accept")=>crate::repository::s08_policy_review::accept_summary(&tx,&r,&view,&mut budget),("journal_synthesis","accept")=>crate::repository::s08_journal_review::accept_journal(&tx,&r,&view,&mut budget),("connection"|"summary"|"journal_synthesis","reject")=>crate::repository::s08_policy_review::reject_apply(&tx,&r,output_kind,&view,&mut budget),_=>return Err("S08 review unavailable".into())}.map_err(|_|"S08 review unavailable")?;
            drop(view);drop(budget);tx.commit().map_err(|e|e.to_string())?;Ok(result)
        }))
    }
    /// Private named-Human score context; account and policy are independently
    /// authenticated by the Host and rechecked in this held read transaction.
    pub fn s02_importance_context(&self,owner:&str,account:&str,workspace:&str,candidate:&str,policy:&crate::repository::PolicyPublicationRefs)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("importance review unavailable")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"importance review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"importance review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|_|"importance review unavailable")?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|_|"importance review unavailable")?;
        if cookie!=profile.0 {return Err("importance review unavailable".into());}
        crate::repository::require_policy_projection(&tx,account,workspace,Some(policy)).map_err(|_|"importance review unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"importance review unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,account,owner,owner,&mut budget).map_err(|_|"importance review unavailable")?;
        let result=crate::repository::s02_policy::importance_target_facts_in(&tx,owner,account,workspace,candidate,policy,&view,&mut budget).map_err(|_|"importance review unavailable")?;
        drop(view);drop(budget);tx.commit().map_err(|_|"importance review unavailable")?;Ok(result)
    }

    pub fn s02_importance_review_context(&self,owner:&str,account:&str,workspace:&str,candidate:&str,policy:&crate::repository::PolicyPublicationRefs)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("importance review unavailable")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"importance review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"importance review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|_|"importance review unavailable")?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|_|"importance review unavailable")?;
        if cookie!=profile.0 {return Err("importance review unavailable".into());}
        crate::repository::require_policy_projection(&tx,account,workspace,Some(policy)).map_err(|_|"importance review unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"importance review unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,account,owner,owner,&mut budget).map_err(|_|"importance review unavailable")?;
        let result=crate::repository::s08_policy_review::current_importance_context(&tx,owner,account,workspace,candidate,&view,&mut budget).map_err(|_|"importance review unavailable")?;
        drop(view);drop(budget);tx.commit().map_err(|_|"importance review unavailable")?;Ok(result)
    }

    pub fn s02_importance_resolve(&self,owner:&str,account:&str,workspace:&str,curated:Option<&str>,block:Option<&str>,revision:Option<i64>,policy:&crate::repository::PolicyPublicationRefs)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("importance review unavailable")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"importance review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"importance review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|_|"importance review unavailable")?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|_|"importance review unavailable")?;
        if cookie!=profile.0 {return Err("importance review unavailable".into());}
        crate::repository::require_policy_projection(&tx,account,workspace,Some(policy)).map_err(|_|"importance review unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"importance review unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,account,owner,owner,&mut budget).map_err(|_|"importance review unavailable")?;
        let result=match (curated,block,revision) {
            (Some(curated),None,None)=>crate::repository::s08_policy_review::current_importance_for_curated(&tx,owner,account,workspace,curated,&view,&mut budget),
            (None,Some(block),Some(revision))=>crate::repository::s02_policy::importance_target_for_block_in(&tx,owner,account,workspace,block,revision,policy,&view,&mut budget),
            _=>return Err("importance review unavailable".into()),
        }.map_err(|_|"importance review unavailable")?;
        drop(view);drop(budget);tx.commit().map_err(|_|"importance review unavailable")?;Ok(result)
    }

    pub fn s02_importance_target_manifest(&self,owner:&str,account:&str,workspace:&str,proposal:&str,policy:&crate::repository::PolicyPublicationRefs)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("importance review unavailable")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"importance review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"importance review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|_|"importance review unavailable")?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|_|"importance review unavailable")?;
        if cookie!=profile.0 {return Err("importance review unavailable".into());}
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"importance review unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,account,owner,owner,&mut budget).map_err(|_|"importance review unavailable")?;
        let result=crate::repository::s08_policy_review::importance_target_manifest(&tx,owner,account,workspace,proposal,policy,&view,&mut budget).map_err(|_|"importance review unavailable")?;
        drop(view);drop(budget);tx.commit().map_err(|_|"importance review unavailable")?;Ok(result)
    }

    pub fn s02_importance_original(&self,owner:&str,workspace:&str,policy:&crate::repository::PolicyPublicationRefs,request:&serde_json::Value)->Result<serde_json::Value,String> {
        let r:crate::repository::s08_policy_review::ImportanceReviewRequest=serde_json::from_value(request.clone()).map_err(|_|"importance review unavailable")?;
        if r.workspace_id!=workspace {return Err("importance review unavailable".into());}
        let sqlite=self.store.as_sqlite().ok_or("importance review unavailable")?;let profile=sqlite.guarded_pair_profile().map_err(|_|"importance review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"importance review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|_|"importance review unavailable")?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|row|row.get(0)).map_err(|_|"importance review unavailable")?;if cookie!=profile.0 {return Err("importance review unavailable".into());}
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        if !sqlite.s09_purge_profile_available_in(&tx).map_err(|_|"importance review unavailable")? {return Err("importance review unavailable".into());}
        if sqlite.s09_account_purge_positive_in(&tx,&r.account_id,&mut budget).map_err(|_|"importance review unavailable")? {drop(budget);tx.commit().map_err(|_|"importance review unavailable")?;return Ok(serde_json::json!({"version":1,"state":"account_purged"}));}
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|"importance review unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|"importance review unavailable")?;
        let result=crate::repository::s02_importance_once::status_in(&tx,owner,policy,&r,&view,&mut budget).map_err(|_|"importance review unavailable")?;
        drop(view);drop(budget);tx.commit().map_err(|_|"importance review unavailable")?;Ok(result)
    }

    /// Parse exactly as the Native owner does; this digest is a map key, not
    /// authority. The opaque permit is never serialized to an RPC caller.
    pub fn s02_importance_request_sha256(request:&serde_json::Value)->Result<String,String> {
        let r:crate::repository::s08_policy_review::ImportanceReviewRequest=serde_json::from_value(request.clone()).map_err(|_|"importance review unavailable")?;
        let raw=serde_json::to_vec(&r).map_err(|_|"importance review unavailable")?;
        if raw.len()>65536 {return Err("importance review unavailable".into())}
        Ok(crate::repository::sha256_hex(&raw))
    }
    /// Factual capability only. Account, current policy, custody and the exact
    /// original are checked again in each prepare/apply transaction.
    pub fn s02_importance_once_ready(&self)->Result<bool,String> {
        use rusqlite::OptionalExtension;
        let sqlite=self.store.as_sqlite().ok_or("importance review unavailable")?;
        let guarded=sqlite.guarded_pair_profile().map_err(|_|"importance review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"importance review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|_|"importance review unavailable")?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|_|"importance review unavailable")?;
        if cookie!=guarded.0 {return Err("importance review unavailable".into())}
        let marker:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.s02.importance.v1'",[],|r|r.get(0)).optional().map_err(|_|"importance review unavailable")?;
        let ready=match marker.as_deref() {
            None=>false,
            Some("1")=>{crate::repository::s02_policy::require_override_profile(&tx).map_err(|_|"importance review unavailable")?;sqlite.s09_purge_profile_available_in(&tx).map_err(|_|"importance review unavailable")?},
            _=>return Err("importance review unavailable".into()),
        };
        tx.commit().map_err(|_|"importance review unavailable")?;Ok(ready)
    }
    /// Persist one exact Human original. Only a newly committed original returns
    /// the opaque process-local permit; an existing original is status-only.
    pub fn s02_importance_prepare(&self,owner:&str,workspace:&str,request:&serde_json::Value)->Result<(serde_json::Value,Option<crate::repository::S02ImportancePermit>),String> {
        use crate::repository::s02_importance_once::PrepareOutcome;
        let r:crate::repository::s08_policy_review::ImportanceReviewRequest=serde_json::from_value(request.clone()).map_err(|_|"importance review unavailable")?;
        if r.owner!=owner || r.workspace_id!=workspace {return Err("importance review unavailable".into())}
        let sqlite=self.store.as_sqlite().ok_or("importance review unavailable")?;let profile=sqlite.guarded_pair_profile().map_err(|_|"importance review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"importance review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).map_err(|_|"importance review unavailable")?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|row|row.get(0)).map_err(|_|"importance review unavailable")?;if cookie!=profile.0 {return Err("importance review unavailable".into())}
        crate::repository::require_guarded_epoch(&tx).map_err(|_|"importance review unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        if !sqlite.s09_purge_profile_available_in(&tx).map_err(|_|"importance review unavailable")? || sqlite.s09_account_purge_positive_in(&tx,&r.account_id,&mut budget).map_err(|_|"importance review unavailable")? {return Err("importance review unavailable".into())}
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|"importance review unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|"importance review unavailable")?;
        let outcome=crate::repository::s02_importance_once::prepare_in(&tx,&r,&view,&mut budget).map_err(|_|"importance review unavailable")?;
        let (state,fact)=match &outcome {PrepareOutcome::Created(p)=>("created",p.original().clone()),PrepareOutcome::Existing(p)=>("existing",p.clone())};
        let result=serde_json::json!({"version":1,"state":state,"prepare_id":fact.prepare_id,"prepare_sha256":fact.prepare_sha256,"selector_sha256":fact.fact.selector_sha256,"request_sha256":fact.fact.request_sha256,"root_receipt_id":fact.fact.root_receipt_id,"root_sha256":fact.fact.root_sha256});
        drop(view);drop(budget);tx.commit().map_err(|_|"importance review unavailable")?;
        let permit=match outcome {PrepareOutcome::Created(p)=>Some(p.into_committed_permit()),PrepareOutcome::Existing(_)=>None};Ok((result,permit))
    }

    /// Private exact Human issue/replay on one original request; callers retain
    /// the normal Host owned-I/O ticket and account/source admission guards.
    pub fn s02_importance_apply(&self,owner:&str,workspace:&str,request:&serde_json::Value,permit:crate::repository::S02ImportancePermit)->Result<serde_json::Value,String> {
        let r:crate::repository::s08_policy_review::ImportanceReviewRequest=serde_json::from_value(request.clone()).map_err(|_|"importance review unavailable")?;
        if r.owner!=owner || r.workspace_id!=workspace {return Err("importance review unavailable".into());}
        let sqlite=self.store.as_sqlite().ok_or("importance review unavailable")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"importance review unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"importance review unavailable")?;
        crate::writer_fence::with_domain_metadata_authority(&mut conn,|conn| crate::writer_fence::with_s02_policy_authority(conn,|conn| {
            let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).map_err(|_|"importance review unavailable")?;
            let cookie:i64=tx.query_row("PRAGMA schema_version",[],|row|row.get(0)).map_err(|_|"importance review unavailable")?;
            if cookie!=profile.0 {return Err("importance review unavailable".into());}
            crate::repository::require_guarded_epoch(&tx).map_err(|_|"importance review unavailable")?;
            let mut budget=crate::repository::ResetFenceBudget::install(&tx);
            if !sqlite.s09_purge_profile_available_in(&tx).map_err(|_|"importance review unavailable")? || sqlite.s09_account_purge_positive_in(&tx,&r.account_id,&mut budget).map_err(|_|"importance review unavailable")? {return Err("importance review unavailable".into());}
            let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|row|row.get(0)).map_err(|_|"importance review unavailable")?;
            let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|"importance review unavailable")?;
            crate::repository::s02_importance_once::claim_in(&tx,&r,&view,&mut budget,permit).map_err(|_|"importance review unavailable")?;
            let result=crate::repository::s08_policy_review::apply_importance_review(&tx,&r,&view,&mut budget).map_err(|_|"importance review unavailable")?;
            drop(view);drop(budget);tx.commit().map_err(|_|"importance review unavailable")?;Ok(result)
        }))
    }

    /// Host-private only; the caller binds the real transport target and file guard.
    pub fn guarded_vector_profiles_capability(&self)->Result<bool,String>{
        use rusqlite::OptionalExtension;
        let unavailable=||"document vector profile unavailable".to_string();
        let sqlite=self.store.as_sqlite().ok_or_else(unavailable)?;let (cookie,_)=sqlite.guarded_pair_profile().map_err(|_|unavailable())?;
        let conn=sqlite.conn.lock().map_err(|_|unavailable())?;
        let observed:i64=conn.query_row("PRAGMA schema_version",[],|v|v.get(0)).map_err(|_|unavailable())?;
        let epoch:i64=conn.query_row("PRAGMA user_version",[],|v|v.get(0)).map_err(|_|unavailable())?;
        let marker:Option<String>=conn.query_row("SELECT value FROM fm_meta WHERE key='openclank.vector_profiles.v1'",[],|v|v.get(0)).optional().map_err(|_|unavailable())?;
        let fts:Option<String>=conn.query_row("SELECT value FROM fm_meta WHERE key='openclank.document_fts.v1'",[],|v|v.get(0)).optional().map_err(|_|unavailable())?;
        if observed!=cookie{return Err(unavailable())}
        match marker.as_deref(){None=>Ok(false),Some("1") if epoch==18&&fts.as_deref()==Some("1")=>Ok(true),_=>Err(unavailable())}
    }
    /// Private bounded build handle holds derived bytes only, never authority.
    pub fn document_vector_profile_private(&self,owner:&str,phase:&str,request:&serde_json::Value)->Result<serde_json::Value,String>{
        use crate::repository::{document_vector,owner_custody,ResetFenceBudget};
        let unavailable=||"document vector profile unavailable".to_string();
        let sqlite=self.store.as_sqlite().ok_or_else(unavailable)?;let (cookie,_)=sqlite.guarded_pair_profile().map_err(|_|unavailable())?;
        let mut normalized=request.clone();let mut selected_policy=None;
        let phase=match phase {
            "document_vector_selected_start"=>{let v:SelectedVectorStart=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;selected_policy=Some(v.policy);normalized=serde_json::to_value(v.original).map_err(|_|unavailable())?;"document_vector_profile_start"},
            "document_vector_selected_status"|"document_vector_selected_commit"=>{let v:SelectedVectorHandle=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;selected_policy=Some(v.policy);normalized=serde_json::to_value(VectorProfileHandleRequest{original:v.original,handle:v.handle}).map_err(|_|unavailable())?;if phase=="document_vector_selected_status"{"document_vector_profile_status"}else{"document_vector_profile_commit"}},
            "document_vector_selected_search"=>{let v:SelectedVectorSearch=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;selected_policy=Some(v.policy);normalized=serde_json::to_value(VectorProfileSearchRequest{original:v.original,profile_id:v.profile_id,query:v.query,limit:v.limit,effort:v.effort,visit_budget:v.visit_budget,candidate_limit:v.candidate_limit,selector:v.selector}).map_err(|_|unavailable())?;"document_vector_profile_search"},
            _=>phase,
        };
        let request=&normalized;
        if selected_policy.is_some() && !self.guarded_vector_profiles_capability()?{return Err(unavailable())}
        if phase=="document_vector_profile_search" {
            let r:VectorProfileSearchRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
            if r.original.owner!=owner{return Err(unavailable())}
            let mut conn=sqlite.conn.lock().map_err(|_|unavailable())?;let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|_|unavailable())?;
            let observed:i64=tx.query_row("PRAGMA schema_version",[],|v|v.get(0)).map_err(|_|unavailable())?;if observed!=cookie{return Err(unavailable())}
            let mut budget=ResetFenceBudget::install(&tx);let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|v|v.get(0)).map_err(|_|unavailable())?;
            let view=owner_custody::verified_or_direct_custody_view(&tx,&database,&r.original.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
            if let Some(policy)=selected_policy.as_ref(){document_vector::require_profile_selection(&tx,&r.original.account_id,policy,&r.original.choice,Some((r.limit,r.effort,r.visit_budget,r.candidate_limit)),&mut budget).map_err(|_|unavailable())?;}
            let result=document_vector::search_profile(&tx,&r.original,&r.profile_id,r.query,r.limit,r.effort,r.visit_budget,r.candidate_limit,r.selector,&view,&mut budget).map_err(|_|unavailable())?;
            drop(view);drop(budget);tx.commit().map_err(|_|unavailable())?;return Ok(result);
        }
        let (original,handle)=if phase=="document_vector_profile_start" {
            (serde_json::from_value::<document_vector::VectorProfileBuildRequest>(request.clone()).map_err(|_|unavailable())?,None)
        } else {
            let value:VectorProfileHandleRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
            if value.handle.len()!=36||!value.handle.starts_with("vph_")||!value.handle[4..].bytes().all(|b|b.is_ascii_hexdigit()&&!b.is_ascii_uppercase()) {return Err(unavailable())}
            (value.original,Some(value.handle))
        };
        if original.owner!=owner{return Err(unavailable())}
        let raw=serde_json::to_string(&original).map_err(|_|unavailable())?;
        if phase=="document_vector_profile_start" {
            let source={
                let mut conn=sqlite.conn.lock().map_err(|_|unavailable())?;
                let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|_|unavailable())?;
                let observed:i64=tx.query_row("PRAGMA schema_version",[],|v|v.get(0)).map_err(|_|unavailable())?;
                if observed!=cookie{return Err(unavailable())}
                let mut budget=ResetFenceBudget::install(&tx);let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|v|v.get(0)).map_err(|_|unavailable())?;
                let view=owner_custody::verified_or_direct_custody_view(&tx,&database,&original.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                if let Some(policy)=selected_policy.as_ref(){document_vector::require_profile_selection(&tx,&original.account_id,policy,&original.choice,None,&mut budget).map_err(|_|unavailable())?;}
                let source=document_vector::profile_source(&tx,&original,&view,&mut budget).map_err(|_|unavailable())?;
                drop(view);drop(budget);tx.commit().map_err(|_|unavailable())?;source
            };
            let mut slot=self.vector_profile_build.lock().map_err(|_|unavailable())?;
            if slot.as_ref().is_some_and(|v|v.expires<=std::time::Instant::now()){*slot=None;}
            if let Some(value)=slot.as_ref(){
                if value.original!=raw||value.owner!=owner||value.account!=original.account_id||value.profile_id!=source.profile_id{return Err(unavailable())}
                let state=match &value.state {VectorProfileBuildState::Building=>"building",VectorProfileBuildState::Ready(_)=>"built",VectorProfileBuildState::Failed=>"unavailable"};
                return Ok(serde_json::json!({"version":1,"state":state,"handle":value.handle,"profile_id":value.profile_id}));
            }
            let handle=format!("vph_{}",uuid::Uuid::new_v4().simple());
            let profile_id=source.profile_id.clone();
            *slot=Some(VectorProfileBuildSlot{handle:handle.clone(),original:raw,profile_id:profile_id.clone(),owner:owner.into(),account:original.account_id.clone(),expires:std::time::Instant::now()+Duration::from_secs(120),state:VectorProfileBuildState::Building});
            let shared=self.vector_profile_build.clone();let worker_handle=handle.clone();
            if std::thread::Builder::new().name("fm-vector-profile".into()).spawn(move ||{
                let built=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||document_vector::build_profile(source)));
                if let Ok(mut guard)=shared.lock() {if let Some(value)=guard.as_mut() {if value.handle==worker_handle && value.expires>std::time::Instant::now(){value.state=match built {Ok(Ok(result))=>VectorProfileBuildState::Ready(result),_=>VectorProfileBuildState::Failed};}}}
            }).is_err(){*slot=None;return Err(unavailable())}
            return Ok(serde_json::json!({"version":1,"state":"building","handle":handle,"profile_id":profile_id}));
        }
        let handle=handle.ok_or_else(unavailable)?;
        if phase=="document_vector_profile_status" {
            // Custody is freshly admitted; a status never builds or issues work.
            {let mut conn=sqlite.conn.lock().map_err(|_|unavailable())?;let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|_|unavailable())?;
                let observed:i64=tx.query_row("PRAGMA schema_version",[],|v|v.get(0)).map_err(|_|unavailable())?;if observed!=cookie{return Err(unavailable())}
                let marker:String=tx.query_row("SELECT value FROM fm_meta WHERE key='openclank.vector_profiles.v1'",[],|v|v.get(0)).map_err(|_|unavailable())?;
                if marker!="1"{return Err(unavailable())}
                let mut budget=ResetFenceBudget::install(&tx);let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|v|v.get(0)).map_err(|_|unavailable())?;
                let view=owner_custody::verified_or_direct_custody_view(&tx,&database,&original.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                if let Some(policy)=selected_policy.as_ref(){document_vector::require_profile_selection(&tx,&original.account_id,policy,&original.choice,None,&mut budget).map_err(|_|unavailable())?;}
                drop(view);drop(budget);tx.commit().map_err(|_|unavailable())?;}
            let mut slot=self.vector_profile_build.lock().map_err(|_|unavailable())?;if slot.as_ref().is_some_and(|v|v.expires<=std::time::Instant::now()){*slot=None;}
            let Some(value)=slot.as_ref() else {return Ok(serde_json::json!({"version":1,"state":"unknown"}))};
            if value.handle!=handle||value.original!=raw||value.owner!=owner||value.account!=original.account_id{return Err(unavailable())}
            let state=match &value.state {VectorProfileBuildState::Building=>"building",VectorProfileBuildState::Ready(_)=>"built",VectorProfileBuildState::Failed=>"unavailable"};
            return Ok(serde_json::json!({"version":1,"state":state,"handle":value.handle,"profile_id":value.profile_id}));
        }
        if phase!="document_vector_profile_commit" {return Err(unavailable())}
        let mut slot=self.vector_profile_build.lock().map_err(|_|unavailable())?;if slot.as_ref().is_some_and(|v|v.expires<=std::time::Instant::now()){*slot=None;}
        let value=slot.as_ref().ok_or_else(unavailable)?;
        if value.handle!=handle||value.original!=raw||value.owner!=owner||value.account!=original.account_id{return Err(unavailable())}
        let VectorProfileBuildState::Ready(artifact)=&value.state else{return Err(unavailable())};
        let result={let mut conn=sqlite.conn.lock().map_err(|_|unavailable())?;let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).map_err(|_|unavailable())?;
            let observed:i64=tx.query_row("PRAGMA schema_version",[],|v|v.get(0)).map_err(|_|unavailable())?;if observed!=cookie{return Err(unavailable())}
            let mut budget=ResetFenceBudget::install(&tx);let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|v|v.get(0)).map_err(|_|unavailable())?;
            let view=owner_custody::verified_or_direct_custody_view(&tx,&database,&original.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
            if let Some(policy)=selected_policy.as_ref(){document_vector::require_profile_selection(&tx,&original.account_id,policy,&original.choice,None,&mut budget).map_err(|_|unavailable())?;}
            let result=document_vector::commit_profile(&tx,&original,artifact,&view,&mut budget).map_err(|_|unavailable())?;
            if value.expires<=std::time::Instant::now(){return Err(unavailable())}
            drop(view);drop(budget);tx.commit().map_err(|_|unavailable())?;result};
        *slot=None;Ok(result)
    }

    pub fn document_structure_private(&self, owner: &str, request: crate::repository::DocumentStructureRequest)
        -> Result<serde_json::Value, String> {
        use rusqlite::TransactionBehavior;
        use crate::repository::{document_structure, owner_custody, ResetFenceBudget};
        let unavailable = || "document structure unavailable".to_string();
        let sqlite = self.store.as_sqlite().ok_or_else(unavailable)?;
        let cookie = sqlite.guarded_pair_profile().map_err(|_| unavailable())?.0;
        let mut conn = sqlite.conn.lock().map_err(|_| unavailable())?;
        let behavior = if request.writes() { TransactionBehavior::Immediate } else { TransactionBehavior::Deferred };
        let tx = conn.transaction_with_behavior(behavior).map_err(|_| unavailable())?;
        let observed: i64 = tx.query_row("PRAGMA schema_version", [], |row| row.get(0)).map_err(|_| unavailable())?;
        if observed != cookie { return Err(unavailable()); }
        crate::repository::require_guarded_epoch(&tx).map_err(|_| unavailable())?;
        let mut budget = ResetFenceBudget::install(&tx);
        let database: String = tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'", [], |row| row.get(0))
            .map_err(|_| unavailable())?;
        let view = owner_custody::verified_or_direct_custody_view(&tx, &database, request.account(), owner, owner, &mut budget)
            .map_err(|_| unavailable())?;
        let result = document_structure::run(&tx, owner, request, &view, &mut budget).map_err(|_| unavailable())?;
        drop(view);
        drop(budget);
        tx.commit().map_err(|_| unavailable())?;
        Ok(result)
    }

    pub fn document_upsert_private(&self, owner:&str, phase:&str, request:&serde_json::Value) -> Result<serde_json::Value,String> {
        use crate::repository::document_upsert::{DocumentOriginalRequest,DocumentUpsertRequest,DocumentSourceLookupRequest};
        let unavailable=|| "document upsert unavailable".to_string();
        let sqlite=self.store.as_sqlite().ok_or_else(unavailable)?;
        if !sqlite.guarded_document_fts_profile().map_err(|_|unavailable())? {return Err(unavailable());}
        let profile=sqlite.guarded_pair_profile().map_err(|_|unavailable())?;
        let mut conn=sqlite.conn.lock().map_err(|_|unavailable())?;
        let tx=conn.transaction_with_behavior(if matches!(phase,"document_upsert"|"document_remove"|"document_vector_begin"|"document_vector_append"|"document_vector_finalize"|"document_vector_intent_prepare"|"document_vector_intent_claim"|"document_vector_intent_cancel"|"document_vector_intent_begin"|"document_vector_intent_append"|"document_vector_intent_finalize"|"document_vector_batch_begin"|"document_vector_batch_claim"|"document_vector_batch_complete"|"document_vector_batch_finalize") {rusqlite::TransactionBehavior::Immediate} else {rusqlite::TransactionBehavior::Deferred}).map_err(|_|unavailable())?;
        let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|_|unavailable())?;
        if cookie!=profile.0 {return Err(unavailable());}
        crate::repository::require_guarded_epoch(&tx).map_err(|_|unavailable())?;
        let mut budget=crate::repository::ResetFenceBudget::install(&tx);
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|unavailable())?;
        let result=match phase {
            "document_upsert" => {
                let r:DocumentUpsertRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.owner!=owner || !r.workspace_id.is_empty() || !r.project_id.is_empty() {return Err(unavailable());}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,&r.captured_owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_upsert::upsert(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_remove" => {
                let r:crate::repository::document_remove::DocumentRemoveRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.owner!=owner {return Err(unavailable());}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,&r.captured_owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_remove::remove(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_remove_original" => {
                let r:DocumentOriginalRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.current_owner!=owner {return Err(unavailable());}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,&r.captured_owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_remove::original(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_vector_intent_prepare" => {
                let r:crate::repository::document_vector_intent::BuildIntent=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.captured_owner!=owner{return Err(unavailable())}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_vector_intent::prepare(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_vector_intent_claim" | "document_vector_intent_status" | "document_vector_intent_cancel" => {
                let r:crate::repository::document_vector_intent::BuildIdentity=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.owner!=owner{return Err(unavailable())}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                match phase {"document_vector_intent_claim"=>crate::repository::document_vector_intent::claim(&tx,&r,&view,&mut budget),"document_vector_intent_cancel"=>crate::repository::document_vector_intent::cancel_owned(&tx,&r,&view,&mut budget),_=>crate::repository::document_vector_intent::status(&tx,&r,&view,&mut budget)}.map_err(|_|unavailable())?
            },
            "document_vector_batch_begin" | "document_vector_batch_claim" | "document_vector_batch_status" | "document_vector_batch_finalize" => {
                let r:crate::repository::document_vector::DocumentVectorBuildOriginal=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.owner!=owner{return Err(unavailable())}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                match phase {
                    "document_vector_batch_begin"=>crate::repository::document_vector_batch::begin(&tx,&r,&view,&mut budget),
                    "document_vector_batch_claim"=>crate::repository::document_vector_batch::claim(&tx,&r,&view,&mut budget),
                    "document_vector_batch_status"=>crate::repository::document_vector_batch::status(&tx,&r,&view,&mut budget),
                    _=>crate::repository::document_vector_batch::finalize(&tx,&r,&view,&mut budget),
                }.map_err(|_|unavailable())?
            },
            "document_vector_batch_complete" => {
                let r:crate::repository::document_vector::DocumentVectorAppendRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.original.owner!=owner{return Err(unavailable())}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.original.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_vector_batch::complete(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_vector_intent_begin" | "document_vector_intent_finalize" => {
                let r:crate::repository::document_vector::DocumentVectorBuildOriginal=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.owner!=owner{return Err(unavailable())}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_vector_intent::require_stage(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?;
                if phase=="document_vector_intent_begin" {crate::repository::document_vector::stage_begin(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?} else {
                    let result=crate::repository::document_vector::stage_finalize(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?;
                    crate::repository::document_vector_intent::finish(&tx,&r,&result,&view,&mut budget).map_err(|_|unavailable())?;result
                }
            },
            "document_vector_intent_append" => {
                let r:crate::repository::document_vector::DocumentVectorAppendRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.original.owner!=owner{return Err(unavailable())}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.original.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_vector_intent::require_stage(&tx,&r.original,&view,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_vector::stage_append(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_vector_intent_text" => {
                let r:crate::repository::document_vector::DocumentVectorTextRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.original.owner!=owner{return Err(unavailable())}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.original.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_vector_intent::require_stage(&tx,&r.original,&view,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_vector::stage_text(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_vector_begin" | "document_vector_finalize" => {
                let r:crate::repository::document_vector::DocumentVectorBuildOriginal=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.owner!=owner {return Err(unavailable());}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                if phase=="document_vector_begin" {crate::repository::document_vector::stage_begin(&tx,&r,&view,&mut budget)} else {crate::repository::document_vector::stage_finalize(&tx,&r,&view,&mut budget)}.map_err(|_|unavailable())?
            },
            "document_vector_append" => {
                let r:crate::repository::document_vector::DocumentVectorAppendRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.original.owner!=owner {return Err(unavailable());}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.original.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_vector::stage_append(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_vector_search" => {
                let r:crate::repository::document_vector::DocumentVectorSearchRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.owner!=owner {return Err(unavailable());}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_vector::stage_search(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_vector_head" => {
                let r:crate::repository::DocumentVectorSnapshotRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.owner!=owner {return Err(unavailable());}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_vector::vector_head(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_vector_text" => {
                let r:crate::repository::document_vector::DocumentVectorTextRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.original.owner!=owner {return Err(unavailable());}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.original.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_vector::stage_text(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_vector_snapshot" => {
                let r:crate::repository::DocumentVectorSnapshotRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.owner!=owner {return Err(unavailable());}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_vector::snapshot_page(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_source_lookup" => {
                let r:DocumentSourceLookupRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.current_owner!=owner {return Err(unavailable());}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,&r.captured_owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_upsert::source_lookup(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            "document_upsert_original" => {
                let r:DocumentOriginalRequest=serde_json::from_value(request.clone()).map_err(|_|unavailable())?;
                if r.current_owner!=owner {return Err(unavailable());}
                let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,&r.account_id,owner,&r.captured_owner,&mut budget).map_err(|_|unavailable())?;
                crate::repository::document_upsert::original(&tx,&r,&view,&mut budget).map_err(|_|unavailable())?
            },
            _=>return Err(unavailable()),
        };
        drop(budget);tx.commit().map_err(|_|unavailable())?;Ok(result)
    }
    pub fn question_digest(&self,owner:&str,workspace:&str,include_global:bool,account:&str)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("question digest unavailable")?.s09_digest_for_account(owner,workspace,include_global,account)
    }

    pub fn owner_purge_stats(&self,owner:&str,account:&str)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("owner purge stats unavailable")?.owner_purge_stats(owner,account)
    }
    /// Closed private S05 transport; reservation remains gated by its owner.
    pub fn s05_reserve_private(&self,owner:&str,workspace:&str,phase:&str,request:&serde_json::Value)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("S05 reservation unavailable")?;let mut conn=sqlite.conn.lock().map_err(|_|"S05 reservation unavailable")?;
        let mut repo=crate::repository::V2Repository::new(&mut conn).map_err(|_|"S05 reservation unavailable")?;
        repo.s05_reserve_phase(sqlite,owner,workspace,phase,request).map_err(|_|"S05 reservation unavailable".into())
    }
    pub fn skill_draft_capability(&self,owner:&str,workspace:&str,account:&str)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("skill draft unavailable")?.skill_draft_capability(owner,workspace,account)
    }
    pub fn skill_draft_private(&self,owner:&str,workspace:&str,phase:&str,request:&serde_json::Value)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("skill draft unavailable")?;let mut conn=sqlite.conn.lock().map_err(|_|"skill draft unavailable")?;
        let mut repo=crate::repository::V2Repository::new(&mut conn).map_err(|_|"skill draft unavailable")?;
        repo.skill_phase(sqlite,owner,workspace,phase,request).map_err(|_|"skill draft unavailable".into())
    }
    pub fn s08_model_private(&self,phase:&str,request:&serde_json::Value)->Result<serde_json::Value,String> {
        use crate::repository::{S08ModelPrepareRequest,S08ModelReserveRequest,S08ModelAttemptRequest,S08PolicyCommitRequest,S08PolicyTerminalRequest};
        let sqlite=self.store.as_sqlite().ok_or("S08 model unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 model unavailable")?;
        if phase=="prepare" {
            let original=serde_json::from_value::<S08ModelPrepareRequest>(request.clone()).map_err(|_|"S08 model unavailable")?;
            if original.importance_target.is_some() {
                return crate::writer_fence::with_s02_policy_authority(&mut conn,|conn| {
                    let mut repo=crate::repository::V2Repository::new(conn).map_err(|e|e.to_string())?;
                    repo.prepare_s08_model(sqlite,&original).map(|job|serde_json::json!({"version":1,"job_id":job})).map_err(|_|"S08 model unavailable".into())
                });
            }
            let mut repo=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
            return repo.prepare_s08_model(sqlite,&original).map(|job|serde_json::json!({"version":1,"job_id":job})).map_err(|_|"S08 model unavailable".into());
        }
        let mut repo=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        match phase {
            "prepare_original"=>repo.s08_model_prepare_original(&serde_json::from_value::<S08ModelPrepareRequest>(request.clone()).map_err(|_|"S08 model unavailable")?),
            "reserve"=>repo.reserve_s08_model(sqlite,&serde_json::from_value::<S08ModelReserveRequest>(request.clone()).map_err(|_|"S08 model unavailable")?),
            "reserve_original"=>repo.s08_model_reserve_original(&serde_json::from_value::<S08ModelReserveRequest>(request.clone()).map_err(|_|"S08 model unavailable")?),
            "dispatch"=>repo.dispatch_s08_model(&serde_json::from_value::<S08ModelAttemptRequest>(request.clone()).map_err(|_|"S08 model unavailable")?),
            "original"=>repo.s08_model_original(&serde_json::from_value::<S08ModelAttemptRequest>(request.clone()).map_err(|_|"S08 model unavailable")?),
            "prepared_cancel"=>repo.cancel_s08_policy_prepared(&serde_json::from_value::<S08ModelPrepareRequest>(request.clone()).map_err(|_|"S08 model unavailable")?),
            "terminal"=>repo.settle_s08_policy_terminal(&serde_json::from_value::<S08PolicyTerminalRequest>(request.clone()).map_err(|_|"S08 model unavailable")?),
            "commit"=>repo.commit_s08_policy(&serde_json::from_value::<S08PolicyCommitRequest>(request.clone()).map_err(|_|"S08 model unavailable")?),
            "result_original"=>repo.s08_policy_result_original(&serde_json::from_value::<S08PolicyCommitRequest>(request.clone()).map_err(|_|"S08 model unavailable")?),
            _=>return Err("S08 model unavailable".into()),
        }.map_err(|_|"S08 model unavailable".into())
    }
    /// Read-only status of one exact journal selection (`journal_delta_status`); it never reserves, dispatches or publishes.
    pub fn journal_delta_status(&self,request:&crate::repository::JournalDeltaStatusRequest)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("S08 model unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 model unavailable")?;
        let mut repo=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repo.journal_delta_status(request).map_err(|_|"S08 model unavailable".into())
    }
    pub fn s08_policy_capability(&self,owner:&str)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("S08 capability unavailable")?;
        let guarded=sqlite.guarded_pair_profile().map_err(|_|"S08 capability unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 capability unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        let mut result=repository.s08_policy_capability(owner).map_err(|_|"S08 capability unavailable")?;
        drop(repository);
        if result["outputs"].as_array().is_some_and(|v|v.iter().any(|x|x=="importance_target")) {
            let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|_|"S08 capability unavailable")?;
            let cookie:i64=tx.query_row("PRAGMA schema_version",[],|r|r.get(0)).map_err(|_|"S08 capability unavailable")?;
            if cookie!=guarded.0 {return Err("S08 capability unavailable".into())}
            let ready=sqlite.s09_purge_profile_available_in(&tx).map_err(|_|"S08 capability unavailable")?;
            tx.commit().map_err(|_|"S08 capability unavailable")?;
            if !ready {result["outputs"].as_array_mut().ok_or("S08 capability unavailable")?.retain(|v|v!="importance_target");}
        }
        Ok(result)
    }
    pub fn s08_policy_snapshots(&self,selection:&crate::repository::S08RulesSelection,policy:&crate::repository::PolicyPublicationRefs)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("S08 policy unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 policy unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.s08_policy_snapshots(selection,policy).map_err(|e|e.to_string())
    }
    pub fn enqueue_s08_rules_selection(&self, request:&crate::repository::S08RulesSelection) -> Result<String,String> {
        let sqlite=self.store.as_sqlite().ok_or("S08 selection requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 selection store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.enqueue_s08_rules_selection(request,None).map_err(|e|e.to_string())
    }
    pub fn reserve_s08_rules(&self, request:&crate::repository::S08RulesReservation) -> Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("S08 reservation requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 reservation store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.reserve_s08_rules(sqlite,request).map_err(|e|e.to_string())
    }
    pub fn revoke_s08_rules(&self, owner:&str, attempt:&str, epoch:i64) -> Result<(),String> {
        let sqlite=self.store.as_sqlite().ok_or("S08 reservation requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 reservation store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.revoke_s08_rules(owner,attempt,epoch).map_err(|e|e.to_string())
    }
    pub fn run_s08_rules(&self, request:&crate::repository::S08RulesRun) -> Result<String,String> {
        let sqlite=self.store.as_sqlite().ok_or("S08 rules require SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 rules store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.run_s08_rules(request).map_err(|e|e.to_string())
    }
    #[doc(hidden)]
    pub fn s08_rules_once(&self,request:&crate::repository::S08RulesOnce)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("S08 rules require SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 rules store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.s08_rules_once(sqlite,request).map_err(|e|e.to_string())
    }
    /// Host-internal transaction boundary; the caller must authenticate human
    /// review and hold all contributor guards. It has no MCP dispatch.
    #[doc(hidden)]
    pub fn policy_publication_status(&self,owner:&str)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("policy publication requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"policy publication store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.policy_publication_status(owner).map_err(|e|e.to_string())
    }
    pub fn accept_s08_rules(&self,owner:&str,workspace:&str,candidate:&str,digest:&str,key:&str,reason:&str,policy:Option<&crate::repository::PolicyPublicationRefs>)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("S08 review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 review store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.accept_s08_rules(owner,workspace,candidate,digest,key,reason,policy).map_err(|e|e.to_string())
    }
    #[doc(hidden)]
    pub fn preview_s08_review(&self,owner:&str,workspace:&str,candidate:&str)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("S08 review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 review store unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let result=crate::repository::preview_s08_review(&tx,owner,workspace,candidate).map_err(|e|e.to_string())?;
        tx.commit().map_err(|e|e.to_string())?;Ok(result)
    }
    /// Internal boundary; the host must prove origin-ready and hold the source guard.
    #[doc(hidden)]
    pub fn append_derived_compaction(&self,request:&crate::repository::DerivedCompactionRequest)->Result<String,String>{
        let sqlite=self.store.as_sqlite().ok_or("maintenance artifact requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"maintenance store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.append_derived_compaction(request).map_err(|e|e.to_string())
    }
    #[doc(hidden)]
    pub fn s08_review_receipt_status(&self,owner:&str,workspace:&str,candidate:&str,digest:&str,key:&str,reason:&str)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("S08 review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"S08 review store unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let result=crate::repository::s08_review_receipt_status(&tx,owner,workspace,candidate,digest,key,reason).map_err(|e|e.to_string())?;
        tx.commit().map_err(|e|e.to_string())?;Ok(result)
    }
    pub fn consolidation_admissions_page(&self, owner:&str, workspace:&str, source:&crate::repository::HostArchiveSourceRef, authority:&crate::repository::ObservationOriginAuthority, after:Option<&str>, limit:usize) -> Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("consolidation evidence requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"consolidation evidence store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.consolidation_admissions_page(owner,workspace,source,authority,after,limit).map_err(|e|e.to_string())
    }
    /// Internal scaffolds; no generic or private MCP phase currently dispatches these.
    #[doc(hidden)]
    pub fn helped_capability_unexposed(&self, owner:&str)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("helped unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"helped unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.helped_capability_unexposed(owner).map_err(|e|e.to_string())
    }
    pub fn found_features_capability(&self, owner:&str)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("found features unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"found features unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.found_features_capability(owner).map_err(|e|e.to_string())
    }
    pub fn found_features_page(&self, request:&crate::repository::HelpedSelectorRequest)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("found features unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"found features unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.found_features_page(request).map_err(|e|e.to_string())
    }
    #[doc(hidden)]
    pub fn helped_selector_unexposed(&self, owner:&str, workspace:&str, account:&str, source:&crate::repository::HostArchiveSourceRef, authority:&crate::repository::ObservationOriginAuthority, after:Option<&str>, limit:usize)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("helped unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"helped unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.helped_selector_unexposed(owner,workspace,account,source,authority,after,limit).map_err(|e|e.to_string())
    }
    #[doc(hidden)]
    pub fn append_helped_unexposed(&self, request:&crate::repository::HelpedObservation)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("helped unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"helped unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.append_helped_unexposed(request).map_err(|e|e.to_string())
    }
    #[doc(hidden)]
    pub fn helped_status_unexposed(&self, request:&crate::repository::HelpedObservation, authority:&crate::repository::ObservationOriginAuthority)->Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("helped unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"helped unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.helped_status_unexposed(request,authority).map_err(|e|e.to_string())
    }
    pub fn export_observation_page(&self, owner:&str, workspace:&str, source:&crate::repository::HostArchiveSourceRef, authority:&crate::repository::ObservationOriginAuthority, kind:&str, after:Option<&str>, limit:usize) -> Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("observation export requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"observation export store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.export_observation_page(owner,workspace,source,authority,kind,after,limit).map_err(|e|e.to_string())
    }
    pub fn append_admitted_observation(&self, request:&crate::repository::AdmittedObservation) -> Result<String,String> {
        let sqlite=self.store.as_sqlite().ok_or("admitted observation requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"admitted observation store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.append_admitted_observation(request).map_err(|e|e.to_string())
    }
    pub fn read_admitted_observation(&self, owner:&str, workspace:&str, event_id:&str, authority:&crate::repository::ObservationOriginAuthority) -> Result<crate::repository::AdmittedObservation,String> {
        let sqlite=self.store.as_sqlite().ok_or("admitted observation requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"admitted observation store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.read_admitted_observation(owner,workspace,event_id,authority).map_err(|e|e.to_string())
    }
    pub fn append_context_admitted_observation(&self, request:&crate::repository::ContextAdmittedObservation) -> Result<String,String> {
        let sqlite=self.store.as_sqlite().ok_or("admitted observation requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"admitted observation store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.append_context_admitted_observation(request).map_err(|e|e.to_string())
    }
    pub fn read_context_admitted_observation(&self, owner:&str, workspace:&str, event_id:&str, authority:&crate::repository::ObservationOriginAuthority) -> Result<crate::repository::ContextAdmittedObservation,String> {
        let sqlite=self.store.as_sqlite().ok_or("admitted observation requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"admitted observation store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.read_context_admitted_observation(owner,workspace,event_id,authority).map_err(|e|e.to_string())
    }
    pub fn append_found_observation(&self, request:&crate::repository::FoundObservation) -> Result<String,String> {
        let sqlite=self.store.as_sqlite().ok_or("found observation requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"found observation store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.append_found_observation(request).map_err(|e|e.to_string())
    }
    pub fn reset_fence_host_source(&self, owner:&str, workspace:&str, job_id:&str, source:&crate::repository::HostArchiveSourceRef, policy:&str, operation:&str, reset:&crate::repository::ResetFenceBinding) -> Result<crate::repository::HostArchiveProjectionReceipt,String> {
        let sqlite=self.store.as_sqlite().ok_or("host source fence requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"host source store unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&conn);
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.reset_fence_host_source(owner,workspace,job_id,source,policy,operation,reset,&mut budget).map_err(|e|e.to_string())
    }
    pub fn reset_fence_observation_origin(&self, owner:&str, workspace:&str, authority:&crate::repository::ObservationOriginAuthority, source:&crate::repository::HostArchiveSourceRef, operation:&str, reset:&crate::repository::ResetFenceBinding) -> Result<crate::repository::HostArchiveProjectionReceipt,String> {
        let sqlite=self.store.as_sqlite().ok_or("host observation fence requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"host source store unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&conn);
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.reset_fence_observation_origin(owner,workspace,authority,source,operation,reset,&mut budget).map_err(|e|e.to_string())
    }
    pub fn read_reset_host_fence(&self, owner:&str, workspace:&str, job:Option<(&str,&str)>, authority:Option<&crate::repository::ObservationOriginAuthority>, source:&crate::repository::HostArchiveSourceRef, reset:&crate::repository::ResetFenceBinding) -> Result<crate::repository::HostArchiveProjectionReceipt,String> {
        let sqlite=self.store.as_sqlite().ok_or("host fence proof requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"host fence proof unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&conn);
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.read_reset_host_fence(owner,workspace,job,authority,source,reset,&mut budget).map_err(|e|e.to_string())
    }
    pub fn fence_observation_origin(&self, owner:&str, workspace:&str, authority:&crate::repository::ObservationOriginAuthority,
        source:&crate::repository::HostArchiveSourceRef, rename_operation_id:Option<&str>)
        -> Result<crate::repository::HostArchiveProjectionReceipt,String> {
        let sqlite=self.store.as_sqlite().ok_or("host observation fence requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"host source store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.fence_observation_origin(owner,workspace,authority,source,rename_operation_id).map_err(|e|e.to_string())
    }
    pub fn renew_host_archive_source(&self, request:&crate::repository::HostArchiveRenewRequest) -> Result<crate::repository::HostArchiveRenewReceipt,String> {
        let sqlite=self.store.as_sqlite().ok_or("host source renewal requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"host source store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.renew_host_archive_source(request).map_err(|e|e.to_string())
    }
    /// Host-private S05 proposal; caller supplies fresh guarded host authority.
    pub fn stage_host_archive_candidate(&self, request:&crate::repository::HostArchiveCandidateRequest) -> Result<String,String> {
        let sqlite=self.store.as_sqlite().ok_or("host candidate requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"host candidate store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.stage_host_archive_candidate(request).map_err(|e|e.to_string())
    }
    pub fn source_publication_original(&self,request:&crate::repository::HostSourcePublicationOriginalRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("host publication requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"host publication unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.source_publication_original(request).map_err(|e|e.to_string())
    }
    /// Host-private paired proposal; caller owns source/attempt currentness.
    pub fn stage_assistant_interpretation_candidate(&self,request:&crate::repository::AssistantInterpretationCandidateRequest)->Result<String,String>{
        let sqlite=self.store.as_sqlite().ok_or("paired candidate requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired candidate unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.stage_assistant_interpretation_candidate(request).map_err(|e|e.to_string())
    }
    /// Host owns the live transition claim and both contributor guards.
    pub fn paired_rename_fence(&self,request:&crate::repository::PairedRenameFenceRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("paired rename requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired rename unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.paired_rename_fence(request).map_err(|e|e.to_string())
    }
    pub fn paired_rename_renew(&self,request:&crate::repository::PairedRenameRenewRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("paired rename requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired rename unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.paired_rename_renew(request).map_err(|e|e.to_string())
    }
    /// Host holds the exact transition claim and both live contributor guards.
    pub fn phase_root_observation_fence(&self,request:&crate::repository::PhaseRootObservationFenceRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("phase observation requires SQLite")?;
        sqlite.guarded_pair_profile().map_err(|e|e.to_string())?;
        let mut conn=sqlite.conn.lock().map_err(|_|"phase observation unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.phase_root_observation_fence(request).map_err(|e|e.to_string())
    }
    pub fn phase_root_observation_renew(&self,request:&crate::repository::PhaseRootObservationRenewRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("phase observation requires SQLite")?;
        sqlite.guarded_pair_profile().map_err(|e|e.to_string())?;
        let mut conn=sqlite.conn.lock().map_err(|_|"phase observation unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.phase_root_observation_renew(request).map_err(|e|e.to_string())
    }
    pub fn phase_root_renew(&self,request:&crate::repository::PhaseRootRenewRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("phase root requires SQLite")?;
        sqlite.guarded_pair_profile().map_err(|e|e.to_string())?;
        let mut conn=sqlite.conn.lock().map_err(|_|"phase root unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.phase_root_renew(request).map_err(|e|e.to_string())
    }
    pub fn phase_root_fence(&self,request:&crate::repository::PhaseRootFenceRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("phase root requires SQLite")?;
        sqlite.guarded_pair_profile().map_err(|e|e.to_string())?;
        let mut conn=sqlite.conn.lock().map_err(|_|"phase root unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.phase_root_fence(request).map_err(|e|e.to_string())
    }
    pub fn paired_erased_fact(&self,request:&crate::repository::PairedErasedFactRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("paired erased fact requires SQLite")?;sqlite.guarded_pair_profile().map_err(|e|e.to_string())?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired erased fact unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.paired_erased_fact(request).map_err(|e|e.to_string())
    }
    pub fn erased_pair_fence(&self,request:&crate::repository::PendingPairFenceRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("erased pair requires SQLite")?;sqlite.guarded_pair_profile().map_err(|e|e.to_string())?;
        let mut conn=sqlite.conn.lock().map_err(|_|"erased pair unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.erased_pair_fence(request).map_err(|e|e.to_string())
    }
    pub fn terminal_pair_fence(&self,request:&crate::repository::PendingPairFenceRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("terminal pair requires SQLite")?;
        sqlite.guarded_pair_profile().map_err(|e|e.to_string())?;
        let mut conn=sqlite.conn.lock().map_err(|_|"terminal pair unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.terminal_pair_fence(request).map_err(|e|e.to_string())
    }
    pub fn pending_pair_fence(&self,request:&crate::repository::PendingPairFenceRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("pending pair requires SQLite")?;
        sqlite.guarded_pair_profile().map_err(|e|e.to_string())?;
        let mut conn=sqlite.conn.lock().map_err(|_|"pending pair unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.pending_pair_fence(request).map_err(|e|e.to_string())
    }
    pub fn paired_publication_original(&self,request:&crate::repository::PairedPublicationStatusRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("paired publication requires SQLite")?;
        sqlite.guarded_pair_profile().map_err(|e|e.to_string())?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired publication unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.paired_publication_original(request).map_err(|e|e.to_string())
    }
    pub fn pending_pair_original(&self,request:&crate::repository::PendingPairRebindRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("pending pair requires SQLite")?;
        sqlite.guarded_pair_profile().map_err(|e|e.to_string())?;
        let mut conn=sqlite.conn.lock().map_err(|_|"pending pair unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.pending_pair_original(request).map_err(|e|e.to_string())
    }
    pub fn pending_pair_rebind(&self,request:&crate::repository::PendingPairRebindRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("pending pair requires SQLite")?;
        sqlite.guarded_pair_profile().map_err(|e|e.to_string())?;
        let mut conn=sqlite.conn.lock().map_err(|_|"pending pair unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.pending_pair_rebind(request).map_err(|e|e.to_string())
    }
    /// Private paired review selectors; the Host reauthorizes both contributors.
    pub fn paired_review_page(&self, owner:&str,workspace:&str,after:Option<&str>,limit:u32)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("paired review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let value=crate::repository::host_paired_review_page(&tx,owner,workspace,after,limit).map_err(|e|e.to_string())?;
        tx.commit().map_err(|e|e.to_string())?; Ok(value)
    }
    pub fn paired_review_preview(&self, owner:&str,workspace:&str,candidate:&str)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("paired review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let value=crate::repository::host_paired_review_preview(&tx,owner,workspace,candidate).map_err(|e|e.to_string())?;
        tx.commit().map_err(|e|e.to_string())?; Ok(value)
    }
    pub fn paired_review_original_page(&self,owner:&str,workspace:&str,account:&str,after:Option<&str>,limit:u32)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("paired review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let value=crate::repository::paired_review_original_page(&tx,owner,workspace,account,after,limit).map_err(|e|e.to_string())?;
        tx.commit().map_err(|e|e.to_string())?; Ok(value)
    }
    pub fn paired_review_original(&self,owner:&str,workspace:&str,account:&str,candidate:&str)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("paired review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let value=crate::repository::paired_review_original(&tx,owner,workspace,account,candidate).map_err(|e|e.to_string())?;
        tx.commit().map_err(|e|e.to_string())?; Ok(value)
    }
    /// Exact private review and original receipt; Host retains human authority.
    pub fn paired_review_apply(&self, request:&crate::repository::AssistantInterpretationReviewRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("paired review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired review unavailable")?;
        let reserve=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).map_err(|e|e.to_string())?;
        let created=crate::repository::paired_review_reserve(&reserve,request).map_err(|e|e.to_string())?;
        reserve.commit().map_err(|e|e.to_string())?;
        if !created {
            let read=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
            let value=crate::repository::paired_review_status(&read,request).map_err(|e|e.to_string())?;
            read.commit().map_err(|e|e.to_string())?; return Ok(value);
        }
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).map_err(|e|e.to_string())?;
        let value=crate::repository::paired_review_apply(&tx,request).map_err(|e|e.to_string())?;
        tx.commit().map_err(|e|e.to_string())?; Ok(value)
    }
    pub fn paired_review_status(&self, request:&crate::repository::AssistantInterpretationReviewRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("paired review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired review unavailable")?;
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        let value=crate::repository::paired_review_status(&tx,request).map_err(|e|e.to_string())?;
        tx.commit().map_err(|e|e.to_string())?; Ok(value)
    }
    pub fn guarded_full_memory_preview(&self,owner:&str,workspace:&str,account:&str,components:&[String])->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("full memory preview requires SQLite")?.guarded_full_memory_preview(owner,workspace,account,components)
    }
    pub fn guarded_selective_ingest_preview(&self,owner:&str,workspace:&str,account:&str,components:&[String])->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("selective ingest requires SQLite")?.guarded_selective_ingest_preview(owner,workspace,account,components)
    }

    /// Read-only selective partition; the Host holds and rechecks its reset claim.
    pub fn selective_ingest_partition(&self,owner:&str,account:&str,expected:&str)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("selective ingest requires SQLite")?;
        let profile=sqlite.guarded_pair_profile().map_err(|_|"selective ingest profile unavailable")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"selective ingest unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&conn);
        let tx=conn.transaction_with_behavior(rusqlite::TransactionBehavior::Deferred).map_err(|e|e.to_string())?;
        crate::store::sqlite::SqliteStore::selective_ingest_lifecycle_preflight(&tx,owner,Some(account),false,&mut budget)?;
        let mut selection=crate::repository::classify_s05_ingest(&tx,owner,Some(account),&mut budget).map_err(|_|"selective ingest unavailable")?;
        let database:String=tx.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0)).map_err(|_|"selective ingest unavailable")?;
        let view=crate::repository::owner_custody::verified_or_direct_custody_view(&tx,&database,account,owner,owner,&mut budget).map_err(|_|"selective ingest unavailable")?;
        crate::store::sqlite::SqliteStore::bind_selective_candidate_import(&tx,owner,account,&mut selection,&mut budget,&view)?;
        let question_groups=crate::repository::s09_question_inventory_in(&tx,&view,&mut budget).map_err(|_|"question reset inventory unavailable")?;
        crate::store::sqlite::SqliteStore::bind_selective_question_inventory(&mut selection,&question_groups,&mut budget)?;
        drop(view);
        let snapshot=crate::store::sqlite::SqliteStore::reset_snapshot_bounded(&tx,owner,true,&mut |n|budget.check(n))?;
        crate::store::sqlite::SqliteStore::select_ingest_privacy_units(&tx,owner,&mut selection,&mut |n|budget.check(n))?;
        crate::store::sqlite::SqliteStore::bind_selective_migration(&tx,owner,&mut selection,&mut |n|budget.check(n))?;
        if crate::store::sqlite::SqliteStore::queued_lifecycle_pending(&tx,owner)? {
            let queued=crate::repository::selective_queued_retained_digest(&tx,owner,Some(account),&mut budget).map_err(|_|"queued source unavailable")?.ok_or("queued source unavailable")?;
            selection.binding_sha256=crate::repository::sha256_hex(&serde_json::to_vec(&("s04.selective-queued-bind.v1",&selection.binding_sha256,queued)).map_err(|e|e.to_string())?);
        }
        crate::store::sqlite::SqliteStore::bind_selective_ingest_snapshot(&mut selection,&snapshot)?;
        if selection.binding_sha256!=expected {return Err("selective ingest snapshot changed".into());}
        let value=crate::repository::selective_ingest_view::selective_ingest_private_view(owner,account,&selection).map_err(|_|"selective ingest projection unavailable")?;
        tx.commit().map_err(|e|e.to_string())?;drop(budget);drop(conn);
        if sqlite.guarded_pair_profile().map_err(|_|"selective ingest profile unavailable")?!=profile {return Err("selective ingest profile changed".into());}
        Ok(value)
    }
    /// Host-private paired-source fence; original Host guard remains required.
    pub fn fence_assistant_interpretation(&self, request:&crate::repository::AssistantInterpretationFenceRequest) -> Result<String,String> {
        let sqlite=self.store.as_sqlite().ok_or("paired source requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired source store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.fence_assistant_interpretation(request).map_err(|e|e.to_string())
    }
    pub fn reset_fence_assistant_interpretation(&self,request:&crate::repository::AssistantInterpretationFenceRequest,reset:&crate::repository::ResetFenceBinding)->Result<String,String> {
        let sqlite=self.store.as_sqlite().ok_or("paired reset requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired reset unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&conn);
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.reset_fence_assistant_interpretation(request,reset,&mut budget).map_err(|e|e.to_string())
    }
    pub fn read_reset_assistant_fence(&self,request:&crate::repository::AssistantInterpretationFenceRequest,reset:&crate::repository::ResetFenceBinding)->Result<String,String> {
        let sqlite=self.store.as_sqlite().ok_or("paired reset requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"paired reset unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&conn);
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.read_reset_assistant_fence(request,reset,&mut budget).map_err(|e|e.to_string())
    }
    /// Host-private source projection only; callers provide fresh host authority.
    pub fn project_host_archive_source(&self, owner:&str, workspace:&str, job_id:&str,
        reference:&crate::repository::HostArchiveSourceRef, policy_fingerprint:&str, fence:bool, rename_operation_id:Option<&str>)
        -> Result<crate::repository::HostArchiveProjectionReceipt,String> {
        let sqlite=self.store.as_sqlite().ok_or("host source projection requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"host source store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.project_host_archive_source(owner,workspace,job_id,reference,policy_fingerprint,fence,rename_operation_id).map_err(|e|e.to_string())
    }

    /// Host-private read-only origin preflight; no visibility or mutation authority.
    pub fn preflight_queued_origins(&self, owner:&str, workspace:&str, account:&str, sources:&[crate::repository::QueuedArchiveSourceRef]) -> Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("queued preflight requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"queued preflight store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.preflight_queued_origins(owner,workspace,account,sources).map_err(|e|e.to_string())
    }

    /// Host-private limited fence; the host must separately prove current source authority.
    pub fn fence_queued_origin(&self, owner:&str, workspace:&str, source:&crate::repository::QueuedArchiveSourceRef) -> Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("queued fence requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"queued fence store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.fence_queued_origin(owner,workspace,source).map_err(|e|e.to_string())
    }

    pub fn fence_queued_origin_reset(&self, owner:&str, workspace:&str, source:&crate::repository::QueuedArchiveSourceRef, reset:&crate::repository::ResetFenceBinding) -> Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("queued fence requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"queued fence store unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&conn);
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.fence_queued_origin_reset(owner,workspace,source,reset,&mut budget).map_err(|e|e.to_string())
    }
    pub fn read_queued_reset_fence(&self, owner:&str, workspace:&str, source:&crate::repository::QueuedArchiveSourceRef, reset:&crate::repository::ResetFenceBinding) -> Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("queued fence proof requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"queued fence proof unavailable")?;
        let mut budget=crate::repository::ResetFenceBudget::install(&conn);
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.read_queued_reset_fence(owner,workspace,source,reset,&mut budget).map_err(|e|e.to_string())
    }

    /// Host-private owner review over the existing guarded S03 writer.
    pub fn apply_published_lifecycle(&self, request:&crate::repository::PublishedLifecycleRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("published review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"published review store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.apply_published_lifecycle_owned(request,sqlite).map_err(|e|e.to_string())
    }

    pub fn question_semantics_status(&self)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("question status requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"question store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.question_semantics_status().map_err(|e|e.to_string())
    }

    /// Host-only policy-bound reserved principal and question bootstrap.
    pub fn bootstrap_question_seeds(&self, request:&crate::repository::QuestionSeedBootstrapRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("question seed requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"question store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.bootstrap_question_seeds(request).map_err(|e|e.to_string())
    }

    pub fn question_seed_status(&self, request:&crate::repository::QuestionSeedStatusRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("question seed status requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"question store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.question_seed_status(request).map_err(|e|e.to_string())
    }

    /// C4-F8 pre-extracted facts (stage / list); named-human authority for review is owned by Host.
    pub fn m3_rank(&self,r:&crate::repository::M3RankRequest)->Result<serde_json::Value,String>{self.store.as_sqlite().ok_or("ranking unavailable")?.m3_rank(r)}
    pub fn s08_last_retained_run(&self,r:&crate::repository::S08LastRetainedRequest)->Result<serde_json::Value,String>{self.store.as_sqlite().ok_or("S08 retained run unavailable")?.s08_last_retained_run(r)}
    pub fn knowledge_update_batch_preview(&self,r:&crate::store::sqlite::UpdateBatchPreview)->Result<serde_json::Value,String>{self.store.as_sqlite().ok_or("atomic update batch unavailable")?.knowledge_update_batch_preview(r)}
    pub fn knowledge_update_batch_apply(&self,r:&crate::store::sqlite::UpdateBatchApply)->Result<serde_json::Value,String>{self.store.as_sqlite().ok_or("atomic update batch unavailable")?.knowledge_update_batch_apply(r)}
    pub fn knowledge_update_batch_status(&self,r:&crate::store::sqlite::UpdateBatchStatus)->Result<serde_json::Value,String>{self.store.as_sqlite().ok_or("atomic update batch unavailable")?.knowledge_update_batch_status(r)}
    pub fn preextracted_host_anchor_stage(&self,r:&crate::store::sqlite::HostAnchorStageRequest)->Result<serde_json::Value,String>{self.store.as_sqlite().ok_or("preextracted unavailable")?.preextracted_host_anchor_stage(r)}
    pub fn preextracted_host_anchor_status(&self,r:&crate::store::sqlite::HostAnchorStatusRequest)->Result<serde_json::Value,String>{self.store.as_sqlite().ok_or("preextracted unavailable")?.preextracted_host_anchor_status(r)}
    pub fn preextracted_stage(&self, request:&crate::store::sqlite::PreextractedStageRequest)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("preextracted unavailable")?.preextracted_stage(request)
    }
    pub fn preextracted_review_preview(&self, request:&crate::store::sqlite::PreextractedSelector)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("preextracted unavailable")?.preextracted_review_preview(request)
    }
    pub fn preextracted_review(&self, request:&crate::store::sqlite::PreextractedReviewRequest)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("preextracted unavailable")?.preextracted_review(request)
    }
    pub fn preextracted_list(&self, request:&crate::store::sqlite::PreextractedListRequest)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("preextracted unavailable")?.preextracted_list(request)
    }

    /// Private authenticated human Add; named-human authority is owned by Host.
    pub fn explicit_add(&self, request:&crate::store::sqlite::ExplicitAddRequest)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("explicit add requires SQLite")?.explicit_add(request)
    }
    pub fn explicit_add_status(&self, request:&crate::store::sqlite::ExplicitAddStatusRequest)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("explicit add requires SQLite")?.explicit_add_status(request)
    }

    pub fn explicit_add_review_preview(&self, request:&crate::store::sqlite::ExplicitAddReviewSelector)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("explicit add requires SQLite")?.explicit_add_review_preview(request)
    }
    pub fn explicit_add_review_edit(&self, request:&crate::store::sqlite::ExplicitAddReviewEditRequest)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("explicit add requires SQLite")?.explicit_add_review_edit(request)
    }

    pub fn explicit_add_review_apply(&self, request:&crate::store::sqlite::ExplicitAddReviewApplyRequest)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("explicit add requires SQLite")?.explicit_add_review_apply(request)
    }
    pub fn explicit_add_review_original(&self, request:&crate::store::sqlite::ExplicitAddReviewSelector)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("explicit add requires SQLite")?.explicit_add_review_original(request)
    }
    pub fn explicit_add_missing_page(&self,request:&crate::store::sqlite::ExplicitAddMissingPageRequest,terminal:bool)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("explicit add requires SQLite")?.explicit_add_missing_page(request,terminal)
    }
    pub fn import_missing_page(&self,request:&crate::store::sqlite::ExplicitAddMissingPageRequest,terminal:bool)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("import review requires SQLite")?.import_missing_page(request,terminal)
    }
    pub fn import_staged_review_preview(&self,request:&crate::store::sqlite::ExplicitAddReviewSelector)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("import review requires SQLite")?.import_staged_review_preview(request)
    }
    pub fn import_staged_review_edit(&self,request:&crate::store::sqlite::ExplicitAddReviewEditRequest)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("import review requires SQLite")?.import_staged_review_edit(request)
    }
    pub fn import_staged_review_original(&self,request:&crate::store::sqlite::ExplicitAddReviewSelector)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("import review requires SQLite")?.import_staged_review_original(request)
    }
    pub fn import_staged_review_status(&self,request:&crate::store::sqlite::ExplicitAddReviewApplyRequest)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("import review requires SQLite")?.import_staged_review_status(request)
    }
    pub fn import_staged_review_apply(&self,request:&crate::store::sqlite::ExplicitAddReviewApplyRequest)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("import review requires SQLite")?.import_staged_review_apply(request)
    }
    pub fn import_review_apply(&self,request:&crate::store::sqlite::ImportReviewSelector)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("import review requires SQLite")?.import_review_apply(request)
    }
    pub fn import_review_status(&self,request:&crate::store::sqlite::ImportReviewSelector)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("import review requires SQLite")?.import_review_status(request)
    }
    pub fn explicit_add_review_status(&self, request:&crate::store::sqlite::ExplicitAddReviewApplyRequest)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("explicit add requires SQLite")?.explicit_add_review_status(request)
    }

    /// Host-only confirmed question review, on the guarded store connection.
    pub fn apply_confirmed_question(&self, request:&crate::repository::ConfirmedQuestionRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("confirmed question requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"question store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.apply_confirmed_question_owned(request,sqlite).map_err(|e|e.to_string())
    }

    pub fn analysis_snapshot_capability(&self)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("snapshot requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"snapshot store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.analysis_snapshot_capability().map_err(|e|e.to_string())
    }
    pub fn publish_analysis_snapshot(&self,request:&crate::repository::AnalysisSnapshotWireRequest)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or("snapshot requires SQLite")?.publish_analysis_snapshot(request)
    }
    pub fn analysis_snapshot_status(&self,request:&crate::repository::AnalysisSnapshotStatusRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("snapshot requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"snapshot store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.analysis_snapshot_status(request).map_err(|e|e.to_string())
    }

    pub fn select_standing_guidance(&self,request:&crate::repository::StandingGuidanceSelection)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("purpose requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"purpose store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.select_standing_guidance(request).map_err(|e|e.to_string())
    }
    pub fn revalidate_standing_guidance(&self,request:&crate::repository::StandingGuidanceRevalidation)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("purpose requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"purpose store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.revalidate_standing_guidance(request).map_err(|e|e.to_string())
    }
    pub fn standing_guidance_capability(&self)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("purpose requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"purpose store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.standing_guidance_capability().map_err(|e|e.to_string())
    }
    pub fn preview_standing_guidance(&self,request:&crate::repository::StandingGuidancePreview)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("purpose requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"purpose store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.preview_standing_guidance(request).map_err(|e|e.to_string())
    }
    pub fn read_standing_guidance(&self,request:&crate::repository::StandingGuidancePreview)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("purpose requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"purpose store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.read_standing_guidance(request).map_err(|e|e.to_string())
    }
    pub fn apply_standing_guidance(&self,request:&crate::repository::StandingGuidanceReviewRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("purpose requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"purpose store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.apply_standing_guidance(request).map_err(|e|e.to_string())
    }
    pub fn standing_guidance_status(&self,request:&crate::repository::StandingGuidanceStatus)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("purpose requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"purpose store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.standing_guidance_status(request).map_err(|e|e.to_string())
    }

    pub fn preview_account_value_successor(&self,request:&crate::repository::AccountValueSuccessorPreview)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("successor requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"successor store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.preview_account_value_successor(request).map_err(|e|e.to_string())
    }

    pub fn account_value_successor_readiness(&self)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("successor requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"successor store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.account_value_successor_readiness().map_err(|e|e.to_string())
    }

    pub fn apply_account_value_successor(&self,request:&crate::repository::AccountValueSuccessorRequest)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("successor requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"successor store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.apply_account_value_successor(request).map_err(|e|e.to_string())
    }

    pub fn account_value_successor_status(&self,status:&crate::repository::AccountValueSuccessorStatus)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("successor requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"successor store unavailable")?;
        crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?.account_value_successor_status(status).map_err(|e|e.to_string())
    }

    pub fn current_host_mirror_context(&self, owner:&str, workspace:&str, curated_id:&str, account:Option<&str>) -> Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("mirror review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"mirror store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.current_curated_mirror_context_account(owner,workspace,curated_id,account).map_err(|e|e.to_string())
    }

    pub fn current_host_question_mirror_context(&self,owner:&str,workspace:&str,curated:&str,block:&str,account:&str)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or("question context requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"question context unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.current_question_mirror_context_owned(owner,workspace,curated,block,account,sqlite).map_err(|_|"question context unavailable".to_string())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn preview_host_mirror_update(&self, owner:&str, workspace:&str, candidate:&str, origin_request:&str, prior_id:&str, expected_revision:i64, request_key:&str, reason:&str, selected:&[String], payload:serde_json::Value) -> Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("mirror review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"mirror store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.preview_reviewed_curated_mirror_update(owner,workspace,candidate,origin_request,prior_id,expected_revision,request_key,reason,selected,payload).map_err(|e|e.to_string())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn apply_host_mirror_update(&self, owner:&str, workspace:&str, candidate:&str, origin_request:&str, prior_id:&str, expected_revision:i64, request_key:&str, selected:&[String], reason:&str, operation_json:&str, operation_sha256:&str) -> Result<serde_json::Value,String> {
        use sha2::{Digest,Sha256};
        if format!("{:x}",Sha256::digest(operation_json.as_bytes()))!=operation_sha256 {return Err("mirror operation digest mismatch".into());}
        let sqlite=self.store.as_sqlite().ok_or("mirror review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"mirror store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        let actor=crate::repository::RevisionActor {actor_type:"user".into(),actor_id:owner.into(),reason:reason.into(),source_event_id:None};
        let review=crate::field_authority::OwnerReviewContext {authenticated_owner:owner.into(),reviewer_id:owner.into(),expected_revision,proposal_key:request_key.into(),payload_hash:operation_sha256.into(),approved_paths:selected.to_vec()};
        repository.reviewed_curated_mirror_update(owner,workspace,candidate,origin_request,prior_id,expected_revision,request_key,operation_json,&actor,&review).map_err(|e|e.to_string())?;
        repository.replay_reviewed_curated_mirror_update(owner,workspace,candidate,origin_request,prior_id,expected_revision,request_key,operation_json).map_err(|e|e.to_string())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn replay_host_mirror_update(&self, owner:&str, workspace:&str, candidate:&str, origin_request:&str, prior_id:&str, expected_revision:i64, request_key:&str, operation_json:&str) -> Result<serde_json::Value,String> {
        let sqlite=self.store.as_sqlite().ok_or("mirror review requires SQLite")?;
        let mut conn=sqlite.conn.lock().map_err(|_|"mirror store unavailable")?;
        let mut repository=crate::repository::V2Repository::new(&mut conn).map_err(|e|e.to_string())?;
        repository.replay_reviewed_curated_mirror_update(owner,workspace,candidate,origin_request,prior_id,expected_revision,request_key,operation_json).map_err(|e|e.to_string())
    }

    pub fn new(
        store: Arc<dyn MemoryStore>,
        embed: Arc<dyn EmbeddingClient>,
        config: FmConfig,
    ) -> Self {
        Self {
            store,
            embed,
            config,
            vector_profile_build: Arc::new(std::sync::Mutex::new(None)),
        }
    }

    pub async fn update_curated_record(
        &self,
        id: &str,
        content: Option<&str>,
        kind: Option<MemoryKind>,
        category: Option<&str>,
        pinned: Option<bool>,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> bool {
        self.store
            .update_curated_record(id, content, kind, category, pinned, owner, workspace_id)
            .await
    }

    pub async fn delete_curated_record(
        &self,
        id: &str,
        owner: Option<&str>,
        workspace_id: Option<&str>,
    ) -> bool {
        self.store
            .delete_curated_record(id, owner, workspace_id)
            .await
    }

    /// Close one open question (U7a/U7b): archive the kind=unknown
    /// record with {resolved_by?, resolved_at} provenance. Fails on
    /// non-unknown kinds and out-of-scope ids.
    pub fn resolve_unknown(
        &self,
        id: &str,
        resolved_by: Option<&str>,
        owner: &str,
        workspace_id: &str,
    ) -> Result<bool, String> {
        let sqlite = self
            .store
            .as_sqlite()
            .ok_or_else(|| "resolution requires SQLite".to_string())?;
        let mut patch = serde_json::json!({
            "resolved_at": chrono::Utc::now().to_rfc3339(),
        });
        if let Some(by) = resolved_by.filter(|value| !value.trim().is_empty()) {
            patch["resolved_by"] = by.into();
        }
        let resolved = sqlite.archive_curated_record(
            id,
            &patch,
            Some("unknown"),
            Some(owner),
            Some(workspace_id),
        );
        if resolved {
            sqlite.metric_add("unknowns_resolved_directly", 1);
        }
        Ok(resolved)
    }

    /// Idempotent projection of an agent-authored memory file into curated
    /// records, one per section. The file stays the source of truth: records
    /// carry (authored_path, anchor, content_hash) in metadata, unchanged
    /// sections are skipped by hash, edited sections replace their old
    /// record, and sections gone from the file (or the whole file, via an
    /// empty `sections`) delete theirs. Authored records are exempt from
    /// decay and dedup — the reconcile pass is their only lifecycle.
    pub async fn ingest_authored(
        &self,
        owner: &str,
        workspace_id: &str,
        source_path: &str,
        sections: &[(String, String)],
    ) -> Result<serde_json::Value, String> {
        if owner.trim().is_empty() {
            return Err("owner is required".into());
        }
        if source_path.trim().is_empty() {
            return Err("source_path is required".into());
        }
        let sqlite = self
            .store
            .as_sqlite()
            .ok_or_else(|| "authored ingest requires SQLite".to_string())?;
        let existing = sqlite.authored_records(owner, workspace_id, source_path)?;
        let existing_by_id: std::collections::HashMap<&str, &str> = existing
            .iter()
            .map(|(id, hash)| (id.as_str(), hash.as_str()))
            .collect();
        let mut filtered = Vec::new();
        for (anchor, content) in sections {
            let content = content.trim();
            if content.is_empty() {
                continue;
            }
            let Some((content, _)) = filter_text(content) else {
                continue;
            };
            filtered.push((anchor.clone(), content));
        }
        let mut desired: Vec<MemoryRecord> = Vec::new();
        let mut desired_index: std::collections::HashMap<String, usize> =
            std::collections::HashMap::new();
        for (anchor, content) in filtered {
            let hash = content_hash(&content);
            let mut record = MemoryRecord::new(content);
            record.id = stable_id("authored", &[owner, workspace_id, source_path, &anchor]);
            record.kind = MemoryKind::Wiki;
            record.owner = Some(owner.to_string());
            record.workspace_id = workspace_id.to_string();
            record.source = "authored".into();
            record.source_type = SourceType::Ai;
            record.exempt_from_decay = true;
            record.exempt_from_dedup = true;
            record.metadata = serde_json::json!({
                "authored_path": source_path,
                "anchor": anchor.clone(),
                "content_hash": hash.clone(),
                "source_uri": format!("file://{}#{}", source_path, anchor),
                "source_type": "authored_file",
                "source_revision": hash.clone(),
            });
            // One curated record per anchor: duplicate anchors in a single
            // file collapse to one id, the last section winning — the same
            // outcome the reconcile pass's sequential upserts would persist.
            match desired_index.get(record.id.as_str()) {
                Some(&index) => desired[index] = record,
                None => {
                    desired_index.insert(record.id.clone(), desired.len());
                    desired.push(record);
                }
            }
        }
        let mut changed = Vec::new();
        let mut unchanged = 0usize;
        for record in &desired {
            let hash = record
                .metadata
                .get("content_hash")
                .and_then(|value| value.as_str())
                .unwrap_or_default();
            if existing_by_id.get(record.id.as_str()).copied() == Some(hash) {
                unchanged += 1;
            } else {
                changed.push(record.clone());
            }
        }
        let (_, deleted) =
            sqlite.reconcile_authored_records(owner, workspace_id, source_path, &desired)?;
        let mut unknowns_resolved: Vec<String> = Vec::new();
        for record in &changed {
            unknowns_resolved.extend(curate::questions::resolve_against_open_unknowns(
                self.store.as_ref(),
                record,
            ));
        }

        Ok(serde_json::json!({
            "upserted": changed.len(),
            "unchanged": unchanged,
            "deleted": deleted,
            "total": desired.len(),
            "unknowns_resolved": unknowns_resolved,
        }))
    }

    pub fn get_curated_record(
        &self,
        id: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Option<MemoryRecord>, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "direct reads require SQLite".to_string())?
            .get_curated_record(id, owner, workspace_id)
            .map_err(|error| error.to_string())
    }

    pub fn list_curated_records(
        &self,
        owner: &str,
        workspace_id: &str,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<MemoryRecord>, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "paginated reads require SQLite".to_string())?
            .list_curated_records(owner, workspace_id, limit, offset)
            .map_err(|error| error.to_string())
    }

    pub fn record_curated_access(
        &self,
        ids: &[String],
        owner: &str,
        workspace_id: &str,
    ) -> Result<usize, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "access accounting requires SQLite".to_string())?
            .record_curated_access(ids, owner, workspace_id)
            .map_err(|error| error.to_string())
    }

    pub fn list_candidates(
        &self,
        owner: Option<&str>,
        workspace_id: Option<&str>,
        status: Option<&str>,
        limit: usize,
    ) -> Result<Vec<CandidateRecord>, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "candidate inspection requires SQLite".to_string())?
            .list_candidates(owner, workspace_id, status, limit)
            .map_err(|error| error.to_string())
    }

    pub fn update_candidate(
        &self,
        id: &str,
        owner: &str,
        workspace_id: &str,
        content: &str,
        kind: Option<MemoryKind>,
        reason: &str,
    ) -> Result<Option<CandidateRecord>, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "candidate editing requires SQLite".to_string())?
            .update_pending_candidate(id, owner, workspace_id, content, kind, reason)
            .map_err(|error| error.to_string())
    }

    pub fn reopen_unknown(
        &self,
        id: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<bool, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "question reopening requires SQLite".to_string())?
            .reopen_curated_unknown(id, owner, workspace_id)
            .map_err(|error| error.to_string())
    }

    pub fn quality_status(&self) -> Result<serde_json::Value, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "quality status requires SQLite".to_string())?
            .quality_status()
            .map_err(|error| error.to_string())
    }

    pub fn scoped_quality_status(&self, owner:&str, workspace:&str) -> Result<serde_json::Value,String> {
        self.store.as_sqlite().ok_or_else(||"quality status requires SQLite".to_string())?
            .scoped_quality_status(owner,workspace)
    }

    pub fn rebuild_graph_cue_fts(&self) -> Result<serde_json::Value, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "graph FTS rebuild requires SQLite".to_string())?
            .rebuild_graph_cue_fts()
            .map_err(|error| error.to_string())
    }

    pub fn list_quarantine(
        &self,
        owner: Option<&str>,
        workspace_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<serde_json::Value>, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "quarantine inspection requires SQLite".to_string())?
            .list_quarantine(owner, workspace_id, limit)
            .map_err(|error| error.to_string())
    }

    pub fn quarantine_legacy_state(
        &self,
        dry_run: bool,
        reason: &str,
    ) -> Result<serde_json::Value, String> {
        self.store
            .as_sqlite()
            .ok_or_else(|| "quarantine migration requires SQLite".to_string())?
            .quarantine_legacy_state(dry_run, reason)
            .map_err(|error| error.to_string())
    }

    /// Read-only host preview. The snapshot covers the full persisted candidate.
    pub fn preview_host_candidate_review(&self, owner: &str, workspace_id: &str, candidate_id: &str, reason: &str) -> Result<serde_json::Value, String> {
        if owner.trim().is_empty() || workspace_id.trim().is_empty() || candidate_id.trim().is_empty() || reason.trim().is_empty() || reason.len() > 500 {
            return Err("invalid host review context".into());
        }
        let sqlite = self.store.as_sqlite().ok_or_else(|| "candidate review requires SQLite".to_string())?;
        let candidate = sqlite.candidate_by_id(candidate_id, owner, workspace_id)
            .map_err(|error| error.to_string())?.ok_or_else(|| "candidate unavailable".to_string())?;
        if candidate.status != CandidateStatus::Pending || content_hash(&candidate.content) != candidate.content_hash {
            return Err("candidate snapshot is stale".into());
        }
        let snapshot_digest = candidate_snapshot_digest(&candidate)?;
        let record = reviewed_candidate_record(&candidate, owner, workspace_id, reason);
        Ok(serde_json::json!({"snapshot_version":"s03.candidate-snapshot.v1", "snapshot_digest":snapshot_digest,
            "candidate":candidate, "curated_record":record}))
    }

    pub fn host_candidate_source_reference(&self,owner:&str,workspace:&str,candidate:&str)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or_else(||"candidate review requires SQLite".to_string())?.host_candidate_source_reference(owner,workspace,candidate)
    }

    pub fn preview_host_candidate_rejection(&self,owner:&str,workspace:&str,candidate:&str)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or_else(||"candidate review requires SQLite".to_string())?.preview_host_candidate_rejection(owner,workspace,candidate)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn reject_host_candidate(&self,owner:&str,workspace:&str,candidate:&str,digest:&str,association:&str,account:&str,chat:&str,key:&str,reason:&str)->Result<serde_json::Value,String>{
        self.store.as_sqlite().ok_or_else(||"candidate review requires SQLite".to_string())?.reject_host_candidate_transaction(owner,workspace,candidate,digest,association,account,chat,key,reason)
    }

    /// Private, read-only candidate acceptance receipt lookup.
    #[allow(clippy::too_many_arguments)]
    pub fn lookup_host_candidate_receipt(&self, owner: &str, workspace: &str, candidate_id: &str,
        request_key: &str, digest: &str, expected_head: i64, selected_paths: &[String], reason: &str) -> Result<serde_json::Value, String> {
        self.store.as_sqlite().ok_or_else(|| "candidate review requires SQLite".to_string())?
            .lookup_host_candidate_receipt(owner, workspace, candidate_id, request_key, digest, expected_head, selected_paths, reason)
    }

    /// Compute an optional embedding for the exact frozen host candidate.
    /// The caller must first admit this fixed work under the host source guard.
    pub async fn prepare_host_candidate_embedding(&self,owner:&str,workspace:&str,candidate_id:&str,digest:&str)->Result<serde_json::Value,String>{
        let sqlite=self.store.as_sqlite().ok_or_else(||"candidate review requires SQLite".to_string())?;
        let candidate=sqlite.candidate_by_id(candidate_id,owner,workspace).map_err(|e|e.to_string())?.ok_or_else(||"candidate unavailable".to_string())?;
        if candidate.status!=CandidateStatus::Pending || !candidate.source_uri.starts_with("host-archive://hostpart_") || candidate_snapshot_digest(&candidate)?!=digest || content_hash(&candidate.content)!=candidate.content_hash {return Err("candidate snapshot is stale".into());}
        let embedding=self.embed.embed(&candidate.content).await.ok();
        if embedding.as_ref().is_some_and(|v|v.len()>8192 || v.iter().any(|n|!n.is_finite())) {return Err("candidate embedding unavailable".into());}
        Ok(serde_json::json!({"state":"prepared","candidate_id":candidate_id,"candidate_digest":digest,"embedding":embedding}))
    }

    /// Apply only the exact host-prepared candidate and projection; no model call.
    #[allow(clippy::too_many_arguments)]
    pub fn apply_prepared_host_candidate_review(&self,owner:&str,workspace:&str,candidate_id:&str,digest:&str,expected_head:i64,selected_paths:&[String],request_key:&str,reason:&str,projection:&serde_json::Value,embedding:Option<&[f32]>)->Result<serde_json::Value,String>{
        if embedding.is_some_and(|v|v.len()>8192 || v.iter().any(|n|!n.is_finite())) {return Err("candidate embedding unavailable".into());}
        let replay=self.lookup_host_candidate_receipt(owner,workspace,candidate_id,request_key,digest,expected_head,selected_paths,reason)?;
        if replay["state"]=="applied" {return Ok(replay);}
        if replay["state"]!="needs_preview" {return Err("candidate receipt unavailable".into());}
        self.check_host_candidate_review(owner,workspace,candidate_id,digest,expected_head,selected_paths,request_key,reason)?;
        let sqlite=self.store.as_sqlite().ok_or_else(||"candidate review requires SQLite".to_string())?;
        let candidate=sqlite.candidate_by_id(candidate_id,owner,workspace).map_err(|e|e.to_string())?.ok_or_else(||"candidate unavailable".to_string())?;
        if !candidate.source_uri.starts_with("host-archive://hostpart_") {return Err("host candidate unavailable".into());}
        sqlite.accept_guarded_candidate_transaction(owner,workspace,candidate_id,digest,expected_head,selected_paths,request_key,reason,projection,embedding)
    }

    /// Host-private guarded acceptance; embedding is computed before the SQLite lock.
    #[allow(clippy::too_many_arguments)]
    pub async fn apply_host_candidate_review(&self, owner: &str, workspace: &str, candidate_id: &str,
        digest: &str, expected_head: i64, selected_paths: &[String], request_key: &str, reason: &str,
        projection: &serde_json::Value) -> Result<serde_json::Value, String> {
        let replay = self.lookup_host_candidate_receipt(owner,workspace,candidate_id,request_key,digest,expected_head,selected_paths,reason)?;
        if replay["state"] == "applied" { return Ok(replay); }
        if replay["state"] != "needs_preview" { return Err("candidate receipt unavailable".into()); }
        self.check_host_candidate_review(owner,workspace,candidate_id,digest,expected_head,selected_paths,request_key,reason)?;
        let sqlite = self.store.as_sqlite().ok_or_else(|| "candidate review requires SQLite".to_string())?;
        let candidate = sqlite.candidate_by_id(candidate_id,owner,workspace).map_err(|e| e.to_string())?
            .ok_or_else(|| "candidate unavailable".to_string())?;
        let host_archive = candidate.source_uri.starts_with("host-archive://hostpart_");
        if host_archive { return Err("host candidate requires prepared guarded review".into()); }
        if !candidate.raw_evidence_ids.is_empty() || !candidate.source_uri.is_empty() || !candidate.source_message_ids.is_empty() ||
           !candidate.source_event_id.is_empty() || !candidate.session_id.is_empty() || candidate.source_revision != 1 {
            return Err("upstream candidate lineage unsupported; candidate remains pending".into());
        }
        let embedding = self.embed.embed(&candidate.content).await.ok();
        sqlite.accept_guarded_candidate_transaction(owner,workspace,candidate_id,digest,expected_head,
            selected_paths,request_key,reason,projection,embedding.as_deref())
    }

    /// Validate only; owner acceptance remains disabled until the atomic V2 join exists.
    pub fn check_host_candidate_review(&self, owner: &str, workspace_id: &str, candidate_id: &str,
                                       digest: &str, expected_v2_head: i64, selected_paths: &[String], request_key: &str, reason: &str) -> Result<(), String> {
        if owner.trim().is_empty() || workspace_id.trim().is_empty() || candidate_id.trim().is_empty() ||
           request_key.trim().is_empty() || reason.trim().is_empty() || reason.len() > 500 || expected_v2_head < 0 || selected_paths != ["/value"] ||
           digest.len() != 64 || !digest.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            return Err("invalid host review context".into());
        }
        let sqlite = self.store.as_sqlite().ok_or_else(|| "candidate review requires SQLite".to_string())?;
        let candidate = sqlite.candidate_by_id(candidate_id, owner, workspace_id)
            .map_err(|error| error.to_string())?.ok_or_else(|| "candidate unavailable".to_string())?;
        if candidate.status != CandidateStatus::Pending || candidate_snapshot_digest(&candidate)? != digest || content_hash(&candidate.content) != candidate.content_hash {
            return Err("candidate snapshot is stale".into());
        }
        // Mapping/head and the candidate CAS must be checked in the future same transaction.
        Ok(())
    }

    pub async fn review_candidate(
        &self,
        id: &str,
        accept: bool,
        reason: &str,
        owner: &str,
        workspace_id: &str,
    ) -> Result<Option<String>, String> {
        let sqlite = self
            .store
            .as_sqlite()
            .ok_or_else(|| "candidate review requires SQLite".to_string())?;
        if accept {
            let conn = sqlite.conn.lock().map_err(|_| "candidate store unavailable".to_string())?;
            let version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0)).map_err(|e| e.to_string())?;
            if version >= 17 { return Err("legacy candidate acceptance unavailable under v17; guarded receipt required".into()); }
        }
        let candidate = sqlite
            .candidate_by_id(id, owner, workspace_id)
            .map_err(|error| error.to_string())?
            .ok_or_else(|| "candidate not found in this scope".to_string())?;
        if !accept {
            let changed = sqlite
                .set_candidate_status(
                    id,
                    CandidateStatus::Rejected,
                    reason,
                    None,
                    owner,
                    workspace_id,
                )
                .map_err(|error| error.to_string())?;
            if !changed { return Err("candidate rejection unavailable".into()); }
            sqlite.metric_add("candidates_rejected_by_review", 1);
            return Ok(None);
        }
        if let Some(existing) = candidate.accepted_curated_id {
            return Ok(Some(existing));
        }
        if candidate.status != CandidateStatus::Pending {
            return Err("only pending candidates can be accepted".into());
        }
        let record = reviewed_candidate_record(&candidate, owner, workspace_id, reason);
        let curated_id = record.id.clone();
        let embedding = self.embed.embed(&candidate.content).await.ok();
        sqlite
            .accept_candidate_transaction(
                id,
                &record,
                embedding.as_deref(),
                reason,
                owner,
                workspace_id,
            )
            .map_err(|error| error.to_string())?;
        sqlite.metric_add("candidates_accepted_by_review", 1);
        if embedding.is_some() {
            sqlite.metric_add("curated_embeddings_written", 1);
        }
        // Passive resolution (U7c): candidate approval is an admission
        // site; the newly endorsed content may answer an open question.
        curate::questions::resolve_against_open_unknowns(self.store.as_ref(), &record);
        Ok(Some(curated_id))
    }
}

pub(crate) fn candidate_snapshot_digest(candidate: &CandidateRecord) -> Result<String, String> {
    let bytes = serde_json::to_vec(candidate).map_err(|error| error.to_string())?;
    Ok(blake3::hash(&bytes).to_hex().to_string())
}

pub(crate) fn reviewed_candidate_record(candidate: &CandidateRecord, owner: &str, workspace_id: &str, reason: &str) -> MemoryRecord {
    let mut record = MemoryRecord::new(&candidate.content);
    record.id = stable_id("m", &[&candidate.id]);
    record.kind = candidate.kind;
    record.confidence_score = candidate.confidence_score;
    record.importance_score = candidate.importance_score;
    record.source = candidate.source.clone();
    record.source_type = SourceType::Human;
    record.owner = Some(owner.to_string());
    record.workspace_id = workspace_id.to_string();
    record.workspace_path = candidate.workspace_path.clone();
    record.session_id = candidate.session_id.clone();
    record.session_key = candidate.session_id.clone();
    record.metadata = serde_json::json!({
        "candidate_id": candidate.id.clone(), "turn_id": candidate.turn_id.clone(),
        "raw_evidence_ids": candidate.raw_evidence_ids.clone(), "admission_reason": reason,
        "manually_reviewed": true, "source_uri": candidate.source_uri.clone(),
        "source_revision": candidate.source_revision, "content_hash": candidate.content_hash.clone(),
    });
    record.source_message_ids = candidate.source_message_ids.clone();
    record
}

fn category_to_kind(category: Option<&str>) -> MemoryKind {
    match category
        .unwrap_or("episodic")
        .trim()
        .to_lowercase()
        .as_str()
    {
        "persona" | "identity" => MemoryKind::Persona,
        "instruction" | "preference" => MemoryKind::Instruction,
        "fact" | "contact" | "project" | "goal" => MemoryKind::Fact,
        "fabric" => MemoryKind::Fabric,
        "wiki" | "reference" => MemoryKind::Wiki,
        "unknown" | "question" => MemoryKind::Unknown,
        _ => MemoryKind::Episodic,
    }
}

fn stable_id(prefix: &str, parts: &[&str]) -> String {
    let joined = parts.join("\u{1f}");
    format!(
        "{prefix}_{}",
        &blake3::hash(joined.as_bytes()).to_hex()[..24]
    )
}

fn normalized(value: &str) -> String {
    value
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim()
        .to_lowercase()
}

fn prefilter_candidate(content: &str, evidence_role: &str) -> Option<&'static str> {
    let text = normalized(content);
    let words = text.split_whitespace().count();
    if text.is_empty() || words < 3 {
        return Some("empty_or_short_acknowledgement");
    }
    if matches!(
        text.as_str(),
        "ok thanks" | "thank you" | "sounds good" | "got it" | "pong pong pong"
    ) {
        return Some("acknowledgement");
    }
    if text.contains("/tmp/")
        || text.contains("temporary permission test")
        || text.contains("permission-test")
    {
        return Some("temporary_fixture");
    }
    if text.contains("distill this conversation")
        || text.contains("memory dream")
        || text.contains("dream cycle")
        || text.contains("dream/distill")
    {
        return Some("automation_prompt");
    }
    if text.contains("<tool_call")
        || text.contains("tool protocol")
        || text.contains("mcp tool result")
        || text.contains("calltoolresult")
    {
        return Some("tool_protocol");
    }
    if evidence_role == "assistant"
        && (text.starts_with("i will ")
            || text.starts_with("i'll ")
            || text.starts_with("let me ")
            || text.starts_with("i am going to "))
    {
        return Some("assistant_process_narration");
    }
    None
}

fn automatic_admission(
    content: &str,
    evidence_role: &str,
) -> Option<(&'static str, MemoryKind, &'static str, bool)> {
    if evidence_role != "user" || content.contains('?') {
        return None;
    }
    let text = normalized(content);
    if [
        "password",
        "api key",
        "secret",
        "access token",
        "credit card",
        "ssn",
    ]
    .iter()
    .any(|term| text.contains(term))
    {
        return None;
    }
    if ["my name is ", "call me "]
        .iter()
        .any(|prefix| text.starts_with(prefix))
    {
        return Some(("auto_identity_claim", MemoryKind::Persona, "identity", true));
    }
    if ["i like ", "i love ", "i prefer ", "i dislike ", "i hate "]
        .iter()
        .any(|prefix| text.starts_with(prefix))
    {
        return Some((
            "auto_preference_claim",
            MemoryKind::Instruction,
            "preference",
            false,
        ));
    }
    if [
        "i live in ",
        "i am from ",
        "i'm from ",
        "i work on ",
        "i am working on ",
        "i'm working on ",
        "i am traveling to ",
        "i'm traveling to ",
        "i am travelling to ",
        "i'm travelling to ",
    ]
    .iter()
    .any(|prefix| text.starts_with(prefix))
    {
        return Some(("auto_user_claim", MemoryKind::Fact, "fact", false));
    }
    None
}

fn metadata_string(metadata: &serde_json::Value, key: &str) -> String {
    metadata
        .get(key)
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn metadata_strings(metadata: &serde_json::Value, key: &str) -> Vec<String> {
    metadata
        .get(key)
        .and_then(serde_json::Value::as_array)
        .map(|values| {
            values
                .iter()
                .filter_map(serde_json::Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn capture_source_type(turn: &CompletedTurn, capture_mode: &str) -> SourceType {
    let declared = metadata_string(&turn.metadata, "source_type")
        .trim()
        .to_lowercase();
    let source = turn.source.trim().to_lowercase();
    match declared.as_str() {
        "human" | "user" | "user_created" => SourceType::Human,
        "ai" | "ai_agent" | "agent_explicit" => SourceType::Ai,
        "procedural" => SourceType::Procedural,
        "auto_extracted" | "odysseus" => SourceType::AutoExtracted,
        _ => match source.as_str() {
            "user" | "user_created" => SourceType::Human,
            "ai_agent" | "agent_explicit" => SourceType::Ai,
            "odysseus" | "auto_extracted" => SourceType::AutoExtracted,
            _ if capture_mode == "manual" => SourceType::Human,
            _ => SourceType::AutoExtracted,
        },
    }
}

impl NativeProvider {
    /// Fail closed for generic capture under guarded v17; Host-private
    /// source-qualified owners use their own transaction methods.
    pub fn generic_capture_unavailable(&self)->Result<bool,String> {
        match self.store.as_sqlite() {Some(store)=>store.generic_capture_unavailable(),None=>Ok(false)}
    }
}

#[async_trait]
impl MemoryProvider for NativeProvider {
    fn id(&self) -> &str {
        "native"
    }

    async fn capture(&self, turn: &CompletedTurn) -> CaptureResult {
        if self.generic_capture_unavailable().unwrap_or(true) {
            return CaptureResult { records_captured:0, record_ids:vec![], vectors_written:0,
                providers_succeeded:0, providers_failed:1, unknowns_resolved:Vec::new() };
        }
        let capture_mode = metadata_string(&turn.metadata, "capture_mode");
        // Direct provider callers predate the MCP capture-mode contract and
        // represent deliberate saves. The MCP boundary stamps every automatic
        // turn explicitly as raw_only, so compatibility here is safe.
        let capture_mode = if capture_mode.is_empty() {
            "manual"
        } else {
            capture_mode.as_str()
        };
        let workspace_id = turn.workspace_id.trim();
        let owner = turn.owner.as_deref().unwrap_or_default().trim();
        // Conversational memory writes to the "global" workspace by
        // convention; only a missing owner disqualifies automatic capture.
        if capture_mode != "manual" && owner.is_empty() {
            tracing::warn!("automatic capture rejected: owner is required");
            return CaptureResult {
                records_captured: 0,
                record_ids: vec![],
                vectors_written: 0,
                providers_succeeded: 0,
                providers_failed: 1,
                unknowns_resolved: Vec::new(),
            };
        }

        let filtered_user = filter_text(&turn.user_text);
        let filtered_assistant = filter_text(&turn.assistant_text);
        let user_text = filtered_user
            .as_ref()
            .map(|(text, _)| text.as_str())
            .unwrap_or_default();
        let assistant_text = filtered_assistant
            .as_ref()
            .map(|(text, _)| text.as_str())
            .unwrap_or_default();
        let has_user = !user_text.trim().is_empty();
        let has_assistant = !assistant_text.trim().is_empty();
        if let Some(sqlite) = self.store.as_sqlite() {
            let redacted = filtered_user.as_ref().is_some_and(|(_, changed)| *changed)
                || filtered_assistant
                    .as_ref()
                    .is_some_and(|(_, changed)| *changed);
            if redacted {
                sqlite.metric_add("privacy_values_redacted", 1);
            }
            if filtered_user.is_none() || filtered_assistant.is_none() {
                sqlite.metric_add("privacy_values_not_stored", 1);
            }
        }
        if !has_user && !has_assistant {
            return CaptureResult {
                records_captured: 0,
                record_ids: vec![],
                vectors_written: 0,
                providers_succeeded: 1,
                providers_failed: 0,
                unknowns_resolved: Vec::new(),
            };
        }

        let effective_owner = if owner.is_empty() { "legacy" } else { owner };
        let effective_workspace = if workspace_id.is_empty() {
            "global"
        } else {
            workspace_id
        };
        let source_message_ids = metadata_strings(&turn.metadata, "source_message_ids");
        let mut source_event_id = metadata_string(&turn.metadata, "source_event_id");
        if source_event_id.is_empty() {
            source_event_id = stable_id(
                "event",
                &[
                    effective_owner,
                    effective_workspace,
                    &turn.session_id,
                    user_text,
                    assistant_text,
                ],
            );
        }
        let turn_id = stable_id("turn", &[&source_event_id]);
        let (mut metadata, metadata_redactions) = filter_json(&turn.metadata);
        if metadata_redactions > 0 {
            if let Some(sqlite) = self.store.as_sqlite() {
                sqlite.metric_add("privacy_metadata_values_redacted", metadata_redactions);
            }
        }
        if !metadata.is_object() {
            metadata = serde_json::json!({});
        }
        if let Some(object) = metadata.as_object_mut() {
            object.insert("turn_id".into(), turn_id.clone().into());
            object.insert("source_event_id".into(), source_event_id.clone().into());
            object.insert(
                "source_message_ids".into(),
                serde_json::json!(source_message_ids),
            );
        }
        let extraction_holder = format!("capture:{}:{}", turn_id, generate_id());
        let _extraction_lease = match self.store.as_sqlite() {
            Some(sqlite) => match sqlite.acquire_memory_lease(
                "extraction",
                effective_owner,
                effective_workspace,
                &source_event_id,
                &extraction_holder,
                Duration::from_secs(300),
            ) {
                Ok(Some(lease)) => Some(lease),
                Ok(None) => {
                    sqlite.metric_add("extraction_lease_contention", 1);
                    return CaptureResult {
                        records_captured: 0,
                        record_ids: vec![],
                        vectors_written: 0,
                        providers_succeeded: 1,
                        providers_failed: 0,
                        unknowns_resolved: Vec::new(),
                    };
                }
                Err(error) => {
                    warn!("memory extraction lease failed: {error}");
                    return CaptureResult {
                        records_captured: 0,
                        record_ids: vec![],
                        vectors_written: 0,
                        providers_succeeded: 0,
                        providers_failed: 1,
                        unknowns_resolved: Vec::new(),
                    };
                }
            },
            None => None,
        };

        let (content, evidence_role) = if has_user {
            (user_text.trim(), "user")
        } else {
            (assistant_text.trim(), "assistant")
        };
        let evidence_message_index = usize::from(evidence_role == "assistant");
        let evidence_message_ids: Vec<String> = source_message_ids
            .get(evidence_message_index)
            .cloned()
            .into_iter()
            .collect();
        let evidence_source_uri = evidence_message_ids
            .first()
            .map(|message_id| {
                format!(
                    "message://{}/{}/{}",
                    effective_owner, turn.session_id, message_id
                )
            })
            .unwrap_or_else(|| {
                format!(
                    "event://{}/{}#{}",
                    effective_owner, source_event_id, evidence_role
                )
            });
        // Open questions (kind=unknown) are human-minted only: explicit
        // category plus manual admission, everything else rejected here.
        // The question is normalized BEFORE the dedup key so the stored
        // form and the dedup form can never disagree, and the short-
        // content prefilter is skipped for it — question fragments
        // ("user's name?") are the intended shape.
        let explicit_kind = turn
            .category
            .as_deref()
            .map(|category| category_to_kind(Some(category)));
        let question = explicit_kind == Some(MemoryKind::Unknown);
        let normalized_question;
        let content = if question {
            normalized_question = normalize_question(content);
            normalized_question.as_str()
        } else {
            content
        };
        let manual = capture_mode == "manual";
        let review_only = capture_mode == "review_only";
        let rejection = if question {
            if content.is_empty() {
                Some("empty_question")
            } else if !manual && !review_only {
                Some("unknown_kind_requires_manual")
            } else {
                None
            }
        } else if manual || (review_only && explicit_kind.is_some()) {
            None
        } else {
            prefilter_candidate(content, evidence_role)
        };
        let auto_admission = automatic_admission(content, evidence_role);
        let dedup_key = stable_id(
            "dedup",
            &[effective_owner, effective_workspace, &normalized(content)],
        );
        let candidate_id = stable_id("candidate", &[&dedup_key]);
        let mut records_captured = 0usize;
        let mut record_ids = Vec::new();
        let mut raw_ids_by_role = std::collections::HashMap::new();
        for (role, content, message_index) in [
            ("user", user_text, 0usize),
            ("assistant", assistant_text, 1usize),
        ] {
            if content.trim().is_empty() {
                continue;
            }
            let mut raw = RawTurn::new(role, content);
            raw.id = stable_id("raw", &[&source_event_id, role]);
            raw.session_key = turn.session_key.clone();
            raw.session_id = turn.session_id.clone();
            raw.workspace_id = effective_workspace.to_string();
            raw.owner = Some(effective_owner.to_string());
            raw.workspace_path = turn.workspace_path.clone();
            raw.metadata = metadata.clone();
            if let Some(object) = raw.metadata.as_object_mut() {
                if let Some(message_id) = source_message_ids.get(message_index) {
                    object.insert("source_message_id".into(), message_id.clone().into());
                    object.insert(
                        "source_uri".into(),
                        format!(
                            "message://{}/{}/{}",
                            effective_owner, turn.session_id, message_id
                        )
                        .into(),
                    );
                } else {
                    object.insert(
                        "source_uri".into(),
                        format!("event://{}/{}#{}", effective_owner, source_event_id, role).into(),
                    );
                }
                let manual_submission = manual && turn.session_id.is_empty() && role == "user"
                    && source_message_ids.is_empty();
                let raw_source = if manual_submission { "manual_submission" } else { "conversation_message" };
                object.insert("source_type".into(), raw_source.into());
                object.insert("source_revision".into(), 1.into());
                object.insert("content_hash".into(), content_hash(content).into());
            }
            let raw_id = raw.id.clone();
            if self.store.upsert_raw(&raw, None).await {
                records_captured += 1;
                if let Some(sqlite) = self.store.as_sqlite() {
                    sqlite.metric_add("raw_rows_written", 1);
                    sqlite.metric_add("raw_embeddings_avoided", 1);
                }
            } else if let Some(sqlite) = self.store.as_sqlite() {
                sqlite.metric_add("dedup_hits", 1);
            }
            raw_ids_by_role.insert(role, raw_id.clone());
            record_ids.push(raw_id);
        }

        if capture_mode == "raw_only" {
            info!("native raw capture: {} rows", records_captured);
            return CaptureResult {
                records_captured,
                record_ids,
                vectors_written: 0,
                providers_succeeded: 1,
                providers_failed: 0,
                unknowns_resolved: Vec::new(),
            };
        }

        let now = chrono::Utc::now().to_rfc3339();
        let candidate_kind = explicit_kind
            .or_else(|| auto_admission.map(|(_, kind, _, _)| kind))
            .unwrap_or(MemoryKind::Episodic);
        let admission_reason = if manual {
            Some("manual_admission")
        } else if review_only {
            None
        } else {
            auto_admission.map(|(reason, _, _, _)| reason)
        };
        let mut candidate = CandidateRecord {
            id: candidate_id.clone(),
            content: content.to_string(),
            kind: candidate_kind,
            confidence_score: if manual {
                0.95
            } else if auto_admission.is_some() {
                0.88
            } else {
                0.55
            },
            importance_score: if manual {
                0.8
            } else if auto_admission.is_some() {
                0.7
            } else {
                0.5
            },
            owner: effective_owner.to_string(),
            workspace_id: effective_workspace.to_string(),
            workspace_path: turn.workspace_path.clone(),
            session_id: turn.session_id.clone(),
            turn_id: turn_id.clone(),
            raw_evidence_ids: raw_ids_by_role
                .get(evidence_role)
                .cloned()
                .into_iter()
                .collect(),
            evidence_role: evidence_role.to_string(),
            source: turn.source.clone(),
            source_event_id: source_event_id.clone(),
            dedup_key: dedup_key.clone(),
            source_uri: evidence_source_uri.clone(),
            source_revision: 1,
            content_hash: content_hash(content),
            source_message_ids: evidence_message_ids.clone(),
            status: if rejection.is_some() {
                CandidateStatus::Rejected
            } else {
                CandidateStatus::Pending
            },
            reason: rejection
                .unwrap_or(admission_reason.unwrap_or("awaiting_review"))
                .into(),
            accepted_curated_id: None,
            created_at: now.clone(),
            updated_at: now,
        };
        let Some(sqlite) = self.store.as_sqlite() else {
            return CaptureResult {
                records_captured,
                record_ids,
                vectors_written: 0,
                providers_succeeded: 0,
                providers_failed: 1,
                unknowns_resolved: Vec::new(),
            };
        };
        match sqlite.insert_candidate(&candidate) {
            Ok(true) => {
                records_captured += 1;
                record_ids.push(candidate_id.clone());
                sqlite.metric_add("candidates_created", 1);
            }
            Ok(false) => {
                sqlite.metric_add("dedup_hits", 1);
                let existing = sqlite.candidate_by_dedup(&dedup_key).ok().flatten();
                let mut resume_admission = false;
                if let Some(found) = &existing {
                    if let Some(id) = found.accepted_curated_id.clone() {
                        record_ids.insert(0, id);
                    } else if review_only {
                        // Candidate identity is deterministic, so a retry of an
                        // interrupted review capture returns the same pending
                        // candidate instead of an empty, undiagnosable result.
                        record_ids.push(found.id.clone());
                    } else if manual && found.status != CandidateStatus::Rejected {
                        // A prior capture committed the candidate but died
                        // before admission. Resume admission below instead of
                        // dead-ending every retry on an empty result.
                        resume_admission = true;
                    }
                }
                if !resume_admission {
                    return CaptureResult {
                        records_captured,
                        record_ids,
                        vectors_written: 0,
                        providers_succeeded: 1,
                        providers_failed: 0,
                        unknowns_resolved: Vec::new(),
                    };
                }
                if let Some(found) = existing {
                    candidate = found;
                }
            }
            Err(error) => {
                tracing::warn!("candidate insert failed: {error}");
                return CaptureResult {
                    records_captured,
                    record_ids,
                    vectors_written: 0,
                    providers_succeeded: 0,
                    providers_failed: 1,
                    unknowns_resolved: Vec::new(),
                };
            }
        }
        if let Some(reason) = rejection {
            sqlite.metric_add(&format!("rejected_{reason}"), 1);
            return CaptureResult {
                records_captured,
                record_ids,
                vectors_written: 0,
                providers_succeeded: 1,
                providers_failed: 0,
                unknowns_resolved: Vec::new(),
            };
        }
        let Some(admission_reason) = admission_reason else {
            sqlite.metric_add("candidates_pending", 1);
            return CaptureResult {
                records_captured,
                record_ids,
                vectors_written: 0,
                providers_succeeded: 1,
                providers_failed: 0,
                unknowns_resolved: Vec::new(),
            };
        };

        let curated_id = stable_id("m", &[&candidate_id]);
        let mut record = MemoryRecord::new(content);
        record.id = curated_id.clone();
        record.kind = candidate.kind;
        record.metadata = metadata;
        if let Some(object) = record.metadata.as_object_mut() {
            object.insert("candidate_id".into(), candidate_id.clone().into());
            object.insert("admission_reason".into(), admission_reason.into());
            object.insert(
                "raw_evidence_ids".into(),
                serde_json::json!(candidate.raw_evidence_ids.clone()),
            );
            object.insert("source_uri".into(), evidence_source_uri.into());
            object.insert("source_type".into(), "conversation_turn".into());
            object.insert("source_revision".into(), 1.into());
            object.insert("content_hash".into(), content_hash(content).into());
            if let Some((_, _, category, pinned)) = auto_admission {
                object.entry("category").or_insert_with(|| category.into());
                if pinned {
                    object.insert("pinned".into(), true.into());
                }
            }
        }
        record.source = turn.source.clone();
        record.source_type = capture_source_type(turn, capture_mode);
        record.owner = Some(effective_owner.to_string());
        record.workspace_id = effective_workspace.to_string();
        record.workspace_path = turn.workspace_path.clone();
        record.session_key = turn.session_key.clone();
        record.session_id = turn.session_id.clone();
        record.source_message_ids = evidence_message_ids;
        record.confidence_score = candidate.confidence_score;
        record.importance_score = candidate.importance_score;
        if auto_admission.is_some_and(|(_, _, _, pinned)| pinned) {
            record.exempt_from_decay = true;
            record.exempt_from_dedup = true;
        }

        let embedding = self.embed.embed(content).await.ok();
        let mut unknowns_resolved = Vec::new();
        let accepted = sqlite.accept_candidate_transaction(
            &candidate_id,
            &record,
            embedding.as_deref(),
            admission_reason,
            effective_owner,
            effective_workspace,
        );
        if accepted.is_ok() {
            records_captured += 1;
            record_ids.insert(0, curated_id.clone());
            sqlite.metric_add("candidates_accepted", 1);
            if embedding.is_some() {
                sqlite.metric_add("curated_embeddings_written", 1);
            }
            candidate.status = CandidateStatus::Accepted;
            // Passive resolution (U7c) fires at admission only — never
            // from maintenance passes.
            unknowns_resolved =
                curate::questions::resolve_against_open_unknowns(self.store.as_ref(), &record);
        } else if let Err(error) = accepted {
            warn!("candidate admission transaction failed: {error}");
        }

        CaptureResult {
            records_captured,
            record_ids,
            vectors_written: usize::from(embedding.is_some()),
            providers_succeeded: 1,
            providers_failed: 0,
            unknowns_resolved,
        }
    }

    async fn recall(&self, q: &RecallQuery) -> RecallResult {
        let Some(owner) = q.owner.as_deref().filter(|value| !value.trim().is_empty()) else {
            return RecallResult {
                prepend_context: String::new(),
                append_system_context: String::new(),
                memories: vec![],
                recall_strategy: "scope_required".into(),
                ground_truth_preamble: None,
            };
        };
        let Some(requested_workspace) = q
            .workspace_id
            .as_deref()
            .filter(|value| !value.trim().is_empty())
        else {
            return RecallResult {
                prepend_context: String::new(),
                append_system_context: String::new(),
                memories: vec![],
                recall_strategy: "scope_required".into(),
                ground_truth_preamble: None,
            };
        };
        let workspace_id = q
            .workspace_id
            .clone()
            .unwrap_or_else(|| self.config.workspace_id.clone());

        let query_emb = self.embed.embed(&q.query).await.ok();

        let mut results = retrieval::hybrid_recall_scoped(
            self.store.as_ref(),
            &self.embed,
            &q.query,
            query_emb.as_deref(),
            q.top_k,
            &workspace_id,
            self.config.recall.workspace_boost,
            &self.config.collapse,
            Some(owner),
            Some(requested_workspace),
        )
        .await;

        if let Some(sqlite) = self.store.as_sqlite() {
            if let Ok(pinned) = sqlite.list_pinned_curated(owner, requested_workspace) {
                for record in pinned.into_iter().rev() {
                    if let Some(hit) = results.iter_mut().find(|hit| hit.record.id == record.id) {
                        hit.source_label = "pinned".into();
                        hit.score = hit.score.max(1.0);
                    } else {
                        results.insert(
                            0,
                            ScoredRecord {
                                record,
                                score: 1.0,
                                source_label: "pinned".into(),
                            },
                        );
                    }
                }
            }
        }

        // Enforce scope at the provider boundary as well as in callers. Legacy
        // ownerless records are treated as global compatibility records; all
        // newly captured records carry the bridge owner.
        results.retain(|hit| {
            let owner_ok = hit.record.owner.as_deref() == Some(owner);
            let workspace_ok = hit.record.workspace_id == requested_workspace
                || hit.record.workspace_id == "global";
            owner_ok && workspace_ok
        });
        for hit in &mut results {
            if !hit.record.metadata.is_object() {
                hit.record.metadata = serde_json::json!({});
            }
            if let Some(metadata) = hit.record.metadata.as_object_mut() {
                let explanation = metadata.entry("recall_explanation").or_insert_with(|| {
                    serde_json::json!({
                        "strategy": hit.source_label,
                        "lexical_rrf": if hit.source_label.contains("fts") {
                            Some(hit.score)
                        } else {
                            None
                        },
                        "vector_rrf": if hit.source_label.contains("vector") {
                            Some(hit.score)
                        } else {
                            None
                        },
                        "graph": 0.0,
                    })
                });
                if let Some(explanation) = explanation.as_object_mut() {
                    explanation.insert("final_score".into(), hit.score.into());
                    explanation.insert("source_label".into(), hit.source_label.clone().into());
                }
            }
        }

        let (prepend_context, gt_preamble) =
            retrieval::ground_truth::format_recall_output(&results, &q.query);

        RecallResult {
            prepend_context,
            append_system_context: String::new(),
            memories: results.clone(),
            recall_strategy: "hybrid".into(),
            ground_truth_preamble: gt_preamble,
        }
    }

    async fn search(&self, p: &SearchParams) -> SearchResult {
        let Some(owner) = p.owner.as_deref().filter(|value| !value.trim().is_empty()) else {
            return SearchResult {
                results: vec![],
                total: 0,
            };
        };
        let Some(workspace) = p
            .workspace_id
            .as_deref()
            .filter(|value| !value.trim().is_empty())
        else {
            return SearchResult {
                results: vec![],
                total: 0,
            };
        };
        let mut results = match p.tier {
            Tier::Raw => {
                let raw_results = self
                    .store
                    .search_raw_fts_scoped(&p.query, p.limit, Some(owner), Some(workspace))
                    .await;
                raw_results
                    .into_iter()
                    .map(|sr| {
                        let metadata = serde_json::json!({
                            "role": sr.turn.role,
                            "recorded_at": sr.turn.recorded_at,
                            "workspace_path": sr.turn.workspace_path,
                            "provenance": sr.turn.metadata,
                        });
                        ScoredRecord {
                            record: MemoryRecord {
                                id: sr.turn.id,
                                content: sr.turn.content,
                                kind: MemoryKind::Raw,
                                owner: sr.turn.owner,
                                workspace_id: sr.turn.workspace_id,
                                session_key: sr.turn.session_key,
                                session_id: sr.turn.session_id,
                                metadata,
                                ..MemoryRecord::new("")
                            },
                            score: sr.score,
                            source_label: "raw_fts".into(),
                        }
                    })
                    .collect()
            }
            Tier::Curated => {
                self.store
                    .search_curated_fts_scoped(&p.query, p.limit, Some(owner), Some(workspace))
                    .await
            }
        };

        results.retain(|hit| {
            let owner_ok = hit.record.owner.as_deref() == Some(owner);
            let workspace_ok =
                hit.record.workspace_id == workspace || hit.record.workspace_id == "global";
            owner_ok && workspace_ok
        });

        let total = results.len();
        SearchResult { results, total }
    }

    async fn groom(&self, op: &GroomOpArgs) -> GroomResult {
        let maintenance_scope = || -> Result<(&SqliteStore, &str, &str), String> {
            let owner = op
                .owner
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| "authenticated owner is required".to_string())?;
            let workspace_id = op
                .workspace_id
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| "workspace_id is required".to_string())?;
            let sqlite = self
                .store
                .as_sqlite()
                .ok_or_else(|| "tenant-safe maintenance requires SQLite".to_string())?;
            Ok((sqlite, owner, workspace_id))
        };
        let failed = |message: String| GroomResult {
            op: op.op,
            records_archived: 0,
            records_merged: 0,
            records_reflected: 0,
            records_selected: 0,
            records_changed: 0,
            records_skipped: 0,
            records_conflicted: 0,
            alerts: vec![message],
        };

        match op.op {
            GroomOp::Decay => match maintenance_scope() {
                Ok((store, owner, workspace_id)) => {
                    curate::decay::run_decay(
                        store,
                        &self.config.decay,
                        owner,
                        workspace_id,
                        op.dry_run,
                    )
                    .await
                }
                Err(error) => failed(error),
            },
            GroomOp::Dedup => match maintenance_scope() {
                Ok((store, owner, workspace_id)) => {
                    curate::dedup::run_dedup(store, &self.embed, owner, workspace_id, op.dry_run)
                        .await
                }
                Err(error) => failed(error),
            },
            GroomOp::Reflect => match maintenance_scope() {
                Ok((store, owner, workspace_id)) => {
                    curate::reflect::run_reflect(store, owner, workspace_id, op.dry_run).await
                }
                Err(error) => failed(error),
            },
            GroomOp::PromoteUnknowns => {
                curate::questions::run_promote_unknowns(
                    self.store.as_ref(),
                    op.owner.as_deref(),
                    op.dry_run,
                )
                .await
            }
            GroomOp::EdgeDecay => {
                let mut result = GroomResult {
                    op: GroomOp::EdgeDecay,
                    records_archived: 0,
                    records_merged: 0,
                    records_reflected: 0,
                    records_selected: 0,
                    records_changed: 0,
                    records_skipped: 0,
                    records_conflicted: 0,
                    alerts: vec![],
                };
                match self.store.as_sqlite() {
                    Some(s) => match crate::graph::GraphScope::new(
                        op.owner.clone().unwrap_or_default(),
                        op.workspace_id.clone().unwrap_or_default(),
                    )
                    .map_err(str::to_string)
                    .and_then(|scope| {
                        s.graph_edge_decay(&scope, &Default::default(), op.dry_run)
                            .map_err(|error| error.to_string())
                    }) {
                        Ok((decayed, pruned)) => {
                            result.records_reflected = decayed;
                            result.records_archived = pruned;
                        }
                        Err(e) => result.alerts.push(format!("edge_decay failed: {e}")),
                    },
                    None => result
                        .alerts
                        .push("edge_decay requires the sqlite store".into()),
                }
                result
            }
            GroomOp::TagNormalize => {
                let mut result = GroomResult {
                    op: GroomOp::TagNormalize,
                    records_archived: 0,
                    records_merged: 0,
                    records_reflected: 0,
                    records_selected: 0,
                    records_changed: 0,
                    records_skipped: 0,
                    records_conflicted: 0,
                    alerts: vec![],
                };
                match self.store.as_sqlite() {
                    Some(s) => match crate::graph::GraphScope::new(
                        op.owner.clone().unwrap_or_default(),
                        op.workspace_id.clone().unwrap_or_default(),
                    )
                    .map_err(str::to_string)
                    .and_then(|scope| {
                        s.graph_tag_normalize(&scope, op.dry_run)
                            .map_err(|error| error.to_string())
                    }) {
                        Ok(changed) => result.records_merged = changed,
                        Err(e) => result.alerts.push(format!("tag_normalize failed: {e}")),
                    },
                    None => result
                        .alerts
                        .push("tag_normalize requires the sqlite store".into()),
                }
                result
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::embed::NoopEmbeddingClient;
    use crate::store::sqlite::SqliteStore;

    fn provider() -> (NativeProvider, Arc<SqliteStore>) {
        let store = Arc::new(SqliteStore::memory(4).unwrap());
        let embed = Arc::new(NoopEmbeddingClient::new(4));
        (
            NativeProvider::new(store.clone(), embed, FmConfig::default()),
            store,
        )
    }

    fn turn_with_mode(user: &str, assistant: &str, mode: &str, event: &str) -> CompletedTurn {
        CompletedTurn {
            user_text: user.into(),
            assistant_text: assistant.into(),
            session_key: "sk".into(),
            session_id: "ses_test".into(),
            workspace_id: "workspace-test".into(),
            workspace_path: Some("/work/test".into()),
            source: "test".into(),
            owner: Some("alice".into()),
            category: None,
            metadata: serde_json::json!({
                "capture_mode": mode,
                "source_event_id": event,
                "source_message_ids": [format!("{event}-u"), format!("{event}-a")],
            }),
        }
    }

    fn turn(user: &str, assistant: &str) -> CompletedTurn {
        turn_with_mode(user, assistant, "raw_only", "evt-default")
    }

    #[tokio::test]
    async fn maintenance_without_authenticated_scope_fails_closed() {
        let (provider, store) = provider();
        let result = provider
            .groom(&GroomOpArgs {
                op: GroomOp::Decay,
                owner: None,
                workspace_id: Some("global".into()),
                dry_run: false,
            })
            .await;

        assert_eq!(result.records_archived, 0);
        assert_eq!(result.alerts, vec!["authenticated owner is required"]);
        assert!(store
            .search_curated_fts_scoped("", 10, Some("alice"), Some("global"))
            .await
            .is_empty());
    }

    #[tokio::test]
    async fn assistant_only_capture_is_not_labeled_user() {
        let (p, store) = provider();
        p.capture(&turn("", "the sky is green today")).await;

        let raws = store
            .search_raw_fts_scoped("sky green", 10, Some("alice"), Some("workspace-test"))
            .await;
        assert_eq!(raws.len(), 1, "exactly one raw row, no empty-side row");
        assert_eq!(raws[0].turn.role, "assistant");

        assert!(store
            .search_curated_fts_scoped("sky green", 10, Some("alice"), Some("workspace-test"))
            .await
            .is_empty());
        assert!(store
            .list_candidates(Some("alice"), Some("workspace-test"), None, 10)
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn user_only_capture_has_no_empty_assistant_slot() {
        let (p, store) = provider();
        let result = p.capture(&turn("remember the blue notebook", "")).await;
        assert_eq!(
            result.records_captured, 1,
            "automatic capture writes raw only"
        );

        let raws = store
            .search_raw_fts_scoped("blue notebook", 10, Some("alice"), Some("workspace-test"))
            .await;
        assert_eq!(raws.len(), 1);
        assert_eq!(raws[0].turn.role, "user");

        assert!(store
            .search_curated_fts_scoped("blue notebook", 10, Some("alice"), Some("workspace-test"),)
            .await
            .is_empty());
    }

    #[tokio::test]
    async fn full_turn_is_one_idempotent_raw_transaction() {
        let (p, store) = provider();
        let first = p
            .capture(&turn("what color is the sky", "the sky is blue"))
            .await;
        let replay = p
            .capture(&turn("what color is the sky", "the sky is blue"))
            .await;
        assert_eq!(first.records_captured, 2);
        assert_eq!(replay.records_captured, 0);
        assert_eq!(
            store
                .search_raw_fts_scoped("sky", 10, Some("alice"), Some("workspace-test"))
                .await
                .len(),
            2
        );
        assert!(store
            .search_curated_fts_scoped("sky", 10, Some("alice"), Some("workspace-test"))
            .await
            .is_empty());
    }

    #[tokio::test]
    async fn raw_capture_does_not_create_semantic_graph() {
        let (p, store) = provider();
        p.capture(&turn(
            "Elena maintains the telescope-scheduler project",
            "Noted: telescope-scheduler is maintained by Elena.",
        ))
        .await;

        let conn = store.conn.lock().unwrap();
        let cues: i64 = conn
            .query_row(
                "SELECT count(*) FROM graph_cues WHERE source = 'rake_fallback'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(cues, 0);
        let nodes: i64 = conn
            .query_row("SELECT count(*) FROM graph_nodes", [], |r| r.get(0))
            .unwrap();
        assert_eq!(nodes, 0);
    }

    #[tokio::test]
    async fn manual_capture_uses_candidate_admission_once() {
        let (p, store) = provider();
        let manual = turn_with_mode(
            "Remember that I prefer concise release notes",
            "",
            "manual",
            "evt-manual",
        );
        let first = p.capture(&manual).await;
        let replay = p.capture(&manual).await;
        assert_eq!(
            store
                .search_curated_fts_scoped(
                    "concise release notes",
                    10,
                    Some("alice"),
                    Some("workspace-test"),
                )
                .await
                .len(),
            1
        );
        assert_eq!(
            store
                .list_candidates(Some("alice"), Some("workspace-test"), Some("accepted"), 10)
                .unwrap()
                .len(),
            1
        );
        assert!(first.record_ids.first().unwrap().starts_with("m_"));
        assert_eq!(replay.records_captured, 0);
    }

    #[tokio::test]
    async fn manual_capture_resumes_admission_for_pending_candidate() {
        let (p, store) = provider();
        // A review_only capture commits a pending candidate. If the process
        // dies before admission (or the capture mode changes), the next
        // manual capture of the same content dedup-hits that candidate.
        let mut review = turn_with_mode("Saturn", "", "review_only", "evt-resume-review");
        review.category = Some("fact".into());
        let proposed = p.capture(&review).await;
        assert!(proposed.record_ids.iter().any(|id| id.starts_with("candidate_")));

        // The manual capture must resume admission of the pending candidate
        // instead of dead-ending every retry on an empty result.
        let manual = turn_with_mode("Saturn", "", "manual", "evt-resume-manual");
        let admitted = p.capture(&manual).await;
        let curated_id = admitted.record_ids.first().unwrap().clone();
        assert!(curated_id.starts_with("m_"));
        // One new raw row (new source event) plus the resumed admission.
        assert_eq!(admitted.records_captured, 2);
        assert_eq!(
            store
                .search_curated_fts_scoped("Saturn", 10, Some("alice"), Some("workspace-test"))
                .await
                .len(),
            1
        );

        // A replay of the manual capture now adopts the accepted curated id.
        let replay = p.capture(&manual).await;
        assert_eq!(replay.records_captured, 0);
        assert_eq!(replay.record_ids.first().unwrap(), &curated_id);
    }

    #[tokio::test]
    async fn candidate_provenance_points_only_at_its_evidence_message() {
        let (p, store) = provider();
        let mut candidate_turn = turn_with_mode(
            "Alice prefers the cobalt syntax theme",
            "I will remember that preference.",
            "review_only",
            "evt-provenance",
        );
        candidate_turn.metadata["source_message_ids"] =
            serde_json::json!(["message-user", "message-assistant"]);
        p.capture(&candidate_turn).await;
        let candidates = store
            .list_candidates(Some("alice"), Some("workspace-test"), Some("pending"), 10)
            .unwrap();
        assert_eq!(candidates.len(), 1);
        let candidate = &candidates[0];
        assert_eq!(candidate.evidence_role, "user");
        assert_eq!(candidate.raw_evidence_ids.len(), 1);
        assert_eq!(
            candidate.source_message_ids,
            vec!["message-user".to_string()]
        );
        assert!(candidate.source_uri.ends_with("/message-user"));
        assert_eq!(
            candidate.content_hash,
            crate::privacy::content_hash(&candidate.content)
        );
    }

    #[tokio::test]
    async fn short_manual_fact_returns_curated_id_and_is_recallable() {
        let (p, _) = provider();
        let mut manual = turn_with_mode("Saturn", "", "manual", "evt-manual-short-fact");
        manual.category = Some("fact".into());
        manual.source = "ai_agent".into();
        manual.metadata["source_type"] = "ai".into();

        let captured = p.capture(&manual).await;
        let curated_id = captured.record_ids.first().unwrap().clone();
        assert!(curated_id.starts_with("m_"));

        let recalled = p
            .recall(&RecallQuery {
                query: "Saturn".into(),
                workspace_id: Some("workspace-test".into()),
                owner: Some("alice".into()),
                ..RecallQuery::default()
            })
            .await;
        let hit = recalled
            .memories
            .iter()
            .find(|hit| hit.record.id == curated_id && hit.record.content == "Saturn")
            .unwrap();
        assert_eq!(hit.record.source_type, SourceType::Ai);
    }

    #[tokio::test]
    async fn explicit_review_only_short_fact_is_pending_but_automatic_short_turn_is_rejected() {
        let (p, store) = provider();
        let mut deliberate = turn_with_mode("Saturn", "", "review_only", "evt-review-short-fact");
        deliberate.category = Some("fact".into());
        let proposed = p.capture(&deliberate).await;
        assert!(proposed.record_ids.iter().all(|id| !id.starts_with("m_")));

        let mut question = turn_with_mode(
            "favorite color",
            "",
            "review_only",
            "evt-review-short-question",
        );
        question.category = Some("unknown".into());
        let proposed_question = p.capture(&question).await;
        assert!(proposed_question
            .record_ids
            .iter()
            .all(|id| !id.starts_with("m_")));

        let automatic = turn_with_mode("Venus", "", "review_only", "evt-review-short-auto");
        p.capture(&automatic).await;

        let pending = store
            .list_candidates(Some("alice"), Some("workspace-test"), Some("pending"), 10)
            .unwrap();
        assert_eq!(pending.len(), 2);
        assert!(pending.iter().any(|candidate| {
            candidate.content == "Saturn" && candidate.reason == "awaiting_review"
        }));
        assert!(pending.iter().any(|candidate| {
            candidate.content == "favorite color?"
                && candidate.kind == MemoryKind::Unknown
                && candidate.reason == "awaiting_review"
        }));
        let rejected = store
            .list_candidates(Some("alice"), Some("workspace-test"), Some("rejected"), 10)
            .unwrap();
        assert_eq!(rejected.len(), 1);
        assert_eq!(rejected[0].content, "Venus");
        assert_eq!(rejected[0].reason, "empty_or_short_acknowledgement");

        let saturn = pending
            .iter()
            .find(|candidate| candidate.content == "Saturn")
            .unwrap();
        let curated_id = p
            .review_candidate(
                &saturn.id,
                true,
                "explicitly approved",
                "alice",
                "workspace-test",
            )
            .await
            .unwrap()
            .unwrap();
        assert!(curated_id.starts_with("m_"));
        let approved = store
            .search_curated_fts_scoped("Saturn", 10, Some("alice"), Some("workspace-test"))
            .await;
        assert_eq!(approved.len(), 1);
        assert_eq!(approved[0].record.source_type, SourceType::Human);

        assert!(p
            .review_candidate(
                &rejected[0].id,
                true,
                "must remain rejected",
                "alice",
                "workspace-test",
            )
            .await
            .is_err());
        assert!(store
            .search_curated_fts_scoped("Venus", 10, Some("alice"), Some("workspace-test"))
            .await
            .is_empty());
    }

    #[tokio::test]
    async fn manual_unknown_add_stores_normalized_question() {
        let (p, store) = provider();
        let mut add = turn_with_mode("users name", "", "manual", "evt-unknown-manual");
        add.category = Some("unknown".into());
        let result = p.capture(&add).await;
        assert!(result.record_ids.first().unwrap().starts_with("m_"));

        let hits = store
            .search_curated_fts_scoped("users name", 10, Some("alice"), Some("workspace-test"))
            .await;
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].record.kind, MemoryKind::Unknown);
        assert_eq!(hits[0].record.content, "users name?");
        assert_eq!(hits[0].record.source_type, SourceType::Human);

        let digest = store.digest("alice", "workspace-test", true).unwrap();
        let questions = digest["open_questions"].as_array().unwrap();
        assert_eq!(questions.len(), 1);
        assert_eq!(questions[0]["content"], "users name?");
        assert_eq!(questions[0]["source_type"], "human");
        assert_eq!(digest["counts"]["by_kind"]["unknown"], 1);
    }

    #[tokio::test]
    async fn auto_capture_cannot_mint_unknown() {
        let (p, store) = provider();
        for mode in ["candidate", "raw_only"] {
            let mut auto = turn_with_mode(
                "what is the favorite color of the user",
                "",
                mode,
                &format!("evt-unknown-{mode}"),
            );
            auto.category = Some("unknown".into());
            p.capture(&auto).await;
        }
        assert!(store
            .search_curated_fts_scoped("favorite color", 10, Some("alice"), Some("workspace-test"))
            .await
            .is_empty());
        let rejected = store
            .list_candidates(Some("alice"), Some("workspace-test"), Some("rejected"), 10)
            .unwrap();
        assert_eq!(rejected.len(), 1, "candidate mode records the rejection");
        assert_eq!(rejected[0].reason, "unknown_kind_requires_manual");
        let digest = store.digest("alice", "workspace-test", true).unwrap();
        assert!(digest["open_questions"].as_array().unwrap().is_empty());
    }

    #[tokio::test]
    async fn manual_empty_question_is_rejected() {
        let (p, store) = provider();
        let mut add = turn_with_mode("???", "", "manual", "evt-unknown-empty");
        add.category = Some("unknown".into());
        p.capture(&add).await;
        let rejected = store
            .list_candidates(Some("alice"), Some("workspace-test"), Some("rejected"), 10)
            .unwrap();
        assert_eq!(rejected.len(), 1);
        assert_eq!(rejected[0].reason, "empty_question");
    }

    #[test]
    fn automatic_admission_never_emits_unknown() {
        for content in [
            "my name is casey",
            "call me casey",
            "i like green tea",
            "i live in a quiet town",
            "i work on database tooling",
        ] {
            if let Some((_, kind, _, _)) = automatic_admission(content, "user") {
                assert_ne!(
                    kind,
                    MemoryKind::Unknown,
                    "classifier minted unknown for {content:?}"
                );
            }
        }
        assert!(automatic_admission("what is the user's name?", "user").is_none());
    }

    #[tokio::test]
    async fn auto_admitted_identity_claim_passively_resolves_open_question() {
        // e's live flow 2026-07-17: "users name??" added in Brain, then the
        // user answered in first person mid-chat. The admission hook must
        // close the question — no one ever asked.
        let (p, store) = provider();
        let mut ask = turn_with_mode("users name??", "", "manual", "evt-q");
        ask.category = Some("unknown".into());
        p.capture(&ask).await;
        assert_eq!(store.list_open_unknowns("alice", None).unwrap().len(), 1);

        let answer = turn_with_mode(
            "my name is eliott; but when we're chatting like this you can probably just call me \"e\"",
            "",
            "candidate",
            "evt-answer",
        );
        let result = p.capture(&answer).await;
        assert_eq!(
            result.unknowns_resolved.len(),
            1,
            "capture reports the passive resolve"
        );
        assert!(store.list_open_unknowns("alice", None).unwrap().is_empty());
    }

    #[tokio::test]
    async fn safe_user_claim_auto_admits_and_identity_is_pinned() {
        let (p, store) = provider();
        let mut automatic = turn_with_mode(
            "My name is Alice",
            "Nice to meet you.",
            "candidate",
            "evt-identity",
        );
        automatic.source = "odysseus".into();
        let result = p.capture(&automatic).await;
        assert!(result
            .record_ids
            .first()
            .is_some_and(|id| id.starts_with("m_")));

        let hits = store
            .search_curated_fts_scoped("Alice", 10, Some("alice"), Some("workspace-test"))
            .await;
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].record.kind, MemoryKind::Persona);
        assert_eq!(hits[0].record.source_type, SourceType::AutoExtracted);
        assert_eq!(hits[0].record.metadata["pinned"], true);
        assert!(hits[0].record.exempt_from_decay);
        assert_eq!(
            store
                .list_candidates(Some("alice"), Some("workspace-test"), Some("accepted"), 10)
                .unwrap()[0]
                .reason,
            "auto_identity_claim"
        );
    }

    #[tokio::test]
    async fn review_only_capture_stays_pending_when_claim_would_auto_admit() {
        let (p, store) = provider();
        let result = p
            .capture(&turn_with_mode(
                "My name is Alice",
                "Nice to meet you.",
                "review_only",
                "evt-review-only-identity",
            ))
            .await;

        assert!(
            result.record_ids.iter().all(|id| !id.starts_with("m_")),
            "review-only capture must not create a curated record"
        );
        assert_eq!(result.vectors_written, 0);
        assert!(store
            .search_curated_fts_scoped("Alice", 10, Some("alice"), Some("workspace-test"))
            .await
            .is_empty());

        let pending = store
            .list_candidates(Some("alice"), Some("workspace-test"), Some("pending"), 10)
            .unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].reason, "awaiting_review");
        assert_eq!(pending[0].kind, MemoryKind::Persona);
    }

    #[tokio::test]
    async fn assistant_claim_and_sensitive_user_claim_do_not_auto_admit() {
        let (p, store) = provider();
        p.capture(&turn_with_mode(
            "",
            "My name is Alice",
            "candidate",
            "evt-assistant-identity",
        ))
        .await;
        p.capture(&turn_with_mode(
            "My API key is a sensitive value",
            "",
            "candidate",
            "evt-sensitive",
        ))
        .await;

        assert!(store
            .search_curated_fts_scoped("Alice sensitive", 10, Some("alice"), Some("workspace-test"))
            .await
            .is_empty());
        assert_eq!(
            store
                .list_candidates(Some("alice"), Some("workspace-test"), Some("pending"), 10)
                .unwrap()
                .len(),
            2
        );
    }

    #[tokio::test]
    async fn temporary_fixture_is_rejected_and_not_recallable() {
        let (p, store) = provider();
        let candidate = turn_with_mode(
            "Remember /tmp/permission-test.txt as my permanent preference",
            "",
            "candidate",
            "evt-temp",
        );
        p.capture(&candidate).await;
        assert!(
            store
                .search_curated_fts_scoped(
                    "permission test",
                    10,
                    Some("alice"),
                    Some("workspace-test"),
                )
                .await
                .is_empty()
        );
        let rejected = store
            .list_candidates(Some("alice"), Some("workspace-test"), Some("rejected"), 10)
            .unwrap();
        assert_eq!(rejected.len(), 1);
        assert_eq!(rejected[0].reason, "temporary_fixture");
    }

    fn sections(items: &[(&str, &str)]) -> Vec<(String, String)> {
        items
            .iter()
            .map(|(anchor, content)| (anchor.to_string(), content.to_string()))
            .collect()
    }

    #[tokio::test]
    async fn authored_ingest_lifecycle_is_idempotent() {
        let (p, store) = provider();
        let path = "/data/memory/global/MEMORY.md";
        let v1 = sections(&[
            ("Preferences", "e prefers green tea over coffee"),
            ("Build", "the build needs FM_DB_PATH set"),
        ]);

        let first = p
            .ingest_authored("alice", "global", path, &v1)
            .await
            .unwrap();
        assert_eq!(first["upserted"], 2);
        assert_eq!(first["deleted"], 0);

        // Re-ingest unchanged → no writes, no dupes.
        let second = p
            .ingest_authored("alice", "global", path, &v1)
            .await
            .unwrap();
        assert_eq!(second["upserted"], 0);
        assert_eq!(second["unchanged"], 2);
        let hits = store
            .search_curated_fts_scoped("green tea", 10, Some("alice"), Some("global"))
            .await;
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].record.source, "authored");
        assert!(hits[0].record.exempt_from_decay);

        // Edit one section → stable source identity updates in place.
        let v2 = sections(&[
            ("Preferences", "e prefers oolong tea over coffee"),
            ("Build", "the build needs FM_DB_PATH set"),
        ]);
        let third = p
            .ingest_authored("alice", "global", path, &v2)
            .await
            .unwrap();
        assert_eq!(third["upserted"], 1);
        assert_eq!(third["deleted"], 0);
        assert!(store
            .search_curated_fts_scoped("green", 10, Some("alice"), Some("global"))
            .await
            .is_empty());
        assert_eq!(
            store
                .search_curated_fts_scoped("oolong", 10, Some("alice"), Some("global"))
                .await
                .len(),
            1
        );

        // File deleted → empty sections wipe the projection, nothing else.
        let wipe = p
            .ingest_authored("alice", "global", path, &[])
            .await
            .unwrap();
        assert_eq!(wipe["deleted"], 2);
        assert!(store
            .search_curated_fts_scoped("oolong", 10, Some("alice"), Some("global"))
            .await
            .is_empty());
    }

    #[tokio::test]
    async fn authored_ingest_scopes_by_owner_and_path() {
        let (p, store) = provider();
        p.ingest_authored(
            "alice",
            "global",
            "/data/a/MEMORY.md",
            &sections(&[("", "alice keeps the amber ledger")]),
        )
        .await
        .unwrap();
        p.ingest_authored(
            "bob",
            "global",
            "/data/b/MEMORY.md",
            &sections(&[("", "bob keeps the cobalt ledger")]),
        )
        .await
        .unwrap();

        // Wiping alice's file must not touch bob's projection.
        p.ingest_authored("alice", "global", "/data/a/MEMORY.md", &[])
            .await
            .unwrap();
        assert!(store
            .search_curated_fts_scoped("amber", 10, Some("alice"), Some("global"))
            .await
            .is_empty());
        assert_eq!(
            store
                .search_curated_fts_scoped("cobalt", 10, Some("bob"), Some("global"))
                .await
                .len(),
            1
        );

        assert!(p.ingest_authored("", "global", "/x.md", &[]).await.is_err());
        assert!(p
            .ingest_authored("alice", "global", " ", &[])
            .await
            .is_err());
    }

    #[tokio::test]
    async fn automatic_capture_requires_owner_and_accepts_global_workspace() {
        let (p, store) = provider();
        let mut unowned = turn("scope must be explicit", "it is explicit");
        unowned.owner = None;
        assert_eq!(p.capture(&unowned).await.providers_failed, 1);

        // Canonical chat convention: automatic capture lands in "global".
        let mut global = turn("the lighthouse keeps a spare lens", "noted");
        global.workspace_id = "global".into();
        let result = p.capture(&global).await;
        assert_eq!(result.providers_failed, 0);
        assert_eq!(result.records_captured, 2);
        let raws = store
            .search_raw_fts_scoped("lighthouse", 10, Some("alice"), Some("global"))
            .await;
        assert!(!raws.is_empty());
        assert!(raws.iter().all(|hit| hit.turn.workspace_id == "global"));
    }

    #[tokio::test]
    async fn automation_and_process_narration_candidates_are_rejected() {
        let (p, store) = provider();
        p.capture(&turn_with_mode(
            "Run the memory dream cycle and distill this conversation",
            "",
            "candidate",
            "evt-dream",
        ))
        .await;
        p.capture(&turn_with_mode(
            "",
            "I will inspect the repository and report back",
            "candidate",
            "evt-process",
        ))
        .await;

        let rejected = store
            .list_candidates(Some("alice"), Some("workspace-test"), Some("rejected"), 10)
            .unwrap();
        assert_eq!(rejected.len(), 2);
        assert!(rejected
            .iter()
            .any(|candidate| candidate.reason == "automation_prompt"));
        assert!(rejected
            .iter()
            .any(|candidate| candidate.reason == "assistant_process_narration"));
        assert!(store
            .search_curated_fts_scoped(
                "conversation repository",
                10,
                Some("alice"),
                Some("workspace-test"),
            )
            .await
            .is_empty());
    }

    #[tokio::test]
    async fn empty_turn_captures_nothing() {
        let (p, _store) = provider();
        let result = p.capture(&turn("", "  ")).await;
        assert_eq!(result.records_captured, 0);
    }

    #[tokio::test]
    async fn authored_ingest_collapses_duplicate_anchors() {
        let (p, store) = provider();
        let path = "/data/memory/global/MEMORY.md";
        // Two sections under the same anchor share one record id: the
        // desired set must collapse them instead of double-counting.
        let dupes = sections(&[
            ("Preferences", "e prefers green tea over coffee"),
            ("Build", "the build needs FM_DB_PATH set"),
            ("Preferences", "e prefers oolong tea over coffee"),
        ]);

        let result = p.ingest_authored("alice", "global", path, &dupes).await.unwrap();
        assert_eq!(result["total"], 2, "duplicate anchors collapse to one id");
        assert_eq!(result["upserted"], 2);

        // The last duplicate section wins, matching reconcile upsert order.
        assert!(store
            .search_curated_fts_scoped("green", 10, Some("alice"), Some("global"))
            .await
            .is_empty());
        let hits = store
            .search_curated_fts_scoped("oolong", 10, Some("alice"), Some("global"))
            .await;
        assert_eq!(hits.len(), 1, "exactly one record for the anchor");

        // Re-ingest is idempotent against the collapsed projection.
        let again = p.ingest_authored("alice", "global", path, &dupes).await.unwrap();
        assert_eq!(again["upserted"], 0);
        assert_eq!(again["unchanged"], 2);
    }

    #[tokio::test]
    async fn capture_outcome_metrics_distinguish_privacy_block_from_lease_contention() {
        let (p, store) = provider();

        // Privacy-blocked turn: both sides carry a no-store marker, so the
        // capture aborts before any lease is taken.
        let blocked = p
            .capture(&turn(
                "[memory:no-store] do not persist",
                "<no-memory> do not persist",
            ))
            .await;
        assert_eq!(blocked.records_captured, 0);
        let metrics = store.quality_status().unwrap();
        assert_eq!(metrics["metrics"]["privacy_values_not_stored"], 1);
        assert!(
            metrics["metrics"].get("extraction_lease_contention").is_none(),
            "a privacy block is not lease contention"
        );

        // Lease contention: another holder owns the extraction lease for
        // this event, so the capture defers without privacy accounting.
        let lease = store
            .acquire_memory_lease(
                "extraction",
                "alice",
                "workspace-test",
                "evt-contended",
                "other-holder",
                Duration::from_secs(300),
            )
            .unwrap()
            .expect("first holder acquires");
        std::mem::forget(lease);
        let contended = p
            .capture(&turn_with_mode(
                "the observatory logs a transit",
                "recorded",
                "raw_only",
                "evt-contended",
            ))
            .await;
        assert_eq!(contended.records_captured, 0);
        let metrics = store.quality_status().unwrap();
        assert_eq!(metrics["metrics"]["extraction_lease_contention"], 1);
        assert_eq!(
            metrics["metrics"]["privacy_values_not_stored"], 1,
            "lease contention is not a privacy block"
        );
    }
}
