//! Graph overlay operations on SqliteStore (design §2.2). Single-step,
//! deterministic, LLM-free: the reconstruction loop lives in the CLIENT
//! model via MCP tool-calling; these are the lookups it walks.

use chrono::Utc;
use rusqlite::{params, Connection, OptionalExtension};
use std::collections::{HashMap, HashSet, VecDeque};

use crate::cue::rake_cues;
use crate::graph::*;
use crate::store::sqlite::SqliteStore;

fn row_to_node(row: &rusqlite::Row) -> rusqlite::Result<GraphNodeRow> {
    Ok(GraphNodeRow {
        id: row.get(0)?,
        kind: row.get(1)?,
        label: row.get(2)?,
        name: row.get(3)?,
        layer: row.get(4)?,
        trust: row.get(5)?,
        created_at: row.get(6)?,
        last_seen: row.get(7)?,
        owner: row.get(8)?,
        workspace_id: row.get(9)?,
    })
}

const NODE_COLS: &str =
    "id, kind, label, name, layer, trust, created_at, last_seen, owner, workspace_id";

#[cfg(test)]
fn test_scope() -> GraphScope {
    GraphScope::new("test-owner", "test-workspace").unwrap()
}

fn upsert_node_inner(
    conn: &Connection,
    scope: &GraphScope,
    kind: &str,
    name: &str,
    label: Option<&str>,
    layer: &str,
    trust: i64,
    now: &str,
) -> rusqlite::Result<String> {
    let id = scope.node_id(kind, name);
    conn.execute(
        "INSERT INTO graph_nodes
         (id, kind, label, name, norm_name, layer, trust, created_at, last_seen, owner, workspace_id, status)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8, ?9, ?10, 'active')
         ON CONFLICT(id) DO UPDATE SET
            last_seen = excluded.last_seen,
            label = COALESCE(excluded.label, graph_nodes.label),
            trust = MAX(graph_nodes.trust, excluded.trust),
            status = 'active'",
        params![
            id,
            kind.trim().to_lowercase(),
            label,
            name.trim(),
            norm_name(name),
            layer,
            trust,
            now,
            scope.owner,
            scope.workspace_id,
        ],
    )?;
    Ok(id)
}

const CURATED_PROJECTION_KIND: &str = "memory";
const CURATED_PROJECTION_LAYER: &str = "episodic";
const CURATED_PROJECTION_CATEGORY_KIND: &str = "category";
const CURATED_PROJECTION_CATEGORY_EDGE: &str = "is";
const CURATED_PROJECTION_CUE_SOURCE: &str = "rake_fallback";
const CURATED_PROJECTION_MAX_CUES: usize = 8;

fn curated_projection_label(content: &str) -> String {
    let label = content.split_whitespace().collect::<Vec<_>>().join(" ");
    if label.chars().count() <= 120 {
        return label;
    }
    format!("{}…", label.chars().take(119).collect::<String>())
}

fn curated_projection_category_label(category: &str) -> String {
    format!("{} memories", category.replace('_', " "))
}

fn curated_projection_node_ids(
    conn: &Connection,
    curated_id: &str,
) -> rusqlite::Result<Vec<String>> {
    let mut statement = conn.prepare(
        "SELECT id FROM graph_nodes
         WHERE ref_table = 'curated' AND ref_id = ?1
           AND kind = ?2 AND layer = ?3 AND name = ?1",
    )?;
    let rows = statement
        .query_map(
            params![
                curated_id,
                CURATED_PROJECTION_KIND,
                CURATED_PROJECTION_LAYER
            ],
            |row| row.get(0),
        )?
        .collect();
    rows
}

fn curated_projection_category_ids(
    conn: &Connection,
    memory_node_id: &str,
) -> rusqlite::Result<Vec<String>> {
    let mut statement = conn.prepare(
        "SELECT DISTINCT n.id
         FROM graph_edges e
         JOIN graph_nodes n ON n.id = e.dst_id
         WHERE e.src_id = ?1 AND e.tag = ?2
           AND e.fact_id IS NULL AND e.trust = 0
           AND n.kind = ?3",
    )?;
    let rows = statement
        .query_map(
            params![
                memory_node_id,
                CURATED_PROJECTION_CATEGORY_EDGE,
                CURATED_PROJECTION_CATEGORY_KIND
            ],
            |row| row.get(0),
        )?
        .collect();
    rows
}

fn prune_curated_projection_categories_inner(
    conn: &Connection,
    category_ids: &[String],
) -> rusqlite::Result<usize> {
    let mut pruned = 0;
    for category_id in category_ids {
        let category = conn
            .query_row(
                "SELECT name, label, owner, workspace_id
                 FROM graph_nodes
                 WHERE id = ?1 AND kind = ?2 AND layer = 'semantic'
                   AND trust = 0 AND ref_table IS NULL AND ref_id IS NULL",
                params![category_id, CURATED_PROJECTION_CATEGORY_KIND],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, Option<String>>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                    ))
                },
            )
            .optional()?;
        let Some((name, label, owner, workspace_id)) = category else {
            continue;
        };
        let Ok(scope) = GraphScope::new(owner, workspace_id) else {
            continue;
        };
        if scope.node_id(CURATED_PROJECTION_CATEGORY_KIND, &name) != *category_id
            || label.as_deref() != Some(curated_projection_category_label(&name).as_str())
        {
            continue;
        }
        pruned += conn.execute(
            "DELETE FROM graph_nodes
             WHERE id = ?1
               AND NOT EXISTS (
                   SELECT 1 FROM graph_edges
                   WHERE src_id = ?1 OR dst_id = ?1
               )
               AND NOT EXISTS (
                   SELECT 1 FROM graph_cues WHERE node_id = ?1
               )",
            params![category_id],
        )?;
    }
    Ok(pruned)
}

fn delete_curated_projection_node_inner(conn: &Connection, node_id: &str) -> rusqlite::Result<()> {
    let category_ids = curated_projection_category_ids(conn, node_id)?;
    let _ = conn.execute(
        "DELETE FROM facts_fts WHERE rowid IN (
             SELECT f.rowid FROM facts f
             JOIN graph_edges e ON e.fact_id = f.id
             WHERE e.src_id = ?1 OR e.dst_id = ?1
         )",
        params![node_id],
    );
    conn.execute(
        "DELETE FROM facts WHERE id IN (
             SELECT fact_id FROM graph_edges
             WHERE (src_id = ?1 OR dst_id = ?1) AND fact_id IS NOT NULL
         )",
        params![node_id],
    )?;
    conn.execute(
        "DELETE FROM graph_edges WHERE src_id = ?1 OR dst_id = ?1",
        params![node_id],
    )?;
    conn.execute(
        "DELETE FROM graph_cues WHERE node_id = ?1",
        params![node_id],
    )?;
    let _ = conn.execute(
        "DELETE FROM graph_cues_fts WHERE node_id = ?1",
        params![node_id],
    );
    conn.execute("DELETE FROM graph_nodes WHERE id = ?1", params![node_id])?;
    prune_curated_projection_categories_inner(conn, &category_ids)?;
    Ok(())
}

pub(super) fn delete_curated_projection_inner(
    conn: &Connection,
    curated_id: &str,
) -> rusqlite::Result<usize> {
    let node_ids = curated_projection_node_ids(conn, curated_id)?;
    for node_id in &node_ids {
        delete_curated_projection_node_inner(conn, node_id)?;
    }
    Ok(node_ids.len())
}

pub(super) fn sync_curated_projection_inner(
    conn: &Connection,
    curated_id: &str,
) -> rusqlite::Result<Option<String>> {
    let row = conn
        .query_row(
            "SELECT content, kind, metadata, owner, workspace_id, updated_at
             FROM curated
             WHERE id = ?1 AND archived = 0
               AND owner IS NOT NULL AND trim(owner) <> ''
               AND trim(workspace_id) <> ''",
            params![curated_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, String>(5)?,
                ))
            },
        )
        .optional()?;
    let Some((content, curated_kind, metadata, owner, workspace_id, updated_at)) = row else {
        delete_curated_projection_inner(conn, curated_id)?;
        return Ok(None);
    };
    let curated_category = serde_json::from_str::<serde_json::Value>(&metadata)
        .ok()
        .and_then(|metadata| {
            metadata
                .get("category")
                .and_then(serde_json::Value::as_str)
                .map(str::trim)
                .filter(|category| !category.is_empty())
                .map(str::to_owned)
        })
        .unwrap_or(curated_kind);

    let scope = GraphScope::new(owner, workspace_id).map_err(|_| rusqlite::Error::InvalidQuery)?;
    let label = curated_projection_label(&content);
    let node_id = upsert_node_inner(
        conn,
        &scope,
        CURATED_PROJECTION_KIND,
        curated_id,
        Some(&label),
        CURATED_PROJECTION_LAYER,
        0,
        &updated_at,
    )?;
    conn.execute(
        "UPDATE graph_nodes
         SET ref_table = 'curated', ref_id = ?2, label = ?3, layer = ?4
         WHERE id = ?1 AND owner = ?5 AND workspace_id = ?6",
        params![
            node_id,
            curated_id,
            label,
            CURATED_PROJECTION_LAYER,
            scope.owner,
            scope.workspace_id
        ],
    )?;
    for old_node_id in curated_projection_node_ids(conn, curated_id)?
        .into_iter()
        .filter(|old_node_id| old_node_id != &node_id)
    {
        delete_curated_projection_node_inner(conn, &old_node_id)?;
    }

    let old_category_ids = curated_projection_category_ids(conn, &node_id)?;
    let category_label = curated_projection_category_label(&curated_category);
    let category_id = scope.node_id(CURATED_PROJECTION_CATEGORY_KIND, &curated_category);
    let category_exists = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM graph_nodes WHERE id = ?1)",
        params![category_id],
        |row| row.get::<_, bool>(0),
    )?;
    let category_id = upsert_node_inner(
        conn,
        &scope,
        CURATED_PROJECTION_CATEGORY_KIND,
        &curated_category,
        (!category_exists).then_some(category_label.as_str()),
        "semantic",
        0,
        &updated_at,
    )?;
    conn.execute(
        "DELETE FROM graph_edges
         WHERE src_id = ?1 AND tag = ?2
           AND fact_id IS NULL AND trust = 0
           AND dst_id IN (
               SELECT id FROM graph_nodes
               WHERE kind = ?3
           )",
        params![
            node_id,
            CURATED_PROJECTION_CATEGORY_EDGE,
            CURATED_PROJECTION_CATEGORY_KIND
        ],
    )?;
    let edge_id = uuid::Uuid::new_v5(
        &FM_GRAPH_NAMESPACE,
        format!(
            "edge:{node_id}:{}:{category_id}",
            CURATED_PROJECTION_CATEGORY_EDGE
        )
        .as_bytes(),
    )
    .to_string();
    conn.execute(
        "INSERT INTO graph_edges
         (id, src_id, tag, dst_id, fact_id, trust, created_at, last_seen,
          owner, workspace_id, status)
         VALUES (?1, ?2, ?3, ?4, NULL, 0, ?5, ?5, ?6, ?7, 'active')
         ON CONFLICT(src_id, tag, dst_id) DO UPDATE SET
           last_seen = excluded.last_seen, status = 'active'",
        params![
            edge_id,
            node_id,
            CURATED_PROJECTION_CATEGORY_EDGE,
            category_id,
            updated_at,
            scope.owner,
            scope.workspace_id
        ],
    )?;
    prune_curated_projection_categories_inner(conn, &old_category_ids)?;

    // Replace only deterministic fallback cues. LLM-extracted cues on graph
    // overlay nodes remain independent and untouched.
    conn.execute(
        "DELETE FROM graph_cues
         WHERE node_id = ?1 AND source = ?2",
        params![node_id, CURATED_PROJECTION_CUE_SOURCE],
    )?;
    let mut cues = rake_cues(&content, CURATED_PROJECTION_MAX_CUES);
    if cues.is_empty() {
        let fallback = norm_name(&label);
        if fallback.len() > 2 {
            cues.push(fallback);
        }
    }
    for cue in cues {
        let cue = norm_name(&cue);
        if cue.is_empty() {
            continue;
        }
        conn.execute(
            "INSERT INTO graph_cues
             (cue, node_id, source, created_at, owner, workspace_id, status)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'active')
             ON CONFLICT(cue, node_id) DO UPDATE SET status = 'active'",
            params![
                cue,
                node_id,
                CURATED_PROJECTION_CUE_SOURCE,
                updated_at,
                scope.owner,
                scope.workspace_id
            ],
        )?;
    }
    Ok(Some(node_id))
}

