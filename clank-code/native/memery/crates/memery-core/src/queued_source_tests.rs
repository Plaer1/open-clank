//! One disposable real-opener transaction case; caller prepares the shadow.
use super::*;
use crate::ontology::ScopeRef;
use crate::store::sqlite::SqliteStore;

fn source(message:&str)->QueuedArchiveSourceRef {
    QueuedArchiveSourceRef{version:1,kind:"queued_user_text".into(),account_id:"account-fixture".into(),
        chat_id:"chat-fixture".into(),workspace_id:"files-ws".into(),request_id:format!("request-{message}"),
        native_session_id:"native-session".into(),message_id:message.into(),part_id:"part-fixture".into(),
        archive_revision:1,archive_content_hash:"a".repeat(64),text_sha256:"b".repeat(64),ingress_sha256:"c".repeat(64),
        root_operation_id:"root-fixture".into(),execution_revision:1,inner_turn_id:"turn-fixture".into()}
}

#[test]
fn queued_origin_current_fence_and_physical_erase() -> Result<(),Box<dyn std::error::Error>> {
    let path=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB")?;
    if !path.starts_with("/tmp/openclank-s06-transaction-queued-") {return Err("explicit disposable shadow required".into());}
    let database_id=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB_ID")?;
    let policy:PolicyPublicationRefs=serde_json::from_str(&std::env::var("OPENCLANK_QUEUED_FIXTURE_POLICY")?)?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database_id)?;
    let mut conn=store.conn.lock().map_err(|_|"fixture connection unavailable")?;
    let mut repo=V2Repository::new(&mut conn)?;
    let first=source("message-1");let second=source("message-2");
    let request=QueuedOriginRequest{owner:"alice".into(),workspace_id:"ws".into(),source:first.clone(),policy_projection:policy.clone()};
    let created=repo.project_queued_origin(&request)?;assert_eq!(created["state"],"active");
    let scope=ScopeRef{owner_id:"alice".into(),workspace_id:Some("ws".into()),project_id:None,session_id:None};
    let actor=RevisionActor{actor_type:"user".into(),actor_id:"alice".into(),reason:"fixture erasure".into(),source_event_id:None};
    let id=created["source_id"].as_str().ok_or("source")?;
    assert!(repo.get_source(&scope,id,1).is_err());assert!(repo.source_history(&scope,id).is_err());
    let generic=SourcePayload{source_uri:"host-queued://forged".into(),content_hash:"a".repeat(64),source_type:"fixture".into()};
    assert!(repo.create_source(&scope,"ordinary-looking",1,&generic,&actor).is_err());
    let generic=SourcePayload{source_uri:"fixture://ordinary".into(),content_hash:"a".repeat(64),source_type:"host_queued_part.v1".into()};
    assert!(repo.create_source(&scope,"ordinary-looking",1,&generic,&actor).is_err());
    let generic=SourcePayload{source_uri:"fixture://ordinary".into(),content_hash:"a".repeat(64),source_type:"fixture".into()};
    assert!(repo.create_source(&scope,id,1,&generic,&actor).is_err());
    let generic_candidate=CandidatePayload{proposal:serde_json::json!({"value":"ordinary"}),state:crate::ontology::CandidateState::Proposed,
        confidence:0.5,importance:0.5,reason:"fixture".into(),source_id:Some(id.into()),source_revision:Some(1),accepted_block_id:None,evidence_ids:vec![]};
    assert!(repo.create_candidate(&scope,"ordinary-candidate","ordinary-candidate-key",&generic_candidate,&actor).is_err());
    assert_eq!(repo.project_queued_origin(&request)?["state"],"already_active");
    let mut changed=request.clone();changed.source.text_sha256="d".repeat(64);assert!(repo.project_queued_origin(&changed).is_err());
    let mut next=request.clone();next.source=second.clone();repo.project_queued_origin(&next)?;
    assert_eq!(repo.read_queued_origin("alice","ws",&first)?["state"],"active");
    let page=repo.queued_origin_page("alice","ws","account-fixture",Some("chat-fixture"),None,None,1)?;
    assert_eq!(page["items"].as_array().unwrap().len(),1);let cursor=page["next_after"].as_str().ok_or("cursor")?;
    assert_eq!(repo.queued_origin_page("alice","ws","account-fixture",Some("chat-fixture"),None,Some(cursor),1)?["items"].as_array().unwrap().len(),1);
    let closed=repo.fence_queued_origin("alice","ws",&first)?;assert_eq!(closed["covered"],serde_json::json!(["queued_origin_only"]));
    assert!(repo.read_queued_origin("alice","ws",&first).is_err());
    repo.erase_source(&scope,created["source_id"].as_str().ok_or("source")?,&actor)?;
    assert!(repo.read_queued_origin("alice","ws",&first).is_err());
    assert_eq!(repo.read_queued_origin("alice","ws",&second)?["state"],"active");
    let remaining=repo.queued_origin_page("alice","ws","account-fixture",Some("chat-fixture"),None,None,128)?;
    assert_eq!(remaining["items"].as_array().unwrap().len(),1);
    println!("PASS queued origin exact replay, bounded inventory, logical fence and scoped physical erasure");
    Ok(())
}

