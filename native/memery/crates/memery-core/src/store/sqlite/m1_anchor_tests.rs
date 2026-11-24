//! One disposable Host-anchor owner and lifecycle case.
use super::*;
use crate::store::sqlite::{GuardedBatchForgetRequest,GuardedOwnerPurgeRequest,GuardedOwnerRenameRequest};
#[test]
fn host_anchor_owned_lifecycle() -> Result<(),Box<dyn std::error::Error>> {
    let path=std::env::var("OPENCLANK_M1_DB")?;
    if !path.starts_with("/tmp/native-ops-fixture-") {return Err("disposable fixture required".into());}
    let database=std::env::var("OPENCLANK_M1_DB_ID")?;
    let policy:PolicyPublicationRefs=serde_json::from_str(&std::env::var("OPENCLANK_M1_POLICY")?)?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database)?;
    let key=format!("m1op_{}","a".repeat(64));
    let status=HostAnchorStatusRequest{version:1,owner:"alice".into(),workspace_id:"ws-a".into(),account_id:"acct-a".into(),policy_projection:policy.clone(),request_key:key.clone()};
    assert_eq!(store.preextracted_host_anchor_status(&status).map_err(|e|format!("preextracted_host_anchor_status: {e}"))?["state"],"unknown");
    let source=HostAnchorSource{kind:"host_user_body_anchor".into(),coverage:"host_user_body_anchor_only".into(),reference:repository::HostArchiveSourceRef{version:1,kind:"host_user_body".into(),account_id:"acct-a".into(),chat_id:"chat-fixture".into(),actor_id:"main".into(),message_id:"message-fixture".into(),part_id:"body".into(),revision:1,content_hash:"b".repeat(64),workspace_id:Some("files-fixture".into()),project_id:None}};
    let req=HostAnchorStageRequest{version:1,owner:"alice".into(),workspace_id:"ws-a".into(),account_id:"acct-a".into(),policy_projection:policy.clone(),request_key:key.clone(),items:vec![HostAnchorItem{kind:"fact".into(),text:"Synthetic anchored proposal".into(),source:source.clone(),target:None}]};
    let staged=store.preextracted_host_anchor_stage(&req).map_err(|e|format!("preextracted_host_anchor_stage: {e}"))?;assert_eq!(staged["items"][0]["state"],"staged");
    assert_eq!(store.preextracted_host_anchor_stage(&req).map_err(|e|format!("preextracted_host_anchor_stage: {e}"))?,staged);
    let got=store.preextracted_host_anchor_status(&status).map_err(|e|format!("preextracted_host_anchor_status: {e}"))?;assert_eq!(got["state"],"committed");assert_eq!(got["result"],staged);
    let mut changed=req.clone();changed.items[0].text="Different proposal".into();assert!(store.preextracted_host_anchor_stage(&changed).is_err());
    let mut stale=req.clone();stale.request_key=format!("m1op_{}","d".repeat(64));stale.items[0].source.reference.content_hash="e".repeat(64);assert!(store.preextracted_host_anchor_stage(&stale).is_err());
    let external=PreextractedStageRequest{version:1,owner:"alice".into(),workspace_id:"ws-a".into(),account_id:"acct-a".into(),policy_projection:policy.clone(),request_key:"external-stage".into(),items:vec![PreextractedItem{kind:"fact".into(),text:"External synthetic fact".into(),source:PreextractedSource{uri:"fixture://external".into(),revision:1,content_sha256:"f".repeat(64)},target:None}]};
    store.preextracted_stage(&external)?;
    {let conn=store.conn.lock().map_err(|_|"lock")?;let receipt=read_stage_receipt(&conn,"alice",&stage_receipt_id("acct-a","ws-a","external-stage")?)?.ok_or("external receipt")?;assert!(serde_json::to_value(receipt)?.get("origin").is_none());}
    let mut spoof=external.clone();spoof.request_key=key.clone();assert!(store.preextracted_stage(&spoof).is_err());
    let mut duplicate=req.clone();duplicate.request_key=format!("m1op_{}","c".repeat(64));assert_eq!(store.preextracted_host_anchor_stage(&duplicate).map_err(|e|format!("preextracted_host_anchor_stage: {e}"))?["items"][0]["state"],"duplicate");
    let id=staged["items"][0]["candidate_id"].as_str().ok_or("id")?.to_owned();
    let selector=PreextractedSelector{version:1,owner:"alice".into(),workspace_id:"ws-a".into(),account_id:"acct-a".into(),policy_projection:policy.clone(),candidate_id:id.clone()};
    let preview=store.preextracted_review_preview(&selector).map_err(|e|format!("preextracted_review_preview: {e}"))?;assert_eq!(preview["source"]["coverage"],"host_user_body_anchor_only");
    let review=PreextractedReviewRequest{version:1,owner:"alice".into(),workspace_id:"ws-a".into(),account_id:"acct-a".into(),policy_projection:policy.clone(),candidate_id:id.clone(),candidate_digest:preview["candidate_digest"].as_str().ok_or("digest")?.into(),action:"accept".into(),request_key:"anchor-review".into(),reason:"owner review".into()};
    {
        let mut conn=store.conn.lock().map_err(|_|"lock")?;let tx=conn.transaction()?;
        let prepared=repository::prepare_preextracted_candidate_fact(&tx,"alice","ws-a",&review.candidate_id,&review.candidate_digest,&review.reason).map_err(|e|format!("prepare review: {e}"))?;
        SqliteStore::upsert_curated_inner(&tx,&prepared.record,None).map_err(|e|format!("curated review: {e}"))?;
        repository::create_candidate_fact_revision(&tx,"alice","ws-a",&review.request_key,&review.reason,&prepared,&prepared.projection).map_err(|e|format!("revision review: {e}"))?;
    }
    let accepted=store.preextracted_review(&review).map_err(|e|format!("preextracted_review: {e}"))?;
    let curated=accepted["curated_id"].as_str().ok_or("curated")?;
    let src=store.owner_counts("alice")?;let dst=store.owner_counts("bob")?;
    let rename=GuardedOwnerRenameRequest{version:1,database_id:database.clone(),account_id:"acct-a".into(),operation_id:"m1-rename".into(),step:"fm".into(),generation:1,action:"forward".into(),owner:"alice".into(),new_owner:"bob".into(),expected_source_fingerprint:src["fingerprint"].as_str().ok_or("source")?.into(),expected_target_fingerprint:dst["fingerprint"].as_str().ok_or("target")?.into(),prior_receipt_id:None,prior_receipt_hash:None,rag_relocation:None};
    store.guarded_rename_owner(&rename).map_err(|e|format!("rename: {e}"))?;
    {let mut conn=store.conn.lock().map_err(|_|"lock")?;let block:String=conn.query_row("SELECT block_id FROM fm_v2_knowledge_blocks WHERE owner_id='bob' AND claim_slot=?",[curated],|r|r.get(0))?;let repo=repository::V2Repository::new(&mut conn)?;let scope=crate::ontology::ScopeRef{owner_id:"bob".into(),workspace_id:Some("ws-a".into()),project_id:None,session_id:None};repo.get_knowledge(&scope,&block,None).map_err(|e|format!("renamed read: {e}"))?;}
    let mut renamed_status=status.clone();renamed_status.owner="bob".into();assert_eq!(store.preextracted_host_anchor_status(&renamed_status)?["state"],"committed");
    let mut renamed_req=req.clone();renamed_req.owner="bob".into();assert_eq!(store.preextracted_host_anchor_stage(&renamed_req)?,staged);
    let mut pending=renamed_req.clone();pending.request_key=format!("m1op_{}","9".repeat(64));pending.items[0].source.reference.message_id="pending-fixture".into();pending.items[0].text="Pending proposal".into();
    let pending_id=store.preextracted_host_anchor_stage(&pending)?["items"][0]["candidate_id"].as_str().ok_or("pending id")?.to_owned();
    let pending_sid=repository::host_archive_source_id(&pending.items[0].source.reference)?;
    {let conn=store.conn.lock().map_err(|_|"lock")?;conn.execute("UPDATE fm_v2_sources SET forget_state='forgotten' WHERE owner_id='bob' AND source_id=?",[&pending_sid])?;}
    let pending_selector=PreextractedSelector{version:1,owner:"bob".into(),workspace_id:"ws-a".into(),account_id:"acct-a".into(),policy_projection:policy.clone(),candidate_id:pending_id.clone()};
    let unavailable=store.preextracted_review_preview(&pending_selector).map_err(|e|format!("stale preview: {e}"))?;
    assert_eq!(unavailable["source"]["navigation"],"unavailable");assert_eq!(unavailable["text"],"Pending proposal");
    let mut pending_forget=GuardedBatchForgetRequest{version:1,owner:"bob".into(),workspace_id:"ws-a".into(),account_id:"acct-a".into(),policy_projection:policy.clone(),action:"preview".into(),selector_kind:Some("record_id".into()),selector:Some(pending_id),preview_token:None,tombstone_id:None,operation_id:None};
    let pp=store.guarded_batch_forget_preview(&pending_forget).map_err(|e|format!("pending forget preview: {e}"))?;pending_forget.action="commit".into();pending_forget.preview_token=Some(pp["token"].as_str().ok_or("pending token")?.into());pending_forget.operation_id=Some("22222222222222222222222222222222".into());store.guarded_batch_forget_commit(&pending_forget).map_err(|e|format!("pending forget commit: {e}"))?;
    let mut forget=GuardedBatchForgetRequest{version:1,owner:"bob".into(),workspace_id:"ws-a".into(),account_id:"acct-a".into(),policy_projection:policy.clone(),action:"preview".into(),selector_kind:Some("record_id".into()),selector:Some(id),preview_token:None,tombstone_id:None,operation_id:None};
    let preview=store.guarded_batch_forget_preview(&forget).map_err(|e|format!("guarded_batch_forget_preview: {e}"))?;forget.action="commit".into();forget.preview_token=Some(preview["token"].as_str().ok_or("token")?.into());forget.operation_id=Some("11111111111111111111111111111111".into());store.guarded_batch_forget_commit(&forget).map_err(|e|format!("guarded_batch_forget_commit: {e}"))?;
    assert_eq!(store.preextracted_host_anchor_status(&renamed_status).map_err(|e|format!("preextracted_host_anchor_status: {e}"))?["state"],"terminal");assert!(store.preextracted_host_anchor_stage(&renamed_req).is_err());
    let sid=repository::host_archive_source_id(&source.reference)?;
    {let conn=store.conn.lock().map_err(|_|"lock")?;let retained:i64=conn.query_row("SELECT count(*) FROM fm_v2_sources WHERE owner_id='bob' AND source_id=?",[sid],|r|r.get(0))?;assert_eq!(retained,1);}
    let count=store.owner_counts("bob")?;
    let purge=GuardedOwnerPurgeRequest{version:1,database_id:database,account_id:"acct-a".into(),operation_id:"m1-purge".into(),step:"memory_fm".into(),generation:1,action:"purge".into(),owner:"bob".into(),expected_source_fingerprint:count["fingerprint"].as_str().ok_or("fingerprint")?.into()};
    store.guarded_purge_owner(&purge).map_err(|e|format!("guarded_purge_owner: {e}"))?;assert!(store.preextracted_host_anchor_status(&renamed_status).is_err());assert!(store.preextracted_host_anchor_stage(&renamed_req).is_err());
    Ok(())
}