pub(super) fn backfill_curated_projections_inner(conn: &Connection) -> rusqlite::Result<usize> {
    let curated_ids: Vec<String> = {
        let mut statement = conn.prepare(
            "SELECT id FROM curated
             WHERE archived = 0
               AND owner IS NOT NULL AND trim(owner) <> ''
               AND trim(workspace_id) <> ''",
        )?;
        let rows = statement
            .query_map([], |row| row.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        rows
    };
    for curated_id in &curated_ids {
        sync_curated_projection_inner(conn, curated_id)?;
    }

    let stale_node_ids: Vec<String> = {
        let mut statement = conn.prepare(
            "SELECT n.id FROM graph_nodes n
             WHERE n.ref_table = 'curated' AND n.ref_id IS NOT NULL
               AND n.kind = ?1 AND n.layer = ?2 AND n.name = n.ref_id
               AND NOT EXISTS (
                   SELECT 1 FROM curated c
                   WHERE c.id = n.ref_id AND c.archived = 0
                     AND c.owner = n.owner AND c.workspace_id = n.workspace_id
               )",
        )?;
        let rows = statement
            .query_map(
                params![CURATED_PROJECTION_KIND, CURATED_PROJECTION_LAYER],
                |row| row.get(0),
            )?
            .collect::<rusqlite::Result<_>>()?;
        rows
    };
    for node_id in stale_node_ids {
        delete_curated_projection_node_inner(conn, &node_id)?;
    }
    Ok(curated_ids.len())
}

impl SqliteStore {
    /// Point a node at the tier row backing it (fetch returns that content).
    pub fn graph_link_ref(
        &self,
        scope: &GraphScope,
        node_id: &str,
        ref_table: &str,
        ref_id: &str,
    ) -> rusqlite::Result<()> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "UPDATE graph_nodes SET ref_table = ?2, ref_id = ?3
             WHERE id = ?1 AND owner = ?4 AND workspace_id = ?5",
            params![node_id, ref_table, ref_id, scope.owner, scope.workspace_id],
        )?;
        Ok(())
    }

    pub fn graph_upsert(
        &self,
        scope: &GraphScope,
        input: &GraphUpsertInput,
    ) -> rusqlite::Result<GraphUpsertResult> {
        let conn = self.conn.lock().unwrap();
        Self::graph_upsert_inner(&conn, scope, input)
    }

    fn graph_upsert_inner(
        conn: &Connection,
        scope: &GraphScope,
        input: &GraphUpsertInput,
    ) -> rusqlite::Result<GraphUpsertResult> {
        let now = Utc::now().to_rfc3339();
        let mut nodes_upserted = 0usize;
        let mut edges_upserted = 0usize;
        let mut cues_upserted = 0usize;

        for n in &input.nodes {
            upsert_node_inner(
                conn,
                scope,
                &n.kind,
                &n.name,
                n.label.as_deref(),
                n.layer.as_deref().unwrap_or("semantic"),
                n.trust.unwrap_or(0),
                &now,
            )?;
            nodes_upserted += 1;
        }

        for e in &input.edges {
            // Edge endpoints are auto-created (idempotent by UUIDv5) so an
            // extraction payload never has to list every node explicitly.
            let src = upsert_node_inner(
                conn,
                scope,
                &e.src.kind,
                &e.src.name,
                None,
                "semantic",
                0,
                &now,
            )?;
            let dst = upsert_node_inner(
                conn,
                scope,
                &e.dst.kind,
                &e.dst.name,
                None,
                "semantic",
                0,
                &now,
            )?;
            let tag = e.tag.trim().to_lowercase().replace(' ', "_");

            // The edge fact lives in the facts tier: FTS (and vectors, once
            // the embed path fills them) come for free there.
            let fact_id: Option<String> = match &e.fact {
                Some(fact) if !fact.trim().is_empty() => {
                    let fid = uuid::Uuid::new_v5(
                        &FM_GRAPH_NAMESPACE,
                        format!("fact:{src}:{tag}:{dst}").as_bytes(),
                    )
                    .to_string();
                    conn.execute(
                        "INSERT INTO facts
                         (id, content, entities, created_at, updated_at, owner, workspace_id, status)
                         VALUES (?1, ?2, ?3, ?4, ?4, ?5, ?6, 'active')
                         ON CONFLICT(id) DO UPDATE SET
                           content = excluded.content,
                           updated_at = excluded.updated_at,
                           status = 'active'",
                        params![
                            fid,
                            fact.trim(),
                            serde_json::to_string(&[&e.src.name, &e.dst.name]).unwrap_or_default(),
                            now,
                            scope.owner,
                            scope.workspace_id,
                        ],
                    )?;
                    let rowid: i64 = conn.query_row(
                        "SELECT rowid FROM facts WHERE id = ?1",
                        params![fid],
                        |r| r.get(0),
                    )?;
                    let _ = conn.execute(
                        "INSERT OR REPLACE INTO facts_fts(rowid, content, entities)
                         VALUES (?1, ?2, ?3)",
                        params![
                            rowid,
                            fact.trim(),
                            serde_json::to_string(&[&e.src.name, &e.dst.name]).unwrap_or_default()
                        ],
                    );
                    Some(fid)
                }
                _ => None,
            };

            let edge_id = uuid::Uuid::new_v5(
                &FM_GRAPH_NAMESPACE,
                format!("edge:{src}:{tag}:{dst}").as_bytes(),
            )
            .to_string();
            conn.execute(
                "INSERT INTO graph_edges
                 (id, src_id, tag, dst_id, fact_id, trust, created_at, last_seen, owner, workspace_id, status)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?7, ?8, ?9, 'active')
                 ON CONFLICT(src_id, tag, dst_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    fact_id = COALESCE(excluded.fact_id, graph_edges.fact_id),
                    trust = MAX(graph_edges.trust, excluded.trust),
                    status = 'active'",
                params![
                    edge_id,
                    src,
                    tag,
                    dst,
                    fact_id,
                    e.trust.unwrap_or(0),
                    now,
                    scope.owner,
                    scope.workspace_id,
                ],
            )?;
            edges_upserted += 1;
        }

        for c in &input.cues {
            let node = upsert_node_inner(
                conn,
                scope,
                &c.node.kind,
                &c.node.name,
                None,
                "semantic",
                0,
                &now,
            )?;
            let cue = norm_name(&c.cue);
            if cue.is_empty() {
                continue;
            }
            let inserted = conn.execute(
                "INSERT INTO graph_cues
                 (cue, node_id, source, created_at, owner, workspace_id, status)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'active')
                 ON CONFLICT(cue, node_id) DO UPDATE SET status = 'active'",
                params![
                    cue,
                    node,
                    c.source.as_deref().unwrap_or("extracted"),
                    now,
                    scope.owner,
                    scope.workspace_id,
                ],
            )?;
            // graph_cues_fts is synchronized by database triggers installed
            // with the v5 schema. Keeping a second manual write here created
            // duplicate FTS rows and historical orphans.
            let _ = inserted;
            cues_upserted += 1;
        }

        Ok(GraphUpsertResult {
            nodes_upserted,
            edges_upserted,
            cues_upserted,
        })
    }

    /// FTS match over cues → candidate entry nodes.
    pub fn graph_cues(
        &self,
        scope: &GraphScope,
        query: &str,
        limit: usize,
    ) -> rusqlite::Result<Vec<CueHit>> {
        let conn = self.conn.lock().unwrap();
        let fts_query = query
            .split_whitespace()
            .map(|w| format!("\"{}\"", w.replace('"', "")))
            .collect::<Vec<_>>()
            .join(" OR ");
        if fts_query.is_empty() {
            return Ok(vec![]);
        }
        let mut stmt = conn.prepare(&format!(
            "SELECT f.cue, {cols} FROM graph_cues_fts f
                 JOIN graph_nodes n ON n.id = f.node_id
                 WHERE graph_cues_fts MATCH ?1 AND n.status = 'active'
                   AND n.owner = ?2
                   AND (n.workspace_id = ?3 OR (?4 AND n.workspace_id = 'global'))
                 LIMIT ?5",
            cols = NODE_COLS
                .split(", ")
                .map(|c| format!("n.{c}"))
                .collect::<Vec<_>>()
                .join(", ")
        ))?;
        let rows = stmt.query_map(
            params![
                fts_query,
                scope.owner,
                scope.workspace_id,
                scope.include_global,
                limit as i64,
            ],
            |row| {
                Ok(CueHit {
                    cue: row.get(0)?,
                    node: GraphNodeRow {
                        id: row.get(1)?,
                        kind: row.get(2)?,
                        label: row.get(3)?,
                        name: row.get(4)?,
                        layer: row.get(5)?,
                        trust: row.get(6)?,
                        created_at: row.get(7)?,
                        last_seen: row.get(8)?,
                        owner: row.get(9)?,
                        workspace_id: row.get(10)?,
                    },
                })
            },
        )?;
        rows.collect()
    }

    /// Cheap routing read: which tags leave/enter this node (NO content).
    pub fn graph_tags(&self, scope: &GraphScope, node_id: &str) -> rusqlite::Result<Vec<TagCount>> {
        let conn = self.conn.lock().unwrap();
        let mut out = Vec::new();
        let mut stmt = conn.prepare(
            "SELECT tag, COUNT(*) FROM graph_edges
             WHERE src_id = ?1 AND status='active' AND owner = ?2
               AND (workspace_id = ?3 OR (?4 AND workspace_id = 'global'))
             GROUP BY tag",
        )?;
        for r in stmt.query_map(
            params![
                node_id,
                scope.owner,
                scope.workspace_id,
                scope.include_global
            ],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)),
        )? {
            let (tag, count) = r?;
            out.push(TagCount {
                tag,
                direction: "out".into(),
                count,
            });
        }
        let mut stmt = conn.prepare(
            "SELECT tag, COUNT(*) FROM graph_edges
             WHERE dst_id = ?1 AND status='active' AND owner = ?2
               AND (workspace_id = ?3 OR (?4 AND workspace_id = 'global'))
             GROUP BY tag",
        )?;
        for r in stmt.query_map(
            params![
                node_id,
                scope.owner,
                scope.workspace_id,
                scope.include_global
            ],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)),
        )? {
            let (tag, count) = r?;
            out.push(TagCount {
                tag,
                direction: "in".into(),
                count,
            });
        }
        Ok(out)
    }

    /// Neighbors + edge facts, optionally filtered by tag/direction.
    /// Bumps traversal_count on returned edges (Hebbian raw material for G2).
    pub fn graph_expand(
        &self,
        scope: &GraphScope,
        node_id: &str,
        tag: Option<&str>,
        direction: Option<&str>,
        limit: usize,
    ) -> rusqlite::Result<Vec<ExpandHit>> {
        let conn = self.conn.lock().unwrap();
        let mut hits: Vec<ExpandHit> = Vec::new();
        let directions: Vec<&str> = match direction {
            Some("out") => vec!["out"],
            Some("in") => vec!["in"],
            _ => vec!["out", "in"],
        };
        for dir in directions {
            let (self_col, other_col) = if dir == "out" {
                ("src_id", "dst_id")
            } else {
                ("dst_id", "src_id")
            };
            let sql = format!(
                "SELECT e.id, e.src_id, e.tag, e.dst_id, f.content, e.weight, e.traversal_count, e.trust,
                        {cols}
                 FROM graph_edges e
                 JOIN graph_nodes n ON n.id = e.{other_col}
                 LEFT JOIN facts f ON f.id = e.fact_id
                 WHERE e.{self_col} = ?1 AND e.status='active' AND n.status='active'
                   AND e.owner = ?2 AND n.owner = ?2
                   AND (e.workspace_id = ?3 OR (?4 AND e.workspace_id = 'global'))
                   AND (n.workspace_id = ?3 OR (?4 AND n.workspace_id = 'global'))
                   AND (f.id IS NULL OR (f.status='active' AND f.owner = ?2
                     AND (f.workspace_id = ?3 OR (?4 AND f.workspace_id = 'global'))))
                   AND (?5 IS NULL OR e.tag = ?5)
                 ORDER BY e.weight DESC, e.last_seen DESC
                 LIMIT ?6",
                cols = NODE_COLS
                    .split(", ")
                    .map(|c| format!("n.{c}"))
                    .collect::<Vec<_>>()
                    .join(", ")
            );
            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map(
                params![
                    node_id,
                    scope.owner,
                    scope.workspace_id,
                    scope.include_global,
                    tag,
                    limit as i64,
                ],
                |row| {
                    Ok(ExpandHit {
                        edge: GraphEdgeRow {
                            id: row.get(0)?,
                            src_id: row.get(1)?,
                            tag: row.get(2)?,
                            dst_id: row.get(3)?,
                            fact: row.get(4)?,
                            weight: row.get(5)?,
                            traversal_count: row.get(6)?,
                            trust: row.get(7)?,
                            owner: scope.owner.clone(),
                            workspace_id: row.get(17)?,
                        },
                        other: GraphNodeRow {
                            id: row.get(8)?,
                            kind: row.get(9)?,
                            label: row.get(10)?,
                            name: row.get(11)?,
                            layer: row.get(12)?,
                            trust: row.get(13)?,
                            created_at: row.get(14)?,
                            last_seen: row.get(15)?,
                            owner: row.get(16)?,
                            workspace_id: row.get(17)?,
                        },
                        direction: dir.to_string(),
                    })
                },
            )?;
            for r in rows {
                hits.push(r?);
            }
        }
        hits.truncate(limit);
        for h in &hits {
            let _ = conn.execute(
                "UPDATE graph_edges SET traversal_count = traversal_count + 1
                 WHERE id = ?1 AND owner = ?2 AND workspace_id = ?3",
                params![h.edge.id, h.edge.owner, h.edge.workspace_id],
            );
        }
        Ok(hits)
    }

    pub fn graph_fetch(
        &self,
        scope: &GraphScope,
        node_id: &str,
    ) -> rusqlite::Result<Option<(GraphNodeRow, Option<String>)>> {
        let conn = self.conn.lock().unwrap();
        let node = conn
            .query_row(
                &format!(
                    "SELECT {NODE_COLS} FROM graph_nodes
                     WHERE id = ?1 AND status='active' AND owner = ?2
                       AND (workspace_id = ?3 OR (?4 AND workspace_id = 'global'))"
                ),
                params![
                    node_id,
                    scope.owner,
                    scope.workspace_id,
                    scope.include_global
                ],
                row_to_node,
            )
            .optional()?;
        let Some(node) = node else { return Ok(None) };
        // Referenced tier content, when the node points at a stored record.
        let content: Option<String> = conn
            .query_row(
                "SELECT ref_table, ref_id FROM graph_nodes
                 WHERE id = ?1 AND status='active' AND owner = ?2
                   AND (workspace_id = ?3 OR (?4 AND workspace_id = 'global'))",
                params![
                    node_id,
                    scope.owner,
                    scope.workspace_id,
                    scope.include_global
                ],
                |r| {
                    Ok((
                        r.get::<_, Option<String>>(0)?,
                        r.get::<_, Option<String>>(1)?,
                    ))
                },
            )
            .ok()
            .and_then(|(t, i)| match (t.as_deref(), i) {
                (Some("curated"), Some(id)) => conn
                    .query_row(
                        "SELECT content FROM curated WHERE id = ?1 AND archived=0 AND owner = ?2
                         AND (workspace_id = ?3 OR (?4 AND workspace_id = 'global'))",
                        params![id, scope.owner, scope.workspace_id, scope.include_global],
                        |r| r.get(0),
                    )
                    .ok(),
                (Some("facts"), Some(id)) => conn
                    .query_row(
                        "SELECT content FROM facts WHERE id = ?1 AND status='active' AND owner = ?2
                         AND (workspace_id = ?3 OR (?4 AND workspace_id = 'global'))",
                        params![id, scope.owner, scope.workspace_id, scope.include_global],
                        |r| r.get(0),
                    )
                    .ok(),
                _ => None,
            });
        Ok(Some((node, content)))
    }

    /// BFS over edges (both directions). With a destination: paths src→dst.
    /// Without: the reachable frontier up to max_depth as single-node paths.
    /// Canvas seed: the most-recently-seen nodes plus every active edge
    /// connecting two of them. Unlike expand/trace this needs no starting
    /// node, so a UI can draw the graph cold.
    pub fn graph_overview(
        &self,
        scope: &GraphScope,
        limit: usize,
    ) -> rusqlite::Result<GraphOverview> {
        let conn = self.conn.lock().unwrap();
        let mut nodes: Vec<GraphNodeRow> = Vec::new();
        {
            let sql = format!(
                "SELECT {NODE_COLS} FROM graph_nodes
                 WHERE status='active' AND owner = ?1
                   AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))
                 ORDER BY last_seen DESC
                 LIMIT ?4"
            );
            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map(
                params![
                    scope.owner,
                    scope.workspace_id,
                    scope.include_global,
                    limit as i64,
                ],
                row_to_node,
            )?;
            for row in rows {
                nodes.push(row?);
            }
        }

        let mut edges: Vec<GraphEdgeRow> = Vec::new();
        if !nodes.is_empty() {
            let placeholders = nodes
                .iter()
                .enumerate()
                .map(|(index, _)| format!("?{}", index + 4))
                .collect::<Vec<_>>()
                .join(", ");
            let sql = format!(
                "SELECT id, src_id, tag, dst_id, NULL, weight, traversal_count, trust, workspace_id
                 FROM graph_edges
                 WHERE status='active' AND owner = ?1
                   AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))
                   AND src_id IN ({placeholders}) AND dst_id IN ({placeholders})
                 ORDER BY weight DESC, last_seen DESC"
            );
            let mut stmt = conn.prepare(&sql)?;
            let mut bindings: Vec<&dyn rusqlite::ToSql> =
                vec![&scope.owner, &scope.workspace_id, &scope.include_global];
            for node in &nodes {
                bindings.push(&node.id);
            }
            let rows = stmt.query_map(bindings.as_slice(), |row| {
                Ok(GraphEdgeRow {
                    id: row.get(0)?,
                    src_id: row.get(1)?,
                    tag: row.get(2)?,
                    dst_id: row.get(3)?,
                    fact: row.get(4)?,
                    weight: row.get(5)?,
                    traversal_count: row.get(6)?,
                    trust: row.get(7)?,
                    owner: scope.owner.clone(),
                    workspace_id: row.get(8)?,
                })
            })?;
            for row in rows {
                edges.push(row?);
            }
        }

        let scoped_total = |table: &str| -> rusqlite::Result<i64> {
            conn.query_row(
                &format!(
                    "SELECT COUNT(*) FROM {table}
                     WHERE status='active' AND owner = ?1
                       AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))"
                ),
                params![scope.owner, scope.workspace_id, scope.include_global],
                |row| row.get(0),
            )
        };
        let node_total = scoped_total("graph_nodes")?;
        let edge_total = scoped_total("graph_edges")?;

        Ok(GraphOverview {
            nodes,
            edges,
            node_total,
            edge_total,
        })
    }

    pub fn graph_trace(
        &self,
        scope: &GraphScope,
        src_id: &str,
        dst_id: Option<&str>,
        max_depth: usize,
        limit: usize,
    ) -> rusqlite::Result<Vec<TracePath>> {
        let conn = self.conn.lock().unwrap();
        // parent map: node -> (prev_node, tag)
        let mut parent: HashMap<String, (String, String)> = HashMap::new();
        let mut seen: HashSet<String> = HashSet::from([src_id.to_string()]);
        let mut queue: VecDeque<(String, usize)> = VecDeque::from([(src_id.to_string(), 0)]);
        let mut paths: Vec<TracePath> = Vec::new();

        let mut neighbors_stmt = conn.prepare(
            "SELECT dst_id, tag FROM graph_edges
             WHERE src_id = ?1 AND status='active' AND owner = ?2
               AND (workspace_id = ?3 OR (?4 AND workspace_id = 'global'))
             UNION ALL
             SELECT src_id, tag FROM graph_edges
             WHERE dst_id = ?1 AND status='active' AND owner = ?2
               AND (workspace_id = ?3 OR (?4 AND workspace_id = 'global'))",
        )?;

        while let Some((current, depth)) = queue.pop_front() {
            if depth >= max_depth {
                continue;
            }
            let neighbors: Vec<(String, String)> = neighbors_stmt
                .query_map(
                    params![
                        current,
                        scope.owner,
                        scope.workspace_id,
                        scope.include_global
                    ],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )?
                .collect::<Result<_, _>>()?;
            for (next, tag) in neighbors {
                if !seen.insert(next.clone()) {
                    continue;
                }
                parent.insert(next.clone(), (current.clone(), tag));
                if Some(next.as_str()) == dst_id {
                    // Reconstruct path.
                    let mut node_ids = vec![next.clone()];
                    let mut tags = Vec::new();
                    let mut cursor = next.clone();
                    while let Some((prev, t)) = parent.get(&cursor) {
                        tags.push(t.clone());
                        node_ids.push(prev.clone());
                        cursor = prev.clone();
                    }
                    node_ids.reverse();
                    tags.reverse();
                    paths.push(TracePath { node_ids, tags });
                    if paths.len() >= limit {
                        return Ok(paths);
                    }
                    continue;
                }
                queue.push_back((next, depth + 1));
            }
        }

        if dst_id.is_none() {
            for id in seen.into_iter().filter(|n| n != src_id).take(limit) {
                paths.push(TracePath {
                    node_ids: vec![id],
                    tags: vec![],
                });
            }
        }
        Ok(paths)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store() -> SqliteStore {
        SqliteStore::memory(4).unwrap()
    }

    fn upsert_fixture(s: &SqliteStore) -> GraphUpsertResult {
        s.graph_upsert(
            &test_scope(),
            &GraphUpsertInput {
                nodes: vec![GraphNodeInput {
                    kind: "person".into(),
                    name: "e".into(),
                    label: Some("owner".into()),
                    layer: None,
                    trust: Some(4),
                }],
                edges: vec![
                    GraphEdgeInput {
                        src: NodeRef {
                            kind: "person".into(),
                            name: "e".into(),
                        },
                        tag: "works_on".into(),
                        dst: NodeRef {
                            kind: "project".into(),
                            name: "open-clank".into(),
                        },
                        fact: Some("e works on the open-clank workspace".into()),
                        trust: Some(4),
                    },
                    GraphEdgeInput {
                        src: NodeRef {
                            kind: "project".into(),
                            name: "open-clank".into(),
                        },
                        tag: "uses".into(),
                        dst: NodeRef {
                            kind: "tool".into(),
                            name: "frankenmemory".into(),
                        },
                        fact: Some("open-clank uses frankenmemory for agent memory".into()),
                        trust: Some(3),
                    },
                ],
                cues: vec![GraphCueInput {
                    cue: "Hedgehog Snacks".into(),
                    node: NodeRef {
                        kind: "project".into(),
                        name: "open-clank".into(),
                    },
                    source: None,
                }],
            },
        )
        .unwrap()
    }

    #[test]
    fn upsert_is_idempotent() {
        let s = store();
        let first = upsert_fixture(&s);
        assert_eq!(first.nodes_upserted, 1);
        assert_eq!(first.edges_upserted, 2);
        assert_eq!(first.cues_upserted, 1);

        upsert_fixture(&s);
        let conn = s.conn.lock().unwrap();
        let nodes: i64 = conn
            .query_row("SELECT count(*) FROM graph_nodes", [], |r| r.get(0))
            .unwrap();
        let edges: i64 = conn
            .query_row("SELECT count(*) FROM graph_edges", [], |r| r.get(0))
            .unwrap();
        let cues: i64 = conn
            .query_row("SELECT count(*) FROM graph_cues", [], |r| r.get(0))
            .unwrap();
        assert_eq!(nodes, 3, "e, open-clank, frankenmemory — no dupes");
        assert_eq!(edges, 2);
        assert_eq!(cues, 1);
    }

    #[test]
    fn cue_lookup_finds_entry_node() {
        let s = store();
        upsert_fixture(&s);
        let hits = s
            .graph_cues(&test_scope(), "hedgehog snacks pantry", 10)
            .unwrap();
        assert!(!hits.is_empty());
        assert_eq!(hits[0].node.name, "open-clank");
    }

    #[test]
    fn graph_scope_isolates_identical_entities() {
        let s = store();
        let alice = GraphScope::new("alice", "same-workspace").unwrap();
        let bob = GraphScope::new("bob", "same-workspace").unwrap();
        let input = GraphUpsertInput {
            nodes: vec![],
            edges: vec![],
            cues: vec![GraphCueInput {
                cue: "private telescope".into(),
                node: NodeRef {
                    kind: "project".into(),
                    name: "shared-name".into(),
                },
                source: None,
            }],
        };
        s.graph_upsert(&alice, &input).unwrap();
        s.graph_upsert(&bob, &input).unwrap();

        let alice_hits = s.graph_cues(&alice, "private telescope", 10).unwrap();
        let bob_hits = s.graph_cues(&bob, "private telescope", 10).unwrap();
        assert_eq!(alice_hits.len(), 1);
        assert_eq!(bob_hits.len(), 1);
        assert_eq!(alice_hits[0].node.owner, "alice");
        assert_eq!(bob_hits[0].node.owner, "bob");
        assert_ne!(alice_hits[0].node.id, bob_hits[0].node.id);
        assert!(s
            .graph_fetch(&alice, &bob_hits[0].node.id)
            .unwrap()
            .is_none());
    }

    #[test]
    fn tags_then_expand_walks_without_content() {
        let s = store();
        upsert_fixture(&s);
        let oc = test_scope().node_id("project", "open-clank");

        let tags = s.graph_tags(&test_scope(), &oc).unwrap();
        let out: Vec<_> = tags
            .iter()
            .filter(|t| t.direction == "out")
            .map(|t| t.tag.as_str())
            .collect();
        let inn: Vec<_> = tags
            .iter()
            .filter(|t| t.direction == "in")
            .map(|t| t.tag.as_str())
            .collect();
        assert_eq!(out, vec!["uses"]);
        assert_eq!(inn, vec!["works_on"]);

        let hits = s
            .graph_expand(&test_scope(), &oc, Some("uses"), Some("out"), 10)
            .unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].other.name, "frankenmemory");
        assert!(hits[0]
            .edge
            .fact
            .as_deref()
            .unwrap()
            .contains("agent memory"));

        // Hebbian raw material: traversal bumped.
        let hits2 = s
            .graph_expand(&test_scope(), &oc, Some("uses"), Some("out"), 10)
            .unwrap();
        assert_eq!(hits2[0].edge.traversal_count, 1);
    }

    #[test]
    fn fetch_returns_node() {
        let s = store();
        upsert_fixture(&s);
        let id = test_scope().node_id("person", "e");
        let (node, content) = s.graph_fetch(&test_scope(), &id).unwrap().unwrap();
        assert_eq!(node.kind, "person");
        assert_eq!(node.label.as_deref(), Some("owner"));
        assert!(content.is_none(), "no ref content attached");
        assert!(s
            .graph_fetch(&test_scope(), "nonexistent")
            .unwrap()
            .is_none());
    }

    #[test]
    fn trace_finds_two_hop_path() {
        let s = store();
        upsert_fixture(&s);
        let e = test_scope().node_id("person", "e");
        let fm = test_scope().node_id("tool", "frankenmemory");
        let paths = s.graph_trace(&test_scope(), &e, Some(&fm), 4, 5).unwrap();
        assert_eq!(paths.len(), 1);
        assert_eq!(paths[0].node_ids.len(), 3);
        assert_eq!(
            paths[0].tags,
            vec!["works_on".to_string(), "uses".to_string()]
        );
    }

    #[test]
    fn migration_v1_db_gains_graph_tables() {
        // Fresh store is already v2; simulate a v1 DB by dropping graph
        // tables and stamping v1, then re-running init.
        let s = store();
        {
            let conn = s.conn.lock().unwrap();
            conn.execute_batch(
                "DROP TABLE graph_nodes; DROP TABLE graph_edges; DROP TABLE graph_cues;",
            )
            .unwrap();
            conn.pragma_update(None, "user_version", 1).unwrap();
        }
        s.init_tables().unwrap();
        let conn = s.conn.lock().unwrap();
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, crate::store::sqlite::SCHEMA_VERSION);
        let n: i64 = conn
            .query_row("SELECT count(*) FROM graph_nodes", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 0);
    }
}

