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
    pub fn code_workspace_checkout(&self,scope:&GraphScope,binding:&crate::code::WorkspaceBinding)->Result<Option<String>,String> {
        let checkouts=self.code_workspace_checkouts(scope,binding)?;
        if checkouts.len()>1 {return Err("ambiguous_registration: explicit checkout required".into());}
        Ok(checkouts.into_iter().next())
    }
    pub fn code_workspace_checkouts(&self,scope:&GraphScope,binding:&crate::code::WorkspaceBinding)->Result<Vec<String>,String> {
        binding.validate_cleanup()?;
        let conn=self.conn.lock().unwrap();
        let checkouts:Vec<String>=conn.prepare("SELECT checkout_id FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND owner_subject_id=?3 AND location_id=?4").map_err(|e|e.to_string())?.query_map(params![scope.owner,scope.workspace_id,binding.owner_subject_id,binding.location_id],|r|r.get(0)).map_err(|e|e.to_string())?.collect::<Result<_,_>>().map_err(|e|e.to_string())?;
        Ok(checkouts)
    }
    pub fn code_workspace_prepare(&self,scope:&GraphScope,binding:&crate::code::WorkspaceBinding,admit:bool)->Result<serde_json::Value,String> {
        let _authority=binding.publication_guard()?;
        if binding.owner!=scope.owner || binding.workspace_id!=scope.workspace_id {return Err("denied: lifecycle scope differs".into());}
        let conn=self.conn.lock().unwrap();
        conn.execute_batch("BEGIN IMMEDIATE").map_err(|e|e.to_string())?;
        let prepared=(|| {
        if admit {
            conn.execute("INSERT INTO code_scope_epochs(owner,workspace_id,admission_epoch,erased) VALUES(?1,?2,1,0) ON CONFLICT(owner,workspace_id) DO UPDATE SET admission_epoch=admission_epoch+1,erased=0",params![scope.owner,scope.workspace_id]).map_err(|e|e.to_string())?;
            let identity=crate::code::directory_identity(&binding.root)?;
            conn.execute("UPDATE code_registrations SET lifecycle='admitted',admission_epoch=admission_epoch+1 WHERE owner=?1 AND workspace_id=?2 AND directory_identity=?3 AND lifecycle='erased'",params![scope.owner,scope.workspace_id,identity]).map_err(|e|e.to_string())?;
        }
        let checkout=Self::codebase_for_root(&conn,scope,&binding.root)?;
        conn.execute("UPDATE code_index_runs SET status='cancelled',finished_at=?1 WHERE owner=?2 AND workspace_id=?3 AND codebase=?4 AND status='running' AND EXISTS(SELECT 1 FROM code_registrations c WHERE c.owner=?2 AND c.workspace_id=?3 AND c.checkout_id=?4 AND (c.policy_generation!=?5 OR c.workspace_revision!=?6 OR c.admission_epoch!=code_index_runs.admission_epoch OR c.config_fingerprint!=code_index_runs.config_fingerprint))",params![Utc::now().to_rfc3339(),scope.owner,scope.workspace_id,checkout,binding.policy_generation,binding.workspace_revision.to_string()]).map_err(|e|e.to_string())?;
        conn.execute("UPDATE code_registrations SET owner_subject_id=?1,location_id=?2,workspace_revision=?3,policy_generation=?4,config_fingerprint=?5 WHERE owner=?6 AND workspace_id=?7 AND checkout_id=?8",params![binding.owner_subject_id,binding.location_id,binding.workspace_revision.to_string(),binding.policy_generation,crate::code::producer_fingerprint(),scope.owner,scope.workspace_id,checkout]).map_err(|e|e.to_string())?;
        Self::code_lifecycle_inner(&conn,scope,&checkout).map(|value|self.lifecycle_progress(scope,&checkout,value))
        })();
        match prepared{Ok(payload)=>{conn.execute_batch("COMMIT").map_err(|e|e.to_string())?;Ok(payload)},Err(error)=>{let _=conn.execute_batch("ROLLBACK");Err(error)}}
    }

    pub fn code_workspace_status(&self,scope:&GraphScope,binding:&crate::code::WorkspaceBinding)->Result<serde_json::Value,String> {
        binding.validate()?;
        let conn=self.conn.lock().unwrap();
        let identity=crate::code::directory_identity(&binding.root)?;
        let checkout:Option<String>=conn.query_row("SELECT checkout_id FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND directory_identity=?3",params![scope.owner,scope.workspace_id,identity],|r|r.get(0)).optional().map_err(|e|e.to_string())?;
        if let Some(checkout)=checkout { Self::code_lifecycle_inner(&conn,scope,&checkout).map(|value|self.lifecycle_progress(scope,&checkout,value)) } else {
            let epoch:Option<(i64,i64)>=conn.query_row("SELECT admission_epoch,erased FROM code_scope_epochs WHERE owner=?1 AND workspace_id IN (?2,'*') ORDER BY CASE WHEN workspace_id=?2 THEN 0 ELSE 1 END LIMIT 1",params![scope.owner,scope.workspace_id],|r|Ok((r.get(0)?,r.get(1)?))).optional().map_err(|e|e.to_string())?;
            Ok(serde_json::json!({"registration_id":null,"checkout_id":null,"admission_epoch":epoch.map(|v|v.0).unwrap_or(0),"run_id":null,"head_generation":null,"lifecycle":if epoch.is_some_and(|v|v.1==1){"erased"}else{"missing"},"freshness":"missing","config_fingerprint":crate::code::producer_fingerprint(),"coverage":[],"progress":{"completed_files":0,"total_files":0,"heartbeat_at":null},"error":null}))
        }
    }

    fn code_lifecycle_inner(conn:&Connection,scope:&GraphScope,checkout:&str)->Result<serde_json::Value,String> {
        let (registration,epoch,lifecycle,config):(String,i64,String,String)=conn.query_row("SELECT registration_id,admission_epoch,lifecycle,config_fingerprint FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,checkout],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).map_err(|e|e.to_string())?;
        let run:Option<(String,String,String,i64,i64,Option<String>,String,String)>=conn.query_row("SELECT run_id,status,coverage,completed_files,total_files,heartbeat_at,errors,config_fingerprint FROM code_index_runs WHERE owner=?1 AND workspace_id=?2 AND codebase=?3 ORDER BY started_at DESC,rowid DESC LIMIT 1",params![scope.owner,scope.workspace_id,checkout],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?))).optional().map_err(|e|e.to_string())?;
        let head:Option<String>=conn.query_row("SELECT run_id FROM code_index_heads WHERE owner=?1 AND workspace_id=?2 AND codebase=?3",params![scope.owner,scope.workspace_id,checkout],|r|r.get(0)).optional().map_err(|e|e.to_string())?;
        let (run_id,status,coverage,completed,total,heartbeat,errors,run_config)=run.unwrap_or((String::new(),"missing".into(),"[]".into(),0,0,None,"[]".into(),String::new()));
        let mut coverage:serde_json::Value=serde_json::from_str(&coverage).unwrap_or(serde_json::json!([]));
        Self::hydrate_lifecycle_coverage(conn,scope,checkout,&mut coverage)?;
        let errors:Vec<String>=serde_json::from_str(&errors).unwrap_or_default();
        let status=if lifecycle=="erased" {"erased"}else if status=="running" {"building"}else if status=="verified"{"ready"}else{&status};
        let eligible_unavailable = coverage.as_array().is_some_and(|entries| entries.iter().any(|entry| serde_json::from_value::<crate::code::ScanCoverage>(entry.clone()).is_ok_and(|entry| crate::code::eligible_unavailable(&entry))));
        let freshness=if head.is_none(){"missing"}else if !errors.is_empty() || eligible_unavailable {"partial"}else if status!="ready" || run_config!=crate::code::producer_fingerprint(){"stale"}else{"current"};
        Ok(serde_json::json!({"registration_id":registration,"checkout_id":checkout,"admission_epoch":epoch,"run_id":if run_id.is_empty(){None}else{Some(run_id)},"head_generation":head,"lifecycle":status,"freshness":freshness,"config_fingerprint":config,"coverage":coverage,"coverage_complete":status=="ready","progress":{"completed_files":completed,"total_files":total,"heartbeat_at":heartbeat},"error":errors.first().map(|message|serde_json::json!({"code":"index_partial_or_failed","message":message}))}))
    }

    /// Preserve the historical live code_files overlay, without one SQL
    /// prepare/lookup per coverage entry under the shared FM connection lock.
    fn hydrate_lifecycle_coverage(conn:&Connection,scope:&GraphScope,checkout:&str,coverage:&mut serde_json::Value)->Result<(),String>{
        let Some(entries)=coverage.as_array_mut() else{return Ok(())};
        if entries.is_empty(){return Ok(());}
        let json=serde_json::to_string(entries).map_err(|e|e.to_string())?;
        let key=Self::codebase_key(scope,checkout);
        // CASE makes every json_extract input valid even for heterogeneous
        // array members. Only TEXT rel_path values participated in the old
        // Rust as_str lookup. Ordinals preserve duplicates and original order.
        let mut statement=conn.prepare("SELECT j.key,f.blake3,f.source_generation FROM json_each(?1) AS j CROSS JOIN code_files AS f ON f.codebase=?2 AND f.rel_path=json_extract(CASE WHEN j.type='object' THEN j.value ELSE '{}' END,'$.rel_path') WHERE json_type(CASE WHEN j.type='object' THEN j.value ELSE '{}' END,'$.rel_path')='text'").map_err(|e|e.to_string())?;
        let mut rows=statement.query(params![json,key]).map_err(|e|e.to_string())?;
        while let Some(row)=rows.next().map_err(|e|e.to_string())?{
            let ordinal:usize=row.get(0).map_err(|e|e.to_string())?;
            let hash:String=row.get(1).map_err(|e|e.to_string())?;
            let generation:String=row.get(2).map_err(|e|e.to_string())?;
            let entry=entries.get_mut(ordinal).ok_or("invalid lifecycle coverage ordinal")?;
            entry["source_hash"]=serde_json::json!(hash);
            entry["source_generation"]=serde_json::json!(generation);
        }
        Ok(())
    }

    pub fn code_workspace_cancel(&self,scope:&GraphScope,checkout:&str)->Result<(),String> {
        let conn=self.conn.lock().unwrap();
        conn.execute_batch("BEGIN IMMEDIATE").map_err(|e|e.to_string())?;
        let cancelled=(||{
        conn.execute("UPDATE code_registrations SET admission_epoch=admission_epoch+1,lifecycle='cancelled' WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,checkout]).map_err(|e|e.to_string())?;
        conn.execute("UPDATE code_index_runs SET status='cancelled',finished_at=?1 WHERE owner=?2 AND workspace_id=?3 AND codebase=?4 AND status='running'",params![Utc::now().to_rfc3339(),scope.owner,scope.workspace_id,checkout]).map_err(|e|e.to_string())?;
        Ok(())
        })();
        match cancelled{Ok(())=>conn.execute_batch("COMMIT").map_err(|e|e.to_string()),Err(error)=>{let _=conn.execute_batch("ROLLBACK");Err(error)}}
    }

    pub fn code_workspace_remove(&self,scope:&GraphScope,binding:&crate::code::WorkspaceBinding)->Result<usize,String>{
        binding.validate_cleanup()?;
        self.code_query_audit.invalidate(&scope.owner,Some(&scope.workspace_id),None);
        self.code_visual_cache.invalidate(&scope.owner,Some(&scope.workspace_id),None);
        let conn=self.conn.lock().unwrap();
        conn.execute_batch("BEGIN IMMEDIATE").map_err(|e|e.to_string())?;
        let removed=(||{
            let mut namespaces:Vec<String>=conn.prepare("SELECT checkout_id FROM code_registrations WHERE owner=?1 AND workspace_id=?2 UNION SELECT codebase FROM code_index_runs WHERE owner=?1 AND workspace_id=?2").map_err(|e|e.to_string())?.query_map(params![scope.owner,scope.workspace_id],|r|r.get(0)).map_err(|e|e.to_string())?.collect::<Result<_,_>>().map_err(|e|e.to_string())?;
            for key in Self::code_owner_keys_inner(&conn,&scope.owner)?{
                if let Some(tuple)=key.strip_prefix("code_v1:").and_then(|json|serde_json::from_str::<[String;3]>(json).ok()){if tuple[1]==scope.workspace_id{namespaces.push(tuple[2].clone());}}
                else{let parts:Vec<_>=key.split('\u{1f}').collect();if parts.len()==3 && parts[1]==scope.workspace_id{namespaces.push(parts[2].into());}}
            }
            namespaces.sort();namespaces.dedup();let mut count=0;
            for namespace in namespaces {count+=Self::code_remove_inner(&conn,scope,&namespace)?;}
            conn.execute("INSERT INTO code_scope_epochs(owner,workspace_id,admission_epoch,erased) VALUES(?1,?2,1,1) ON CONFLICT(owner,workspace_id) DO UPDATE SET admission_epoch=admission_epoch+1,erased=1",params![scope.owner,scope.workspace_id]).map_err(|e|e.to_string())?;
            Ok(count)
        })();
        match removed{Ok(count)=>{conn.execute_batch("COMMIT").map_err(|e|e.to_string())?;self.code_visual_cache.invalidate(&scope.owner,Some(&scope.workspace_id),None);super::code_cache_lifecycle::remove_workspace_cache(&conn,scope)?;Ok(count)},Err(error)=>{let _=conn.execute_batch("ROLLBACK");Err(error)}}
    }
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

    pub(crate) fn graph_upsert_inner(
        conn: &Connection,
        scope: &GraphScope,
        input: &GraphUpsertInput,
    ) -> rusqlite::Result<GraphUpsertResult> {
        let now = Utc::now().to_rfc3339();
        let mut nodes_upserted = 0usize;
        let mut edges_upserted = 0usize;
        let mut cues_upserted = 0usize;

        // Capture cue-target state before either explicit nodes or edge endpoints
        // can reactivate it. Startup FTS sync excludes cues on inactive nodes;
        // those input cues still need one status trigger after reactivation.
        // Only keyed node lookups are needed, never a scan of existing cues.
        let mut cue_targets = HashSet::new();
        let mut reactivated_cue_targets = HashSet::new();
        for c in &input.cues {
            if norm_name(&c.cue).is_empty() {
                continue;
            }
            let id = scope.node_id(&c.node.kind, &c.node.name);
            if cue_targets.insert(id.clone()) {
                let status: Option<String> = conn.query_row(
                    "SELECT status FROM graph_nodes WHERE id=?1",
                    params![id], |row| row.get(0),
                ).optional()?;
                if status.as_deref().is_some_and(|status| status != "active") {
                    reactivated_cue_targets.insert(id);
                }
            }
        }
        let mut repaired_cues = HashSet::new();

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
            let repair = reactivated_cue_targets.contains(&node)
                && !repaired_cues.contains(&(node.clone(), cue.clone()));
            let inserted = conn.execute(
                "INSERT INTO graph_cues
                 (cue, node_id, source, created_at, owner, workspace_id, status)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'active')
                 ON CONFLICT(cue, node_id) DO UPDATE SET status = 'active'
                 WHERE graph_cues.status <> 'active' OR ?7",
                params![
                    cue,
                    node,
                    c.source.as_deref().unwrap_or("extracted"),
                    now,
                    scope.owner,
                    scope.workspace_id,
                    repair,
                ],
            )?;
            if repair {
                repaired_cues.insert((node, cue));
            }
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
    fn legacy_v1_db_is_rejected_without_upgrade() {
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
        assert!(s.init_tables().is_err());
        let conn = s.conn.lock().unwrap();
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, 1);
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
    pub(crate) fn codebase_key(scope: &GraphScope, codebase: &str) -> String {
        if codebase.strip_prefix("checkout_").is_some_and(|id|id.len()==32 && id.bytes().all(|b|b.is_ascii_hexdigit())) {
            return format!("code_v1:{}",serde_json::to_string(&[scope.owner.as_str(),scope.workspace_id.as_str(),codebase]).expect("string tuple serializes"));
        }
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

    fn codebase_lookup(conn:&Connection,scope:&GraphScope,root:&std::path::Path)->Result<String,String> {
        let canonical=root.canonicalize().map_err(|e|e.to_string())?;
        let binding=blake3::hash(canonical.as_os_str().as_encoded_bytes()).to_hex().to_string();
        Ok(conn.query_row("SELECT checkout_id FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND root_binding=?3",params![scope.owner,scope.workspace_id,binding],|r|r.get(0)).optional().map_err(|e|e.to_string())?.unwrap_or_else(||"unregistered".into()))
    }

    fn codebase_for_root(
        conn: &Connection,
        scope: &GraphScope,
        root: &std::path::Path,
    ) -> Result<String, String> {
        let canonical = root.canonicalize().map_err(|e| e.to_string())?;
        let binding = blake3::hash(canonical.as_os_str().as_encoded_bytes()).to_hex().to_string();
        let directory_identity=crate::code::directory_identity(root)?;
        let erased:Option<i64>=conn.query_row("SELECT erased FROM code_scope_epochs WHERE owner=?1 AND workspace_id IN (?2,'*') ORDER BY CASE WHEN workspace_id=?2 THEN 0 ELSE 1 END LIMIT 1",params![scope.owner,scope.workspace_id],|r|r.get(0)).optional().map_err(|e|e.to_string())?;
        if erased==Some(1) { return Err("erased: explicit admit required".into()); }
        let existing: Option<(String, String,String)> = conn.query_row(
            "SELECT checkout_id,lifecycle,directory_identity FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND (root_binding=?3 OR directory_identity=?4)",
            params![scope.owner,scope.workspace_id,binding,directory_identity], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional().map_err(|e|e.to_string())?;
        if let Some((checkout, lifecycle,known_identity)) = existing {
            if lifecycle == "erased" { return Err("erased: explicit admit required".into()); }
            if known_identity!=directory_identity { return Err("denied: directory binding changed".into()); }
            conn.execute("UPDATE code_registrations SET root_binding=?1,config_fingerprint=?5 WHERE owner=?2 AND workspace_id=?3 AND checkout_id=?4",params![binding,scope.owner,scope.workspace_id,checkout,crate::code::producer_fingerprint()]).map_err(|e|e.to_string())?;
            return Ok(checkout);
        }
        let registration = format!("registration_{}", uuid::Uuid::new_v4().simple());
        let checkout = format!("checkout_{}", uuid::Uuid::new_v4().simple());
        conn.execute("INSERT INTO code_registrations(owner,workspace_id,registration_id,checkout_id,root_binding,config_fingerprint,directory_identity,lineage) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",params![scope.owner,scope.workspace_id,registration,checkout,binding,crate::code::producer_fingerprint(),directory_identity,crate::code::git_lineage_pinned(root,&directory_identity)]).map_err(|e|e.to_string())?;
        Ok(checkout)
    }

    #[cfg(test)]
    fn begin_code_index_run(
        &self,
        conn: &Connection,
        scope: &GraphScope,
        codebase: &str,
        snapshot: &crate::code::CodeSourceSnapshot,
    ) -> Result<String, String> {
        self.begin_code_index_run_ticket(conn,scope,codebase,snapshot,None)
    }

    fn begin_code_index_run_ticket(&self,conn:&Connection,scope:&GraphScope,codebase:&str,snapshot:&crate::code::CodeSourceSnapshot,ticket:Option<&crate::code::CodeWorkTicket>)->Result<String,String> {
        // Claim the run atomically. Stale-run recovery, the active-run check
        // and the insert must be one IMMEDIATE transaction: SQLite serializes
        // writers under BEGIN IMMEDIATE, so a second fm-mcp process blocks
        // (then fails the check) instead of racing the check-then-insert.
        // A partial unique index on status='running' would be stronger but
        // requires a schema bump, which is not available here.
        conn.execute_batch("BEGIN IMMEDIATE")
            .map_err(|e| format!("claim code-index run: {e}"))?;
        let claimed = (|| {
            if let Some(ticket)=ticket {
                let count:i64=conn.query_row("SELECT count(*) FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3 AND admission_epoch=?4 AND config_fingerprint=?5 AND policy_generation=?6 AND workspace_revision=?7 AND lifecycle!='erased'",params![scope.owner,scope.workspace_id,ticket.checkout_id,ticket.admission_epoch,ticket.config_fingerprint,ticket.policy_generation,ticket.workspace_revision.to_string()],|r|r.get(0)).map_err(|e|e.to_string())?;
                if count!=1 || codebase!=ticket.checkout_id {return Err("stale_queued_ticket: registration/admission/config/policy changed".into());}
            }
            self.begin_code_index_run_inner(conn, scope, codebase, snapshot)
        })();
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
               AND COALESCE(heartbeat_at,started_at) < ?5",
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
             (owner, workspace_id, codebase, run_id, source_snapshot, status, started_at,heartbeat_at,admission_epoch,config_fingerprint)
             VALUES (?1, ?2, ?3, ?4, ?5, 'running', ?6,?6,
                COALESCE((SELECT admission_epoch FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3),0),?7)",
            params![
                scope.owner,
                scope.workspace_id,
                codebase,
                run_id,
                source_snapshot,
                Utc::now().to_rfc3339(),
                crate::code::producer_fingerprint()
            ],
        )
        .map_err(|e| e.to_string())?;
        Ok(run_id)
    }

    pub(crate) fn finish_code_index_run(
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
                     finished_at=?8,heartbeat_at=?8,completed_files=CASE WHEN ?1 IN ('ready','verified') THEN total_files ELSE completed_files END,source_snapshot=?13
                 WHERE owner=?9 AND workspace_id=?10 AND codebase=?11 AND run_id=?12 AND status='running'",
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
                    run_id,
                    serde_json::to_string(&result.source_snapshot).map_err(|e|e.to_string())?
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

    pub(crate) fn code_delete_file_nodes(
        conn: &Connection,
        scope: &GraphScope,
        codebase: &str,
        rel_path: &str,
    ) -> rusqlite::Result<()> {
        let prefix = format!("{codebase}::{rel_path}");
        if !codebase.strip_prefix("checkout_").is_some_and(|id|id.len()==32 && id.bytes().all(|b|b.is_ascii_hexdigit())){return Err(rusqlite::Error::InvalidQuery);}
        // Enabled producers use identifier definitions (no '::'). Split at
        // the FINAL separator, preserving every literal filename component.
        // An arbitrary defines overlay is not a source ownership authority.
        let ids: Vec<String> = {
            let mut stmt = conn.prepare(
                "SELECT id,kind,name FROM graph_nodes
                 WHERE (id=?1 OR (kind='code_symbol' AND substr(name,1,length(?4))=?4)) AND owner=?2 AND workspace_id=?3",
            )?;
            let rows = stmt.query_map(
                params![
                    scope.node_id("file",&prefix),
                    scope.owner,
                    scope.workspace_id,
                    format!("{codebase}::")
                ],
                |r| Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?)),
            )?;
            rows.collect::<Result<Vec<_>, _>>()?.into_iter().filter(|(_,kind,name)|
                (kind=="file" && name==&prefix) || (kind=="code_symbol" && name.rsplit_once("::").is_some_and(|(file,identifier)|file==prefix && !identifier.is_empty()))
            ).map(|(id,_,_)|id).collect()
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
        let root_identity = crate::code::directory_identity(root)?;
        let scan = crate::code::scan_codebase_pinned(root, &root_identity)?;
        let policy = scan.policy;
        let policy_changed = {
            let conn = self.conn.lock().unwrap();
            let prior: Option<String> = conn.query_row("SELECT json_extract(r.source_snapshot,'$.selection_policy_digest') FROM code_index_heads h JOIN code_index_runs r ON r.owner=h.owner AND r.workspace_id=h.workspace_id AND r.codebase=h.codebase AND r.run_id=h.run_id WHERE h.owner=?1 AND h.workspace_id=?2 AND h.codebase=?3", params![scope.owner, scope.workspace_id, codebase], |r| r.get::<_,Option<String>>(0)).optional().map_err(|e|e.to_string())?.flatten();
            prior.as_deref() != Some(policy.digest().as_str())
        };
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
            policy.validate_source(root, &root_identity, &path)?;
            let acquired = match crate::code::acquire_source_pinned(root,&path,&root_identity) {
                Ok(acquired) => acquired,
                Err(e) => {
                    result.errors.push(format!("{rel}: {e}"));
                    continue;
                }
            };
            policy.validate_source(root, &root_identity, &path)?;
            let meta = &acquired.metadata;
            result.checked_files += 1;
            let mtime_ns = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_nanos() as i64)
                .unwrap_or(0);
            let current = (
                blake3::hash(&acquired.bytes).to_hex().to_string(),
                mtime_ns,
                meta.len() as i64,
            );
            if policy_changed || known.get(&rel) != Some(&current) {
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
        policy.validate(root, &root_identity)?;
        Ok(result)
    }

    pub fn code_index(
        &self,
        scope: &GraphScope,
        root: &std::path::Path,
    ) -> Result<CodeIndexResult, String> {
        self.code_index_inner(scope,root,None,None)
    }

    pub fn code_index_bound(&self,scope:&GraphScope,binding:&crate::code::WorkspaceBinding,ticket:&crate::code::CodeWorkTicket)->Result<CodeIndexResult,String> {
        binding.validate()?;
        self.code_index_inner(scope,&binding.root,Some(binding),Some(ticket))
    }

    fn code_index_inner(&self,scope:&GraphScope,root:&std::path::Path,binding:Option<&crate::code::WorkspaceBinding>,ticket:Option<&crate::code::CodeWorkTicket>) -> Result<CodeIndexResult,String> {
        let scheduler=self.index_scheduler();
        if !scheduler.is_current(){return scheduler.run(||self.code_index_inner(scope,root,binding,ticket));}
        if let Some(binding)=binding{binding.validate()?;}
        let _timing = crate::code_metrics::span(crate::code_metrics::Stage::IndexTotal);
        let (codebase,root_identity) = {
            let conn = self.conn.lock().unwrap();
            if let Some(ticket)=ticket {
                let checkout=Self::codebase_lookup(&conn,scope,root)?;
                if checkout!=ticket.checkout_id {return Err("stale_queued_ticket: checkout replaced".into());}
                let expected:String=conn.query_row("SELECT directory_identity FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,checkout],|r|r.get(0)).map_err(|e|e.to_string())?;
                (checkout,expected)
            }else{(Self::codebase_for_root(&conn, scope, root)?,crate::code::directory_identity(root)?)}
        };
        let codebase_key = Self::codebase_key(scope, &codebase);
        let mut source_snapshot = crate::code::source_snapshot_pinned(root, &[],&root_identity)?;
        let registration_id={let conn=self.conn.lock().unwrap();conn.query_row("SELECT registration_id FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,codebase],|r|r.get::<_,String>(0)).map_err(|e|e.to_string())?};
        source_snapshot.repository_id = registration_id.clone();
        let run_id = {
            let conn = self.conn.lock().unwrap();
            self.begin_code_index_run_ticket(&conn, scope, &codebase, &source_snapshot,ticket)?
        };
        struct RunFailure<'a>{store:&'a SqliteStore,scope:&'a GraphScope,codebase:String,run:String,complete:std::cell::Cell<bool>}
        impl Drop for RunFailure<'_>{fn drop(&mut self){if !self.complete.get(){let conn=self.store.conn.lock().unwrap();let _=conn.execute("UPDATE code_index_runs SET status='failed',finished_at=?1,errors='[\"index_aborted_before_publication\"]' WHERE owner=?2 AND workspace_id=?3 AND codebase=?4 AND run_id=?5 AND status='running'",params![Utc::now().to_rfc3339(),self.scope.owner,self.scope.workspace_id,self.codebase,self.run]);}}}
        let run_failure=RunFailure{store:self,scope,codebase:codebase.clone(),run:run_id.clone(),complete:std::cell::Cell::new(false)};
        // A scoped heartbeat owns exactly this run token; it never restarts a
        // worker or renews a successor. It is independent of file parse time.
        let heartbeat_stop=std::sync::Arc::new((std::sync::Mutex::new(false),std::sync::Condvar::new()));
        struct StopHeartbeat(std::sync::Arc<(std::sync::Mutex<bool>,std::sync::Condvar)>);
        impl Drop for StopHeartbeat {fn drop(&mut self){let (stop,wake)=&*self.0;*stop.lock().unwrap()=true;wake.notify_all();}}
        std::thread::scope(|threads| {
        let stopping=heartbeat_stop.clone();
        let heartbeat_run=run_id.clone();
        let heartbeat_codebase=codebase.clone();
        threads.spawn(move || {
            let (stop,wake)=&*stopping;
            loop {
                let stopped=stop.lock().unwrap();
                let (stopped,_)=wake.wait_timeout(stopped,std::time::Duration::from_secs(1)).unwrap();
                if *stopped {break;}
                drop(stopped);
                let conn=self.conn.lock().unwrap();
                let renewed=conn.execute("UPDATE code_index_runs SET heartbeat_at=?1 WHERE owner=?2 AND workspace_id=?3 AND codebase=?4 AND run_id=?5 AND status='running'",params![Utc::now().to_rfc3339(),scope.owner,scope.workspace_id,heartbeat_codebase,heartbeat_run]);
                if renewed!=Ok(1){break;}
            }
        });
        let _stop_heartbeat=StopHeartbeat(heartbeat_stop.clone());
        #[cfg(test)] index_test_pause(root,"before_scan");
        self.set_index_progress(scope,&codebase,&run_id,"inventory",None,None,"entries")?;
        let scan = crate::code::scan_codebase_pinned(root,&root_identity)?;
        self.set_index_progress(scope,&codebase,&run_id,"inventory",Some(scan.coverage.len()),Some(scan.coverage.len()),"entries")?;
        let policy = scan.policy;
        source_snapshot.selection_policy_digest = policy.digest();
        let files = scan.files;
        let total_selected=files.len();
        if total_selected>super::code_evidence::MAX_CORPUS_FILES{return Err("budget_exceeded: selected source file count".into());}
        let mut result = CodeIndexResult {
            codebase: codebase.clone(),
            repository_id: registration_id,
            run_id: run_id.clone(),
            source_snapshot,
            files_indexed: 0,
            files_unchanged: 0,
            files_removed: 0,
            symbols: 0,
            errors: vec![],
            coverage: scan.coverage,
        };

        let effective_limits=crate::code_index_limits::current();
        if files.len()>effective_limits.max_files{return Err("budget_exceeded: configured selected-file limit".into());}
        let selected_bytes=files.iter().try_fold(0u64,|total,path|std::fs::metadata(path).ok().and_then(|m|total.checked_add(m.len()))).ok_or("source_changed: source metadata unavailable")?;
        if selected_bytes>effective_limits.max_source_bytes as u64{return Err("budget_exceeded: configured total-source limit".into());}
        // The v1 path retains its original whole-snapshot budgets. Disk
        // partitions bound larger repositories without weakening v1 readers.
        let file_backed=self.conn.lock().unwrap().path().is_some_and(|path|!path.is_empty());
        if file_backed&&(files.len()>128 || files.iter().any(|p|std::fs::metadata(p).is_ok_and(|m|m.len()>128*1024))) {
            let output=self.code_index_partitioned(scope,root,binding,&root_identity,files,&policy,result)?;
            run_failure.complete.set(true);
            return Ok(output);
        }

        self.set_index_progress(scope,&codebase,&run_id,"extraction",Some(0),Some(total_selected),"files")?;
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
                    ..Default::default()
                });
            }
        };

        let evidence_base = {
            let conn = self.conn.lock().unwrap();
            // Capture the head FIRST and all known source/evidence inputs in
            // one SQLite snapshot. An independent writer cannot mix versions.
            conn.execute_batch("SAVEPOINT code_evidence_base").map_err(|e|e.to_string())?;
            let loaded=super::code_evidence::load_base(&conn,scope,&codebase);
            if loaded.is_err(){let _=conn.execute_batch("ROLLBACK TO code_evidence_base");}
            conn.execute_batch("RELEASE code_evidence_base").map_err(|e|e.to_string())?;
            loaded?
        };
        #[cfg(test)] index_test_pause(root,"after_evidence_base");
        let known_map: HashMap<String, (String, i64, i64)> = evidence_base.known.clone()
            .into_iter()
            .map(|(p, hash, mtime, size)| (p, (hash, mtime, size)))
            .collect();

        let mut seen: HashSet<String> = HashSet::new();
        let mut staged: Vec<crate::code::IndexedFile> = Vec::new();
        let mut staged_evidence_bytes=0usize;
        let mut acquired_source_bytes=0usize;
        let mut manifest = std::collections::BTreeMap::new();
        let mut failed:Vec<String>=known_map.keys().filter(|rel|result.coverage.iter().any(|entry|entry.status!="selected" && (entry.rel_path==***rel || (entry.status=="ignored" && rel.starts_with(&format!("{}/",entry.rel_path)))))).cloned().collect();
        seen.extend(result.coverage.iter().map(|entry|entry.rel_path.clone()));
        for (completed,path) in files.into_iter().enumerate() {
            if let Some(binding)=binding { binding.validate()?; }
            let rel = path
                .strip_prefix(root)
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_default();
            seen.insert(rel.clone());
            self.set_index_progress(scope,&codebase,&run_id,"extraction",Some(completed),Some(total_selected),"files")?;
            policy.validate_source(root, &root_identity, &path)?;
            let acquired = match crate::code::acquire_source_pinned(root, &path,&root_identity) {
                Ok(acquired) => acquired,
                Err(e) => {
                    result.errors.push(format!("{rel}: {e}"));
                    manifest.insert(rel.clone(),format!("failed:{e}"));
                    failed.push(rel.clone());
                    set_coverage(
                        &mut result.coverage,
                        &mut coverage_index,
                        &rel,
                        "permission_denied",
                        Some(e),
                    );
                    continue;
                }
            };
            acquired_source_bytes=acquired_source_bytes.checked_add(acquired.bytes.len()).ok_or("budget_exceeded: source byte overflow")?;
            if acquired_source_bytes>effective_limits.max_source_bytes{return Err("budget_exceeded: configured total-source limit".into());}
            policy.validate_source(root, &root_identity, &path)?;
            let meta = &acquired.metadata;
            let mtime_ns = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_nanos() as i64)
                .unwrap_or(0);
            let current_hash = crate::code_metrics::source_hash(&acquired.bytes).to_hex().to_string();
            if let Some(index)=coverage_index.get(&rel).copied(){result.coverage[index].source_hash=Some(current_hash.clone());result.coverage[index].source_generation=Some(run_id.clone());}
            manifest.insert(rel.clone(),current_hash.clone());
            // Only a complete current-config typed record captured in the
            // same base snapshot may skip extraction. v15 never invents it.
            let fingerprint_current=evidence_base.files.contains_key(&rel)
                && evidence_base.policy_digest.as_deref() == Some(result.source_snapshot.selection_policy_digest.as_str());
            if fingerprint_current && known_map.get(&rel) == Some(&(current_hash.clone(), mtime_ns, acquired.bytes.len() as i64)) {
                if let Some(index)=coverage_index.get(&rel).copied(){result.coverage[index].source_generation=evidence_base.files.get(&rel).map(|file|file.source_generation.clone());}
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
            match crate::code::index_acquired(&codebase, root, &path, &acquired) {
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
                    let mut _staging=crate::code_metrics::span(crate::code_metrics::Stage::Stage);
                    let evidence_bytes=serde_json::to_vec(&indexed.evidence).map_err(|_|"invalid_evidence: cannot serialize staged evidence")?.len();
                    _staging.add(1,evidence_bytes);
                    staged_evidence_bytes=staged_evidence_bytes.checked_add(evidence_bytes).ok_or("budget_exceeded: staged evidence overflow")?;
                    if evidence_bytes>super::code_evidence::MAX_FILE_EVIDENCE_BYTES || staged_evidence_bytes>super::code_evidence::MAX_CORPUS_EVIDENCE_BYTES{return Err("budget_exceeded: staged evidence corpus".into());}
                    staged.push(indexed);
                }
                Err(e) => {
                    if e.starts_with("budget_exceeded:"){return Err(e);}
                    failed.push(rel.clone());
                    result.errors.push(format!("{rel}: {e}"));
                    set_coverage(
                        &mut result.coverage,
                        &mut coverage_index,
                        &rel,
                        if e=="binary source" {"binary"}else{"parse_failed"},
                        Some(e),
                    );
                }
            }
        }
        self.set_index_progress(scope,&codebase,&run_id,"extraction",Some(total_selected),Some(total_selected),"files")?;
        for entry in &result.coverage {
            manifest.entry(entry.rel_path.clone()).or_insert_with(||format!("{}:{}",entry.status,entry.detail.as_deref().unwrap_or("")));
        }
        let mut hasher=blake3::Hasher::new();
        hasher.update(result.source_snapshot.selection_policy_digest.as_bytes());
        hasher.update(&[0]);
        for (path,digest) in manifest { hasher.update(path.as_bytes());hasher.update(&[0]);hasher.update(digest.as_bytes());hasher.update(&[0]); }
        result.source_snapshot.file_manifest_digest=hasher.finalize().to_hex().to_string();

        // Sweep files that vanished from disk.
        let gone: Vec<String> = known_map
            .keys()
            .filter(|k| !seen.contains(*k))
            .cloned()
            .collect();
        // Pure definition-first corpus resolution is outside the FM write
        // transaction, but its exact base is rechecked below before mutation.
        self.set_index_progress(scope,&codebase,&run_id,"dependency_resolution",None,None,"files")?;
        let prepared_evidence=super::code_evidence::prepare_evidence(&evidence_base,&codebase,&staged,&gone,&failed,&result.coverage,&run_id)?;
        let evidence_changed=prepared_evidence.changed || evidence_base.manifest.as_deref()!=Some(result.source_snapshot.file_manifest_digest.as_str());
        #[cfg(test)] index_test_pause(root,"before_publish");
        policy.validate(root, &root_identity)?;
        let _authority_guard=if let Some(binding)=binding {Some(binding.publication_guard()?)}else{None};
        if crate::code::directory_identity(root)?!=root_identity{return Err("denied: admitted directory replaced before publication".into());}
        self.set_index_progress(scope,&codebase,&run_id,"publication",None,None,"files")?;
        let _publication_timing = crate::code_metrics::span(crate::code_metrics::Stage::Publish);
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
            let current_base:Option<String>=conn.query_row("SELECT run_id FROM code_index_heads WHERE owner=?1 AND workspace_id=?2 AND codebase=?3",params![scope.owner,scope.workspace_id,codebase],|r|r.get(0)).optional()?;
            if current_base!=evidence_base.head{return Err(rusqlite::Error::InvalidQuery);}
            let fenced: i64=conn.query_row("SELECT count(*) FROM code_index_runs r JOIN code_registrations c ON c.owner=r.owner AND c.workspace_id=r.workspace_id AND c.checkout_id=r.codebase WHERE r.owner=?1 AND r.workspace_id=?2 AND r.codebase=?3 AND r.run_id=?4 AND r.status='running' AND c.lifecycle!='erased' AND r.admission_epoch=c.admission_epoch AND r.config_fingerprint=c.config_fingerprint",params![scope.owner,scope.workspace_id,codebase,run_id],|r|r.get(0))?;
            if fenced != 1 { return Err(rusqlite::Error::InvalidQuery); }
            // Source replacement owns producer properties, not accumulated
            // observations. Retain those only for identities that survive.
            // Endpoints/tag (rather than legacy edge id) also survive owner
            // rename, which historically rekeys nodes but not edge ids.
            let node_observations=conn.prepare("SELECT id,created_at,ref_table,ref_id,candidate_id,status FROM graph_nodes WHERE owner=?1 AND workspace_id=?2 AND substr(name,1,length(?3))=?3")?.query_map(params![scope.owner,scope.workspace_id,format!("{codebase}::")],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,Option<String>>(2)?,r.get::<_,Option<String>>(3)?,r.get::<_,Option<String>>(4)?,r.get::<_,String>(5)?)))?.collect::<Result<Vec<_>,_>>()?;
            let edge_observations=conn.prepare("SELECT e.src_id,e.tag,e.dst_id,e.weight,e.traversal_count,e.created_at FROM graph_edges e JOIN graph_nodes n ON n.id=e.src_id WHERE e.owner=?1 AND e.workspace_id=?2 AND substr(n.name,1,length(?3))=?3")?.query_map(params![scope.owner,scope.workspace_id,format!("{codebase}::")],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,f64>(3)?,r.get::<_,i64>(4)?,r.get::<_,String>(5)?)))?.collect::<Result<Vec<_>,_>>()?;
            let external_edges=conn.prepare("SELECT DISTINCT json_object('id',e.id,'src',e.src_id,'tag',e.tag,'dst',e.dst_id,'fact',e.fact_id,'weight',e.weight,'count',e.traversal_count,'trust',e.trust,'created',e.created_at,'seen',e.last_seen,'candidate',e.candidate_id,'status',e.status) FROM graph_edges e JOIN graph_nodes n ON (n.id=e.src_id OR n.id=e.dst_id) WHERE e.owner=?1 AND e.workspace_id=?2 AND substr(n.name,1,length(?3))=?3 AND e.tag NOT IN ('defines','imports','calls')")?.query_map(params![scope.owner,scope.workspace_id,format!("{codebase}::")],|r|r.get::<_,String>(0))?.collect::<Result<Vec<_>,_>>()?;
            let external_cues=conn.prepare("SELECT json_object('cue',c.cue,'node',c.node_id,'source',c.source,'created',c.created_at,'candidate',c.candidate_id,'status',c.status) FROM graph_cues c JOIN graph_nodes n ON n.id=c.node_id WHERE c.owner=?1 AND c.workspace_id=?2 AND substr(n.name,1,length(?3))=?3 AND c.source!='code_index'")?.query_map(params![scope.owner,scope.workspace_id,format!("{codebase}::")],|r|r.get::<_,String>(0))?.collect::<Result<Vec<_>,_>>()?;
            for rel in &failed {
                Self::code_delete_file_nodes(&conn,scope,&codebase,rel)?;
                conn.execute("DELETE FROM code_files WHERE codebase=?1 AND rel_path=?2",params![codebase_key,rel])?;
            }
            for indexed in &staged {
                Self::code_delete_file_nodes(&conn, scope, &codebase, &indexed.rel_path)?;
                Self::graph_upsert_inner(&conn, scope, &indexed.upsert)?;
                conn.execute(
                    "INSERT OR REPLACE INTO code_files
                     (codebase, rel_path, blake3, mtime_ns, size, symbol_count, indexed_at,config_fingerprint,source_generation)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7,?8,?9)",
                    params![
                        codebase_key,
                        indexed.rel_path,
                        indexed.blake3,
                        indexed.mtime_ns,
                        indexed.size,
                        indexed.symbol_count as i64,
                        Utc::now().to_rfc3339(),crate::code::producer_fingerprint(),run_id
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
            // Shared endpoint nodes have no independent producer. Remove
            // only orphaned code candidates after the complete file delta.
            let unsupported_candidates=conn.prepare("SELECT n.id FROM graph_nodes n WHERE n.owner=?1 AND n.workspace_id=?2 AND n.kind IN ('module','callable') AND substr(n.name,1,length(?3))=?3 AND NOT EXISTS(SELECT 1 FROM graph_edges e JOIN graph_nodes f ON f.id=e.src_id WHERE e.dst_id=n.id AND e.owner=?1 AND e.workspace_id=?2 AND e.tag IN ('imports','calls') AND f.kind='file' AND f.owner=?1 AND f.workspace_id=?2 AND substr(f.name,1,length(?3))=?3)")?.query_map(params![scope.owner,scope.workspace_id,format!("{codebase}::")],|r|r.get::<_,String>(0))?.collect::<Result<Vec<_>,_>>()?;
            for id in unsupported_candidates{conn.execute("DELETE FROM graph_edges WHERE src_id=?1 OR dst_id=?1",params![id])?;conn.execute("DELETE FROM graph_cues WHERE node_id=?1",params![id])?;conn.execute("DELETE FROM graph_nodes WHERE id=?1",params![id])?;}
            for (id,created,table,reference,candidate,status) in node_observations{conn.execute("UPDATE graph_nodes SET created_at=?1,ref_table=?2,ref_id=?3,candidate_id=?4,status=?5 WHERE id=?6 AND owner=?7 AND workspace_id=?8",params![created,table,reference,candidate,status,id,scope.owner,scope.workspace_id])?;}
            for edge in external_edges{conn.execute("INSERT OR IGNORE INTO graph_edges(id,src_id,tag,dst_id,fact_id,weight,traversal_count,trust,created_at,last_seen,candidate_id,status,owner,workspace_id) SELECT json_extract(?1,'$.id'),json_extract(?1,'$.src'),json_extract(?1,'$.tag'),json_extract(?1,'$.dst'),json_extract(?1,'$.fact'),json_extract(?1,'$.weight'),json_extract(?1,'$.count'),json_extract(?1,'$.trust'),json_extract(?1,'$.created'),json_extract(?1,'$.seen'),json_extract(?1,'$.candidate'),json_extract(?1,'$.status'),?2,?3 WHERE EXISTS(SELECT 1 FROM graph_nodes WHERE id=json_extract(?1,'$.src') AND owner=?2 AND workspace_id=?3) AND EXISTS(SELECT 1 FROM graph_nodes WHERE id=json_extract(?1,'$.dst') AND owner=?2 AND workspace_id=?3)",params![edge,scope.owner,scope.workspace_id])?;}
            for cue in external_cues{conn.execute("INSERT INTO graph_cues(cue,node_id,source,created_at,candidate_id,status,owner,workspace_id) SELECT json_extract(?1,'$.cue'),json_extract(?1,'$.node'),json_extract(?1,'$.source'),json_extract(?1,'$.created'),json_extract(?1,'$.candidate'),json_extract(?1,'$.status'),?2,?3 WHERE EXISTS(SELECT 1 FROM graph_nodes WHERE id=json_extract(?1,'$.node') AND owner=?2 AND workspace_id=?3) ON CONFLICT(cue,node_id) DO UPDATE SET source=excluded.source,created_at=excluded.created_at,candidate_id=excluded.candidate_id,status=excluded.status",params![cue,scope.owner,scope.workspace_id])?;}
            for (src,tag,dst,weight,traversals,created) in edge_observations{conn.execute("UPDATE graph_edges SET weight=?1,traversal_count=?2,created_at=?3 WHERE src_id=?4 AND tag=?5 AND dst_id=?6 AND owner=?7 AND workspace_id=?8",params![weight,traversals,created,src,tag,dst,scope.owner,scope.workspace_id])?;}
            result.files_indexed = staged.len();
            result.symbols = staged.iter().map(|item| item.symbol_count).sum();
            result.files_removed = gone.len();
            for indexed in &staged {
                set_coverage(
                    &mut result.coverage,
                    &mut coverage_index,
                    &indexed.rel_path,
                    if indexed.upsert.edges.is_empty(){"no_source_units"}else{"indexed"},
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
            let prior:Option<String>=conn.query_row("SELECT run_id FROM code_index_heads WHERE owner=?1 AND workspace_id=?2 AND codebase=?3",params![scope.owner,scope.workspace_id,codebase],|r|r.get(0)).optional()?;
            let publish_status=if prior.is_some() && !evidence_changed && staged.is_empty() && gone.is_empty() && failed.is_empty() && result.errors.is_empty(){"verified"}else{"ready"};
            // Verified no-change runs keep the prior head and its resolution
            // generation. New logical/availability evidence deliberately
            // publishes a new matching head, never mixed generation rows.
            if publish_status=="ready"{super::code_evidence::publish_evidence(&conn,scope,&codebase,&run_id,&prepared_evidence)?;}
            #[cfg(test)] index_test_pause(root,"before_commit");
            policy.validate(root, &root_identity).map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(std::io::Error::other(error))))?;
            if crate::code::directory_identity(root).map_err(|error|rusqlite::Error::ToSqlConversionFailure(Box::new(std::io::Error::other(error))))?!=root_identity{
                return Err(rusqlite::Error::ToSqlConversionFailure(Box::new(std::io::Error::other("denied: admitted directory replaced before ready publication"))));
            }
            if let (Some(binding),Some(authority))=(binding,_authority_guard.as_ref()) {
                // Revocation is serialized by the existing authority lock,
                // but wall-clock expiry can occur while FM rows are staged.
                // Recheck on that SAME connection: no inverted lock order.
                binding.validate_authority(authority).map_err(|error|rusqlite::Error::ToSqlConversionFailure(Box::new(std::io::Error::other(error))))?;
            }
            Self::finish_code_index_run(&conn, scope, &codebase, &run_id, publish_status, &result)
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
        run_failure.complete.set(true);
        Ok(result)
        })
    }

    pub fn code_status(
        &self,
        scope: &GraphScope,
        codebase: &str,
    ) -> rusqlite::Result<(usize, usize, Option<String>)> {
        let conn = self.conn.lock().unwrap();
        let codebase = if std::path::Path::new(codebase).is_dir() {
            Self::codebase_lookup(&conn, scope, std::path::Path::new(codebase))
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
            Self::codebase_lookup(&conn, scope, std::path::Path::new(codebase))
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
        self.code_query_audit.invalidate(&scope.owner,Some(&scope.workspace_id),None);
        self.code_visual_cache.invalidate(&scope.owner,Some(&scope.workspace_id),None);
        let codebase = if std::path::Path::new(codebase).is_dir() {
            let conn = self.conn.lock().unwrap();
            Self::codebase_lookup(&conn, scope, std::path::Path::new(codebase))?
        } else {
            Self::codebase_identity(codebase)
        };
        let conn=self.conn.lock().unwrap();
        conn.execute_batch("BEGIN IMMEDIATE").map_err(|e|e.to_string())?;
        let outcome=Self::code_remove_inner(&conn,scope,&codebase);
        match outcome { Ok(count)=>{conn.execute_batch("COMMIT").map_err(|e|e.to_string())?;self.code_visual_cache.invalidate(&scope.owner,Some(&scope.workspace_id),None);super::code_cache_lifecycle::remove_scope_cache(&conn,scope,&codebase)?;Ok(count)},Err(error)=>{let _=conn.execute_batch("ROLLBACK");Err(error)} }
    }

    fn code_remove_inner(conn:&Connection,scope:&GraphScope,codebase:&str)->Result<usize,String>{
        let codebase_key = Self::codebase_key(scope, codebase);
        conn.execute("DELETE FROM code_resolutions WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,codebase]).map_err(|e|e.to_string())?;
        conn.execute("DELETE FROM code_file_evidence WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,codebase]).map_err(|e|e.to_string())?;
        let rels: Vec<String> = {
            let mut stmt = conn
                .prepare("SELECT rel_path FROM code_files WHERE codebase = ?1")
                .map_err(|e| e.to_string())?;
            let rows = stmt
                .query_map(params![codebase_key], |r| r.get(0))
                .map_err(|e| e.to_string())?;
            rows.collect::<Result<_, _>>().map_err(|e| e.to_string())?
        };
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
                     WHERE substr(name,1,length(?1)) = ?1 AND owner = ?2 AND workspace_id = ?3",
                )
                .map_err(|e| e.to_string())?;
            let rows = stmt
                .query_map(
                    params![format!("{codebase}::"), scope.owner, scope.workspace_id],
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
        conn.execute("UPDATE code_registrations SET lifecycle='erased',admission_epoch=admission_epoch+1,workspace_revision='',policy_generation=0,lineage=NULL WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3",params![scope.owner,scope.workspace_id,codebase]).map_err(|e|e.to_string())?;
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
            Self::codebase_lookup(&conn, scope, std::path::Path::new(codebase))
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
                 WHERE kind = 'module' AND substr(name,1,length(?1))=?1 AND instr(name,?2)>0
                   AND owner = ?3 AND workspace_id = ?4",
            )?;
            let rows = stmt.query_map(
                params![
                    format!("{codebase}::"),
                    stem,
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
type IndexTestGate=std::sync::Arc<(std::sync::Barrier,std::sync::Barrier)>;
#[cfg(test)]
static INDEX_TEST_GATES:std::sync::OnceLock<std::sync::Mutex<HashMap<(std::path::PathBuf,String),IndexTestGate>>>=std::sync::OnceLock::new();
#[cfg(test)]
fn index_test_pause(root:&std::path::Path,phase:&str){
    let gate=INDEX_TEST_GATES.get_or_init(Default::default).lock().unwrap().remove(&(root.to_path_buf(),phase.into()));
    if let Some(gate)=gate{gate.0.wait();gate.1.wait();}
}

#[cfg(test)]
#[path = "code_lifecycle_tests.rs"]
mod code_lifecycle_tests;

#[cfg(test)]
mod code_index_tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn s01_ignore_privacy_reclassification_and_policy_identity() {
        let repo = fixture_repo(); let scope = test_scope(); let store = SqliteStore::memory(4).unwrap();
        write(&repo.0, "main.py", "def public_symbol(): pass\n");
        for name in ["data/private.py", ".clanker/private.py", ".references/private.py", "build/private.py"] {
            write(&repo.0, name, "def S01_PRIVATE_CANARY(): pass\n");
        }
        write(&repo.0, ".gitignore", "/data/\n");
        let first = store.code_index(&scope, &repo.0).unwrap();
        assert_eq!(first.files_indexed, 1);
        let exported = store.export_scope(&scope.owner, &scope.workspace_id).unwrap().to_string();
        assert!(!exported.contains("S01_PRIVATE_CANARY"));
        let conn = store.conn.lock().unwrap();
        assert_eq!(conn.query_row("SELECT count(*) FROM graph_nodes WHERE name LIKE '%S01_PRIVATE_CANARY%'", [], |r| r.get::<_,i64>(0)).unwrap(), 0);
        assert_eq!(conn.query_row("SELECT count(*) FROM code_file_evidence", [], |r| r.get::<_,i64>(0)).unwrap(), 1);
        drop(conn);
        write(&repo.0, ".gitignore", "/data/\nmain.py\n");
        let excluded = store.code_index(&scope, &repo.0).unwrap();
        assert_ne!(first.source_snapshot.selection_policy_digest, excluded.source_snapshot.selection_policy_digest);
        assert_eq!(excluded.files_unchanged, 0);
        assert_eq!(store.conn.lock().unwrap().query_row("SELECT count(*) FROM code_file_evidence", [], |r|r.get::<_,i64>(0)).unwrap(), 0);
        write(&repo.0, ".gitignore", "/data/\n# changed policy restores public source\n");
        let restored = store.code_index(&scope, &repo.0).unwrap();
        assert_eq!(restored.files_indexed, 1);
        write(&repo.0, ".gitignore", "/data/\n# another comment still changes policy identity\n");
        let refreshed = store.code_index(&scope, &repo.0).unwrap();
        assert_eq!(refreshed.files_indexed, 1);
        assert_eq!(refreshed.files_unchanged, 0);
    }

    #[test]
    fn s01_ignore_races_and_invalid_rebuild_keep_ready_head() {
        for phase in ["after_evidence_base", "before_publish", "before_commit"] {
            let repo = fixture_repo(); let scope = test_scope(); let store = std::sync::Arc::new(SqliteStore::memory(4).unwrap());
            write(&repo.0, "main.py", "def original(): pass\n");
            let first = store.code_index(&scope, &repo.0).unwrap();
            let original = logical_rows(&store, &scope, &first.codebase);
            let gate = pause_next(&repo.0, phase); let worker = store.clone(); let root = repo.0.clone();
            let job = std::thread::spawn(move || worker.code_index(&test_scope(), &root));
            gate.0.wait(); write(&repo.0, ".gitignore", "main.py\n"); gate.1.wait();
            assert!(job.join().unwrap().unwrap_err().contains("ignore_policy_changed"), "{phase}");
            assert_eq!(store.code_index_head(&scope, &first.codebase).unwrap().unwrap().run_id, first.run_id);
            assert_eq!(logical_rows(&store, &scope, &first.codebase), original);
            write(&repo.0, ".gitignore", "[z-a]\n");
            assert!(store.code_index(&scope, &repo.0).unwrap_err().contains("ignore_policy_invalid"));
            assert_eq!(store.code_index_head(&scope, &first.codebase).unwrap().unwrap().run_id, first.run_id);
        }
    }

    #[test]
    fn s01_more_than_ten_thousand_unsupported_assets_do_not_fill_source_budget() {
        let repo = fixture_repo(); let scope = test_scope(); let store = SqliteStore::memory(4).unwrap();
        write(&repo.0, "main.py", "def public_symbol(): pass\n");
        for i in 0..10_001 { write(&repo.0, &format!("assets/f{i}.pack"), "irrelevant"); }
        let indexed = store.code_index(&scope, &repo.0).unwrap();
        assert_eq!(indexed.files_indexed, 1);
        assert_eq!(indexed.coverage.iter().filter(|e| e.status=="unsupported").count(), 10_001);
        assert!(!indexed.coverage.iter().any(crate::code::eligible_unavailable));
        { let conn = store.conn.lock().unwrap(); assert_eq!(SqliteStore::code_lifecycle_inner(&conn, &scope, &indexed.codebase).unwrap()["freshness"], "current"); }
        write(&repo.0, "large.py", "");
        std::fs::OpenOptions::new().write(true).open(repo.0.join("large.py")).unwrap().set_len(512*1024+1).unwrap();
        let partial = store.code_index(&scope, &repo.0).unwrap();
        assert!(partial.coverage.iter().any(crate::code::eligible_unavailable));
        assert_eq!(partial.files_unchanged, 1);
        { let conn = store.conn.lock().unwrap(); let lifecycle=SqliteStore::code_lifecycle_inner(&conn, &scope, &indexed.codebase).unwrap(); assert_eq!(lifecycle["freshness"], "partial"); assert_eq!(lifecycle["coverage_complete"], true); }
    }

    static AUTHORITY_LOCK:std::sync::Mutex<()>=std::sync::Mutex::new(());
    struct AuthorityEnv(Option<std::ffi::OsString>);
    impl Drop for AuthorityEnv{fn drop(&mut self){if let Some(value)=&self.0{std::env::set_var("OPEN_CLANK_AUTHORITY_DB_PATH",value)}else{std::env::remove_var("OPEN_CLANK_AUTHORITY_DB_PATH")}}}
    #[test]
    fn rich_publication_failure_and_base_head_cas_preserve_prior_generation(){
        let repo=fixture_repo();let store=std::sync::Arc::new(SqliteStore::memory(4).unwrap());let scope=test_scope();write(&repo.0,"a.py","def target(): pass\ndef caller():\n target()\n");let first=store.code_index(&scope,&repo.0).unwrap();let before=logical_rows(&store,&scope,&first.codebase);
        {let conn=store.conn.lock().unwrap();conn.execute_batch("CREATE TRIGGER fail_rich_publish BEFORE INSERT ON code_resolutions BEGIN SELECT RAISE(ABORT,'fixture rich publication failure'); END;").unwrap();}
        write(&repo.0,"a.py","def changed(): pass\ndef caller():\n changed()\n");assert!(store.code_index(&scope,&repo.0).unwrap_err().contains("fixture rich publication failure"));assert_eq!(logical_rows(&store,&scope,&first.codebase),before);assert_eq!(store.code_index_head(&scope,&first.codebase).unwrap().unwrap().run_id,first.run_id);
        {let conn=store.conn.lock().unwrap();conn.execute_batch("DROP TRIGGER fail_rich_publish").unwrap();}
        write(&repo.0,"a.py","def target(): pass\ndef caller():\n target()\n");store.code_index(&scope,&repo.0).unwrap();let verified=store.code_index(&scope,&repo.0).unwrap();let stable=logical_rows(&store,&scope,&first.codebase);
        write(&repo.0,"a.py","def late_change(): pass\n");let gate=pause_next(&repo.0,"after_evidence_base");let job_store=store.clone();let root=repo.0.clone();let job=std::thread::spawn(move||job_store.code_index(&test_scope(),&root));gate.0.wait();
        {let conn=store.conn.lock().unwrap();conn.execute("UPDATE code_index_heads SET run_id=?1 WHERE owner=?2 AND workspace_id=?3 AND codebase=?4",params![verified.run_id,scope.owner,scope.workspace_id,first.codebase]).unwrap();}gate.1.wait();assert_eq!(job.join().unwrap().unwrap_err(),format!("publishing code index failed: {}",rusqlite::Error::InvalidQuery));assert_eq!(logical_rows(&store,&scope,&first.codebase),stable);assert_eq!(store.code_index_head(&scope,&first.codebase).unwrap().unwrap().run_id,verified.run_id);
    }

    #[test]
    fn rich_delta_repairs_unchanged_callers_and_failed_sources_remove_exactness(){
        use crate::code_evidence::*;
        let repo=fixture_repo();let store=SqliteStore::memory(4).unwrap();let scope=test_scope();write(&repo.0,"lib.py","def target(): pass\n");write(&repo.0,"caller.py","from lib import target\ndef caller():\n target()\n");let first=store.code_index(&scope,&repo.0).unwrap();
        let resolutions=||{let conn=store.conn.lock().unwrap();let mut stmt=conn.prepare("SELECT payload_json FROM code_resolutions WHERE owner=?1 AND rel_path='caller.py' ORDER BY occurrence_id").unwrap();stmt.query_map(params![scope.owner],|r|r.get::<_,String>(0)).unwrap().map(|r|serde_json::from_str::<ResolutionEvidence>(&r.unwrap()).unwrap()).collect::<Vec<_>>()};
        assert!(resolutions().iter().any(|r|r.state==ResolutionState::Exact && r.target_id.is_some()));
        write(&repo.0,"lib.py","def renamed(): pass\n");let changed=store.code_index(&scope,&repo.0).unwrap();assert_eq!(changed.files_unchanged,1);assert!(resolutions().iter().all(|r|r.state!=ResolutionState::Exact));
        write(&repo.0,"lib.py","def target(): pass\n");store.code_index(&scope,&repo.0).unwrap();assert!(resolutions().iter().any(|r|r.state==ResolutionState::Exact));
        write(&repo.0,"lib.py","def broken(:\n");let partial=store.code_index(&scope,&repo.0).unwrap();assert!(partial.coverage.iter().any(|c|c.rel_path=="lib.py" && c.status=="parse_failed"));assert!(resolutions().iter().all(|r|r.state!=ResolutionState::Exact));
        let conn=store.conn.lock().unwrap();assert_eq!(conn.query_row("SELECT count(*) FROM code_file_evidence WHERE rel_path='lib.py'",[],|r|r.get::<_,i64>(0)).unwrap(),0);drop(conn);store.code_remove(&scope,&first.codebase).unwrap();let conn=store.conn.lock().unwrap();for table in ["code_file_evidence","code_resolutions"]{assert_eq!(conn.query_row(&format!("SELECT count(*) FROM {table}"),[],|r|r.get::<_,i64>(0)).unwrap(),0);}
    }

    #[test]
    fn rich_owner_export_reset_ticket_and_purge_cover_complete_payloads(){
        let repo=fixture_repo();let store=SqliteStore::memory(4).unwrap();write(&repo.0,"a.py","def target(): pass\ndef caller():\n target()\n");let a=GraphScope::new("rich-alice","a").unwrap();let b=GraphScope::new("rich-bob","b").unwrap();let first=store.code_index(&a,&repo.0).unwrap();let other=store.code_index(&b,&repo.0).unwrap();let untouched=logical_rows(&store,&b,&other.codebase);
        let exported=store.export_scope(&a.owner,&a.workspace_id).unwrap();assert_eq!(exported["code_index"]["evidence"]["file_evidence"].as_array().unwrap().len(),1);assert!(!exported["code_index"]["evidence"]["resolutions"].as_array().unwrap().is_empty());assert!(!exported.to_string().contains(repo.0.to_str().unwrap()));
        let categories=vec!["graph".into()];let preview=store.reset_owner("reset_preview",&a.owner,&categories,None).unwrap();{let conn=store.conn.lock().unwrap();conn.execute("UPDATE code_file_evidence SET payload_json=json_set(payload_json,'$.diagnostics',json('[{\"code\":\"fixture\",\"message\":\"changed\",\"source\":null}]')) WHERE owner=?1",params![a.owner]).unwrap();}
        assert!(store.reset_owner("reset_commit",&a.owner,&categories,Some(&preview["components"])).is_err());let preview=store.reset_owner("reset_preview",&a.owner,&categories,None).unwrap();store.reset_owner("reset_commit",&a.owner,&categories,Some(&preview["components"])).unwrap();assert!(logical_rows(&store,&a,&first.codebase).is_empty());assert_eq!(logical_rows(&store,&b,&other.codebase),untouched);
        store.rename_owner(&b.owner,"rich-renamed").unwrap();let renamed=GraphScope::new("rich-renamed","b").unwrap();assert_eq!(store.export_scope(&renamed.owner,&renamed.workspace_id).unwrap()["code_index"]["evidence"]["file_evidence"].as_array().unwrap().len(),1);store.purge_owner(&renamed.owner).unwrap();let conn=store.conn.lock().unwrap();for table in ["code_file_evidence","code_resolutions"]{assert_eq!(conn.query_row(&format!("SELECT count(*) FROM {table}"),[],|r|r.get::<_,i64>(0)).unwrap(),0);}
    }
    fn evidence_binding(store:&SqliteStore,scope:&GraphScope,root:&std::path::Path,authority:&std::path::Path)->(crate::code::WorkspaceBinding,AuthorityEnv){
        let root=root.canonicalize().unwrap();
        let conn=rusqlite::Connection::open(authority).unwrap();
        conn.execute_batch("CREATE TABLE IF NOT EXISTS file_policy_subject_aliases(username TEXT,subject_id TEXT,is_admin INTEGER);CREATE TABLE IF NOT EXISTS file_policy_workspaces(id TEXT,owner_subject_id TEXT,location_id TEXT,relative_folder TEXT,archived INTEGER,revision INTEGER);CREATE TABLE IF NOT EXISTS file_policy_locations(id TEXT,canonical_path TEXT,capabilities_json TEXT,enabled INTEGER,availability TEXT);CREATE TABLE IF NOT EXISTS file_policy_meta(singleton INTEGER,generation INTEGER);CREATE TABLE IF NOT EXISTS file_policy_bindings(binding_class TEXT,subject_kind TEXT,subject_id TEXT,location_id TEXT,status TEXT,expires_unix_ms INTEGER,workspace_id TEXT,chat_id TEXT,lifetime TEXT,remaining_uses INTEGER,capabilities_json TEXT);INSERT INTO file_policy_meta SELECT 1,1 WHERE NOT EXISTS(SELECT 1 FROM file_policy_meta);").unwrap();
        let subject=format!("subject-{}",scope.owner);let location=format!("location-{}-{}",scope.owner,scope.workspace_id);
        conn.execute("INSERT INTO file_policy_subject_aliases VALUES(?1,?2,1)",params![scope.owner,subject]).unwrap();
        conn.execute("INSERT INTO file_policy_workspaces VALUES(?1,?2,?3,'',0,1)",params![scope.workspace_id,subject,location]).unwrap();
        conn.execute("INSERT INTO file_policy_locations VALUES(?1,?2,'[\"read\"]',1,'available')",params![location,root.to_str().unwrap()]).unwrap();drop(conn);
        let env=AuthorityEnv(std::env::var_os("OPEN_CLANK_AUTHORITY_DB_PATH"));std::env::set_var("OPEN_CLANK_AUTHORITY_DB_PATH",authority);
        let binding=crate::code::WorkspaceBinding{owner:scope.owner.clone(),workspace_id:scope.workspace_id.clone(),owner_subject_id:subject,location_id:location,workspace_revision:1,policy_generation:1,root,origin:"app".into(),subscriber_id:"evidence-fixture".into(),authority_expires_unix_ms:None};
        store.code_workspace_prepare(scope,&binding,false).unwrap();
        (binding,env)
    }

    #[test]
    fn evidence_wrappers_validate_dependency_path_span_policy_and_receipt(){
        use crate::store::code_evidence::*;use crate::code_evidence::*;
        let _lock=AUTHORITY_LOCK.lock().unwrap();let repo=fixture_repo();let auth=fixture_repo();let store=SqliteStore::memory(4).unwrap();let scope=test_scope();write(&repo.0,"lib.py","def target(): pass\n");write(&repo.0,"caller.py","from lib import target\ndef caller():\n target()\n");store.code_index(&scope,&repo.0).unwrap();let authority=auth.0.join("authority.db");let (binding,_env)=evidence_binding(&store,&scope,&repo.0,&authority);let receipt=store.code_evidence_receipt(&scope,&binding).unwrap();
        let page=store.code_evidence_page(&scope,&binding,&receipt,CodeEvidenceQuery{kind:CodeEvidenceKind::Resolutions,rel_path:Some("caller.py".into()),..Default::default()}).unwrap();let claim=page.rows.iter().map(|row|serde_json::from_value::<ResolutionEvidence>(row["evidence"].clone()).unwrap()).find(|r|r.state==ResolutionState::Exact).unwrap();
        assert_eq!(store.code_validate_resolution(&scope,&binding,&receipt,&claim).unwrap().state,ValidationState::Current);assert_eq!(store.code_validate_path(&scope,&binding,&receipt,&[claim.occurrence_id.clone()]).unwrap().state,ValidationState::Current);
        let raw=SourceClaim{source:claim.source.clone(),predicate:SourcePredicate::ExactText{text:"target".into()}};assert_eq!(store.code_validate_source(&scope,&binding,&receipt,&raw).unwrap().state,ValidationState::Current);let mut invalid=raw.clone();invalid.source.span.end_byte=999999;assert_eq!(store.code_validate_source(&scope,&binding,&receipt,&invalid).unwrap().state,ValidationState::Invalid);
        assert_eq!(store.code_validate_path(&scope,&binding,&receipt,&vec![claim.occurrence_id.clone();65]).unwrap_err().code,CodeEvidenceErrorCode::BudgetExceeded);
        write(&repo.0,"unsupported.js","function outside_rich_grammar() {}\n");let mut unsupported=raw.clone();unsupported.source.rel_path="unsupported.js".into();unsupported.predicate=SourcePredicate::File;assert_eq!(store.code_validate_source(&scope,&binding,&receipt,&unsupported).unwrap_err().code,CodeEvidenceErrorCode::Missing,"canonical-only API must not grant generic raw file access");
        write(&repo.0,"lib.py","def changed(): pass\n");assert_eq!(store.code_validate_resolution(&scope,&binding,&receipt,&claim).unwrap().state,ValidationState::Stale);assert_eq!(store.code_validate_path(&scope,&binding,&receipt,&[claim.occurrence_id.clone()]).unwrap().state,ValidationState::Stale);
        store.code_index(&scope,&repo.0).unwrap();assert_eq!(store.code_validate_resolution(&scope,&binding,&receipt,&claim).unwrap_err().code,CodeEvidenceErrorCode::StaleCursor);
        let current=store.code_evidence_receipt(&scope,&binding).unwrap();{let conn=rusqlite::Connection::open(&authority).unwrap();conn.execute("UPDATE file_policy_meta SET generation=2",[]).unwrap();}assert_eq!(store.code_validate_source(&scope,&binding,&current,&raw).unwrap_err().code,CodeEvidenceErrorCode::Denied);
    }

    #[test]
    fn evidence_preflight_budget_failure_preserves_prior_rich_head(){
        let repo=fixture_repo();let store=SqliteStore::memory(4).unwrap();let scope=test_scope();write(&repo.0,"a.py","def original(): pass\n");let first=store.code_index(&scope,&repo.0).unwrap();let before=logical_rows(&store,&scope,&first.codebase);
        {let conn=store.conn.lock().unwrap();let key=SqliteStore::codebase_key(&scope,&first.codebase);conn.execute("WITH RECURSIVE ids(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM ids WHERE n<10001) INSERT INTO code_files(codebase,rel_path,blake3,mtime_ns,size,symbol_count,indexed_at,config_fingerprint,source_generation) SELECT ?1,'budget-'||n||'.py',?2,0,0,0,'fixture',?3,?4 FROM ids",params![key,"0".repeat(64),crate::code::producer_fingerprint(),first.run_id]).unwrap();}
        assert!(store.code_index(&scope,&repo.0).unwrap_err().starts_with("budget_exceeded:"));assert_eq!(logical_rows(&store,&scope,&first.codebase),before);assert_eq!(store.code_index_head(&scope,&first.codebase).unwrap().unwrap().run_id,first.run_id);let conn=store.conn.lock().unwrap();assert_eq!(conn.query_row("SELECT count(*) FROM code_index_runs WHERE status='running'",[],|r|r.get::<_,i64>(0)).unwrap(),0);
    }

    #[test]
    fn s01_stored_validation_rejects_ignored_or_unpinned_policy_before_read() {
        use crate::store::code_evidence::*; use crate::code_evidence::*;
        let _lock = AUTHORITY_LOCK.lock().unwrap();
        for change in ["ignored", "missing-policy"] {
            let repo=fixture_repo(); let auth=fixture_repo(); let store=SqliteStore::memory(4).unwrap(); let scope=test_scope();
            write(&repo.0,"main.py","def private_after_policy_change(): pass\n");
            store.code_index(&scope,&repo.0).unwrap();
            let (binding,_env)=evidence_binding(&store,&scope,&repo.0,&auth.0.join("authority.db"));
            let receipt=store.code_evidence_receipt(&scope,&binding).unwrap();
            let page=store.code_evidence_page(&scope,&binding,&receipt,CodeEvidenceQuery{kind:CodeEvidenceKind::Declarations,..Default::default()}).unwrap();
            let declaration:DeclarationEvidence=serde_json::from_value(page.rows[0]["evidence"].clone()).unwrap();
            let claim=SourceClaim{source:declaration.source,predicate:SourcePredicate::File};
            if change=="ignored" { write(&repo.0,".gitignore","main.py\n"); }
            else { store.conn.lock().unwrap().execute("UPDATE code_index_runs SET source_snapshot=json_remove(source_snapshot,'$.selection_policy_digest') WHERE run_id=?1",params![receipt.head_generation]).unwrap(); }
            let trace=trace_validation_reads(&binding.root);
            let (result,bytes)=store.code_validate_source_measured(&scope,&binding,&receipt,&claim);
            finish_validation_fixture(&binding.root);
            assert_eq!(result.unwrap_err().code,CodeEvidenceErrorCode::ReindexRequired,"{change}");
            assert_eq!(bytes,0); assert!(trace.lock().unwrap().is_empty(),"{change} must prevent the first source read");
        }
    }

    #[test]
    fn evidence_revocation_between_files_prevents_further_source_acquisition(){
        use crate::store::code_evidence::*;use crate::code_evidence::*;
        let _lock=AUTHORITY_LOCK.lock().unwrap();
        for change in ["policy","erase","head","ignore","ignore-second-pass"]{
            let repo=fixture_repo();let auth=fixture_repo();let store=std::sync::Arc::new(SqliteStore::memory(4).unwrap());let scope=test_scope();write(&repo.0,"a.py","def first(): pass\n");if change!="ignore-second-pass"{write(&repo.0,"b.py","def second(): pass\n");}let first=store.code_index(&scope,&repo.0).unwrap();let verified=store.code_index(&scope,&repo.0).unwrap();let authority=auth.0.join("authority.db");let (binding,_env)=evidence_binding(&store,&scope,&repo.0,&authority);let receipt=store.code_evidence_receipt(&scope,&binding).unwrap();let page=store.code_evidence_page(&scope,&binding,&receipt,CodeEvidenceQuery{kind:CodeEvidenceKind::Declarations,rel_path:Some("a.py".into()),..Default::default()}).unwrap();let declaration:DeclarationEvidence=serde_json::from_value(page.rows[0]["evidence"].clone()).unwrap();let claim=SourceClaim{source:declaration.source,predicate:SourcePredicate::ExactText{text:"def first(): pass".into()}};
            let (gate,trace)=pause_next_validation(&binding.root);let trace_root=binding.root.clone();let job_store=store.clone();let job=std::thread::spawn(move||job_store.code_validate_source(&test_scope(),&binding,&receipt,&claim));gate.0.wait();
            let expected=match change{
                "policy"=>{let conn=rusqlite::Connection::open(&authority).unwrap();conn.execute("UPDATE file_policy_meta SET generation=2",[]).unwrap();CodeEvidenceErrorCode::Denied},
                "erase"=>{store.code_remove(&scope,&first.codebase).unwrap();CodeEvidenceErrorCode::Erased},
                "ignore"=>{write(&repo.0,".gitignore","b.py\n");CodeEvidenceErrorCode::ReindexRequired},
                "ignore-second-pass"=>{write(&repo.0,".gitignore","a.py\n");CodeEvidenceErrorCode::ReindexRequired},
                _=>{let conn=store.conn.lock().unwrap();conn.execute("UPDATE code_index_heads SET run_id=?1 WHERE owner=?2 AND workspace_id=?3 AND codebase=?4",params![verified.run_id,scope.owner,scope.workspace_id,first.codebase]).unwrap();CodeEvidenceErrorCode::StaleCursor}
            };
            gate.1.wait();assert_eq!(job.join().unwrap().unwrap_err().code,expected);finish_validation_fixture(&trace_root);assert_eq!(*trace.lock().unwrap(),vec!["a.py".to_string()],"{change} must prevent second-file acquisition");
        }
    }

    #[test]
    fn oversized_source_coverage_is_partial_not_current_complete(){
        use crate::store::code_evidence::*;
        let _lock=AUTHORITY_LOCK.lock().unwrap();let repo=fixture_repo();let auth=fixture_repo();let store=SqliteStore::memory(4).unwrap();let scope=test_scope();write(&repo.0,"a.py","def accepted(): pass\n");write(&repo.0,"too_big.py",&"#".repeat(512*1024+1));let indexed=store.code_index(&scope,&repo.0).unwrap();assert!(indexed.coverage.iter().any(|c|c.rel_path=="too_big.py" && c.status=="oversize"));let (binding,_env)=evidence_binding(&store,&scope,&repo.0,&auth.0.join("authority.db"));let receipt=store.code_evidence_receipt(&scope,&binding).unwrap();let page=store.code_evidence_page(&scope,&binding,&receipt,Default::default()).unwrap();assert_eq!(page.availability,CodeEvidenceAvailability::Partial);assert!(page.partial);assert_eq!(page.evidence_files,1);assert!(!page.current_source_verified);
    }

    #[test]
    fn evidence_metadata_distinguishes_missing_empty_reindex_required_and_erased(){
        use crate::store::code_evidence::*;
        let _lock=AUTHORITY_LOCK.lock().unwrap();let repo=fixture_repo();let auth=fixture_repo();let store=SqliteStore::memory(4).unwrap();let scope=test_scope();let (binding,_env)=evidence_binding(&store,&scope,&repo.0,&auth.0.join("authority.db"));assert_eq!(store.code_evidence_receipt(&scope,&binding).unwrap_err().code,CodeEvidenceErrorCode::Missing);
        store.code_index(&scope,&repo.0).unwrap();let empty=store.code_evidence_receipt(&scope,&binding).unwrap();let page=store.code_evidence_page(&scope,&binding,&empty,Default::default()).unwrap();assert_eq!(page.availability,CodeEvidenceAvailability::Empty);assert!(page.rows.is_empty());assert!(!page.partial);
        write(&repo.0,"a.py","def admitted(): pass\n");store.code_index(&scope,&repo.0).unwrap();let ready=store.code_evidence_receipt(&scope,&binding).unwrap();{let conn=store.conn.lock().unwrap();conn.execute("DELETE FROM code_file_evidence WHERE owner=?1 AND workspace_id=?2",params![scope.owner,scope.workspace_id]).unwrap();}
        assert_eq!(store.code_evidence_receipt(&scope,&binding).unwrap_err().code,CodeEvidenceErrorCode::ReindexRequired);let page=store.code_evidence_page(&scope,&binding,&ready,Default::default()).unwrap();assert_eq!(page.availability,CodeEvidenceAvailability::ReindexRequired);assert!(page.rows.is_empty());assert!(!page.current_source_verified);
        store.code_remove(&scope,&ready.checkout_id).unwrap();assert_eq!(store.code_evidence_receipt(&scope,&binding).unwrap_err().code,CodeEvidenceErrorCode::Erased);
    }

    #[test]
    fn evidence_coverage_preflight_bounds_bytes_records_and_rejects_malformed_receipts(){
        use crate::store::code_evidence::*;
        let _lock=AUTHORITY_LOCK.lock().unwrap();let repo=fixture_repo();let auth=fixture_repo();let store=SqliteStore::memory(4).unwrap();let scope=test_scope();write(&repo.0,"a.py","def accepted(): pass\n");store.code_index(&scope,&repo.0).unwrap();let (binding,_env)=evidence_binding(&store,&scope,&repo.0,&auth.0.join("authority.db"));let receipt=store.code_evidence_receipt(&scope,&binding).unwrap();
        let conn=store.conn.lock().unwrap();assert_eq!(coverage_test_budget(&conn,&scope,&receipt,1,100).unwrap_err().code,CodeEvidenceErrorCode::BudgetExceeded,"SQL byte gate before loading coverage");assert_eq!(coverage_test_budget(&conn,&scope,&receipt,1024*1024,0).unwrap_err().code,CodeEvidenceErrorCode::BudgetExceeded,"SQL record gate before loading coverage");assert_eq!(coverage_test_budget(&conn,&scope,&receipt,1024*1024,100).unwrap(),1);let mut missing=receipt.clone();missing.head_generation="missing".into();assert_eq!(coverage_test_budget(&conn,&scope,&missing,1024,100).unwrap_err().code,CodeEvidenceErrorCode::Missing);drop(conn);
        for malformed in ["not-json","{}","[null]","[7]"]{let conn=store.conn.lock().unwrap();conn.execute("UPDATE code_index_runs SET coverage=?1 WHERE run_id=?2",params![malformed,receipt.head_generation]).unwrap();drop(conn);assert_eq!(store.code_evidence_page(&scope,&binding,&receipt,Default::default()).unwrap_err().code,CodeEvidenceErrorCode::InvalidEvidence);}
    }

    #[test]
    fn evidence_pages_bound_cursor_provenance_and_unchanged_generation(){
        use crate::store::code_evidence::*;
        let _lock=AUTHORITY_LOCK.lock().unwrap();let repo=fixture_repo();let auth=fixture_repo();let store=SqliteStore::memory(4).unwrap();let scope=test_scope();
        write(&repo.0,"a.py","def target(): pass\ndef caller():\n target()\n target()\n");
        let first=store.code_index(&scope,&repo.0).unwrap();let (binding,_env)=evidence_binding(&store,&scope,&repo.0,&auth.0.join("authority.db"));
        let receipt=store.code_evidence_receipt(&scope,&binding).unwrap();
        for kind in [CodeEvidenceKind::Files,CodeEvidenceKind::Declarations,CodeEvidenceKind::Occurrences,CodeEvidenceKind::Resolutions]{
            let query=CodeEvidenceQuery{kind,max_rows:1,..Default::default()};let page=store.code_evidence_page(&scope,&binding,&receipt,query.clone()).unwrap();assert_eq!(page.returned_rows,1);assert_eq!(page.returned_bytes,serde_json::to_vec(&page).unwrap().len());assert!(!page.current_source_verified);assert_eq!(page.availability,CodeEvidenceAvailability::Current);
            if let Some(cursor)=page.next_cursor{let next=store.code_evidence_page(&scope,&binding,&receipt,CodeEvidenceQuery{cursor:Some(cursor.clone()),..query.clone()}).unwrap();assert_ne!(page.rows,next.rows);assert_eq!(store.code_evidence_page(&scope,&binding,&receipt,CodeEvidenceQuery{max_rows:2,cursor:Some(cursor),..query}).unwrap_err().code,CodeEvidenceErrorCode::StaleCursor);}
        }
        let again=store.code_index(&scope,&repo.0).unwrap();assert_eq!(again.files_unchanged,1);assert_eq!(store.code_evidence_receipt(&scope,&binding).unwrap(),receipt);
        let conn=store.conn.lock().unwrap();assert_eq!(conn.query_row("SELECT count(*) FROM code_resolutions WHERE resolution_generation!=?1",params![first.run_id],|r|r.get::<_,i64>(0)).unwrap(),0);drop(conn);
        for max_rows in [0,MAX_PAGE_ROWS+1]{assert_eq!(store.code_evidence_page(&scope,&binding,&receipt,CodeEvidenceQuery{max_rows,..Default::default()}).unwrap_err().code,CodeEvidenceErrorCode::Invalid);}
        assert_eq!(store.code_evidence_page(&scope,&binding,&receipt,CodeEvidenceQuery{max_bytes:100,..Default::default()}).unwrap_err().code,CodeEvidenceErrorCode::BudgetExceeded);
        let bob=GraphScope::new("bob","bob-workspace").unwrap();let (bob_binding,_bob_env)=evidence_binding(&store,&bob,&repo.0,&auth.0.join("authority.db"));bob_binding.validate().unwrap();assert_eq!(store.code_evidence_page(&scope,&bob_binding,&receipt,Default::default()).unwrap_err().code,CodeEvidenceErrorCode::Denied);
        for field in ["declarations","occurrences","source_len","file_id"]{
            let conn=store.conn.lock().unwrap();let original:String=conn.query_row("SELECT payload_json FROM code_file_evidence WHERE owner=?1",params![scope.owner],|r|r.get(0)).unwrap();conn.execute("UPDATE code_file_evidence SET payload_json=json_remove(payload_json,?2) WHERE owner=?1",params![scope.owner,format!("$.{field}")]).unwrap();drop(conn);
            assert_eq!(store.code_evidence_page(&scope,&binding,&receipt,Default::default()).unwrap_err().code,CodeEvidenceErrorCode::InvalidEvidence,"missing {field}");let conn=store.conn.lock().unwrap();conn.execute("UPDATE code_file_evidence SET payload_json=?2 WHERE owner=?1",params![scope.owner,original]).unwrap();
        }
        let conn=store.conn.lock().unwrap();conn.execute("UPDATE code_resolutions SET payload_json=json_set(payload_json,'$.source.source_hash',?1)",params!["0".repeat(64)]).unwrap();drop(conn);assert_eq!(store.code_evidence_page(&scope,&binding,&receipt,Default::default()).unwrap_err().code,CodeEvidenceErrorCode::InvalidEvidence);
    }

    #[test]
    fn evidence_page_many_occurrences_is_bounded_and_disk_currency_is_separate(){
        use crate::store::code_evidence::*;use crate::code_evidence::*;
        let _lock=AUTHORITY_LOCK.lock().unwrap();let repo=fixture_repo();let auth=fixture_repo();let store=SqliteStore::memory(4).unwrap();let scope=test_scope();
        let source=format!("def target(): pass\ndef caller():\n{}", " target()\n".repeat(1000));write(&repo.0,"many.py",&source);store.code_index(&scope,&repo.0).unwrap();let (binding,_env)=evidence_binding(&store,&scope,&repo.0,&auth.0.join("authority.db"));let receipt=store.code_evidence_receipt(&scope,&binding).unwrap();
        let start=std::time::Instant::now();let page=store.code_evidence_page(&scope,&binding,&receipt,CodeEvidenceQuery{kind:CodeEvidenceKind::Resolutions,max_rows:1,..Default::default()}).unwrap();eprintln!("S04 bounded page occurrences=1000 rows={} elapsed_ms={:.3}",page.rows.len(),start.elapsed().as_secs_f64()*1000.0);assert_eq!(page.rows.len(),1);assert!(page.next_cursor.is_some());
        let resolution:ResolutionEvidence=serde_json::from_value(page.rows[0]["evidence"].clone()).unwrap();assert_eq!(store.code_validate_resolution(&scope,&binding,&receipt,&resolution).unwrap().state,ValidationState::Current);
        let claim=SourceClaim{source:resolution.source.clone(),predicate:SourcePredicate::Occurrence{occurrence_id:resolution.occurrence_id.clone(),occurrence_kind:OccurrenceKind::Call,symbol_text:"target".into()}};
        assert_eq!(store.code_validate_source(&scope,&binding,&receipt,&claim).unwrap().state,ValidationState::Current);
        write(&repo.0,"many.py",&source.replace("def target(): pass","def otherx(): pass"));let still=store.code_evidence_page(&scope,&binding,&receipt,Default::default()).unwrap();assert_eq!(still.availability,CodeEvidenceAvailability::Current);assert!(!still.current_source_verified);assert_eq!(store.code_validate_source(&scope,&binding,&receipt,&claim).unwrap().state,ValidationState::Stale);assert_eq!(store.code_validate_resolution(&scope,&binding,&receipt,&resolution).unwrap().state,ValidationState::Stale);
    }

    fn pause_next(root:&std::path::Path,phase:&str)->IndexTestGate{
        let gate=std::sync::Arc::new((std::sync::Barrier::new(2),std::sync::Barrier::new(2)));
        INDEX_TEST_GATES.get_or_init(Default::default).lock().unwrap().insert((root.to_path_buf(),phase.into()),gate.clone());gate
    }

    #[test]
    fn literal_code_paths_and_file_owned_deletion_keep_distinct_siblings(){
        let repo=fixture_repo();let store=SqliteStore::memory(4).unwrap();let scope=test_scope();
        for (i,path) in ["a_b.py","aXb.py","a%b.py","a::b.py","real\\name.py","with space.py"].iter().enumerate(){write(&repo.0,path,&format!("def symbol_{i}(): pass\n"));}
        let first=store.code_index(&scope,&repo.0).unwrap();let before=logical_rows(&store,&scope,&first.codebase);
        write(&repo.0,"a_b.py","def changed(): pass\n");let delta=store.code_index(&scope,&repo.0).unwrap();assert_eq!(delta.files_indexed,1);assert_eq!(delta.files_unchanged,5);
        let after=logical_rows(&store,&scope,&first.codebase);for i in 1..6{assert!(after.iter().any(|r|r.contains(&format!("symbol_{i}"))));}
        assert!(before.iter().any(|r|r.contains("symbol_0")));assert!(!after.iter().any(|r|r.contains("symbol_0")));
        let namespace=format!("{}::",first.codebase);
        let names=["Foo.py","foo.py","with  space.py","with space.py","a%.py","a_.py","a\\.py"];
        let ids:HashSet<_>=names.iter().map(|path|scope.node_id("file",&format!("{namespace}{path}"))).collect();assert_eq!(ids.len(),names.len());
        assert_eq!(scope.node_id("entity","Ordinary  Memory"),scope.node_id("entity","ordinary memory"));
    }

    #[test]
    fn stale_writer_is_fenced_before_any_graph_mutation_and_ready_head_survives(){
        let repo=fixture_repo();let db_repo=fixture_repo();let db=db_repo.0.join("index.sqlite");let store=std::sync::Arc::new(SqliteStore::new(db.to_str().unwrap(),4).unwrap());let successor_store=SqliteStore::new(db.to_str().unwrap(),4).unwrap();let scope=test_scope();write(&repo.0,"main.py","def previous(): pass\n");let prior=store.code_index(&scope,&repo.0).unwrap();
        write(&repo.0,"main.py","def stale_writer(): pass\n");let gate=pause_next(&repo.0,"before_publish");let job_store=store.clone();let job_root=repo.0.clone();let job=std::thread::spawn(move||job_store.code_index(&test_scope(),&job_root));gate.0.wait();
        assert_eq!(store.code_index_head(&scope,&prior.codebase).unwrap().unwrap().run_id,prior.run_id);
        // Hold the old connection mutex while the independent connection
        // expires/claims, preventing its heartbeat from racing fixture setup.
        write(&repo.0,"main.py","def successor(): pass\n");let successor={let _old=store.conn.lock().unwrap();{let conn=successor_store.conn.lock().unwrap();conn.execute("UPDATE code_index_runs SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE status='running'",[]).unwrap();}successor_store.code_index(&scope,&repo.0).unwrap()};let expected=logical_rows(&successor_store,&scope,&prior.codebase);
        gate.1.wait();assert!(job.join().unwrap().is_err());assert_eq!(expected,logical_rows(&store,&scope,&prior.codebase));assert_eq!(store.code_index_head(&scope,&prior.codebase).unwrap().unwrap().run_id,successor.run_id);
    }

    #[test]
    fn independent_heartbeat_protects_a_held_scan_despite_old_start_time(){
        let repo=fixture_repo();let store=std::sync::Arc::new(SqliteStore::memory(4).unwrap());write(&repo.0,"main.py","def alive(): pass\n");let gate=pause_next(&repo.0,"before_scan");let cloned=store.clone();let root=repo.0.clone();let job=std::thread::spawn(move||cloned.code_index(&test_scope(),&root));gate.0.wait();
        let before:String={let conn=store.conn.lock().unwrap();conn.execute("UPDATE code_index_runs SET started_at='2000-01-01T00:00:00+00:00' WHERE status='running'",[]).unwrap();conn.query_row("SELECT heartbeat_at FROM code_index_runs WHERE status='running'",[],|r|r.get(0)).unwrap()};
        std::thread::sleep(std::time::Duration::from_millis(1200));
        let conn=store.conn.lock().unwrap();let (checkout,heartbeat):(String,String)=conn.query_row("SELECT codebase,heartbeat_at FROM code_index_runs WHERE status='running'",[],|r|Ok((r.get(0)?,r.get(1)?))).unwrap();assert_ne!(before,heartbeat);
        let snapshot=crate::code::source_snapshot(&repo.0,&[]).unwrap();assert!(store.begin_code_index_run(&conn,&test_scope(),&checkout,&snapshot).unwrap_err().contains("already active"));drop(conn);gate.1.wait();assert!(job.join().unwrap().is_ok());
    }

    #[cfg(unix)]
    #[test]
    fn root_swap_after_claim_never_enumerates_replacement_or_changes_ready_rows(){
        let repo=fixture_repo();let other=fixture_repo();let store=std::sync::Arc::new(SqliteStore::memory(4).unwrap());write(&repo.0,"main.py","def original(): pass\n");write(&other.0,"private_canary.py","def external(): pass\n");let first=store.code_index(&test_scope(),&repo.0).unwrap();let expected=logical_rows(&store,&test_scope(),&first.codebase);
        let gate=pause_next(&repo.0,"before_scan");let root=repo.0.clone();let cloned=store.clone();let job=std::thread::spawn(move||cloned.code_index(&test_scope(),&root));gate.0.wait();let moved=repo.0.with_extension("original");std::fs::rename(&repo.0,&moved).unwrap();std::os::unix::fs::symlink(&other.0,&repo.0).unwrap();gate.1.wait();assert!(job.join().unwrap().is_err());assert_eq!(expected,logical_rows(&store,&test_scope(),&first.codebase));assert_eq!(store.code_index_head(&test_scope(),&first.codebase).unwrap().unwrap().run_id,first.run_id);
        {let conn=store.conn.lock().unwrap();let coverage:String=conn.query_row("SELECT coverage FROM code_index_runs ORDER BY rowid DESC LIMIT 1",[],|r|r.get(0)).unwrap();assert!(!coverage.contains("private_canary"));}
        std::fs::remove_file(&repo.0).unwrap();std::fs::rename(moved,&repo.0).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn incomplete_enumeration_preserves_last_ready_but_real_mass_deletion_publishes(){
        use std::os::unix::fs::PermissionsExt;
        let repo=fixture_repo();let store=SqliteStore::memory(4).unwrap();for i in 0..80{write(&repo.0,&format!("sources/f{i}.py"),&format!("def n{i}(): pass\n"));}let first=store.code_index(&test_scope(),&repo.0).unwrap();
        std::fs::set_permissions(repo.0.join("sources"),std::fs::Permissions::from_mode(0)).unwrap();assert!(store.code_index(&test_scope(),&repo.0).is_err());assert_eq!(store.code_index_head(&test_scope(),&first.codebase).unwrap().unwrap().run_id,first.run_id);
        std::fs::set_permissions(repo.0.join("sources"),std::fs::Permissions::from_mode(0o700)).unwrap();std::fs::remove_dir_all(repo.0.join("sources")).unwrap();let deleted=store.code_index(&test_scope(),&repo.0).unwrap();assert_eq!(deleted.files_removed,80);assert_eq!(deleted.coverage.iter().filter(|e|e.status=="deleted").count(),80);assert!(logical_rows(&store,&test_scope(),&first.codebase).is_empty());
    }

    #[test]
    fn registered_identity_is_config_independent_distinct_and_relocation_verified() {
        let a=fixture_repo();let b=fixture_repo();let store=SqliteStore::memory(4).unwrap();
        for root in [&a.0,&b.0]{write(root,".git/config","[core]\n bare=false\n");write(root,"main.py","def same(): pass\n");}
        let first=store.code_index(&test_scope(),&a.0).unwrap();let other=store.code_index(&test_scope(),&b.0).unwrap();
        assert_ne!(first.repository_id,other.repository_id);assert_ne!(first.codebase,other.codebase);
        write(&a.0,".git/config","[remote \"origin\"]\n url=changed\n");
        assert_eq!(store.code_index(&test_scope(),&a.0).unwrap().repository_id,first.repository_id);
        let moved=a.0.with_extension("relocated");std::fs::rename(&a.0,&moved).unwrap();
        assert_eq!(store.code_index(&test_scope(),&moved).unwrap().codebase,first.codebase);
        std::fs::rename(moved,&a.0).unwrap();
    }

    fn logical_rows(store:&SqliteStore,scope:&GraphScope,checkout:&str)->Vec<String>{
        let conn=store.conn.lock().unwrap();let prefix=format!("{checkout}::");
        let mut rows=Vec::new();
        for sql in ["SELECT json_array(id,kind,name,label,layer,trust) FROM graph_nodes WHERE owner=?1 AND workspace_id=?2 AND substr(name,1,length(?3))=?3 ORDER BY id","SELECT json_array(e.id,e.src_id,e.tag,e.dst_id,e.fact_id,e.trust) FROM graph_edges e JOIN graph_nodes n ON n.id=e.src_id WHERE e.owner=?1 AND e.workspace_id=?2 AND substr(n.name,1,length(?3))=?3 ORDER BY e.id","SELECT json_array(c.cue,c.node_id,c.source,c.status) FROM graph_cues c JOIN graph_nodes n ON n.id=c.node_id WHERE c.owner=?1 AND c.workspace_id=?2 AND substr(n.name,1,length(?3))=?3 ORDER BY c.cue,c.node_id"]{
            rows.extend(conn.prepare(sql).unwrap().query_map(params![scope.owner,scope.workspace_id,prefix],|r|r.get::<_,String>(0)).unwrap().map(Result::unwrap));
        }
        for table in ["code_file_evidence","code_resolutions"]{
            rows.extend(conn.prepare(&format!("SELECT payload_json FROM {table} WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3 ORDER BY payload_json")).unwrap().query_map(params![scope.owner,scope.workspace_id,checkout],|r|r.get::<_,String>(0)).unwrap().map(Result::unwrap));
        }
        rows
    }

    fn declaration_node(store:&SqliteStore,scope:&GraphScope,checkout:&str,rel:&str,name:&str)->String{
        let conn=store.conn.lock().unwrap();
        let json:String=conn.query_row("SELECT payload_json FROM code_file_evidence WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3 AND rel_path=?4",params![scope.owner,scope.workspace_id,checkout,rel],|r|r.get(0)).unwrap();
        let file:crate::code_evidence::FileEvidence=serde_json::from_str(&json).unwrap();
        let declaration=file.declarations.iter().find(|d|d.qualified_name==name).unwrap();
        let id=scope.node_id("code_symbol",&format!("{checkout}::{rel}::{}",declaration.id));
        assert_eq!(conn.query_row("SELECT label FROM graph_nodes WHERE id=?1",params![id],|r|r.get::<_,String>(0)).unwrap(),name);
        id
    }

    #[test]
    fn full_and_delta_logical_rows_match_after_rename_delete_readd_and_parser_loss(){
        let repo=fixture_repo();let store=SqliteStore::memory(4).unwrap();let scope=test_scope();
        write(&repo.0,"a.py","from oldmodule import x\ndef alpha():\n x()\n");write(&repo.0,"b.py","def beta(): pass\n");
        let first=store.code_index(&scope,&repo.0).unwrap();
        std::fs::rename(repo.0.join("a.py"),repo.0.join("renamed.py")).unwrap();std::fs::remove_file(repo.0.join("b.py")).unwrap();
        write(&repo.0,"renamed.py","def alpha():\n newcall()\n");write(&repo.0,"empty.py","# no units\n");
        let delta=store.code_index(&scope,&repo.0).unwrap();assert_eq!(delta.files_removed,2);assert!(delta.coverage.iter().any(|e|e.status=="no_source_units"));
        let logical=logical_rows(&store,&scope,&first.codebase);
        let clean=SqliteStore::memory(4).unwrap();
        {let source=store.conn.lock().unwrap();let target=clean.conn.lock().unwrap();let (registration,checkout,binding,identity,config):(String,String,String,String,String)=source.query_row("SELECT registration_id,checkout_id,root_binding,directory_identity,config_fingerprint FROM code_registrations WHERE checkout_id=?1",params![first.codebase],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).unwrap();target.execute("INSERT INTO code_registrations(owner,workspace_id,registration_id,checkout_id,root_binding,directory_identity,config_fingerprint) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![scope.owner,scope.workspace_id,registration,checkout,binding,identity,config]).unwrap();}
        let full=clean.code_index(&scope,&repo.0).unwrap();assert_eq!(logical,logical_rows(&clean,&scope,&first.codebase));
        assert_eq!(delta.source_snapshot.file_manifest_digest,full.source_snapshot.file_manifest_digest);
        let file_receipts=|s:&SqliteStore|{let conn=s.conn.lock().unwrap();let mut stmt=conn.prepare("SELECT json_array(rel_path,blake3,size,symbol_count,config_fingerprint) FROM code_files ORDER BY rel_path").unwrap();let values=stmt.query_map([],|r|r.get::<_,String>(0)).unwrap().map(Result::unwrap).collect::<Vec<_>>();values};
        assert_eq!(file_receipts(&store),file_receipts(&clean));
        let coverage=|r:&CodeIndexResult|r.coverage.iter().filter(|e|e.status!="deleted").map(|e|(e.rel_path.clone(),if e.status=="unchanged"{"indexed".to_string()}else{e.status.clone()})).collect::<Vec<_>>();
        assert_eq!(coverage(&delta),coverage(&full));
        {let conn=store.conn.lock().unwrap();conn.execute("UPDATE code_files SET config_fingerprint=''",[]).unwrap();}
        store.code_index(&scope,&repo.0).unwrap();assert_eq!(logical,logical_rows(&store,&scope,&first.codebase));
        write(&repo.0,"renamed.py","def broken(:\n");let partial=store.code_index(&scope,&repo.0).unwrap();assert!(!partial.errors.is_empty());
        assert!(!logical_rows(&store,&scope,&first.codebase).iter().any(|r|r.contains("alpha")||r.contains("newcall")));
        write(&repo.0,"renamed.py","def repaired(): pass\n");assert!(store.code_index(&scope,&repo.0).unwrap().errors.is_empty());
    }

    #[test]
    fn erase_and_owner_reset_fence_reopen_without_readmission(){
        let repo=fixture_repo();let store=SqliteStore::memory(4).unwrap();write(&repo.0,"main.py","def alpha(): pass\n");
        let first=store.code_index(&test_scope(),&repo.0).unwrap();store.code_remove(&test_scope(),&first.codebase).unwrap();
        assert!(store.code_index(&test_scope(),&repo.0).unwrap_err().contains("erased"));
        assert!(store.code_index_head(&test_scope(),&first.codebase).unwrap().is_none());
        let other=GraphScope::new("another-owner","test-workspace").unwrap();assert!(store.code_index(&other,&repo.0).is_ok());
    }

    #[test]
    fn root_replacement_before_publish_and_after_staging_never_changes_ready(){
        for phase in ["before_publish","before_commit"]{
            let repo=fixture_repo();let store=std::sync::Arc::new(SqliteStore::memory(4).unwrap());let scope=test_scope();write(&repo.0,"main.py","def prior_ready(): pass\n");let prior=store.code_index(&scope,&repo.0).unwrap();let before=logical_rows(&store,&scope,&prior.codebase);
            write(&repo.0,"main.py","def captured_changed_source(): pass\n");let gate=pause_next(&repo.0,phase);let root=repo.0.clone();let job_store=store.clone();let job=std::thread::spawn(move||job_store.code_index(&test_scope(),&root));gate.0.wait();
            let moved=repo.0.with_extension(format!("original-{phase}"));std::fs::rename(&repo.0,&moved).unwrap();std::fs::create_dir(&repo.0).unwrap();write(&repo.0,"replacement-private.py","def replacement_private_payload(): pass\n");gate.1.wait();
            assert!(job.join().unwrap().is_err(),"{phase} replacement must reject publication");assert_eq!(logical_rows(&store,&scope,&prior.codebase),before);assert_eq!(store.code_index_head(&scope,&prior.codebase).unwrap().unwrap().run_id,prior.run_id);
            let conn=store.conn.lock().unwrap();let coverage:String=conn.query_row("SELECT coverage FROM code_index_runs WHERE status='failed' ORDER BY rowid DESC LIMIT 1",[],|r|r.get(0)).unwrap();assert!(!coverage.contains("replacement-private"));drop(conn);
            std::fs::remove_dir_all(&repo.0).unwrap();std::fs::rename(moved,&repo.0).unwrap();
        }
    }

    #[test]
    fn expiry_during_publication_rolls_back_rows_and_preserves_last_ready(){
        let _lock=AUTHORITY_LOCK.lock().unwrap();
        let repo=fixture_repo();let root=repo.0.canonicalize().unwrap();write(&root,"main.py","def prior_ready(): pass\n");let store=std::sync::Arc::new(SqliteStore::memory(4).unwrap());let scope=test_scope();let prior=store.code_index(&scope,&root).unwrap();let logical=logical_rows(&store,&scope,&prior.codebase);
        let authority_repo=fixture_repo();let authority=authority_repo.0.join("authority.sqlite");let conn=rusqlite::Connection::open(&authority).unwrap();
        conn.execute_batch("CREATE TABLE file_policy_subject_aliases(username TEXT,subject_id TEXT,is_admin INTEGER);CREATE TABLE file_policy_workspaces(id TEXT,owner_subject_id TEXT,location_id TEXT,relative_folder TEXT,archived INTEGER,revision INTEGER);CREATE TABLE file_policy_locations(id TEXT,canonical_path TEXT,capabilities_json TEXT,enabled INTEGER,availability TEXT);CREATE TABLE file_policy_meta(singleton INTEGER,generation INTEGER);CREATE TABLE file_policy_bindings(binding_class TEXT,subject_kind TEXT,subject_id TEXT,location_id TEXT,status TEXT,expires_unix_ms INTEGER,workspace_id TEXT,chat_id TEXT,lifetime TEXT,remaining_uses INTEGER,capabilities_json TEXT);INSERT INTO file_policy_subject_aliases VALUES('test-owner','subject',0);INSERT INTO file_policy_workspaces VALUES('test-workspace','subject','location','',0,1);INSERT INTO file_policy_meta VALUES(1,1);").unwrap();
        conn.execute("INSERT INTO file_policy_locations VALUES('location',?1,'[\"read\"]',1,'available')",params![root.to_str().unwrap()]).unwrap();
        let expires=Utc::now().timestamp_millis()+6000;
        conn.execute("INSERT INTO file_policy_bindings VALUES('people','user','subject','location','active',?1,NULL,NULL,'always',NULL,'[\"read\"]')",params![expires]).unwrap();drop(conn);
        struct Restore(std::option::Option<std::ffi::OsString>);impl Drop for Restore{fn drop(&mut self){if let Some(value)=&self.0{std::env::set_var("OPEN_CLANK_AUTHORITY_DB_PATH",value)}else{std::env::remove_var("OPEN_CLANK_AUTHORITY_DB_PATH")}}}
        let _restore=Restore(std::env::var_os("OPEN_CLANK_AUTHORITY_DB_PATH"));std::env::set_var("OPEN_CLANK_AUTHORITY_DB_PATH",&authority);
        let binding=crate::code::WorkspaceBinding{owner:scope.owner.clone(),workspace_id:scope.workspace_id.clone(),owner_subject_id:"subject".into(),location_id:"location".into(),workspace_revision:1,policy_generation:1,root:root.clone(),origin:"app".into(),subscriber_id:"fixture".into(),authority_expires_unix_ms:None};
        let receipt=store.code_workspace_prepare(&scope,&binding,false).unwrap();let ticket=crate::code::CodeWorkTicket{checkout_id:prior.codebase.clone(),admission_epoch:receipt["admission_epoch"].as_i64().unwrap(),config_fingerprint:crate::code::producer_fingerprint(),policy_generation:1,workspace_revision:1};
        write(&root,"main.py","def must_rollback(): pass\n");let gate=pause_next(&root,"before_commit");let job_store=store.clone();let job=std::thread::spawn(move||job_store.code_index_bound(&test_scope(),&binding,&ticket));gate.0.wait();
        // Anchor to the actual canonical grant timestamp, not an assumed
        // parse duration. The worker is held after mutation, before ready.
        while Utc::now().timestamp_millis()<=expires{std::thread::sleep(std::time::Duration::from_millis(10));}
        gate.1.wait();assert!(job.join().unwrap().unwrap_err().contains("expired"));assert_eq!(logical_rows(&store,&scope,&prior.codebase),logical);assert_eq!(store.code_index_head(&scope,&prior.codebase).unwrap().unwrap().run_id,prior.run_id);
    }

    #[test]
    fn code_observations_survive_only_stable_logical_identities(){
        let repo=fixture_repo();let store=SqliteStore::memory(4).unwrap();let scope=test_scope();write(&repo.0,"main.py","import keep_module\ndef alpha():\n oldcall()\n");write(&repo.0,"other.py","def independent_beta(): pass\n");
        let first=store.code_index(&scope,&repo.0).unwrap();
        let alpha=declaration_node(&store,&scope,&first.codebase,"main.py","alpha");
        {let conn=store.conn.lock().unwrap();conn.execute("UPDATE graph_edges SET weight=7.5,traversal_count=19,created_at='observed-before'",[]).unwrap();conn.execute("UPDATE graph_nodes SET created_at='observed-node'",[]).unwrap();
        conn.execute("UPDATE graph_nodes SET ref_table='curated',ref_id='independent-ref',candidate_id='independent-candidate' WHERE id=?1",params![alpha]).unwrap();
        conn.execute("INSERT INTO graph_nodes(id,kind,name,norm_name,created_at,last_seen,owner,workspace_id) VALUES('external','entity','external','external','before','before',?1,?2)",params![scope.owner,scope.workspace_id]).unwrap();
        conn.execute("INSERT INTO graph_edges(id,src_id,tag,dst_id,created_at,last_seen,owner,workspace_id) VALUES('overlay','external','annotates',?1,'before','before',?2,?3)",params![alpha,scope.owner,scope.workspace_id]).unwrap();
        conn.execute("INSERT INTO graph_cues(cue,node_id,source,created_at,owner,workspace_id) VALUES('manual-cue',?1,'manual','before',?2,?3)",params![alpha,scope.owner,scope.workspace_id]).unwrap();}
        let keep=scope.node_id("module",&format!("{}::keep_module",first.codebase));let removed=scope.node_id("callable",&format!("{}::oldcall",first.codebase));
        let independent=declaration_node(&store,&scope,&first.codebase,"other.py","independent_beta");let main_file=scope.node_id("file",&format!("{}::main.py",first.codebase));
        {let conn=store.conn.lock().unwrap();conn.execute("UPDATE graph_cues SET source='manual',created_at='manual-before',candidate_id='manual-candidate',status='quarantined' WHERE node_id=?1 AND cue='alpha'",params![alpha]).unwrap();
        for (edge,dst) in [("malformed-ordinary-defines","external"),("malformed-other-file-defines",independent.as_str())]{conn.execute("INSERT INTO graph_edges(id,src_id,tag,dst_id,created_at,last_seen,owner,workspace_id) VALUES(?1,?2,'defines',?3,'before','before',?4,?5)",params![edge,main_file,dst,scope.owner,scope.workspace_id]).unwrap();}
        for (edge,node,cue) in [("keep-overlay",&keep,"keep-note"),("candidate-overlay",&removed,"candidate-note")]{conn.execute("INSERT INTO graph_edges(id,src_id,tag,dst_id,created_at,last_seen,owner,workspace_id) VALUES(?1,'external','annotates',?2,'before','before',?3,?4)",params![edge,node,scope.owner,scope.workspace_id]).unwrap();conn.execute("INSERT INTO graph_cues(cue,node_id,source,created_at,owner,workspace_id) VALUES(?1,?2,'manual','before',?3,?4)",params![cue,node,scope.owner,scope.workspace_id]).unwrap();}}
        write(&repo.0,"main.py","import keep_module\ndef alpha():\n newcall()\n");store.code_index(&scope,&repo.0).unwrap();
        let conn=store.conn.lock().unwrap();assert_eq!(conn.query_row("SELECT count(*) FROM graph_nodes WHERE id IN (?1,'external')",params![independent],|r|r.get::<_,i64>(0)).unwrap(),2,"external defines never own ordinary or another file's source identity");let (weight,count,created):(f64,i64,String)=conn.query_row("SELECT weight,traversal_count,created_at FROM graph_edges WHERE tag='defines'",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap();assert_eq!((weight,count,created),(7.5,19,"observed-before".into()));
        let (weight,count):(f64,i64)=conn.query_row("SELECT weight,traversal_count FROM graph_edges WHERE tag='calls'",[],|r|Ok((r.get(0)?,r.get(1)?))).unwrap();assert_eq!((weight,count),(1.0,0));
        assert_eq!(conn.query_row("SELECT created_at FROM graph_nodes WHERE id=?1",params![alpha],|r|r.get::<_,String>(0)).unwrap(),"observed-node");
        assert_eq!(conn.query_row("SELECT ref_id||':'||candidate_id FROM graph_nodes WHERE id=?1",params![alpha],|r|r.get::<_,String>(0)).unwrap(),"independent-ref:independent-candidate");
        assert_eq!(conn.query_row("SELECT source||':'||created_at||':'||candidate_id||':'||status FROM graph_cues WHERE node_id=?1 AND cue='alpha'",params![alpha],|r|r.get::<_,String>(0)).unwrap(),"manual:manual-before:manual-candidate:quarantined");
        assert_eq!(conn.query_row("SELECT count(*) FROM graph_nodes WHERE id=?1",params![removed],|r|r.get::<_,i64>(0)).unwrap(),0);assert_eq!(conn.query_row("SELECT count(*) FROM graph_edges WHERE id='candidate-overlay'",[],|r|r.get::<_,i64>(0)).unwrap(),0);assert_eq!(conn.query_row("SELECT count(*) FROM graph_cues WHERE cue='candidate-note'",[],|r|r.get::<_,i64>(0)).unwrap(),0);
        assert_eq!(conn.query_row("SELECT count(*) FROM graph_edges WHERE id='keep-overlay'",[],|r|r.get::<_,i64>(0)).unwrap(),1);assert_eq!(conn.query_row("SELECT count(*) FROM graph_cues WHERE cue='keep-note'",[],|r|r.get::<_,i64>(0)).unwrap(),1);
        assert_eq!(conn.query_row("SELECT count(*) FROM graph_edges WHERE id='overlay'",[],|r|r.get::<_,i64>(0)).unwrap(),1);assert_eq!(conn.query_row("SELECT count(*) FROM graph_cues WHERE cue='manual-cue'",[],|r|r.get::<_,i64>(0)).unwrap(),1);drop(conn);
        write(&repo.0,"main.py","def replacement(): pass\n");store.code_index(&scope,&repo.0).unwrap();let conn=store.conn.lock().unwrap();assert_eq!(conn.query_row("SELECT count(*) FROM graph_edges WHERE id='overlay'",[],|r|r.get::<_,i64>(0)).unwrap(),0);assert_eq!(conn.query_row("SELECT count(*) FROM graph_cues WHERE cue='manual-cue'",[],|r|r.get::<_,i64>(0)).unwrap(),0);
        assert_eq!(conn.query_row("SELECT count(*) FROM graph_nodes WHERE id=?1",params![independent],|r|r.get::<_,i64>(0)).unwrap(),1);drop(conn);
        let selector=crate::store::sqlite::ForgetSelector::RecordId("external".into());let preview=store.preview_forget(&scope.owner,&scope.workspace_id,selector.clone()).unwrap();store.commit_forget_with_operation(&scope.owner,&scope.workspace_id,selector,&preview.token,&"2".repeat(32)).unwrap();
    }

    #[test]
    fn owner_scope_keys_are_literal_and_rename_reindex_remove_stays_isolated(){
        let repo=fixture_repo();write(&repo.0,"main.py","def alpha(): pass\n");let store=SqliteStore::memory(4).unwrap();
        let a=GraphScope::new("a_b","ws").unwrap();let b=GraphScope::new("aXb","ws").unwrap();let unusual=GraphScope::new("a\u{1f}b%","ws").unwrap();
        let first=store.code_index(&a,&repo.0).unwrap();let other=store.code_index(&b,&repo.0).unwrap();let odd=store.code_index(&unusual,&repo.0).unwrap();let untouched=logical_rows(&store,&b,&other.codebase);
        assert!(store.export_scope("a_b","ws").unwrap()["code_index"]["files"].as_array().unwrap().iter().all(|v|v["checkout_id"]==first.codebase));
        store.rename_owner("a_b","renamed%\u{1f}owner").unwrap();let renamed=GraphScope::new("renamed%\u{1f}owner","ws").unwrap();let next=store.code_index(&renamed,&repo.0).unwrap();assert_eq!(next.files_unchanged,1);assert_eq!(next.codebase,first.codebase);
        write(&repo.0,"main.py","def alpha():\n newcall()\n");store.code_index(&renamed,&repo.0).unwrap();
        {let conn=store.conn.lock().unwrap();assert!(SqliteStore::code_owner_keys_inner(&conn,"a_b").unwrap().is_empty());for key in SqliteStore::code_owner_keys_inner(&conn,&renamed.owner).unwrap(){let tuple:[String;3]=serde_json::from_str(key.strip_prefix("code_v1:").unwrap()).unwrap();assert_eq!(tuple[0],renamed.owner);}
        let wrong:i64=conn.query_row("SELECT count(*) FROM graph_edges e JOIN graph_nodes a ON a.id=e.src_id JOIN graph_nodes b ON b.id=e.dst_id WHERE e.owner!=a.owner OR e.owner!=b.owner",[],|r|r.get(0)).unwrap();assert_eq!(wrong,0);}
        store.code_remove(&renamed,&first.codebase).unwrap();assert!(logical_rows(&store,&renamed,&first.codebase).is_empty());assert_eq!(logical_rows(&store,&b,&other.codebase),untouched);assert!(store.code_index_head(&unusual,&odd.codebase).unwrap().is_some());
        store.purge_owner(&unusual.owner).unwrap();assert_eq!(logical_rows(&store,&b,&other.codebase),untouched);
    }

    #[test]
    fn global_reset_invalidates_readmission_on_every_cycle(){
        let repo=fixture_repo();write(&repo.0,"main.py","def alpha(): pass\n");let store=std::sync::Arc::new(SqliteStore::memory(4).unwrap());let a=GraphScope::new("reset-owner","a").unwrap();let b=GraphScope::new("reset-owner","b").unwrap();store.code_index(&a,&repo.0).unwrap();store.code_index(&b,&repo.0).unwrap();
        let reset=|s:&SqliteStore|{let categories=vec!["graph".into()];let preview=s.reset_owner("reset_preview","reset-owner",&categories,None).unwrap();s.reset_owner("reset_commit","reset-owner",&categories,Some(&preview["components"])).unwrap();};
        reset(&store);assert!(store.code_index(&a,&repo.0).unwrap_err().contains("erased"));
        // The explicit-admit override is the only operation that can lift A.
        {let conn=store.conn.lock().unwrap();conn.execute("INSERT INTO code_scope_epochs(owner,workspace_id,admission_epoch,erased) VALUES('reset-owner','a',2,0)",[]).unwrap();}
        store.code_index(&a,&repo.0).unwrap();assert!(store.code_index(&b,&repo.0).unwrap_err().contains("erased"));
        write(&repo.0,"main.py","def late_reset_writer(): pass\n");let gate=pause_next(&repo.0,"before_publish");let job_store=store.clone();let root=repo.0.clone();let job_scope=a.clone();let job=std::thread::spawn(move||job_store.code_index(&job_scope,&root));gate.0.wait();reset(&store);gate.1.wait();assert!(job.join().unwrap().is_err());
        for scope in [&a,&b]{assert!(store.code_index(scope,&repo.0).unwrap_err().contains("erased"));}
    }

    #[test]
    fn synthetic_index_baseline_small_medium_large(){
        for count in [10,100,1000]{let repo=fixture_repo();for i in 0..count{write(&repo.0,&format!("f{i}.py"),&format!("def symbol_{i}():\n return {i}\n"));}let store=SqliteStore::memory(4).unwrap();let start=std::time::Instant::now();let result=store.code_index(&test_scope(),&repo.0).unwrap();assert_eq!(result.files_indexed,count);eprintln!("S03 baseline files={count} elapsed_ms={:.3}",start.elapsed().as_secs_f64()*1000.0);}
    }

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
        assert!(result.repository_id.starts_with("registration_"));
        assert_eq!(result.symbols, 2, "core_helper + main");
        assert!(result.errors.is_empty(), "{:?}", result.errors);

        let codebase = root.to_string_lossy().to_string();
        let (files, symbols, last) = s.code_status(&test_scope(), &codebase).unwrap();
        assert_eq!(files, 2);
        assert_eq!(symbols, 2);
        assert!(last.is_some());

        // cues from identifiers findable through the normal graph surface
        let hits = s.graph_cues(&test_scope(), "core helper", 10).unwrap();
        let helper=declaration_node(&s,&test_scope(),&result.codebase,"src/lib.py","core_helper");
        assert!(hits.iter().any(|h| h.node.id==helper && h.node.kind=="code_symbol" && h.node.label.as_deref()==Some("core_helper")));

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
            evidence_store_version:0,
            evidence_cache_identity: String::new(),
            selection_policy_digest: String::new(),
            repository_id: "repo_test".into(),
            git_commit: None,
            git_ref: None,
            git_dirty: false,
            git_dirty_known:false,
            git_metadata_state:"fixture".into(),
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
