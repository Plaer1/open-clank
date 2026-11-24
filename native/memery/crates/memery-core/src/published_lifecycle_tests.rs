//! Disposable guarded published lifecycle case.
use super::*;
use crate::ontology::ScopeRef;
use crate::store::sqlite::SqliteStore;

#[test]
fn reviewed_withdrawal_replay_and_reversion() -> Result<(),Box<dyn std::error::Error>> {
    let path=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB")?;
    if !path.starts_with("/tmp/openclank-s06-transaction-queued-") {return Err("disposable shadow required".into());}
    let id=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB_ID")?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&id)?;
    let mut conn=store.conn.lock().map_err(|_|"connection unavailable")?;
    let mut repo=V2Repository::new(&mut conn)?;
    let scope=ScopeRef{owner_id:"alice".into(),workspace_id:Some("ws".into()),project_id:None,session_id:None};
    let original=repo.get_knowledge(&scope,"fixture-block",None)?;
    assert_eq!(original.revision,1);
    assert!(original.source_event_id.is_none() && original.payload.evidence_ids.is_empty());
    let paths=vec!["/status".to_owned()];
    let operation=json!({"version":"s03.published-update.v1","owner":"alice","workspace":"ws","project":"","block_id":"fixture-block","expected_revision":1,"payload":{"status":"retracted"},"request_key":"withdraw-1","actor_type":"user","actor_id":"alice","reason":"retracted by user","source_event_id":null,"approved_paths":paths,"producer":null}).to_string();
    let request=PublishedLifecycleRequest{version:1,owner:"alice".into(),workspace_id:"ws".into(),project_id:None,block_id:"fixture-block".into(),action:"retract".into(),expected_revision:1,target_revision:None,request_key:"withdraw-1".into(),reason:"retracted by user".into(),approved_paths:paths.clone(),account_id:None,original_request_key:None,operation_json:operation};
    let first=repo.apply_published_lifecycle(&request)?;
    assert_eq!(first["state"],"applied");assert_eq!(first["applied_revision"],2);
    assert_eq!(repo.apply_published_lifecycle(&request)?["state"],"already_applied");
    let mut changed=request.clone();changed.request_key="other".into();
    let mut value:Value=serde_json::from_str(&changed.operation_json)?;value["request_key"]=json!("other");changed.operation_json=value.to_string();
    assert!(repo.apply_published_lifecycle(&changed).is_err());
    let operation=json!({"version":"s03.published-update.v1","owner":"alice","workspace":"ws","project":"","block_id":"fixture-block","expected_revision":2,"payload":original.payload,"request_key":"restore-1","actor_type":"user","actor_id":"alice","reason":"revert revision 1","source_event_id":null,"approved_paths":paths,"producer":null}).to_string();
    let restore=PublishedLifecycleRequest{version:1,owner:"alice".into(),workspace_id:"ws".into(),project_id:None,block_id:"fixture-block".into(),action:"revert".into(),expected_revision:2,target_revision:Some(1),request_key:"restore-1".into(),reason:"revert revision 1".into(),approved_paths:paths,account_id:None,original_request_key:None,operation_json:operation};
    let restored=repo.apply_published_lifecycle(&restore)?;assert_eq!(restored["applied_revision"],3);
    let replay=repo.apply_published_lifecycle(&request)?;assert_eq!(replay["applied_revision"],2);assert_eq!(replay["current_revision"],3);assert_eq!(replay["visibility"],"not_rechecked");
    println!("PASS guarded reviewed withdrawal, replay, stale refusal and source-free reversion");Ok(())
}
