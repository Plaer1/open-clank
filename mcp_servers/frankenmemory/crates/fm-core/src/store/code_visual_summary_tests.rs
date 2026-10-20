use super::*;

fn staging() -> (Fixture, SqliteStore, Generation, Vec<ScanCoverage>) {
    let f = Fixture::new(false);
    let store = f.store();
    let ticket = f.prepare(&store);
    let connection = store.conn.lock().unwrap();
    let mut generation = Generation::create(
        &connection,
        &f.scope,
        &ticket.checkout_id,
        "codegen_0000000000000000000000000000007f",
        "policy",
    )
    .unwrap();
    drop(connection);
    let sources = [
        ("a.py", "def target(): return 1\n"),
        (
            "b.py",
            "from a import target\ndef caller():\n    target()\n    target()\n",
        ),
    ];
    let mut coverage = Vec::new();
    for (path, text) in sources {
        let file = crate::code_evidence::extract_evidence(
            &ticket.checkout_id,
            path,
            text.as_bytes(),
            &crate::code_evidence::ExtractionBudget::default(),
        )
        .unwrap();
        generation
            .stage(
                &file,
                0,
                &crate::graph::GraphUpsertInput {
                    nodes: vec![],
                    edges: vec![],
                    cues: vec![],
                },
                &f.scope,
            )
            .unwrap();
        coverage.push(ScanCoverage {
            rel_path: path.into(),
            status: "indexed".into(),
            ..Default::default()
        });
    }
    generation
        .resolve(&ticket.checkout_id, &[], || Ok(()))
        .unwrap();
    (f, store, generation, coverage)
}

fn read(generation: &Generation, coverage: &[ScanCoverage]) -> Result<Summary, QueryError> {
    let cancel = AtomicBool::new(false);
    let mut work = Work::new(MAX_WORK, &cancel);
    generation.map_summary(coverage, &mut work)
}

#[test]
fn compact_reader_does_not_need_rich_node_edge_or_kind_indexes() {
    let (_f, _store, generation, coverage) = staging();
    let expected = read(&generation, &coverage).unwrap();
    assert!(!expected.links.is_empty());
    generation
        .db
        .execute_batch("DROP TABLE edges; DROP TABLE occurrence_kinds; DROP TABLE nodes;")
        .unwrap();
    let actual = read(&generation, &coverage).unwrap();
    assert!(actual.links == expected.links);
    assert_eq!(actual.nodes.len(), expected.nodes.len());
}

#[test]
fn compact_reader_rejects_old_missing_and_corrupt_capability_counts_digest_and_references() {
    let (_f, _store, generation, coverage) = staging();
    let raw: String = generation
        .db
        .query_row(
            "SELECT value FROM meta WHERE key='visual_summary'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    for field in [
        "version",
        "files",
        "links",
        "resolved_rows",
        "digest",
        "status",
    ] {
        let mut value: serde_json::Value = serde_json::from_str(&raw).unwrap();
        value[field] = match field {
            "version" => 2.into(),
            "files" | "links" | "resolved_rows" => 9999999.into(),
            "digest" => "0".repeat(64).into(),
            _ => "unknown".into(),
        };
        generation
            .db
            .execute(
                "UPDATE meta SET value=?1 WHERE key='visual_summary'",
                params![value.to_string()],
            )
            .unwrap();
        assert!(read(&generation, &coverage).is_err(), "{field}");
    }
    generation
        .db
        .execute(
            "UPDATE meta SET value=?1 WHERE key='visual_summary'",
            params![raw],
        )
        .unwrap();
    generation
        .db
        .execute_batch("UPDATE visual_links SET dst=999999;")
        .unwrap();
    assert!(read(&generation, &coverage).is_err());
    generation
        .db
        .execute("DELETE FROM meta WHERE key='visual_summary'", [])
        .unwrap();
    assert_eq!(
        read(&generation, &coverage).err().unwrap().code,
        "reindex_required"
    );
}

