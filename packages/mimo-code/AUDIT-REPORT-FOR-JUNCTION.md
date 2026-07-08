# MiMoCode Audit — Integration Report for Junction (VS Code Extension)

**Date:** 2026-06-13  
**Scope:** packages/opencode, packages/sdk, packages/plugin, packages/shared  
**Source:** /home/e/sauce/ai/agents/mimo-code/

---

## 1. Technical Integration Options

### Option A: Launch `mimo serve` as a subprocess + HTTP client ⭐ EASIEST

**Effort:** Low (~2-3 days)

Launch `mimo serve` as a managed child process from the VS Code extension, then use the auto-generated HTTP client from `@mimo-ai/sdk/v2/client` to talk to it.

**How it works:**

```ts
// 1. Spawn `mimo serve --hostname=127.0.0.1 --port=0` (port 0 = random available port)
// 2. Read the URL from stdout: "mimocode server listening on http://127.0.0.1:XXXXX"
// 3. Create client:
import { createOpencodeClient } from "@mimo-ai/sdk/v2/client"

const mimocode = createOpencodeClient({
  baseUrl: `http://127.0.0.1:${port}`,
  directory: vscode.workspace.rootPath,
})

// 4. Use it:
const session = await mimocode.session.create({ directory: workspacePath })
const response = await mimocode.session.prompt({
  sessionID: session.id,
  message: "fix the bug in main.ts",
  stream: true, // SSE streaming
})
```

**Key details:**
- The SDK ships `@mimo-ai/sdk` as a published npm package — you can install it directly without building MiMoCode from source
- The server speaks a rich REST API + SSE event stream (124 operationIds in the OpenAPI spec)
- All session management is on the server side: create, prompt, messages, actors, tasks, memory
- The server handles LLM provider auth, tool execution, file operations, subagent orchestration automatically
- The HTTP API includes `session.prompt` (sync streaming POST) and `session.prompt_async` (fire-and-forget)
- SSE events (`/event`) stream diagnostics, permission requests, question prompts, tool results in real-time
- The server runs headless — no TUI, no terminal UI, just the Hono HTTP server

**Client SDK exports (very clean):**
```ts
import { OpencodeClient, createOpencodeClient } from "@mimo-ai/sdk/v2/client"
const client = createOpencodeClient({ baseUrl, directory }) 
// client.session.list(), client.session.create(), client.session.prompt(), etc.
```

**Pros:**
- HTTP API is the most natural interface for a webview-based VS Code extension
- SSE event stream gives you real-time token streaming, tool execution, diagnostics
- No LSP protocol complexity, no JSON-RPC over stdio
- Published npm package ready to use
- Server handles ALL the agent complexity — you just send prompts and get responses
- The extension already launches child processes (OpenClaw, Hermes bridges) — this is the same pattern

**Cons:**
- Requires `mimo` binary to be installed on the user's machine
- `mimo serve --port=0` must be launched on workspace open (or lazily on first use)
- Need to manage the child process lifecycle (start, health check, restart on crash, kill on extension deactivate)
- The `@mimo-ai/sdk` npm package requires `opencode` as the server binary — it'll look for the binary to spawn

---

### Option B: ACP Protocol over stdio (direct stdio child process)

**Effort:** Low-Medium (~3-5 days)

Launch `mimo acp` as a child process and communicate via JSON-RPC over stdio using the Agent Client Protocol.

**How it works:**
```bash
# Extension launches:
mimo acp --cwd /path/to/project

