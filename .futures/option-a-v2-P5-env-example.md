# P5 — Update .env.example

**Goal:** anyone cloning the repo knows what to configure for openthesius.

## Current state
`.env.example` has zero openthesius vars. The 6 required vars are scattered across code:
- `OPENTHESIUS_DRIVE=mimo` — gates mimo bridge mode
- `OPENTHESIUS_SRC=<repo>/src` — sys.path for openclank imports
- `OPENTHESIUS_GLOBAL_CWD=/home/e/open-clank` — default workspace
- `OPENTHESIUS_SAFE_DIRS=~/sauce:~/entities:~/open-clank` — auto-approve paths
- `FM_MCP_COMMAND=<repo>/mcp_servers/frankenmemory/target/release/fm-mcp` — binary path
- `FM_DB_PATH=<repo>/data/frankenmemory.db` — shared memory DB

## Fix
Append to `.env.example`:
```
# ── openthesius (mimo bridge) ──
# Route chat through mimo ACP bridge instead of native agent loop.
# OPENTHESIUS_DRIVE=mimo
# OPENTHESIUS_SRC=/path/to/open-clank/src
# OPENTHESIUS_GLOBAL_CWD=/home/user/open-clank
# OPENTHESIUS_SAFE_DIRS=~/sauce:~/entities:~/open-clank
# FM_MCP_COMMAND=/path/to/mcp_servers/frankenmemory/target/release/fm-mcp
# FM_DB_PATH=/path/to/data/frankenmemory.db
```

## Also
Add `MEMORY_PROVIDER=frankenmemory` if not already present — gates Odysseus's native memory off.
