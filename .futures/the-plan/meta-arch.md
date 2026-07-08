# open-clank — Meta Architecture

## What this is

Odysseus is a self-hosted AI workspace — web UI for chat, agents, email, documents, calendar, research, image generation, model serving, and more. It's a Python/FastAPI app with its own database, auth, settings, and plugin system.

MiMo Code (formerly OpenCode) is an AI coding agent — a TypeScript/Effect TS runtime that manages sessions, tools, subagents, memory, MCP servers, and LLM provider abstraction. It speaks ACP (Agent Client Protocol) over stdio.

**open-clank is Odysseus with its native agent loop replaced by MiMo Code as the agent brain.** Odysseus provides the web UI, data layer, and application features. MiMo Code provides the agent runtime, tool execution, session management, and memory system. They connect over ACP — Odysseus spawns `mimo acp` as a child process and communicates via JSON-RPC over stdin/stdout.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Odysseus (Python/FastAPI)                       │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ Web UI   │ │ Database │ │ App features     │ │
│  │ (chat,   │ │ (SQLite) │ │ (email, docs,    │ │
│  │  SSE)    │ │          │ │  calendar, etc.) │ │
│  └────┬─────┘ └──────────┘ └──────────────────┘ │
│       │                                          │
│  ┌────┴─────────────────────────────────────┐    │
│  │  src/openclank/ (integration layer)      │    │
│  │  ┌──────────────┐ ┌───────────────────┐  │    │
│  │  │mimo_supervisor│ │   acp_bridge      │  │    │
│  │  │(spawn/crash/  │ │(SSE translation,  │  │    │
│  │  │ restart)      │ │ permission, MCP   │  │    │
│  │  └──────┬───────┘ │ descriptors)      │  │    │
│  │         │         └────────┬──────────┘  │    │
│  │  ┌──────┴─────────────────┴──────────┐   │    │
│  │  │        acp_client                 │   │    │
│  │  │   (JSON-RPC 2.0 over ndJSON)      │   │    │
│  │  └──────────────┬────────────────────┘   │    │
│  └─────────────────┼────────────────────────┘    │
│                    │ stdio (stdin/stdout)         │
└────────────────────┼────────────────────────────-┘
                     │
