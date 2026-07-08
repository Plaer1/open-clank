# P4 — Terminal support

**Goal:** agent can execute shell commands (optional — depends on use case).

## Current state
All terminal callbacks in `acp_bridge.py:134-144` throw "not supported":
```python
async def _terminal_stub(params: dict) -> dict:
    raise Exception("terminal not supported")
```

## Options

### A: Wire to subprocess (simpler)
- Handle `terminal/create` by spawning `subprocess.Popen` with the command
- `terminal/output` → read stdout/stderr
- `terminal/wait_for_exit` → `proc.wait()`
- `terminal/kill` → `proc.kill()`
- No PTY allocation, no interactive sessions

### B: Wire to PTY (full support)
- Use `pty` module or `node-pty` for full terminal emulation
- Supports interactive programs, colors, screen clearing
- More complex, security implications

### C: Keep stubbed (current)
Agent uses file tools (read/write/edit/glob/grep/bash-via-mimo) instead of direct shell.
**Trade-off:** mimo's bash tool still works (it's a native mimo tool, not a bridged terminal). The stubs only block ACP-level terminal requests.

## Recommendation
C — mimo's own `bash` tool works independently of the ACP terminal stubs. The stubs are for ACP-level terminal allocation, which Odysseus doesn't need since mimo handles shell internally.

## Note
This may be a non-issue. mimo's `bash` tool (`src/tool/bash.ts`) executes shell commands directly — it doesn't go through the ACP terminal callbacks. The stubs only affect ACP-level terminal requests from the client side. Verify during P2 boot test whether shell commands work via mimo's native bash tool.