#[test]
fn queued_guarded_rename_rebind_roundtrip() -> Result<(),Box<dyn std::error::Error>> {
    use crate::store::sqlite::GuardedOwnerRenameRequest;
    let path=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB")?;
    if !path.starts_with("/tmp/openclank-s06-transaction-queued-") {return Err("explicit disposable shadow required".into());}
    let database_id=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB_ID")?;
    let policy:PolicyPublicationRefs=serde_json::from_str(&std::env::var("OPENCLANK_QUEUED_FIXTURE_POLICY")?)?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database_id)?;
    let part=source("message-rename");
    let (origin,origin_hash)={
        let mut conn=store.conn.lock().map_err(|_|"fixture connection unavailable")?;
        let mut repo=V2Repository::new(&mut conn)?;
        let result=repo.project_queued_origin(&QueuedOriginRequest{owner:"alice".into(),workspace_id:"ws".into(),source:part.clone(),policy_projection:policy.clone()})?;
        let origin=result["origin_id"].as_str().ok_or("origin unavailable")?.to_owned();
        let hash:String=conn.query_row("SELECT content_hash FROM fm_v2_change_sets WHERE owner_id='alice' AND change_set_id=?",[&origin],|r|r.get(0))?;
        (origin,hash)
    };
    let mut prior:Option<(String,String)>=None;
    for (old,new,operation) in [("alice","bob","op-ab"),("bob","alice","op-ba")] {
        let binding=QueuedTransitionBinding{database_id:database_id.clone(),account_id:part.account_id.clone(),operation_id:operation.into(),step:"memory_fm".into(),generation:1,action:"forward".into(),owner:old.into(),new_owner:new.into()};
        {
            let mut conn=store.conn.lock().map_err(|_|"fixture connection unavailable")?;
            let mut repo=V2Repository::new(&mut conn)?;
            let fence=repo.fence_queued_origin_for_rename("ws",&part,&binding)?;
            assert_eq!(fence["covered"],serde_json::json!(["queued_origin_only"]));
        }
        let src=store.owner_counts(old)?;let dst=store.owner_counts(new)?;
        let transition=GuardedOwnerRenameRequest{version:1,database_id:database_id.clone(),account_id:part.account_id.clone(),operation_id:operation.into(),step:"memory_fm".into(),generation:1,action:"forward".into(),owner:old.into(),new_owner:new.into(),expected_source_fingerprint:src["fingerprint"].as_str().ok_or("source fingerprint")?.into(),expected_target_fingerprint:dst["fingerprint"].as_str().ok_or("target fingerprint")?.into(),prior_receipt_id:None,prior_receipt_hash:None,rag_relocation:None};
        let renamed=store.guarded_rename_owner(&transition)?;
        let request=QueuedRebindRequest{transition,transition_receipt_id:renamed["receipt_id"].as_str().ok_or("rename receipt")?.into(),transition_receipt_hash:renamed["receipt_hash"].as_str().ok_or("rename hash")?.into(),workspace_id:"ws".into(),captured_owner:"alice".into(),source:part.clone(),origin_hash:origin_hash.clone(),prior_receipt_id:prior.as_ref().map(|p|p.0.clone()),prior_receipt_hash:prior.as_ref().map(|p|p.1.clone()),target_fingerprint:"d".repeat(64),policy_projection:policy.clone()};
        let mut conn=store.conn.lock().map_err(|_|"fixture connection unavailable")?;
        let mut repo=V2Repository::new(&mut conn)?;
        let result=repo.rebind_queued_origin(&request)?;
        assert_eq!(result["source_state"],"active");
        assert_eq!(repo.rebind_queued_origin(&request)?["state"],"already_applied");
        assert_eq!(repo.read_queued_origin(new,"ws",&part)?["origin_id"],origin);
        prior=Some((result["receipt_id"].as_str().ok_or("rebind receipt")?.into(),result["receipt_hash"].as_str().ok_or("rebind hash")?.into()));
    }
    println!("PASS queued exact guarded rename and immutable A-B-A rebind replay");Ok(())
}

