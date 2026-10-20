//! Root-owned independent S04 contract checks. Source-only synthetic fixtures;
//! no filesystem, authority, model, language-server or live-store access.

use fm_core::code_evidence::{
    extract_evidence, language_capability, resolve_evidence, validate_source_evidence, CorpusInput,
    EvidenceErrorCode, ExtractionBudget, FileEvidence, IdentityState, OccurrenceKind,
    ResolutionBudget, ResolutionState, SourceClaim, SourcePredicate, ValidationState,
};

fn extract(path: &str, source: &str) -> FileEvidence {
    extract_evidence(
        "checkout-root-contract",
        path,
        source.as_bytes(),
        &ExtractionBudget::default(),
    )
    .expect("supported valid synthetic source")
}

fn corpus(file: FileEvidence) -> CorpusInput {
    CorpusInput {
        checkout_id: "checkout-root-contract".into(),
        files: vec![file],
        unavailable_files: vec![],
    }
}

#[test]
fn repeated_lexical_calls_have_separate_source_witnesses() {
    let file = extract(
        "source.py",
        "def target():\n    return 1\n\ndef caller():\n    target()\n    target()\n",
    );
    let target = file
        .declarations
        .iter()
        .find(|d| d.name == "target")
        .unwrap();
    let calls: Vec<_> = file
        .occurrences
        .iter()
        .filter(|o| o.kind == OccurrenceKind::Call)
        .collect();
    assert_eq!(calls.len(), 2);
    assert_ne!(calls[0].id, calls[1].id);
    assert_ne!(
        calls[0].source.span.start_byte,
        calls[1].source.span.start_byte
    );
    let resolved = resolve_evidence(&corpus(file.clone()), &ResolutionBudget::default()).unwrap();
    for call in calls {
        let binding = resolved
            .resolutions
            .iter()
            .find(|r| r.occurrence_id == call.id)
            .unwrap();
        assert_eq!(binding.state, ResolutionState::Exact);
        assert_eq!(binding.target_id.as_deref(), Some(target.id.as_str()));
        assert!(!binding.witnesses.is_empty());
    }
}

#[test]
fn python_parameters_and_later_local_assignment_shadow_module_function() {
    for source in [
        "def target():\n    return 1\n\ndef caller(target):\n    target()\n",
        "def target():\n    return 1\n\ndef caller():\n    target()\n    target = lambda: 2\n",
    ] {
        let file = extract("shadow.py", source);
        let module_target = file
            .declarations
            .iter()
            .find(|d| d.name == "target")
            .unwrap()
            .id
            .clone();
        let call = file
            .occurrences
            .iter()
            .find(|o| o.kind == OccurrenceKind::Call && o.symbol_text == "target")
            .unwrap()
            .id
            .clone();
        let resolved = resolve_evidence(&corpus(file), &ResolutionBudget::default()).unwrap();
        let binding = resolved
            .resolutions
            .iter()
            .find(|r| r.occurrence_id == call)
            .unwrap();
        assert_ne!(
            binding.state,
            ResolutionState::Exact,
            "A parameter or local callable needs runtime value evidence"
        );
        assert_ne!(binding.target_id.as_deref(), Some(module_target.as_str()));
    }
}

#[test]
fn receiver_and_missing_import_do_not_bind_by_bare_name() {
    for (source, callee) in [
        ("def target():\n    return 1\n\ndef caller(obj):\n    obj.target()\n", "obj.target"),
        ("from absent import target as action\n\ndef target():\n    return 1\n\ndef caller():\n    action()\n", "action"),
    ] {
        let file = extract("unknown.py", source);
        let call = file.occurrences.iter().find(|o| o.kind == OccurrenceKind::Call && o.symbol_text == callee).unwrap().id.clone();
        let resolved = resolve_evidence(&corpus(file), &ResolutionBudget::default()).unwrap();
        let binding = resolved.resolutions.iter().find(|r| r.occurrence_id == call).unwrap();
        assert_ne!(binding.state, ResolutionState::Exact);
        assert!(binding.target_id.is_none(), "An unresolved receiver/import must not acquire an unrelated target");
    }
}

