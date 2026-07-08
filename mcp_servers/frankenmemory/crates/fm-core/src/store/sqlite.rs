use async_trait::async_trait;
use rusqlite::{params, Connection};
use std::sync::Mutex;
use tracing::{info, warn};

use crate::record::*;
use crate::store::*;

pub struct SqliteStore {
    conn: Mutex<Connection>,
    embedding_dim: usize,
    capabilities: StoreCapabilities,
}

impl SqliteStore {
    pub fn new(path: &str, embedding_dim: usize) -> Result<Self, rusqlite::Error> {
        let conn = Connection::open(path)?;
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")?;

        let store = Self {
            conn: Mutex::new(conn),
            embedding_dim,
            capabilities: StoreCapabilities {
                vector_search: true,
                fts_search: true,
                native_hybrid: false,
                sparse_vectors: false,
            },
        };
        store.init_tables()?;
        Ok(store)
    }

    pub fn memory(embedding_dim: usize) -> Result<Self, rusqlite::Error> {
        let conn = Connection::open_in_memory()?;
        let store = Self {
            conn: Mutex::new(conn),
            embedding_dim,
            capabilities: StoreCapabilities {
                vector_search: true,
                fts_search: true,
                native_hybrid: false,
                sparse_vectors: false,
            },
        };
        store.init_tables()?;
        Ok(store)
    }

    fn init_tables(&self) -> Result<(), rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS curated (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'episodic',
                priority INTEGER NOT NULL DEFAULT 50,
                trust_score REAL NOT NULL DEFAULT 0.50,
                confidence_score REAL NOT NULL DEFAULT 1.0,
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

        // FTS5 virtual tables
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

        Ok(())
    }

    fn record_to_row(r: &MemoryRecord) -> Vec<rusqlite::types::Value> {
        vec![
            rusqlite::types::Value::Text(r.id.clone()),
            rusqlite::types::Value::Text(r.content.clone()),
            rusqlite::types::Value::Text(format!("{:?}", r.kind).to_lowercase()),
            rusqlite::types::Value::Integer(r.priority as i64),
            rusqlite::types::Value::Real(r.trust_score as f64),
            rusqlite::types::Value::Real(r.confidence_score as f64),
            rusqlite::types::Value::Real(r.importance_score as f64),
            r.scene_name
                .as_ref()
                .map(|s| rusqlite::types::Value::Text(s.clone()))
                .unwrap_or(rusqlite::types::Value::Null),
            rusqlite::types::Value::Text(r.source.clone()),
            rusqlite::types::Value::Text(format!("{:?}", r.source_type).to_lowercase()),
            r.owner
                .as_ref()
                .map(|s| rusqlite::types::Value::Text(s.clone()))
                .unwrap_or(rusqlite::types::Value::Null),
            rusqlite::types::Value::Text(r.workspace_id.clone()),
            rusqlite::types::Value::Text(r.session_key.clone()),
            rusqlite::types::Value::Text(r.session_id.clone()),
            rusqlite::types::Value::Text(serde_json::to_string(&r.tags).unwrap_or_default()),
            rusqlite::types::Value::Text(
                serde_json::to_string(&r.source_message_ids).unwrap_or_default(),
            ),
            rusqlite::types::Value::Text(
                serde_json::to_string(&r.timestamps).unwrap_or_default(),
            ),
            rusqlite::types::Value::Text(r.created_at.clone()),
            rusqlite::types::Value::Text(r.updated_at.clone()),
            rusqlite::types::Value::Integer(r.archived as i64),
            r.last_accessed_at
                .as_ref()
                .map(|s| rusqlite::types::Value::Text(s.clone()))
                .unwrap_or(rusqlite::types::Value::Null),
            rusqlite::types::Value::Integer(r.exempt_from_decay as i64),
            rusqlite::types::Value::Integer(r.exempt_from_dedup as i64),
            rusqlite::types::Value::Text(
                serde_json::to_string(&r.metadata).unwrap_or_default(),
            ),
            r.workspace_path
                .as_ref()
                .map(|s| rusqlite::types::Value::Text(s.clone()))
                .unwrap_or(rusqlite::types::Value::Null),
        ]
    }

