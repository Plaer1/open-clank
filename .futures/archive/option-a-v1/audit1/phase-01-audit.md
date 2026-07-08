# Phase 1 Audit — build & activate

Audit date: 2026-06-29

## Claims verified

### 1. `script/build.ts` exists and produces `bin/mimo` (plan line ~7)

**VERIFIED** — but the binary has NOT been built.

`apps/mimo/packages/opencode/script/build.ts:1` exists, 333 lines. Line 247: `outfile: dist/${name}/bin/mimo`. Line 148-167: `--single` flag filters to current platform. Line 266-273: smoke test runs `--version` after build. Output path: `dist/mimocode-linux-x64/bin/mimo`.

BUT: `bin/` directory does not exist at repo root. The build command has not been run. The plan's claim that "mimo typecheck is clean" is a construction claim, not a verified fact at this point.

### 2. `mimo_supervisor.py:20` and harness service both expect `repo_root/bin/mimo`

**VERIFIED.**

`src/openclank/mimo_supervisor.py:20`:
```python
MIMO_BIN = REPO_ROOT / "bin" / "mimo"
```

`harness/openthesius-mimo.service:7`:
```
ExecStart=/home/e/sauce/ai/openclanker/bin/mimo acp --hostname 127.0.0.1
```

Paths match. However: the service file specifies `--hostname 127.0.0.1` (TCP mode), while the supervisor in `mimo_supervisor.py` spawns via stdio (ACP over stdin/stdout). These are two different launch modes. The service file is for standalone operation; the supervisor does NOT use it.

### 3. `.env` has core openthesius vars

**VERIFIED** — but incomplete. Confirmed by grep:
- `OPENTHESIUS_DRIVE=mimo`
- `OPENTHESIUS_SRC=/home/e/sauce/ai/openclanker/src`
- `OPENTHESIUS_GLOBAL_CWD=/home/e/open-clank`

**MISSING:** `FM_MCP_COMMAND` is NOT set. The default `"fm-mcp"` won't resolve on PATH. The binary exists at `mcp_servers/frankenmemory/target/release/fm-mcp` (6.4MB, built Jun 27) but nothing points to it.

### 4. `config.py` (env vars) — plan claim is misleading

**MISLEADING.** `src/config.py:1-210` defines `DataConfig`, `LLMConfig`, `SearchConfig`, `SecurityConfig`, `AppConfig` using pydantic-settings. It reads env vars with prefixes `DATA_`, `LLM_`, `SEARCH_`, `SECURITY_`. It does NOT define or read any `OPENTHESIUS_*` or `FM_MCP_COMMAND` vars. These are consumed ad-hoc via `os.environ.get()` scattered across 5+ files: `mimo_supervisor.py:139`, `acp_bridge.py:20`, `chat_routes.py:1272-1274`, `app_initializer.py:86`, `app.py:1157-1163`.

### 5. ACP handshake — `acp_bridge.py` and `acp_client.py`

**VERIFIED.**

`acp_client.py:42-60` — `initialize()` sends JSON-RPC handshake with `protocolVersion: 1`, `clientCapabilities: {fs: {readTextFile, writeTextFile}}`, `clientInfo: {name: "openthesius", version: "0.1.0"}`. Response carries `agentInfo`.

`acp_bridge.py:64-126` — `register_client_callbacks()` registers 6 callbacks: `fs/read_text_file`, `fs/write_text_file`, `session/request_permission`, `terminal/create`, `terminal/output`, `terminal/wait_for_exit`, `terminal/kill`, `terminal/release`. Terminal stubs all throw "not supported."

### 6. Modified mimo `script/build.ts` exists

**VERIFIED.** Identical structure to vanilla (both 333 lines). No meaningful diff. Neither version includes permission-related flags. No `MIMOCODE_PERMISSION` define in the build pipeline.

### 7. `config.ts:337` — `Schema.Literals(["native", "frankenmemory"])`

**VERIFIED** — this is a genuine modification.

`apps/mimo/packages/opencode/src/config/config.ts:337`:
```typescript
provider: Schema.optional(Schema.Literals(["native", "frankenmemory"]))
```
This is the `memory.provider` config key. **Does NOT exist in vanilla** (`.references/mimo-code/packages/opencode/src/config/config.ts` has no `provider` field under `memory` — confirmed via grep).

### 8. `compaction-capture.ts:70` — `parentID` guard

**VERIFIED.**

`apps/mimo/packages/opencode/src/memory/compaction-capture.ts:70`:
```typescript
parent_message_id: "parentID" in summary.info ? summary.info.parentID : undefined,
```
The `parentID` field is guarded with `in` check before access. The `capture` MCP tool call passes `source: "mimo"`, `category: "compaction"`, `kind: "episodic"`.

### 9. `fm-mcp` Cargo.toml exists, binary built

**VERIFIED.**

