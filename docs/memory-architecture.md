# Memory architecture

One memory engine, two hosts, one bank. frankenmemory (`fm-mcp`, Rust +
SQLite FTS5) is the only memory store; Odysseus and MiMo each consume it
through their own native idioms and never learn they're sharing.

## The engine

`mcp_servers/frankenmemory` — an MCP stdio server over a single SQLite
database (`FM_DB_PATH`). Tiers: **raw** (verbatim turn halves), **candidates**
(heuristically admitted, pending review), **curated** (recallable memories,
kinds persona/episodic/instruction/fact/fabric/wiki), plus a semantic graph
(nodes/edges/cues) and per-repo code index.

Every record is scoped by `(owner, workspace_id)`. Owner is the hard security
wall. The workspace axis exists but conversational memory writes to one
canonical workspace — the engine default `"global"` — via a single symbol per
language: `src/memory_scope.py::chat_workspace()` (Python) and
`CHAT_WORKSPACE` in `memory/session-scope.ts` (mimo). Read tools union the
caller's workspace with `"global"`, so per-session workspaces still see the
bank. The axis stays reserved for future genuinely project-scoped memory;
`code_index` already uses it per repo. Never spell the literal — import the
symbol.

## Capture — every chat path writes

- **Direct-endpoint chats** (DeepSeek row, subscription rows): after each
  saved turn, `run_post_response_tasks` calls
  `FrankenmemoryProvider.capture()` → fm `capture` with
  `capture_mode="candidate"` — the same admission pipeline mimo uses.
  Heuristic prefilter, no LLM call, so it runs per turn.
- **MiMo-transport turns**: the child's own `capture.ts` captures them;
  Odysseus skips those (`captured_by_runtime`, i.e. the dispatch went over
  `mimo://acp`) so nothing double-stores.
- **Agent-authored MEMORY.md files**: mimo's reconcile pass projects every
  mimo-root MEMORY-type file into fm through `ingest_authored` — one wiki
  record per `##` section, idempotent by content hash, deletions wipe the
  projection. The file stays the source of truth (records are decay- and
  dedup-exempt); checkpoints, task notes, and cc-root files never ingest.
- **Compaction summaries**: `compaction-capture.ts`, unchanged.
- Incognito and compare-mode turns are never captured anywhere.

## The overlay — index card + pull recall

Every non-incognito turn carries one small **memory index card**: counts,
up to 5 pinned headlines, up to 6 relationship clusters, up to 5 recent
topics (bounded by item counts, never token budgets). It says what the bank
*holds*, never the contents — the model pulls details through the memory
search tool when a topic matters.

- Source: fm `digest` tool (read-only, direct queries, p50 well under 1ms).
- Renderer + sentinel: `src/memory_digest.py` (`render_digest`,
  `DIGEST_SENTINEL = "[Memory Index]"`).
- Odysseus seam: `chat_processor.build_context_preface` injects the card as
  an untrusted context message; it **replaced** the old top-3 auto-recall
  preface. 250ms timeout; failure degrades to no card.
- Bridge seam: `ACPBridge._maybe_inject_digest` prepends the card to mimo
  turns that don't already carry the sentinel (Odysseus-prefaced turns do),
  covering resume flows and future mimo-first paths.
- Deep recall (pull): Odysseus memory tool `search` →
  `provider.recall(top_k=20)`; mimo → its memory tool /
  `frankenmemory:recall`.

## Provider-always

`app_initializer` always constructs an active provider —
`FrankenmemoryProvider` (default) or `NativeMemoryProvider`
(`MEMORY_PROVIDER=native`, JSON store + optional Chroma vector, same
interface). Routes, tools, and the preface talk to the provider only; the
old native fallback branches are gone. `mcp_servers/memory_server.py` (the
standalone memory MCP server) keeps its own native path — separate process.

## Scope plumbing

Both hosts resolve fm scope from one place: Odysseus passes owner per call
under `FM_SCOPE_AUTHORITY=trusted-caller`; mimo sessions get
`FM_OWNER`/`FM_WORKSPACE_ID` from the bridge's MCP descriptor
(`frankenmemory_mcp_descriptor`) and propagate them verbatim
(`session-scope.ts`). MiMo runtimes are partitioned per owner
(`MimoSupervisorPool`), so one child never mixes owners.

## Tests that pin all this

- `tests/test_memory_holistic_acceptance.py` — the whole story against the
  real binary: Brain add → mimo-scoped hit, capture → pending candidate,
  authored section → recall, digest reflects everything, owner wall.
- `tests/test_memory_scope_convention.py` — canonical-workspace guardrails.
- `tests/test_capture_parity.py` — the capture gate matrix.
- `tests/test_memory_digest_preface.py`, `tests/test_bridge_digest.py` —
  both injection seams.
- `tests/test_authored_ingest_tool.py` + bun `test/memory/authored-ingest.test.ts`
  — the file projection lifecycle.
- fm engine: `cargo test` in `mcp_servers/frankenmemory`.
