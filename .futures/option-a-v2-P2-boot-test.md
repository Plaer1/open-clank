# P2 — Boot test (THE GATE)

**Goal:** prove end-to-end flow: chat + memory round-trip + tool execution. Nothing ships until this passes.

**Depends on:** P1 (memory capture must be active).

## Test procedure

### Step 1: Supervisor boot
```bash
cd /home/e/sauce/ai/open-clank
source .env  # or ensure .env loaded by app.py
python app.py
```
Check logs for:
- `[openthesius] mimo supervisor started` — supervisor spawned mimo child
- ACP handshake completed — `acp_client.py:initialize()` returned `agentInfo`
- No import errors from `lifetools_server.py` or `thesius_identity.py`

### Step 2: Chat flow
Send a chat via UI or API:
```
POST /api/chat/send
{"session_id": "<ses_xxx>", "message": "hello, what can you do?"}
```
Verify:
- SSE stream returns text chunks (`data: {"delta": "..."}`)
- No 503 errors
- Stop reason is `end_turn`
- Metrics event at end

### Step 3: Memory round-trip
Send: "remember that my favorite color is blue"
Then send: "what's my favorite color?"
Verify:
- First message triggers capture (check mimo logs for capture tool call)
- Second message returns "blue" (recall from frankenmemory)
- DB check: `sqlite3 <FM_DB_PATH> "SELECT content FROM memories WHERE content LIKE '%blue%' LIMIT 1;"`

### Step 4: File tool execution
Send: "read the file README.md"
Verify:
- Tool call fires (SSE `tool_start` event)
- File content returned (SSE `tool_output` event)
- No permission rejection (README.md is within workspace)

### Step 5: External file access (safe dir)
Send: "list files in ~/sauce"
Verify:
- Permission auto-approved (safe dir match in `PermissionHandler`)
- Directory listing returned

### Step 6: External file access (non-safe dir)
Send: "read /etc/hostname"
Verify:
- Permission rejected (fail-safe)
- Agent receives error and responds gracefully (doesn't hang)

## Exit criteria
- [ ] Supervisor boots without crash
- [ ] Chat completes end-to-end via bridge
- [ ] Memory captured + recalled (round-trip)
- [ ] File tool works within workspace
- [ ] Safe-dir auto-approve works
- [ ] Non-safe-dir rejection is graceful

## Known failure points
- **Import crashes:** `lifetools_server.py` imports `from mcp.types` and `from src.tool_schemas` at module level. If PYTHONPATH doesn't include the repo root, these fail at supervisor start.
- **fm-mcp not found:** `FM_MCP_COMMAND` in .env must point to the actual binary. Verify: `ls -la $FM_MCP_COMMAND`
- **DB locked:** if another fm-mcp process has the DB open, captures fail. Check: `lsof $FM_DB_PATH`