/// Disposable seed-only entry point for the host/native joined fixture.
#[test]
fn queued_origin_seed_for_host_adapter() -> Result<(),Box<dyn std::error::Error>> {
    let path=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB")?;
    if !path.starts_with("/tmp/openclank-s06-transaction-queued-") {return Err("explicit disposable shadow required".into());}
    let database_id=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB_ID")?;
    let policy:PolicyPublicationRefs=serde_json::from_str(&std::env::var("OPENCLANK_QUEUED_FIXTURE_POLICY")?)?;
    let part:QueuedArchiveSourceRef=serde_json::from_str(&std::env::var("OPENCLANK_QUEUED_FIXTURE_SOURCE")?)?;
    if part.account_id!="account-fixture" {return Err("synthetic account required".into());}
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database_id)?;
    let mut conn=store.conn.lock().map_err(|_|"fixture connection unavailable")?;
    let mut repo=V2Repository::new(&mut conn)?;
    let result=repo.project_queued_origin(&QueuedOriginRequest{owner:"alice".into(),workspace_id:"ws".into(),source:part,policy_projection:policy})?;
    if result["state"]!="active" {return Err("fixture seed unavailable".into());}
    println!("QUEUED_SEED {}",serde_json::to_string(&result)?);Ok(())
}

#[test]
fn queued_guarded_ingest_reset_retirement() -> Result<(),Box<dyn std::error::Error>> {
    let path=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB")?;
    if !path.starts_with("/tmp/openclank-s06-transaction-queued-") {return Err("explicit disposable shadow required".into());}
    let database_id=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB_ID")?;
    let policy:PolicyPublicationRefs=serde_json::from_str(&std::env::var("OPENCLANK_QUEUED_FIXTURE_POLICY")?)?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database_id)?;
    let part=source("message-reset");let components=vec!["ingest".to_string()];
    let request=QueuedOriginRequest{owner:"alice".into(),workspace_id:"ws".into(),source:part.clone(),policy_projection:policy};
    {
        let mut conn=store.conn.lock().map_err(|_|"fixture connection unavailable")?;
        V2Repository::new(&mut conn)?.project_queued_origin(&request)?;
    }
    let preview=store.reset_owner("reset_preview","alice",&components,None)?;
    let expected=preview["components"].clone();
    assert!(expected["ingest"]["count"].as_i64().unwrap_or(0)>0);
    assert!(store.guarded_reset_owner("alice","ws","account-fixture","reset-op",&components,&expected,false).is_err());
    {
        let mut conn=store.conn.lock().map_err(|_|"fixture connection unavailable")?;
        V2Repository::new(&mut conn)?.fence_queued_origin("alice","ws",&part)?;
    }
    assert_eq!(store.reset_owner("reset_preview","alice",&components,None)?["components"]["ingest"],expected["ingest"]);
    assert!(store.guarded_reset_owner("alice","ws","wrong-account","reset-op",&components,&expected,true).is_err());
    let completed=store.guarded_reset_owner("alice","ws","account-fixture","reset-op",&components,&expected,true)?;
    assert_eq!(completed["complete"],true);
    assert_eq!(store.guarded_reset_owner("alice","ws","account-fixture","reset-op",&components,&expected,true)?,completed);
    {
        let mut conn=store.conn.lock().map_err(|_|"fixture connection unavailable")?;
        let id=part.stable_source_id()?;
        let tombstone:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_tombstones WHERE owner_id='alice' AND source_id=?)",[&id],|r|r.get(0))?;
        assert!(tombstone);
        assert!(V2Repository::new(&mut conn)?.project_queued_origin(&request).is_err());
    }
    println!("PASS queued ingest preview CAS, guarded retirement, replay and no reintroduction");Ok(())
}