┌────────────────────┼────────────────────────────-┐
│  MiMo Code (TypeScript/Effect TS)                 │
│  ┌─────────────────┴────────────────────┐        │
│  │  ACP Agent (acp/agent.ts)            │        │
│  │  ┌──────────┐ ┌──────────────────┐   │        │
│  │  │ Sessions │ │ Tools (bash,     │   │        │
│  │  │ (SQLite) │ │ read, write,     │   │        │
│  │  │          │ │ edit, glob, etc.)│   │        │
│  │  └──────────┘ └──────────────────┘   │        │
│  │  ┌──────────────────────────────┐    │        │
│  │  │ Memory (FTS5 + checkpoint)   │    │        │
│  │  └──────────────────────────────┘    │        │
│  └─────────────────────────────────────┘        │
│                                                  │
│  MCP Servers (attached per session):             │
│  ┌──────────────┐ ┌──────────────────────┐      │
│  │ lifetools     │ │ frankenmemory (fm-mcp)│      │
│  │ (Odysseus     │ │ (Rust+SQLite,         │      │
│  │  tools via    │ │  cross-chat memory)   │      │
│  │  MCP bridge)  │ │                       │      │
│  └──────────────┘ └──────────────────────┘      │
└──────────────────────────────────────────────────-┘
```

## The glue layer: `src/openclank/`

Six files that make Odysseus and MiMo Code work together:

- **`acp_client.py`** — Hand-rolled JSON-RPC 2.0 client speaking ndJSON over stdio to the mimo child process. Handles requests, responses, notifications, and callback dispatch.
- **`acp_bridge.py`** — Translates mimo's `session/update` notifications into Odysseus SSE strings. Manages per-turn state, model matching, pre-turn memory recall, and MCP server descriptors (lifetools + frankenmemory). Contains the `PermissionHandler` for auto-approving safe directory access.
- **`mimo_supervisor.py`** — Spawns `bin/mimo acp` as a child process. Handles crash detection, bounded restart backoff, and DB-driven session reconciliation. Injects config via `MIMOCODE_CONFIG_CONTENT` (skills paths + memory provider).
- **`thesius_identity.py`** — Agent identity system. Reads roster entries, composes system prompts from bootstrap files (AGENTS.md, SOUL.md, etc.), writes mimo agent config files with permission rules. Checksum-gated sync.
- **`lifetools_server.py`** — MCP server that bridges Odysseus's tools (documents, email, calendar, etc.) to mimo as `lifetools:*` tools.
- **`fmmcp_builder.py`** — Auto-builds the frankenmemory Rust binary at startup if missing.

## Memory system

Two layers, deliberately:

| Layer | Engine | Scope | Purpose |
|-------|--------|-------|---------|
| mimo native | SQLite FTS5 over markdown files | Per-process (shared across sessions) | Agent continuity — checkpoints, notes, task progress |
| frankenmemory | Rust+SQLite via MCP stdio (fm-mcp) | Shared DB, workspace-filtered | Cross-chat knowledge — user facts, preferences, conversation captures |

Mimo's `capture.ts` sends user/assistant text to frankenmemory after each turn (when `memory.provider: "frankenmemory"` is set). Mimo's `checkpoint.md` and `notes.md` stay session-scoped for the agent's own reasoning context. Frankenmemory's `recall` pulls cross-chat knowledge into the current turn via pre-turn injection in `acp_bridge.py`.

All fm-mcp spawners converge on one DB via `FM_DB_PATH` env var. Odysseus's native memory provider is disabled — frankenmemory is the sole user-facing memory system.

## Agent identity

Each agent has a home directory (`~/entities/[agent]/`) with bootstrap files: AGENTS.md, SOUL.md, IDENTITY.md, TOOLS.md, USER.md. The identity system reads these, composes a system prompt, computes permission rules (identity gate + frontmatter), and writes mimo agent config files. Checksum-gated sync means agent files only rewrite when the persona actually changes.

## Packages

`packages/mimo-code/` — The vendored MiMo Code fork. Modifications are minimal (5 files): `config.ts` (memory.provider field), `capture.ts`, `compaction-capture.ts`, `frankenmemory.ts`, `mcp-client.ts`. The rest is upstream drift between fork snapshots.

## Current state

**Working:**
- Supervisor spawns mimo child, ACP handshake completes
- Safe-dir auto-approve for `external_directory` permissions
- fm-mcp binary built, FM_DB_PATH reads env var
- `memory.provider: "frankenmemory"` injected into mimo config
- Odysseus native memory disabled, frankenmemory active
- Session ownership: mimo `ses_…` IDs are canonical, DB-driven reconciliation

**Not yet tested end-to-end:**
- Memory round-trip (capture via mimo → recall via frankenmemory) — P2 gate
- Real chat flow through the bridge with tool execution
- Non-safe-dir permission rejection (graceful degradation)

**Known limitations:**
- Terminal callbacks stubbed (mimo's native bash tool works independently)
- No UI for non-safe-dir permission requests (fail-safe reject)
- `.env.example` missing openthesius vars

## Communication topology (canonical)

```
Browser ←→ Odysseus:        HTTP/S + SSE — KEEP (remote access required)
Odysseus  ←→ mimo:          ACP over stdio — KEEP (child process + pipes, already optimal)
Odysseus  ←→ frankenmemory: PyO3 in-process — NEW (eliminates subprocess + JSON + MCP)
mimo      ←→ frankenmemory: MCP over stdio — KEEP (TypeScript can't load .so)
```

**Design principle:** local-only operation should use the most direct path possible. Odysseus loads fm-core as a native Python extension (`.so` via PyO3) — zero IPC, zero serialization. Mimo still uses the MCP subprocess path because it's TypeScript and can't load native Rust. Both hit the same SQLite DB via `FM_DB_PATH`.

The HTTP/S surface is preserved ONLY for browser access to Odysseus. Internal component communication (Odysseus ↔ mimo, Odysseus ↔ frankenmemory) is always in-process or pipe-based — never network.
