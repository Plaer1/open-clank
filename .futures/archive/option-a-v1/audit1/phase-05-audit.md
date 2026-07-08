## Phase 5 Audit

### Claims verified

| Claim | Verdict |
|-------|---------|
| fm-mcp is a Rust+SQLite engine, `cargo test` passes | **TRUE.** 40/40 tests pass (33 unit + 7 integration). Uses `SqliteStore` via `fm-core`. |
| fm-mcp uses stdio transport (rmcp) | **TRUE.** `main.rs:316` — `let transport = stdio()` via rmcp. |
| fm-mcp is spawned by `FrankenmemoryProvider` over MCP stdio | **TRUE.** `frankenmemory_provider.py:47-52` — `StdioServerParameters` + `stdio_client`. |
| mimo `capture.ts` spawns fm-mcp for captures | **TRUE.** Via `getSharedMcpClient()` in `mcp-client.ts`. Also `compaction-capture.ts` and `frankenmemory.ts` reuse that singleton. |
| Vanilla odysseus does NOT have FrankenmemoryProvider | **TRUE.** `.references/odysseus/src/memory_provider.py` contains only the ABC + `NativeMemoryProvider` + `MemoryProviderRegistry`. No fm-mcp. |
| `memory_provider.py` defines the abstract base | **TRUE.** ABC with `remember`, `recall`, `list_memories`, `delete`. |
| `.env.example` does NOT include `FM_MCP_COMMAND` | **TRUE.** The env example has no openthesius-specific variables. |
| `mimo_supervisor.py` has a comparable supervisor pattern | **TRUE.** It spawns `bin/mimo acp`, monitors health, restarts with backoff. Could serve as a template for fm-mcp auto-build. |
| `memory.py` MemoryManager is a JSON-file manager, NOT fm-mcp | **TRUE.** Old Odysseus native memory. Unrelated to frankenmemory. |

### Claims wrong / missing

| Claim | Reality |
|-------|---------|
| **"Detect + auto-build: if fm-mcp missing, `cargo build --release`"** | **MISSING.** No code anywhere does this. `FrankenmemoryProvider` fails silently if `FM_MCP_COMMAND` points to nothing. `mimo_supervisor.py` has the pattern (health monitor + restart backoff) but does NOT build fm-mcp — it only builds `bin/mimo`. |
| **"Per-agent db path under `~/entities/[agent]/`"** | **MISSING.** `fm-core/config.rs:65` hardcodes `db_path: "frankenmemory.db"` — a **relative** path. No env var override, no CLI arg, no per-agent parameterization. `main.rs` uses `FmConfig::default()` unconditionally. |
| **"All fm-mcp clients see one db within an agent"** | **FALSE.** See fragmentation analysis. |
| **"Resolve Phase-2 fragmentation"** | **NOT ATTEMPTED.** Multiple spawns still create separate processes with potentially separate DBs. |

### Fragmentation analysis

**Four** distinct places may independently spawn fm-mcp processes:

1. **frankenmemory_provider.py** (thesius recall/remember)
   - Entry: `FrankenmemoryProvider.__init__` → `_call_tool` lazily calls `initialize()` → `StdioServerParameters(command, args=[], env)` + `stdio_client`
   - No `FM_WORKSPACE_ID` env passed
   - Used by: `app_initializer.py` (startup), `memory_server.py` (standalone MCP server)

2. **acp_bridge.py `frankenmemory_mcp_descriptor()`** (bridged tool for agent)
   - Sends `{name:"frankenmemory", command, args:[], env:[{FM_WORKSPACE_ID}]}` to mimo via `ACPClient.new_session(mcp_servers=[...])`
   - Mimo spawns this as a **per-session** MCP server for the agent
   - Purpose: agent-visible tools (`frankenmemory:recall`, `frankenmemory:capture`, etc.)

3. **mcp-client.ts `getSharedMcpClient()`** (mimo-side services)
   - Singleton `StdioClientTransport` shared by `capture.ts`, `compaction-capture.ts`, `frankenmemory.ts`
   - Reads `FM_MCP_COMMAND` from env, passes `FM_WORKSPACE_ID` if set
   - Spawned ONCE per mimo process

4. **memory_server.py** (standalone Odysseus MCP server)
   - Creates its own `FrankenmemoryProvider` if `MEMORY_PROVIDER=frankenmemory`
   - Independent from the main thesius instance

