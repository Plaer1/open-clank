use crate::{repository::{AccountCandidateReviewRequest,DomainDefinitionReviewRequest,MetadataAssignmentRequest,MetadataAssignmentApply,MetadataReadRequest,PreparedKnowledgeRead,PreparedKnowledgeItem,PublishedLifecycleRequest,V2Repository},store::sqlite::SqliteStore};
use serde_json::json;

#[test]
fn guarded_metadata_assignment_is_atomic() -> Result<(),Box<dyn std::error::Error>> {
    let root=std::path::PathBuf::from(std::env::var("DOMAIN_FIXTURE_ROOT")?);
    assert!(root.parent()==Some(std::path::Path::new("/tmp")) && root.file_name().and_then(|s|s.to_str()).is_some_and(|s|s.starts_with("openclank-s10-review-")));
    let mut legacy=rusqlite::Connection::open(root.join("source.sqlite"))?;
    assert!(V2Repository::new(&mut legacy)?.metadata_status().is_err());drop(legacy);
    let id=std::fs::read_to_string(root.join("id"))?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(root.join("shadow.sqlite").to_str().ok_or("path")?,4,id.trim())?;
    let (other,_,_,_,_,_)=SqliteStore::open_guarded(root.join("shadow.sqlite").to_str().ok_or("path")?,4,id.trim())?;
    let review:AccountCandidateReviewRequest=serde_json::from_slice(&std::fs::read(root.join("request.json"))?)?;
    let mut conn=store.conn.lock().map_err(|_|"lock")?;
    let other_conn=other.conn.lock().map_err(|_|"other lock")?;
    crate::writer_fence::with_domain_metadata_authority(&mut conn,|first| -> rusqlite::Result<()> {
        assert_eq!(first.query_row("SELECT fm_domain_metadata_authorized()",[],|r|r.get::<_,i64>(0))?,1);
        assert_eq!(other_conn.query_row("SELECT fm_domain_metadata_authorized()",[],|r|r.get::<_,i64>(0))?,0);
        let denied=other_conn.execute("INSERT INTO fm_v2_revision_metadata(owner_id,block_id,revision,metadata_schema,metadata_json,metadata_hash,actor_type,actor_id,change_set_id,source_event_id,evidence_json,created_at) VALUES('alice','missing',1,'openclank.memory-revision-metadata.v1','{}','x','user','alice','missing',NULL,'[]','now')",[]).expect_err("other connection must not inherit authority");
        assert!(denied.to_string().contains("metadata requires guarded writer"));Ok(())
    })?;
    drop(other_conn);
    let predecessor:String=conn.query_row("SELECT change_set_id FROM fm_v2_outbox WHERE owner_id='alice' AND kind='candidate_review' AND json_extract(payload_json,'$.candidate_id')='s10-forward'",[],|r|r.get(0))?;
    let curated:String=conn.query_row("SELECT json_extract(payload_json,'$.curated_id') FROM fm_v2_outbox WHERE owner_id='alice' AND change_set_id=? AND kind='candidate_review'",[&predecessor],|r|r.get(0))?;
    let authority_snapshot=|db:&rusqlite::Connection,revision:i64| -> rusqlite::Result<Vec<String>> {
        let mut stmt=db.prepare("SELECT json_array(field_path,origin,approved_by,source_event_id,evidence_json) FROM fm_v2_revision_field_authority WHERE owner_id='alice' AND revision=? AND block_id=(SELECT json_extract(payload_json,'$.block_id') FROM fm_v2_outbox WHERE owner_id='alice' AND kind='candidate_review' AND json_extract(payload_json,'$.candidate_id')='s10-forward') ORDER BY field_path")?;
        let rows=stmt.query_map([revision],|r|r.get(0))?.collect();rows
    };
    let original_authority=authority_snapshot(&conn,1)?;
    let mut repository=V2Repository::new(&mut conn)?;
    assert_eq!(repository.metadata_status()?["phase"],"metadata_assignment_apply");
    let domain=repository.review_domain_definition(&DomainDefinitionReviewRequest{version:1,owner:"alice".into(),workspace_id:"ws".into(),account_id:"account-fixture".into(),request_key:"domain-one".into(),name:"Synthetic domain".into(),aliases:vec![],value:"Synthetic definition".into(),reason:"explicit review".into(),policy_projection:review.policy_projection.clone()})?;
    let mut request=MetadataAssignmentRequest{version:1,owner:"alice".into(),workspace_id:"ws".into(),account_id:"account-fixture".into(),curated_id:curated,predecessor_receipt_id:predecessor,expected_revision:1,request_key:"metadata-one".into(),reason:"explicit review".into(),metadata:json!({"name":"Reviewed name","domain_refs":[{"block_id":domain["block_id"],"revision":1}]}),manual:true,policy_projection:review.policy_projection,selected:None};
    let mut read_metadata=MetadataReadRequest{version:1,owner:"alice".into(),workspace_id:"ws".into(),account_id:"account-fixture".into(),curated_id:None,policy_projection:request.policy_projection.clone()};
    assert_eq!(repository.metadata_read(&read_metadata,true)?["items"].as_array().ok_or("domains")?.len(),1);
    read_metadata.curated_id=Some(request.curated_id.clone());assert_eq!(repository.metadata_read(&read_metadata,false)?["revision"],1);
    let apply=MetadataAssignmentApply{version:1,owner:request.owner.clone(),workspace_id:request.workspace_id.clone(),account_id:request.account_id.clone(),curated_id:request.curated_id.clone(),predecessor_receipt_id:request.predecessor_receipt_id.clone(),expected_revision:request.expected_revision,request_key:request.request_key.clone(),reason:request.reason.clone(),metadata:request.metadata.clone(),policy_projection:request.policy_projection.clone()};
    assert_eq!(repository.apply_manual_metadata(&apply)?["state"],"applied");
    assert_eq!(repository.assign_reviewed_metadata(&request,false)?["state"],"already_applied");
    let mut conflict=request.clone();conflict.metadata=json!({"name":"Changed"});assert!(repository.assign_reviewed_metadata(&conflict,false).is_err());
    drop(repository);
    let copied=authority_snapshot(&conn,2)?;assert!(original_authority.iter().all(|v|copied.contains(v)));
    let wrong_producer:i64=conn.query_row("SELECT count(*) FROM fm_v2_revision_field_authority WHERE owner_id='alice' AND revision=2 AND block_id=(SELECT json_extract(payload_json,'$.block_id') FROM fm_v2_outbox WHERE owner_id='alice' AND kind='candidate_review' AND json_extract(payload_json,'$.candidate_id')='s10-forward') AND (producer_type<>'user' OR producer_id<>'alice')",[],|r|r.get(0))?;assert_eq!(wrong_producer,0);
    request.expected_revision=2;request.predecessor_receipt_id=conn.query_row("SELECT change_set_id FROM fm_v2_outbox WHERE owner_id='alice' AND kind='mirror_update' AND source_revision=2",[],|r|r.get(0))?;
    request.request_key="inferred-change".into();request.manual=false;request.metadata=json!({"name":"Unreviewed name"});
    let refused=V2Repository::new(&mut conn)?.assign_reviewed_metadata(&request,false).expect_err("manual metadata must be protected");
    assert!(refused.to_string().contains("metadata review required"),"unexpected refusal: {refused}");
    let head:i64=conn.query_row("SELECT current_revision FROM fm_v2_knowledge_blocks WHERE owner_id='alice' AND block_id=(SELECT json_extract(payload_json,'$.block_id') FROM fm_v2_outbox WHERE owner_id='alice' AND kind='mirror_update' AND source_revision=2)",[],|r|r.get(0))?;
    assert_eq!(head,2);
    request.request_key="metadata-two".into();request.manual=true;request.metadata=json!({"aliases":["reviewed alias"]});
    assert_eq!(V2Repository::new(&mut conn)?.assign_reviewed_metadata(&request,false)?["revision"],3);
    let context={let tx=conn.transaction()?;let value=crate::repository::current_curated_mirror_context_in(&tx,"alice","ws",&request.curated_id)?;tx.commit()?;value};
    assert_eq!(context["expected_revision"],3);assert_eq!(context["reviewer_account_id"],"account-fixture");
    let block=context["binding"]["block_id"].as_str().ok_or("block")?.to_owned();drop(conn);
    let mut read=PreparedKnowledgeRead{version:1,owner:"alice".into(),workspace_id:"ws".into(),account_id:"account-fixture".into(),policy_projection:request.policy_projection.clone(),limit:10,cursor:None,expected:None,curated_id:None};
    let page=store.prepared_knowledge_read(&read)?;let selected=page["items"].as_array().ok_or("items")?.iter().find(|v|v["selected"]["curated_id"]==request.curated_id).ok_or("missing current item")?;
    let item:PreparedKnowledgeItem=serde_json::from_value(selected.clone())?;assert_eq!(item.selected.reference.revision,3);
    read.expected=Some(item);assert_eq!(store.prepared_knowledge_read(&read)?["state"],"ready");
    let operation=json!({"version":"s03.published-update.v1","owner":"alice","workspace":"ws","project":"","block_id":block,"expected_revision":3,"payload":{"status":"retracted"},"request_key":"fixture-withdraw","actor_type":"user","actor_id":"alice","reason":"retracted by user","source_event_id":null,"approved_paths":["/status"],"producer":null});
    let withdrawal=PublishedLifecycleRequest{version:1,owner:"alice".into(),workspace_id:"ws".into(),project_id:None,block_id:block,action:"retract".into(),expected_revision:3,target_revision:None,request_key:"fixture-withdraw".into(),reason:"retracted by user".into(),approved_paths:vec!["/status".into()],account_id:None,original_request_key:None,operation_json:serde_json::to_string(&operation)?};
    {let mut conn=store.conn.lock().map_err(|_|"lock")?;assert_eq!(V2Repository::new(&mut conn)?.apply_published_lifecycle(&withdrawal)?["state"],"applied");}
    assert!(store.prepared_knowledge_read(&read).is_err());
    let components=vec!["memories".to_string()];let preview=store.reset_owner("reset_preview","alice",&components,None)?;
    store.guarded_reset_owner("alice","ws","account-fixture","fixture-reset",&components,&preview["components"],false)?;
    let conn=store.conn.lock().map_err(|_|"lock")?;let remaining:i64=conn.query_row("SELECT count(*) FROM fm_v2_outbox WHERE owner_id='alice' AND kind IN ('mirror_update','domain_definition_review')",[],|r|r.get(0))?;assert_eq!(remaining,0);Ok(())
}
