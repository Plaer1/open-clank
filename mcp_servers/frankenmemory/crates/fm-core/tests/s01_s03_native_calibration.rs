//! Distinct S01/S03 target: never rebuild the executing S00 integration target.
//! Serial synthetic smoke; ignored corpus run requires root-coordinated opt-in.
#![cfg(unix)]
use fm_core::code::{producer_fingerprint, CodeWorkTicket, WorkspaceBinding};
use fm_core::code_evidence::{ResolutionEvidence, ResolutionState};
use fm_core::code_metrics::{capture_code_metrics, CodeMetrics, Stage};
use fm_core::code_query::{CodeQuery, CodeQueryResult, Direction, Field, Operation};
use fm_core::graph::GraphScope;
use fm_core::store::{code_evidence::{CodeEvidenceKind, CodeEvidenceQuery}, sqlite::SqliteStore};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::{collections::{BTreeMap, BTreeSet}, ffi::OsString, path::{Path, PathBuf}, sync::atomic::AtomicBool, time::Instant};

const CORPUS: &str = "/private/tmp/openclank-graph-qol-s06.xq1AvF/codebase";
const MANIFEST: &str = "cc4f7300ac1d899348cc5d45fa00738230272801524af618dad783e029476537";
struct Fixture { root: PathBuf, previous: Vec<(&'static str, Option<OsString>)> }
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("openclank-s01-s03-{}",uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap(); let root = root.canonicalize().unwrap();
        for name in ["source","home"] { std::fs::create_dir(root.join(name)).unwrap(); }
        let previous = ["HOME","OPEN_CLANK_AUTHORITY_DB_PATH"].into_iter().map(|k|(k,std::env::var_os(k))).collect();
        std::env::set_var("HOME",root.join("home"));
        std::env::set_var("OPEN_CLANK_AUTHORITY_DB_PATH",root.join("authority.sqlite"));
        Self { root, previous }
    }
    fn binding(&self, source: PathBuf) -> (GraphScope, WorkspaceBinding) {
        let authority = Connection::open(self.root.join("authority.sqlite")).unwrap();
        authority.execute_batch("CREATE TABLE file_policy_subject_aliases(username TEXT,subject_id TEXT,is_admin INTEGER);
            CREATE TABLE file_policy_workspaces(id TEXT,owner_subject_id TEXT,location_id TEXT,relative_folder TEXT,archived INTEGER,revision INTEGER);
            CREATE TABLE file_policy_locations(id TEXT,canonical_path TEXT,capabilities_json TEXT,enabled INTEGER,availability TEXT);
            CREATE TABLE file_policy_meta(singleton INTEGER,generation INTEGER);
            CREATE TABLE file_policy_bindings(binding_class TEXT,subject_kind TEXT,subject_id TEXT,location_id TEXT,status TEXT,expires_unix_ms INTEGER,workspace_id TEXT,chat_id TEXT,lifetime TEXT,remaining_uses INTEGER,capabilities_json TEXT);
            INSERT INTO file_policy_subject_aliases VALUES('alice','calibration-alice',0);
            INSERT INTO file_policy_workspaces VALUES('workspace','calibration-alice','location','',0,1);
            INSERT INTO file_policy_meta VALUES(1,1);
            INSERT INTO file_policy_bindings VALUES('people','user','calibration-alice','location','active',NULL,NULL,NULL,'always',NULL,'[\"read\"]');
            INSERT INTO file_policy_bindings VALUES('agent','user','calibration-alice','location','active',NULL,NULL,NULL,'always',NULL,'[\"read\"]');").unwrap();
        authority.execute("INSERT INTO file_policy_locations VALUES('location',?1,'[\"read\"]',1,'available')",params![source.to_str().unwrap()]).unwrap();
        (GraphScope::new("alice","workspace").unwrap(), WorkspaceBinding {
            owner:"alice".into(),owner_subject_id:"calibration-alice".into(),workspace_id:"workspace".into(),location_id:"location".into(),
            workspace_revision:1,policy_generation:1,root:source,origin:"agent".into(),subscriber_id:"s01-s03-calibration".into(),authority_expires_unix_ms:None,
        })
    }
    fn store(&self) -> SqliteStore { SqliteStore::new(self.root.join("fm.sqlite").to_str().unwrap(),4).unwrap() }
}
impl Drop for Fixture { fn drop(&mut self) {
    for (key,value) in &self.previous { if let Some(value)=value { std::env::set_var(key,value); } else { std::env::remove_var(key); } }
    assert!(self.root.file_name().unwrap().to_str().unwrap().starts_with("openclank-s01-s03-"));
    let _ = std::fs::remove_dir_all(&self.root);
} }
fn calls(metrics:&CodeMetrics,stage:Stage)->u64 { metrics.stages[stage as usize].counters.calls }
fn request(operation:Operation)->CodeQuery {
    // Exact original S00 Overview request, not a smaller/faster projection.
    if operation==Operation::Overview {return CodeQuery{operation,..Default::default()};}
    // Separate newly measured Neighbors operation: current workbench fields
    // and native envelope after Python's existing projection reserve.
    let mut q=CodeQuery{operation,fields:vec![Field::Label,Field::Source,Field::Signature,Field::Documentation,Field::Properties,Field::Resolution,Field::Witnesses],..Default::default()};
    q.budgets.bytes=64_512;q.budgets.tokens=7_168;q.budgets.rows=50;q.budgets.work=4_000_000;q.budgets.hops=4;q
}
fn language(path:&str)-> &'static str { match Path::new(path).extension().and_then(|s|s.to_str()) {
    Some("py")=>"python",Some("rs")=>"rust",Some("ts")=>"typescript",Some("tsx")=>"tsx",_=>"other",
} }
fn histogram(coverage:&[fm_core::code::ScanCoverage])->BTreeMap<String,usize> {
    let mut out=BTreeMap::new();for row in coverage { *out.entry(format!("{}:{}",row.status,language(&row.rel_path))).or_default()+=1; }out
}
fn execute(store:&SqliteStore,scope:&GraphScope,binding:&WorkspaceBinding,q:&CodeQuery)->CodeQueryResult {
    let r=store.code_query(scope,binding,q,&AtomicBool::new(false),&[43;32]).unwrap_or_else(|e|panic!("query failed: {}",e.code));
    assert!(r.error.is_none());assert!(r.receipt.is_some());
    assert_eq!(r.cost.files_bytes_read,0);assert_eq!(r.cost.validation_bytes_read,0);
    assert!(r.cost.inspected_records<=q.budgets.work);assert!(r.cost.serialized_bytes<=q.budgets.tokens.min(q.budgets.bytes));
    assert_eq!(r.cost.token_upper_bound,r.cost.serialized_bytes);
    assert!(r.hits.iter().all(|h|!h.current_source_verified));r
}
// A finite receipt-bound metadata page, never an asset or guessed name as seed.
fn edge_seed(store:&SqliteStore,scope:&GraphScope,binding:&WorkspaceBinding,path:&str)->ResolutionEvidence {
    let receipt=store.code_evidence_receipt(scope,binding).unwrap();let mut cursor=None;let mut seen=BTreeSet::new();
    for _ in 0..128 {
        let page=store.code_evidence_page(scope,binding,&receipt,CodeEvidenceQuery{kind:CodeEvidenceKind::Resolutions,rel_path:Some(path.into()),cursor:cursor.clone(),max_rows:200,max_bytes:256*1024}).unwrap_or_else(|_|panic!("seed evidence unavailable"));
        let mut candidates:Vec<ResolutionEvidence>=page.rows.into_iter().map(|r|serde_json::from_value(r["evidence"].clone()).unwrap()).filter(|r:&ResolutionEvidence|matches!(r.state,ResolutionState::Exact|ResolutionState::Inferred)&&r.caller_id.is_some()&&r.target_id.is_some()&&r.caller_id!=r.target_id).collect();
        candidates.sort_by(|a,b|a.occurrence_id.cmp(&b.occurrence_id));
        if let Some(edge)=candidates.into_iter().next(){return edge;}
        cursor=page.next_cursor;let Some(next)=&cursor else {break};assert!(seen.insert(next.clone()));
    }
    panic!("no bounded real one-hop seed");
}
fn same_edge(result:&CodeQueryResult,edge:&ResolutionEvidence)->bool {
    let state=match edge.state {ResolutionState::Exact=>"exact",ResolutionState::Inferred=>"inferred",_=>return false};
    result.edges.iter().any(|e|e.from==format!("decl:{}",edge.caller_id.as_ref().unwrap())&&e.to==format!("decl:{}",edge.target_id.as_ref().unwrap())&&e.occurrence_id==format!("occ:{}",edge.occurrence_id)&&e.resolution_state==state)
}
fn emit_query(phase:&str,label:&str,iteration:Option<usize>,r:&CodeQueryResult,m:&CodeMetrics) {
    let logical=serde_json::to_vec(r).unwrap();
    println!("S01_S03_QUERY {}",json!({"phase":phase,"sample":label,"iteration":iteration,"operation":r.operation,"metrics":m,"cost":r.cost,"has_more":r.next_cursor.is_some(),"hits":r.hits.len(),"edges":r.edges.len(),"logical_blake3":blake3::hash(&logical).to_hex().to_string(),"receipt_blake3":blake3::hash(&serde_json::to_vec(&r.receipt).unwrap()).to_hex().to_string()}));
}
struct Neighborhood { pages:Vec<CodeQueryResult>, first_page_ns:u128, first_edge_ns:u128, complete_ns:u128 }
fn neighborhood(store:&SqliteStore,scope:&GraphScope,binding:&WorkspaceBinding,q:&CodeQuery,edge:&ResolutionEvidence)->Neighborhood {
    let started=Instant::now();let mut request=q.clone();let mut pages=Vec::new();let mut cursors=BTreeSet::new();
    let mut first_page_ns=0;let mut first_edge_ns=None;
    for _ in 0..128 {
        let result=execute(store,scope,binding,&request);
        assert!(result.receipt==q.expected_receipt,"neighborhood head changed");
        if pages.is_empty(){first_page_ns=started.elapsed().as_nanos();}
        if first_edge_ns.is_none()&&same_edge(&result,edge){first_edge_ns=Some(started.elapsed().as_nanos());}
        request.cursor=result.next_cursor.clone();pages.push(result);
        if let Some(cursor)=&request.cursor {assert!(cursors.insert(cursor.clone()),"neighborhood cursor cycle");}
        else {return Neighborhood{pages,first_page_ns,first_edge_ns:first_edge_ns.expect("complete neighborhood omitted fixed real edge"),complete_ns:started.elapsed().as_nanos()};}
    }
    panic!("neighborhood still has a cursor at 128-page ceiling");
}
fn emit_neighborhood(phase:&str,label:&str,iteration:Option<usize>,view:&Neighborhood,m:&CodeMetrics,edge:&ResolutionEvidence) {
    let logical=serde_json::to_vec(&view.pages).unwrap();
    let per_page:Vec<_>=view.pages.iter().map(|p|json!({"cost":p.cost,"hits":p.hits.len(),"edges":p.edges.len(),"fixed_real_edge":same_edge(p,edge),"has_more":p.next_cursor.is_some()})).collect();
    println!("S01_S03_NEIGHBORHOOD {}",json!({"phase":phase,"sample":label,"iteration":iteration,"metrics":m,"pages":per_page,"page_count":view.pages.len(),"first_page_ns":view.first_page_ns,"first_real_edge_ns":view.first_edge_ns,"complete_sequence_ns":view.complete_ns,"first_page_contains_fixed_edge":same_edge(&view.pages[0],edge),"logical_pages_blake3":blake3::hash(&logical).to_hex().to_string(),"receipt_blake3":blake3::hash(&serde_json::to_vec(&view.pages[0].receipt).unwrap()).to_hex().to_string(),"historical_same_operation_baseline":false}));
}
fn calibrate(source:PathBuf,synthetic:bool) {
    let fixture=Fixture::new();let(scope,binding)=fixture.binding(source);let store=fixture.store();let mut prior_coverage=None;
    for phase in ["cold","unchanged"] {
        let state=store.code_workspace_prepare(&scope,&binding,false).unwrap();
        let work=CodeWorkTicket{checkout_id:state["checkout_id"].as_str().unwrap().into(),admission_epoch:state["admission_epoch"].as_i64().unwrap(),config_fingerprint:producer_fingerprint(),policy_generation:1,workspace_revision:1};
        let(index,metrics)=capture_code_metrics(||store.code_index_bound(&scope,&binding,&work));
        let index=index.unwrap_or_else(|_|panic!("index failed (private detail omitted)"));
        let coverage=histogram(&index.coverage);assert_eq!(coverage.values().sum::<usize>(),index.coverage.len());
        if let Some(prior)=&prior_coverage {assert_eq!(prior,&coverage);} prior_coverage=Some(coverage.clone());
        if synthetic { assert_eq!(index.errors.len(),1);assert!(index.files_indexed>128); }
        else {
            assert_eq!(index.files_indexed,3877);assert_eq!(index.errors.len(),6);assert_eq!(index.coverage.len(),8589);
            let mut statuses=BTreeMap::new();for c in &index.coverage{*statuses.entry(c.status.as_str()).or_insert(0usize)+=1;}
            assert_eq!(statuses,BTreeMap::from([("indexed",3755),("no_source_units",122),("ignored",395),("outside_root",26),("oversize",1),("parse_failed",6),("unsupported",4284)]));
        }
        assert_eq!(metrics.schema,"openclank.code-metrics/v2");
        for stage in [Stage::IndexTotal,Stage::Inventory,Stage::Extract,Stage::Interfaces,Stage::Resolve,Stage::Stage,Stage::Seal,Stage::Publish] {assert!(calls(&metrics,stage)>0);}
        println!("S01_S03_INDEX {}",json!({"phase":phase,"synthetic":synthetic,"producer_fingerprint":producer_fingerprint(),"source_manifest_sha256_operator_verified":if synthetic{None}else{Some(MANIFEST)},"indexed":index.files_indexed,"unchanged":index.files_unchanged,"removed":index.files_removed,"errors":index.errors.len(),"coverage_entries":index.coverage.len(),"coverage_status_extension_language":coverage,"metrics":metrics}));
        // Seed preparation is outside timed query captures. Evidence reads do
        // not use the query audit memo. The first Overview must remain a miss.
        let edge=edge_seed(&store,&scope,&binding,if synthetic{"caller.py"}else{"src/openclank/code_resource_bridge.py"});
        let overview=request(Operation::Overview);
        let(first,miss)=capture_code_metrics(||execute(&store,&scope,&binding,&overview));
        assert_eq!(calls(&miss,Stage::GenerationAuditMiss),1);assert_eq!(calls(&miss,Stage::EligibleCountMiss),1);
        assert_eq!(calls(&miss,Stage::GenerationAuditHit),0);assert!(!first.hits.is_empty());
        emit_query(phase,"initial_audit_count_miss",None,&first,&miss);
        let mut neighbors=request(Operation::Neighbors);neighbors.seed_ids=vec![format!("decl:{}",edge.caller_id.as_ref().unwrap())];neighbors.direction=Some(Direction::Outgoing);neighbors.expected_receipt=first.receipt.clone();
        println!("S01_S03_CONFIG {}",json!({"phase":phase,"overview":overview,"neighbors":{"operation":neighbors.operation,"fields":neighbors.fields,"channels":neighbors.channels,"budgets":neighbors.budgets,"direction":neighbors.direction,"real_edge_proof_required":true},"neighbors_historical_same_operation_baseline":false}));
        let(warm,warm_metrics)=capture_code_metrics(||execute(&store,&scope,&binding,&overview));
        assert!(warm==first,"Overview logical parity failed");emit_query(phase,"excluded_warmup",None,&warm,&warm_metrics);
        for iteration in 0..5 {
            let(r,m)=capture_code_metrics(||execute(&store,&scope,&binding,&overview));assert!(r==warm,"measured Overview logical parity failed");
            assert_eq!(calls(&m,Stage::GenerationAuditHit),1);assert_eq!(calls(&m,Stage::GenerationAuditMiss),0);
            assert_eq!(calls(&m,Stage::EligibleCountHit),1);assert_eq!(calls(&m,Stage::EligibleCountMiss),0);
            emit_query(phase,"measured",Some(iteration),&r,&m);
        }
        let(warm,m)=capture_code_metrics(||neighborhood(&store,&scope,&binding,&neighbors,&edge));
        emit_neighborhood(phase,"excluded_warmup",None,&warm,&m,&edge);
        for iteration in 0..5 {
            let(view,m)=capture_code_metrics(||neighborhood(&store,&scope,&binding,&neighbors,&edge));
            assert!(view.pages==warm.pages,"complete ordered neighborhood pages changed");
            assert_eq!(calls(&m,Stage::GenerationAuditHit),view.pages.len() as u64);assert_eq!(calls(&m,Stage::GenerationAuditMiss),0);
            assert_eq!(calls(&m,Stage::EligibleCountHit),view.pages.len() as u64);assert_eq!(calls(&m,Stage::EligibleCountMiss),0);
            emit_neighborhood(phase,"measured",Some(iteration),&view,&m,&edge);
        }
        assert!(store.code_evidence_receipt(&scope,&binding).unwrap()==first.receipt.unwrap().evidence,"final head changed");
    }
}

