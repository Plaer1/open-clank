//! Synthetic integration target. Run serially: HOME/authority are process-wide.
//! The ignored frozen-corpus harness needs explicit admission by the operator.
#![cfg(unix)]
use fm_core::code::{producer_fingerprint, CodeWorkTicket, WorkspaceBinding};
use fm_core::code_evidence::{DeclarationEvidence, OccurrenceEvidence, ResolutionEvidence, ResolutionState};
use fm_core::code_metrics::{capture_code_metrics, CodeMetrics, Stage};
use fm_core::code_query::{Channel, CodeQuery, Operation, QueryFilter};
use fm_core::graph::GraphScope;
use fm_core::store::{code_evidence::{CodeEvidenceKind,CodeEvidenceQuery}, sqlite::SqliteStore};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::{collections::BTreeMap, ffi::OsString, path::PathBuf, sync::atomic::AtomicBool};

struct Fixture { root:PathBuf, previous:Vec<(&'static str,Option<OsString>)> }
impl Fixture {
    fn new()->Self {
        let root=std::env::temp_dir().join(format!("openclank-s00-{}",uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();let root=root.canonicalize().unwrap();
        for name in ["source","home"] {std::fs::create_dir(root.join(name)).unwrap();}
        let previous=["HOME","OPEN_CLANK_AUTHORITY_DB_PATH"].into_iter().map(|k|(k,std::env::var_os(k))).collect();
        std::env::set_var("HOME",root.join("home"));std::env::set_var("OPEN_CLANK_AUTHORITY_DB_PATH",root.join("authority.sqlite"));
        Self{root,previous}
    }
    fn binding(&self,source:PathBuf)->(GraphScope,WorkspaceBinding) {
        let authority=Connection::open(self.root.join("authority.sqlite")).unwrap();
        authority.execute_batch("CREATE TABLE file_policy_subject_aliases(username TEXT,subject_id TEXT,is_admin INTEGER);
            CREATE TABLE file_policy_workspaces(id TEXT,owner_subject_id TEXT,location_id TEXT,relative_folder TEXT,archived INTEGER,revision INTEGER);
            CREATE TABLE file_policy_locations(id TEXT,canonical_path TEXT,capabilities_json TEXT,enabled INTEGER,availability TEXT);
            CREATE TABLE file_policy_meta(singleton INTEGER,generation INTEGER);
            CREATE TABLE file_policy_bindings(binding_class TEXT,subject_kind TEXT,subject_id TEXT,location_id TEXT,status TEXT,expires_unix_ms INTEGER,workspace_id TEXT,chat_id TEXT,lifetime TEXT,remaining_uses INTEGER,capabilities_json TEXT);
            INSERT INTO file_policy_subject_aliases VALUES('alice','s00-alice',0);
            INSERT INTO file_policy_workspaces VALUES('workspace','s00-alice','location','',0,1);
            INSERT INTO file_policy_meta VALUES(1,1);
            INSERT INTO file_policy_bindings VALUES('people','user','s00-alice','location','active',NULL,NULL,NULL,'always',NULL,'[\"read\"]');
            INSERT INTO file_policy_bindings VALUES('agent','user','s00-alice','location','active',NULL,NULL,NULL,'always',NULL,'[\"read\"]');").unwrap();
        authority.execute("INSERT INTO file_policy_locations VALUES('location',?1,'[\"read\"]',1,'available')",params![source.to_str().unwrap()]).unwrap();
        (GraphScope::new("alice","workspace").unwrap(),WorkspaceBinding{owner:"alice".into(),owner_subject_id:"s00-alice".into(),workspace_id:"workspace".into(),location_id:"location".into(),workspace_revision:1,policy_generation:1,root:source,origin:"agent".into(),subscriber_id:"s00-baseline".into(),authority_expires_unix_ms:None})
    }
    fn store(&self)->SqliteStore {SqliteStore::new(self.root.join("fm.sqlite").to_str().unwrap(),4).unwrap()}
}
impl Drop for Fixture {fn drop(&mut self){
    for (key,value) in &self.previous {if let Some(value)=value{std::env::set_var(key,value)}else{std::env::remove_var(key)}}
    assert!(self.root.file_name().unwrap().to_str().unwrap().starts_with("openclank-s00-"));let _=std::fs::remove_dir_all(&self.root);
}}
fn ticket(store:&SqliteStore,scope:&GraphScope,binding:&WorkspaceBinding)->CodeWorkTicket {
    let state=store.code_workspace_prepare(scope,binding,false).unwrap();
    CodeWorkTicket{checkout_id:state["checkout_id"].as_str().unwrap().into(),admission_epoch:state["admission_epoch"].as_i64().unwrap(),config_fingerprint:producer_fingerprint(),policy_generation:1,workspace_revision:1}
}
fn query(store:&SqliteStore,scope:&GraphScope,binding:&WorkspaceBinding,q:&CodeQuery)->fm_core::code_query::CodeQueryResult {
    store.code_query(scope,binding,q,&AtomicBool::new(false),&[43;32]).unwrap()
}
fn calls(metrics:&CodeMetrics,stage:Stage)->u64 {metrics.stages[stage as usize].counters.calls}

#[test]
fn golden_relationships_small_and_partitioned_and_metrics_do_not_change_queries(){
    let golden:Value=serde_json::from_str(include_str!("fixtures/s00_code_graph.json")).unwrap();
    for large in [false,true] {
        let fixture=Fixture::new();let source=fixture.root.join("source");
        for (path,text) in golden["files"].as_object().unwrap(){let path=source.join(path);std::fs::create_dir_all(path.parent().unwrap()).unwrap();std::fs::write(path,text.as_str().unwrap()).unwrap();}
        if large {for n in 0..golden["partition_fillers"].as_u64().unwrap(){std::fs::write(source.join(format!("f{n:03}.py")),format!("def filler_{n}():\n    return {n}\n")).unwrap();}}
        let (scope,binding)=fixture.binding(source);let store=fixture.store();let work=ticket(&store,&scope,&binding);
        let (indexed,metrics)=capture_code_metrics(||store.code_index_bound(&scope,&binding,&work));let indexed=indexed.unwrap();
        assert_eq!(indexed.errors.len(),1);assert!(indexed.coverage.iter().any(|c|c.rel_path=="broken.py"&&c.status=="parse_failed"));
        assert!(indexed.coverage.iter().any(|c|c.rel_path=="ignored.py"&&c.status=="ignored"));
        assert!(indexed.coverage.iter().any(|c|c.rel_path=="assets/mark.svg"&&c.status=="unsupported"));
        for stage in [Stage::IndexTotal,Stage::Inventory,Stage::Read,Stage::Hash,Stage::Extract,Stage::Interfaces,Stage::Resolve,Stage::Publish]{assert!(calls(&metrics,stage)>0,"missing {stage:?}");}
        if large {assert!(calls(&metrics,Stage::Stage)>0);assert!(calls(&metrics,Stage::Seal)>0);}
        let head=store.code_index_head(&scope,&indexed.codebase).unwrap().unwrap();assert_eq!(head.source_snapshot.evidence_store_version,u32::from(large));
        let receipt=store.code_evidence_receipt(&scope,&binding).unwrap();
        let mut declarations=BTreeMap::<String,DeclarationEvidence>::new();let mut resolutions=Vec::<ResolutionEvidence>::new();let mut occurrences=Vec::<OccurrenceEvidence>::new();
        for path in ["dep.py","caller.py","island.py","src/dep.rs","src/lib.rs"] {
            for kind in [CodeEvidenceKind::Declarations,CodeEvidenceKind::Occurrences,CodeEvidenceKind::Resolutions] {
                let page=store.code_evidence_page(&scope,&binding,&receipt,CodeEvidenceQuery{kind,rel_path:Some(path.into()),..Default::default()}).unwrap();
                assert!(page.next_cursor.is_none());
                for row in page.rows {let value=row["evidence"].clone();match kind {CodeEvidenceKind::Declarations=>{let d:DeclarationEvidence=serde_json::from_value(value).unwrap();declarations.insert(d.id.clone(),d);},CodeEvidenceKind::Occurrences=>occurrences.push(serde_json::from_value(value).unwrap()),CodeEvidenceKind::Resolutions=>resolutions.push(serde_json::from_value(value).unwrap()),_=>unreachable!()}}
            }
        }
        for expected in golden["relationships"].as_array().unwrap(){assert!(resolutions.iter().any(|r| {
            let caller=r.caller_id.as_ref().and_then(|id|declarations.get(id));let target=r.target_id.as_ref().and_then(|id|declarations.get(id));
            caller.is_some_and(|d|d.name==expected["caller"].as_str().unwrap()&&d.source.rel_path==expected["caller_file"].as_str().unwrap())&&target.is_some_and(|d|d.name==expected["target"].as_str().unwrap()&&d.source.rel_path==expected["target_file"].as_str().unwrap())&&serde_json::to_value(&r.state).unwrap()==expected["state"]
        }),"missing semantic relationship {expected}");}
        let unresolved=occurrences.iter().find(|o|o.symbol_text==golden["unresolved_symbol"].as_str().unwrap()).unwrap();
        assert!(resolutions.iter().any(|r|r.occurrence_id==unresolved.id&&r.state==ResolutionState::Unresolved&&r.target_id.is_none()));
        let island=declarations.values().find(|d|d.name==golden["disconnected_declaration"].as_str().unwrap()).unwrap();assert!(!resolutions.iter().any(|r|r.caller_id.as_ref()==Some(&island.id)||r.target_id.as_ref()==Some(&island.id)));
        let q=CodeQuery{query:Some("caller".into()),channels:vec![Channel::Exact],filter:QueryFilter{kinds:vec!["declaration".into()],..Default::default()},..Default::default()};
        let plain=query(&store,&scope,&binding,&q);let (measured,qmetrics)=capture_code_metrics(||query(&store,&scope,&binding,&q));
        assert_eq!(serde_json::to_value(&plain).unwrap(),serde_json::to_value(&measured).unwrap());assert!(!plain.hits.is_empty());
        for stage in [Stage::QueryTotal,Stage::QueryPrepare,Stage::QuerySql,Stage::Projection,Stage::Serialization]{assert!(calls(&qmetrics,stage)>0,"missing {stage:?}");}
        println!("S00_GOLDEN {}",json!({"partitioned":large,"index":metrics,"query":qmetrics}));
    }
}

#[test]
#[ignore="root-coordinated frozen disposable corpus only; creates isolated authority/index"]
fn frozen_corpus_cold_warm_baseline(){
    let source=PathBuf::from(std::env::var_os("OPENCLANK_S00_FROZEN_CORPUS").expect("explicit frozen source required")).canonicalize().unwrap();
    assert!(source.starts_with("/private/tmp")||source.starts_with("/tmp"));assert!(!source.join(".git").exists()&&!source.join(".clanker").exists());
    let fixture=Fixture::new();let (scope,binding)=fixture.binding(source);let store=fixture.store();
    for phase in ["cold","warm"]{
        let work=ticket(&store,&scope,&binding);let (index,metrics)=capture_code_metrics(||store.code_index_bound(&scope,&binding,&work));let index=index.unwrap();
        println!("S00_BASELINE {}",json!({"phase":phase,"producer_fingerprint":producer_fingerprint(),"indexed":index.files_indexed,"errors":index.errors.len(),"coverage_entries":index.coverage.len(),"metrics":metrics}));
        for op in [Operation::Overview,Operation::Facets,Operation::Catalog]{for iteration in 0..5{let (result,metrics)=capture_code_metrics(||query(&store,&scope,&binding,&CodeQuery{operation:op,..Default::default()}));println!("S00_QUERY {}",json!({"phase":phase,"operation":op,"iteration":iteration,"metrics":metrics,"cost":result.cost,"has_more":result.next_cursor.is_some()}));}}
    }
}