`mcp_servers/frankenmemory/Cargo.toml:1-3` — workspace with `fm-core` and `fm-mcp` crates. `crates/fm-mcp/Cargo.toml:7-8` — binary `fm-mcp` from `src/main.rs`. Binary exists at `target/release/fm-mcp` (6.4MB, built Jun 27).

### 10. Vanilla mimo comparison

**VERIFIED** — changes are surgical:
- `config.ts`: `memory.provider` field added (line 337)
- `compaction-capture.ts`: new file (91 lines) — hooks into `SessionCompaction.Event.Compacted`
- `build.ts`: identical to vanilla

### 11. Thesius openthesius integration points

**VERIFIED.** The `OPENTHESIUS_DRIVE=mimo` guard gates exist in:
- `chat_routes.py:1272` — routes to bridge instead of `stream_agent_loop()`
- `agent_loop.py:1968` — logs error if old loop invoked
- `teacher_escalation.py:569` — skips escalation
- `task_scheduler.py:1724` — skips agent loop
- `bg_monitor.py:36` — skips background drain
- `app.py:1157` — spawns `MimoSupervisor` on startup

---

## Claims wrong / missing

### WRONG: `.env.example` contains openthesius vars

The plan says "`.env.example` and `src/config.py` (env vars)". `.env.example` is an **unmodified odysseus template** — 199 lines of standard odysseus config (LLM_HOST, SEARXNG_INSTANCE, DATABASE_URL, GPU support, AUTH, etc.). It contains zero openthesius-specific variables. Anyone following the plan's `.env.example` as reference will not see `OPENTHESIUS_DRIVE`, `OPENTHESIUS_SRC`, `OPENTHESIUS_GLOBAL_CWD`, or `FM_MCP_COMMAND`.

### WRONG: `.env` is "complete" for phase 1

The `.env` lacks `FM_MCP_COMMAND`. Without it, `frankenmemory_provider.py:36` and `acp_bridge.py:20` fall back to `"fm-mcp"` which won't resolve on PATH. The fm-mcp binary exists but is unreachable. This is a boot-blocker for phase 2.

### MISSING: `bin/mimo` not built

Step 1 of the plan says to build and copy `bin/mimo`. Neither `bin/` directory nor `bin/mimo` binary exists. The `APP_MIMO` overlay in `apps/mimo/` contains modified source files ready to build, but the build script has not been executed. The plan's "Exit criteria" (`bin/mimo --version` works) is NOT met.

### WRONG: `config.py` handles openthesius env vars

The plan lists `src/config.py` as the env var file. It does not handle any openthesius var. The actual consumption is fragmented across 5+ files with `os.environ.get()`. No centralised config validation for openthesius vars exists — if `OPENTHESIUS_DRIVE` isn't set, the old agent loop runs by default with no warning.

### WRONG: Plan claims `from src.openclank…` resolves because "thesius runs with cwd = repo root"

The import works because `OPENTHESIUS_SRC` is injected into `sys.path` at `app.py:1163` and `chat_routes.py:1276` — not because cwd is repo root. The supervisor (`mimo_supervisor.py`) uses `Path(__file__).resolve().parents[2]` to find REPO_ROOT at line 19. These two mechanisms are independent. The plan's explanation is wrong even though the imports do work.

---

## Permission UX gap

**This is the most serious gap in the plan. Zero attention to permission forwarding.**

### How it works

The mimo permission system has two independent gates:

1. **Workspace trust** (`workspace-trust.ts:23-44`): Checks whether `cwd` is in `~/.mimocode/data/trusted-workspaces.json`. If untrusted, mimo prompts a TUI dialog. This is a one-time gate per workspace.

2. **External directory** (`external-directory.ts:19-55`): On EVERY file read/write outside the worktree bounds, mimo sends a `session/request_permission` JSON-RPC callback. The Thesius bridge's `PermissionHandler` (`acp_bridge.py:511-582`) receives this call, creates a `PermissionRequest`, and **blocks waiting for a human response** (300s timeout → reject).

### Why this breaks Thesius

Thesius is designed as an always-on daemon. Scheduled tasks, background memory consolidation, and automated processing all need filesystem access. But:

- `external_directory` permission fires for ANY file access outside the workspace (e.g., reading `/home/e/entities/`, writing to `/home/e/sauce/`, accessing `/home/e/.config/`, etc.)
- The `PermissionHandler` has **no pre-approve capability**. It always waits for human input.
- If no UI is connected when a permission prompt fires, the request times out and is **rejected** (fail-safe on timeout at `acp_bridge.py:501`)
- Memory writes to the mimo memory tree (`~/.mimocode/data/memory/`) have a special bypass in `external-directory.ts:37` — but all other external paths are gated
- Terminal callbacks are hard-stubbed to throw "not supported" — the agent cannot run shell commands at all

### What's missing from the plan (and code)