#[test]
fn queued_origin_readonly_preflight() -> Result<(),Box<dyn std::error::Error>> {
    let path=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB")?;
    if !path.starts_with("/tmp/openclank-s06-transaction-queued-") {return Err("explicit disposable shadow required".into());}
    let database_id=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB_ID")?;
    let policy:PolicyPublicationRefs=serde_json::from_str(&std::env::var("OPENCLANK_QUEUED_FIXTURE_POLICY")?)?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database_id)?;
    let mut conn=store.conn.lock().map_err(|_|"fixture connection unavailable")?;
    let mut repo=V2Repository::new(&mut conn)?;
    let first=source("preflight-1");let second=source("preflight-2");
    for item in [&first,&second] {repo.project_queued_origin(&QueuedOriginRequest{owner:"alice".into(),workspace_id:"ws".into(),source:item.clone(),policy_projection:policy.clone()})?;}
    let before=repo.conn.total_changes();
    let receipt=repo.preflight_queued_origins("alice","ws","account-fixture",&[first.clone(),second.clone()])?;
    assert_eq!(receipt["items"][0]["state"],"active");assert_eq!(receipt["items"][1]["state"],"active");
    assert_eq!(repo.conn.total_changes(),before);
    assert!(repo.preflight_queued_origins("alice","ws","account-fixture",&[source("missing")]).is_err());
    assert!(repo.preflight_queued_origins("alice","ws","wrong-account",&[first.clone()]).is_err());
    assert_eq!(repo.conn.total_changes(),before);
    let source_id=first.stable_source_id()?;
    repo.conn.execute("INSERT INTO fm_v2_privacy_erasure_context(owner_id,erasure_id,source_id,authorized_by,reason,created_at) VALUES ('alice','fixture',?,'fixture','fixture','fixture')",[&source_id])?;
    let dependent_before=repo.conn.total_changes();
    assert!(repo.preflight_queued_origins("alice","ws","account-fixture",&[first.clone()]).is_err());
    assert_eq!(repo.conn.total_changes(),dependent_before);
    repo.conn.execute("DELETE FROM fm_v2_privacy_erasure_context WHERE owner_id='alice'",[])?;
    repo.fence_queued_origin("alice","ws",&first)?;
    let fenced_before=repo.conn.total_changes();
    let fenced=repo.preflight_queued_origins("alice","ws","account-fixture",&[first.clone(),second.clone()])?;
    assert_eq!(fenced["items"][0]["state"],"fenced");assert_eq!(fenced["items"][1]["state"],"active");
    assert_eq!(repo.conn.total_changes(),fenced_before);
    let outboxes:i64=repo.conn.query_row("SELECT count(*) FROM fm_v2_outbox WHERE owner_id='alice' AND kind='s04_queued_origin'",[],|r|r.get(0))?;
    assert_eq!(outboxes,2);
    println!("PASS queued preflight exact/fenced/dependent/missing and no writes");Ok(())
}

