# Frankenmemory — Final State Writeup

**Date:** 2026-07-02
**Source:** `mcp_servers/frankenmemory/` (Rust, 2 crates)
**Binary:** `target/release/fm-mcp` (6.4MB, built Jun 29)
**Tests:** 40/40 passing (33 unit + 7 integration)

---

## What it is

Frankenmemory is a purpose-built memory engine for AI agents. It's a Rust binary (`fm-mcp`) that speaks MCP (Model Context Protocol) over stdio, stores memories in SQLite with FTS5 full-text search, and provides capture/recall/search/groom tools. It replaces both Odysseus's native JSON-file memory and MiMo Code's native FTS5 markdown memory as the single cross-chat knowledge store.

## Crate structure

```
frankenmemory/
├── crates/
│   ├── fm-core/          # Library: store, providers, retrieval, curation, embedding
│   │   ├── src/
│   │   │   ├── config.rs         # FmConfig + sub-configs
│   │   │   ├── record.rs         # MemoryRecord, RawTurn, CompletedTurn, RecallQuery, etc.
│   │   │   ├── embed.rs          # EmbeddingClient trait + Noop/Http/BM25 implementations
│   │   │   ├── store/
│   │   │   │   ├── mod.rs        # MemoryStore trait
│   │   │   │   └── sqlite.rs     # SqliteStore implementation (FTS5 + vector)
│   │   │   ├── provider/
│   │   │   │   ├── mod.rs        # MemoryProvider trait
│   │   │   │   ├── native.rs     # NativeProvider (the real implementation)
│   │   │   │   ├── memos.rs      # MemosProvider (stub, delegates to native)
│   │   │   │   └── tencent.rs    # TencentProvider (stub, delegates to native)
│   │   │   ├── retrieval/
│   │   │   │   ├── mod.rs        # hybrid_recall (4-level fallback cascade)
│   │   │   │   ├── collapse.rs   # Salience scoring, Hebbian amplification, dedup, pruning
│   │   │   │   ├── rrf.rs        # Reciprocal Rank Fusion (k=60)
│   │   │   │   └── ground_truth.rs # GT classification + formatted output
│   │   │   ├── curate/
│   │   │   │   ├── mod.rs        # Groom dispatcher
│   │   │   │   ├── decay.rs      # Exponential decay with importance-based half-life
│   │   │   │   ├── dedup.rs      # Cosine similarity dedup (threshold 0.92)
│   │   │   │   └── reflect.rs    # Confidence adjustment from corroboration
│   │   │   └── layer/
│   │   │       ├── mod.rs        # RecallLayer trait
│   │   │       ├── layer_a.rs    # Layer A (hybrid recall)
│   │   │       └── layer_b.rs    # Layer B (identical to A, framework for future)
│   │   └── tests/
│   │       └── integration.rs    # 7 integration tests
│   └── fm-mcp/           # Binary: MCP server (stdio transport)
│       └── src/
│           └── main.rs   # 4 MCP tools: capture, recall, search, groom
```

## Config system

```rust
pub struct FmConfig {
    pub db_path: String,           // env FM_DB_PATH or "frankenmemory.db"
    pub embedding: EmbeddingConfig,
    pub recall: RecallConfig,
    pub collapse: CollapseConfig,
    pub decay: DecayConfig,
    pub providers: ProviderConfig,
    pub workspace_id: String,      // default "global"
}
```

### Key config values

| Config | Field | Default | Purpose |
|--------|-------|---------|---------|
| Embedding | `api_base` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| Embedding | `model` | `text-embedding-3-small` | Embedding model |
| Embedding | `dimensions` | `1536` | Vector dimensions |
| Embedding | `cache_size` | `256` | LRU cache entries |
| Recall | `default_mode` | `layer_a` | Default recall strategy |
| Recall | `top_k` | `10` | Default result count |
| Recall | `score_threshold` | `0.3` | Minimum score to return |
| Recall | `fts_score_floor` | `0.15` | Relative score floor (keep top hit, drop noise) |
| Recall | `workspace_boost` | `1.5` | Multiplier for same-workspace records |
| Collapse | `budget` | `6` | Max results after collapse |
| Collapse | `prune_ratio` | `0.35` | Prune below this fraction of max salience |
| Collapse | `dup_overlap` | `0.82` | Token overlap threshold for near-dup detection |
| Collapse | `amplify_gain` | `0.15` | Hebbian corroboration boost per corroborating source |
| Collapse | `amplify_cap` | `0.50` | Max corroboration boost |
| Decay | `half_life_important_days` | `90.0` | Half-life for importance >= 0.3 |
| Decay | `half_life_normal_days` | `30.0` | Half-life for importance < 0.3 |
| Decay | `importance_threshold` | `0.3` | Boundary between normal and important |
| Decay | `exempt_importance_threshold` | `0.7` | Above this, exempt from decay |
| Decay | `decay_threshold` | `0.1` | Below this decay score, archive |
| Providers | `native_enabled` | `true` | Native provider active |
| Providers | `memos_enabled` | `false` | Memos provider (stub) |
| Providers | `tencent_enabled` | `false` | Tencent provider (stub) |