/// Canonical edge-tag vocabulary — keep in sync with
/// .clanker/futures/frankenmemory-update/tag-vocabulary.md and the extraction prompt
/// in mimo's memory/graph-extract.ts.
pub const CANONICAL_TAGS: &[&str] = &[
    "is",
    "has",
    "uses",
    "makes",
    "runs",
    "talks_to",
    "lives_in",
    "made_by",
    "works_on",
    "wants",
    "likes",
    "dislikes",
    "before",
    "blocks",
    "fixes",
    "about",
    "imports",
    "calls",
    "defines",
    "extends",
    "tests",
    "configures",
];

fn levenshtein(a: &str, b: &str) -> usize {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    let mut cur = vec![0usize; b.len() + 1];
    for i in 1..=a.len() {
        cur[0] = i;
        for j in 1..=b.len() {
            let cost = if a[i - 1] == b[j - 1] { 0 } else { 1 };
            cur[j] = (prev[j] + 1).min(cur[j - 1] + 1).min(prev[j - 1] + cost);
        }
        std::mem::swap(&mut prev, &mut cur);
    }
    prev[b.len()]
}

/// Map a stray tag onto the canonical vocabulary, or None to leave it alone.
/// Matches: exact, singular/plural, and small typos (edit distance scaled to
/// tag length). Deliberately conservative — a wrong merge is worse than a
/// stray tag (G2 can re-run after vocabulary grows).
pub fn canonicalize_tag(tag: &str) -> Option<&'static str> {
    let t = tag.trim().to_lowercase().replace(' ', "_");
    for c in CANONICAL_TAGS {
        if t == *c {
            return Some(c);
        }
    }
    for c in CANONICAL_TAGS {
        if t.strip_suffix('s') == Some(c) || format!("{t}s") == *c {
            return Some(c);
        }
        let max_dist = if c.len() <= 4 { 1 } else { 2 };
        if levenshtein(&t, c) <= max_dist {
            return Some(c);
        }
    }
    None
}