#[test]
fn nested_same_names_keep_qualified_identity_after_line_only_edits() {
    let source = "def first():\n    def shared():\n        return 1\n    return shared()\n\ndef second():\n    def shared():\n        return 2\n    return shared()\n";
    let before = extract("nested.py", source);
    let after = extract("nested.py", &format!("# a comment\n\n{source}"));
    let identity = |file: &FileEvidence| {
        let mut values: Vec<_> = file
            .declarations
            .iter()
            .map(|d| {
                (
                    d.qualified_name.clone(),
                    d.id.clone(),
                    d.signature_hash.clone(),
                )
            })
            .collect();
        values.sort();
        values
    };
    assert_eq!(identity(&before), identity(&after));
    let nested: Vec<_> = before
        .declarations
        .iter()
        .filter(|d| d.name == "shared")
        .collect();
    assert_eq!(nested.len(), 2);
    assert_ne!(nested[0].id, nested[1].id);
    assert_ne!(nested[0].lexical_parent_id, nested[1].lexical_parent_id);
    assert!(nested
        .iter()
        .all(|d| d.identity_state == IdentityState::Stable));
}

#[test]
fn signature_normalization_preserves_literal_content() {
    let before = extract(
        "defaults.py",
        "def target(value='a b'):\n    return value\n",
    );
    let changed = extract("defaults.py", "def target(value='ab'):\n    return value\n");
    assert_ne!(
        before.declarations[0].signature_hash,
        changed.declarations[0].signature_hash
    );
    assert_ne!(before.declarations[0].id, changed.declarations[0].id);
    let body_only = extract(
        "defaults.py",
        "def target(value='a b'):\n    return (value, 2)\n",
    );
    assert_eq!(before.declarations[0].id, body_only.declarations[0].id);
    assert_ne!(
        before.declarations[0].content_hash,
        body_only.declarations[0].content_hash
    );
}

#[test]
fn indistinguishable_duplicate_declarations_are_snapshot_local() {
    let file = extract(
        "duplicate.py",
        "def target():\n    return 1\n\ndef target():\n    return 2\n",
    );
    assert_eq!(file.declarations.len(), 2);
    assert_ne!(file.declarations[0].id, file.declarations[1].id);
    assert!(file
        .declarations
        .iter()
        .all(|d| d.identity_state == IdentityState::AmbiguousSnapshotLocal));
}

#[test]
fn unsupported_javascript_is_not_claimed_as_typescript_capability() {
    for path in ["source.js", "source.jsx"] {
        assert!(!language_capability(path).available);
        let error = extract_evidence(
            "checkout-root-contract",
            path,
            b"export function f() {}",
            &ExtractionBudget::default(),
        )
        .unwrap_err();
        assert_eq!(error.code, EvidenceErrorCode::UnsupportedLanguage);
    }
}

#[test]
fn utf8_span_predicates_validate_content_not_only_identity() {
    let source = "# snow: 雪\ndef café():\n    return '☕'\n";
    let file = extract("unicode.py", source);
    let declaration = file.declarations.iter().find(|d| d.name == "café").unwrap();
    let locator = declaration.source.clone();
    let span = &locator.span;
    let text = source
        .get(span.start_byte as usize..span.end_byte as usize)
        .unwrap();
    let claim = SourceClaim {
        source: locator.clone(),
        predicate: SourcePredicate::ExactText { text: text.into() },
    };
    assert_eq!(
        validate_source_evidence(
            &file,
            source.as_bytes(),
            &claim,
            &ExtractionBudget::default()
        )
        .unwrap()
        .state,
        ValidationState::Current
    );
    let false_claim = SourceClaim {
        source: locator.clone(),
        predicate: SourcePredicate::ExactText {
            text: "def café(): pass".into(),
        },
    };
    assert_ne!(
        validate_source_evidence(
            &file,
            source.as_bytes(),
            &false_claim,
            &ExtractionBudget::default()
        )
        .unwrap()
        .state,
        ValidationState::Current
    );
    let changed = source.replace("☕", "茶");
    assert_eq!(
        validate_source_evidence(
            &file,
            changed.as_bytes(),
            &claim,
            &ExtractionBudget::default()
        )
        .unwrap()
        .state,
        ValidationState::Stale
    );
    let mut wrong_identity = claim;
    wrong_identity.source.file_id = "other-file".into();
    assert_ne!(
        validate_source_evidence(
            &file,
            source.as_bytes(),
            &wrong_identity,
            &ExtractionBudget::default()
        )
        .unwrap()
        .state,
        ValidationState::Current
    );
}

