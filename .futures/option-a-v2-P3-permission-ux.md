# P3 — Permission UX for non-safe-dir requests

**Goal:** user can approve/reject permission requests beyond the auto-approved safe dirs.

**Depends on:** P2 passing (need working chat first).

## Current state
- Safe dirs: auto-approve via `PermissionHandler._safe_dirs` ✅
- Non-safe dirs: fail-safe reject (300s timeout → reject) ❌

## Options

### A: SSE-based permission flow (recommended for production)
1. `PermissionHandler.on_request()` emits SSE event `permission_request` to frontend
2. Frontend renders modal with tool details + approve/reject buttons
3. User clicks → `POST /api/session/{sid}/permission` → `handler.resolve(request_id, option_id)`
4. Agent continues

**Files to modify:**
- `acp_bridge.py` — emit SSE event on permission request
- `chat_routes.py` — add permission endpoint
- Frontend — render permission modal

### B: Agent-level bypass (quick and dirty)
Set `external_directory: "allow"` in agent frontmatter → suppresses all external-directory prompts.
**Trade-off:** zero filesystem protection for that agent.

### C: Accept reject-by-default (current)
Document the limitation. Agent gracefully handles rejection (tool error → agent responds).
**Trade-off:** agent can only access files within workspace + safe dirs.

## Recommendation
C now, A later. The agent works within its workspace. External access is a UX feature, not a blocker.