#[derive(Debug, Clone, Copy)]
pub struct EdgeDecayParams {
    pub half_life_days: f64,
    /// Edges whose decayed weight falls below this are pruned.
    pub min_weight: f64,
    /// Hebbian bonus per ln(1+traversal_count).
    pub traversal_gain: f64,
}

impl Default for EdgeDecayParams {
    fn default() -> Self {
        Self {
            half_life_days: 30.0,
            min_weight: 0.05,
            traversal_gain: 0.15,
        }
    }
}

impl SqliteStore {
    /// Decay edge weights by age since last_seen, boosted by traversal count
    /// (the Hebbian raw material graph_expand accumulates). Prunes edges
    /// whose decayed weight drops below `min_weight`. Returns (decayed, pruned).
    pub fn graph_edge_decay(
        &self,
        scope: &GraphScope,
        p: &EdgeDecayParams,
        dry_run: bool,
    ) -> rusqlite::Result<(usize, usize)> {
        let conn = self.conn.lock().unwrap();
        let now = Utc::now();
        let mut decayed = 0usize;
        let mut prune: Vec<String> = Vec::new();
        let mut updates: Vec<(String, f64)> = Vec::new();
        {
            let mut stmt = conn.prepare(
                "SELECT id, weight, traversal_count, last_seen FROM graph_edges
                 WHERE status='active' AND owner = ?1
                   AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))",
            )?;
            let rows = stmt.query_map(
                params![scope.owner, scope.workspace_id, scope.include_global],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, f64>(1)?,
                        r.get::<_, i64>(2)?,
                        r.get::<_, String>(3)?,
                    ))
                },
            )?;
            for row in rows {
                let (id, weight, traversals, last_seen) = row?;
                let age_days = chrono::DateTime::parse_from_rfc3339(&last_seen)
                    .map(|t| (now - t.with_timezone(&chrono::Utc)).num_seconds() as f64 / 86_400.0)
                    .unwrap_or(0.0)
                    .max(0.0);
                let decay = 0.5f64.powf(age_days / p.half_life_days);
                let boost = 1.0 + p.traversal_gain * ((1 + traversals) as f64).ln();
                let new_weight = (weight * decay * boost).min(10.0);
                if new_weight < p.min_weight {
                    prune.push(id);
                } else if (new_weight - weight).abs() > f64::EPSILON {
                    updates.push((id, new_weight));
                    decayed += 1;
                }
            }
        }
        if !dry_run {
            for (id, w) in &updates {
                conn.execute(
                    "UPDATE graph_edges SET weight = ?2 WHERE id = ?1 AND owner = ?3",
                    params![id, w, scope.owner],
                )?;
            }
            for id in &prune {
                conn.execute(
                    "DELETE FROM graph_edges WHERE id = ?1 AND owner = ?2",
                    params![id, scope.owner],
                )?;
            }
        }
        Ok((decayed, prune.len()))
    }

    /// Merge stray tags into the canonical vocabulary. When the rename would
    /// collide with an existing (src, tag, dst) edge, the edges MERGE:
    /// traversal counts add, max weight/trust win, the stray row dies.
    /// Returns the number of edges rewritten or merged.
    pub fn graph_tag_normalize(
        &self,
        scope: &GraphScope,
        dry_run: bool,
    ) -> rusqlite::Result<usize> {
        let conn = self.conn.lock().unwrap();
        let strays: Vec<(String, String, String, String)> = {
            let mut stmt = conn.prepare(
                "SELECT id, src_id, tag, dst_id FROM graph_edges
                 WHERE status='active' AND owner = ?1
                   AND (workspace_id = ?2 OR (?3 AND workspace_id = 'global'))",
            )?;
            let rows = stmt.query_map(
                params![scope.owner, scope.workspace_id, scope.include_global],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, String>(3)?,
                    ))
                },
            )?;
            rows.collect::<Result<Vec<_>, _>>()?
                .into_iter()
                .filter(|(_, _, tag, _)| {
                    canonicalize_tag(tag)
                        .map(|c| c != tag.as_str())
                        .unwrap_or(false)
                })
                .collect()
        };
        let mut changed = 0usize;
        for (id, src, tag, dst) in strays {
            let canonical = canonicalize_tag(&tag).expect("filtered above");
            if dry_run {
                changed += 1;
                continue;
            }
            let existing: Option<String> = conn
                .query_row(
                    "SELECT id FROM graph_edges
                     WHERE src_id = ?1 AND tag = ?2 AND dst_id = ?3 AND owner = ?4
                       AND (workspace_id = ?5 OR (?6 AND workspace_id = 'global'))",
                    params![
                        src,
                        canonical,
                        dst,
                        scope.owner,
                        scope.workspace_id,
                        scope.include_global,
                    ],
                    |r| r.get(0),
                )
                .optional()?;
            match existing {
                Some(winner) => {
                    conn.execute(
                        "UPDATE graph_edges SET
                            traversal_count = traversal_count + (SELECT traversal_count FROM graph_edges WHERE id = ?2),
                            weight = MAX(weight, (SELECT weight FROM graph_edges WHERE id = ?2)),
                            trust = MAX(trust, (SELECT trust FROM graph_edges WHERE id = ?2)),
                            fact_id = COALESCE(fact_id, (SELECT fact_id FROM graph_edges WHERE id = ?2))
                         WHERE id = ?1",
                        params![winner, id],
                    )?;
                    conn.execute("DELETE FROM graph_edges WHERE id = ?1", params![id])?;
                }
                None => {
                    conn.execute(
                        "UPDATE graph_edges SET tag = ?2 WHERE id = ?1",
                        params![id, canonical],
                    )?;
                }
            }
            changed += 1;
        }
        Ok(changed)
    }
}

#[cfg(test)]
mod groom_tests {
    use super::*;

    fn seeded() -> SqliteStore {
        let s = SqliteStore::memory(4).unwrap();
        s.graph_upsert(
            &test_scope(),
            &GraphUpsertInput {
                nodes: vec![],
                edges: vec![
                    GraphEdgeInput {
                        src: NodeRef {
                            kind: "person".into(),
                            name: "e".into(),
                        },
                        tag: "works_onn".into(),
                        dst: NodeRef {
                            kind: "project".into(),
                            name: "open-clank".into(),
                        },
                        fact: Some("e is working on open-clank".into()),
                        trust: Some(2),
                    },
                    GraphEdgeInput {
                        src: NodeRef {
                            kind: "person".into(),
                            name: "e".into(),
                        },
                        tag: "works_on".into(),
                        dst: NodeRef {
                            kind: "project".into(),
                            name: "open-clank".into(),
                        },
                        fact: None,
                        trust: Some(4),
                    },
                    GraphEdgeInput {
                        src: NodeRef {
                            kind: "project".into(),
                            name: "open-clank".into(),
                        },
                        tag: "usess".into(),
                        dst: NodeRef {
                            kind: "tool".into(),
                            name: "frankenmemory".into(),
                        },
                        fact: None,
                        trust: None,
                    },
                ],
                cues: vec![],
            },
        )
        .unwrap();
        s
    }

    #[test]
    fn canonicalize_matches_typos_plurals_not_strangers() {
        assert_eq!(canonicalize_tag("uses"), Some("uses"));
        assert_eq!(canonicalize_tag("usess"), Some("uses"));
        assert_eq!(canonicalize_tag("use"), Some("uses"));
        assert_eq!(canonicalize_tag("Works On"), Some("works_on"));
        assert_eq!(canonicalize_tag("maintains"), None, "no aggressive merges");
        assert_eq!(
            canonicalize_tag("working_on"),
            None,
            "distance 3 stays stray by design"
        );
    }

