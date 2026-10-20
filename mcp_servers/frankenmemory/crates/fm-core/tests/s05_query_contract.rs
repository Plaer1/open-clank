//! Independent root-owned wire and synthetic pure-query checks.
//! These do not qualify live transport, Files reads, context or Graph consumers.
use fm_core::code_query::*;
use fm_core::code_evidence::{extract_evidence, resolve_evidence, CorpusInput, ExtractionBudget, ResolutionBudget};
use fm_core::code::ScanCoverage;
use serde_json::{json, Value};
use std::sync::atomic::AtomicBool;
use std::collections::BTreeSet;

fn receipt() -> Value {
    json!({
        "workspace_id": "workspace-a",
        "registration_id": "registration-a",
        "source_snapshot_id": "snapshot-a",
        "checkout_id": "checkout-a",
        "head_generation": "generation-a",
        "admission_epoch": 2,
        "policy_generation": 3,
        "config_fingerprint": "producer-a"
    })
}

#[test]
fn defaults_are_read_only_and_do_not_select_owner_or_root() {
    let query: CodeQuery = serde_json::from_value(json!({})).unwrap();
    assert_eq!(query.operation, Operation::Search);
    assert_eq!(query.channels, vec![Channel::Exact, Channel::Identifier]);
    assert!(!query.fields.contains(&Field::Excerpt));
    assert_eq!(query.schema, QUERY_SCHEMA);
    let wire = serde_json::to_value(query).unwrap();
    assert!(wire.get("owner").is_none());
    assert!(wire.get("root").is_none());
}

#[test]
fn receipt_round_trips_without_losing_scope_or_native_evidence() {
    let wire = receipt();
    let parsed: QueryReceipt = serde_json::from_value(wire.clone()).unwrap();
    assert_eq!(parsed.evidence.checkout_id, "checkout-a");
    assert_eq!(parsed.workspace_id, "workspace-a");
    assert_eq!(serde_json::to_value(parsed).unwrap(), wire);
    let query: CodeQuery = serde_json::from_value(json!({"expected_receipt": wire})).unwrap();
    let decoded: CodeQuery = serde_json::from_slice(&serde_json::to_vec(&query).unwrap()).unwrap();
    assert_eq!(decoded, query);
}

#[test]
fn receipt_cannot_smuggle_a_root_owner_or_unrecognized_revision() {
    for key in ["root", "owner", "authority_db", "fake_generation"] {
        let mut wire = receipt();
        wire[key] = json!("untrusted");
        assert!(serde_json::from_value::<QueryReceipt>(wire).is_err(), "{key}");
    }
}

#[test]
fn public_request_rejects_unknown_fields_at_every_input_boundary() {
    for wire in [
        json!({"owner":"bob"}),
        json!({"root":"/private"}),
        json!({"action":"admit"}),
        json!({"filter":{"owner":"bob"}}),
        json!({"budgets":{"infinite":true}}),
        json!({"read":{"rel_path":"a.py","start_byte":0,"length":20,"root":"/private"}}),
        json!({"stages":[{"operation":"find","root":"/private"}]}),
        json!({"operation":"erase"}),
        json!({"channels":["invented_semantic"]}),
        json!({"fields":["private_root"]}),
    ] {
        assert!(serde_json::from_value::<CodeQuery>(wire.clone()).is_err(), "{wire}");
    }
}

#[test]
fn omitted_source_fields_stay_absent_on_hits_and_path_edges() {
    let hit: QueryHit = serde_json::from_value(json!({
        "id":"decl:opaque", "kind":"declaration", "matches":[],
        "evidence_state":"metadata", "current_source_verified":false
    })).unwrap();
    let wire = serde_json::to_value(hit).unwrap();
    for field in ["source", "signature", "documentation", "excerpt", "raw_source", "properties"] {
        assert!(wire.get(field).is_none(), "{field}");
    }
    let path: QueryPath = serde_json::from_value(json!({
        "node_ids":["decl:a","decl:b"],
        "edges":[{"from":"decl:a","to":"decl:b","occurrence_id":"occ:c","resolution_state":"inferred"}]
    })).unwrap();
    let wire = serde_json::to_value(path).unwrap();
    assert!(wire["edges"][0].get("source").is_none());
    assert!(wire["edges"][0].get("witnesses").is_none());
    assert_eq!(wire["edges"][0]["resolution_state"], "inferred");
}