| Gap | Severity | Detail |
|-----|----------|--------|
| No safe-folder configuration | **CRITICAL** | Neither config.py, .env, nor the ACP bridge support a master list of pre-approved paths (e.g., `~/sauce/`, `~/entities/`, `~/open-clank/`) |
| No auto-approve for known agents | **CRITICAL** | The `PermissionHandler` has no path-based auto-approve logic. It unconditionally blocks. |
| No `MIMOCODE_PERMISSION` define in build | **HIGH** | The build script (`build.ts`) could define default permission rules at compile time (the `MIMOCODE_PERMISSION` env/flag that `config.ts:914` reads). None are set. |
| No `external_directory` default allowlist in the bridge | **HIGH** | The bridge could pre-seed the permission handler with a `{"external_directory": {"~/sauce/*": "allow", "~/entities/*": "allow"}}` map. It does not. |
| `bin/mimo` will be mint-fresh with no prior trust | **MEDIUM** | On first boot, `trusted-workspaces.json` is empty. Thesius's cwd (`/home/e/open-clank`) is "untrusted." Workspace trust prompt depends on TUI — in daemon mode (ACP), this blocks. |

### The interaction in practice

1. Thesius starts → spawns `bin/mimo acp` via stdio
2. Mimo initializes, checks workspace trust → workspace is `untrusted` → sends `session/request_permission` for workspace trust
3. PermissionHandler waits 300s for human → times out → rejects
4. **Mimo refuses to operate.** The agent loop gate never opens.

Even if workspace trust passes (pre-marked trusted), every subsequent `read`/`write`/`bash` outside `/home/e/open-clank` triggers a new `external_directory` prompt. For a user with files across `~/sauce/`, `~/entities/`, `~/.config/`, `~/Documents/` — this means **dozens of prompts per turn**. Thesius as "always-on personal assistant" is non-functional without an auto-approve layer.

### No plan phase addresses this

Phases 2-5 do not mention permission forwarding, batching, or auto-approve. The "locked architecture" section (overview line 27) lists 8 decisions — none mentions permissions. The permission system is treated as if it doesn't exist, when it is the single largest UX barrier between Thesius and actual use.

---

## Recommendations

### Immediate (block Phase 2 gate)

1. **Add `FM_MCP_COMMAND` to `.env`:**
   ```
   FM_MCP_COMMAND=/home/e/sauce/ai/openclanker/mcp_servers/frankenmemory/target/release/fm-mcp
   ```

2. **Build `bin/mimo` before Phase 2:**
   ```
   cd apps/mimo/packages/opencode && bun install && bun run script/build.ts --single
   cp dist/mimocode-linux-x64/bin/mimo ../../bin/mimo
   ```

3. **Update `.env.example` to include openthesius vars** (currently missing):
   ```
   # ── openthesius ──
   # OPENTHESIUS_DRIVE=mimo
   # OPENTHESIUS_SRC=/home/e/sauce/ai/openclanker/src
   # OPENTHESIUS_GLOBAL_CWD=/home/e/open-clank
   # FM_MCP_COMMAND=/home/e/sauce/ai/openclanker/mcp_servers/frankenmemory/target/release/fm-mcp
   ```

### Permission fixes (should be Phase 1b or grafted into Phase 2)

4. **Add a master safe-folder list to `mimo_supervisor.py`** (or a new env var `OPENTHESIUS_SAFE_DIRS`). The `PermissionHandler.handle()` in `acp_bridge.py:535` should check `external_directory` requests against this list before deferring to human input.

5. **Pre-mark workspace as trusted at supervisor startup.** Before first `new_session` call, call `markTrusted` equivalent via ACP (or write to `trusted-workspaces.json` directly). This avoids the first-boot workspace-trust deadlock.

6. **Add `MIMOCODE_PERMISSION` env propagation** in the supervisor. The build script doesn't need permission flags, but the runtime config should. Add to `mimo_supervisor.py`:
   ```python
   env["MIMOCODE_PERMISSION"] = json.dumps({
       "external_directory": {
           os.path.expanduser("~/sauce/*"): "allow",
           os.path.expanduser("~/entities/*"): "allow",
           os.path.expanduser("~/open-clank/*"): "allow",
       }
   })
   ```

7. **Fix the plan itself** — add a dedicated "Permission UX" section to Phase 1 or Phase 2, describing:
   - How workspace trust is bootstrapped for daemon mode
   - How `external_directory` requests are forwarded/auto-approved
   - What the master safe-folder configuration looks like
   - Whether the TUI permission dialog has a fallback when no terminal is attached

### Plan accuracy

8. Fix plan claim that `.env.example` has openthesius vars. It does not.
9. Fix plan claim that `config.py` handles openthesius env vars. It does not.
10. The plan says "`from src.openclank…` resolves because thesius runs with cwd = repo root" — this is false. Import resolution happens via `sys.path.insert(0, OPENTHESIUS_SRC)` at `app.py:1163` and `chat_routes.py:1276`. Correct the plan text.