**Do they share a db path? NO.**

`FmConfig::default()` → `db_path: "frankenmemory.db"` is a **relative** path. Each fm-mcp process writes `frankenmemory.db` in its own **CWD**. The CWD depends on who launched it:

- **thesius** (frankenmemory_provider.py): thesius CWD (e.g., repo root `/home/e/sauce/ai/openclanker/`)
- **mimo** (mcp-client.ts and bridged descriptor): mimo CWD (e.g., `/home/e/open-clank` or the workspace directory)
- **memory_server.py**: same as thesius but potentially different lifecycle

Result: **at runtime, there can be 2–4 independent `frankenmemory.db` files scattered across the filesystem.** Captures from mimo go to one db, recall from thesius reads from another. Phase 5 exit criterion "capture visible to recall" is **violated**.

Additionally: the `workspace_id` parameter is a **logical filter** (SQL WHERE clause), not a physical db selector. All workspace data lives in the same db file. So even "cross-agent db isolation" relies entirely on correct `workspace_id` filtering — no physical separation.

### Permission UX gap

**fm-mcp bypasses mimo's permission system entirely.** Evidence:

- `workspace-trust.ts`: controls trust level per directory. `external_directory` permission gate applied to mimo's file tools (`read.ts`, `write.ts`, `edit.ts`, `lsp.ts`, `bash.ts`).
- fm-mcp communicates via **stdio** (rmcp Rust transport, `StdioClientTransport` in TS, `stdio_client` in Python). It is NOT a mimo file tool.
- fm-mcp's SQLite database access goes through **direct file I/O** inside the fm-mcp process (via `SqliteStore`), not through mimo's tool layer.
- Therefore: **fm-mcp can read/write `frankenmemory.db` anywhere on the filesystem without triggering mimo's permission prompt.**

This is **correct behavior** — an external MCP server manages its own resources. BUT the plan's implicit assumption ("db under `~/entities/[agent]/` and that path in the trust list") conflates two mechanisms. The db path never interacts with `external_directory` or `workspace-trust`. The permission system only gates mimo's own file operations.

**If the plan intends to constrain fm-mcp's db to `~/entities/[agent]/` for organizational reasons, that's fine — but it has nothing to do with the permission gate.** The trust/permission system is irrelevant to fm-mcp.

### Recommendations

1. **Converge all fm-mcp clients on ONE db path.** Make `FmConfig::default()` read `FM_DB_PATH` env var. Set `FM_DB_PATH` to `~/entities/[agent]/fm-memory.db` in the thesius supervisor (Phase 4). Both thesius and mimo inherit the same env, guaranteeing a single db.

2. **Make thesius the sole fm-mcp spawner.** Currently there are three spawn paths for fm-mcp. Pick one:
   - **Recommended**: thesius spawns one fm-mcp as a supervised child. Mimo `mcp-client.ts` and the bridged descriptor point to the SAME process via `FM_MCP_COMMAND`. Use a Unix socket or named pipe for transport if multiple stdio clients conflict — OR just have mimo services call through the ACP bridge's frankenmemory tool, eliminating `mcp-client.ts` entirely.
   - **Minimum fix**: ensure all spawners use the same `FM_DB_PATH` env so the relative path doesn't matter.

3. **Add auto-build.** Clone the `mimo_supervisor` pattern: before spawning fm-mcp, check if the binary exists at `target/release/fm-mcp`. If not, run `cargo build --release` from `mcp_servers/frankenmemory`. Gate behind a config flag (`FM_AUTO_BUILD=1`) for production safety.

4. **Remove `memory_server.py`'s FrankenmemoryProvider.** It imports and creates a separate fm-mcp instance. Either wire through the shared provider or drop the frankenmemory path in this server — it's duplicating the main thesius provider.

5. **Clarify permission docs.** Explicitly document that fm-mcp DB access bypasses `external_directory`/`workspace-trust`. This is by design, not a bug. The permission gate is for mimo tools, not for external MCP server I/O.

### Summary verdict

**Phase 5 is NOT implemented.** The plan's three steps (auto-build, per-agent db path, fragmentation convergence) are all absent from the codebase. fm-mcp exists and works, but it uses a relative `frankenmemory.db` — capture and recall likely hit different databases at runtime. This is a **blocker** for the boot test (Phase 2).
