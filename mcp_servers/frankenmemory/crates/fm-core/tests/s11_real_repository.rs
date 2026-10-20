//! Read-only real copied corpus diagnostic, no source execution or live store.
use fm_core::code_evidence::{extract_evidence, resolve_evidence, CorpusInput, ExtractionBudget, ResolutionBudget};
#[test]
fn query_work_capacity_is_finite_and_omitted_matches_explicit_default() {
    use fm_core::code_query::*;
    let omitted:CodeQuery=serde_json::from_value(serde_json::json!({"operation":"catalog"})).unwrap();
    assert_eq!(omitted.budgets.work,MAX_QUERY_WORK);
    assert_eq!(MAX_QUERY_WORK,4_000_000);
    let explicit:CodeQuery=serde_json::from_value(serde_json::json!({"operation":"catalog","budgets":{"work":MAX_QUERY_WORK}})).unwrap();
    assert_eq!(omitted,explicit);
    let mut too_big=omitted.clone();too_big.budgets.work=MAX_QUERY_WORK+1;
    assert!(validate_query(&too_big).is_err());
    let mut tiny=omitted;tiny.budgets.work=1;
    assert!(validate_query(&tiny).is_ok());
}
#[test]
#[ignore = "requires manifest-pinned disposable S11 source corpus"]
fn copied_first_party_corpus_producer_diagnostic() {
    let root = std::path::PathBuf::from(std::env::var("OPEN_CLANK_S11_COPIED_CORPUS").expect("explicit disposable copied corpus required"));
    assert!(root.starts_with("/tmp") || root.starts_with("/private/tmp"));
    assert!(!root.join(".git").exists() && !root.join(".clanker").exists());
    fn visit(path: &std::path::Path, paths: &mut Vec<std::path::PathBuf>) {
        for entry in std::fs::read_dir(path).unwrap() {
            let entry=entry.unwrap(); let metadata=entry.file_type().unwrap();
            assert!(!metadata.is_symlink());
            if metadata.is_dir(){visit(&entry.path(),paths)} else {paths.push(entry.path())}
        }
    }
    let mut paths=vec![];visit(&root,&mut paths);paths.sort();assert_eq!(paths.len(),35);
    let mut files=vec![];let mut failures=vec![];
    for path in paths {
        let relative=path.strip_prefix(&root).unwrap().to_str().unwrap();
        assert!(relative.ends_with(".py") || relative.ends_with(".rs"));
        let bytes=std::fs::read(&path).unwrap();
        match extract_evidence("checkout_s11_diagnostic",relative,&bytes,&ExtractionBudget::default()) {
            Ok(file)=>{eprintln!("S11_EXTRACT {relative} bytes={} evidence_bytes={} declarations={} occurrences={} bindings={}",bytes.len(),serde_json::to_vec(&file).unwrap().len(),file.declarations.len(),file.occurrences.len(),file.bindings.len());files.push(file)}
            Err(error)=>{eprintln!("S11_EXTRACT_ERROR {relative} {error:?}");failures.push(relative.to_owned())}
        }
    }
    let input=CorpusInput{checkout_id:"checkout_s11_diagnostic".into(),files,unavailable_files:vec![]};
    let mut budget=ResolutionBudget::default();
    if std::env::var("OPEN_CLANK_S11_MEASURE_LARGER_OUTPUT").as_deref()==Ok("1") { budget.max_output_bytes=256*1024*1024; }
    let result=resolve_evidence(&input,&budget);
    match &result { Ok(value)=>{
        let mut totals=std::collections::BTreeMap::<String,usize>::new();
        for resolution in &value.resolutions { for (key,value) in serde_json::to_value(resolution).unwrap().as_object().unwrap() { *totals.entry(key.clone()).or_default()+=serde_json::to_vec(value).unwrap().len(); } }
        eprintln!("S11_RESOLVE_OK resolutions={} bytes={} fields={totals:?}",value.resolutions.len(),serde_json::to_vec(value).unwrap().len());
    }, Err(error)=>eprintln!("S11_RESOLVE_ERROR {error:?}") }
    assert!(failures.is_empty() && result.is_ok(),"per-file errors: {failures:?}; corpus error: {:?}",result.as_ref().err());
}
