## Phase 4 Audit — Agent Isolation

### Claims verified

1. **`thesius_identity.py` already parameterized by `home` path** — `compose_system_prompt(home)` accepts a directory, reads bootstrap files from it. The roster mechaniesm (`load_roster()` at line 599) maps agent IDs to `home` directories. This part works. The missing piece: these hardcoded module-level paths are NOT under `THESIUS_AGENT_HOME`:
   - `REPO_ROOT = Path(__file__).resolve().parents[2]` (line 46)
   - `ROSTER_PATH = REPO_ROOT / "config" / "openclank" / "roster.json5"` (line 47)
   - `CHECKSUMS_PATH = REPO_ROOT / "config" / "openclank" / ".checksums.json"` (line 48)
   - `_DEFAULT_MIMOCODE_HOME = REPO_ROOT / ".mimocode_home"` (line 62)
   These must resolve under the agent home, not the repo root.

2. **`mimo_supervisor.py` spawns per-mimo child** — Spawns `bin/mimo acp` (line 88), passes `MIMOCODE_CONFIG_CONTENT` env var (line 79). Injects `ODYSSEUS_DATA_DIR` from env or from `REPO_ROOT/"data"` (lines 25-28). Currently shares ONE `MIMOCODE_HOME` across all agents — per-agent isolation requires distinct `MIMOCODE_HOME` per spawn.

3. **`acp_bridge.py` cwd handling already correct** — `ensure_session(odysseus_session, cwd)` (line 193) accepts per-chat cwd override. Default falls back to `OPENTHESIUS_GLOBAL_CWD` which defaults to `Path.home()/"open-clank"` (mimo_supervisor.py:139). For per-agent isolation, `OPENTHESIUS_GLOBAL_CWD` should default to the agent's home directory, not one global path.

4. **Mimo's path system (`Global.Path`) derives from `MIMOCODE_HOME`** — `shared/src/global.ts:28-49`: if `MIMOCODE_HOME` env var is set, all subdirectories (data, cache, config, state) live under it. If unset, falls through to XDG. Per-agent isolation works IF each thesius process sets `MIMOCODE_HOME=~/entities/<agent>/.mimocode` for its mimo child.

5. **`FrankenmemoryProvider` command configurable** — `FM_MCP_COMMAND` env var overrides the binary path (`frankenmemory_provider.py:36`). fm-mcp db path defaults to `"frankenmemory.db"` in CWD (`fm-core/config.rs:65`) — MUST be moved under agent home explicitly (e.g., `FM_DB_PATH` env var or `--db-path` CLI flag).

6. **`~/entities/` DOES exist** (7 agent dirs: ada, jiminy, ling, lingMo, testLing + .ling directory) — but nothing in thesius code references `~/entities/` directly. The roster at `config/openclank/roster.json5` maps ids to arbitrary `home` paths. This is fine: `THESIUS_AGENT_HOME` can point to any roster entry's `home` field.

### Claims wrong / missing

1. **"Isolation falls out of the structure — no isolation logic to build"** — FALSE. The plan's verbatim claim at step 1. The fixed-path inventory below proves this wrong: ~25+ paths must be redirected. That IS isolation logic. The plan correctly notes "inventory + redirect them" as a step, then immediately contradicts itself by claiming there's no logic to build. The redirection IS the logic.

