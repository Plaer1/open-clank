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

## Capture — every chat path writes, one owner

Turn capture is **Odysseus-owned and transport-blind**: mimo is a provider
leg, not a separate memory system — every dispatched turn (direct endpoint
or `mimo://acp`) passes through the same post-response seam, and only that
seam knows the full policy context (incognito, compare mode, the user's
auto-memory preference).

- After each saved turn, `run_post_response_tasks` runs
  `capture_turn_and_enrich` (`services/memory/graph_extractor.py`):
  `FrankenmemoryProvider.capture()` → fm `capture` with
  `capture_mode="candidate"` (heuristic admission, no LLM), then — when the
  turn was admitted as a candidate — LLM graph enrichment on the task
  endpoint (entities/edges/cues → `graph_upsert`; same prompt, tag
  vocabulary, throttle policy, and forgiving wire schema as mimo's former
  child-side extractor). Extraction failures never touch the captured
  record. Throttle knobs: settings key `memory_graph`
  (`every_n_turns`, `min_interval_seconds`, `enabled`).
- The mimo child no longer captures turns; its `capture.ts` owns only the
  memory session-scope lifecycle.
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
*holds*, never the contents — the model pulls details through the recall
tool its lane actually has (below) when a topic matters.

- Source: fm `digest` tool (read-only, direct queries, p50 well under 1ms).
  Pinned entries are enriched (id, full content, source_type, explicit
  pinned flag) so the trust split below can classify them.
- Renderer + sentinels: `src/memory_digest.py` (`render_split`,
  `DIGEST_SENTINEL = "[Memory Index]"`,
  `TRUST_SENTINEL = "[Endorsed Memory Guidance]"`).
- Odysseus seam: `chat_processor.build_context_preface` injects the split;
  it **replaced** the old top-3 auto-recall preface. 250ms timeout;
  failure degrades to no card.
- Bridge seam: `ACPBridge._maybe_inject_digest` prepends the card to mimo
  turns that don't already carry the sentinel (Odysseus-prefaced turns do),
  covering resume flows and future mimo-first paths; the trusted block
  rides `envelope.system_prompt` to true system tier and the demoted
  in-message copy is skipped at part building.

## Trust tiers (memory-trust metaplan, 2026-07-17)

`src/memory_trust.trusted(record, prefs)` — one classifier, both hosts:

- **Hand-authored** (`source_type=human`) and **explicitly pinned**
  records are ALWAYS trusted (a pin is an endorsement).
- Everything auto-captured is trusted only when the per-user MASTER
  toggle (`memory_trust_auto`, default off) AND that kind's switch
  (`memory_trust_auto_kinds`; defaults: fact/episodic/fabric/wiki on,
  instruction/persona off) are both on. `raw`/unknown kinds never.
- The classifier keys on record fields, never digest-array membership —
  fm auto-includes every persona-kind record in the digest's pinned
  array, and that membership is NOT an endorsement.

Trusted records render in an **endorsed guidance block** directly below
the persona (real force, system role; behavior kinds whole, capped;
knowledge kinds headline-only). Everything else stays inside the
`untrusted_context_message` guard wrapper — the injection firewall is
unchanged for untrusted content, and a memory never appears on both
sides of the split. Presentation mirror:
`static/js/util/memoryTrust.js` (parity-tested against the Python
classifier).

## Pull recall (T8: pitch first, pull by choice)

- Direct chat mode: the HTTP leg runs the shared agent loop restricted
  to exactly `recall_memory` (read-only search / `id:` fetch, 2 calls,
  3 rounds). Agent mode carries `recall_memory` in ALWAYS_AVAILABLE
  (manage_memory keeps the write actions). Pulled results inherit the
  trust split: endorsed plain, the rest guard-wrapped.
- Mimo: the chat agent hard-allows its native read-only `memory` tool
  (everything else stays denied); agent lanes keep the full memory
  manual. The digest tail names the tool per lane.

## Open questions (known-unknowns metaplan, 2026-07-17)

`kind=unknown` is a question the user wants answered ("user's name?"),
question-form even as a fragment.

- **Human-minted only (U2)**: Brain add or a directed instruction
  (`manage_memory` add with category `unknown`/`question`). The capture
  pipeline hard-rejects the kind outside manual admission
  (`unknown_kind_requires_manual`), so every unknown is
  `source_type=human` → always trusted, no new toggle. Content
  normalizes to one trailing `?` on save and update.
- **Surfacing (U6)**: the digest carries `open_questions` (scoped, cap
  5); the renderer places them INSIDE the endorsed guidance block with
  weave-in framing — bring one up when the conversation is already
  nearby, never interrogate. Human-only entries render (K5
  defense-in-depth). They recall like any record; `recall_memory`
  annotates them `[open question]`.
- **Promotion (U4/U5)**: groom op `promote_unknowns` — the same
  question open in >= 3 distinct workspaces merges into one global
  record (earliest copy canonical, idempotent), workspace copies
  archive with `{merged_into}`. Same-question matching is a
  deterministic overlap coefficient over stopword-free, lightly
  stemmed tokens, one conservative threshold (0.6,
  `fm-core/src/curate/questions.rs`).
- **Resolution (U7)**: three doors, all landing on the same archive
  surface (`archived=1` + `{resolved_by, resolved_at}` — never a plain
  delete): the Brain card's ✓ Resolve, the directed `manage_memory`
  resolve action, and PASSIVE resolution — every admission (candidate
  accept, capture direct admission, authored ingest) fuzzy-checks the
  new content against open questions in scope and closes what it
  answers. The hook lives at admission sites only, never in
  `upsert_curated`, so decay/dedup/reflect can never close a question
  (regression-pinned).

## Brain surfaces

- `/api/memory` list/get carry the full record (kind, source_type,
  scores, tags, scene, exemptions, workspace, last_accessed, …).
- `GET /api/memory/graph` — owner-scoped `graph_walk` passthrough
  (`overview` seeds the canvas; cues/tags/expand/fetch/trace for
  exploration). Canvas: `static/js/util/memoryGraph.js` (self-contained
  force layout, no CDN).
- `GET /api/memory/digest-preview` — the byte-identical injected blocks
  (raw digest + trusted/untrusted split with the caller's own prefs).
- Audit trail: `.robonotes/memory-trust-brain-audit-2026-07-17.md`.

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
- `tests/test_memory_trust_injection.py` — classifier matrix, T6
  placement, injection-firewall regression, cross-host split parity.
- `tests/test_recall_memory_pull.py` — pull tool read-only surface,
  trust-tiered results, per-lane registration and tail wording.
- `tests/test_memory_wire_enrichment.py` — full record on the wire,
  graph + digest-preview endpoints (live fm round-trips included).
- `tests/test_memory_brain_ui_js.py`, `tests/test_memory_graph_canvas_js.py`
  — JS↔Python classifier parity, chip semantics, canvas layout.
- `tests/test_authored_ingest_tool.py` + bun `test/memory/authored-ingest.test.ts`
  — the file projection lifecycle.
- fm engine: `cargo test` in `mcp_servers/frankenmemory`.
