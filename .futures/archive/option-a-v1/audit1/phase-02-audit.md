## Phase 2 Audit

### Claims verified

Each claim from the plan file (`option-a-02-boot-test.md`) checked against actual code.

1. **`app.py:1158 _startup_mimo` spawns mimo via supervisor** — CONFIRMED.
   `app.py:1156-1172` guards on `OPENTHESIUS_DRIVE=mimo`, imports `MimoSupervisor`, calls `_sup.start()`, sets `app.state.mimo_supervisor`. Correctly structured as a non-blocking asyncio task.

2. **`app.py:1170` logs failures** — CONFIRMED.
   `app.py:1169-1171` catches `Exception`, logs at ERROR level with `exc_info=True`, sets `app.state.mimo_supervisor = None`. Recovery path (503 in chat_routes) works.

3. **Supervisor not None → glue imported + child spawned + ACP handshake** — CONFIRMED.
   The `MimoSupervisor` singleton is the single access point. `mimo_supervisor.py:131` performs the ACP handshake via `self._client.initialize()`. On failure it calls `_teardown_child()` and re-raises, which `app.py` catches and logs.

4. **`chat_routes.py:1272` routes to bridge when `OPENTHESIUS_DRIVE=mimo`** — CONFIRMED.
   `chat_routes.py:1271-1282`: checks env var, imports, gets `_mimo_sup.bridge`, calls `bridge.run_turn(session, messages, model=sess.model, cwd=workspace or None)`. Falls back to 503 SSE error stream if supervisor/bridge unavailable.

5. **cwd passed: global default + per-chat override** — CONFIRMED.
   `mimo_supervisor.py:139`: global default from `OPENTHESIUS_GLOBAL_CWD` env or `~/open-clank`.
   `chat_routes.py:1281`: `cwd=workspace or None`, which flows into `bridge.ensure_session(odysseus_session, cwd=cwd)` at `acp_bridge.py:246`.
   `acp_bridge.py:204`: `target_cwd = cwd or self._cwd` — correct fallback chain.

5b. **mimo session `directory` resolves from cwd** — CONFIRMED.
   `capture.ts:101-103`: `const sessionInfo = yield* sessions.get(sessionID)` then `const workspacePath = sessionInfo.directory; const workspaceId = sessionInfo.projectID ?? sessionInfo.directory`. The `session.get()` returns `Info.directory` from `session.ts:67` which is the column `directory` from the SessionTable — populated when `ACPSessionManager.create()` calls `sdk.session.create({ directory: cwd })`.

6. **`run_turn` method exists in `acp_bridge.py`** — CONFIRMED.
   `acp_bridge.py:234-352`: full implementation. Creates session, builds prompt parts, performs pre-turn recall, fires prompt, consumes notifications in a 1s-poll loop, processes agent_message_chunk / agent_thought_chunk / tool_call / tool_call_update, emits metrics SSE, handles cancellation. Drop-in compatible with `stream_agent_loop`.

7. **Pre-turn recall via `memory_provider.recall`** — CONFIRMED.
   `acp_bridge.py:258-271`: extracts last user text, calls `self._memory_provider.recall(user_text, owner=self._owner, top_k=5)`, formats hits with GT preamble as synthetic assistant audience parts. Swallows errors gracefully.

8. **ACP protocol version** — CONFIRMED.
   `acp_client.py:47`: `"protocolVersion": 1` in `initialize()`. Client name `"openthesius"`, version `"0.1.0"`. Wire format is JSON-RPC 2.0 over ndJSON.

9. **`capture.ts` reads workspace from session** — CONFIRMED.
   `capture.ts:101-103` as above. Additionally at line 103: `const workspaceId = sessionInfo.projectID ?? sessionInfo.directory` — uses projectID when available, falls back to directory.

10. **`FrankenmemoryProvider.recall` method** — CONFIRMED.
   `frankenmemory_provider.py:113-150`: calls fm-mcp `recall` tool with `query`, `top_k`, `tier="curated"`, `workspace_id`, optional `owner`. Returns `List[MemorySearchHit]`. Worker-provider comparison: this is the thesius-side provider (one fm-mcp process), NOT the bridged `frankenmemory` MCP server (another fm-mcp process). Same binary, separate instances — fragmentation risk.