#[test]
fn queued_reset_fence_requires_original_snapshot_and_no_s08() -> Result<(),Box<dyn std::error::Error>> {
    let path=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB")?;
    if !path.starts_with("/tmp/openclank-s06-transaction-queued-") {return Err("explicit disposable shadow required".into());}
    let database_id=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB_ID")?;
    let policy:PolicyPublicationRefs=serde_json::from_str(&std::env::var("OPENCLANK_QUEUED_FIXTURE_POLICY")?)?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database_id)?;
    let mut conn=store.conn.lock().map_err(|_|"fixture connection unavailable")?;
    let mut repo=V2Repository::new(&mut conn)?;
    let part=source("reset-fence");
    repo.project_queued_origin(&QueuedOriginRequest{owner:"alice".into(),workspace_id:"ws".into(),source:part.clone(),policy_projection:policy})?;
    let snapshot=SqliteStore::reset_snapshot_inner(repo.conn,"alice",true)?;
    let expected=serde_json::json!({"memories":snapshot["memories"],"graph":snapshot["graph"],"ingest":snapshot["ingest"]});
    let mut binding=ResetFenceBinding{account_id:"account-fixture".into(),operation_id:"reset-fixture".into(),components:vec!["memories".into(),"graph".into(),"ingest".into()],expected_counts:expected.clone(),mode:None};
    binding.expected_counts["ingest"]["fingerprint"]=serde_json::json!("changed");
    let before=repo.conn.total_changes();
    {let mut budget=ResetFenceBudget::install(repo.conn);
    assert!(repo.fence_queued_origin_reset("alice","ws",&part,&binding,&mut budget).is_err());}
    assert_eq!(before,repo.conn.total_changes());
    binding.expected_counts=expected;
    {let mut budget=ResetFenceBudget::install(repo.conn);let before=repo.conn.total_changes();
    assert!(repo.read_queued_reset_fence("alice","ws",&part,&binding,&mut budget).is_err());
    assert_eq!(before,repo.conn.total_changes());}
    // Seed a terminal historical job in this disposable fixture, then restore
    // the exact schema before exercising the real profile and fence checks.
    let trigger:String=repo.conn.query_row("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='fm_v2_s05_job_insert_guard'",[],|r|r.get(0))?;
    repo.conn.execute_batch("DROP TRIGGER fm_v2_s05_job_insert_guard")?;
    repo.conn.execute("INSERT INTO fm_v2_jobs(owner_id,job_id,kind,idempotency_key,state,input_hash,created_at,updated_at) VALUES('alice','s08-fixture','access_consolidation','s08-key','succeeded','hash','now','now')",[])?;
    repo.conn.execute_batch(&trigger)?;
    let before=repo.conn.total_changes();
    {let mut budget=ResetFenceBudget::install(repo.conn);
    assert!(repo.fence_queued_origin_reset("alice","ws",&part,&binding,&mut budget).is_err());}
    assert_eq!(before,repo.conn.total_changes());
    repo.conn.execute("DELETE FROM fm_v2_jobs WHERE owner_id='alice' AND job_id='s08-fixture'",[])?;
    let previous=unsafe{rusqlite::ffi::sqlite3_limit(repo.conn.handle(),rusqlite::ffi::SQLITE_LIMIT_LENGTH,-1)};
    {let mut budget=ResetFenceBudget::install(repo.conn);budget.deadline=std::time::Instant::now();
    let before=repo.conn.total_changes();
    assert!(repo.fence_queued_origin_reset("alice","ws",&part,&binding,&mut budget).is_err());
    assert_eq!(before,repo.conn.total_changes());}
    assert_eq!(previous,unsafe{rusqlite::ffi::sqlite3_limit(repo.conn.handle(),rusqlite::ffi::SQLITE_LIMIT_LENGTH,-1)});
    assert_eq!(repo.conn.query_row("SELECT 1",[],|r|r.get::<_,i64>(0))?,1);
    let mut budget=ResetFenceBudget::install(repo.conn);
    let result=repo.fence_queued_origin_reset("alice","ws",&part,&binding,&mut budget)?;
    drop(budget);
    assert_eq!(result["state"],"fenced");
    {let mut budget=ResetFenceBudget::install(repo.conn);let before=repo.conn.total_changes();
    let proof=repo.read_queued_reset_fence("alice","ws",&part,&binding,&mut budget)?;
    assert_eq!(proof["state"],"already_fenced");assert_eq!(proof["source"],serde_json::to_value(&part)?);
    assert_eq!(before,repo.conn.total_changes());}
    let after=SqliteStore::reset_snapshot_inner(repo.conn,"alice",true)?;
    for key in &binding.components {assert_eq!(binding.expected_counts[key],after[key]);}
    println!("PASS reset fence original snapshot/no-S08 refusal and stable successful fence");
    Ok(())
}