# Communicates via JSON-RPC over stdin/stdout:
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}
{"jsonrpc":"2.0","id":2,"method":"session/new","params":{}}
{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionID":"...","message":{"role":"user","parts":[{"type":"text","text":"fix the bug"}]}}}
```

**Key details:**
- Uses `@agentclientprotocol/sdk` — MiMoCode already ships a full ACP v1 implementation
- Works out-of-the-box with Zed's agent_servers config (see `packages/extensions/zed/extension.toml`)
- The ACP agent maps cleanly to MiMoCode's internal session model
- Currently no streaming (returns complete responses), no tool call reporting, no session persistence on reload
- The README at `packages/opencode/src/acp/README.md` explicitly lists these as "Not Yet Implemented"

**Pros:**
- Pure stdio — no port management, no security concerns about open ports
- Protocol is standardized (Agent Client Protocol v1)
- Same pattern as how the Junction extension already works with Hermes (program output over stdio)
- No HTTP server to manage — just talk to the child process stdin/stdout

**Cons:**
- **No streaming yet** — you get complete responses, not partial token-by-token output
- **No tool call reporting** — the ACP agent doesn't emit progress notifications
- **Session persistence is stub** — `session/load` doesn't restore real conversation history
- Less mature than the HTTP API — it was the last integration added
- VS Code doesn't natively speak ACP (Zed does, but VS Code doesn't have a native ACP client)
- You'd need to implement or bring a JSON-RPC library in the extension

---

### Option C: Embed the agent programmatically via `@mimo-ai/sdk` server constructor

**Effort:** Medium (~5-7 days)

Use the `createOpencodeServer()` function from `@mimo-ai/sdk` directly in the Node.js extension host process, rather than spawning a subprocess.

```ts
import { createOpencodeServer } from "@mimo-ai/sdk"
import { createOpencodeClient } from "@mimo-ai/sdk/v2"

// This spawns mimocode as a child process internally
const server = await createOpencodeServer({
  port: 0, // random port
  hostname: "127.0.0.1",
})

const client = createOpencodeClient({
  baseUrl: server.url,
  directory: workspacePath,
})
```

**But:** This still spawns a child process — `createOpencodeServer` literally calls `launch("opencode", ["serve", ...])` via `cross-spawn`. It's just a thin wrapper around Option A with a programmatic API.

**Pros:**
- Cleaner TypeScript API than managing the subprocess manually
- AbortSignal support for cancellation
- Returns both server URL and close() function

**Cons:**
- Still requires the `opencode` binary on PATH (same as Option A)
- The SDK function `createOpencodeServer` is essentially a convenience wrapper around spawning `opencode serve`
- Can't embed MiMoCode's agent logic in-process without importing the entire core package (which uses Bun-specific imports, Effect TS, and has native dependencies via node-pty)

---

### Option D: Import `@mimo-ai/cli` core directly (in-process embedding)

**Effort:** High (weeks) — NOT RECOMMENDED

Import MiMoCode's core agent loop directly into the extension host's Node.js process.

```ts
import { Server } from "@mimo-ai/cli/src/server/server"
import { Effect } from "effect"
// ... deep integration requires understanding Effect TS, the actor system, etc.
```

**Pros:**
- True in-process integration, no subprocess overhead
- Direct access to memory, actor registry, tool registry
- Full control over lifecycle

**Cons:**
- MiMoCode uses **Effect TS** as its core framework — the application is structured as Effect layers. You'd need to understand the entire Effect ecosystem
- Uses Bun-specific module resolution (`#hono`, `#pty`, `#db`) with conditional imports
- node-pty is a native addon (napi) — works in VS Code extension host only if prebuilt for the right platform
- SQLite via bun:sqlite (Bun-native) has a Node.js fallback but it's clearly secondary
- Imports from `@mimo-ai/plugin` which deep-imports from the opencode package — circular dependency risk
- Massive dependency tree (160+ dependencies in packages/opencode alone)
- Every `mimo serve` startup does a DB migration that can take minutes (JSON migration)
- The TUI and server share the same process — embedding would pull in TUI code
- Overkill for what Junction needs: a backend agent to drive code edits

---

## Comparison Matrix

| Aspect | A: HTTP Server | B: ACP stdio | C: SDK Wrapper | D: In-process |
|--------|:---:|:---:|:---:|:---:|
| **Effort** | Low | Low-Med | Low | Very High |
| **Streaming** | ✅ SSE | ❌ Not yet | ✅ | ✅ |
| **Tool progress** | ✅ SSE events | ❌ Stub | ✅ | ✅ |
| **Session persistence** | ✅ Full | ❌ Stub | ✅ | ✅ |
| **Binary dependency** | `mimo` on PATH | `mimo` on PATH | `mimo` on PATH | None (but massive deps) |
| **Port management** | Needed | None | Needed | None |
| **Security** | Loopback only | Via stdio | Loopback only | Process-internal |
| **Published SDK** | ✅ npm | ❌ Need stdio lib | ✅ npm | ❌ |
| **Maturity** | ✅ Stable | ⚠️ New | ✅ Stable | ❌ Fragile |

---

## 2. Architecture Lessons — What to Steal, What to Skip

### ✅ STEAL: Patterns Worth Borrowing

#### 1. Session-as-a-Service Model

MiMoCode models every conversation as a **session** with a defined lifecycle (create → prompt → messages → abort → delete). Sessions are persisted in SQLite and survive restarts. The Junction extension currently has in-memory conversation state — **adopting a persistent session model** would let users close and reopen VS Code without losing context.

**Steal:** The RESTful session CRUD pattern (`session.create`, `session.prompt`, `session.messages`, `session.delete`).

#### 2. SSE-Based Streaming with Event Bus

MiMoCode uses Server-Sent Events for real-time communication. The `/event` SSE endpoint streams tool results, permission requests, diagnostics, and token-by-token LLM output. It uses a typed event bus (`Bus.publish` / `Bus.subscribe`) internally and serializes to SSE for external consumers.

**Steal:** The typed SSE event pattern. Replace Junction's current polling or websocket-based approach with SSE from the MiMoCode server.

#### 3. Plugin System Architecture (22 Hooks)

MiMoCode's plugin system has 22 well-defined event hooks spanning the entire agent lifecycle: `chat.message`, `chat.params`, `chat.headers`, `tool.execute.before`, `tool.execute.after`, `permission.ask`, `shell.env`, `actor.preStop`, `actor.postStop`, etc.

**Steal:** Define a similar hook interface for the Junction extension's chat backend. Currently, Junction's runtime bridges (OpenClaw, Hermes) are hardcoded. A hook system would let third parties extend the chat without modifying core code.

#### 4. Agent Protocol (ACP) Implementation as a Translation Layer

The ACP implementation (`packages/opencode/src/acp/agent.ts`) acts as a **translation layer** between the standardized ACP protocol and MiMoCode's internal session model. It maps ACP operations to internal Effect TS calls.

**Steal:** This is exactly what Junction's runtime bridges already do — translate between a chat UI protocol and different backend agents. The ACP translation pattern is a clean, well-documented reference for how to structure this.

#### 5. The Message/Part Data Model

MiMoCode's message model separates **message info** (role, agent, model, timestamps) from **message parts** (text, tool_use, tool_result, reasoning). Parts have IDs, can be paginated, and are individually deletable. This is the correct way to model multi-turn agent conversations where each response may contain text, tool calls, and structured output.

**Steal:** Junction should adopt a part-based message model. The current "message = blob of text" approach can't properly represent tool calls, subagent results, or reasoning disclosure. Part-level IDs enable edit-in-place and selective deletion.

#### 6. Worker/Spawn Pattern for Subagents

MiMoCode has a full subagent system: the `ActorRegistry`, `spawn`/`spawn-ref` primitives, and lifecycle tracking with `mode` (subagent/peer) and `lifecycle` (ephemeral/persistent).

**Steal:** The hierarchical spawn pattern, not necessarily the implementation. Junction could use a simplified version where "subagents" are spawned LLM workers with isolated context but shared filesystem access.

#### 7. Memory System Architecture (SQLite FTS5)

The memory system uses SQLite FTS5 for full-text search across `MEMORY.md`, `checkpoint.md`, `notes.md`, and task progress files. It reconciles on-search (reads/re-indexes memory files lazily) and uses BM25 scoring with a relative score floor to filter common-word noise.

**Steal:** Junction could implement project-level memory persistence using SQLite FTS5. Material is straightforward: install `better-sqlite3` (which works in VS Code extension host) and create an FTS5 virtual table. The BM25 score floor trick is excellent UX — always keep the top hit, drop noise below a relative threshold.

---

### ❌ SKIP: Don't Borrow

#### 1. Effect TS Framework

MiMoCode's entire codebase is built on Effect TS — the functional effect system from TypeScript. Effects, layers, services, managed async, structural concurrency. This is **not** grokkable on a weekend. Junction would need weeks to onboard a new developer into Effect TS just to make a small change.

**Skip:** Don't try to integrate Effect TS into Junction. The HTTP API (Option A) is the abstraction boundary — you consume MiMoCode's capabilities over the wire without importing Effect TS at all.

#### 2. The Monorepo Build System (Bun + Turbo)

MiMoCode uses Bun as its runtime, package manager, and test runner, with turborepo for orchestration. 16 packages linked via workspace dependencies with catalog version pinning. Building from source requires Bun 1.3.11 precisely.

**Skip:** Never build MiMoCode from source. Use the published npm packages (`@mimo-ai/sdk`, `@mimo-ai/cli`) or the binary artifact. If the npm `opencode` binary isn't available for a platform, fall back to building, but treat it as an exceptional case.

#### 3. The TUI (SolidJS Terminal Rendering)

The TUI is a massive SolidJS application rendering to the terminal using OpenTUI. 100+ components, keyboard handling, theme system, i18n (9 languages), VAD voice detection, sound effects. It's deeply coupled to terminal rendering primitives.

**Skip:** Junction already has a working webview-based chat. The TUI is irrelevant. Don't import any TUI code or components.

#### 4. Native Addon Dependencies (node-pty, tree-sitter)

MiMoCode depends on node-pty (native PTY allocation) and web-tree-sitter (syntax-aware code parsing) as native addons. These require platform-specific prebuilt binaries (node-pty does ship prebuilts, tree-sitter doesn't for all targets).

**Skip:** VS Code extensions can use native addons if prebuilt via `@vscode/vsce` with `--prebuild` or packaged with the right platform, but it's fragile. Junction should avoid native addons. Use the HTTP API instead — the server handles native dependencies on the server side.

#### 5. The Bun-Specific Module Resolution

MiMoCode uses Bun's conditional imports heavily:
```ts
// packages/opencode/package.json "imports"
"#hono": { "bun": "./src/server/adapter.bun.ts", "node": "./src/server/adapter.node.ts", ... }
"#pty":  { "bun": "./src/pty/pty.bun.ts",    "node": "./src/pty/pty.node.ts",    ... }
"#db":   { "bun": "./src/storage/db.bun.ts",  "node": "./src/storage/db.node.ts",  ... }
```

**Skip:** If you're running under Node.js (as VS Code extension host does), you'd need to handle these conditional imports yourself. Don't try — use the HTTP API.

#### 6. The Yargs CLI Framework

The entire CLI is built on yargs with ~25 commands. The middleware system runs JSON migration, Claude Code import, heap profiler startup. These are all startup-time costs you don't need in a long-lived server process.

**Skip:** `mimo serve` is the only CLI command you care about. The yargs machinery fires once at serve startup and never matters again.

#### 7. Auto-Download of LSP Servers

MiMoCode ships code in `packages/opencode/src/lsp/server.ts` that downloads gopls, clangd, zls, texlab, tinymist, lua-language-server, kotlin-ls, elixir-ls, and other LSP servers from GitHub releases at runtime.

**Skip:** This is MiMoCode's JIT LSP server provisioning for its own agent. Junction is a chat extension, not a coding agent. Don't borrow this — it adds network calls, disk writes, and failure points.

---

## 3. Concrete Next Steps — Easiest Integration Path

### Phase 1: Verify `mimo serve` Works as a Backend (Day 1)

1. **Install `@mimo-ai/sdk` in Junction's dependency tree:**
   ```bash
   cd /home/e/sauce/ai/bridges/openclaw_vscode
   npm install @mimo-ai/sdk
   ```

2. **Write a test script** that launches `mimo serve`, creates a session, sends a prompt, and prints the result:
   ```ts
   import { createOpencodeServer } from "@mimo-ai/sdk"
   import { createOpencodeClient } from "@mimo-ai/sdk/v2"

   const server = await createOpencodeServer({ port: 0, hostname: "127.0.0.1" })
   const client = createOpencodeClient({ baseUrl: server.url, directory: process.cwd() })

   const session = await client.session.create({ directory: process.cwd() })
   // Send a prompt and stream the response
   const response = await client.session.prompt({
     sessionID: session.id,
     message: "What can you help me with?",
   })

   console.log(JSON.stringify(response, null, 2))
   await server.close()
   ```

3. **Verify these basics:**
   - Can start/stop the server from within a Node.js process
   - Session create + prompt works end-to-end
   - Messages are returned with proper parts (text, tool_use, tool_result)
   - Server shuts down cleanly with `server.close()`
   - Multiple sequential prompts work in the same session

### Phase 2: Integrate as a New Backend Provider (Days 2-5)

4. **Create a `MiMoCodeBackend` class** in Junction that mirrors the existing `OpenClawBackend` or `HermesBackend` pattern:
   - Start/stop server management (`createOpencodeServer`)
   - Client creation with workspace directory
   - `sendMessage(text, session?)` method using `client.session.prompt`
   - `streamMessage(text, session?)` method using SSE event subscription
   - Session lifecycle (list, delete, fork)

5. **Map MiMoCode responses to Junction's message model:**
   - MiMoCode returns `{ info: Assistant, parts: Part[] }` where parts can be `text`, `tool_use`, `tool_result`, `reasoning`
   - Map to Junction's display: text parts → rendered text, reasoning parts → collapsible thinking block, tool_use → tool cards, tool_result → code results
   - The SSE `/event` stream emits tool execution events — subscribe to these for real-time tool card rendering

6. **Add a "MiMoCode" option to the backend selector** in the webview sidebar

7. **Test with real tasks:**
   - "List files in the workspace"
   - "Read package.json"
   - "Edit a file"
   - Complex multi-turn conversation

### Phase 3: Polish and Productionize (Days 5-10)

8. **Handle server lifecycle edge cases:**
   - Auto-start server when VS Code opens a workspace (if MiMoCode is the selected backend)
   - Auto-kill server on extension deactivate
   - Health check with `/global/health` endpoint
   - Automatic restart on crash with exponential backoff
   - Graceful handling when `mimo` binary is not installed (show user-friendly install prompt: `npm install -g @mimo-ai/cli`)

9. **Implement SSE event subscription** for real-time progress:
   ```ts
   // Subscribe to /event SSE stream
   const eventSource = new EventSource(`http://127.0.0.1:${port}/event?directory=${encodeURIComponent(workspace)}`)
   eventSource.onmessage = (event) => {
     const data = JSON.parse(event.data)
     switch (data.type) {
       case "server.heartbeat": // periodic keepalive
       case "tool.execute.before": // show tool card with spinner
       case "tool.execute.after": // show tool card with result
       case "session.message.part": // streaming text chunk
       // etc.
     }
   }
   ```

10. **Add authentication passthrough:**
    - MiMoCode needs API keys configured in its own config or passed via `MIMOCODE_*` env vars
    - For MiMo Auto (free tier), it works without config
    - For custom providers, offer a config UI in the webview sidebar, or pass the user's existing OpenClaw/Hermes keys to the MiMoCode server

### Quick Reference: Key API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/session` | POST | Create a new session |
| `/session/:id` | POST | Send a message (sync, streaming) |
| `/session/:id/prompt_async` | POST | Send a message (fire-and-forget) |
| `/session/:id/message` | GET | Get all messages in a session |
| `/session/:id/abort` | POST | Cancel the current generation |
| `/session/:id/fork` | POST | Fork a session at a specific message |
| `/event` | GET (SSE) | Subscribe to real-time events |
| `/global/health` | GET | Health check |
| `/global/config` | GET/PATCH | Read/update config |
| `/session/:id/actors` | GET | List subagents in a session |
| `/session/:id/task` | GET | List tasks in a session |

All endpoints accept `?directory=` query parameter or `x-mimocode-directory` header.

---

## Summary

**Recommended path: Option A** (HTTP server + SDK client).

- It's the most mature integration surface in MiMoCode
- The published `@mimo-ai/sdk` npm package works out of the box
- Full streaming, tool progress, session persistence
- Junction already spawns child processes for OpenClaw/Hermes — this is the same pattern
- You can start with a 2-day proof of concept and grow from there
- No need to touch MiMoCode's source at all (pure API-level integration)

**Don't try in-process embedding.** MiMoCode's Effect TS stack, Bun-specific imports, and massive dependency tree make it impractical to import directly into the VS Code extension host.

The **ACP protocol (Option B)** is worth watching — once the MiMoCode team adds streaming and tool reporting to the ACP implementation, it becomes a strong contender for a protocol-level integration that doesn't depend on HTTP.
