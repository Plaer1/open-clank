# MEATBAGS.md — Known issues and observations

## Status: 2026-06-27

### Resolved
- mimo doesn't un-delegate sometimes (bad completion signal from sub agents) — investigated in ses_0f53d15b5ffe, runLoop at prompt.ts:1628-3855 identified as the critical code path. Root cause: preStop/postStop ReAct loops (MAX_PRE_REACT=3, MAX_POST_REACT=3) in actor/spawn.ts can force re-entry. TaskGate.decide may also force re-entry on incomplete tasks. Not yet fixed but diagnosed.

### Active
- chronology and workspace highest signal for relevance of auto-insert memory — addressed by workspace-aware memory design (Phase 4a, workspace tag on records, recall priority boost)
- rest api retrieval of memes — not yet implemented (Phase 6?)

### Config requirements for deployment
- `dream.auto: false` — prevents double-consolidation with engine's groom("reflect")
- `distill.auto: false` — same
- `memory.provider: "frankenmemory"` — selects frankenmemory over native FTS
- `MEMORY_VECTOR_ENABLED=0` — disables Chroma vector path
- `FM_MCP_COMMAND` — env var to fm-mcp binary path if not in PATH
