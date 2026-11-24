use rusqlite::{Connection, params};
use tracing::warn;
use crate::ontology;
const SCHEMA_VERSION: i64 = 16;

pub fn mapping_is_replay(conn:&Connection,owner:&str,workspace:&str,legacy_namespace:&str,checkout:&str)->Result<bool,rusqlite::Error>{
    if [owner,workspace,legacy_namespace,checkout].iter().any(|value|value.is_empty()||value.contains('\u{1f}')||value.contains('\0')){return Err(rusqlite::Error::InvalidQuery);}
        let key=format!("{owner}\u{1f}{workspace}\u{1f}{legacy_namespace}");
        let registration:String=conn.query_row("SELECT registration_id FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3 AND owner_subject_id!='' AND lifecycle='admitted'",params![owner,workspace,checkout],|r|r.get(0))?;
        let prior:(String,String,Option<String>,Option<String>,String)=conn.query_row("SELECT owner,workspace_id,checkout_id,registration_id,status FROM code_legacy_mappings WHERE legacy_key=?1",params![key],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?;
        if prior.0!=owner || prior.1!=workspace || prior.2.as_deref().is_some_and(|previous|previous!=checkout){return Err(rusqlite::Error::InvalidQuery);}
        let collision:i64=conn.query_row("SELECT count(*) FROM code_legacy_mappings WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3 AND legacy_key!=?4",params![owner,workspace,checkout,key],|r|r.get(0))?;
        if collision!=0{return Err(rusqlite::Error::InvalidQuery);}
        if prior.2.is_some() && (prior.3.as_deref()!=Some(registration.as_str())||prior.4!="mapped_pending_reindex"){return Err(rusqlite::Error::InvalidQuery);}
        Ok(prior.2.as_deref()==Some(checkout))
}

pub fn map_legacy(conn:&Connection,owner:&str,workspace:&str,legacy_namespace:&str,checkout:&str)->Result<(),rusqlite::Error>{
    conn.execute_batch("BEGIN IMMEDIATE")?;
    let mapped=(||{
        if mapping_is_replay(conn,owner,workspace,legacy_namespace,checkout)? {return Ok(());}
        let key=format!("{owner}\u{1f}{workspace}\u{1f}{legacy_namespace}");
        let registration:String=conn.query_row("SELECT registration_id FROM code_registrations WHERE owner=?1 AND workspace_id=?2 AND checkout_id=?3 AND owner_subject_id!='' AND lifecycle='admitted'",params![owner,workspace,checkout],|r|r.get(0))?;
        conn.execute("UPDATE code_legacy_mappings SET registration_id=?1,checkout_id=?2,status='mapped_pending_reindex',reason='explicit authority mapping; legacy rows retained; no histories merged' WHERE legacy_key=?3",params![registration,checkout,key])?;
        Ok(())
    })();
    match mapped{Ok(())=>conn.execute_batch("COMMIT"),Err(error)=>{let _=conn.execute_batch("ROLLBACK");Err(error)}}
}
    pub fn upgrade(conn: &Connection) -> Result<(), rusqlite::Error> {
        conn.execute_batch("BEGIN EXCLUSIVE;")?;
        let migration = (|| -> Result<(), rusqlite::Error> {
            let version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;

            if version > SCHEMA_VERSION {
                return Err(rusqlite::Error::InvalidQuery);
            }

            if version < 1 {
                baseline_schema(&conn)?;
                conn.pragma_update(None, "user_version", 1)?;
            }
            if version < 2 {
                graph_schema(&conn)?;
                conn.pragma_update(None, "user_version", 2)?;
            }
            if version < 3 {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS code_files (
                    codebase TEXT NOT NULL,
                    rel_path TEXT NOT NULL,
                    blake3 TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    symbol_count INTEGER NOT NULL DEFAULT 0,
                    indexed_at TEXT NOT NULL,
                    PRIMARY KEY (codebase, rel_path)
                );",
                )?;
                conn.pragma_update(None, "user_version", 3)?;
            }
            if version < 4 {
                let has_owner = conn
                    .prepare("PRAGMA table_info(raw)")?
                    .query_map([], |row| row.get::<_, String>(1))?
                    .filter_map(|name| name.ok())
                    .any(|name| name == "owner");
                if !has_owner {
                    conn.execute_batch("ALTER TABLE raw ADD COLUMN owner TEXT;")?;
                }
                conn.pragma_update(None, "user_version", 4)?;
            }
            if version < 5 {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    confidence_score REAL NOT NULL,
                    importance_score REAL NOT NULL,
                    owner TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    workspace_path TEXT,
                    session_id TEXT NOT NULL DEFAULT '',
                    turn_id TEXT NOT NULL,
                    raw_evidence_ids TEXT NOT NULL DEFAULT '[]',
                    evidence_role TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    dedup_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    accepted_curated_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_scope_status
                    ON candidates(owner, workspace_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS memory_quarantine (
                    id TEXT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    original_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    owner TEXT,
                    workspace_id TEXT NOT NULL DEFAULT 'global',
                    reason TEXT NOT NULL,
                    quarantined_at TEXT NOT NULL,
                    UNIQUE(tier, original_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_quarantine_scope
                    ON memory_quarantine(owner, workspace_id, quarantined_at);
                CREATE TABLE IF NOT EXISTS memory_metrics (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );",
                )?;
                for (table, column, definition) in [
                    ("graph_nodes", "owner", "TEXT"),
                    (
                        "graph_nodes",
                        "workspace_id",
                        "TEXT NOT NULL DEFAULT 'global'",
                    ),
                    ("graph_nodes", "candidate_id", "TEXT"),
                    ("graph_nodes", "status", "TEXT NOT NULL DEFAULT 'active'"),
                    ("graph_edges", "owner", "TEXT"),
                    (
                        "graph_edges",
                        "workspace_id",
                        "TEXT NOT NULL DEFAULT 'global'",
                    ),
                    ("graph_edges", "candidate_id", "TEXT"),
                    ("graph_edges", "status", "TEXT NOT NULL DEFAULT 'active'"),
                    ("graph_cues", "owner", "TEXT"),
                    (
                        "graph_cues",
                        "workspace_id",
                        "TEXT NOT NULL DEFAULT 'global'",
                    ),
                    ("graph_cues", "candidate_id", "TEXT"),
                    ("graph_cues", "status", "TEXT NOT NULL DEFAULT 'active'"),
                    ("facts", "owner", "TEXT"),
                    ("facts", "workspace_id", "TEXT NOT NULL DEFAULT 'global'"),
                    ("facts", "candidate_id", "TEXT"),
                    ("facts", "status", "TEXT NOT NULL DEFAULT 'active'"),
                ] {
                    if !has_column(&conn, table, column)? {
                        conn.execute_batch(&format!(
                            "ALTER TABLE {table} ADD COLUMN {column} {definition};"
                        ))?;
                    }
                }
                conn.pragma_update(None, "user_version", 5)?;
            }
            if version < 6 {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS fm_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                 );
                 INSERT OR IGNORE INTO fm_meta(key,value)
                    VALUES ('database_id', lower(hex(randomblob(16))));
                 CREATE INDEX IF NOT EXISTS idx_graph_nodes_scope
                    ON graph_nodes(owner,workspace_id,status);
                 CREATE INDEX IF NOT EXISTS idx_graph_edges_scope
                    ON graph_edges(owner,workspace_id,status);
                 CREATE INDEX IF NOT EXISTS idx_graph_cues_scope
                    ON graph_cues(owner,workspace_id,status);
                 CREATE INDEX IF NOT EXISTS idx_facts_scope
                    ON facts(owner,workspace_id,status);
                 UPDATE graph_nodes SET status='quarantined'
                    WHERE owner IS NULL OR trim(owner)='';
                 UPDATE graph_edges SET status='quarantined'
                    WHERE owner IS NULL OR trim(owner)='';
                 UPDATE graph_cues SET status='quarantined'
                    WHERE owner IS NULL OR trim(owner)='';
                 UPDATE facts SET status='quarantined'
                    WHERE owner IS NULL OR trim(owner)='';",
                )?;
                conn.pragma_update(None, "user_version", 6)?;
            }
            if version < 7 {
                for (column, definition) in [
                    ("source_uri", "TEXT NOT NULL DEFAULT ''"),
                    ("source_revision", "INTEGER NOT NULL DEFAULT 1"),
                    ("content_hash", "TEXT NOT NULL DEFAULT ''"),
                    ("source_message_ids", "TEXT NOT NULL DEFAULT '[]'"),
                ] {
                    if !has_column(&conn, "candidates", column)? {
                        conn.execute_batch(&format!(
                            "ALTER TABLE candidates ADD COLUMN {column} {definition};"
                        ))?;
                    }
                }
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS memory_leases (
                        kind TEXT NOT NULL,
                        owner TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        holder TEXT NOT NULL,
                        acquired_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        attempt INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY(kind, owner, workspace_id, subject)
                    );
                    CREATE INDEX IF NOT EXISTS idx_memory_leases_expiry
                        ON memory_leases(expires_at);
                    CREATE TABLE IF NOT EXISTS memory_tombstones (
                        id TEXT PRIMARY KEY,
                        owner TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        source_key TEXT NOT NULL,
                        affected_ids TEXT NOT NULL,
                        recovery_payload TEXT,
                        created_at TEXT NOT NULL,
                        recover_until TEXT,
                        recovered_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_memory_tombstones_scope
                        ON memory_tombstones(owner, workspace_id, created_at);",
                )?;
                conn.pragma_update(None, "user_version", 7)?;
            }
            if version < 8 {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS memory_retention_policy (
                        owner TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        raw_days INTEGER NOT NULL,
                        candidate_days INTEGER NOT NULL,
                        curated_days INTEGER,
                        graph_days INTEGER,
                        recovery_seconds INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(owner, workspace_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_memory_tombstones_expiry
                        ON memory_tombstones(recover_until, recovered_at);",
                )?;
                conn.pragma_update(None, "user_version", 8)?;
            }
            if version < 9 {
                if !has_column(&conn, "memory_tombstones", "operation_id")? {
                    conn.execute_batch(
                        "ALTER TABLE memory_tombstones ADD COLUMN operation_id TEXT;",
                    )?;
                }
                if !has_column(&conn, "memory_tombstones", "operation_key")? {
                    conn.execute_batch(
                        "ALTER TABLE memory_tombstones ADD COLUMN operation_key TEXT;",
                    )?;
                }
                conn.execute_batch(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_tombstones_operation
                        ON memory_tombstones(owner, workspace_id, operation_id)
                        WHERE operation_id IS NOT NULL;",
                )?;
                conn.pragma_update(None, "user_version", 9)?;
            }
            if version < 10 {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS memory_retention_operations (
                        owner TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        request_key TEXT NOT NULL,
                        closure TEXT NOT NULL,
                        committed_at TEXT NOT NULL,
                        PRIMARY KEY(owner, workspace_id, operation_id)
                    );",
                )?;
                conn.pragma_update(None, "user_version", 10)?;
            }
            if version < 11 {
                ontology::install_schema(&conn)?;
                conn.pragma_update(None, "user_version", 11)?;
            }
            if version < 12 {
                ontology::ensure_schema_extensions(&conn)?;
                conn.pragma_update(None, "user_version", 12)?;
            }
            if version < 13 {
                ontology::ensure_schema_extensions(&conn)?;
                conn.pragma_update(None, "user_version", 13)?;
            }
            if version < 14 {
                conn.execute_batch(
                    "CREATE TABLE IF NOT EXISTS code_index_runs (
                        owner TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        codebase TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        source_snapshot TEXT NOT NULL,
                        status TEXT NOT NULL,
                        files_indexed INTEGER NOT NULL DEFAULT 0,
                        files_unchanged INTEGER NOT NULL DEFAULT 0,
                        files_removed INTEGER NOT NULL DEFAULT 0,
                        symbols INTEGER NOT NULL DEFAULT 0,
                        errors TEXT NOT NULL DEFAULT '[]',
                        coverage TEXT NOT NULL DEFAULT '[]',
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        PRIMARY KEY (owner, workspace_id, codebase, run_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_code_index_runs_status
                        ON code_index_runs(owner, workspace_id, codebase, status);
                    CREATE TABLE IF NOT EXISTS code_index_heads (
                        owner TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        codebase TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        published_at TEXT NOT NULL,
                        PRIMARY KEY (owner, workspace_id, codebase)
                    );",
                )?;
                conn.pragma_update(None, "user_version", 14)?;
            }
            if version < 15 {
                conn.execute_batch("ALTER TABLE code_files ADD COLUMN config_fingerprint TEXT NOT NULL DEFAULT '';
                    ALTER TABLE code_files ADD COLUMN source_generation TEXT NOT NULL DEFAULT '';
                    ALTER TABLE code_index_runs ADD COLUMN heartbeat_at TEXT;
                    ALTER TABLE code_index_runs ADD COLUMN completed_files INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE code_index_runs ADD COLUMN total_files INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE code_index_runs ADD COLUMN admission_epoch INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE code_index_runs ADD COLUMN config_fingerprint TEXT NOT NULL DEFAULT '';
                    CREATE TABLE code_registrations(owner TEXT NOT NULL,workspace_id TEXT NOT NULL,registration_id TEXT NOT NULL,checkout_id TEXT NOT NULL,root_binding TEXT NOT NULL DEFAULT '',directory_identity TEXT NOT NULL DEFAULT '',owner_subject_id TEXT NOT NULL DEFAULT '',location_id TEXT NOT NULL DEFAULT '',workspace_revision TEXT NOT NULL DEFAULT '',policy_generation INTEGER NOT NULL DEFAULT 0,admission_epoch INTEGER NOT NULL DEFAULT 0,lifecycle TEXT NOT NULL DEFAULT 'admitted',config_fingerprint TEXT NOT NULL DEFAULT '',lineage TEXT,PRIMARY KEY(owner,workspace_id,checkout_id),UNIQUE(owner,workspace_id,root_binding));
                    CREATE TABLE code_scope_epochs(owner TEXT NOT NULL,workspace_id TEXT NOT NULL,admission_epoch INTEGER NOT NULL DEFAULT 0,erased INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(owner,workspace_id));
                    CREATE TABLE code_legacy_mappings(legacy_key TEXT PRIMARY KEY,owner TEXT NOT NULL,workspace_id TEXT NOT NULL,registration_id TEXT,checkout_id TEXT,status TEXT NOT NULL DEFAULT 'unmapped',reason TEXT NOT NULL DEFAULT 'explicit mapping required');
                    INSERT INTO code_legacy_mappings(legacy_key,owner,workspace_id,status,reason) SELECT DISTINCT codebase,substr(codebase,1,instr(codebase,char(31))-1),substr(substr(codebase,instr(codebase,char(31))+1),1,instr(substr(codebase,instr(codebase,char(31))+1),char(31))-1),'unmapped','explicit authority mapping required; legacy evidence retained without merging' FROM code_files;
                    UPDATE code_legacy_mappings SET owner='',workspace_id='',status='unavailable',reason='namespace lacks unambiguous owner/workspace; explicit repair required' WHERE owner='' OR workspace_id='' OR length(legacy_key)-length(replace(legacy_key,char(31),''))!=2 OR substr(legacy_key,-1)=char(31) OR instr(legacy_key,char(0))>0;")?;
                conn.pragma_update(None,"user_version",15)?;
            }
            if version < 16 {
                conn.execute_batch(memery_core::store::code_evidence::SCHEMA_SQL)?;
                conn.pragma_update(None, "user_version", 16)?;
            }
            conn.execute(
                "UPDATE memory_tombstones SET recovery_payload=NULL
                 WHERE recovered_at IS NULL AND recover_until IS NOT NULL
                   AND recover_until<?1",
                params![chrono::Utc::now().to_rfc3339()],
            )?;
            // Keep additive v2 scope columns present for databases that were
            // already stamped at version 11 before those columns existed.
            ontology::ensure_schema_extensions(&conn)?;
            Ok(())
        })();
        if let Err(error) = migration {
            let _ = conn.execute_batch("ROLLBACK;");
            return Err(error);
        }
        conn.execute_batch("COMMIT;")?;

        // FTS virtual tables stay OUTSIDE the version gate: creation is
        // tolerant (environments without FTS5 only warn), so it must retry
        // on every open rather than being skipped forever after one stamp.
        fts_schema(&conn);
        graph_fts_sync(&conn);
        

        Ok(())
    }

    fn has_column(conn: &Connection, table: &str, column: &str) -> Result<bool, rusqlite::Error> {
        let mut statement = conn.prepare(&format!("PRAGMA table_info({table})"))?;
        let found = statement
            .query_map([], |row| row.get::<_, String>(1))?
            .filter_map(Result::ok)
            .any(|name| name == column);
        Ok(found)
    }

    fn baseline_schema(conn: &Connection) -> Result<(), rusqlite::Error> {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS curated (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'episodic',
                priority INTEGER NOT NULL DEFAULT 50,
                trust_score REAL NOT NULL DEFAULT 0.50,
                confidence_score REAL NOT NULL DEFAULT 0.6,
                importance_score REAL NOT NULL DEFAULT 0.5,
                scene_name TEXT,
                source TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'auto_extracted',
                owner TEXT,
                workspace_id TEXT NOT NULL DEFAULT 'global',
                session_key TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                source_message_ids TEXT NOT NULL DEFAULT '[]',
                timestamps TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                last_accessed_at TEXT,
                exempt_from_decay INTEGER NOT NULL DEFAULT 0,
                exempt_from_dedup INTEGER NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL DEFAULT 'null',
                workspace_path TEXT,
                embedding BLOB
            );
            CREATE TABLE IF NOT EXISTS raw (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                session_key TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                workspace_id TEXT NOT NULL DEFAULT 'global',
                owner TEXT,
                recorded_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT 'null',
                workspace_path TEXT,
                embedding BLOB
            );
            CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                entities TEXT NOT NULL DEFAULT '[]',
                trust_score REAL NOT NULL DEFAULT 0.50,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                embedding BLOB
            );",
        )?;
        Ok(())
    }

    fn graph_schema(conn: &Connection) -> Result<(), rusqlite::Error> {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS graph_nodes (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                label TEXT,
                name TEXT NOT NULL,
                norm_name TEXT NOT NULL,
                layer TEXT NOT NULL DEFAULT 'semantic',
                ref_table TEXT,
                ref_id TEXT,
                trust INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_graph_nodes_norm ON graph_nodes(norm_name);
            CREATE TABLE IF NOT EXISTS graph_cues (
                cue TEXT NOT NULL,
                node_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'extracted',
                created_at TEXT NOT NULL,
                PRIMARY KEY (cue, node_id)
            );
            CREATE TABLE IF NOT EXISTS graph_edges (
                id TEXT PRIMARY KEY,
                src_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                dst_id TEXT NOT NULL,
                fact_id TEXT,
                weight REAL NOT NULL DEFAULT 1.0,
                traversal_count INTEGER NOT NULL DEFAULT 0,
                trust INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                UNIQUE (src_id, tag, dst_id)
            );
            CREATE INDEX IF NOT EXISTS idx_graph_edges_src ON graph_edges(src_id);
            CREATE INDEX IF NOT EXISTS idx_graph_edges_dst ON graph_edges(dst_id);",
        )?;
        // Cue FTS mirrors the tier shadows: tolerant creation, manual sync.
        if let Err(e) = conn.execute_batch(
            "CREATE VIRTUAL TABLE IF NOT EXISTS graph_cues_fts USING fts5(
                cue, node_id UNINDEXED
            );",
        ) {
            warn!("FTS5 not available for graph_cues: {e}");
        }
        Ok(())
    }

    fn fts_schema(conn: &Connection) {
        let fts_curated = conn.execute_batch(
            "CREATE VIRTUAL TABLE IF NOT EXISTS curated_fts USING fts5(
                content, scene_name, tags, workspace_id,
                content='curated', content_rowid='rowid'
            );",
        );
        if let Err(e) = fts_curated {
            warn!("FTS5 not available for curated: {e}");
        }

        let fts_raw = conn.execute_batch(
            "CREATE VIRTUAL TABLE IF NOT EXISTS raw_fts USING fts5(
                content, workspace_id,
                content='raw', content_rowid='rowid'
            );",
        );
        if let Err(e) = fts_raw {
            warn!("FTS5 not available for raw: {e}");
        }

        let fts_facts = conn.execute_batch(
            "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                content, entities,
                content='facts', content_rowid='rowid'
            );",
        );
        if let Err(e) = fts_facts {
            warn!("FTS5 not available for facts: {e}");
        }
    }


    pub fn graph_fts_sync(conn: &Connection) {
        let result = conn.execute_batch(
            "CREATE TRIGGER IF NOT EXISTS graph_cues_fts_insert
                 AFTER INSERT ON graph_cues WHEN new.status = 'active' BEGIN
                   INSERT INTO graph_cues_fts(cue, node_id) VALUES (new.cue, new.node_id);
                 END;
             CREATE TRIGGER IF NOT EXISTS graph_cues_fts_delete
                 AFTER DELETE ON graph_cues BEGIN
                   DELETE FROM graph_cues_fts WHERE cue = old.cue AND node_id = old.node_id;
                 END;
             CREATE TRIGGER IF NOT EXISTS graph_cues_fts_status
                 AFTER UPDATE OF status ON graph_cues BEGIN
                   DELETE FROM graph_cues_fts WHERE cue = old.cue AND node_id = old.node_id;
                   INSERT INTO graph_cues_fts(cue, node_id)
                     SELECT new.cue, new.node_id WHERE new.status = 'active';
                 END;
             DELETE FROM graph_cues_fts;
             INSERT INTO graph_cues_fts(cue, node_id)
                 SELECT c.cue, c.node_id FROM graph_cues c
                 JOIN graph_nodes n ON n.id = c.node_id
                 WHERE c.status = 'active' AND n.status = 'active';",
        );
        if let Err(error) = result {
            warn!("graph cue FTS synchronization unavailable: {error}");
        }
    }

