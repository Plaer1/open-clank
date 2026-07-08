## Phase 3 Audit — Session-Ownership Rip-Out

### Claims verified

| Claim | Plan ref | Code evidence | Verdict |
|-------|----------|--------------|---------|
| `session_manager.create_session(session_id=…)` takes the id as a param | Steps §1 | `core/session_manager.py:459-467` — `session_id: str` is the first param, assigned directly to `DbSession.id` | **TRUE** |
| PK is `String` — `ses_` and uuid4 coexist, no schema change | Steps §1 | `core/database.py:111` — `id = Column(String, primary_key=True)` | **TRUE** |
| `acp_bridge.py` has `_session_map` / `_reverse_map` / `get_all_mapped_sessions` to delete | Step 2 | `acp_bridge.py:176-178`: `self._session_map: Dict[str, str]`, `self._reverse_map: Dict[str, str]`; `:227-232`: `get_all_mapped_sessions()` | **TRUE** |
| `mimo_supervisor._reconcile_sessions` exists | Step 2 | `mimo_supervisor.py:230-238` — iterates `old_sessions` dict, calls `bridge.resume_session()` | **TRUE**, but currently only restores from in-memory crash snapshot, NOT from thesius DB (plan says it should query DB) |
| `session_routes.py:415` — `POST /api/session` uses `uuid.uuid4()` | Step 1 | `session_routes.py:415`: `sid = str(uuid.uuid4())` | **TRUE** |
| `history_routes.py:479` — fork uses `uuid.uuid4()` | Step 1 | `history_routes.py:479`: `new_id = str(uuid.uuid4())` | **TRUE** |
| `webhook_routes.py` creates sessions with `uuid.uuid4()` | Step 1 implicit | `webhook_routes.py:305, 365`: `sid = str(uuid.uuid4())` in Cases 2+3 of sync-chat | **TRUE** |
| Legacy `ALTER TABLE` pattern at `database.py ~1079` | Step 3 | `database.py:1079`: `_migrate_add_mode_column()` — uses `PRAGMA table_info`, `ALTER TABLE … ADD COLUMN` | **TRUE** |
| `cwd` pins at create | Watch note | `acp_bridge.py:167` stores `self._cwd` at bridge init; `ensure_session:204` uses it as default if no per-chat override; `chat_routes.py:1281` passes `cwd=workspace or None` | **TRUE** |
| FK tables unaffected (PK value changes, type doesn't) | Watch note | 8+ FK tables use `String` FK → `sessions.id`: ChatMessage(193), Document(216), GalleryImage(284), Comparison(432), Note(513), ScheduledTask(560-592), TaskRun(694), ResearchReport(1624). Schema unchanged. | **TRUE** |
| mimo session IDs are `ses_`-prefixed | Overview | `id.ts:76`: `return prefix + "_" + timeBytes.toString("hex") + randomBase62(LENGTH - 12)` where `prefixes.session = "ses"`. Format: `ses_<12hex><14base62>` = 29 chars total. | **TRUE** |
| MCP servers attached at session create | Step 1 implied | `acp_bridge.py:206-219`: `ensure_session()` builds `mcp_servers` list (lifetools + frankenmemory) and passes to `client.new_session()` | **TRUE** (current impl — plan wants `open_session` to be the single create point) |

### Claims wrong / missing

**F1. `chat_routes.py:863` is not an openai-style session creator.**
Plan step 1 says: `session_routes.py:415 (POST /api/session) + other creators (openai :863, fork history_routes.py:479, task_scheduler, research/webhook)`. Line 863 of `chat_routes.py` is inside a `compare_strip` disabled-tools set (stripping `create_session` from blind-compare sessions). The actual "openai" session creator is `session_routes.py:854` (`/session/openai` → `create_session_openai`). The plan conflates `chat_routes.py` with `session_routes.py`. Fix: update the plan to reference `session_routes.py:854-877` and `session_routes.py:855` (the `create_session_openai` function, not chat_routes).

**F2. `task_scheduler.py` does NOT create sessions.**
Plan step 1 lists `task_scheduler` among creators. `task_scheduler.py` uses pre-existing session IDs (`task.session_id` at line ~646+), and `session_manager.ensure_task_session()` at `session_manager.py:624-637` checks cache first, only creates if absent — but that's idempotent, not a new-session minting. The session ID is generated upstream (by the task creation route in `task_routes.py`, which does `task_id = str(uuid.uuid4())` at line 517 — but that's a TASK ID, not a SESSION ID). The plan should either remove task_scheduler from the list or clarify where the actual session creation happens.

