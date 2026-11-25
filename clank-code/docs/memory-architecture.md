# Memory architecture

This is an implementation reference, not an installation or recovery guide.
For daily use read the shared handbook through **Settings → Help**; for
recovery read [backup coverage](backup-restore.md).

Memery (Rust + SQLite FTS5) is the default intended memory/RAG
bank shared by Open Clank and its MiMo engine. Native provider compatibility
selection remains in source; the retired Chroma vector path is not an optional
live backend. The architecture below describes the Memery path and
retains implementation/historical names where they identify actual interfaces.

## Account defaults

Memery starts in Automatic mode. Explicit saves can enter the account’s memory immediately. Normal chats can recall up to three relevant curated records within a 1,200-character budget. Manual review stages new saves for approval. Off disables capture and recall. Incognito, per-turn privacy choices, and an administrator’s explicit permission denial still apply.

Inferred material enters the existing candidate workflow. Background model jobs need a configured assignment and an accepted scheduling policy. Changes to standing guidance, personality and Hexes have their own review controls.

## The engine

Open Clank runs the Rust Memery worker from `native/memery` over a private stdio protocol. The host authenticates the account, binds the workspace, checks permissions and owns worker lifecycle. MiMo calls back into the host for recall. A separate MCP server or model is unnecessary. Rust persists records, evidence, candidates, review receipts and retrieval indexes in SQLite.

Conversational records use the account’s canonical chat workspace from `src/memory_scope.py::chat_workspace()`. Agent tools receive an authenticated scope. Repository indexes can also be workspace-specific.

## Saving a memory

In a regular chat, type `/remember <text>` or `/memory add <text>`. The composer sends the text directly to the same authenticated Add operation as the Memery applet. Each submission has a request key so a retry checks the original outcome. In Automatic mode a saved fact is eligible for later recall. In Manual mode it waits in Candidates. This path uses no model.

On guarded stores, ordinary conversation alone does not create a curated fact on every turn. Inferred material follows the source, permission and review rules for its admission path. Completed-turn background extraction also needs an accepted scheduling policy, eligible source evidence and a configured model assignment. Temporary and Compare chats do not participate in ordinary turn capture.

Older provider configurations used `run_post_response_tasks` and `capture_turn_and_enrich` for eligible turns. That compatibility path is separate from guarded-store admission.

## Recall and standing guidance

The managed host can add a bounded selection of relevant curated records to an eligible turn before the model responds. An authorized Agent can also request deeper scoped recall with `recall_memory`. Privacy settings and account permissions apply to both paths. Recalled facts are context. Changes to standing guidance and personality follow their own review paths.

Providers that supply a digest can also render a short memory index using `src/memory_digest.py`. Its compatibility trust classifier separates endorsed entries from ordinary context. The digest is distinct from guarded curated recall and may be absent on a turn.

### Compatibility reference

The legacy capture path stores eligible turn evidence, can propose a candidate without a model, and may enrich a candidate with entities, edges and retrieval cues on its configured task endpoint. The `memory_graph` settings control the enrichment cadence. MiMo's `capture.ts` manages session scope. Its authored-file reconciler uses section content hashes to project eligible `MEMORY.md` files when the provider supports authored ingest.

The compatibility digest splits `[Memory Index]` context from `[Endorsed Memory Guidance]` using `src/memory_digest.py` and `src/memory_trust.py`. The classifier checks the record's source and explicit pin. Listing a persona entry among digest headlines does not itself grant endorsement. The `memory_trust_auto` preference and per-kind choices govern trust for auto-captured entries on this path.

A human can open a question with the `unknown` kind. Compatible digest providers can surface up to five open questions. The legacy grooming operation `promote_unknowns` matches recurring questions across workspaces, and resolution archives the record while keeping resolution metadata. Guarded review operations use the current record revision and original request identity. See `routes/memory/memory_routes.py` for the accepted request shapes.

## Brain surfaces

- `/api/memory` list/get carry the full record (kind, source_type,
  scores, tags, scene, exemptions, workspace, last_accessed, …).
- `GET /api/memory/graph` — owner-scoped `graph_walk` passthrough
  (`overview` seeds the canvas; cues/tags/expand/fetch/trace for
  exploration). Canvas: `static/js/util/memoryGraph.js` (self-contained
  force layout, no CDN).
- `GET /api/memory/digest-preview` — the byte-identical injected blocks
  (raw digest + trusted/untrusted split with the caller's own prefs).
- The July 17, 2026 memory audit is historical private operator evidence.

## Provider selection and retired Chroma

`src/app_initializer.py` defaults `MEMORY_PROVIDER` to `memery`,
constructing `MemeryProvider` and a disabled native provider. Its remaining
compatibility branch still constructs `NativeMemoryProvider` for an alternate
selection. That code has not been removed, and these docs do not promise its
removal or recommend a second deployment authority.

The initializer sets the historical `memory_vector` slot to `None`; no
environment toggle restores Chroma as a second live vector/RAG authority.
Preserve old Chroma payloads for reviewed explicit conversion under
private operator recovery material, rather than treating them as an
optional current service.

New installations use `memery.db` under the resolved data root. Existing installations reopen `frankenmemory.db` in place with its SQLite companions. Set `OPEN_CLANK_MEMERY_DB_PATH` to select an explicit store. If both names exist and point to separate files, the host requires an explicit selection. Memory recall/graph projection is not a complete
backup: app auth/keys, Copal documents/assets, History and host workspace files
have their own storage/recovery boundaries.

## Document index administration

The host tool `manage_rag` is available to administrators. It lists indexed sources and status, indexes or removes a directory, rebuilds embedding generations, and rolls back a generation. The host supplies the authenticated owner and the vetted workspace. In a managed Agent turn, directory indexing reads through Agent Files. Changes require approval in the current chat. Replaying a call returns its recorded outcome or reports that the original outcome is unknown.

## Scope plumbing

The host binds Memery operations to the authenticated owner and workspace. MiMo requests deeper recall through the host. MiMo runtimes are partitioned per owner (`MimoSupervisorPool`).

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
  trust-tiered results, Agent registration, and absence of generic Chat.
- `tests/test_memory_wire_enrichment.py` — full record on the wire,
  graph + digest-preview endpoints (live fm round-trips included).
- `tests/test_memory_brain_ui_js.py`, `tests/test_memory_graph_canvas_js.py`
  — JS↔Python classifier parity, chip semantics, canvas layout.
- `tests/test_authored_ingest_tool.py` + bun `test/memory/authored-ingest.test.ts`
  — the file projection lifecycle.
- fm engine: `cargo test` in `native/memery`.