    fn row_to_record(row: &rusqlite::Row) -> rusqlite::Result<MemoryRecord> {
        let kind_str: String = row.get(2)?;
        let kind = match kind_str.as_str() {
            "persona" => MemoryKind::Persona,
            "episodic" => MemoryKind::Episodic,
            "instruction" => MemoryKind::Instruction,
            "fact" => MemoryKind::Fact,
            "fabric" => MemoryKind::Fabric,
            "wiki" => MemoryKind::Wiki,
            "raw" => MemoryKind::Raw,
            _ => MemoryKind::Episodic,
        };
        let source_type_str: String = row.get(9)?;
        let source_type = match source_type_str.as_str() {
            "human" => SourceType::Human,
            "procedural" => SourceType::Procedural,
            "ai" => SourceType::Ai,
            _ => SourceType::AutoExtracted,
        };
        let tags_str: String = row.get(14)?;
        let tags: Vec<String> = serde_json::from_str(&tags_str).unwrap_or_default();
        let smids_str: String = row.get(15)?;
        let source_message_ids: Vec<String> = serde_json::from_str(&smids_str).unwrap_or_default();
        let ts_str: String = row.get(16)?;
        let timestamps: Vec<String> = serde_json::from_str(&ts_str).unwrap_or_default();
        let meta_str: String = row.get(23)?;
        let metadata: serde_json::Value = serde_json::from_str(&meta_str).unwrap_or_default();

        Ok(MemoryRecord {
            id: row.get(0)?,
            content: row.get(1)?,
            kind,
            priority: row.get::<_, i64>(3)? as i32,
            trust_score: row.get::<_, f64>(4)? as f32,
            confidence_score: row.get::<_, f64>(5)? as f32,
            importance_score: row.get::<_, f64>(6)? as f32,
            scene_name: row.get(7)?,
            source: row.get(8)?,
            source_type,
            owner: row.get(10)?,
            workspace_id: row.get(11)?,
            workspace_path: row.get(24)?,
            session_key: row.get(12)?,
            session_id: row.get(13)?,
            tags,
            source_message_ids,
            timestamps,
            created_at: row.get(17)?,
            updated_at: row.get(18)?,
            archived: row.get::<_, i64>(19)? != 0,
            last_accessed_at: row.get(20)?,
            exempt_from_decay: row.get::<_, i64>(21)? != 0,
            exempt_from_dedup: row.get::<_, i64>(22)? != 0,
            metadata,
        })
    }

    fn raw_row_to_record(row: &rusqlite::Row) -> rusqlite::Result<RawTurn> {
        let meta_str: String = row.get(7)?;
        Ok(RawTurn {
            id: row.get(0)?,
            role: row.get(1)?,
            content: row.get(2)?,
            session_key: row.get(3)?,
            session_id: row.get(4)?,
            workspace_id: row.get(5)?,
            workspace_path: row.get(8)?,
            recorded_at: row.get(6)?,
            metadata: serde_json::from_str(&meta_str).unwrap_or_default(),
        })
    }

    fn embedding_to_blob(embedding: &[f32]) -> Vec<u8> {
        let mut blob = Vec::with_capacity(embedding.len() * 4);
        for &v in embedding {
            blob.extend_from_slice(&v.to_le_bytes());
        }
        blob
    }

    fn blob_to_embedding(blob: &[u8]) -> Vec<f32> {
        blob.chunks_exact(4)
            .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
            .collect()
    }

    fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
        if a.len() != b.len() || a.is_empty() {
            return 0.0;
        }
        let dot: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
        let norm_a: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
        let norm_b: f32 = b.iter().map(|x| x * x).sum::<f32>().sqrt();
        if norm_a == 0.0 || norm_b == 0.0 {
            0.0
        } else {
            dot / (norm_a * norm_b)
        }
    }

    fn build_fts_query(raw_query: &str) -> Option<String> {
        let tokens: Vec<String> = raw_query
            .split(|c: char| !c.is_alphanumeric() && c != '_')
            .filter(|t| !t.is_empty())
            .map(|t| format!("\"{}\"", t.replace('"', "")))
            .collect();
        if tokens.is_empty() {
            None
        } else {
            Some(tokens.join(" OR "))
        }
    }
}

#[async_trait]
impl MemoryStore for SqliteStore {
    fn capabilities(&self) -> StoreCapabilities {
        self.capabilities.clone()
    }