**Env var support:** Only `FM_DB_PATH` is read from environment (in `FmConfig::default()`). All other config is struct-default.

## SQLite schema

### Tables

**`curated`** (26 columns) — the primary memory table:
- `id` TEXT PK — format `m_{epoch_ms}_{rand_hex8}`
- `content` TEXT NOT NULL — the memory text
- `kind` TEXT default `'episodic'` — Persona|Episodic|Instruction|Fact|Fabric|Wiki|Raw
- `priority` INTEGER default `50`
- `trust_score` REAL default `0.50`
- `confidence_score` REAL default `1.0`
- `importance_score` REAL default `0.5`
- `scene_name` TEXT — optional scene/context label
- `source` TEXT default `''`
- `source_type` TEXT default `'auto_extracted'` — Human|Procedural|Ai|AutoExtracted
- `owner` TEXT — optional owner filter
- `workspace_id` TEXT default `'global'` — workspace scoping
- `session_key` TEXT, `session_id` TEXT — session metadata
- `tags` TEXT default `'[]'`, `source_message_ids` TEXT, `timestamps` TEXT
- `created_at` TEXT, `updated_at` TEXT
- `archived` INTEGER default `0`
- `last_accessed_at` TEXT
- `exempt_from_decay` INTEGER, `exempt_from_dedup` INTEGER
- `metadata` TEXT default `'null'`
- `workspace_path` TEXT
- `embedding` BLOB — f32 little-endian bytes

**`raw`** (10 columns) — raw conversation turns:
- `id`, `role`, `content`, `session_key`, `session_id`, `workspace_id`, `workspace_path`, `recorded_at`, `metadata`, `embedding`

**`facts`** (7 columns) — standalone fact store:
- `id`, `content`, `entities` TEXT, `trust_score`, `created_at`, `updated_at`, `embedding`

**FTS5 virtual tables:**
- `curated_fts` — indexes `content, scene_name, tags, workspace_id` (content-synced to `curated`)
- `raw_fts` — indexes `content, workspace_id` (content-synced to `raw`)
- `facts_fts` — indexes `content, entities` (content-synced to `facts`)

**Pragmas:** `journal_mode=WAL`, `synchronous=NORMAL`

## MCP tools (4)

### `capture`
Captures a conversation turn. Creates a `CompletedTurn` with `user_text=content`, `assistant_text=""`. The NativeProvider creates 3 records: raw user turn, raw assistant turn, and a curated combined record. Embeds all three.

**Params:** `content` (required), `owner?`, `session_key?`, `session_id?`, `workspace_id?`, `workspace_path?`, `source?`, `category?`, `metadata?`

### `recall`
Recalls memories relevant to a query. Embeds query, runs hybrid retrieval (FTS + vector via RRF), applies collapse (salience scoring, Hebbian amplification, dedup, pruning), formats with Ground Truth classification.

**Params:** `query` (required), `mode?` ("layer_a"|"layer_b"), `owner?`, `workspace_id?`, `top_k?`, `tier?` ("curated"|"raw")

**Ground Truth ranks:**
- Terminal (1): Human source, exempt from decay, or trust >= 0.8
- Injected (2): Ai source + trust >= 0.6
- Docs (3): Procedural source
- Training (4): everything else

### `search`
Raw FTS search over a tier. No GT framing, no collapse. Returns matching memories directly.

**Params:** `query` (required), `kind?`, `scene?`, `tier?` ("curated"|"raw"), `limit?`, `workspace_id?`

### `groom`
Curation dispatcher. Three operations:
- **decay**: Exponential decay with importance-based half-life. Archives records below threshold (unless high-confidence, which generates alert instead).
- **dedup**: Pairwise cosine similarity on embeddings. Threshold 0.92. Merges near-duplicates (keeps higher trust, unions tags/timestamps).
- **reflect**: For each record, searches for similar records. Low trust variance → boost confidence. High trust variance → lower confidence.

**Params:** `op` (required: "decay"|"dedup"|"reflect"), `workspace_id?`, `dry_run?`

## Retrieval pipeline

`hybrid_recall()` uses a 4-level fallback cascade:
1. **Hybrid** — FTS + vector search merged via RRF (k=60)
2. **Dense-only** — vector search only
3. **FTS-only** — full-text search only
4. **FTS-only again** — keyword fallback

After retrieval, `collapse()` applies:
1. **Salience scoring** — blend of query-token overlap and raw score, multiplied by `rank_decay^rank` and source prior (facts=1.10, fabric=1.05, else=1.00)
2. **Hebbian amplification** — corroboration boost from different-source candidates with high token overlap
3. **Pruning** — remove below `prune_ratio * max_salience`
4. **Near-dup suppression** — greedy keep-if-not-near-dup (overlap >= 0.82), stop at budget (6)