    #[test]
    fn tag_normalize_merges_collisions_and_rewrites_strays() {
        let s = seeded();
        // bump traversals on the stray twin so the merge has something to add
        {
            let conn = s.conn.lock().unwrap();
            conn.execute(
                "UPDATE graph_edges SET traversal_count = 3 WHERE tag = 'works_onn'",
                [],
            )
            .unwrap();
        }
        let changed = s.graph_tag_normalize(&test_scope(), false).unwrap();
        assert_eq!(changed, 2, "works_onn merged + usess rewritten");

        let conn = s.conn.lock().unwrap();
        let tags: Vec<String> = conn
            .prepare("SELECT DISTINCT tag FROM graph_edges WHERE status='active' ORDER BY tag")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap();
        assert_eq!(tags, vec!["uses".to_string(), "works_on".to_string()]);
        let (trav, trust): (i64, i64) = conn
            .query_row(
                "SELECT traversal_count, trust FROM graph_edges WHERE tag = 'works_on'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(trav, 3, "merged traversals survive");
        assert_eq!(trust, 4, "max trust wins");
    }

    #[test]
    fn edge_decay_prunes_stale_and_keeps_traversed() {
        let s = seeded();
        {
            let conn = s.conn.lock().unwrap();
            let old = (Utc::now() - chrono::Duration::days(365)).to_rfc3339();
            conn.execute("UPDATE graph_edges SET last_seen = ?1", params![old])
                .unwrap();
            // one edge earned heavy traversal — Hebbian boost should not save
            // it from a year of silence at default settings, but weights must
            // differ from the untraversed one before pruning kicks both out.
            conn.execute(
                "UPDATE graph_edges SET traversal_count = 50 WHERE tag = 'works_on'",
                [],
            )
            .unwrap();
        }
        let (_, pruned) = s
            .graph_edge_decay(&test_scope(), &EdgeDecayParams::default(), false)
            .unwrap();
        assert!(
            pruned >= 2,
            "year-old edges die at default half-life, got {pruned}"
        );

        // fresh store: traversal boost GROWS weight instead of decaying it
        let s2 = seeded();
        {
            let conn = s2.conn.lock().unwrap();
            conn.execute("UPDATE graph_edges SET traversal_count = 20", [])
                .unwrap();
        }
        let (decayed, pruned) = s2
            .graph_edge_decay(&test_scope(), &EdgeDecayParams::default(), false)
            .unwrap();
        assert_eq!(pruned, 0);
        assert!(decayed > 0);
        let conn = s2.conn.lock().unwrap();
        let w: f64 = conn
            .query_row("SELECT MAX(weight) FROM graph_edges", [], |r| r.get(0))
            .unwrap();
        assert!(w > 1.0, "hebbian boost lifted weight, got {w}");
    }

    #[test]
    fn dry_run_changes_nothing() {
        let s = seeded();
        let changed = s.graph_tag_normalize(&test_scope(), true).unwrap();
        assert_eq!(changed, 2);
        let conn = s.conn.lock().unwrap();
        let stray: i64 = conn
            .query_row(
                "SELECT count(*) FROM graph_edges WHERE tag = 'usess'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(stray, 1, "dry run must not rewrite");
    }

    #[test]
    fn overview_returns_scoped_nodes_edges_and_totals() {
        let s = seeded();
        let o = s.graph_overview(&test_scope(), 25).unwrap();
        assert_eq!(o.nodes.len(), 3, "e + open-clank + frankenmemory");
        assert_eq!(o.node_total, 3);
        assert!(!o.edges.is_empty(), "edges between returned nodes ride");
        assert_eq!(o.edge_total, o.edges.len() as i64);
        for edge in &o.edges {
            assert!(o.nodes.iter().any(|n| n.id == edge.src_id));
            assert!(o.nodes.iter().any(|n| n.id == edge.dst_id));
        }
    }

    #[test]
    fn overview_is_owner_scoped_and_limit_capped() {
        let s = seeded();
        let stranger = GraphScope::new("mallory", "test-workspace").unwrap();
        let foreign = s.graph_overview(&stranger, 25).unwrap();
        assert!(foreign.nodes.is_empty());
        assert_eq!(foreign.node_total, 0);

        let capped = s.graph_overview(&test_scope(), 1).unwrap();
        assert_eq!(capped.nodes.len(), 1);
        assert_eq!(capped.node_total, 3, "totals ignore the limit");
        assert!(
            capped.edges.is_empty(),
            "no dangling edges: both endpoints must be in the node set"
        );
    }
}

impl SqliteStore {
    /// Random-walk-with-restart (personalized PageRank) over the 2-hop
    /// neighborhood of `seeds` — the deterministic, embedding-free structural
    /// relevance signal (codegraph steal). Returns node_id → score.
    pub fn graph_rwr(
        &self,
        scope: &GraphScope,
        seeds: &[String],
        alpha: f64,
        iterations: usize,
    ) -> rusqlite::Result<HashMap<String, f64>> {
        if seeds.is_empty() {
            return Ok(HashMap::new());
        }
        let conn = self.conn.lock().unwrap();

        // Caller-provided IDs are hints, not authority. Only seed nodes that
        // are visible in this scope so rank cannot echo a foreign/arbitrary ID.
        let mut seed_stmt = conn.prepare(
            "SELECT 1 FROM graph_nodes
             WHERE id = ?1 AND status = 'active' AND owner = ?2
               AND (workspace_id = ?3 OR (?4 AND workspace_id = 'global'))",
        )?;
        let scoped_seeds: Vec<String> = seeds
            .iter()
            .filter_map(|seed| {
                seed_stmt
                    .query_row(
                        params![seed, scope.owner, scope.workspace_id, scope.include_global],
                        |_| Ok(()),
                    )
                    .optional()
                    .ok()
                    .flatten()
                    .map(|_| seed.clone())
            })
            .collect();
        drop(seed_stmt);
        if scoped_seeds.is_empty() {
            return Ok(HashMap::new());
        }
        let seeds = scoped_seeds.as_slice();

        // Collect the bounded subgraph: seeds + 2 hops, undirected.
        let mut nodes: HashSet<String> = seeds.iter().cloned().collect();
        let mut frontier: Vec<String> = seeds.to_vec();
        let mut adjacency: HashMap<String, Vec<String>> = HashMap::new();
        let mut stmt = conn.prepare(
            "SELECT dst_id FROM graph_edges
             WHERE src_id = ?1 AND status='active' AND owner = ?2
               AND (workspace_id = ?3 OR (?4 AND workspace_id = 'global'))
             UNION ALL
             SELECT src_id FROM graph_edges
             WHERE dst_id = ?1 AND status='active' AND owner = ?2
               AND (workspace_id = ?3 OR (?4 AND workspace_id = 'global'))",
        )?;
        for _ in 0..2 {
            let mut next = Vec::new();
            for node in frontier.drain(..) {
                let neighbors: Vec<String> = stmt
                    .query_map(
                        params![node, scope.owner, scope.workspace_id, scope.include_global],
                        |r| r.get(0),
                    )?
                    .collect::<Result<_, _>>()?;
                for n in &neighbors {
                    if nodes.insert(n.clone()) {
                        next.push(n.clone());
                    }
                }
                adjacency.entry(node).or_default().extend(neighbors);
            }
            frontier = next;
        }
        // Adjacency for the last frontier layer (their edges INTO the set).
        for node in frontier {
            let neighbors: Vec<String> = stmt
                .query_map(
                    params![
                        node.clone(),
                        scope.owner,
                        scope.workspace_id,
                        scope.include_global,
                    ],
                    |r| r.get(0),
                )?
                .collect::<Result<_, _>>()?;
            adjacency
                .entry(node)
                .or_default()
                .extend(neighbors.into_iter().filter(|n| nodes.contains(n)));
        }

        let restart = 1.0 / seeds.len() as f64;
        let mut score: HashMap<String, f64> = seeds.iter().map(|s| (s.clone(), restart)).collect();
        for _ in 0..iterations {
            let mut next: HashMap<String, f64> = HashMap::new();
            for s in seeds {
                *next.entry(s.clone()).or_default() += alpha * restart;
            }
            for (node, mass) in &score {
                let neighbors = adjacency.get(node);
                let Some(neighbors) = neighbors.filter(|n| !n.is_empty()) else {
                    // Dangling mass restarts.
                    for s in seeds {
                        *next.entry(s.clone()).or_default() += (1.0 - alpha) * mass * restart;
                    }
                    continue;
                };
                let share = (1.0 - alpha) * mass / neighbors.len() as f64;
                for n in neighbors {
                    *next.entry(n.clone()).or_default() += share;
                }
            }
            score = next;
        }
        Ok(score)
    }
}

#[cfg(test)]
mod rwr_tests {
    use super::*;

    #[test]
    fn hub_outranks_leaf_from_same_seed() {
        let s = SqliteStore::memory(4).unwrap();
        // star: hub connected to seeds and many others; leaf hangs alone off seed
        let mut edges = vec![GraphEdgeInput {
            src: NodeRef {
                kind: "concept".into(),
                name: "seed".into(),
            },
            tag: "about".into(),
            dst: NodeRef {
                kind: "concept".into(),
                name: "leaf".into(),
            },
            fact: None,
            trust: None,
        }];
        for i in 0..4 {
            edges.push(GraphEdgeInput {
                src: NodeRef {
                    kind: "concept".into(),
                    name: "seed".into(),
                },
                tag: "about".into(),
                dst: NodeRef {
                    kind: "concept".into(),
                    name: "hub".into(),
                },
                fact: None,
                trust: None,
            });
            edges.push(GraphEdgeInput {
                src: NodeRef {
                    kind: "concept".into(),
                    name: format!("spoke{i}"),
                },
                tag: "about".into(),
                dst: NodeRef {
                    kind: "concept".into(),
                    name: "hub".into(),
                },
                fact: None,
                trust: None,
            });
        }
        s.graph_upsert(
            &test_scope(),
            &GraphUpsertInput {
                nodes: vec![],
                edges,
                cues: vec![],
            },
        )
        .unwrap();

        let seed = test_scope().node_id("concept", "seed");
        let scores = s
            .graph_rwr(&test_scope(), &[seed.clone()], 0.25, 20)
            .unwrap();
        let hub = scores
            .get(&test_scope().node_id("concept", "hub"))
            .copied()
            .unwrap_or(0.0);
        let leaf = scores
            .get(&test_scope().node_id("concept", "leaf"))
            .copied()
            .unwrap_or(0.0);
        assert!(hub > leaf, "hub {hub} must outrank leaf {leaf}");
        assert!(scores.get(&seed).copied().unwrap_or(0.0) > 0.0);
    }

    #[test]
    fn deterministic_and_empty_seeds_safe() {
        let s = SqliteStore::memory(4).unwrap();
        assert!(s
            .graph_rwr(&test_scope(), &[], 0.25, 10)
            .unwrap()
            .is_empty());
        upsert_smoke(&s);
        let seed = test_scope().node_id("person", "e");
        let a = s
            .graph_rwr(&test_scope(), &[seed.clone()], 0.25, 20)
            .unwrap();
        let b = s.graph_rwr(&test_scope(), &[seed], 0.25, 20).unwrap();
        assert_eq!(a.len(), b.len());
        for (k, v) in &a {
            assert!((v - b[k]).abs() < 1e-12);
        }
    }

    fn upsert_smoke(s: &SqliteStore) {
        s.graph_upsert(
            &test_scope(),
            &GraphUpsertInput {
                nodes: vec![],
                edges: vec![GraphEdgeInput {
                    src: NodeRef {
                        kind: "person".into(),
                        name: "e".into(),
                    },
                    tag: "works_on".into(),
                    dst: NodeRef {
                        kind: "project".into(),
                        name: "open-clank".into(),
                    },
                    fact: None,
                    trust: None,
                }],
                cues: vec![],
            },
        )
        .unwrap();
    }
}

#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct CodeIndexResult {
    pub codebase: String,
    pub repository_id: String,
    pub run_id: String,
    pub source_snapshot: crate::code::CodeSourceSnapshot,
    pub files_indexed: usize,
    pub files_unchanged: usize,
    pub files_removed: usize,
    pub symbols: usize,
    pub errors: Vec<String>,
    pub coverage: Vec<crate::code::ScanCoverage>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct CodeStaleResult {
    pub codebase: String,
    pub repository_id: String,
    pub checked_files: usize,
    pub changed_files: Vec<String>,
    pub missing_files: Vec<String>,
    pub errors: Vec<String>,
    pub coverage: Vec<crate::code::ScanCoverage>,
}

#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct CodeIndexHead {
    pub run_id: String,
    pub status: String,
    pub source_snapshot: crate::code::CodeSourceSnapshot,
    pub published_at: String,
}

#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct CodeImpactResult {
    pub impacted_files: Vec<String>,
    pub total: usize,
    pub truncated: bool,
}

use serde::Serialize;

impl SqliteStore {
    fn codebase_key(scope: &GraphScope, codebase: &str) -> String {
        format!(
            "{}\u{1f}{}\u{1f}{codebase}",
            scope.owner, scope.workspace_id
        )
    }

    fn codebase_identity(codebase: &str) -> String {
        let path = std::path::Path::new(codebase);
        if path.is_dir() {
            crate::code::repository_id(path).unwrap_or_else(|_| codebase.to_string())
        } else {
            // Accept already-opaque IDs and preserve compatibility with
            // callers that stored an older path namespace.
            codebase.to_string()
        }
    }

    fn codebase_for_root(
        conn: &Connection,
        scope: &GraphScope,
        root: &std::path::Path,
    ) -> Result<String, String> {
        let opaque = crate::code::repository_id(root)?;
        let legacy = root.to_string_lossy().to_string();
        if opaque == legacy {
            return Ok(opaque);
        }
        let opaque_key = Self::codebase_key(scope, &opaque);
        let has_opaque: i64 = conn
            .query_row(
                "SELECT count(*) FROM code_files WHERE codebase=?1",
                params![opaque_key],
                |row| row.get(0),
            )
            .map_err(|e| e.to_string())?;
        if has_opaque > 0 {
            return Ok(opaque);
        }
        let legacy_key = Self::codebase_key(scope, &legacy);
        let has_legacy: i64 = conn
            .query_row(
                "SELECT count(*) FROM code_files WHERE codebase=?1",
                params![legacy_key],
                |row| row.get(0),
            )
            .map_err(|e| e.to_string())?;
        Ok(if has_legacy > 0 { legacy } else { opaque })
    }

    fn begin_code_index_run(
        &self,
        conn: &Connection,
        scope: &GraphScope,
        codebase: &str,
        snapshot: &crate::code::CodeSourceSnapshot,
    ) -> Result<String, String> {
        // Claim the run atomically. Stale-run recovery, the active-run check
        // and the insert must be one IMMEDIATE transaction: SQLite serializes
        // writers under BEGIN IMMEDIATE, so a second fm-mcp process blocks
        // (then fails the check) instead of racing the check-then-insert.
        // A partial unique index on status='running' would be stronger but
        // requires a schema bump, which is not available here.
        conn.execute_batch("BEGIN IMMEDIATE")
            .map_err(|e| format!("claim code-index run: {e}"))?;
        let claimed = self.begin_code_index_run_inner(conn, scope, codebase, snapshot);
        match claimed {
            Ok(run_id) => {
                conn.execute_batch("COMMIT")
                    .map_err(|e| format!("commit code-index run claim: {e}"))?;
                Ok(run_id)
            }
            Err(error) => {
                let _ = conn.execute_batch("ROLLBACK");
                Err(error)
            }
        }
    }