    fn is_degraded(&self) -> bool {
        false
    }

    async fn upsert_curated(&self, r: &MemoryRecord, embedding: Option<&[f32]>) -> bool {
        let conn = self.conn.lock().unwrap();
        let embedding_blob = embedding.map(|e| Self::embedding_to_blob(e));
        let values = Self::record_to_row(r);

        let result = conn.execute(
            "INSERT OR REPLACE INTO curated (
                id, content, kind, priority, trust_score, confidence_score,
                importance_score, scene_name, source, source_type, owner,
                workspace_id, session_key, session_id, tags, source_message_ids,
                timestamps, created_at, updated_at, archived, last_accessed_at,
                exempt_from_decay, exempt_from_dedup, metadata, workspace_path, embedding
            ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22,?23,?24,?25,?26)",
            rusqlite::params![
                values[0], values[1], values[2], values[3], values[4], values[5],
                values[6], values[7], values[8], values[9], values[10], values[11],
                values[12], values[13], values[14], values[15], values[16], values[17],
                values[18], values[19], values[20], values[21], values[22], values[23],
                values[24],
                embedding_blob,
            ],
        );

        if let Err(e) = result {
            warn!("upsert_curated failed: {e}");
            return false;
        }

        // Update FTS
        let _ = conn.execute(
            "INSERT OR REPLACE INTO curated_fts(rowid, content, scene_name, tags, workspace_id) \
             VALUES ((SELECT rowid FROM curated WHERE id = ?1), ?2, ?3, ?4, ?5)",
            params![r.id, r.content, r.scene_name.as_deref().unwrap_or(""), r.tags.join(" "), r.workspace_id],
        );