#[test]
fn reset_host_fence_read_proves_exact_closed_source() -> Result<(),Box<dyn std::error::Error>> {
    let path=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB")?;
    if !path.starts_with("/tmp/openclank-s06-transaction-queued-") {return Err("explicit disposable shadow required".into());}
    let database_id=std::env::var("OPENCLANK_QUEUED_FIXTURE_DB_ID")?;
    let (store,_,_,_,_,_)=SqliteStore::open_guarded(&path,4,&database_id)?;
    let mut conn=store.conn.lock().map_err(|_|"fixture connection unavailable")?;
    let reference=HostArchiveSourceRef{version:1,kind:"host_user_body".into(),account_id:"account-fixture".into(),chat_id:"chat".into(),actor_id:"main".into(),message_id:"message".into(),part_id:"body".into(),revision:1,content_hash:"a".repeat(64),workspace_id:Some("files".into()),project_id:None};
    let policy="b".repeat(64);let id=host_archive_source_id(&reference)?;
    let raw=serde_json::to_string(&reference)?;
    let key=sha256_hex(&serde_json::to_vec(&serde_json::json!(["s05_host_user_capture",serde_json::to_value(&reference)?]))?);
    let trigger:String=conn.query_row("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='fm_v2_s05_job_insert_guard'",[],|r|r.get(0))?;
    conn.execute_batch("DROP TRIGGER fm_v2_s05_job_insert_guard")?;
    conn.execute("INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,input_hash,created_at,updated_at) VALUES('alice','job','s05_host_user_capture','ws','','key','succeeded','hash','now','now')",[])?;
    conn.execute_batch(&trigger)?;
    conn.execute("INSERT INTO fm_v2_s05_job_refs(owner_id,job_id,source_ref_json,source_key,source_account_id,source_workspace_id,target_workspace_id,owner_epoch,durable_epoch,binding_fingerprint,source_hash,admission_id,policy_fingerprint,connection_id,model_route_id,authority_fingerprint) VALUES('alice','job',?1,?2,'account-fixture','files','ws',0,'0','binding',?3,'admission',?4,'connection','route','authority')",params![raw,key,reference.content_hash,policy])?;
    conn.execute("INSERT INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,source_uri,source_revision,content_hash,source_type,forget_state,created_at) VALUES('alice',?1,'ws','',?2,1,?3,'host_archive_part.v1','active','now')",params![id,format!("host-archive://{id}"),reference.content_hash])?;
    let snapshot=SqliteStore::reset_snapshot_inner(&conn,"alice",true)?;
    let expected=serde_json::json!({"memories":snapshot["memories"],"graph":snapshot["graph"],"ingest":snapshot["ingest"]});
    let binding=ResetFenceBinding{account_id:"account-fixture".into(),operation_id:"reset-fixture".into(),components:vec!["memories".into(),"graph".into(),"ingest".into()],expected_counts:expected,mode:None};
    let mut repo=V2Repository::new(&mut conn)?;
    let before=repo.conn.total_changes();
    {let mut budget=ResetFenceBudget::install(repo.conn);assert!(repo.read_reset_host_fence("alice","ws",Some(("job",&policy)),None,&reference,&binding,&mut budget).is_err());}
    assert_eq!(before,repo.conn.total_changes());
    {let mut budget=ResetFenceBudget::install(repo.conn);repo.reset_fence_host_source("alice","ws","job",&reference,&policy,"reset-fixture",&binding,&mut budget)?;}
    let before=repo.conn.total_changes();
    {let mut budget=ResetFenceBudget::install(repo.conn);let proof=repo.read_reset_host_fence("alice","ws",Some(("job",&policy)),None,&reference,&binding,&mut budget)?;assert_eq!(proof.state,"already_fenced");assert_eq!(proof.source_id,id);}
    assert_eq!(before,repo.conn.total_changes());
    let mut changed=reference.clone();changed.content_hash="c".repeat(64);
    {let mut budget=ResetFenceBudget::install(repo.conn);assert!(repo.read_reset_host_fence("alice","ws",Some(("job",&policy)),None,&changed,&binding,&mut budget).is_err());}
    assert_eq!(before,repo.conn.total_changes());
    println!("PASS exact read-only reset fence reconciliation and changed-source refusal");Ok(())
}