#[test]
fn failure_without_an_authorized_head_does_not_invent_a_receipt() {
    let wire = json!({
        "schema":QUERY_SCHEMA,"operation":"search","receipt":null,
        "capabilities":[],"channels":[],"hits":[],"edges":[],"paths":[],
        "counts":[],"facets":{},"omissions":[],"partial":false,"degraded":true,
        "reasons":["denied"],"next_cursor":null,
        "error":{"code":"denied","message":"Unavailable code resource"},
        "cost":{"inspected_records":0,"serialized_bytes":0,"token_upper_bound":0,
            "metadata_bytes":0,"files_bytes_read":0,"validation_bytes_read":0}
    });
    let parsed: CodeQueryResult = serde_json::from_value(wire).unwrap();
    assert!(parsed.receipt.is_none());
    assert_eq!(parsed.error.unwrap().code, "denied");
}

fn snapshot(sources: &[(&str, &str)]) -> QuerySnapshot {
    let files: Vec<_> = sources.iter().map(|(path, source)| {
        extract_evidence("checkout-a", path, source.as_bytes(), &ExtractionBudget::default()).unwrap()
    }).collect();
    let evidence = resolve_evidence(&CorpusInput {
        checkout_id: "checkout-a".into(), files: files.clone(), unavailable_files: vec![],
    }, &ResolutionBudget::default()).unwrap();
    let coverage = files.iter().map(|file| ScanCoverage {
        rel_path: file.rel_path.clone(), status: "indexed".into(),
        source_hash: Some(file.source_hash.clone()), source_generation: Some("generation-a".into()),
        detail: None,
    }).collect();
    QuerySnapshot { receipt: serde_json::from_value(receipt()).unwrap(),
        scope_key: "alice:subject-a:workspace-a:revision-3".into(), files,
        resolutions: evidence.resolutions, coverage, metadata_bytes: 1000, metadata_records: 0,
        eligible_records: None }
}

fn execute(snapshot: &QuerySnapshot, query: &CodeQuery) -> Result<CodeQueryResult, QueryError> {
    run_query(snapshot, query, &AtomicBool::new(false), &[7;32])
}

#[test]
fn coverage_file_identity_matches_parser_file_identity() {
    let snapshot = snapshot(&[("source.py", "def target(): pass\n")]);
    assert_eq!(file_id("checkout-a", "source.py"), snapshot.files[0].file_id);
}

#[test]
fn resolved_occurrence_filter_uses_resolution_state_before_candidate_filtering() {
    let snapshot = snapshot(&[("source.py", "def target(): pass\ndef caller():\n    target()\n")]);
    let query = CodeQuery { operation: Operation::Search, query: Some("target".into()),
        filter: QueryFilter { kinds: vec!["occurrence".into()], states: vec!["exact".into()], ..Default::default() },
        ..Default::default() };
    let result = execute(&snapshot, &query).unwrap();
    assert!(!result.hits.is_empty(), "a statically exact call must remain searchable");
    assert!(result.hits.iter().all(|hit| hit.kind == "occurrence" && hit.evidence_state == "exact"));
}

#[test]
fn outline_seed_selects_only_its_file() {
    let snapshot = snapshot(&[("a.py", "def alpha(): pass\n"), ("b.py", "def beta(): pass\n")]);
    let query = CodeQuery { operation: Operation::Outline,
        seed_ids: vec![format!("file:{}", snapshot.files[0].file_id)], ..Default::default() };
    let result = execute(&snapshot, &query).unwrap();
    assert_eq!(result.hits.len(), 1);
    assert_eq!(result.hits[0].source.as_ref().unwrap().rel_path, "a.py");
}

