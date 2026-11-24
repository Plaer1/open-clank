//! One isolated guarded question review case; setup uses real S03 creation.
use super::*;
use crate::ontology::{ScopeRef,PredicateCardinality};
use crate::store::sqlite::SqliteStore;
use crate::field_authority::TrustedProducerContext;

#[test]
fn confirmed_question_requires_exact_subject_and_revision() -> Result<(),Box<dyn std::error::Error>> {
    let path=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB")?;
    if !path.starts_with("/tmp/openclank-s06-transaction-queued-") {return Err("explicit disposable shadow required".into());}
    let database_id=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB_ID")?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database_id)?;
    // A pre-S09 guarded writer lacks this UDF: even an empty archive update
    // must refuse rather than silently retain the former fuzzy behavior.
    {
        let old=rusqlite::Connection::open(&path)?;
        old.create_scalar_function("fm_writer_capability",0,rusqlite::functions::FunctionFlags::SQLITE_UTF8|rusqlite::functions::FunctionFlags::SQLITE_DETERMINISTIC|rusqlite::functions::FunctionFlags::SQLITE_INNOCUOUS,|_|Ok("openclank.frankenmemory.writer.v2"))?;
        assert!(old.execute("UPDATE curated SET archived=archived WHERE 0",[]).is_err());
    }
    let mut conn=store.conn.lock().map_err(|_|"fixture connection unavailable")?;
    let mut repo=V2Repository::new(&mut conn)?;
    assert_eq!(repo.question_semantics_status()?,json!({"version":1,"qualified":true}));
    let scope=ScopeRef{owner_id:"alice".into(),workspace_id:Some("ws".into()),project_id:None,session_id:None};
    let identity=KnowledgeIdentity{block_id:"question-fixture".into(),subject_entity_id:"fixture-entity".into(),predicate:"preferred_address".into(),claim_slot:"address".into(),cardinality:PredicateCardinality::Single};
    let actor=RevisionActor{actor_type:"system".into(),actor_id:"fixture".into(),reason:"fixture question".into(),source_event_id:None};
    let producer=TrustedProducerContext{kind:"automatic".into(),validated_inherited_paths:vec![]};
    let payload=json!({"value":{"type":"null"},"expected_value_type":"string","kind":"open_question","status":"open","activation_rationale":"fixture"});
    let create=json!({"version":"s03.published-create.v1","owner":"alice","workspace":"ws","project":"","block_id":identity.block_id,"subject_entity_id":identity.subject_entity_id,"predicate":identity.predicate,"claim_slot":identity.claim_slot,"cardinality":"single","payload":payload,"request_key":"create-q","actor_type":"system","actor_id":"fixture","reason":"fixture question","source_event_id":null,"approved_paths":null,"producer":{"kind":"automatic","validated_inherited_paths":[]}}).to_string();
    repo.guarded_create_knowledge(&scope,&identity,&actor,"create-q",&create,None,Some(&producer),true)?;
    // An automatic lexical candidate cannot close the exact existing question.
    let automatic=json!({"version":"s03.published-update.v1","owner":"alice","workspace":"ws","project":"","block_id":"question-fixture","expected_revision":1,"payload":{"value":{"type":"string","value":"Alice"},"status":"active"},"request_key":"lexical","actor_type":"system","actor_id":"fixture","reason":"lexical suggestion","source_event_id":null,"approved_paths":null,"producer":{"kind":"automatic","validated_inherited_paths":[]}}).to_string();
    let automatic_actor=RevisionActor{actor_type:"system".into(),actor_id:"fixture".into(),reason:"lexical suggestion".into(),source_event_id:None};
    assert!(repo.guarded_update_knowledge(&scope,"question-fixture",1,&automatic_actor,"lexical",&automatic,None,Some(&producer),true).is_err());
    assert_eq!(repo.get_knowledge(&scope,"question-fixture",None)?.payload.status,KnowledgeStatus::Open);
    let paths=vec!["/status".to_owned(),"/value/type".to_owned(),"/value/value".to_owned()];
    let operation=json!({"version":"s03.published-update.v1","owner":"alice","workspace":"ws","project":"","block_id":"question-fixture","expected_revision":1,"payload":{"value":{"type":"string","value":"Alice"},"status":"active"},"request_key":"review-q","actor_type":"user","actor_id":"alice","reason":"answer open question","source_event_id":null,"approved_paths":paths,"producer":null}).to_string();
    let request=ConfirmedQuestionRequest{version:1,account_id:None,owner:"alice".into(),workspace_id:"ws".into(),project_id:None,block_id:"question-fixture".into(),subject_entity_id:"fixture-entity".into(),predicate:"preferred_address".into(),claim_slot:"address".into(),expected_revision:1,answer:"Alice".into(),request_key:"review-q".into(),approved_paths:paths,operation_json:operation};
    let mut wrong=request.clone();wrong.subject_entity_id="other-entity".into();assert!(repo.apply_confirmed_question(&wrong).is_err());
    let applied=repo.apply_confirmed_question(&request)?;assert_eq!(applied["question"]["payload"]["status"],"active");
    assert_eq!(repo.apply_confirmed_question(&request)?,applied);
    let mut stale=request.clone();stale.request_key="different-key".into();
    let mut stale_operation:Value=serde_json::from_str(&stale.operation_json)?;stale_operation["request_key"]=json!("different-key");stale.operation_json=stale_operation.to_string();
    assert!(matches!(repo.apply_confirmed_question(&stale),Err(RepositoryError::RevisionConflict{..})));
    println!("PASS guarded lexical refusal, exact subject review, confirmed answer and replay");Ok(())
}

