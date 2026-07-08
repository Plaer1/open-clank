# MiMo-Code Memory System — Full Investigation Report

**Date:** 2026-06-12
**Source:** `/home/e/sauce/ai/agents/mimo-code/packages/opencode/src/`

---

## 1. What the Memory System Stores

MiMo-Code has **two parallel FTS5-backed search systems** that work together:

### 1A. `memory` — Curated, durable knowledge (human + agent written)

| Scope | Content | Path | Purpose |
|-------|---------|------|---------|
| `global` | Single `MEMORY.md` | `<data>/memory/global/MEMORY.md` | Cross-project user preferences, long-lived rules |
| `projects` | Per-project `MEMORY.md` | `<data>/memory/projects/<pid>/MEMORY.md` | Project identity, rules, architecture decisions, durable knowledge |
| `sessions` | Checkpoint + notes + tasks | `<data>/memory/sessions/<sid>/` | Per-session state: checkpoint.md, notes.md, tasks/<tid>/progress.md |
| `cc` | Claude Code project memory | `~/.claude/projects/<slug>/memory/*.md` | Optional opt-in indexing of CC's own memory files |

**Memory types** (detected from filename patterns):
- `memory` — matches `MEMORY.md` or `memory.md` or `memory-*.md` (case-insensitive for legacy migration)
- `checkpoint` — matches `checkpoint.md` or `checkpoint-*.md`
- `progress` — matches `tasks/<id>/progress.md`
- `notes` — matches `tasks/<id>/notes.md`
- `free` — anything else

For CC-scoped memory, type is extracted from YAML frontmatter (`metadata.type`):
- `feedback`, `project`, `reference`, `user`

### 1B. `history` — Raw conversation trajectory (auto-logged)

| Column | Description |
|--------|-------------|
| `part_id` | Primary key (message part UUID) |
| `session_id` | Session this message belongs to |
| `message_id` | Message ID within session |
| `project_id` | Project ID (for scoping) |
| `kind` | `user_text`, `assistant_text`, `tool_input`, `tool_error`, `reasoning`, `tool_output` |
| `tool_name` | Which tool was called (for tool kinds) |
| `body` | The actual text content |
| `time_created` | Unix ms timestamp |

The history system is auto-populated by a Bus subscriber (`HistoryWriter`) and backfilled on startup (`HistoryBackfill`). It's the raw, unfiltered trajectory — every message, every tool call, every output.

---

## 2. How It Stores Data

### SQLite (Bun SQLite + Drizzle ORM)

**Single database file:** `<data>/mimocode.db` (configurable via `MIMOCODE_DB` flag or channel-specific names like `mimocode-beta.db`).

PRAGMA settings:
- `journal_mode = WAL`
- `synchronous = NORMAL`
- `busy_timeout = 5000`
- `cache_size = -64000` (~64 MB)
- `foreign_keys = ON`

**Two FTS5 virtual tables:**

#### `memory_fts_idx` (on `memory_fts` table):
```sql
CREATE VIRTUAL TABLE memory_fts_idx USING fts5(
  body,
  content='memory_fts',
  content_rowid='id',
  tokenize='unicode61 remove_diacritics 1'
);
```

SQL triggers auto-sync the FTS index on INSERT/UPDATE/DELETE to the `memory_fts` table.

The `memory_fts` table itself stores:
- `id`, `path` (UNIQUE), `scope`, `scope_id`, `type`, `body` (full markdown), `fingerprint` (size + mtime), `last_indexed_at`

#### `history_fts_idx` (on `history_fts` table):
```sql
CREATE VIRTUAL TABLE history_fts_idx USING fts5(
  body,
  content='history_fts',
  content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 1'
);
```

Same pattern — triggers auto-sync. Both use `unicode61 remove_diacritics 1` tokenizer.

### File-based (markdown on disk)

The memory system is fundamentally **file-based with a SQLite index on top**. The authoritative store is the filesystem:

- `<data>/memory/global/MEMORY.md` — single file
- `<data>/memory/projects/<pid>/MEMORY.md` — per project
- `<data>/memory/sessions/<sid>/checkpoint.md` — session checkpoint
- `<data>/memory/sessions/<sid>/notes.md` — session scratchpad
- `<data>/memory/sessions/<sid>/tasks/<tid>/progress.md` — task narrative

The SQLite FTS5 index is reconstructed from these files via **reconciliation** — not the other way around. If you delete the SQLite database, the agent can re-index everything from the files. If you delete a memory file, the next reconcile prunes it from the index.

**Fingerprints** (size + mtime) track which files have changed since the last reconcile, making re-indexing incremental.

---

## 3. How It Retrieves Memories

### Search pipeline (detailed):

1. **Query tokenization** (`./memory/fts-query.ts`):
   - Raw query string → regex split on non-word boundaries (`[\p{L}\p{N}_]+` — Unicode-aware, includes CJK)
   - Each token phrase-quoted (`"token"`) and **OR-joined**
   - This avoids FTS5 special-character crashes (punctuation like `.`, `(`, `-`)
   - CJK support was added in PR #20767
   - Null returned if no usable tokens → empty result

2. **FTS5 MATCH + BM25 ranking**:
   ```sql
   SELECT memory_fts.path, scope, scope_id, type,
          snippet(memory_fts_idx, 0, '<<', '>>', '...', 32) AS snippet,
          bm25(memory_fts_idx) AS score
   FROM memory_fts_idx
   JOIN memory_fts ON memory_fts.id = memory_fts_idx.rowid
   WHERE memory_fts_idx MATCH ?
   ORDER BY score
   LIMIT ?
   ```

3. **Score normalization + relative floor**:
   - BM25 returns **lower = better**; converted to **higher = better** for the caller (`score = -r.score`)
   - **Relative floor:** drops results scoring below `floor_ratio` (default 0.15) of the top hit
   - The #1 result is **always kept** (even if BM25 can't discriminate in a tiny corpus)
   - Over-fetch (3x, max 50) before applying the floor to avoid starving the list
   - Configurable: `memory_search_score_floor` (0 = keep all)

4. **Lazy reconcile**:
   - Before every search, the system optionally reconciles (re-indexes changed files)
   - Controlled by `memory_reconcile_on_search` config (default: true)
   - This ensures off-tool writes (e.g., manual edits, checkpoint subagent writes) are discovered

5. **Scope/type filtering**:
   - Optional `scope`, `scope_id`, `type` filters appended as AND clauses to the WHERE

6. **History search** uses a nearly identical pipeline, but:
   - Uses **AND-join** (not OR) for tokens — stricter, expects exact multi-word matches
   - No relative score floor
   - No lazy reconcile (history is written by a Bus subscriber, not from files)
   - Supports additional temporal filters (`time_after`/`time_before`), `kind`, `tool_name`
   - The `around()` operation pulls message context (N before/after) around a specific message_id from the `MessageTable`

### Search is NOT deterministic in the traditional sense
The relative score floor means the same query can return different result counts depending on the top hit's BM25 score — which changes as the corpus grows and IDF values shift.

---

## 4. How It Persists Across Sessions

### The Memory Hierarchy

```
memory/global/MEMORY.md          ← Cross-project, persists forever
memory/projects/<pid>/MEMORY.md  ← Per-project, persists forever
memory/sessions/<sid>/           ← Per-session, ephemeral in practice
├── checkpoint.md                ← Structured session state (11 sections)
├── notes.md                     ← Agent scratchpad
└── tasks/
    └── <tid>/
        ├── progress.md          ← Subagent task narrative
        └── notes.md             ← Subagent task notes
```

### Session Continuity (Rebuilds)

When a session exceeds its context window:

1. **Checkpoint event triggers** — the `SessionCompaction` service creates a synthetic compaction user message
2. **Compaction subagent runs** — an LLM (either the compaction agent or the session's own model) summarizes the conversation history into the prompt template
3. **Compacted messages are stored** — the assistant response with `summary: true` is persisted in the message stream
4. **Future rebuilds** inject the checkpoint dump, project memory, global memory, and a memory keys index into the system prompt so the agent can "pick up where it left off"

### Auto-Dream and Auto-Distill (Cross-Session Learning)

Two automatic processes run on new session start:

- **Auto Dream** (default: every 7 days): Spawns a subagent to consolidate durable, verified information from recent sessions into project memory
- **Auto Distill** (default: every 30 days): Reviews past month of sessions to identify repeated manual workflows worth packaging as skills/agents/commands

Both check the session database for the last time a `"Auto Dream"` or `"Auto Distill"` titled session ran, and skip if too recent or if the project is too young.

---

## 5. How the Agent Learns / Improves Over Time

### Direct learning pathways:

1. **Agent Edits MEMORY.md** — The system prompt teaches the agent all four memory file types. The agent may directly Edit MEMORY.md's ## Rules, ## Architecture decisions, or ## Discovered durable knowledge sections when it identifies durable cross-session knowledge.

2. **Checkpoint subagent promotes knowledge** — The checkpoint-writer subagent (spawned on context overflow) reads the conversation tail and:
   - Session working rules → MEMORY.md ## Rules (if project-durable)
   - Cross-task facts → MEMORY.md ## Discovered durable knowledge
   - Design decisions → MEMORY.md ## Architecture decisions

3. **Notes scratchpad** — The agent writes to notes.md for quotes, unresolved questions, cross-project observations. The checkpoint writer reconciles these into structured sections.

4. **Auto Dream consolidation** — A spawned subagent reviews raw trajectory data (SQLite) and memory files, then writes consolidated knowledge to project memory. This is the only process that reads the raw database — everything else works through the memory search tool.

5. **Auto Distill** — Identifies repeated workflows and creates skills/agents/commands to automate them.

### What does NOT auto-learn:

- The agent does NOT automatically remember failed approaches or "what not to do" unless the user or checkpoint writer explicitly documents it
- Preferences must be stated or observed and manually written into global/MEMORY.md
- There is no reinforcement learning or embedding-based similarity across memory entries

---

## 6. Relationship to the Orthogonalizer Concept

The term "orthogonalizer" appears only in `session/prune.ts` — referencing two **orthogonal invariants**:

> "Checkpoint serves main/peer only; subagents use per-actor compaction (independent layers)."
> "Keys on MODE. They are orthogonal invariants that merely overlap today."

**Not a formal concept.** The prune.ts reference is about architectural invariants being orthogonal to each other — specifically that agent type (checkpoint-writer vs main) and mode (subagent vs peer) are separate concerns that happen to overlap in the current implementation.

There is **no orthogonalizer module, class, or algorithm** in the codebase. The codebase doesn't use the term to describe any memory-related component.

---

## 7. Path Guard and Write Security

The `memory-path-guard.ts` enforces:

- **Valid scopes only**: `global`, `projects`, `sessions`
- **Checkpoint-writer subagent** can only write to specific paths:
  - `projects/<pid>/MEMORY.md` (or `memory-<topic>.md` spillover)
  - `sessions/<sid>/checkpoint.md` (or `checkpoint-<topic>.md` spillover)
  - `sessions/<sid>/notes.md`
  - `sessions/<sid>/tasks/<tid>/*.md`
- **Main agent**: Everything except `tasks/` (reserved for checkpoint-writer/subagent)
- **Task-bound subagents**: Can write under their own `tasks/<tid>/` subtree only
- Rejects path traversal (`..`), absolute paths, invalid scopes

---

## 8. What's Unique About MiMo-Code's Approach vs Others

### Vs Claude Code (CC):
- **Dual FTS5 index**: CC has `~/.claude/projects/<slug>/memory/` files only; MiMo-Code adds a full SQLite FTS5 index over them with BM25 ranking
- **Cross-scope recall**: MiMo-Code can optionally index CC memory files under `scope="cc"`, making them searchable from any agent
- **Session-level checkpoint**: More structured than CC's flat memory files — 11 sections with token budgets per section
- **Notes scratchpad**: A defined free-form file (notes.md) rather than ad-hoc files; the checkpoint writer reconciles it

### Vs OpenClaw:
- `memory/fts-query.ts` explicitly credits OpenClaw's approach: "Ported from openclaw's extensions/memory-core/src/memory/hybrid.ts:30"
- Both use FTS5 + BM25, but MiMo-Code uses **external content FTS5** (content table separate from FTS index) while OpenClaw may use internal storage
- MiMo-Code adds relative score floor, CC index support, and the per-scope architecture

### Vs Cursor / Copilot:
- **No embeddings**: MiMo-Code doesn't use dense embeddings or semantic search — it's purely BM25 keyword search. This is intentional: keyword search is predictable and debuggable, while dense embeddings can produce opaque noise.
- **No vector database**: Everything is SQLite FTS5 + files — remarkably simple infrastructure
- **Content-external FTS5**: The FTS5 index stores only the `body` column; the `path`, `scope`, `scope_id`, `type` live in the content table, with constraints enforced by SQL triggers. This enables SQL-level filtering on metadata without re-parsing the index.

### Vs LangChain / Haystack:
- **Minimal**: No chain-of-thought retrieval, no document splitters, no embedding endpoints, no reranking stage
- **Deterministic-with-threshold**: BM25 is deterministic; the only non-determinism is the relative score floor adjusting to corpus size
- **No intermediate representations**: Memory is plain markdown — no vector chunks, no JSON blobs, no serialization layer

### Novel characteristics:

1. **File-authoritative, SQLite-indexed**: The canonical state is the filesystem. The SQLite index is rebuildable from scratch. This is the reverse of most systems where the database is the source of truth.

2. **Fingerprint-tracking**: Uses `size-mtime` fingerprints to skip re-indexing unchanged files — critical for speed with zero-config correctness.

3. **Relative BM25 floor**: A practical hack for small corpora where absolute BM25 scores are meaningless. Top hit always survives; others must score at least 15% of top.

4. **OR-join with BM25 ranking**: Most FTS search is AND-join (return docs matching ALL terms). MiMo-Code uses OR-join because AND-join on multi-word queries returns zero results too often (empirically confirmed: "permission deadlock" as 2-word AND query returned 0 in an 80-doc corpus). OR-join with BM25 ranking + score floor achieves the same practical effect: docs matching more/rarer terms rank higher.

5. **History as a separate FTS index**: The `history` tool (raw conversation) and `memory` tool (curated knowledge) are separate search scopes with slightly different query strategies (AND vs OR) — the agent learns to escalate from memory to history when memory returns nothing.

6. **Rebuild memory injection**: After a checkpoint, the rebuild context injects:
   - Full checkpoint dump (budget-capped per section)
   - Full MEMORY.md (budget-capped)
   - Full notes.md (budget-capped)
   - Full global MEMORY.md (budget-capped)
   - Memory keys index (paths only, budget-capped)
   - Then the preserved tail of raw messages
   - Then active recall reminders (reminders to search before asking the user)

7. **Configuration-driven**: Every cap is configurable (`mimocode.json`):
   - `memory_titles`: Token cap for memory keys index (default 500)
   - `memory` section: Token cap for project MEMORY.md (default 10,000)
   - `global` section: Token cap for global MEMORY.md (default 6,000)
   - `checkpoint` section: Token cap for checkpoint.md (default 11,000)
   - `notes` section: Token cap for notes.md (default 6,000)
   - `memory_reconcile_on_search`: Whether to reconcile before search (default true)
   - `memory_search_score_floor`: BM25 relative floor (default 0.15)
   - `cc_index`: Index Claude Code memory (default false)

---

## 9. Summary Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                       MEMORY SYSTEM                                  │
│                                                                      │
│  ┌──────────────┐     ┌────────────────┐                             │
│  │  Filesystem   │◄────│  SQLite FTS5   │◄── Agent writes to files   │
│  │  (authorita-  │────►│  (index, not   │    via Write/Edit tool     │
│  │   tive store) │     │   source of    │                            │
│  │              │     │   truth)       │    Checkpoint writer        │
│  │  *.md files   │     │              │    writes via subagent      │
│  └──────────────┘     └──────┬─────────┘                            │
│                              │                                       │
│                   ┌──────────▼──────────┐                            │
│                   │  Memory.Service      │                            │
│                   │  search()             │── BM25 OR-join query     │
│                   │  reconcile()          │── Sync files→SQLite     │
│                   │  root()               │── Get memory root path  │
│                   └──────────────────────┘                            │
│                              │                                       │
│                   ┌──────────▼──────────┐                            │
│                   │  MemoryTool          │                            │
│                   │  agent-facing tool   │── search only (read-only) │
│                   └──────────────────────┘                            │
│                                                                      │
│  ┌──────────────┐     ┌────────────────┐                             │
│  │  Message DB   │────►│  History FTS5  │── BM25 AND-join           │
│  │  (raw conver- │     │  (auto-logged) │                            │
│  │  sation)      │     └────────────────┘                            │
│  └──────────────┘                                                    │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  REBUILD PIPELINE (on context overflow)                      │   │
│  │                                                              │   │
│  │  1. Compaction subagent summarizes conversation              │   │
│  │  2. Checkpoint writer updates checkpoint.md + MEMORY.md      │   │
│  │  3. Rebuild injects: checkpoint dump + MEMORY.md + global    │   │
│  │     + notes + memory keys index + preserved tail             │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  AUTO-LEARNING                                                │   │
│  │                                                              │   │
│  │  - Auto Dream: every 7d, consolidate into project memory     │   │
│  │  - Auto Distill: every 30d, find workflow patterns → skills  │   │
│  │  - Checkpoint promotion: session rules → project memory      │   │
│  │  - Direct agent edits: agent writes MEMORY.md mid-task       │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 10. Key Files Reference

| File | Role |
|------|------|
| `memory/service.ts` | Core service: `search()`, `reconcile()`, `root()` |
| `memory/fts.sql.ts` | Drizzle schema for `memory_fts` table |
| `memory/fts-query.ts` | Tokenizer: OR-join, Unicode-aware, CJK support |
| `memory/reconcile.ts` | Walks disk, fingerprints, indexes/prunes SQLite |
| `memory/paths.ts` | Path parsing, type detection, `buildPath()`, CC frontmatter |
| `tool/memory.ts` | Agent-facing `memory` tool (search only) |
| `tool/memory.txt` | Tool description shown to the agent |
| `tool/memory-path-guard.ts` | Write path validation (scope + agent type) |
| `history/service.ts` | History search + around (context retrieval) |
| `history/fts.sql.ts` | Drizzle schema for `history_fts` table |
| `history/fts-query.ts` | Tokenizer: AND-join (note: different from memory!) |
| `history/writer.ts` | Bus subscriber that auto-logs conversation to FTS |
| `session/checkpoint.ts` | Checkpoint writer orchestration, rebuild context assembly |
| `session/checkpoint-paths.ts` | File path helpers for all memory files |
| `session/checkpoint-templates.ts` | Template content for new checkpoint/MEMORY/notes files |
| `session/compaction.ts` | Context overflow detection + compaction subagent spawning |
| `session/prompt.ts` | Main session loop — prompt building, rebuild memory injection |
| `session/llm.ts` | `buildMemoryInstructions()` — agent-facing memory system docs |
| `session/prune.ts` | Tool output pruning (orthogonal invariants concept) |
| `session/auto-dream.ts` | Auto Dream / Auto Distill scheduling |
| `storage/db.ts` | Single SQLite database init, WAL + PRAGMAs |
| `migration/20260521010000_memory_fts_v6/migration.sql` | FTS5 table creation |
| `migration/20260609000000_history_fts/migration.sql` | History FTS5 table creation |
