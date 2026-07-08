# Option A — Phase 5: fm-mcp lifecycle (always loads)

**Goal:** fm-mcp is always available — detect if missing, build it, run it. Per agent (Phase 4): each agent's process spawns its own fm-mcp with its db under the agent folder → memory isolated per agent, and **stdio is sufficient** (one client per fm-mcp within an agent).

## Steps
1. **Detect + auto-build.** At thesius startup: if the `FM_MCP_COMMAND` binary is missing, `cargo build --release` from `mcp_servers/frankenmemory` and point `FM_MCP_COMMAND` at `target/release/fm-mcp`. A small builder, like the mimo supervisor.
2. **Per-agent db path.** Ensure each fm-mcp opens the agent's db under `~/entities/[agent]/` (set fm-mcp's db-path env/arg). Guarantees no cross-agent bleed + no cross-process write contention (one writer per agent).
3. **Resolve the Phase-2 fragmentation finding.** If Phase 2 showed thesius-provider / mimo-capture / bridged-tool fm-mcps were split → converge them on the agent's single db path. (If a single in-process shared instance is ever wanted, that needs a socket transport — deferred.)
4. **Add `FM_DB_PATH` env-var to fm-mcp.** `fm-core/src/config.rs:62-74` (the `Default` impl for `FmConfig`) hardcodes relative `db_path: "frankenmemory.db"`. Change to read `FM_DB_PATH` env var, falling back to `"frankenmemory.db"` if unset. One-line change in `Default`, no struct changes, no CLI flag needed. All 3 callers (`main.rs`, `integration.rs`, `native.rs`) get the override automatically. Zero test impact (tests use in-memory store).

## Exit criteria
- fm-mcp missing → auto-built + run.
- Within an agent, all fm-mcp clients see **one** db (capture visible to recall).
- Across agents, dbs isolated.

## Watch
- fm-mcp is stdio-only today (rmcp). A socket/daemon (one shared instance across processes) is the only thing needing a Rust transport addition — **deferred** unless per-agent isolation proves insufficient.
- Concurrent SQLite only matters if multiple *processes* share one db; per-agent isolation sidesteps it.
- This phase is mostly a *consequence* of Phase 4 + a small builder; keep it light.