**Attestation:** Blake2b-256 hash over sorted `(source_label:id)` pairs + nonce + salt for tamper detection.

## Embedding system

Three implementations:
- **NoopEmbeddingClient** — deterministic hash-based, for testing. Used by default in fm-mcp.
- **HttpEmbeddingClient** — OpenAI-compatible `/embeddings` endpoint with LRU cache. Feature-gated (`http-embed`, on by default).
- **Bm25Encoder** — sparse vector encoder with IDF corpus stats. Defined but not wired into any search path.

## Workspace filtering

- `workspace_id` tags every record. Default `"global"`.
- Hybrid recall applies `workspace_boost` (1.5x) to records matching the query's workspace.
- Global workspace records are never demoted.
- Decay skips records in other workspaces (unless global).
- `owner` is stored but not used for filtering in recall/search.
- `session_key`/`session_id` are stored but not used for filtering — purely metadata.

## Integration in open-clank

### Three spawn points (all converge on FM_DB_PATH)

1. **Python FrankenmemoryProvider** (`src/frankenmemory_provider.py`) — spawns fm-mcp via MCP stdio for Odysseus-side recall/remember
2. **ACP bridge descriptor** (`src/openclank/acp_bridge.py`) — registers fm-mcp as MCP server on each mimo session
3. **mimo shared client** (`packages/mimo-code/.../memory/mcp-client.ts`) — singleton fm-mcp spawn for mimo's capture/compaction-capture

All three inherit `FM_DB_PATH` from environment → same SQLite file.

### Config injection

`mimo_supervisor.py` injects via `MIMOCODE_CONFIG_CONTENT`:
```json
{
  "skills": {"paths": ["/path/to/skills"]},
  "memory": {"provider": "frankenmemory"}
}
```

This activates mimo's `capture.ts` (per-turn capture) and `compaction-capture.ts` (compaction summary capture). Without `memory.provider: "frankenmemory"`, both skip silently.

### Pre-turn recall

`ACPBridge.run_turn()` calls `memory_provider.recall(user_text, owner, top_k=5)` before each turn. Results are formatted with Ground Truth preamble and injected as a synthetic `audience: ["assistant"]` prompt part.

### Odysseus native memory disabled

`app_initializer.py` sets `native.enabled = False` when frankenmemory is active. The old JSON-file `MemoryManager` still exists as infrastructure but the `NativeMemoryProvider` is not registered as active.

## What's actually working vs what's not

**Working (verified at boot test 2026-07-02):**
- fm-mcp binary built and accessible ✅
- FM_DB_PATH reads env var (config.rs:64) ✅
- Supervisor spawns mimo child, ACP handshake completes ✅
- Safe-dir auto-approve via PermissionHandler ✅
- `memory.provider: "frankenmemory"` injected into mimo config ✅
- FrankenmemoryProvider registers as active, native disabled ✅

**Not yet verified:**
- Memory round-trip (capture via mimo → recall via FrankenmemoryProvider) — P2 gate
- Per-turn automatic capture firing in mimo
- Compaction summary capture
- Pre-turn recall injection in ACP bridge
- Groom operations (decay/dedup/reflect) in production

**Known limitations:**
- NoopEmbeddingClient used by default (no real embeddings unless HTTP endpoint configured)
- Fact store exists but not exposed as MCP tool
- Layer system (LayerA/LayerB) defined but bypassed by NativeProvider
- BM25Encoder defined but not wired
- `category` field in MCP CaptureParams but never stored (no column)
- MemosProvider and TencentProvider are stubs (delegate to native)
- No migration system (CREATE TABLE IF NOT EXISTS only)

## Test coverage

| Area | Tests | Coverage |
|------|-------|---------|
| record.rs | 4 | Serde roundtrip, ID format, defaults, GT rank ordering |
| store/sqlite.rs | 6 | Upsert+FTS, vector search, hybrid RRF, raw turns, fact CRUD, workspace isolation, FTS query builder |
| embed.rs | 5 | Noop normalization, determinism, distinct inputs, BM25 scoring, IDF effect, stopword stripping |
| retrieval/collapse.rs | 4 | Basic collapse, pruning, dedup, attestation roundtrip |
| retrieval/rrf.rs | 2 | Basic merge, ordering preservation |
| retrieval/ground_truth.rs | 5 | GT classification (4 ranks), formatted output |
| curate/dedup.rs | 2 | Cosine similarity (identical + orthogonal) |
| curate/decay.rs | 1 | Decay formula verification |
| integration.rs | 7 | Dual-record capture, recall toggle, GT in both, hybrid pipeline determinism, two-tier search, workspace-scoped recall, standalone no-external-services |
| **Total** | **40** | |

## Archive

Code archived to `~/Downloads/frankenmemory-20260702.tar.gz` (excludes `target/` build artifacts).