    fn begin_code_index_run_inner(
        &self,
        conn: &Connection,
        scope: &GraphScope,
        codebase: &str,
        snapshot: &crate::code::CodeSourceSnapshot,
    ) -> Result<String, String> {
        // A process crash cannot run a finally block. Recover only runs that
        // are older than the lease window; a recent run is still active and
        // must not be silently failed by a concurrent index request.
        let stale_before = (Utc::now()
            - chrono::Duration::seconds(self.code_index_lease_seconds() as i64))
        .to_rfc3339();
        conn.execute(
            "UPDATE code_index_runs
             SET status='failed', errors='[\"interrupted_before_completion\"]', finished_at=?1
             WHERE owner=?2 AND workspace_id=?3 AND codebase=?4 AND status='running'
               AND started_at < ?5",
            params![
                Utc::now().to_rfc3339(),
                scope.owner,
                scope.workspace_id,
                codebase,
                stale_before,
            ],
        )
        .map_err(|e| e.to_string())?;
        let active: Option<String> = conn
            .query_row(
                "SELECT run_id FROM code_index_runs
                 WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 AND status='running'
                 ORDER BY started_at DESC LIMIT 1",
                params![scope.owner, scope.workspace_id, codebase],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| e.to_string())?;
        if let Some(active) = active {
            return Err(format!("code-index run already active: {active}"));
        }
        let run_id = format!("codegen_{}", uuid::Uuid::new_v4().simple());
        let source_snapshot = serde_json::to_string(snapshot).map_err(|e| e.to_string())?;
        conn.execute(
            "INSERT INTO code_index_runs
             (owner, workspace_id, codebase, run_id, source_snapshot, status, started_at)
             VALUES (?1, ?2, ?3, ?4, ?5, 'running', ?6)",
            params![
                scope.owner,
                scope.workspace_id,
                codebase,
                run_id,
                source_snapshot,
                Utc::now().to_rfc3339()
            ],
        )
        .map_err(|e| e.to_string())?;
        Ok(run_id)
    }

    fn finish_code_index_run(
        conn: &Connection,
        scope: &GraphScope,
        codebase: &str,
        run_id: &str,
        status: &str,
        result: &CodeIndexResult,
    ) -> Result<(), String> {
        let errors = serde_json::to_string(&result.errors).map_err(|e| e.to_string())?;
        let coverage = serde_json::to_string(&result.coverage).map_err(|e| e.to_string())?;
        let changed = conn
            .execute(
                "UPDATE code_index_runs
                 SET status=?1, files_indexed=?2, files_unchanged=?3,
                     files_removed=?4, symbols=?5, errors=?6, coverage=?7,
                     finished_at=?8
                 WHERE owner=?9 AND workspace_id=?10 AND codebase=?11 AND run_id=?12",
                params![
                    status,
                    result.files_indexed as i64,
                    result.files_unchanged as i64,
                    result.files_removed as i64,
                    result.symbols as i64,
                    errors,
                    coverage,
                    Utc::now().to_rfc3339(),
                    scope.owner,
                    scope.workspace_id,
                    codebase,
                    run_id
                ],
            )
            .map_err(|e| e.to_string())?;
        if changed != 1 {
            return Err(format!("code-index run {run_id} was not found"));
        }
        if status == "ready" {
            conn.execute(
                "INSERT INTO code_index_heads
                 (owner, workspace_id, codebase, run_id, published_at)
                 VALUES (?1, ?2, ?3, ?4, ?5)
                 ON CONFLICT(owner, workspace_id, codebase) DO UPDATE SET
                    run_id=excluded.run_id, published_at=excluded.published_at",
                params![
                    scope.owner,
                    scope.workspace_id,
                    codebase,
                    run_id,
                    Utc::now().to_rfc3339()
                ],
            )
            .map_err(|e| e.to_string())?;
        }
        Ok(())
    }

    fn code_delete_file_nodes(
        conn: &Connection,
        scope: &GraphScope,
        codebase: &str,
        rel_path: &str,
    ) -> rusqlite::Result<()> {
        let prefix = format!("{codebase}::{rel_path}");
        // file node + its symbols share the FQN prefix
        let ids: Vec<String> = {
            let mut stmt = conn.prepare(
                "SELECT id FROM graph_nodes
                 WHERE (name = ?1 OR name LIKE ?2) AND owner = ?3 AND workspace_id = ?4",
            )?;
            let rows = stmt.query_map(
                params![
                    prefix,
                    format!("{prefix}::%"),
                    scope.owner,
                    scope.workspace_id
                ],
                |r| r.get(0),
            )?;
            rows.collect::<Result<_, _>>()?
        };
        for id in &ids {
            conn.execute(
                "DELETE FROM graph_edges WHERE src_id = ?1 OR dst_id = ?1",
                params![id],
            )?;
            conn.execute("DELETE FROM graph_cues WHERE node_id = ?1", params![id])?;
            let _ = conn.execute("DELETE FROM graph_cues_fts WHERE node_id = ?1", params![id]);
            conn.execute("DELETE FROM graph_nodes WHERE id = ?1", params![id])?;
        }
        Ok(())
    }

