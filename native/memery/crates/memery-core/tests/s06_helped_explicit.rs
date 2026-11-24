use std::sync::Arc;
use memery_core::{config::FmConfig,embed::DisabledEmbeddingClient,ontology::ScopeRef,provider::native::NativeProvider,store::sqlite::{SqliteStore,GuardedOwnerRenameRequest}};
use memery_core::repository::{AdmittedObservation,FoundInvocation,FoundMemoryRef,FoundObservation,HelpedObservation,HostArchiveSourceRef,HostObservationRenewRequest,ObservationOriginAuthority,RevisionActor};
use rusqlite::Connection;
use serde_json::json;
use sha2::{Digest,Sha256};

fn key(r:&HelpedObservation)->String {let m=&r.memory;format!("{:x}",Sha256::digest(serde_json::to_vec(&json!(["openclank.s06.helped.v1",r.account_id,r.admitted_kind,r.admitted_event_id,m.version,m.provider,m.owner,m.workspace_id,m.project_id,m.block_id,m.revision,m.value_sha256,r.polarity])).unwrap()))}

#[test]
fn explicit_helped_receipt_source_and_erase() -> Result<(),Box<dyn std::error::Error>> {
 let path=std::env::var("S06_HELPED_FIXTURE")?; assert!(path.starts_with("/tmp/openclank-s06-helped-"));
 let db=Connection::open_with_flags(&path,rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
 let id:String=db.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
 let value:String=db.query_row("SELECT value_json FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id='fixture-block' AND revision=1",[],|r|r.get(0))?;
 let value_hash=format!("{:x}",Sha256::digest(value.as_bytes()));
 let (store,feedback,admitted,erase,_,_)=SqliteStore::open_guarded(&path,4,&id)?;assert!(feedback&&admitted&&erase);
 let store=Arc::new(store);let provider=NativeProvider::new(store.clone(),Arc::new(DisabledEmbeddingClient::new(4)),FmConfig::default());
 let source=HostArchiveSourceRef{version:1,kind:"host_user_body".into(),account_id:"fixture-account".into(),chat_id:"helped-chat".into(),actor_id:"main".into(),message_id:"helped-root".into(),part_id:"body".into(),revision:1,content_hash:"a".repeat(64),workspace_id:Some("files-ws".into()),project_id:None};
 let memory=FoundMemoryRef{version:1,provider:"fm-v2".into(),owner:"alice".into(),workspace_id:"ws".into(),project_id:None,block_id:"fixture-block".into(),revision:1,value_sha256:value_hash};
 let found=FoundObservation{version:1,owner:"alice".into(),workspace_id:"ws".into(),source:source.clone(),invocation:FoundInvocation{session_id:"native".into(),root_operation_id:"helped-root".into(),actor_id:"main".into(),execution_revision:1,inner_turn_id:"turn".into(),recall_attempt_id:"helped-recall".into()},memory:memory.clone(),features:None};
 let found_id=provider.append_found_observation(&found)?;
 let (origin_id,origin_hash):(String,String)=db.query_row("SELECT change_set_id,content_hash FROM fm_v2_change_sets WHERE owner_id='alice' AND reason='s06.observation-origin.v1' AND json_extract(operation_metadata_json,'$.source.message_id')='helped-root'",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
 let authority=ObservationOriginAuthority{origin_id,origin_hash,captured_owner:"alice".into(),current_fingerprint:"b".repeat(64),rebind_id:None,rebind_hash:None};
 let admission=AdmittedObservation{version:1,owner:"alice".into(),workspace_id:"ws".into(),found_event_id:found_id,authority:authority.clone(),tool_call_id:"tool".into(),attestation_id:"abcdefghijklmnop".into(),result_sha256:"c".repeat(64),dispatch_id:"dispatch".into(),body_sha256:"d".repeat(64)};
 let admitted_id=provider.append_admitted_observation(&admission)?;
 let mut helped=HelpedObservation{version:1,producer:"host_human_review.v1".into(),owner:"alice".into(),workspace_id:"ws".into(),account_id:"fixture-account".into(),admitted_event_id:admitted_id,admitted_kind:"tool".into(),authority:authority.clone(),memory,polarity:"positive".into(),request_key:String::new()};helped.request_key=key(&helped);
 let first=provider.append_helped_unexposed(&helped)?;assert_eq!(provider.append_helped_unexposed(&helped)?,first);assert_eq!(provider.helped_status_unexposed(&helped,&authority)?,first);
 let page=provider.helped_selector_unexposed("alice","ws","fixture-account",&source,&authority,None,16)?;assert_eq!(page["items"][0]["recorded"]["positive"],true);assert_eq!(page["items"][0]["recorded"]["negative"],false);
 let mut negative=helped.clone();negative.polarity="negative".into();negative.request_key=key(&negative);provider.append_helped_unexposed(&negative)?;
 let mut conflict=helped.clone();conflict.authority.current_fingerprint="c".repeat(64);assert!(provider.append_helped_unexposed(&conflict).is_err());
 let mut foreign=helped.clone();foreign.account_id="foreign".into();foreign.request_key=key(&foreign);assert!(provider.append_helped_unexposed(&foreign).is_err());
 let exported=provider.export_observation_page("alice","ws",&source,&authority,"helped",None,16)?;assert_eq!(exported["events"].as_array().unwrap().len(),2);
 let mut other=found.clone();other.source.message_id="helped-other-root".into();other.invocation.root_operation_id="helped-other-root".into();other.invocation.recall_attempt_id="helped-other-recall".into();let other_id=provider.append_found_observation(&other)?;
 let (other_origin,other_hash):(String,String)=db.query_row("SELECT change_set_id,content_hash FROM fm_v2_change_sets WHERE owner_id='alice' AND reason='s06.observation-origin.v1' AND json_extract(operation_metadata_json,'$.source.message_id')='helped-other-root'",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
 let other_authority=ObservationOriginAuthority{origin_id:other_origin,origin_hash:other_hash,captured_owner:"alice".into(),current_fingerprint:"b".repeat(64),rebind_id:None,rebind_hash:None};
 let fence=provider.fence_observation_origin("alice","ws",&authority,&source,None)?;assert!(provider.helped_status_unexposed(&helped,&authority).is_err());
 let scope=ScopeRef{owner_id:"alice".into(),workspace_id:Some("ws".into()),project_id:None,session_id:None};let actor=RevisionActor{actor_type:"user".into(),actor_id:"alice".into(),reason:"fixture erase".into(),source_event_id:None};provider.erase_source(&scope,&fence.source_id,&actor)?;
 let count:i64=db.query_row("SELECT count(*) FROM fm_v2_change_sets WHERE owner_id='alice' AND source_event_id=?",[fence.source_id],|r|r.get(0))?;assert_eq!(count,0);
 assert!(provider.read_found_observation("alice","ws",&other_id,&other_authority).is_ok());
 let other_fence=provider.fence_observation_origin("alice","ws",&other_authority,&other.source,None)?;provider.erase_source(&scope,&other_fence.source_id,&actor)?;
 // found features: stored with the found row, joined to admitted/helped, owner-scoped, gone after erase.
 let mut feat=found.clone();feat.source.message_id="helped-feat-root".into();feat.invocation.root_operation_id="helped-feat-root".into();feat.invocation.recall_attempt_id="helped-feat-recall".into();
 feat.features=Some(memery_core::repository::FoundFeaturesV1{v:1,fts_ordinal:2,route_kind:"curated_fts".into(),directness:Some(0.5),entity_match:None,freshness:Some(1.0)});
 let mut bad=feat.clone();bad.features.as_mut().unwrap().fts_ordinal=64;assert!(provider.append_found_observation(&bad).is_err());
 assert_eq!(provider.found_features_capability("alice")?,json!({"version":1,"feature":"found_features_v1","state":"ready"}));
 let feat_id=provider.append_found_observation(&feat)?;assert_eq!(provider.append_found_observation(&feat)?,feat_id);
 let mut changed=feat.clone();changed.features.as_mut().unwrap().fts_ordinal=3;assert!(provider.append_found_observation(&changed).is_err());
 let mut plain=feat.clone();plain.features=None;assert!(provider.append_found_observation(&plain).is_err());
 let (feat_origin,feat_hash):(String,String)=db.query_row("SELECT change_set_id,content_hash FROM fm_v2_change_sets WHERE owner_id='alice' AND reason='s06.observation-origin.v1' AND json_extract(operation_metadata_json,'$.source.message_id')='helped-feat-root'",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
 let feat_auth=ObservationOriginAuthority{origin_id:feat_origin,origin_hash:feat_hash,captured_owner:"alice".into(),current_fingerprint:"b".repeat(64),rebind_id:None,rebind_hash:None};
 let mut feat_admission=admission.clone();feat_admission.found_event_id=feat_id.clone();feat_admission.authority=feat_auth.clone();feat_admission.dispatch_id="feat-dispatch".into();
 let feat_admitted=provider.append_admitted_observation(&feat_admission)?;
 let mut feat_helped=helped.clone();feat_helped.admitted_event_id=feat_admitted.clone();feat_helped.authority=feat_auth.clone();feat_helped.polarity="negative".into();feat_helped.request_key=key(&feat_helped);provider.append_helped_unexposed(&feat_helped)?;
 let feat_request=|owner:&str,auth:&ObservationOriginAuthority|memery_core::repository::HelpedSelectorRequest{version:1,owner:owner.into(),workspace_id:"ws".into(),account_id:"fixture-account".into(),source:feat.source.clone(),authority:auth.clone(),after:None,limit:200};
 let features_page=provider.found_features_page(&feat_request("alice",&feat_auth))?;
 assert_eq!(features_page["items"],json!([{"admitted_event_id":feat_admitted,"found_event_id":feat_id,"recall_attempt_id":"helped-feat-recall","features":{"v":1,"fts_ordinal":2,"route_kind":"curated_fts","directness":0.5,"entity_match":null,"freshness":1.0},"recorded":{"positive":false,"negative":true}}]));
 assert_eq!(provider.read_found_observation("alice","ws",&feat_id,&feat_auth)?.features,feat.features);
 assert!(provider.found_features_page(&feat_request("mallory",&feat_auth)).is_err());
 let none_yet=provider.found_features_page(&{let mut r=feat_request("alice",&other_authority);r.source=other.source.clone();r});assert!(none_yet.map(|p|p["items"].as_array().map(|a|a.is_empty()).unwrap_or(false)).unwrap_or(true));
 let feat_fence=provider.fence_observation_origin("alice","ws",&feat_auth,&feat.source,None)?;provider.erase_source(&scope,&feat_fence.source_id,&actor)?;
 assert!(provider.found_features_page(&feat_request("alice",&feat_auth)).map(|p|p["items"].as_array().unwrap().is_empty()).unwrap_or(true));
 let feat_left:i64=db.query_row("SELECT count(*) FROM fm_v2_change_sets WHERE owner_id='alice' AND source_event_id=?",[feat_fence.source_id],|r|r.get(0))?;assert_eq!(feat_left,0);
 let mut reset_source=source.clone();reset_source.message_id="helped-reset-root".into();
 let mut reset_found=found.clone();reset_found.source=reset_source.clone();reset_found.invocation.root_operation_id="helped-reset-root".into();reset_found.invocation.recall_attempt_id="helped-reset-recall".into();
 let reset_found_id=provider.append_found_observation(&reset_found)?;
 let (origin_id,origin_hash):(String,String)=db.query_row("SELECT change_set_id,content_hash FROM fm_v2_change_sets WHERE owner_id='alice' AND reason='s06.observation-origin.v1' AND json_extract(operation_metadata_json,'$.source.message_id')='helped-reset-root'",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
 let reset_authority=ObservationOriginAuthority{origin_id,origin_hash,captured_owner:"alice".into(),current_fingerprint:"b".repeat(64),rebind_id:None,rebind_hash:None};
 let mut reset_admission=admission.clone();reset_admission.found_event_id=reset_found_id;reset_admission.authority=reset_authority.clone();reset_admission.dispatch_id="reset-dispatch".into();
 let reset_admitted_id=provider.append_admitted_observation(&reset_admission)?;
 let mut reset_helped=helped.clone();reset_helped.admitted_event_id=reset_admitted_id;reset_helped.authority=reset_authority;reset_helped.request_key=key(&reset_helped);provider.append_helped_unexposed(&reset_helped)?;
 // Design: .clanker/robonotes/harness-and-provider-2026-10-28/implementation/memery/s04-ingest-retained-native.md (selective ingest reset is account-bound; helped receipts are retained).
 let components=vec!["ingest".to_string()];
 let helped_ids=|db:&Connection|->rusqlite::Result<Vec<String>>{let mut q=db.prepare("SELECT change_set_id FROM fm_v2_change_sets WHERE owner_id='alice' AND reason='s06.helped.v1' ORDER BY 1")?;let rows=q.query_map([],|r|r.get(0))?.collect();rows};
 let helped_before=helped_ids(&db)?;assert_eq!(helped_before.len(),1);
 let outbox_before:i64=db.query_row("SELECT count(*) FROM fm_v2_outbox WHERE owner_id='alice' AND kind='s06_helped'",[],|r|r.get(0))?;
 let preview=provider.guarded_selective_ingest_preview("alice","ws","fixture-account",&components)?;
 let selective=&preview["components"]["ingest"]["selective"];assert_eq!(selective["state"],"complete");
 // The ingest count is the one helped change set plus its outbox row (the earlier helped receipts were erased above); the fixture seeds no other ingest rows.
 assert_eq!(preview["components"]["ingest"]["count"],json!(helped_before.len() as i64+outbox_before));
 // Host manifest (services/memory/nuke_coordinator.py:1052): sha256 of canonical JSON of the partition and queued inventory (zero queued rows here).
 let partition=provider.selective_ingest_partition("alice","fixture-account",selective["partition_sha256"].as_str().ok_or("partition hash")?)?;
 let manifest=format!("{:x}",Sha256::digest(serde_json::to_vec(&json!({"version":1,"account_id":"fixture-account","operation_id":"helped-reset","partition":partition,"queued_inventory":{"count":0}}))?));
 provider.guarded_selective_ingest_reset("alice","ws","fixture-account","helped-reset",&components,&preview["components"],&manifest)?;
 // Retained by design: the helped receipt survives the ingest reset, and it is the very row that existed before it.
 assert_eq!(helped_ids(&db)?,helped_before);
 let mut rename_source=source.clone();rename_source.message_id="helped-rename-root".into();
 let mut rename_found=found.clone();rename_found.source=rename_source.clone();rename_found.invocation.root_operation_id="helped-rename-root".into();rename_found.invocation.recall_attempt_id="helped-rename-recall".into();let rename_found_id=provider.append_found_observation(&rename_found)?;
 let (rename_origin,rename_hash):(String,String)=db.query_row("SELECT change_set_id,content_hash FROM fm_v2_change_sets WHERE owner_id='alice' AND reason='s06.observation-origin.v1' AND json_extract(operation_metadata_json,'$.source.message_id')='helped-rename-root'",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
 let rename_auth=ObservationOriginAuthority{origin_id:rename_origin,origin_hash:rename_hash,captured_owner:"alice".into(),current_fingerprint:"b".repeat(64),rebind_id:None,rebind_hash:None};
 let mut rename_admission=admission.clone();rename_admission.found_event_id=rename_found_id;rename_admission.authority=rename_auth.clone();rename_admission.dispatch_id="rename-dispatch".into();let rename_admitted=provider.append_admitted_observation(&rename_admission)?;
 let mut rename_helped=helped.clone();rename_helped.admitted_event_id=rename_admitted;rename_helped.authority=rename_auth.clone();rename_helped.request_key=key(&rename_helped);let prior=provider.append_helped_unexposed(&rename_helped)?;
 let rename_fence=provider.fence_observation_origin("alice","ws",&rename_auth,&rename_source,Some("helped-rename"))?;let proof=rename_fence.rename_proof.ok_or("rename proof")?;
 let before=store.owner_counts("alice")?;let target=store.owner_counts("bob")?;
 let rename=GuardedOwnerRenameRequest{version:1,database_id:id,account_id:"fixture-account".into(),operation_id:"helped-rename".into(),step:"fm".into(),generation:1,action:"forward".into(),owner:"alice".into(),new_owner:"bob".into(),expected_source_fingerprint:before["fingerprint"].as_str().ok_or("source fingerprint")?.into(),expected_target_fingerprint:target["fingerprint"].as_str().ok_or("target fingerprint")?.into(),prior_receipt_id:None,prior_receipt_hash:None,rag_relocation:None};
 store.guarded_rename_owner(&rename)?;
 let renew=HostObservationRenewRequest{version:2,owner:"bob".into(),workspace_id:"ws".into(),original_owner:"alice".into(),operation_id:"helped-rename".into(),origin_id:rename_auth.origin_id.clone(),origin_hash:rename_auth.origin_hash.clone(),captured_owner:"alice".into(),prior_receipt_id:None,prior_receipt_hash:None,previous_fingerprint:rename_auth.current_fingerprint.clone(),current_fingerprint:"e".repeat(64),source:rename_source.clone(),rename_proof_id:proof.change_set_id};
 let renewed=provider.renew_observation_source(&renew)?;let current=ObservationOriginAuthority{origin_id:rename_auth.origin_id,origin_hash:rename_auth.origin_hash,captured_owner:"alice".into(),current_fingerprint:"e".repeat(64),rebind_id:Some(renewed.receipt_id),rebind_hash:Some(renewed.receipt_sha256)};
 let mut selector=rename_helped.clone();selector.owner="bob".into();selector.authority=current.clone();assert_eq!(provider.helped_status_unexposed(&selector,&current)?,prior);
 let page=provider.helped_selector_unexposed("bob","ws","fixture-account",&rename_source,&current,None,16)?;assert_eq!(page["items"][0]["recorded"]["positive"],true);
 Ok(())
}