#[test]
fn synthetic_partitioned_calibration_smoke() {
    // Keep synthetic source alive outside the independently isolated store.
    let source=Fixture::new();let root=source.root.join("source");
    let golden:Value=serde_json::from_str(include_str!("fixtures/s00_code_graph.json")).unwrap();
    for(path,text)in golden["files"].as_object().unwrap(){let p=root.join(path);std::fs::create_dir_all(p.parent().unwrap()).unwrap();std::fs::write(p,text.as_str().unwrap()).unwrap();}
    for n in 0..129 {std::fs::write(root.join(format!("f{n:03}.py")),format!("def filler_{n}(): return {n}\n")).unwrap();}
    calibrate(root,true);
}

#[test]
#[ignore="explicit root-coordinated frozen corpus, no concurrent benchmark jobs"]
fn frozen_combined_s01_s03_calibration() {
    let source=PathBuf::from(std::env::var_os("OPENCLANK_S01_S03_FROZEN_CORPUS").expect("explicit source opt-in required")).canonicalize().unwrap();
    assert_eq!(source,PathBuf::from(CORPUS));
    assert_eq!(std::env::var("OPENCLANK_S01_S03_VERIFIED_MANIFEST_SHA256").unwrap(),MANIFEST);
    assert!(!source.join(".git").exists()&&!source.join(".clanker").exists());
    calibrate(source,false);
}
