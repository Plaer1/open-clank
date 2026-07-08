# Option A — Phase 4: Agent isolation (per-agent process + folder)

**Goal:** each agent = its own thesius process rooted at `~/entities/[agent]/` (configs + data + dbs). Isolation falls out of moving fixed paths to the agent home — minimal new logic, mostly env-var routing. Reuses the existing `roster` / `~/entities/[agent]/` layout (AGENTS.md/SOUL.md/IDENTITY.md/…).

## Steps
1. **Parameterize thesius's data/config root.** Add `THESIUS_AGENT_HOME=~/entities/[agent]` env var. Set `ODYSSEUS_DATA_DIR=$THESIUS_AGENT_HOME/data` — this single env var gates ~25 sub-paths (DBs, uploads, logs, config/state files, caches, vector stores, generated images, TTS cache — all defined in `constants.py:12-56`). Also set `OPENTHESIUS_GLOBAL_CWD=$THESIUS_AGENT_HOME` (default workspace for this agent).
2. **Route mimo data under agent home.** Set `MIMOCODE_HOME=$THESIUS_AGENT_HOME/.mimocode` on the mimo child process. Without this, all agents share one `mimocode.db` (mimo defaults to XDG/common path). `mimo_supervisor.py` currently only sets `MIMOCODE_CONFIG_CONTENT` + `ODYSSEUS_DATA_DIR` — must also set `MIMOCODE_HOME` in the child's env.
3. **One process per agent.** Launch thesius once per agent with its `THESIUS_AGENT_HOME`. Each gets its own mimo child (supervisor) + its own fm-mcp (Phase 5), all under the agent folder.
4. **Agent config from the folder.** thesius reads the agent's identity/config from `~/entities/[agent]/` (roster already maps id→home; `thesius_identity.py` already loads the bootstrap md). Note: `thesius_identity.py:46-62` has `REPO_ROOT`-based hardcodes for roster, checksums, default `MIMOCODE_HOME`, and a vendored mimo prompt file. For standalone per-agent processes not launched from repo root, these need env-var overrides or relocation under agent home.
5. **Auth stays global.** `AUTH_FILE` (auth.json) should NOT move under `THESIUS_AGENT_HOME` — one user, one auth, shared across agents. Explicitly exclude it from the `ODYSSEUS_DATA_DIR` sweep.
6. **Trust pre-population.** Each mimo child gets its own `trusted-workspaces.json` at `MIMOCODE_HOME/data/`. For an always-on assistant, pre-populate it at agent creation time with the agent home and common workspace dirs. Alternatively, set `external_directory: "allow"` in the agent's frontmatter to bypass the gate entirely for fully-trusted agents.

## Exit criteria
- Run two agents (two `THESIUS_AGENT_HOME`) → fully isolated: separate sessions, memory, mimo, fm-mcp, process. Zero cross-talk.

## Watch
- `ODYSSEUS_DATA_DIR` is the single gating lever — if it's set correctly, ~25 derived paths follow automatically.
- `MIMOCODE_HOME` must be set per child spawn — missing it means all agents share one mimo DB.
- Auth file explicitly excluded from agent home to keep single-user simplicity.
- Per-agent trust lists are a UX cost for multi-directory assistants; pre-populate or bypass via frontmatter.
- `thesius_identity.py` hardcodes need env overrides for standalone agent processes.
