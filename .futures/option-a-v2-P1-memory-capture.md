# P1 — Activate mimo memory capture

**Goal:** mimo's automatic memory capture writes to frankenmemory instead of silently skipping.

## The fix
`src/openclank/mimo_supervisor.py:91` — change:
```python
skills_config = json.dumps({"skills": {"paths": [_ODYSSEUS_SKILLS_DIR]}})
```
to:
```python
skills_config = json.dumps({
    "skills": {"paths": [_ODYSSEUS_SKILLS_DIR]},
    "memory": {"provider": "frankenmemory"},
})
```

## Why it works
- `capture.ts:62` checks `cfg.memory?.provider !== "frankenmemory"` → skip. With the config set, captures flow to fm-mcp.
- `compaction-capture.ts:25` same gate → compaction summaries get captured.
- `frankenmemory.ts` handles the actual MCP calls to fm-mcp.
- `FM_DB_PATH` is already inherited via `os.environ.copy()` at `mimo_supervisor.py:84` → all spawners converge on one db.

## Verify
1. Start Odysseus with `OPENTHESIUS_DRIVE=mimo`
2. Send a chat that should be remembered (e.g. "remember that my favorite color is blue")
3. Check fm-mcp DB for the capture: `sqlite3 /home/e/sauce/ai/openclanker/data/frankenmemory.db "SELECT * FROM memories ORDER BY rowid DESC LIMIT 5;"`
4. Recall via chat: "what's my favorite color?" → should return "blue"

## Watch
- fm-mcp must be running and DB accessible (FM_DB_PATH set in .env ✅)
- mimo's capture.ts swallows errors silently — check mimo stderr logs if captures don't appear
