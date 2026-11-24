use super::*;

fn original(db: &Connection) -> BTreeMap<(i64, i64, u8, u8), u64> {
    let mut result = BTreeMap::new();
    let mut statement = db.prepare("SELECT e.file,t.file,k.kind,CASE e.state WHEN 'exact' THEN 1 WHEN 'inferred' THEN 2 END FROM edges e JOIN nodes o ON o.id='occ:'||e.occurrence AND o.file=e.file AND o.kind='occurrence' JOIN occurrence_kinds k ON k.occurrence=o.row JOIN nodes t ON t.id='decl:'||e.target WHERE k.kind IN(1,2) AND e.state IN('exact','inferred')").unwrap();
    for row in statement
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
        .unwrap()
    {
        *result.entry(row.unwrap()).or_default() += 1;
    }
    result
}

fn compact(db: &Connection) -> BTreeMap<(i64, i64, u8, u8), u64> {
    db.prepare("SELECT src,dst,kind,state,count FROM visual_links ORDER BY src,dst,kind,state")
        .unwrap()
        .query_map([], |r| {
            Ok(((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?), r.get(4)?))
        })
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
}

fn metadata(db: &Connection) -> VisualSummaryMetadata {
    serde_json::from_str(
        &db.query_row::<String, _, _>(
            "SELECT value FROM meta WHERE key='visual_summary'",
            [],
            |r| r.get(0),
        )
        .unwrap(),
    )
    .unwrap()
}