11. **Vanilla mimo vs openclank mimo comparison** — IDENTICAL for permission/trust files.
   `workspace-trust.ts`: byte-identical between `.references/mimo-code/` and `apps/mimo/`.
   `external-directory.ts`: byte-identical between `.references/mimo-code/` and `apps/mimo/`.
   No openclank-specific permission bypasses or modifications exist in the mimo fork.

12. **Vanilla odysseus vs thesius comparison** — DIFF CONFIRMED.
   The only changes to `app.py` are: (a) memory_provider plumbing (lines 539-540, 610, 757), (b) mimo supervisor startup/shutdown blocks (lines 1156-1185). `chat_routes.py` adds `OPENTHESIUS_DRIVE=mimo` routing at lines 1271-1287. No other openthesius code in vanilla odysseus.

---

### Claims wrong / missing

Claims in the plan that don't match reality.

1. **"fm-mcp startup and db path are configurable" — WRONG.**
   `main.rs:307`: `let config = FmConfig::default()`. The struct `FmConfig` at `config.rs:5` has `db_path: "frankenmemory.db"` hardcoded in its `Default` impl. **No env var is read.** Not `FM_DB_PATH`, not `FM_DATA_DIR`, nothing. Every fm-mcp instance writes to `frankenmemory.db` in whatever directory it's launched from (likely the process cwd). If two fm-mcp instances start in different directories, they get separate databases — guaranteed split-brain. If they start in the same directory, they share one DB file but with zero isolation guarantees (two SQLite writers).

   The env var `FM_WORKSPACE_ID` passed by `frankenmemory_mcp_descriptor()` at `acp_bridge.py:57-61` is a tool-call parameter (tag on each capture/recall), NOT a config override. It doesn't control DB path, startup, or anything structural.

2. **Plan says nothing about permission gates** — MISSING ENTIRELY.
   Not one word about `workspace-trust` or `external_directory`. Not in Phase 2, not in the overview. The term "permission" appears nowhere in `option-a-02-boot-test.md`.

3. **Plan says "verify tool calls" — underspecified.**
   "Streamed response, tool calls, stop reason, no 503" — this tests text generation + tool announcements, NOT tool execution. A chat that never touches files would pass all exit criteria. A chat that reads a file outside the workspace would fail silently (tool error masked by the SSE stream).

4. **Plan says nothing about passing permission state from mimo back to thesius** — MISSING.
   The `PermissionHandler` class exists at `acp_bridge.py:511-582` with full UI-surface wiring (pending requests, `on_request` callback, `resolve` method). But it's **never instantiated or wired anywhere in thesius**. `app.py:1165` creates `MimoSupervisor(memory_provider=memory_provider)` with no `permission_handler` argument. `mimo_supervisor.py:60` defaults `permission_handler=None`. `register_client_callbacks` at `mimo_supervisor.py:126` passes `None`. When a `session/request_permission` arrives, the fail-safe at `acp_bridge.py:113-114` returns `{"outcome": {"outcome": "selected", "optionId": "reject"}}` — **every permission request is auto-rejected**.

---

### Permission UX gap

Two gates. Both matter for an always-on assistant. Neither is mentioned in the plan.

#### Gate 1: `workspace-trust.ts` — **PARTIALLY MITIGATED, accidental**

`checkTrust()` is only called from `cli/cmd/tui/thread.ts:205` — the TUI startup path. In ACP mode (`session/new` via `ACPSessionManager`), the workspace-trust prompt is **never triggered**. Session creation goes directly through `sdk.session.create()` at `acp/session.ts:21-28` which bypasses the TUI thread entirely.

- If the cwd is `/home/e/open-clank` (the default), it falls under the "dangerous" category (home directory match at `workspace-trust.ts:36-38`). In TUI mode this would show a scary prompt. In ACP mode: silently bypassed.
- This is **good for now** (no blocking prompt) but **fragile** — any future code path that introduces `checkTrust` into the `session/create` pipeline would break the ACP flow without warning. The plan should document this bypass as intentional.

#### Gate 2: `external_directory` permission — **BROKEN, material gap**

Every file-access tool in mimo (read, write, glob, grep, edit, lsp, change-directory) calls `assertExternalDirectoryEffect()` at:
- `read.ts:165`
- `glob.ts:49`
- `grep.ts:57`
- `lsp.ts:44`
- `change-directory.ts:75`
- `edit.ts`, `write.ts`, etc. via `assertWriteAllowed()` at `external-directory.ts:76-105`