2. **"Agent config from the folder" (step 3)** — Partially true. `thesius_identity.py` already reads bootstrap files from a home directory. But the roster loading and checksum storage are hardcoded to `config/openclank/` under `REPO_ROOT`. For a standalone agent process, those paths need to move under the agent home (or remain shared, if the roster is global — but the plan doesn't address this tension).

3. **"Per-agent fm-mcp are automatic consequences"** — Partially true. fm-mcp spawns via `FM_MCP_COMMAND` env var (configurable), but its DB path defaults to `"frankenmemory.db"` in CWD. The plan doesn't address how to give each agent's fm-mcp instance a distinct DB file. Need either `FM_DB_PATH` env var or a `--db-path` flag on fm-mcp.

4. **Mimo database sharing** — the plan implies each agent gets its own mimo child with its own DB (`mimocode.db` at `MIMOCODE_HOME/data`). This is correct structurally, but the plan doesn't mention that `MIMOCODE_HOME` must be set per-child spawn. `mimo_supervisor.py` currently does NOT set `MIMOCODE_HOME` — it only sets `MIMOCODE_CONFIG_CONTENT` and `ODYSSEUS_DATA_DIR`.

### Permission UX gap

**How per-agent isolation interacts with workspace-trust/external_directory:**

`workspace-trust.ts` stores the trusted-workspaces list at `Global.Path.data/trusted-workspaces.json`. Since each `Global.Path.data` is rooted under `MIMOCODE_HOME/data` (or XDG), each mimo child instance has its OWN trust list.

`external_directory` (permission.ts:44, default = `"ask"`) fires whenever a tool touches a path outside the project worktree. The user must approve each external directory once, after which it's saved to `trusted-workspaces.json` for that mimo instance.

**The problem for an always-on assistant:** An assistant that may access many directories (like a system-wide agent) would trigger `external_directory` prompts across many directories. With per-agent isolation:

1. **Each agent has ITS OWN trust list.** If "ling" (one agent) visits `/home/e/projects/foo` and the user approves it, that trust is saved only in ling's `trusted-workspaces.json`. If "ada" (another agent) later visits `/home/e/projects/foo`, the user must approve it AGAIN.

2. **No centralized trust management.** There is no mechanism to share trust across mimo instances, no global `trusted-workspaces.json`, no import/export of trust. Trust is instance-local by design.

3. **Per-agent isolation makes this WORSE**, not better. Without isolation (one shared mimo process), one trust list covers all access. With isolation, N agents = N trust lists to maintain. The plan says isolation "falls out of the structure" — but the trust UXI is the opposite: it's about making cross-directory access seamless for an always-on assistant, and isolation fragments the trust state.

4. **Workaround:** Pre-populate `trusted-workspaces.json` for each agent at agent creation time from a master list (e.g., the agent's home directory + a curated list of common dirs). Or bypass `external_directory` completely for trusted agents by setting `external_directory: "allow"` in the agent's permission config (via frontmatter). The agent's `permission:` frontmatter in `AGENTS.md` flows through `thesius_identity.py` → `compose_agent_file()` → `sync_to_mimo_config()` into the mimo agent `.md` file. A frontmatter like:
   ```yaml
   permission:
     external_directory: "allow"
   ```
   would suppress all external-directory prompts for that agent — which is the sensible default for an always-on assistant. But this means the permission system provides ZERO protection for that agent — a tradeoff the plan does not discuss.

5. **Bottom line:** The plan's claim that "isolation falls out of the structure" ignores the trust fragmentation problem. For an always-on assistant, either (a) accept trust fragmentation and per-agent approval prompts, (b) bypass `external_directory` entirely (losing the protection), or (c) build centralized trust management (the complexity the plan claims to avoid).

### Fixed-path inventory

Every fixed path in thesius that must move under `THESIUS_AGENT_HOME` (or be parameterized by it):

#### 1. Core data root — THE SINGLE GATING CONSTANT
- **`constants.py:12`** — `DATA_DIR = os.getenv("ODYSSEUS_DATA_DIR", get_default_data_dir())`
  All paths below derive from this. Setting `ODYSSEUS_DATA_DIR=$THESIUS_AGENT_HOME/data` would redirect everything. This is the plan's primary lever.

#### 2. Database files (derived from DATA_DIR, constants.py:38-40)
- `APP_DB = os.path.join(DATA_DIR, "app.db")` — SQLite session/memory/config DB
- `SCHEDULED_EMAILS_DB = os.path.join(DATA_DIR, "scheduled_emails.db")`
- `EMAIL_CACHE_DB = os.path.join(DATA_DIR, "email_cache.db")`
- **`database.py:40-41`** — `_default_database_url()` → `sqlite:///{DATA_DIR / app.db}`

#### 3. Upload directory
- **`constants.py:23`** — `UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")`
- **`constants.py:43`** — `PERSONAL_UPLOADS_DIR = os.path.join(DATA_DIR, "personal_uploads")`

#### 4. Log directory
- **`app.py:99`** — `_log_dir = os.path.join(DATA_DIR, "logs")`
- **`app.py:101`** — `_log_file = os.path.join(_log_dir, "app.log")`

#### 5. Config / state files (constants.py:17-37)
- `SESSIONS_FILE` (.json)
- `MEMORY_FILE` (.json)
- `MEMORY_DOC` (.md)
- `SETTINGS_FILE` (.json)
- `AUTH_FILE` (auth.json) — **NOTE:** auth is per-agent or shared? The plan doesn't specify. If each agent has its own auth, this moves. If auth is system-wide, it stays global.
- `USER_PREFS_FILE` (.json)
- `PRESETS_FILE` (.json)
- `INTEGRATIONS_FILE` (.json)
- `CONTACTS_FILE` (.json)
- `APP_KEY_FILE` (.app_key) — encryption key; must be per-agent to keep encrypted data scoped
- `COOKBOOK_STATE_FILE` (.json)
- `BG_JOBS_FILE` (.json)
- `VAULT_FILE` (.json)
- `SKILLS_FILE` (.json)
- `TIDY_CALENDAR_STATE_FILE` (.json)
- `EMBEDDING_ENDPOINT_FILE` (.json)
- `FEATURES_FILE` (.json)

#### 6. Cache directories (constants.py:44-56)
- `EMOJI_CACHE_DIR`
- `RAG_DIR`, `CHROMA_DIR` — ChromaDB vector store
- `BG_JOBS_DIR`
- `DEEP_RESEARCH_DIR`
- `MCP_OAUTH_DIR`
- `GENERATED_IMAGES_DIR`
- `TTS_CACHE_DIR`
- `EMAIL_URGENCY_CACHE_DIR`
- `SKILLS_DIR`
- `GALLERY_DIR`, `GALLERY_UPLOADS_DIR`
- `MEMORY_VECTORS_DIR`
- `FASTEMBED_CACHE_DIR`

#### 7. Mimo data (mimo side, env-controlled)
- **`shared/src/global.ts:28-49`** — `MIMOCODE_HOME` env var controls all mimo paths (data, cache, config, state, bin, log). Each agent's mimo child needs `MIMOCODE_HOME=$THESIUS_AGENT_HOME/.mimocode`
- **`workspace-trust.ts:9`** — `trusted-workspaces.json` at `Global.Path.data` — per-mimo-instance trust list (see Permission UX gap above)
- **`storage/db.ts:32-34`** — `mimocode.db` at `Global.Path.data` — per-agent mimo sessions/clients db

#### 8. frankenmemory DB
- **`fm-core/config.rs:65`** — `db_path: "frankenmemory.db"` (default CWD)
- `FrankenmemoryProvider` spawns `fm-mcp` via `FM_MCP_COMMAND` env var (`frankenmemory_provider.py:36`). No env var for DB path exists today. Need `FM_DB_PATH` or `--db-path` on fm-mcp CLI.

#### 9. OPENTHESIUS_GLOBAL_CWD
- **`mimo_supervisor.py:139`** — `os.environ.get("OPENTHESIUS_GLOBAL_CWD", str(Path.home() / "open-clank"))`
  The default `~/open-clank` makes no sense for an agent with its own home. Should default to `$THESIUS_AGENT_HOME` (the agent's root directory).

#### 10. Hardcoded ~/ paths
- **`runtime_paths.py:29`** — Frozen mode: `~/.odysseus/data` — needs agent routing
- **`mimo_supervisor.py:139`** — `Path.home() / "open-clank"` — default CWD

#### 11. thesius_identity.py hardcoded repo paths
- **`line 46`** — `REPO_ROOT = Path(__file__).resolve().parents[2]` — used for roster, checksums, default MIMOCODE_HOME, base-behavior path
- **`line 47`** — `ROSTER_PATH = REPO_ROOT / "config" / "openclank" / "roster.json5"`
- **`line 48`** — `CHECKSUMS_PATH = REPO_ROOT / "config" / "openclank" / ".checksums.json"`
- **`line 62`** — `_DEFAULT_MIMOCODE_HOME = REPO_ROOT / ".mimocode_home"`
- **`line 58`** — `_BASE_BEHAVIOR_PATH = REPO_ROOT / "apps" / "mimo" / ...` — vendored mimo prompt file

#### 12. Mimo supervisor paths
- **`mimo_supervisor.py:19`** — `REPO_ROOT = Path(__file__).resolve().parents[2]`
- **`mimo_supervisor.py:20`** — `MIMO_BIN = REPO_ROOT / "bin" / "mimo"` — MUST stay (binary location, not agent data)
- **`mimo_supervisor.py:24-28`** — `_ODYSSEUS_SKILLS_DIR` — derived from `ODYSSEUS_DATA_DIR` env var, falls back to `REPO_ROOT/data/skills`. For per-agent isolation, skills dir should be under agent home.

#### Summary table

| Category | Path | Control point |
|----------|------|--------------|
| Everything under DATA_DIR | `constants.py:12` | `ODYSSEUS_DATA_DIR` env var |
| mimo home | `shared/global.ts:28` | `MIMOCODE_HOME` env var |
| fm-mcp DB | `fm-core/config.rs:65` | Needs `FM_DB_PATH` env var (doesn't exist yet) |
| CWD default | `mimo_supervisor.py:139` | `OPENTHESIUS_GLOBAL_CWD` env var (defaults to ~/open-clank) |
| Roster/checksums | `thesius_identity.py:47-48` | Hardcoded to `REPO_ROOT/config/openclank/` |
| Default MIMOCODE_HOME | `thesius_identity.py:62` | Hardcoded to `REPO_ROOT/.mimocode_home` |
| Frozen data dir | `runtime_paths.py:29` | Hardcoded `~/.odysseus/data` |

### Recommendations

1. **Add `THESIUS_AGENT_HOME` env var** — set it to `~/entities/<agent>` at process launch. Route everything through it:
   - `ODYSSEUS_DATA_DIR=$THESIUS_AGENT_HOME/data` (handles all constants.py paths)
   - `MIMOCODE_HOME=$THESIUS_AGENT_HOME/.mimocode` (handles mimo DB + trust)
   - `OPENTHESIUS_GLOBAL_CWD=$THESIUS_AGENT_HOME` (default workspace)
   - `FM_DB_PATH=$THESIUS_AGENT_HOME/frankenmemory.db` (needs fm-mcp change)

2. **Fix `thesius_identity.py` hardcodes** — `REPO_ROOT`-based paths for roster, checksums, base-behavior, and default MIMOCODE_HOME should fall back to `THESIUS_AGENT_HOME` or be env-overrideable. The roster file might legitimately be shared (one roster for all agents), but the checksums sidecar should be per-agent.

3. **fm-mcp needs `FM_DB_PATH` env var or `--db-path` flag** — The `FmConfig.db_path` defaults to `"frankenmemory.db"` (CWD). Without this, every agent's fm-mcp writes to the same file (or fails if launched from different dirs). This is a Phase 5 concern but must be accounted for in Phase 4 planning.

4. **Pre-populate `trusted-workspaces.json`** — For an always-on assistant, seed the trust list with common directories at agent creation time. Or set `external_directory: "allow"` in the agent's permission frontmatter if the agent should have unrestricted filesystem access. The plan should explicitly state which approach to take.

5. **Document the trust fragmentation tradeoff** — The plan claims "no isolation logic to build" but the trust fragmentation created by isolation IS a UX problem that needs addressing. Either document it as a known acceptable tradeoff (user approves per-agent) or design a mitigation (pre-populated trust, centralized trust store, or permission bypass for trusted agents).

6. **Auth file tension** — `AUTH_FILE` (auth.json) contains user accounts. If each agent process has its own `DATA_DIR`, each has its own auth. For a system where one user accesses multiple agents, auth should be shared (global). The plan should specify: auth is system-wide and lives outside `THESIUS_AGENT_HOME`, or auth is per-agent and users configure separately. Currently `auth.json` is at `DATA_DIR/auth.json` — it moves with everything else unless explicitly excluded.