**F3. `research_handler.py` does NOT create sessions.**
Plan step 1 lists `research` among creators. `research_handler.py` receives `session_id` as a parameter (`start_research(self, session_id: str, …)` at line 241). It does NOT call `session_manager.create_session()` anywhere. The actual research session creation happens in `research_routes.py:404` (`session_id = f"rp-{uuid.uuid4().hex[:12]}"`) and `research_routes.py:625` (`new_sid = str(uuid.uuid4())`) — the plan should point to `research_routes.py`, not `research_handler.py`.

**F4. `_reconcile_sessions` queries in-memory map, not thesius DB.**
Plan step 2 says: "query thesius DB for recent `ses_`-prefixed sessions → `resume_session`". Current `mimo_supervisor.py:230-238` only reconciles from `old_sessions` which is `bridge.get_all_mapped_sessions()` — an in-memory RAM dict that dies on thesius restart. The plan is correct about what needs to change, but the current code does NOT implement DB-driven reconcile yet. This is a TODO, not a verified existing capability.

**F5. `ensure_session` stores odysseus_session → mimo mapping — not the plan's proposed identity model.**
Plan step 2 says: "`odysseus_session` *is* the mimo id → drop `_session_map`/`_reverse_map`". Current `acp_bridge.py:193-219` stores the odysseus uuid4 as key, mimo `ses_` as value. After the rip-out, `odysseus_session` WILL BE the mimo id — the map becomes unnecessary. The plan is correct about the end state but doesn't mention that `ensure_session()` line 201-203 returns the mapped mimo_id — post-ripout, `ensure_session` just needs to verify/create the mimo session using the ID it's given, with no mapping needed.

**F6. Plan says "7 FK tables" — actual count is higher.**
Thesius has at least 8 FKs referencing `sessions.id`: ChatMessage, Document, GalleryImage, Note (×3 FK variants: session_id on notes, session_id on note_attachments, session_id on scheduled_tasks per crew_member), ScheduledTask (session_id for task output), Comparison, TaskRun (session_id), ResearchReport. The "7" is approximately true for the core chat-relevant tables but undersells the blast radius if there were a type change. Since the plan is correct that the type doesn't change (String → String), this is a minor documentation issue, not a bug.

**F7. `session_routes.py:855` (`/session/openai`) is NOT inside `chat_routes.py`.**
Same root cause as F1 — the plan's file references for openai endpoint are wrong. The route is `POST /api/session/openai` at `session_routes.py:854`, not in `chat_routes.py`.

### Permission UX gap

**The workspace-trust gate blocks only in the TUI, not in ACP/server mode.**

- `workspace-trust.ts:checkTrust()` returns `"trusted"`, `"untrusted"`, or `"dangerous"`.
- Only the TUI calls it — `cli/cmd/tui/thread.ts:205-212` checks trust and prompts the user BEFORE starting. If rejected, the session doesn't start.
- Server-side session creation (`server/routes/instance/session.ts:284-308`) has NO trust check. The ACP path → `acp/session.ts:20-44` → `sdk.session.create({directory: cwd})` creates sessions in any directory without blocking.
- **Thesius will NOT hit this gate on session creation.** When thesius creates a mimo session, it goes through ACP → server session.create, which bypasses the TUI trust prompt entirely.

**However, the `external_directory` permission WILL trigger mid-chat.**

- When mimo's agent touches files outside the project/worktree, it fires `permission.asked` events (`agent.ts:189-266`).
- The ACP agent calls `connection.requestPermission({…})` (agent.ts:199-211).
- Thesius receives this via the ACP callback `session/request_permission` → `acp_bridge.py:109-113` calls `permission_handler(params)`.

**The handler is NOT wired to the UI.**

- `MimoSupervisor` is instantiated in `app.py:1165` with `memory_provider=memory_provider` only — **no `permission_handler`** is passed.
- `acp_bridge.py:111-113`: when `permission_handler` is None, the fail-safe rejects ALL permission requests with `{"outcome": {"outcome": "selected", "optionId": "reject"}}`.
- The `PermissionHandler` class (`acp_bridge.py:511-581`) exists but is NEVER instantiated or connected to the request handlers.