The check at `external-directory.ts:30`:
```ts
if (Instance.containsPath(full, ins)) return
```
Files **inside** the instance's project directory pass without asking. Files **outside** trigger `ctx.ask({ permission: "external_directory", ... })`, which in ACP mode flows:

```
mimo tool → Permission.ask() → bus.publish(permission.asked)
→ ACP agent.handleEvent() → connection.requestPermission({...})
→ thesius acp_client → _request_permission callback
→ {"outcome": {"outcome": "selected", "optionId": "reject"}}
→ ACP agent → sdk.permission.reply({reply: "reject"})
→ tool fails with PermissionRejectedError
```

**All file access outside the workspace directory is silently blocked.** The agent receives a permission error, which it may retry, skip, or abort — but the file operation never happens.

For an always-on assistant working in `~/sauce/ai/`, `~/entities/`, `~/.config/`, etc., this is a hard failure. The assistant can only read/write within the project directory defined by the cwd (`/home/e/open-clank` by default).

The `bypassCwdCheck` flag at `read.ts:166` is only set in `session/prompt.ts:1501` for file-reference resolution (when the model references a `file://` URL from context). It's **not set in ACP mode** for normal tool execution.

**The plan never mentions this.** Phase 2's exit criteria ("a chat completes end-to-end") could pass trivially with a chat that never accesses files. A real agent chat that reads source code or writes files outside the workspace will fail.

---

### Recommendations

For the GATE (Phase 2) to be honest about proving end-to-end flow:

1. **Add a permission test to the exit criteria.** The chat test MUST include a tool call that reads or writes a file **outside** the project boundary. Verify it either (a) passes through PermissionHandler with human approval OR (b) is auto-allowed via a pre-seeded permission rule. Without this, the gate is testing text-only flow, not real agent behavior.

2. **Wire PermissionHandler into `app.py` startup.** Instantiate `PermissionHandler` from `acp_bridge.py`, pass it through `MimoSupervisor(permission_handler=handler.handle)`, wire `handler.on_request()` to a UI surface (WebSocket, polling endpoint, or SSE stream alongside chat). At minimum for boot-test: seed it with a rule that auto-allows `external_directory:*` for the test workspace, so the test can run without human interaction.

3. **Document the workspace-trust bypass as intentional.** Add a comment at `mimo_supervisor.py:139` or the plan: "ACP session creation bypasses workspace-trust (no TUI prompt). If future code paths add trust checks to `session/create`, ACP mode will break silently." Consider adding an explicit `markTrusted()` call in `MimoSupervisor.start()` for the global cwd.

4. **Fix fm-mcp DB path configurability.** Add `FM_DB_PATH` env var support to `FmConfig` or `main.rs`. Currently every fm-mcp instance writes to `frankenmemory.db` relative to its cwd — two instances in different directories get separate databases, causing memory split-brain. For the boot test, ensure all fm-mcp instances (thesius provider, bridged MCP) point to the same absolute DB path. Set `FM_DB_PATH` in `.env` and read it in `main.rs`.

5. **Add a memory round-trip test that verifies same-DB.** The plan already flags fm-mcp fragmentation as a risk (`option-a-02-boot-test.md:19`). The boot test MUST verify that a capture from mimo's `capture.ts` is visible to thesius's `FrankenmemoryProvider.recall`. Current architecture spawns **three** fm-mcp instances: (a) thesius's FrankenmemoryProvider, (b) the bridged `frankenmemory` MCP per-session, (c) mimo's shared MCP client. Without a shared DB path, they write to separate databases and recall returns nothing — the memory round-trip silently fails (capture errors are swallowed at `capture.ts:53`).

6. **Add ACP protocol version validation to the boot test.** `acp_client.py:47` hardcodes `"protocolVersion": 1`. If mimo's `@agentclientprotocol/sdk` expects a different version, the handshake fails. The boot test should explicitly verify `result.get("agentInfo")` is populated after `initialize()` (which it is at `acp_client.py:59`, but the plan doesn't check).

7. **Rename "verify tool calls" to "verify external file access + tool calls."** The current wording lets reviewers assume tool execution works. It doesn't.