#[test]
fn extraction_budget_failure_does_not_return_truncated_exact_evidence() {
    let budget = ExtractionBudget {
        max_occurrences: 1,
        ..ExtractionBudget::default()
    };
    let result = extract_evidence(
        "checkout-root-contract",
        "capped.py",
        b"def target(): pass\ntarget()\ntarget()\n",
        &budget,
    );
    let error = result.expect_err("Budget overflow must not look like a complete one-call file");
    assert_eq!(error.code, EvidenceErrorCode::BudgetExceeded);
    assert!(error.incomplete);
}

#[test]
fn python_import_cannot_resolve_to_other_language_file() {
    let caller = extract("caller.py", "from dep import target\ntarget()\n");
    let target = extract("dep.ts", "export function target() { return 1; }\n");
    let input = CorpusInput {
        checkout_id: "checkout-root-contract".into(),
        files: vec![caller, target],
        unavailable_files: vec![],
    };
    let resolved = resolve_evidence(&input, &ResolutionBudget::default()).unwrap();
    for binding in resolved
        .resolutions
        .iter()
        .filter(|r| r.source.rel_path == "caller.py")
    {
        assert_ne!(binding.state, ResolutionState::Exact);
        assert!(
            binding.target_id.is_none(),
            "Python imports cannot acquire TypeScript declarations"
        );
    }
}

#[test]
fn imported_reassigned_python_name_is_not_exact_function() {
    let caller = extract("caller.py", "from dep import target\ntarget()\n");
    let target = extract("dep.py", "def target():\n    return 1\ntarget = 42\n");
    let input = CorpusInput {
        checkout_id: "checkout-root-contract".into(),
        files: vec![caller, target],
        unavailable_files: vec![],
    };
    let resolved = resolve_evidence(&input, &ResolutionBudget::default()).unwrap();
    assert!(resolved
        .resolutions
        .iter()
        .filter(|r| r.source.rel_path == "caller.py")
        .all(|r| r.state != ResolutionState::Exact));
}

#[test]
fn lambda_parameter_does_not_resolve_to_module_name() {
    let file = extract(
        "lambda.py",
        "def target():\n    return 1\nwrapper = lambda target: target()\n",
    );
    let call = file
        .occurrences
        .iter()
        .find(|o| o.kind == OccurrenceKind::Call)
        .unwrap()
        .id
        .clone();
    let result = resolve_evidence(&corpus(file), &ResolutionBudget::default()).unwrap();
    assert_ne!(
        result
            .resolutions
            .iter()
            .find(|r| r.occurrence_id == call)
            .unwrap()
            .state,
        ResolutionState::Exact
    );
}

#[test]
fn rust_macro_name_is_not_a_function_call_binding() {
    let file = extract("macro.rs", "fn target() {}\nfn caller() { target!(); }\n");
    let call = file
        .occurrences
        .iter()
        .find(|o| o.kind == OccurrenceKind::Call)
        .unwrap()
        .id
        .clone();
    let result = resolve_evidence(&corpus(file), &ResolutionBudget::default()).unwrap();
    assert_ne!(
        result
            .resolutions
            .iter()
            .find(|r| r.occurrence_id == call)
            .unwrap()
            .state,
        ResolutionState::Exact
    );
}

#[test]
fn arrow_signature_tracks_parameters_and_not_body() {
    let before = extract(
        "arrow.ts",
        "const target = (value: string): string => value;\n",
    );
    let changed = extract(
        "arrow.ts",
        "const target = (value: number): number => value;\n",
    );
    let body = extract(
        "arrow.ts",
        "const target = (value: string): string => value + '!';\n",
    );
    assert_ne!(
        before.declarations[0].signature_hash,
        changed.declarations[0].signature_hash
    );
    assert_eq!(
        before.declarations[0].signature_hash,
        body.declarations[0].signature_hash
    );
}
