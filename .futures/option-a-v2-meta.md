# Option A v2 — Meta plan

**Context:** v1 plans + audits archived to `.futures/archive/option-a-v1/`. Many audit findings were stale — FM_DB_PATH and PermissionHandler safe-dirs are already fixed. This plan covers only what's actually broken.

**Goal:** Phase 2 boot test passes end-to-end (chat + memory round-trip + tool execution), then ship the remaining gaps.

## Status of v1 claims

| v1 Phase | Claimed | Actual |
|----------|---------|--------|
| 1 — Build & activate | DONE | DONE (bin/mimo + fm-mcp built, .env set) |
| 2 — Boot test | DONE | **NOT PASSED** — never ran end-to-end with memory round-trip |
| 3 — Session ownership | DONE | DONE (open_session + DB-driven reconcile) |
| 4 — Agent isolation | DONE | DONE (env vars parameterized, MIMOCODE_HOME passed) |
| 5 — fm-mcp lifecycle | DONE | Partially — FM_DB_PATH reads env ✅, auto-build ✅, but mimo memory capture is dead (see P1) |

## Remaining work

### P1 — Activate mimo memory capture (BLOCKER for Phase 2 gate)
**File:** `src/openclank/mimo_supervisor.py:91`
**Problem:** `MIMOCODE_CONFIG_CONTENT` injects only `skills.paths`. Mimo's `capture.ts` and `compaction-capture.ts` check `cfg.memory?.provider !== "frankenmemory"` and skip. All automatic memory capture from mimo conversations is dead.
**Fix:**
```python
skills_config = json.dumps({
    "skills": {"paths": [_ODYSSEUS_SKILLS_DIR]},
    "memory": {"provider": "frankenmemory"},
})
```
**Verify:** boot test memory round-trip (capture via mimo → recall via FrankenmemoryProvider).

### P2 — Run the Phase 2 boot test for real
**Gate:** nothing ships until this passes.
**Steps:**
1. Start Odysseus with `OPENTHESIUS_DRIVE=mimo`
2. Verify supervisor spawns, ACP handshake completes
3. Send a chat → verify streamed response + tool calls
4. Verify memory capture landed in fm-mcp DB
5. Verify recall returns it
6. Verify file access outside workspace auto-approves (safe dirs)

### P3 — Permission UX for non-safe-dir requests
**Problem:** Safe dirs auto-approve. Everything else silently rejects (fail-safe). No way for user to approve on-the-fly.
**Options:**
- A: Wire PermissionHandler to SSE stream → frontend renders prompt → user clicks approve/reject
- B: Add `external_directory: "allow"` to agent frontmatter for fully-trusted agents (loses protection)
- C: Accept reject-by-default for now, document the limitation
**Recommendation:** C for now, A later.

### P4 — Terminal support
**Problem:** All terminal callbacks throw "not supported". Agent can't run shell commands.
**Status:** Terminal stubs exist at `acp_bridge.py:134-144`. The ACP protocol supports terminal, mimo implements it. Just need Odysseus to handle the callbacks instead of stubbing.
**Options:**
- A: Wire to a real PTY (complex, security implications)
- B: Wire to subprocess execution (simpler, no PTY allocation)
- C: Keep stubbed, agent uses file tools only
**Recommendation:** C for now unless shell access is required.

### P5 — Update .env.example
**Problem:** `.env.example` has zero openthesius vars. Anyone cloning won't configure correctly.
**Fix:** Add commented-out openthesius section with all vars + descriptions.

### P6 — Frankenmemory PyO3 binding (Odysseus in-process)
**Goal:** compile fm-core as a Python extension module so Odysseus calls frankenmemory directly — no subprocess, no pipe, no JSON, no MCP protocol overhead.
**Spec:** [the-plan/p6-frankenmemory-pyo3.md](the-plan/p6-frankenmemory-pyo3.md)
**Impact:** eliminates one subprocess, ~100x latency improvement on memory calls, simpler error handling (no pipe EOF or MCP protocol errors). Mimo still uses MCP subprocess (TypeScript).

### P7 — Clean up stale audit references
**Problem:** `.futures/archive/option-a-v1/audit1/` contains findings that are now stale (FM_DB_PATH, PermissionHandler). The audit files reference the old plan structure.
**Action:** No action needed — archived. New audits should reference this plan.

## Phase order
P1 → P2 (gate) → P5/P6 in parallel → P3 (later) → P4 (as needed)

## Locked architecture (updated — PyO3 canonized)
- mimo owns sessions, no shim
- mimo self-manages cwd
- chat = a mimo session
- agent = isolated process + ~/entities/[agent]/ folder
- memory = frankenmemory only (native disabled)
- **frankenmemory access is split by consumer:**
  - Odysseus (Python) → PyO3 in-process (no subprocess, no JSON, no MCP) — see P6
  - mimo (TypeScript) → MCP subprocess over stdio (fm-mcp binary still built for this path)
  - Both hit same SQLite DB via FM_DB_PATH
- **ACP over stdio for mimo is correct** — child process + pipes is the fastest local IPC. JSON-RPC is mimo's protocol, non-negotiable. Not a bottleneck.
- **Browser ↔ Odysseus stays HTTP/S + SSE** — remote access preserved. Local access uses TCP loopback.

## Communication topology (canonical)
```
Browser ←→ Odysseus:        HTTP/S + SSE (KEEP — remote access)
Odysseus  ←→ mimo:          ACP over stdio (KEEP — already optimal, child process + pipes)
Odysseus  ←→ frankenmemory: PyO3 in-process (NEW — eliminates subprocess + JSON + MCP)
mimo      ←→ frankenmemory: MCP over stdio (KEEP — TypeScript can't load .so)
```