    /// Index (or re-index) a codebase root. Incremental: unchanged files
    /// (content hash, mtime_ns, and size match) are skipped; changed files
    /// cascade-delete their old nodes first; files gone from disk are swept.
    /// Mtime/size are hints only — same-size edits with preserved timestamps
    /// are detected by hashing before the skip decision. OPT-IN ONLY —
    /// nothing calls this except the code_index tool.
    pub fn code_stale(
        &self,
        scope: &GraphScope,
        root: &std::path::Path,
    ) -> Result<CodeStaleResult, String> {
        let codebase = {
            let conn = self.conn.lock().unwrap();
            Self::codebase_for_root(&conn, scope, root)?
        };
        let codebase_key = Self::codebase_key(scope, &codebase);
        let scan = crate::code::scan_codebase_detailed(root)?;
        let files = scan.files;
        let known: HashMap<String, (String, i64, i64)> = {
            let conn = self.conn.lock().unwrap();
            let mut stmt = conn
                .prepare(
                    "SELECT rel_path, blake3, mtime_ns, size FROM code_files WHERE codebase = ?1",
                )
                .map_err(|e| e.to_string())?;
            let rows = stmt
                .query_map(params![codebase_key], |r| {
                    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
                })
                .map_err(|e| e.to_string())?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(|e| e.to_string())?
                .into_iter()
                .map(|(path, hash, mtime, size)| (path, (hash, mtime, size)))
                .collect()
        };

        let mut result = CodeStaleResult {
            codebase: codebase.clone(),
            repository_id: codebase.clone(),
            checked_files: 0,
            changed_files: Vec::new(),
            missing_files: Vec::new(),
            errors: Vec::new(),
            coverage: scan.coverage,
        };
        let mut seen = HashSet::new();
        for path in files {
            let rel = path
                .strip_prefix(root)
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_default();
            seen.insert(rel.clone());
            let bytes = match std::fs::read(&path) {
                Ok(bytes) => bytes,
                Err(e) => {
                    result.errors.push(format!("{rel}: {e}"));
                    continue;
                }
            };
            let meta = match std::fs::metadata(&path) {
                Ok(meta) => meta,
                Err(e) => {
                    result.errors.push(format!("{rel}: {e}"));
                    continue;
                }
            };
            result.checked_files += 1;
            let mtime_ns = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_nanos() as i64)
                .unwrap_or(0);
            let current = (
                blake3::hash(&bytes).to_hex().to_string(),
                mtime_ns,
                meta.len() as i64,
            );
            if known.get(&rel) != Some(&current) {
                result.changed_files.push(rel);
            }
        }
        result.missing_files = known
            .keys()
            .filter(|path| !seen.contains(*path))
            .cloned()
            .collect();
        result.changed_files.sort();
        result.missing_files.sort();
        Ok(result)
    }

    pub fn code_index(
        &self,
        scope: &GraphScope,
        root: &std::path::Path,
    ) -> Result<CodeIndexResult, String> {
        let codebase = {
            let conn = self.conn.lock().unwrap();
            Self::codebase_for_root(&conn, scope, root)?
        };
        let codebase_key = Self::codebase_key(scope, &codebase);
        let scan = crate::code::scan_codebase_detailed(root)?;
        let files = scan.files;
        let source_snapshot = crate::code::source_snapshot(root, &files)?;
        let run_id = {
            let conn = self.conn.lock().unwrap();
            self.begin_code_index_run(&conn, scope, &codebase, &source_snapshot)?
        };
        let mut result = CodeIndexResult {
            codebase: codebase.clone(),
            repository_id: codebase.clone(),
            run_id: run_id.clone(),
            source_snapshot,
            files_indexed: 0,
            files_unchanged: 0,
            files_removed: 0,
            symbols: 0,
            errors: vec![],
            coverage: scan.coverage,
        };

        let mut coverage_index: HashMap<String, usize> = result
            .coverage
            .iter()
            .enumerate()
            .map(|(index, entry)| (entry.rel_path.clone(), index))
            .collect();
        let set_coverage = |coverage: &mut Vec<crate::code::ScanCoverage>,
                            indexes: &mut HashMap<String, usize>,
                            rel: &str,
                            status: &str,
                            detail: Option<String>| {
            if let Some(index) = indexes.get(rel).copied() {
                coverage[index].status = status.to_string();
                coverage[index].detail = detail;
            } else {
                indexes.insert(rel.to_string(), coverage.len());
                coverage.push(crate::code::ScanCoverage {
                    rel_path: rel.to_string(),
                    status: status.to_string(),
                    detail,
                });
            }
        };

        let known: Vec<(String, String, i64, i64)> = {
            let conn = self.conn.lock().unwrap();
            let mut stmt = conn
                .prepare(
                    "SELECT rel_path, blake3, mtime_ns, size FROM code_files WHERE codebase = ?1",
                )
                .map_err(|e| e.to_string())?;
            let rows = stmt
                .query_map(params![codebase_key], |r| {
                    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
                })
                .map_err(|e| e.to_string())?;
            rows.collect::<Result<_, _>>().map_err(|e| e.to_string())?
        };
        let known_map: HashMap<String, (String, i64, i64)> = known
            .into_iter()
            .map(|(p, hash, mtime, size)| (p, (hash, mtime, size)))
            .collect();

        let mut seen: HashSet<String> = HashSet::new();
        let mut staged: Vec<crate::code::IndexedFile> = Vec::new();
        for path in files {
            let rel = path
                .strip_prefix(root)
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_default();
            seen.insert(rel.clone());
            let bytes = match std::fs::read(&path) {
                Ok(bytes) => bytes,
                Err(e) => {
                    result.errors.push(format!("{rel}: {e}"));
                    set_coverage(
                        &mut result.coverage,
                        &mut coverage_index,
                        &rel,
                        "permission_denied",
                        Some(e.to_string()),
                    );
                    continue;
                }
            };
            let meta = match std::fs::metadata(&path) {
                Ok(meta) => meta,
                Err(e) => {
                    result.errors.push(format!("{rel}: {e}"));
                    set_coverage(
                        &mut result.coverage,
                        &mut coverage_index,
                        &rel,
                        "permission_denied",
                        Some(e.to_string()),
                    );
                    continue;
                }
            };
            let mtime_ns = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_nanos() as i64)
                .unwrap_or(0);
            let current_hash = blake3::hash(&bytes).to_hex().to_string();
            if known_map.get(&rel) == Some(&(current_hash.clone(), mtime_ns, meta.len() as i64)) {
                result.files_unchanged += 1;
                set_coverage(
                    &mut result.coverage,
                    &mut coverage_index,
                    &rel,
                    "unchanged",
                    None,
                );
                continue;
            }
            match crate::code::index_file(&codebase, root, &path) {
                Ok(indexed) if indexed.blake3 != current_hash => {
                    let detail = "source changed while indexing; retry against a stable snapshot";
                    result.errors.push(format!("{rel}: {detail}"));
                    set_coverage(
                        &mut result.coverage,
                        &mut coverage_index,
                        &rel,
                        "parse_failed",
                        Some(detail.into()),
                    );
                }
                Ok(indexed) => {
                    staged.push(indexed);
                }
                Err(e) => {
                    result.errors.push(format!("{rel}: {e}"));
                    set_coverage(
                        &mut result.coverage,
                        &mut coverage_index,
                        &rel,
                        "parse_failed",
                        Some(e),
                    );
                }
            }
        }

        // Sweep files that vanished from disk.
        let gone: Vec<String> = known_map
            .keys()
            .filter(|k| !seen.contains(*k))
            .cloned()
            .collect();
        let conn = self.conn.lock().unwrap();
        // Per-file failure policy: tolerate and report. A file that cannot be
        // read or parsed (permission error, mid-edit mutation, unsupported
        // construct) is recorded in `result.errors` and marked in coverage,
        // but does NOT abort the run — the files that did index are published
        // and the failed files stay absent from `code_files`, so the next
        // incremental run retries them. Only structural failures (database
        // errors below) fail the run and preserve the prior ready generation.
        if let Err(error) = conn.execute_batch("BEGIN IMMEDIATE") {
            let _ =
                Self::finish_code_index_run(&conn, scope, &codebase, &run_id, "failed", &result);
            return Err(error.to_string());
        }
        let committed = (|| -> rusqlite::Result<()> {
            for indexed in &staged {
                Self::code_delete_file_nodes(&conn, scope, &codebase, &indexed.rel_path)?;
                Self::graph_upsert_inner(&conn, scope, &indexed.upsert)?;
                conn.execute(
                    "INSERT OR REPLACE INTO code_files
                     (codebase, rel_path, blake3, mtime_ns, size, symbol_count, indexed_at)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                    params![
                        codebase_key,
                        indexed.rel_path,
                        indexed.blake3,
                        indexed.mtime_ns,
                        indexed.size,
                        indexed.symbol_count as i64,
                        Utc::now().to_rfc3339()
                    ],
                )?;
            }
            for rel in &gone {
                Self::code_delete_file_nodes(&conn, scope, &codebase, rel)?;
                conn.execute(
                    "DELETE FROM code_files WHERE codebase = ?1 AND rel_path = ?2",
                    params![codebase_key, rel],
                )?;
            }
            result.files_indexed = staged.len();
            result.symbols = staged.iter().map(|item| item.symbol_count).sum();
            result.files_removed = gone.len();
            for indexed in &staged {
                set_coverage(
                    &mut result.coverage,
                    &mut coverage_index,
                    &indexed.rel_path,
                    "indexed",
                    None,
                );
            }
            for rel in &gone {
                set_coverage(
                    &mut result.coverage,
                    &mut coverage_index,
                    rel,
                    "deleted",
                    Some("previously indexed file is absent".into()),
                );
            }
            // The ready pointer and its manifest are committed with the graph
            // rows. A crash before COMMIT leaves both the old graph and old
            // pointer live; a failure is recorded after rollback below.
            Self::finish_code_index_run(&conn, scope, &codebase, &run_id, "ready", &result)
                .map_err(|error| {
                    rusqlite::Error::ToSqlConversionFailure(Box::new(std::io::Error::other(error)))
                })?;
            Ok(())
        })();
        if let Err(error) = committed {
            let _ = conn.execute_batch("ROLLBACK");
            let _ =
                Self::finish_code_index_run(&conn, scope, &codebase, &run_id, "failed", &result);
            return Err(format!("publishing code index failed: {error}"));
        }
        if let Err(error) = conn.execute_batch("COMMIT") {
            let _ = conn.execute_batch("ROLLBACK");
            let _ =
                Self::finish_code_index_run(&conn, scope, &codebase, &run_id, "failed", &result);
            return Err(error.to_string());
        }
        Ok(result)
    }

    pub fn code_status(
        &self,
        scope: &GraphScope,
        codebase: &str,
    ) -> rusqlite::Result<(usize, usize, Option<String>)> {
        let conn = self.conn.lock().unwrap();
        let codebase = if std::path::Path::new(codebase).is_dir() {
            Self::codebase_for_root(&conn, scope, std::path::Path::new(codebase))
                .map_err(|_| rusqlite::Error::InvalidQuery)?
        } else {
            Self::codebase_identity(codebase)
        };
        let codebase_key = Self::codebase_key(scope, &codebase);
        let (files, symbols): (i64, i64) = conn.query_row(
            "SELECT count(*), COALESCE(SUM(symbol_count),0) FROM code_files WHERE codebase = ?1",
            params![codebase_key],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        let last: Option<String> = conn
            .query_row(
                "SELECT MAX(indexed_at) FROM code_files WHERE codebase = ?1",
                params![codebase_key],
                |r| r.get(0),
            )
            .optional()?
            .flatten();
        Ok((files as usize, symbols as usize, last))
    }

    /// Return the last atomically published run for a scoped repository.  A
    /// failed or interrupted run never replaces this pointer.
    pub fn code_index_head(
        &self,
        scope: &GraphScope,
        codebase: &str,
    ) -> rusqlite::Result<Option<CodeIndexHead>> {
        let conn = self.conn.lock().unwrap();
        let codebase = if std::path::Path::new(codebase).is_dir() {
            Self::codebase_for_root(&conn, scope, std::path::Path::new(codebase))
                .map_err(|_| rusqlite::Error::InvalidQuery)?
        } else {
            Self::codebase_identity(codebase)
        };
        let row = conn
            .query_row(
                "SELECT h.run_id, r.status, r.source_snapshot, h.published_at
                 FROM code_index_heads h
                 JOIN code_index_runs r
                   ON r.owner=h.owner AND r.workspace_id=h.workspace_id
                  AND r.codebase=h.codebase AND r.run_id=h.run_id
                 WHERE h.owner=?1 AND h.workspace_id=?2 AND h.codebase=?3",
                params![scope.owner, scope.workspace_id, codebase],
                |row| {
                    let snapshot: String = row.get(2)?;
                    let snapshot = serde_json::from_str(&snapshot).map_err(|error| {
                        rusqlite::Error::FromSqlConversionFailure(
                            snapshot.len(),
                            rusqlite::types::Type::Text,
                            Box::new(error),
                        )
                    })?;
                    Ok(CodeIndexHead {
                        run_id: row.get(0)?,
                        status: row.get(1)?,
                        source_snapshot: snapshot,
                        published_at: row.get(3)?,
                    })
                },
            )
            .optional()?;
        Ok(row)
    }

    pub fn code_remove(&self, scope: &GraphScope, codebase: &str) -> Result<usize, String> {
        let codebase = if std::path::Path::new(codebase).is_dir() {
            let conn = self.conn.lock().unwrap();
            Self::codebase_for_root(&conn, scope, std::path::Path::new(codebase))?
        } else {
            Self::codebase_identity(codebase)
        };
        let codebase_key = Self::codebase_key(scope, &codebase);
        let rels: Vec<String> = {
            let conn = self.conn.lock().unwrap();
            let mut stmt = conn
                .prepare("SELECT rel_path FROM code_files WHERE codebase = ?1")
                .map_err(|e| e.to_string())?;
            let rows = stmt
                .query_map(params![codebase_key], |r| r.get(0))
                .map_err(|e| e.to_string())?;
            rows.collect::<Result<_, _>>().map_err(|e| e.to_string())?
        };
        let conn = self.conn.lock().unwrap();
        for rel in &rels {
            Self::code_delete_file_nodes(&conn, scope, &codebase, rel)
                .map_err(|e| e.to_string())?;
        }
        // Modules and callables are codebase-scoped (not file-prefixed) —
        // sweep whatever remains under the namespace.
        let leftovers: Vec<String> = {
            let mut stmt = conn
                .prepare(
                    "SELECT id FROM graph_nodes
                     WHERE name LIKE ?1 AND owner = ?2 AND workspace_id = ?3",
                )
                .map_err(|e| e.to_string())?;
            let rows = stmt
                .query_map(
                    params![format!("{codebase}::%"), scope.owner, scope.workspace_id],
                    |r| r.get(0),
                )
                .map_err(|e| e.to_string())?;
            rows.collect::<Result<_, _>>().map_err(|e| e.to_string())?
        };
        for id in &leftovers {
            conn.execute(
                "DELETE FROM graph_edges WHERE src_id = ?1 OR dst_id = ?1",
                params![id],
            )
            .map_err(|e| e.to_string())?;
            conn.execute("DELETE FROM graph_cues WHERE node_id = ?1", params![id])
                .map_err(|e| e.to_string())?;
            let _ = conn.execute("DELETE FROM graph_cues_fts WHERE node_id = ?1", params![id]);
            conn.execute("DELETE FROM graph_nodes WHERE id = ?1", params![id])
                .map_err(|e| e.to_string())?;
        }
        conn.execute(
            "DELETE FROM code_files WHERE codebase = ?1",
            params![codebase_key],
        )
        .map_err(|e| e.to_string())?;
        // A repository removal is also a projection forget: no stale ready
        // pointer or run manifest may resurrect the deleted namespace in a
        // later status/recovery call.
        conn.execute(
            "DELETE FROM code_index_heads WHERE owner=?1 AND workspace_id=?2 AND codebase=?3",
            params![scope.owner, scope.workspace_id, codebase],
        )
        .map_err(|e| e.to_string())?;
        conn.execute(
            "DELETE FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3",
            params![scope.owner, scope.workspace_id, codebase],
        )
        .map_err(|e| e.to_string())?;
        Ok(rels.len())
    }

    /// Which files transitively import `rel_path`? Reverse-imports BFS —
    /// the cheap blast-radius query.
    pub fn code_impact(
        &self,
        scope: &GraphScope,
        codebase: &str,
        rel_path: &str,
        max_depth: usize,
    ) -> rusqlite::Result<Vec<String>> {
        let conn = self.conn.lock().unwrap();
        let codebase = if std::path::Path::new(codebase).is_dir() {
            Self::codebase_for_root(&conn, scope, std::path::Path::new(codebase))
                .map_err(|_| rusqlite::Error::InvalidQuery)?
        } else {
            Self::codebase_identity(codebase)
        };
        // module nodes that this file could be imported as: match rel_path
        // stem against module node names (suffix match — import strings are
        // rarely full paths).
        let stem = std::path::Path::new(rel_path)
            .file_stem()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_else(|| rel_path.to_string());
        let mut targets: Vec<String> = {
            let mut stmt = conn.prepare(
                "SELECT id FROM graph_nodes
                 WHERE kind = 'module' AND name LIKE ?1 AND name LIKE ?2
                   AND owner = ?3 AND workspace_id = ?4",
            )?;
            let rows = stmt.query_map(
                params![
                    format!("{codebase}::%"),
                    format!("%{stem}%"),
                    scope.owner,
                    scope.workspace_id,
                ],
                |r| r.get(0),
            )?;
            rows.collect::<Result<_, _>>()?
        };
        // plus the file node itself
        targets.push(scope.node_id("file", &format!("{codebase}::{rel_path}")));

        let mut impacted: HashSet<String> = HashSet::new();
        let mut frontier = targets;
        let mut stmt = conn.prepare(
            "SELECT n.name FROM graph_edges e JOIN graph_nodes n ON n.id = e.src_id
             WHERE e.tag = 'imports' AND e.dst_id = ?1
               AND e.owner = ?2 AND e.workspace_id = ?3
               AND n.owner = ?2 AND n.workspace_id = ?3",
        )?;
        for _ in 0..max_depth {
            let mut next = Vec::new();
            for target in frontier.drain(..) {
                let importers: Vec<String> = stmt
                    .query_map(params![target, scope.owner, scope.workspace_id], |r| {
                        r.get(0)
                    })?
                    .collect::<Result<_, _>>()?;
                for name in importers {
                    if impacted.insert(name.clone()) {
                        next.push(scope.node_id("file", &name));
                    }
                }
            }
            if next.is_empty() {
                break;
            }
            frontier = next;
        }
        let mut out: Vec<String> = impacted.into_iter().collect();
        out.sort();
        Ok(out)
    }

    /// Bounded impact view with explicit accounting. The legacy unbounded
    /// method remains available for internal callers; MCP uses this report so
    /// a capped answer cannot be mistaken for the complete blast radius.
    pub fn code_impact_bounded(
        &self,
        scope: &GraphScope,
        codebase: &str,
        rel_path: &str,
        max_depth: usize,
        limit: usize,
    ) -> rusqlite::Result<CodeImpactResult> {
        let all = self.code_impact(scope, codebase, rel_path, max_depth)?;
        let total = all.len();
        let cap = limit.max(1);
        let truncated = total > cap;
        Ok(CodeImpactResult {
            impacted_files: all.into_iter().take(cap).collect(),
            total,
            truncated,
        })
    }
}

#[cfg(test)]
mod code_index_tests {
    use super::*;
    use std::io::Write;

    fn fixture_repo() -> tempdir_like::TempRepo {
        tempdir_like::TempRepo::new()
    }

    mod tempdir_like {
        use std::path::PathBuf;
        use std::sync::atomic::{AtomicUsize, Ordering};

        static NEXT_ID: AtomicUsize = AtomicUsize::new(0);

        pub struct TempRepo(pub PathBuf);
        impl TempRepo {
            pub fn new() -> Self {
                let dir = std::env::temp_dir().join(format!(
                    "fm-code-fixture-{}-{}",
                    std::process::id(),
                    NEXT_ID.fetch_add(1, Ordering::Relaxed)
                ));
                std::fs::create_dir_all(&dir).unwrap();
                Self(dir)
            }
        }
        impl Drop for TempRepo {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
    }

    fn write(root: &std::path::Path, rel: &str, content: &str) {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let mut f = std::fs::File::create(path).unwrap();
        f.write_all(content.as_bytes()).unwrap();
    }

    #[test]
    fn stale_reports_content_changes_and_missing_files() {
        let repo = fixture_repo();
        let root = &repo.0;
        write(root, "src/lib.py", "def core_helper():\n    return 1\n");
        let store = SqliteStore::memory(4).unwrap();
        store.code_index(&test_scope(), root).unwrap();

        let clean = store.code_stale(&test_scope(), root).unwrap();
        assert!(
            clean.changed_files.is_empty(),
            "fresh index should be clean: {clean:?}"
        );

        write(root, "src/lib.py", "def core_helper():\n    return 2\n");
        let changed = store.code_stale(&test_scope(), root).unwrap();
        assert_eq!(changed.changed_files, vec!["src/lib.py"]);

        std::fs::remove_file(root.join("src/lib.py")).unwrap();
        let stale = store.code_stale(&test_scope(), root).unwrap();
        assert!(stale.changed_files.is_empty());
        assert_eq!(stale.missing_files, vec!["src/lib.py"]);
    }

