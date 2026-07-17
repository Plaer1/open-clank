# Agent tool parity — one implementation per logical capability

Identity metaplan Slice 05A record (2026-07-16). Generated from the live
registries: `src/openclank/lifetools_server.py` (`_BRIDGED_TOOLS` /
`_ALL_EXCLUDED`), mimo's native tool registry, and the frankenmemory MCP
descriptor. Rule: on the ACP path each logical capability has exactly ONE
implementation; duplicates are excluded at the Lifetools bridge.

## Disposition table

| Logical capability | Implementation on ACP | Duplicate excluded |
|---|---|---|
| Shell / Python execution | MiMo native (`bash`) | Odysseus `bash`, `python` |
| File read/write/edit/list/glob/grep, workspace | MiMo native | Odysseus `read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep`, `get_workspace` |
| Web search / fetch | MiMo native (`websearch`, `webfetch`) | Odysseus `web_search`, `web_fetch` |
| Ask-the-user | MiMo native question tool (`_odysseus/question` ext → host UI) | Odysseus `ask_user` |
| Plan updates | MiMo plan flow (plan-file diff → `plan_update` SSE) | Odysseus `update_plan` |
| Conversational memory (recall/capture/search/graph) | frankenmemory MCP (both hosts, one bank); turn capture is Odysseus-owned post-119d3201 | mimo turn auto-capture (deleted); Odysseus native memory (retired) |
| Agent file memory (MEMORY.md, checkpoints) | MiMo native (agent lane only; chat profile gets no memory manual) | — |
| Application/life tools (54): calendar, notes, tasks, email, contacts, documents, sessions, research, images, model serving, settings, skills, MCP admin, webhooks, tokens, bg jobs, teacher, pipeline, UI control | Lifetools MCP bridge (owner/session-scoped env) | — (no mimo equivalent) |
| Third-party MCP servers | Admin-gated `odysseus_*` descriptors attached per session (non-incognito, admin only) | — |

Exclusion sets live in `src/tool_schemas.py`
(`OPENTHESIUS_BRIDGE_EXCLUDED_TOOLS`, 9 coding + web/ask/plan overlaps) and
`src/openclank/lifetools_server.py` (`_BRIDGE_EXTRA_EXCLUDED`).

## Lane policy (rulings R2/R16)

- Chat lane: the mimo `chat` profile — hard wildcard tool deny, no
  lifetools/fm/MCP schemas, question allowed. Odysseus-side context
  services (memory digest, RAG, attachments, preprocessing) are passive
  and remain available; they are not model-callable tools.
- Agent lane: mimo native + Lifetools + frankenmemory + admitted MCP,
  filtered per turn by the envelope tool policy (complete revision
  semantics — a chat turn's deny-all cannot leak forward).
- Chat→agent intent auto-escalation is upstream-vanilla behavior and
  KEPT (R16).

## Deliberate non-retirements (upstream parity)

- The native Python agent loop and direct-provider chat path STAY as the
  non-ACP transport. Capture, persona, digest, and policy are decided
  Odysseus-side and transport-blind, so both transports render the same
  product behavior (one-app rule). Retirement of the Python loop is a
  separate future decision, not part of this metaplan's closure.
- Scheduled tasks/check-ins run the native loop with crew tool
  allowlists; their voice is the synced default persona (R13/R15).
