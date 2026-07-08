# openthesius — Option A plan (boot-first): OVERVIEW

**Decision: Option A.** Boot the current single-instance build **first** (prove the wiring end-to-end), **then** the session/agent rip-out wave.

**Why:** the never-run code is the *drive path* (`run_turn → bridge → mimo → fm-mcp`). The rip-out (Phase 3) doesn't touch it — it only changes the session-id source. So validate the unknown on the smallest stack; the rip-out then lands on a base we *know* runs. (`B` = build more blind, debug a big stack.)

Old `openthesius-phase-*` + `wave-activation` plans → `.archive/futures-superseded/`.

## Phases (each its own file)
1. [01 — build & activate](option-a-01-build-and-activate.md) — produce `bin/mimo` + `fm-mcp`, set config.
2. [02 — boot test](option-a-02-boot-test.md) — prove a real chat flows end-to-end + memory round-trips. **GATE: nothing in 3-5 starts until this passes.**
3. [03 — session-ownership rip-out](option-a-03-session-ownership-ripout.md) — thesius adopts mimo session ids; durable resume.
4. [04 — agent isolation](option-a-04-agent-isolation.md) — parameterize `~/entities/[agent]/`; per-agent process/mimo/fm-mcp.
5. [05 — fm-mcp lifecycle](option-a-05-fmmcp-lifecycle.md) — detect/build/run automation.

**1-2 = "boot it." 3-5 = the agent/session wave (only after 2 passes).**

## Already done (the wiring — built, NOT yet run)
- Glue relocated → `src/openclank/` (imports/paths fixed; py_compile + import OK).
- frankenmemory engine → `mcp_servers/frankenmemory/` (`cargo test` 40/40); `workspace_path` field end-to-end.
- mimo `capture.ts` reads workspace from the mimo session (`Session.get` → projectID/directory).
- Bridge cwd: global default (`OPENTHESIUS_GLOBAL_CWD=/home/e/open-clank`) + per-chat override; `chat_routes.py:1281` passes `cwd=workspace or None`.
- Build blockers fixed (`config.ts:337` → `Schema.Literals`; `compaction-capture.ts:70` → `parentID` guard) → **mimo typecheck clean**.
- `frankenmemory_provider` fail-safe (try/except + `FM_MCP_COMMAND`).
- `.env`: `OPENTHESIUS_DRIVE=mimo`, `OPENTHESIUS_SRC`, `OPENTHESIUS_GLOBAL_CWD`.
- PermissionHandler wired: `OPENTHESIUS_SAFE_DIRS` auto-approves `external_directory` for safe dirs. `acp_bridge.py:PermissionHandler`, wired through `mimo_supervisor.py` → `app.py`.
- fm-mcp `FM_DB_PATH` env var added (`fm-core/src/config.rs:62-74`). All spawners share one db when set.
- `FM_MCP_COMMAND` + `FM_DB_PATH` in `.env`.

## Phase 1 status: DONE
- `bin/mimo` built (0.0.0-phase-5-compaction, `--single`).
- `target/release/fm-mcp` built (with `FM_DB_PATH` support, 40/40 tests).
- `.env` complete (all 6 openthesius vars).

## Phase 3 status: DONE
- `open_session()` added to `acp_bridge.py` — mints mimo `ses_…` IDs.
- 14 session-creator sites across 8 files mint mimo IDs when `OPENTHESIUS_DRIVE=mimo`.
- `_session_map`, `_reverse_map`, `get_all_mapped_sessions` deleted.
- `_reconcile_sessions` now DB-driven (queries `ses_%` sessions from thesius DB).
- Optional `mimo_session_id` column on Session table.
- 11 files modified, all compile clean.

## Phase 4 status: DONE
- `THESIUS_AGENT_HOME` env var gates `ODYSSEUS_DATA_DIR`, `OPENTHESIUS_GLOBAL_CWD`, `MIMOCODE_HOME`, `FM_DB_PATH`.
- `MIMOCODE_HOME` passed to mimo child for per-agent DB isolation.
- Auth stays global (pinned outside agent home).
- `thesius_identity.py` paths overridable via `OPENCLANK_*` env vars.
- Agent home auto-included in safe dirs.
- 5 files modified, all compile clean.

## Phase 5 status: DONE
- `fmmcp_builder.py` — auto-builds fm-mcp at startup when `FM_AUTO_BUILD=1`.
- `FM_DB_PATH` explicitly passed in `frankenmemory_mcp_descriptor()`.
- Duplicate frankenmemory path removed from `memory_server.py`.
- All spawners converge on shared `FM_DB_PATH` via env inheritance.
- 5 files modified, all compile clean.

## Locked architecture (decisions — don't relitigate)
- **mimo owns sessions, no shim** — thesius adopts mimo session ids directly (Phase 3).
- **mimo self-manages cwd** — directory-keyed instances resolve project/worktree/projectID per dir, multiplexed in one process. No pool for workspace tagging.
- **chat = a mimo session** (not a process); one mimo multiplexes chats.
- **agent = isolated process + `~/entities/[agent]/` folder** — isolation is a *consequence* (Phase 4), not a built feature.
- **fm-mcp** = light (Rust+SQLite); one per agent, automatic; just ensure it loads (Phase 5). Daemon/socket deferred.
- **app blank by default**; user-agent onboarding = QOL, much later.
- **memory:** session-scoped recall CUT; workspace = global-first + opt-in tag + captured `workspace_path`; recall = soft boost (owner = only hard filter).