    #[test]
    fn index_status_impact_remove_cycle() {
        let repo = fixture_repo();
        let root = &repo.0;
        write(root, "src/lib.py", "def core_helper():\n    return 1\n");
        write(
            root,
            "src/app.py",
            "from lib import core_helper\ndef main():\n    core_helper()\n",
        );

        let s = SqliteStore::memory(4).unwrap();
        let result = s.code_index(&test_scope(), root).unwrap();
        assert_eq!(result.files_indexed, 2);
        assert!(result.repository_id.starts_with("repo_"));
        assert_eq!(result.symbols, 2, "core_helper + main");
        assert!(result.errors.is_empty(), "{:?}", result.errors);

        let codebase = root.to_string_lossy().to_string();
        let (files, symbols, last) = s.code_status(&test_scope(), &codebase).unwrap();
        assert_eq!(files, 2);
        assert_eq!(symbols, 2);
        assert!(last.is_some());

        // cues from identifiers findable through the normal graph surface
        let hits = s.graph_cues(&test_scope(), "core helper", 10).unwrap();
        assert!(hits.iter().any(|h| h.node.name.ends_with("::core_helper")));

        // incremental: nothing changed → nothing re-indexed
        let again = s.code_index(&test_scope(), root).unwrap();
        assert_eq!(again.files_indexed, 0);
        assert_eq!(again.files_unchanged, 2);

        // impact: app.py imports lib → touching lib.py impacts app.py
        let impacted = s
            .code_impact(&test_scope(), &codebase, "src/lib.py", 4)
            .unwrap();
        assert!(
            impacted.iter().any(|f| f.ends_with("src/app.py")),
            "expected app.py in impact set, got {impacted:?}"
        );
        let bounded = s
            .code_impact_bounded(&test_scope(), &codebase, "src/lib.py", 4, 1)
            .unwrap();
        assert_eq!(bounded.total, impacted.len());
        assert_eq!(bounded.impacted_files.len(), 1);
        assert_eq!(bounded.truncated, impacted.len() > 1);

        // removal sweeps everything
        let removed = s.code_remove(&test_scope(), &codebase).unwrap();
        assert_eq!(removed, 2);
        let conn = s.conn.lock().unwrap();
        let left: i64 = conn
            .query_row(
                "SELECT count(*) FROM graph_nodes WHERE name LIKE ?1",
                params![format!("{codebase}%")],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(left, 0);
        drop(conn);
        assert!(s
            .code_index_head(&test_scope(), &codebase)
            .unwrap()
            .is_none());
    }

    #[test]
    fn vanished_files_are_swept_on_reindex() {
        let repo = fixture_repo();
        let root = &repo.0;
        write(root, "a.rs", "pub fn alpha() {}\n");
        write(root, "b.rs", "pub fn beta() {}\n");
        let s = SqliteStore::memory(4).unwrap();
        assert_eq!(s.code_index(&test_scope(), root).unwrap().files_indexed, 2);

        std::fs::remove_file(root.join("b.rs")).unwrap();
        let result = s.code_index(&test_scope(), root).unwrap();
        assert_eq!(result.files_removed, 1);
        let hits = s.graph_cues(&test_scope(), "beta", 10).unwrap();
        assert!(hits.is_empty(), "beta's cues must be gone");
    }

    #[test]
    fn same_size_content_change_is_not_hidden_by_metadata_hint() {
        let repo = fixture_repo();
        let root = &repo.0;
        write(root, "stable.py", "def alpha():\n    return 1\n");
        let s = SqliteStore::memory(4).unwrap();
        assert_eq!(s.code_index(&test_scope(), root).unwrap().files_indexed, 1);

        // Keep the byte length identical. The index must compare the stored
        // content digest before treating a metadata match as unchanged.
        write(root, "stable.py", "def alpha():\n    return 2\n");
        let result = s.code_index(&test_scope(), root).unwrap();
        assert_eq!(result.files_indexed, 1, "content mutation must re-index");
        assert_eq!(result.files_unchanged, 0);
    }

    #[test]
    fn code_index_reports_non_selected_scan_coverage() {
        let repo = fixture_repo();
        let root = &repo.0;
        write(root, "good.py", "def good():\n    return 1\n");
        write(root, "README.md", "documentation\n");
        write(root, "binary.rs", "placeholder\n");
        std::fs::write(root.join("binary.rs"), [b'f', 0, b'x']).unwrap();
        std::fs::write(root.join("huge.py"), vec![b'x'; 512 * 1024 + 1]).unwrap();
        write(root, "target/ignored.py", "def ignored(): pass\n");

        let store = SqliteStore::memory(4).unwrap();
        let result = store.code_index(&test_scope(), root).unwrap();
        let statuses: HashMap<_, _> = result
            .coverage
            .iter()
            .map(|entry| (entry.rel_path.as_str(), entry.status.as_str()))
            .collect();
        assert_eq!(statuses.get("good.py"), Some(&"indexed"));
        assert_eq!(statuses.get("README.md"), Some(&"unsupported"));
        assert_eq!(statuses.get("binary.rs"), Some(&"binary"));
        assert_eq!(statuses.get("huge.py"), Some(&"oversize"));
        assert_eq!(statuses.get("target"), Some(&"ignored"));
    }

    #[test]
    fn code_index_publishes_a_path_free_ready_generation() {
        let repo = fixture_repo();
        let root = &repo.0;
        write(root, "main.py", "def main():\n    return 1\n");
        let store = SqliteStore::memory(4).unwrap();

        let first = store.code_index(&test_scope(), root).unwrap();
        let head = store
            .code_index_head(&test_scope(), root.to_string_lossy().as_ref())
            .unwrap()
            .expect("successful index must publish a head");
        assert_eq!(head.run_id, first.run_id);
        assert_eq!(head.status, "ready");
        assert_eq!(head.source_snapshot.repository_id, first.repository_id);
        assert!(!head.source_snapshot.file_manifest_digest.is_empty());
        let root_text = root.to_string_lossy();
        assert!(!serde_json::to_string(&head)
            .unwrap()
            .contains(root_text.as_ref()));

        // Simulate a process death after run creation. The next explicit
        // request must close that abandoned run without moving the ready head.
        let abandoned_started_at = (Utc::now()
            - chrono::Duration::seconds(store.code_index_lease_seconds() as i64 + 1))
        .to_rfc3339();
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO code_index_runs
                 (owner, workspace_id, codebase, run_id, source_snapshot, status, started_at)
                 VALUES (?1, ?2, ?3, 'codegen_abandoned', ?4, 'running', ?5)",
                params![
                    test_scope().owner,
                    test_scope().workspace_id,
                    first.codebase,
                    serde_json::to_string(&first.source_snapshot).unwrap(),
                    abandoned_started_at
                ],
            )
            .unwrap();
        }

        // A same-size content change must produce a new immutable run and
        // move only the ready pointer; the prior run remains auditable.
        write(root, "main.py", "def main():\n    return 2\n");
        let second = store.code_index(&test_scope(), root).unwrap();
        assert_ne!(first.run_id, second.run_id);
        assert_ne!(
            first.source_snapshot.file_manifest_digest,
            second.source_snapshot.file_manifest_digest
        );
        let current = store
            .code_index_head(&test_scope(), root.to_string_lossy().as_ref())
            .unwrap()
            .unwrap();
        assert_eq!(current.run_id, second.run_id);

        let conn = store.conn.lock().unwrap();
        let (runs, ready): (i64, i64) = conn
            .query_row(
                "SELECT count(*), sum(status='ready') FROM code_index_runs
                 WHERE owner=?1 AND workspace_id=?2 AND codebase=?3",
                params![
                    test_scope().owner,
                    test_scope().workspace_id,
                    second.codebase
                ],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(runs, 3);
        assert_eq!(ready, 2);
        let abandoned: String = conn
            .query_row(
                "SELECT status FROM code_index_runs WHERE run_id='codegen_abandoned'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(abandoned, "failed");
    }

    #[test]
    fn code_index_does_not_fail_a_recent_concurrent_run() {
        let repo = fixture_repo();
        let root = &repo.0;
        write(root, "main.py", "def main():\n    return 1\n");
        let store = SqliteStore::memory(4).unwrap();
        let first = store.code_index(&test_scope(), root).unwrap();
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO code_index_runs
                 (owner, workspace_id, codebase, run_id, source_snapshot, status, started_at)
                 VALUES (?1, ?2, ?3, 'codegen_active', ?4, 'running', ?5)",
                params![
                    test_scope().owner,
                    test_scope().workspace_id,
                    first.codebase,
                    serde_json::to_string(&first.source_snapshot).unwrap(),
                    Utc::now().to_rfc3339(),
                ],
            )
            .unwrap();
        }

        let error = store.code_index(&test_scope(), root).unwrap_err();
        assert!(error.contains("code-index run already active: codegen_active"));
        let conn = store.conn.lock().unwrap();
        let status: String = conn
            .query_row(
                "SELECT status FROM code_index_runs WHERE run_id='codegen_active'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(status, "running");
    }

    #[cfg(unix)]
    #[test]
    fn code_index_tolerates_and_reports_per_file_failures() {
        use std::os::unix::fs::PermissionsExt;

        let repo = fixture_repo();
        let root = &repo.0;
        write(root, "good.py", "def good():\n    return 1\n");
        write(root, "bad.py", "def bad():\n    return 2\n");
        std::fs::set_permissions(root.join("bad.py"), std::fs::Permissions::from_mode(0o000))
            .unwrap();

        let store = SqliteStore::memory(4).unwrap();
        let result = store
            .code_index(&test_scope(), root)
            .expect("one unreadable file must not fail the run");
        assert_eq!(result.files_indexed, 1, "good.py still indexed");
        assert!(
            result.errors.iter().any(|e| e.starts_with("bad.py: ")),
            "failure must be reported in errors: {:?}",
            result.errors
        );
        let coverage: HashMap<_, _> = result
            .coverage
            .iter()
            .map(|entry| (entry.rel_path.as_str(), entry.status.as_str()))
            .collect();
        assert_eq!(coverage.get("good.py"), Some(&"indexed"));
        assert_eq!(coverage.get("bad.py"), Some(&"permission_denied"));

        // The partial generation is published and the failed file is retried
        // once it becomes readable again.
        let head = store
            .code_index_head(&test_scope(), root.to_string_lossy().as_ref())
            .unwrap()
            .expect("a run with per-file failures still publishes a head");
        assert_eq!(head.run_id, result.run_id);
        std::fs::set_permissions(root.join("bad.py"), std::fs::Permissions::from_mode(0o644))
            .unwrap();
        let retried = store.code_index(&test_scope(), root).unwrap();
        assert_eq!(retried.files_indexed, 1, "bad.py indexed on retry");
        assert!(retried.errors.is_empty(), "{:?}", retried.errors);
    }

    #[test]
    fn code_index_run_claim_is_exclusive_across_connections() {
        let repo = fixture_repo();
        let root = &repo.0;
        write(root, "main.py", "def main():\n    return 1\n");
        let db = std::env::temp_dir().join(format!("fm-code-claim-{}.db", std::process::id()));
        let db_path = db.to_string_lossy().to_string();

        // Two stores on one database file stand in for two fm-mcp processes.
        let store_a = SqliteStore::new(&db_path, 4).unwrap();
        let store_b = SqliteStore::new(&db_path, 4).unwrap();
        let snapshot = crate::code::CodeSourceSnapshot {
            repository_id: "repo_test".into(),
            git_commit: None,
            git_ref: None,
            git_dirty: false,
            file_manifest_digest: "digest".into(),
        };
        let codebase = root.to_string_lossy().to_string();

        let run_a = {
            let conn = store_a.conn.lock().unwrap();
            store_a
                .begin_code_index_run(&conn, &test_scope(), &codebase, &snapshot)
                .unwrap()
        };
        // The second process must lose the claim: the active-run check now
        // runs inside the same IMMEDIATE transaction as the insert, so it
        // cannot observe the pre-claim state.
        let error = {
            let conn = store_b.conn.lock().unwrap();
            store_b
                .begin_code_index_run(&conn, &test_scope(), &codebase, &snapshot)
                .unwrap_err()
        };
        assert!(
            error.contains(&format!("code-index run already active: {run_a}")),
            "second claim must fail, got: {error}"
        );

        drop(store_a);
        drop(store_b);
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{db_path}{suffix}"));
        }
    }

    #[cfg(unix)]
    #[test]
    fn code_scan_never_follows_symlinked_files_or_directories() {
        use std::os::unix::fs::symlink;

        let repo = fixture_repo();
        let outside = fixture_repo();
        write(&outside.0, "escape.py", "def escape():\n    return 1\n");
        write(&repo.0, "inside.py", "def inside():\n    return 1\n");
        symlink(outside.0.join("escape.py"), repo.0.join("escape.py")).unwrap();
        symlink(&outside.0, repo.0.join("outside-dir")).unwrap();

        let files = crate::code::scan_codebase(&repo.0).unwrap();
        assert_eq!(files, vec![repo.0.join("inside.py")]);
    }

    #[cfg(unix)]
    #[test]
    fn code_scan_rejects_a_symlinked_root() {
        use std::os::unix::fs::symlink;

        let repo = fixture_repo();
        let link = repo.0.with_extension("-root-link");
        symlink(&repo.0, &link).unwrap();
        let error = crate::code::scan_codebase(&link).unwrap_err();
        assert!(error.contains("real directory"));
        let _ = std::fs::remove_file(link);
    }
}