#[test]
fn compact_typed_summary_matches_original_mixed_membership_and_multiplicity() {
    let fixture = Fixture::new();
    let mut generation = fixture.stage(71);
    let target: String = generation
        .db
        .query_row(
            "SELECT substr(id,6) FROM nodes WHERE kind='declaration' LIMIT 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    generation.db.execute_batch("DELETE FROM edges; DELETE FROM occurrence_kinds; DELETE FROM nodes WHERE kind='occurrence'").unwrap();
    let mut ordinal = 0;
    for kind in 1..=4 {
        for state in ["exact", "inferred", "unresolved", "ambiguous"] {
            for value in [None, Some(target.as_str()), Some("missing-target")] {
                for _ in 0..3 {
                    let id = format!("{ordinal:064x}");
                    ordinal += 1;
                    generation
                        .db
                        .execute(
                            "INSERT INTO nodes(id,file,kind,ordinal) VALUES(?1,1,'occurrence',0)",
                            params![format!("occ:{id}")],
                        )
                        .unwrap();
                    let row = generation.db.last_insert_rowid();
                    generation
                        .db
                        .execute(
                            "INSERT INTO occurrence_kinds VALUES(?1,?2)",
                            params![row, kind],
                        )
                        .unwrap();
                    generation
                        .db
                        .execute(
                            "INSERT INTO edges VALUES(?1,NULL,?2,?3,1,0)",
                            params![id, value, state],
                        )
                        .unwrap();
                }
            }
        }
    }
    let expected = original(&generation.db);
    generation.build_visual_summary(&mut || Ok(())).unwrap();
    assert_eq!(compact(&generation.db), expected);
    let m = metadata(&generation.db);
    assert_eq!(m.version, 1);
    assert_eq!(m.status, "complete");
    assert!(m.partial);
    assert_eq!(m.resolved_rows, expected.values().sum::<u64>());
    assert_eq!(m.links, expected.len());
    assert_eq!(m.files, 4);
    assert_eq!(
        m.resolved_rows, 12,
        "value/type, missing targets and unresolved rows never become resolved links"
    );
}

#[test]
fn compact_summary_cancellation_rolls_back_and_cannot_publish() {
    let fixture = Fixture::new();
    let mut generation = fixture.stage(72);
    let before: String = generation
        .db
        .query_row(
            "SELECT value FROM meta WHERE key='visual_summary'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let links = compact(&generation.db);
    let mut calls = 0;
    let error = generation
        .build_visual_summary(&mut || {
            calls += 1;
            if calls >= 3 {
                Err("cancelled: summary fixture".into())
            } else {
                Ok(())
            }
        })
        .unwrap_err();
    assert!(error.starts_with("cancelled"));
    assert!(generation.db.is_autocommit());
    assert_eq!(
        before,
        generation
            .db
            .query_row::<String, _, _>(
                "SELECT value FROM meta WHERE key='visual_summary'",
                [],
                |r| r.get(0)
            )
            .unwrap()
    );
    assert_eq!(links, compact(&generation.db));
    assert_eq!(
        fixture
            .main
            .query_row::<i64, _, _>("SELECT count(*) FROM code_index_heads", [], |r| r.get(0))
            .unwrap(),
        0
    );
    generation.build_visual_summary(&mut || Ok(())).unwrap();
}

#[test]
fn compact_summary_vm_budget_is_map_only_and_does_not_swallow_cancellation() {
    let fixture = Fixture::new();
    let mut generation = fixture.stage(74);
    generation
        .build_visual_summary_with_budget(&mut || Ok(()), 0)
        .unwrap();
    let m = metadata(&generation.db);
    assert_eq!(m.status, "budget_exceeded");
    assert_eq!(m.files, 0);
    assert_eq!(m.links, 0);
    assert!(generation.db.is_autocommit());
    let mut hash = visual_summary_hash();
    visual_finish_hash(&mut hash, "budget_exceeded", true);
    assert_eq!(hash.finalize().to_hex().as_str(), m.digest);
    let error = generation
        .build_visual_summary_with_budget(&mut || Err("denied: revoked fixture".into()), 0)
        .unwrap_err();
    assert!(error.starts_with("denied"));
    generation.build_visual_summary(&mut || Ok(())).unwrap();
    assert_eq!(metadata(&generation.db).status, "complete");
}

#[test]
fn compact_file_capacity_is_map_only_and_has_empty_digested_records() {
    let fixture = Fixture::new();
    let mut generation = fixture.stage(75);
    generation.db.execute_batch("BEGIN").unwrap();
    for file in 5..=10_001i64 {
        generation.db.execute("INSERT INTO files(path,hash,source_bytes,mtime,symbols,header,interface,payload,payload_hash,graph,resolutions,resolution_hash,node_count) SELECT ?1,hash,source_bytes,mtime,symbols,header,interface,payload,payload_hash,graph,resolutions,resolution_hash,node_count FROM files WHERE id=1",params![format!("synthetic{file}.py")]).unwrap();
        generation
            .db
            .execute(
                "INSERT INTO nodes(id,file,kind,ordinal) VALUES(?1,?2,'file',0)",
                params![format!("file:{file:064x}"), file],
            )
            .unwrap();
    }
    generation.db.execute_batch("COMMIT").unwrap();
    generation.build_visual_summary(&mut || Ok(())).unwrap();
    let m = metadata(&generation.db);
    assert_eq!(m.status, "budget_exceeded");
    assert_eq!(m.files, 0);
    assert_eq!(m.links, 0);
    assert_eq!(
        generation
            .db
            .query_row::<i64, _, _>("SELECT count(*) FROM visual_files", [], |r| r.get(0))
            .unwrap(),
        0
    );
    assert_eq!(
        generation
            .db
            .query_row::<i64, _, _>("SELECT count(*) FROM files", [], |r| r.get(0))
            .unwrap(),
        10_001,
        "summary limit does not erase the index"
    );
}

#[test]
fn compact_summary_capacity_is_explicit_not_a_successful_prefix() {
    let fixture = Fixture::new();
    let mut generation = fixture.stage(73);
    generation.db.execute_batch("BEGIN; DELETE FROM edges; DELETE FROM occurrence_kinds; DELETE FROM nodes WHERE kind='occurrence';").unwrap();
    for file in 5..=257 {
        generation.db.execute("INSERT INTO files(path,hash,source_bytes,mtime,symbols,header,interface,payload,payload_hash,graph,resolutions,resolution_hash,node_count) SELECT ?1,hash,source_bytes,mtime,symbols,header,interface,payload,payload_hash,graph,resolutions,resolution_hash,node_count FROM files WHERE id=1",params![format!("synthetic{file}.py")]).unwrap();
        assert_eq!(generation.db.last_insert_rowid(), file);
        generation
            .db
            .execute(
                "INSERT INTO nodes(id,file,kind,ordinal) VALUES(?1,?2,'file',0)",
                params![format!("file:{file:064x}"), file],
            )
            .unwrap();
    }
    for file in 1..=257 {
        generation
            .db
            .execute(
                "INSERT INTO nodes(id,file,kind,ordinal) VALUES(?1,?2,'declaration',0)",
                params![format!("decl:capacity-{file}"), file],
            )
            .unwrap();
    }
    for n in 0..=VISUAL_LINKS {
        let src = (n / 257 + 1) as i64;
        let dst = (n % 257 + 1) as i64;
        generation
            .db
            .execute(
                "INSERT INTO nodes(id,file,kind,ordinal) VALUES(?1,?2,'occurrence',0)",
                params![format!("occ:capacity-{n}"), src],
            )
            .unwrap();
        let row = generation.db.last_insert_rowid();
        generation
            .db
            .execute("INSERT INTO occurrence_kinds VALUES(?1,1)", params![row])
            .unwrap();
        generation
            .db
            .execute(
                "INSERT INTO edges VALUES(?1,NULL,?2,'exact',?3,0)",
                params![format!("capacity-{n}"), format!("capacity-{dst}"), src],
            )
            .unwrap();
    }
    generation.db.execute_batch("COMMIT").unwrap();
    generation.build_visual_summary(&mut || Ok(())).unwrap();
    let m = metadata(&generation.db);
    assert_eq!(m.status, "budget_exceeded");
    assert_eq!(m.links, 0);
    assert_eq!(m.resolved_rows, 0);
    assert!(compact(&generation.db).is_empty());
    assert_eq!(m.files, 257);
}