#[test]
fn impact_follows_dependents_not_the_changed_symbols_dependencies() {
    let snapshot = snapshot(&[("source.py", "def leaf(): pass\ndef caller():\n    leaf()\ndef outer():\n    caller()\n")]);
    let id = |name: &str| format!("decl:{}", snapshot.files[0].declarations.iter().find(|d|d.name==name).unwrap().id);
    let query = CodeQuery { operation: Operation::Impact, seed_ids: vec![id("leaf")],
        channels: vec![Channel::Structural], ..Default::default() };
    let result = execute(&snapshot, &query).unwrap();
    let hits: BTreeSet<_> = result.hits.iter().map(|hit|hit.id.clone()).collect();
    assert!(hits.contains(&id("caller")) && hits.contains(&id("outer")), "incoming callsites are impacted by a leaf change");
    assert!(result.edges.iter().all(|edge|edge.from!=id("leaf")), "reverse traversal must preserve original directed edge witnesses");
}

#[test]
fn catalog_does_not_claim_to_have_executed_search_channels() {
    let snapshot = snapshot(&[("a.py", "def alpha(): pass\n")]);
    let query = CodeQuery { operation: Operation::Catalog, ..Default::default() };
    let result = execute(&snapshot, &query).unwrap();
    for lane in &result.channels {
        assert!(!lane.executed, "catalog did not run {:?} search", lane.channel);
        assert!(lane.elapsed_micros.is_none(), "unexecuted lanes have no measured latency");
    }
}

#[test]
fn unsupported_coverage_remains_partial_without_a_next_page() {
    let mut snapshot = snapshot(&[("a.py", "def alpha(): pass\n")]);
    snapshot.coverage.push(ScanCoverage { rel_path: "b.jsx".into(), status: "unsupported".into(),
        detail: Some("grammar unavailable".into()), source_hash: None, source_generation: None });
    let result = execute(&snapshot, &CodeQuery {operation: Operation::Catalog, ..Default::default()}).unwrap();
    assert!(result.next_cursor.is_none());
    assert!(result.partial, "source coverage and result pagination are independent");
    assert_eq!(result.hits.len(), 2, "catalog must include unavailable source entries");
}

#[test]
fn small_envelopes_never_return_a_nonprogressing_continuation() {
    let paths: Vec<_> = (0..12).map(|n|format!("file_{n}.py")).collect();
    let sources: Vec<_> = paths.iter().map(|path|(path.as_str(), "def target(): pass\n")).collect();
    let snapshot = snapshot(&sources);
    let mut saw_success = false;
    for budget in (2000..=8192).step_by(127) {
        let mut query = CodeQuery { operation: Operation::Catalog, ..Default::default() };
        query.budgets.bytes = budget;
        query.budgets.rows = 3;
        let mut seen = BTreeSet::new();
        for page in 0..32 {
            let result = match execute(&snapshot, &query) { Ok(value)=>value, Err(error)=>{
                assert_eq!(error.code,"budget_exceeded"); break;
            }};
            saw_success = true;
            let size = serde_json::to_vec(&result).unwrap().len();
            assert!(size<=budget && size<=query.budgets.tokens);
            assert_eq!(size,result.cost.serialized_bytes);
            for hit in &result.hits { assert!(seen.insert(hit.id.clone()), "duplicate across pages"); }
            if let Some(next) = result.next_cursor {
                assert!(!result.hits.is_empty(), "continuation without a first row at budget {budget}");
                assert_ne!(Some(&next), query.cursor.as_ref(), "unchanged continuation");
                assert!(page<31, "bounded catalog never completes");
                query.cursor = Some(next);
            } else { assert_eq!(seen.len(),12); break; }
        }
    }
    assert!(saw_success, "fixture must exercise positive pages, not only budget errors");
}

