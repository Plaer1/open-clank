# Option A — Phase 3: Session-ownership rip-out (mimo ids canonical)

**Goal:** thesius uses mimo's `ses_…` session id as its canonical id — no shim, no in-memory map. Durable resume across restarts. (Audit ad4ac.) **Start only after Phase 2 passes.**

**Key leverage:** `session_manager.create_session(session_id=…)` already takes the id as a param → change the *source* of the id, not the internals. PK is `String` → uuid4 + `ses_` coexist, **no schema change**.

## Steps
1. **Mint mimo id at create.**
   - `acp_bridge.py`: add `async def open_session(self, cwd) -> str` → `self._client.new_session(...)` returns the `ses_…` id (no map write).
   - `session_routes.py:415` (`POST /api/session`) + other session-creator sites: openai `session_routes.py:863`, fork `history_routes.py:479`, task_scheduler `task_scheduler.py:1405,1592,1916,2408`, research `research_routes.py:404,625`, webhook `webhook_routes.py:305,365`, compare `compare_routes.py:89-90`, cookbook `cookbook_routes.py:524`. When `OPENTHESIUS_DRIVE=="mimo"` + supervisor present → `sid = await bridge.open_session(cwd)`. Make these routes `async`.
   - **Fallback:** supervisor None / non-mimo → keep `uuid.uuid4()` (or explicit 503 — pick one).
2. **Bridge → identity + DB-driven reconcile.**
   - `odysseus_session` *is* the mimo id → drop `_session_map`/`_reverse_map`; `ensure_session` returns it directly.
   - `mimo_supervisor._reconcile_sessions`: query thesius DB for recent `ses_`-prefixed sessions → `resume_session`. **= the durability win** (today the RAM map dies on a thesius restart → old chats can't resume).
3. **Legacy coexist.** Old uuid4 rows keep working (String PK). Optional nullable `Session.mimo_session_id` as a durable lazy-cache for pre-migration rows (guarded `ALTER TABLE`, pattern `database.py:1079`).
4. **Delete dead map code** (`_session_map`/`_reverse_map`/`get_all_mapped_sessions`).

## Exit criteria
- New session create returns a `ses_…` id; chat works under it.
- Restart thesius mid-conversation → old chat resumes (DB-driven reconcile).
- Legacy uuid4 sessions still load + chat.

## Watch
- create path sync→async is the only structural change. `create_session(session_id=)` stays sync — just feed it the pre-minted id.
- cwd pins at create (vs per-turn today) — fine, mimo is directory-keyed.
- compare / one-shot sessions stay uuid4 (non-resumable).
- thesius-only fields stay on the `Session` row (owner/name/folder/endpoint/model/tokens/crew_member_id/mode); 7 FK tables unaffected (PK *value* changes, type doesn't).