**Effect: tools requiring `external_directory` or other permissions will SILENTLY FAIL.**

The chat will NOT hang — the fail-safe reject is immediate. But operations like reading/writing files outside the worktree will be rejected without the user ever knowing why. The agent may retry or produce degraded output.

**This is a pre-existing gap, not introduced by Phase 3.** Phase 3 doesn't change permission handling. But the plan should acknowledge it because Phase 3 makes the gap more impactful: as more sessions run through mimo, more tools will hit permission gates that the UI can't surface.

### Recommendations

1. **Fix file references in the plan** — Replace `chat_routes.py:863` with `session_routes.py:854` (openai create). Remove `task_scheduler` and `research_handler` from the creator list; add `research_routes.py:404, 625` instead. Add `compare_routes.py:89-90` (compare-mode sessions) and `cookbook_routes.py:524` (cookbook sessions). This touches ~6-8 files, not the 4 the plan lists.

2. **Full creator inventory** — Audit all `uuid.uuid4()` sites that feed `session_manager.create_session()`:
   - `session_routes.py:415` — main create
   - `session_routes.py:863` — openai create
   - `history_routes.py:479` — fork
   - `webhook_routes.py:305, 365` — sync chat Cases 2+3
   - `research_routes.py:404, 625` — research sessions
   - `compare_routes.py:89-90` — compare helper sessions
   - `cookbook_routes.py:524` — cookbook sessions
   - `email_routes.py:2265` — email draft sessions (shorthand hex, 16 chars — might conflict with `ses_` ID length)
   That's 9-10 distinct creation sites, not the 4 the plan implies.

3. **`_reconcile_sessions` rewrite scope** — The plan says "query thesius DB for recent `ses_`-prefixed sessions." This is a new query that doesn't exist yet. It needs: (a) a DB query in `session_manager.py` returning sessions where `id LIKE 'ses_%'` AND `last_accessed > cutoff`, (b) calling `resume_session` for each, (c) backoff/retry on failure. This is a non-trivial new code path, not a one-line change.

4. **Permission UX — wire the handler OR document the gap.** Options:
   - **A (recommended):** Wire `PermissionHandler` to the SSE stream. When a permission request arrives, emit an SSE event type `"permission_request"` with the tool details and options. The frontend renders a prompt. The user's choice routes back via a new endpoint (`POST /api/session/{sid}/permission`).
   - **B:** Pre-trust the thesius workspace directory (`OPENTHESIUS_GLOBAL_CWD`) in mimo's `trusted-workspaces.json` at startup so `external_directory` never fires for that tree. This doesn't solve the general case (per-chat workspace overrides).
   - **C:** Document the gap and defer. The fail-safe reject means tools just fail rather than hanging. Acceptable for Phase 3 boot-test, unacceptable for production.
   At minimum, the plan should add a "Watch" note documenting that permission requests are currently reject-by-default and need wiring before production use.

5. **`open_session` vs `ensure_session` semantics** — The plan proposes `open_session` as the NEW mint point. But `ensure_session` is idempotent (get-or-create) and used per-turn. `open_session` should be the CREATE-only path (called once at session creation time); `ensure_session` should become a VERIFY-only path (just check the session exists in mimo, no mapping). Clarify in the plan whether `ensure_session` stays or is replaced.

6. **Session ID registration timing** — Currently, thesius creates the DB row BEFORE the mimo session. Post-ripout, the mimo ID must exist first (it's the PK). The flow becomes: `bridge.open_session(cwd)` → `ses_xxx` → `session_manager.create_session(session_id=ses_xxx, …)`. If `open_session` fails, the DB row is never created (no orphan). If `create_session` fails, the mimo session is orphaned — add a cleanup path or accept this as low-risk (mimo sessions auto-expire).

7. **`email_routes.py:2265` 16-char hex IDs** — `sid = _uuid.uuid4().hex[:16]` produces a 16-char hex string. This is NOT a `ses_`-prefixed ID and would not match the `LIKE 'ses_%'` query in reconcile. Either these email-draft sessions stay as legacy uuid4 shorts, or the reconcile query needs to handle both formats.