fn has_key(value: &Value, key: &str) -> bool {
    match value {
        Value::Object(object) => object.contains_key(key) || object.values().any(|value| has_key(value, key)),
        Value::Array(values) => values.iter().any(|value| has_key(value, key)),
        _ => false,
    }
}

#[test]
fn resolution_projection_omits_nested_source_and_witnesses_not_requested() {
    let snapshot = snapshot(&[("source.py", "def target(): pass\ndef caller():\n    target()\n")]);
    let query = CodeQuery { operation: Operation::Search, query: Some("target".into()),
        filter: QueryFilter { kinds: vec!["occurrence".into()], states: vec!["exact".into()], ..Default::default() },
        fields: vec![Field::Resolution], ..Default::default() };
    let result = execute(&snapshot, &query).unwrap();
    assert!(!result.hits.is_empty());
    for hit in &result.hits {
        let wire = serde_json::to_value(hit).unwrap();
        assert!(wire.get("resolution").is_some(), "requested resolution should remain");
        assert!(!has_key(&wire, "source"), "unrequested source leaked inside resolution");
        assert!(!has_key(&wire, "witnesses"), "unrequested witnesses leaked inside resolution");
    }
    let with_witnesses = execute(&snapshot, &CodeQuery {
        fields: vec![Field::Resolution, Field::Witnesses], ..query.clone()
    }).unwrap();
    for hit in &with_witnesses.hits {
        let wire = serde_json::to_value(hit).unwrap();
        assert!(has_key(&wire, "witnesses"));
        assert!(!has_key(&wire, "source"), "witness source must obey source omission");
    }
    let complete = execute(&snapshot, &CodeQuery {
        fields: vec![Field::Resolution, Field::Witnesses, Field::Source], ..query
    }).unwrap();
    assert!(complete.hits.iter().any(|hit| has_key(&serde_json::to_value(hit).unwrap(), "source")));
}

#[test]
fn continuation_binds_private_owner_receipt_filters_budgets_and_process_secret() {
    let mut snapshot = snapshot(&[("a.py", "def alpha(): pass\n"), ("b.py", "def beta(): pass\n")]);
    let mut query = CodeQuery { operation: Operation::Catalog, ..Default::default() };
    query.budgets.rows = 1;
    query.cursor = execute(&snapshot, &query).unwrap().next_cursor;
    assert!(query.cursor.is_some());
    assert!(execute(&snapshot, &query).is_ok(), "positive continuation control");
    query.expected_receipt = Some(snapshot.receipt.clone());
    assert!(execute(&snapshot, &query).is_ok(), "Graph adds the returned receipt on Load more");
    let mut wrong_receipt = query.clone();
    wrong_receipt.expected_receipt.as_mut().unwrap().evidence.admission_epoch += 1;
    assert_eq!(execute(&snapshot, &wrong_receipt).unwrap_err().code, "stale_cursor");
    let original_scope = snapshot.scope_key.clone();
    snapshot.scope_key = "another-owner:same-public-receipt".into();
    assert_eq!(execute(&snapshot, &query).unwrap_err().code, "stale_cursor");
    snapshot.scope_key = original_scope;
    snapshot.receipt.evidence.admission_epoch += 1;
    assert_eq!(execute(&snapshot, &query).unwrap_err().code, "stale_cursor");
    snapshot.receipt.evidence.admission_epoch -= 1;
    let mut changed = query.clone();
    changed.filter.path_prefix = Some("a".into());
    assert_eq!(execute(&snapshot, &changed).unwrap_err().code, "stale_cursor");
    changed = query.clone();
    changed.budgets.rows = 2;
    assert_eq!(execute(&snapshot, &changed).unwrap_err().code, "stale_cursor");
    changed = query.clone();
    changed.cursor.as_mut().unwrap().push('0');
    assert_eq!(execute(&snapshot, &changed).unwrap_err().code, "stale_cursor");
    assert_eq!(run_query(&snapshot, &query, &AtomicBool::new(false), &[8;32]).unwrap_err().code, "stale_cursor");
}