#[test]
fn compact_reader_counts_still_pay_original_per_occurrence_work_and_validate_overflow_digest() {
    use super::super::super::code_partitioned::{
        visual_finish_hash, visual_summary_hash, VisualSummaryMetadata,
    };
    let (_f, _store, generation, coverage) = staging();
    let rows: u64 = generation
        .db
        .query_row("SELECT sum(count) FROM visual_links", [], |r| r.get(0))
        .unwrap();
    assert!(rows >= 3);
    let cancel = AtomicBool::new(false);
    let mut links = BTreeMap::new();
    let mut work = Work::new((rows - 1) as usize, &cancel);
    assert_eq!(
        append_link_count(&mut links, 0, 1, "call", "exact", rows, &mut work)
            .unwrap_err()
            .code,
        "budget_exceeded"
    );
    assert!(links.is_empty());
    generation
        .db
        .execute_batch("DELETE FROM visual_files; DELETE FROM visual_links;")
        .unwrap();
    let mut hash = visual_summary_hash();
    visual_finish_hash(&mut hash, "budget_exceeded", true);
    let mut metadata = VisualSummaryMetadata {
        version: 1,
        status: "budget_exceeded".into(),
        files: 0,
        links: 0,
        resolved_rows: 0,
        partial: true,
        digest: hash.finalize().to_hex().to_string(),
    };
    generation
        .db
        .execute(
            "UPDATE meta SET value=?1 WHERE key='visual_summary'",
            params![serde_json::to_string(&metadata).unwrap()],
        )
        .unwrap();
    assert_eq!(
        read(&generation, &coverage).err().unwrap().code,
        "budget_exceeded"
    );
    metadata.digest = "0".repeat(64);
    generation
        .db
        .execute(
            "UPDATE meta SET value=?1 WHERE key='visual_summary'",
            params![serde_json::to_string(&metadata).unwrap()],
        )
        .unwrap();
    assert_eq!(
        read(&generation, &coverage).err().unwrap().code,
        "reindex_required"
    );
}

#[test]
fn compact_real_partition_matches_original_file_pair_counts_before_first_map() {
    let f = Fixture::new(true);
    let store = f.store();
    f.index(&store);
    let context = store.map_context(&f.scope, &f.binding).unwrap();
    let connection = store.conn.lock().unwrap();
    let generation = Generation::open(
        &connection,
        &f.scope,
        &context.receipt.as_ref().unwrap().evidence,
    )
    .unwrap();
    drop(connection);
    let summary = read(&generation, &context.coverage).unwrap();
    assert!(
        summary
            .nodes
            .iter()
            .filter(|n| n.kind == NodeKind::File)
            .count()
            > 128
    );
    let mut expected = BTreeMap::new();
    let mut statement=generation.db.prepare("SELECT f.path,u.path,k.kind,CASE e.state WHEN 'exact' THEN 1 WHEN 'inferred' THEN 2 END FROM edges e JOIN nodes o ON o.id='occ:'||e.occurrence AND o.file=e.file AND o.kind='occurrence' JOIN occurrence_kinds k ON k.occurrence=o.row JOIN nodes t ON t.id='decl:'||e.target JOIN files f ON f.id=e.file JOIN files u ON u.id=t.file WHERE k.kind IN(1,2) AND e.state IN('exact','inferred')").unwrap();
    for row in statement
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, u8>(2)?,
                r.get::<_, u8>(3)?,
            ))
        })
        .unwrap()
    {
        *expected.entry(row.unwrap()).or_insert(0u64) += 1;
    }
    let actual: BTreeMap<_, _> = summary
        .links
        .iter()
        .map(|l| {
            (
                (
                    summary.nodes[l.from].path.clone(),
                    summary.nodes[l.to].path.clone(),
                    l.kind,
                    l.state,
                ),
                l.count,
            )
        })
        .collect();
    assert_eq!(actual, expected);
}