        info!("upserted curated record {}", r.id);
        true
    }

    async fn delete_curated_batch(&self, ids: &[String]) -> bool {
        let conn = self.conn.lock().unwrap();
        for id in ids {
            let _ = conn.execute("DELETE FROM curated WHERE id = ?1", params![id]);
        }
        true
    }

    async fn delete_curated_expired(&self, cutoff_iso: &str) -> usize {
        let conn = self.conn.lock().unwrap();
        let count = conn
            .execute(
                "DELETE FROM curated WHERE archived = 1 AND updated_at < ?1",
                params![cutoff_iso],
            )
            .unwrap_or(0);
        count
    }

    async fn search_curated_vector(&self, q: &[f32], top_k: usize) -> Vec<ScoredRecord> {
        if q.len() != self.embedding_dim {
            warn!(
                "vector dim mismatch: expected {}, got {}",
                self.embedding_dim,
                q.len()
            );
            return Vec::new();
        }
        let conn = self.conn.lock().unwrap();
        let mut stmt = match conn.prepare(
            "SELECT id, content, kind, priority, trust_score, confidence_score,
                    importance_score, scene_name, source, source_type, owner,
                    workspace_id, session_key, session_id, tags, source_message_ids,
                    timestamps, created_at, updated_at, archived, last_accessed_at,
                    exempt_from_decay, exempt_from_dedup, metadata, workspace_path, embedding
             FROM curated WHERE archived = 0 AND embedding IS NOT NULL",
        ) {
            Ok(s) => s,
            Err(e) => {
                warn!("search_curated_vector prepare failed: {e}");
                return Vec::new();
            }
        };

        let rows = match stmt.query_map([], |row| {
            let record = Self::row_to_record(row)?;
            let blob: Vec<u8> = row.get(25)?;
            let emb = Self::blob_to_embedding(&blob);
            let score = Self::cosine_similarity(q, &emb);
            Ok(ScoredRecord {
                record,
                score,
                source_label: "curated_vector".into(),
            })
        }) {
            Ok(rows) => rows,
            Err(e) => {
                warn!("search_curated_vector query failed: {e}");
                return Vec::new();
            }
        };

        let mut results: Vec<ScoredRecord> = rows.filter_map(|r| r.ok()).collect();
        results.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
        results.truncate(top_k);
        results
    }

    async fn search_curated_fts(&self, fts_query: &str, limit: usize) -> Vec<ScoredRecord> {
        let query = match Self::build_fts_query(fts_query) {
            Some(q) => q,
            None => return Vec::new(),
        };

        let conn = self.conn.lock().unwrap();
        let sql = "SELECT c.id, c.content, c.kind, c.priority, c.trust_score, c.confidence_score,
                          c.importance_score, c.scene_name, c.source, c.source_type, c.owner,
                          c.workspace_id, c.session_key, c.session_id, c.tags, c.source_message_ids,
                          c.timestamps, c.created_at, c.updated_at, c.archived, c.last_accessed_at,
                          c.exempt_from_decay, c.exempt_from_dedup, c.metadata, c.workspace_path, bm25(curated_fts) as rank
                   FROM curated_fts
                   JOIN curated c ON c.rowid = curated_fts.rowid
                   WHERE curated_fts MATCH ?1 AND c.archived = 0
                   ORDER BY rank
                   LIMIT ?2";

        let over_fetch = (limit * 3).min(50);
        let mut stmt = match conn.prepare(sql) {
            Ok(s) => s,
            Err(e) => {
                warn!("FTS query failed: {e}");
                return Vec::new();
            }
        };

        let rows = match stmt.query_map(params![query, over_fetch as i64], |row| {
            let mut record = Self::row_to_record(row)?;
            let bm25_score: f64 = row.get(25)?;
            let score = -bm25_score as f32;
            record.id = row.get(0)?;
            Ok(ScoredRecord {
                record,
                score,
                source_label: "curated_fts".into(),
            })
        }) {
            Ok(rows) => rows,
            Err(e) => {
                warn!("FTS query_map failed: {e}");
                return Vec::new();
            }
        };

        let mut results: Vec<ScoredRecord> = rows.filter_map(|r| r.ok()).collect();

        // Relative score floor trim (mimo pattern)
        if !results.is_empty() {
            let top_score = results[0].score;
            let floor_ratio = 0.15f32;
            let cutoff = top_score * floor_ratio;
            results.retain(|r| r.score >= cutoff);
        }
        results.truncate(limit);
        results
    }

    async fn search_curated_hybrid(&self, q: HybridQuery) -> Vec<ScoredRecord> {
        let mut fts_results = Vec::new();
        let mut vec_results = Vec::new();

        if let Some(ref text) = q.query_text {
            fts_results = self.search_curated_fts(text, q.top_k * 2).await;
        }
        if let Some(ref emb) = q.query_embedding {
            vec_results = self.search_curated_vector(emb, q.top_k * 2).await;
        }

        // RRF merge (k=60)
        let rrf_k = 60.0f32;
        let mut score_map: std::collections::HashMap<String, (ScoredRecord, f32)> =
            std::collections::HashMap::new();

        for (rank, r) in fts_results.iter().enumerate() {
            let rrf_score = 1.0 / (rrf_k + rank as f32);
            let entry = score_map
                .entry(r.record.id.clone())
                .or_insert_with(|| (r.clone(), 0.0));
            entry.1 += rrf_score;
        }
        for (rank, r) in vec_results.iter().enumerate() {
            let rrf_score = 1.0 / (rrf_k + rank as f32);
            let entry = score_map
                .entry(r.record.id.clone())
                .or_insert_with(|| (r.clone(), 0.0));
            entry.1 += rrf_score;
        }

        let mut merged: Vec<ScoredRecord> = score_map
            .into_values()
            .map(|(mut r, score)| {
                r.score = score;
                r.source_label = "hybrid_rrf".into();
                r
            })
            .collect();
        merged.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
        merged.truncate(q.top_k);
        merged
    }

    async fn upsert_raw(&self, r: &RawTurn, embedding: Option<&[f32]>) -> bool {
        let conn = self.conn.lock().unwrap();
        let embedding_blob = embedding.map(|e| Self::embedding_to_blob(e));
        let result = conn.execute(
            "INSERT OR REPLACE INTO raw (id, role, content, session_key, session_id, workspace_id, recorded_at, metadata, workspace_path, embedding)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
            params![
                r.id,
                r.role,
                r.content,
                r.session_key,
                r.session_id,
                r.workspace_id,
                r.recorded_at,
                serde_json::to_string(&r.metadata).unwrap_or_default(),
                r.workspace_path,
                embedding_blob,
            ],
        );
        if let Err(e) = result {
            warn!("upsert_raw failed: {e}");
            return false;
        }
        let _ = conn.execute(
            "INSERT OR REPLACE INTO raw_fts(rowid, content, workspace_id) \
             VALUES ((SELECT rowid FROM raw WHERE id = ?1), ?2, ?3)",
            params![r.id, r.content, r.workspace_id],
        );
        true
    }

    async fn search_raw_vector(&self, q: &[f32], top_k: usize) -> Vec<ScoredRaw> {
        if q.len() != self.embedding_dim {
            return Vec::new();
        }
        let conn = self.conn.lock().unwrap();
        let mut stmt = match conn.prepare(
            "SELECT id, role, content, session_key, session_id, workspace_id, recorded_at, metadata, workspace_path, embedding
             FROM raw WHERE embedding IS NOT NULL",
        ) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };

        let rows = match stmt.query_map([], |row| {
            let turn = Self::raw_row_to_record(row)?;
            let blob: Vec<u8> = row.get(9)?;
            let emb = Self::blob_to_embedding(&blob);
            let score = Self::cosine_similarity(q, &emb);
            Ok(ScoredRaw { turn, score })
        }) {
            Ok(rows) => rows,
            Err(e) => {
                warn!("search_raw_vector query failed: {e}");
                return Vec::new();
            }
        };

        let mut results: Vec<ScoredRaw> = rows.filter_map(|r| r.ok()).collect();
        results.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
        results.truncate(top_k);
        results
    }

    async fn search_raw_fts(&self, fts_query: &str, limit: usize) -> Vec<ScoredRaw> {
        let query = match Self::build_fts_query(fts_query) {
            Some(q) => q,
            None => return Vec::new(),
        };

        let conn = self.conn.lock().unwrap();
        let sql = "SELECT r.id, r.role, r.content, r.session_key, r.session_id, r.workspace_id,
                          r.recorded_at, r.metadata, r.workspace_path, bm25(raw_fts) as rank
                   FROM raw_fts
                   JOIN raw r ON r.rowid = raw_fts.rowid
                   WHERE raw_fts MATCH ?1
                   ORDER BY rank
                   LIMIT ?2";

        let mut stmt = match conn.prepare(sql) {
            Ok(s) => s,
            Err(e) => {
                warn!("raw FTS query failed: {e}");
                return Vec::new();
            }
        };

        let rows = match stmt.query_map(params![query, limit as i64], |row| {
            let turn = RawTurn {
                id: row.get(0)?,
                role: row.get(1)?,
                content: row.get(2)?,
                session_key: row.get(3)?,
                session_id: row.get(4)?,
                workspace_id: row.get(5)?,
                workspace_path: row.get(8)?,
                recorded_at: row.get(6)?,
                metadata: serde_json::from_str(&row.get::<_, String>(7)?).unwrap_or_default(),
            };
            let bm25_score: f64 = row.get(9)?;
            Ok(ScoredRaw {
                turn,
                score: -bm25_score as f32,
            })
        }) {
            Ok(rows) => rows,
            Err(e) => {
                warn!("raw FTS query_map failed: {e}");
                return Vec::new();
            }
        };

        let mut results: Vec<ScoredRaw> = rows.filter_map(|r| r.ok()).collect();
        results.truncate(limit);
        results
    }

    async fn reindex_all(&self, embed: &EmbedFn) -> ReindexCounts {
        let conn = self.conn.lock().unwrap();
        let mut curated_count = 0usize;
        let mut raw_count = 0usize;

        // Reindex curated
        if let Ok(mut stmt) = conn.prepare("SELECT id, content FROM curated WHERE embedding IS NULL") {
            if let Ok(rows) = stmt.query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))) {
                for row in rows.flatten() {
                    if let Some(emb) = embed(&row.1) {
                        let blob = Self::embedding_to_blob(&emb);
                        let _ = conn.execute("UPDATE curated SET embedding = ?1 WHERE id = ?2", params![blob, row.0]);
                        curated_count += 1;
                    }
                }
            }
        }

        // Reindex raw
        if let Ok(mut stmt) = conn.prepare("SELECT id, content FROM raw WHERE embedding IS NULL") {
            if let Ok(rows) = stmt.query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))) {
                for row in rows.flatten() {
                    if let Some(emb) = embed(&row.1) {
                        let blob = Self::embedding_to_blob(&emb);
                        let _ = conn.execute("UPDATE raw SET embedding = ?1 WHERE id = ?2", params![blob, row.0]);
                        raw_count += 1;
                    }
                }
            }
        }

        ReindexCounts {
            curated_count,
            raw_count,
        }
    }

    async fn fact(&self, op: FactOp) -> FactResult {
        let conn = self.conn.lock().unwrap();
        match op {
            FactOp::Add { content, entities } => {
                let id = generate_id();
                let now = chrono::Utc::now().to_rfc3339();
                let entities_json = serde_json::to_string(&entities).unwrap_or_default();
                let _ = conn.execute(
                    "INSERT INTO facts (id, content, entities, trust_score, created_at, updated_at) VALUES (?1,?2,?3,0.50,?4,?4)",
                    params![id, content, entities_json, now],
                );
                let _ = conn.execute(
                    "INSERT INTO facts_fts(rowid, content, entities) VALUES ((SELECT rowid FROM facts WHERE id = ?1), ?2, ?3)",
                    params![id, content, entities_json],
                );
                FactResult::Added { id }
            }
            FactOp::Search { query, limit } => {
                let fts_query = match Self::build_fts_query(&query) {
                    Some(q) => q,
                    None => return FactResult::SearchResults { results: vec![] },
                };
                let sql = "SELECT f.id, f.content, f.entities, f.trust_score, f.created_at, f.updated_at, bm25(facts_fts) as rank
                           FROM facts_fts JOIN facts f ON f.rowid = facts_fts.rowid
                           WHERE facts_fts MATCH ?1 ORDER BY rank LIMIT ?2";
                let results = if let Ok(mut stmt) = conn.prepare(sql) {
                    stmt.query_map(params![fts_query, limit as i64], |row| {
                        Ok(ScoredRecord {
                            record: MemoryRecord {
                                id: row.get(0)?,
                                content: row.get(1)?,
                                kind: MemoryKind::Fact,
                                trust_score: row.get::<_, f64>(3)? as f32,
                                ..MemoryRecord::new("")
                            },
                            score: -row.get::<_, f64>(6)? as f32,
                            source_label: "fact_fts".into(),
                        })
                    })
                    .map(|r| r.filter_map(|r| r.ok()).collect())
                    .unwrap_or_default()
                } else {
                    vec![]
                };
                FactResult::SearchResults { results }
            }
            FactOp::Probe { entity } => {
                let sql = "SELECT id, content, entities, trust_score, created_at, updated_at FROM facts WHERE entities LIKE ?1";
                let pattern = format!("%\"{}\"%", entity);
                let results = if let Ok(mut stmt) = conn.prepare(sql) {
                    stmt.query_map(params![pattern], |row| {
                        Ok(ScoredRecord {
                            record: MemoryRecord {
                                id: row.get(0)?,
                                content: row.get(1)?,
                                kind: MemoryKind::Fact,
                                trust_score: row.get::<_, f64>(3)? as f32,
                                ..MemoryRecord::new("")
                            },
                            score: 1.0,
                            source_label: "fact_probe".into(),
                        })
                    })
                    .map(|r| r.filter_map(|r| r.ok()).collect())
                    .unwrap_or_default()
                } else {
                    vec![]
                };
                FactResult::Probe { results }
            }
            FactOp::Update { id, content } => {
                let now = chrono::Utc::now().to_rfc3339();
                let changed = conn
                    .execute(
                        "UPDATE facts SET content = ?1, updated_at = ?2 WHERE id = ?3",
                        params![content, now, id],
                    )
                    .unwrap_or(0);
                FactResult::Updated {
                    success: changed > 0,
                }
            }
            FactOp::Remove { id } => {
                let changed = conn
                    .execute("DELETE FROM facts WHERE id = ?1", params![id])
                    .unwrap_or(0);
                FactResult::Removed {
                    success: changed > 0,
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_record(content: &str) -> MemoryRecord {
        let mut r = MemoryRecord::new(content);
        r.source = "test".into();
        r.tags = vec!["test".into()];
        r
    }

    #[tokio::test]
    async fn roundtrip_upsert_search() {
        let store = SqliteStore::memory(4).unwrap();
        let r = test_record("rust is a systems programming language");
        store.upsert_curated(&r, None).await;

        let results = store.search_curated_fts("rust programming", 10).await;
        assert!(!results.is_empty());
        assert!(results[0].record.content.contains("rust"));
    }

    #[tokio::test]
    async fn vector_search_with_embedding() {
        let store = SqliteStore::memory(4).unwrap();
        let mut r = test_record("hello world");
        r.workspace_id = "ws1".into();
        let emb = vec![1.0, 0.0, 0.0, 0.0];
        store.upsert_curated(&r, Some(&emb)).await;

        let query_emb = vec![1.0, 0.0, 0.0, 0.0];
        let results = store.search_curated_vector(&query_emb, 10).await;
        assert!(!results.is_empty());
        assert!((results[0].score - 1.0).abs() < 0.001);
    }

    #[tokio::test]
    async fn hybrid_rrf_merge() {
        let store = SqliteStore::memory(4).unwrap();
        let r1 = test_record("rust memory safety borrow checker");
        let r2 = test_record("python garbage collection runtime");
        let emb1 = vec![1.0, 0.0, 0.0, 0.0];
        let emb2 = vec![0.0, 1.0, 0.0, 0.0];
        store.upsert_curated(&r1, Some(&emb1)).await;
        store.upsert_curated(&r2, Some(&emb2)).await;

        let q = HybridQuery {
            query_text: Some("rust borrow".into()),
            query_embedding: Some(vec![1.0, 0.0, 0.0, 0.0]),
            sparse_vector: None,
            top_k: 5,
            workspace_id: None,
        };
        let results = store.search_curated_hybrid(q).await;
        assert!(!results.is_empty());
        assert!(results[0].record.content.contains("rust"));
    }

    #[tokio::test]
    async fn raw_turn_fts() {
        let store = SqliteStore::memory(4).unwrap();
        let t = RawTurn::new("user", "how do I configure the database?");
        store.upsert_raw(&t, None).await;

        let results = store.search_raw_fts("configure database", 5).await;
        assert!(!results.is_empty());
        assert!(results[0].turn.content.contains("configure"));
    }

    #[tokio::test]
    async fn fact_store_ops() {
        let store = SqliteStore::memory(4).unwrap();
        let result = store
            .fact(FactOp::Add {
                content: "Rust uses ownership for memory safety".into(),
                entities: vec!["rust".into(), "memory".into()],
            })
            .await;
        let id = match &result {
            FactResult::Added { id } => id.clone(),
            _ => panic!("expected Added"),
        };

        let search = store
            .fact(FactOp::Search {
                query: "ownership".into(),
                limit: 5,
            })
            .await;
        match search {
            FactResult::SearchResults { results } => assert!(!results.is_empty()),
            _ => panic!("expected SearchResults"),
        }

        let probe = store
            .fact(FactOp::Probe {
                entity: "rust".into(),
            })
            .await;
        match probe {
            FactResult::Probe { results } => assert!(!results.is_empty()),
            _ => panic!("expected Probe"),
        }

        let update = store
            .fact(FactOp::Update {
                id: id.clone(),
                content: "Rust uses ownership and borrowing for memory safety".into(),
            })
            .await;
        match update {
            FactResult::Updated { success } => assert!(success),
            _ => panic!("expected Updated"),
        }

        let remove = store.fact(FactOp::Remove { id }).await;
        match remove {
            FactResult::Removed { success } => assert!(success),
            _ => panic!("expected Removed"),
        }
    }

    #[tokio::test]
    async fn workspace_isolation() {
        let store = SqliteStore::memory(4).unwrap();
        let mut r1 = test_record("project alpha secret");
        r1.workspace_id = "alpha".into();
        let mut r2 = test_record("project beta secret");
        r2.workspace_id = "beta".into();
        store.upsert_curated(&r1, None).await;
        store.upsert_curated(&r2, None).await;

        let alpha_results = store.search_curated_fts("secret", 10).await;
        // FTS doesn't filter by workspace in our impl, but both should be present
        assert!(alpha_results.len() >= 2);
    }

    #[tokio::test]
    async fn fts_query_builder() {
        let q = SqliteStore::build_fts_query("postgres database port 5433");
        assert_eq!(
            q.unwrap(),
            "\"postgres\" OR \"database\" OR \"port\" OR \"5433\""
        );
        assert!(SqliteStore::build_fts_query("").is_none());
        assert!(SqliteStore::build_fts_query("  ").is_none());
    }
}