#[test]
fn cancellation_and_expected_receipt_drift_fail_closed() {
    let snapshot = snapshot(&[("a.py", "def alpha(): pass\n")]);
    let mut query = CodeQuery { operation: Operation::Catalog, ..Default::default() };
    assert_eq!(run_query(&snapshot, &query, &AtomicBool::new(true), &[7;32]).unwrap_err().code, "cancelled");
    let mut expected = snapshot.receipt.clone();
    expected.registration_id = "replaced-registration".into();
    query.expected_receipt = Some(expected);
    assert_eq!(execute(&snapshot, &query).unwrap_err().code, "stale_cursor");
}

#[test]
fn variable_length_public_collections_have_finite_limits() {
    let mut queries = Vec::new();
    let mut query = CodeQuery::default();
    query.channels = vec![Channel::Exact;100_001];
    queries.push(query);
    let mut query = CodeQuery::default();
    query.fields = vec![Field::Label;100_001];
    queries.push(query);
    let mut query = CodeQuery::default();
    query.filter.states = vec!["state".into();100_001];
    queries.push(query);
    let mut query = CodeQuery::default();
    query.filter.languages = vec!["a".repeat(100_001)];
    queries.push(query);
    let query: CodeQuery = serde_json::from_value(json!({"operation":"pipeline","stages":[{
        "operation":"filter","filter":{"kinds":vec!["file";100_001]}
    }]})).unwrap();
    queries.push(query);
    for query in queries { assert!(validate_query(&query).is_err(), "unbounded public collection accepted"); }
}

#[test]
fn semantic_unavailability_does_not_erase_literal_matches_or_empty_diagnostics() {
    let snapshot = snapshot(&[("source.py", "def target(): pass\n")]);
    let query = CodeQuery { query: Some("target".into()), channels: vec![Channel::Exact, Channel::Semantic], ..Default::default() };
    let result = execute(&snapshot, &query).unwrap();
    assert!(!result.hits.is_empty());
    assert!(result.degraded);
    let semantic = result.channels.iter().find(|lane|lane.channel==Channel::Semantic).unwrap();
    assert!(semantic.requested && !semantic.enabled && !semantic.executed);
    assert!(semantic.elapsed_micros.is_none());
    let empty = execute(&snapshot, &CodeQuery { query: Some("no_such_symbol".into()), ..query }).unwrap();
    assert!(empty.hits.is_empty() && empty.degraded);
    assert!(!empty.reasons.is_empty());
}

#[test]
fn metadata_validation_is_charged_to_the_same_requested_work_budget() {
    let mut snapshot = snapshot(&[("source.py", "def target(): pass\n")]);
    let query = CodeQuery { operation: Operation::Catalog, ..Default::default() };
    snapshot.metadata_records = query.budgets.work + 1;
    assert_eq!(execute(&snapshot, &query).unwrap_err().code, "budget_exceeded");
    snapshot.metadata_records = 5;
    let result = execute(&snapshot, &query).unwrap();
    let cost = serde_json::to_value(&result.cost).unwrap();
    assert_eq!(cost["metadata_records_validated"], 5);
    assert!(result.cost.inspected_records >= 5);
}

#[test]
fn optional_composition_is_not_admitted_as_a_core_operation() {
    let snapshot = snapshot(&[("source.py", "def target(): pass\n")]);
    for wire in [json!({"operation":"pipeline"}), json!({"operation":"catalog","stages":[{"operation":"find","query":"target"}]})] {
        let query: CodeQuery = serde_json::from_value(wire).unwrap();
        let error = execute(&snapshot, &query).unwrap_err();
        assert!(matches!(error.code.as_str(), "unsupported" | "not_admitted"));
    }
}