#[test]
fn guarded_seed_bootstrap_preserves_confirmed_slot() -> Result<(),Box<dyn std::error::Error>> {
    let path=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB")?;
    if !path.starts_with("/tmp/openclank-s06-transaction-queued-") {return Err("disposable shadow required".into());}
    let database_id=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB_ID")?;
    let refs:PolicyPublicationRefs=serde_json::from_str(&std::env::var("OPENCLANK_QUEUED_FIXTURE_POLICY")?)?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database_id)?;
    let mut conn=store.conn.lock().map_err(|_|"connection unavailable")?;
    let mut repo=V2Repository::new(&mut conn)?;
    let request=QuestionSeedBootstrapRequest{version:2,owner:"alice".into(),account_id:"account-fixture".into(),workspace_id:"ws".into(),request_key:"root-seed-1".into(),policy_projection:refs};
    let first=repo.bootstrap_question_seeds(&request)?;assert_eq!(first["created"],2);
    let handler=first["handler_entity_id"].as_str().ok_or("handler")?.to_owned();
    let block:String=repo.conn.query_row("SELECT block_id FROM fm_v2_knowledge_blocks WHERE owner_id='alice' AND subject_entity_id=? AND predicate='preferred_name' AND claim_slot='identity.preferred_name'",[&handler],|r|r.get(0))?;
    let paths=vec!["/status".to_owned(),"/value/type".to_owned(),"/value/value".to_owned()];
    let operation=json!({"version":"s03.published-update.v1","owner":"alice","workspace":"ws","project":"","block_id":block,"expected_revision":1,"payload":{"value":{"type":"string","value":"A"},"status":"active"},"request_key":"answer-seed","actor_type":"user","actor_id":"alice","reason":"answer open question","source_event_id":null,"approved_paths":paths,"producer":null}).to_string();
    let review=ConfirmedQuestionRequest{version:1,account_id:None,owner:"alice".into(),workspace_id:"ws".into(),project_id:None,block_id:block.clone(),subject_entity_id:handler,predicate:"preferred_name".into(),claim_slot:"identity.preferred_name".into(),expected_revision:1,answer:"A".into(),request_key:"answer-seed".into(),approved_paths:paths,operation_json:operation};
    repo.apply_confirmed_question(&review)?;
    let again=repo.bootstrap_question_seeds(&request)?;assert_eq!(again["state"],"already_applied");assert_eq!(again["created"],2);assert_eq!(again["handler_entity_id"],first["handler_entity_id"]);
    let status=repo.question_seed_status(&QuestionSeedStatusRequest{request:request.clone(),current_policy:request.policy_projection.clone()})?;
    assert_eq!(status["created"],2);assert_eq!(status["visibility"],"not_rechecked");
    let mut changed=request.clone();changed.policy_projection.account.epoch+=1;
    assert!(repo.question_seed_status(&QuestionSeedStatusRequest{request:changed,current_policy:request.policy_projection.clone()}).is_err());
    let mut later=request.clone();later.request_key="root-seed-2".into();
    assert_eq!(repo.question_seed_status(&QuestionSeedStatusRequest{request:later.clone(),current_policy:later.policy_projection.clone()})?,json!({"version":1,"state":"not_found"}));
    let second=repo.bootstrap_question_seeds(&later)?;assert_eq!(second["created"],0);
    let scope=ScopeRef{owner_id:"alice".into(),workspace_id:Some("ws".into()),project_id:None,session_id:None};
    let answer=repo.get_knowledge(&scope,&block,None)?;assert_eq!(answer.revision,2);assert_eq!(answer.payload.status,KnowledgeStatus::Active);
    assert_eq!(answer.payload.activation_rationale.pointer("/question_context/provenance/seed_key"),Some(&json!("handler.preferred_address")));
    {let tx=repo.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;erase_question_seed_receipts(&tx,"alice")?;tx.commit()?;}
    assert_eq!(repo.question_seed_status(&QuestionSeedStatusRequest{request:request.clone(),current_policy:request.policy_projection.clone()})?,json!({"version":1,"state":"not_found"}));
    assert_eq!(repo.get_knowledge(&scope,&block,None)?.revision,2);
    println!("PASS guarded principal, seed receipt replay/status/conflict, confirmed slot and reset receipt closure");Ok(())
}
