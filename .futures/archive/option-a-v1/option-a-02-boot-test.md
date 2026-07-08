# Option A — Phase 2: Boot test (single-instance, end-to-end) — THE GATE

**Goal:** prove a real chat flows `thesius → mimo → fm-mcp` and memory round-trips. Validates ~20 turns of never-run wiring. **Nothing in Phases 3-5 starts until this passes.**

## Steps
1. **Launch thesius** with `.env` loaded (`OPENTHESIUS_DRIVE=mimo`). On startup `app.py:1158 _startup_mimo` spawns `mimo acp` via the supervisor; `app.py:1170` logs failures.
2. **Supervisor up?** `app.state.mimo_supervisor` not None → glue imported + mimo child spawned + ACP handshake ([mimo_supervisor.py:131](../src/openclank/mimo_supervisor.py#L131)).
3. **Run a chat** → `chat_routes.py:1272` routes to `bridge.run_turn` (drive=mimo). Verify: streamed response, tool calls, stop reason, no 503.
4. **Memory round-trip:** after a turn, confirm a capture landed (mimo `capture.ts` → fm-mcp) and recall returns it (thesius `FrankenmemoryProvider.recall` and/or bridged `frankenmemory:recall`).
5. **cwd resolves:** the mimo session `directory` = `/home/e/open-clank` (global) or the per-request `workspace`; mimo resolved a project from it.

## Exit criteria
- A chat completes end-to-end via the bridge.
- Memory captured + recalled (round-trip).
- No supervisor / import crash.

## Watch — the likely failures (this is WHY we boot first)
- **Runtime-import crashes:** `lifetools_server.py` (`from mcp.types`, `from src.tool_schemas`) + `thesius_identity.py` (`from src.tool_security`) load deps at import → may fail at supervisor start. Fix imports / PYTHONPATH.
- **fm-mcp fragmentation:** thesius provider, mimo shared client, and the per-session bridged fm-mcp each stdio-spawn their **own** fm-mcp. Confirm they hit the **same db** — else memory is split and recall won't see captures. If split → triggers Phase 5's per-agent-db / shared-db decision.
- **fm-mcp db convergence (required for this gate):** `fm-core/src/config.rs:62-74` hardcodes relative `db_path: "frankenmemory.db"` — every spawn writes to CWD, which differs per process. **Before testing, add `FM_DB_PATH` env-var support** (one-line change in `FmConfig::default()`) and set it to an absolute path in `.env` so all spawners share one db. Without this, the memory round-trip exit criterion silently fails.
- **ACP handshake / version mismatch** between thesius `acp_client.py` and mimo `acp/agent.ts`.
- `fm-mcp` not found → memory silently no-ops (capture swallows errors).
