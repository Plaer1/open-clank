//! One explicitly provisioned disposable F8 owner case; never a live store.
use super::*;
use crate::store::sqlite::{GuardedBatchForgetRequest,GuardedOwnerPurgeRequest};

#[test]
fn f8_owner_stage_review_forget_purge_and_replay() -> Result<(),Box<dyn std::error::Error>> {
    let path=std::env::var("OPENCLANK_F8_FIXTURE_DB")?;
    if !path.starts_with("/tmp/openclank-f8-owner-") {return Err("explicit disposable F8 fixture required".into());}
    let database=std::env::var("OPENCLANK_F8_FIXTURE_ID")?;
    let policy:PolicyPublicationRefs=serde_json::from_str(&std::env::var("OPENCLANK_F8_FIXTURE_POLICY")?)?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database)?;
    let make_stage=|key:&str,text:&str,uri:&str,target:Option<PreextractedTarget>| PreextractedStageRequest{
        version:1,owner:"alice".into(),workspace_id:"ws".into(),account_id:"account-fixture".into(),policy_projection:policy.clone(),request_key:key.into(),
        items:vec![PreextractedItem{kind:"fact".into(),text:text.into(),source:PreextractedSource{uri:uri.into(),revision:1,content_sha256:"a".repeat(64)},target}],
    };
    let a=make_stage("stage-a","protected value","fixture://a",None);
    let old_path=std::env::var("OPENCLANK_F8_OLD_FIXTURE_DB")?;
    let (old_store,_,_,_,_,_)=SqliteStore::open_guarded(&old_path,4,&database)?;
    assert!(old_store.preextracted_stage(&a).is_err());drop(old_store);
    let staged_a=store.preextracted_stage(&a).map_err(|e|format!("stage-a: {e}"))?;assert_eq!(store.preextracted_stage(&a).map_err(|e|format!("stage-a: {e}"))?,staged_a);
    let id_a=staged_a["items"][0]["candidate_id"].as_str().ok_or("id")?.to_owned();
    let selector=|id:&str| PreextractedSelector{version:1,owner:"alice".into(),workspace_id:"ws".into(),account_id:"account-fixture".into(),policy_projection:policy.clone(),candidate_id:id.into()};
    {
        let mut conn=store.conn.lock().map_err(|_|"lock")?;let tx=conn.transaction()?;
        let candidate=SqliteStore::preextracted_candidate(&tx,"alice","ws",&id_a)?;
        assert!(SqliteStore::insert_candidate_inner_impl(&tx,&candidate,false,false,false,false,false).is_err());
    }
    let preview_a=store.preextracted_review_preview(&selector(&id_a)).map_err(|e|format!("preview-a: {e}"))?;
    let review=|id:&str,digest:&str,key:&str| PreextractedReviewRequest{version:1,owner:"alice".into(),workspace_id:"ws".into(),account_id:"account-fixture".into(),policy_projection:policy.clone(),candidate_id:id.into(),candidate_digest:digest.into(),action:"accept".into(),request_key:key.into(),reason:"owner review".into()};
    let ra=review(&id_a,preview_a["candidate_digest"].as_str().ok_or("digest")?,"review-a");
    let applied_a=store.preextracted_review(&ra).map_err(|e|format!("review-a: {e}"))?;assert_eq!(store.preextracted_review(&ra).map_err(|e|format!("review-a: {e}"))?,applied_a);
    let curated_a=applied_a["curated_id"].as_str().ok_or("curated")?.to_owned();
    let b=make_stage("stage-b","competing value","fixture://b",Some(PreextractedTarget{predicate:"fact".into(),claim_slot:curated_a.clone()}));
    let staged_b=store.preextracted_stage(&b).map_err(|e|format!("stage-b: {e}"))?;assert_eq!(staged_b["items"][0]["state"],"conflict");
    let id_b=staged_b["items"][0]["candidate_id"].as_str().ok_or("id")?.to_owned();
    let mut changed=b.clone();changed.items[0].source.content_sha256="b".repeat(64);assert!(store.preextracted_stage(&changed).is_err());
    let listed=store.preextracted_list(&PreextractedListRequest{version:1,owner:"alice".into(),workspace_id:"ws".into(),account_id:"account-fixture".into(),policy_projection:policy.clone(),after:None,limit:50})?;
    assert!(listed["items"].as_array().ok_or("items")?.iter().any(|v|v["candidate_id"]==id_b && v["evidence_role"]=="inferred"));
    let preview_b=store.preextracted_review_preview(&selector(&id_b)).map_err(|e|format!("preview-b: {e}"))?;assert_eq!(preview_b["conflict"]["classification"],"unresolved");
    let rb=review(&id_b,preview_b["candidate_digest"].as_str().ok_or("digest")?,"review-b");
    let applied_b=store.preextracted_review(&rb).map_err(|e|format!("review-b: {e}"))?;assert_eq!(store.preextracted_review(&rb).map_err(|e|format!("review-b: {e}"))?,applied_b);
    let mut changed_review=rb.clone();changed_review.reason="different reason".into();assert!(store.preextracted_review(&changed_review).is_err());
    let mut foreign=selector(&id_b);foreign.owner="mallory".into();assert!(store.preextracted_review_preview(&foreign).is_err());
    let mut req=GuardedBatchForgetRequest{version:1,owner:"alice".into(),workspace_id:"ws".into(),account_id:"account-fixture".into(),policy_projection:policy.clone(),action:"preview".into(),selector_kind:Some("source_uri".into()),selector:Some("fixture://b".into()),preview_token:None,tombstone_id:None,operation_id:None};
    let preview=store.guarded_batch_forget_preview(&req).map_err(|e|format!("forget-preview: {e}"))?;req.action="commit".into();req.preview_token=Some(preview["token"].as_str().ok_or("token")?.into());req.operation_id=Some("11111111111111111111111111111111".into());
    store.guarded_batch_forget_commit(&req).map_err(|e|format!("forget-commit: {e}"))?;
    let status=GuardedBatchForgetRequest{action:"status".into(),selector_kind:None,selector:None,preview_token:None,..req.clone()};
    assert_eq!(store.guarded_batch_forget_status(&status)?["state"],"committed");
    assert!(store.preextracted_stage(&b).is_err());assert!(store.preextracted_review(&rb).is_err());
    {let conn=store.conn.lock().map_err(|_|"lock")?;let n:i64=conn.query_row("SELECT count(*) FROM curated WHERE id=?1",[&curated_a],|r|r.get(0))?;assert_eq!(n,1);}
    // A real guarded owner revision makes a separately staged target stale.
    let c=make_stage("stage-c","later claim","fixture://c",Some(PreextractedTarget{predicate:"fact".into(),claim_slot:curated_a.clone()}));
    let staged_c=store.preextracted_stage(&c)?;let id_c=staged_c["items"][0]["candidate_id"].as_str().ok_or("id")?.to_owned();
    let before_c=store.preextracted_review_preview(&selector(&id_c))?;let block=before_c["target"]["block_id"].as_str().ok_or("target block")?.to_owned();
    {
        use crate::ontology::ScopeRef;
        use crate::repository::{RevisionActor,V2Repository};
        use crate::field_authority::OwnerReviewContext;
        let scope=ScopeRef{owner_id:"alice".into(),workspace_id:Some("ws".into()),project_id:None,session_id:None};
        let actor=RevisionActor{actor_type:"user".into(),actor_id:"alice".into(),reason:"edit target".into(),source_event_id:None};
        let paths=vec!["/value/value".to_owned()];
        let operation=json!({"version":"s03.published-update.v1","owner":"alice","workspace":"ws","project":"","block_id":block,"expected_revision":1,"payload":{"value":{"type":"string","value":"owner changed value"}},"request_key":"edit-a","actor_type":"user","actor_id":"alice","reason":"edit target","source_event_id":null,"approved_paths":paths,"producer":null}).to_string();
        let authority=OwnerReviewContext{authenticated_owner:"alice".into(),reviewer_id:"alice".into(),expected_revision:1,proposal_key:"edit-a".into(),payload_hash:repository::sha256_hex(operation.as_bytes()),approved_paths:paths};
        let mut conn=store.conn.lock().map_err(|_|"lock")?;let mut repo=V2Repository::new(&mut conn)?;
        repo.guarded_update_knowledge(&scope,&block,1,&actor,"edit-a",&operation,Some(&authority),None,true)?;
    }
    let stale=store.preextracted_review_preview(&selector(&id_c))?;assert_eq!(stale["target"]["stale"],true);
    let rc=review(&id_c,stale["candidate_digest"].as_str().ok_or("digest")?,"review-c");assert!(store.preextracted_review(&rc).is_err());
    let edited_forget=GuardedBatchForgetRequest{action:"preview".into(),selector:Some("fixture://a".into()),preview_token:None,operation_id:None,..req.clone()};
    assert!(store.guarded_batch_forget_preview(&edited_forget).is_err());
    let count=store.owner_counts("alice")?;
    let purge=GuardedOwnerPurgeRequest{version:1,database_id:database,account_id:"account-fixture".into(),operation_id:"purge-f8".into(),step:"memory_fm".into(),generation:1,action:"purge".into(),owner:"alice".into(),expected_source_fingerprint:count["fingerprint"].as_str().ok_or("fingerprint")?.into()};
    let result=store.guarded_purge_owner(&purge).map_err(|e|format!("purge: {e}"))?;assert_eq!(result["version"],1);assert_eq!(result["result"].as_object().ok_or("purge result")?.len(),3);
    assert_eq!(store.guarded_purge_status(&purge)?,result);store.guarded_purge_stamp(&purge)?;
    assert!(store.preextracted_stage(&a).is_err());let mut fresh=a.clone();fresh.request_key="stage-a-fresh".into();assert!(store.preextracted_stage(&fresh).is_ok());
    Ok(())
}

#[test]
fn f8_owner_rename_then_reset_terminal() -> Result<(),Box<dyn std::error::Error>> {
    use crate::store::sqlite::GuardedOwnerRenameRequest;
    let path=std::env::var("OPENCLANK_F8_FIXTURE_DB")?;
    if !path.starts_with("/tmp/openclank-f8-owner-") {return Err("explicit disposable F8 fixture required".into());}
    let database=std::env::var("OPENCLANK_F8_FIXTURE_ID")?;
    let policy:PolicyPublicationRefs=serde_json::from_str(&std::env::var("OPENCLANK_F8_FIXTURE_POLICY")?)?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database)?;
    let mut stage=PreextractedStageRequest{version:1,owner:"alice".into(),workspace_id:"ws".into(),account_id:"account-fixture".into(),policy_projection:policy,request_key:"lifecycle-stage".into(),items:vec![PreextractedItem{kind:"fact".into(),text:"lifecycle fact".into(),source:PreextractedSource{uri:"fixture://lifecycle".into(),revision:1,content_sha256:"a".repeat(64)},target:None}]};
    stage.items.push(PreextractedItem{kind:"fact".into(),text:"retained fact".into(),source:PreextractedSource{uri:"fixture://retained".into(),revision:1,content_sha256:"b".repeat(64)},target:None});
    let staged=store.preextracted_stage(&stage)?;
    let retained=staged["items"][1]["candidate_id"].as_str().ok_or("retained candidate")?.to_owned();
    let selector=PreextractedSelector{version:1,owner:"alice".into(),workspace_id:"ws".into(),account_id:"account-fixture".into(),policy_projection:stage.policy_projection.clone(),candidate_id:retained};
    let preview=store.preextracted_review_preview(&selector)?;
    let review=PreextractedReviewRequest{version:1,owner:"alice".into(),workspace_id:"ws".into(),account_id:"account-fixture".into(),policy_projection:stage.policy_projection.clone(),candidate_id:selector.candidate_id,candidate_digest:preview["candidate_digest"].as_str().ok_or("digest")?.into(),action:"accept".into(),request_key:"retained-review".into(),reason:"retain approved fact".into()};
    store.preextracted_review(&review)?;
    let src=store.owner_counts("alice")?;let dst=store.owner_counts("bob")?;
    let request=GuardedOwnerRenameRequest{version:1,database_id:database,account_id:"account-fixture".into(),operation_id:"rename-f8".into(),step:"fm".into(),generation:1,action:"forward".into(),owner:"alice".into(),new_owner:"bob".into(),expected_source_fingerprint:src["fingerprint"].as_str().ok_or("source fingerprint")?.into(),expected_target_fingerprint:dst["fingerprint"].as_str().ok_or("target fingerprint")?.into(),prior_receipt_id:None,prior_receipt_hash:None,rag_relocation:None};
    store.guarded_rename_owner(&request).map_err(|e|format!("rename: {e}"))?;
    { let conn=store.conn.lock().map_err(|_|"lock")?;
      let current:i64=conn.query_row("SELECT count(*) FROM fm_v2_revision_field_authority WHERE owner_id='bob' AND origin='manual' AND approved_by='bob'",[],|r|r.get(0))?;
      let original:i64=conn.query_row("SELECT count(*) FROM fm_v2_change_sets WHERE owner_id='bob' AND actor_type='user' AND actor_id='alice'",[],|r|r.get(0))?;
      assert!(current>0 && original>0); }
    let mut renamed_review=review.clone();renamed_review.owner="bob".into();assert!(store.preextracted_review(&renamed_review).is_ok());
    stage.owner="bob".into();assert_eq!(store.preextracted_stage(&stage).map_err(|e|format!("renamed replay: {e}"))?,staged);
    let ingest=vec!["ingest".to_string()];
    let selective=store.guarded_selective_ingest_preview("bob","ws","account-fixture",&ingest).map_err(|e|format!("selective preview: {e}"))?;
    store.guarded_selective_ingest_reset("bob","ws","account-fixture","selective-f8",&ingest,&selective["components"],&"a".repeat(64)).map_err(|e|format!("selective reset: {e}"))?;
    assert!(store.preextracted_stage(&stage).is_err());
    let mut survivor=stage.clone();survivor.request_key="survivor-exact".into();survivor.items=vec![stage.items[1].clone()];
    store.preextracted_stage(&survivor).map_err(|e|format!("survivor reuse: {e}"))?;
    let mut altered=survivor.clone();altered.request_key="survivor-changed".into();altered.items[0].source.content_sha256="c".repeat(64);assert!(store.preextracted_stage(&altered).is_err());
    stage.request_key="after-selective".into();stage.items.truncate(1);store.preextracted_stage(&stage).map_err(|e|format!("after-selective stage: {e}"))?;
    // A v1 purge receipt on an old profile remains readable by the current validators.
    { let old_path=std::env::var("OPENCLANK_F8_OLD_FIXTURE_DB")?;let (old,_,_,_,_,_)=SqliteStore::open_guarded(&old_path,4,&request.database_id)?;
      let count=old.owner_counts("legacy")?;let purge=GuardedOwnerPurgeRequest{version:1,database_id:request.database_id.clone(),account_id:"account-fixture".into(),operation_id:"legacy-purge".into(),step:"memory_fm".into(),generation:1,action:"purge".into(),owner:"legacy".into(),expected_source_fingerprint:count["fingerprint"].as_str().ok_or("legacy fingerprint")?.into()};
      let response=old.guarded_purge_owner(&purge)?;assert_eq!(old.guarded_purge_status(&purge)?,response);old.guarded_purge_stamp(&purge)?;
      let conn=old.conn.lock().map_err(|_|"lock")?;let raw:String=conn.query_row("SELECT value FROM fm_meta WHERE key=?1",[response["receipt_id"].as_str().ok_or("legacy receipt")?],|r|r.get(0))?;
      assert_eq!(serde_json::from_str::<serde_json::Value>(&raw)?["version"],1); }
    let components=vec!["memories".to_string()];
    let preview=store.reset_owner("reset_preview","bob",&components,None).map_err(|e|format!("reset preview: {e}"))?;
    store.guarded_reset_owner("bob","ws","account-fixture","reset-f8",&components,&preview["components"],false).map_err(|e|format!("reset: {e}"))?;
    assert!(store.preextracted_stage(&stage).is_err());
    stage.request_key="lifecycle-fresh".into();assert!(store.preextracted_stage(&stage).is_ok());
    Ok(())
}
