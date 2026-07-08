# Option A — Phase 1: Build & activate

**Goal:** produce the two binaries + config so the stack can launch. No runtime test yet (that's Phase 2).

## Steps
1. **Build `bin/mimo`** (mimo typecheck is clean) — **deferred: build after Phase 3-5 plan settles, since we may modify mimo source before compiling.** The build produces `dist/mimocode-linux-x64/bin/mimo` (single-platform) and copies it to `REPO_ROOT/bin/mimo`. Until built, mock the binary path or use a pre-existing `bin/mimo` if available.
   - `cd apps/mimo/packages/opencode`
   - `bun install` (deps; fresh if needed)
   - `bun run script/build.ts --single` → `dist/mimocode-linux-x64/bin/mimo` (runs a `--version` smoke test)
   - copy → `bin/mimo` at repo root — the supervisor ([mimo_supervisor.py:20](../src/openclank/mimo_supervisor.py#L20)) + `harness/openthesius-mimo.service` expect `REPO_ROOT/bin/mimo`.
2. **Build `fm-mcp`** (Rust):
   - `cd mcp_servers/frankenmemory && cargo build --release` → `target/release/fm-mcp`
   - make it findable: symlink/copy onto PATH, **or** set `FM_MCP_COMMAND=<repo>/mcp_servers/frankenmemory/target/release/fm-mcp`.
3. **Config / `.env`:** confirm `OPENTHESIUS_DRIVE=mimo`, `OPENTHESIUS_SRC=<repo>/src`, `OPENTHESIUS_GLOBAL_CWD=/home/e/open-clank`; add `FM_MCP_COMMAND` if fm-mcp isn't on PATH. Add `OPENTHESIUS_SAFE_DIRS` (colon-separated, `~`-expanded) — paths under these dirs get auto-approved for `external_directory` permission requests so the always-on assistant isn't gated on every file access outside the workspace. See `acp_bridge.py:PermissionHandler` + `mimo_supervisor.py:_spawn_and_init` for the wiring.

## Exit criteria
- `bin/mimo --version` works (or deferred with plan note).
- `fm-mcp` resolves (on PATH or via `FM_MCP_COMMAND`).
- `.env` complete (all 5 openthesius vars set).
- `PermissionHandler` wired: `OPENTHESIUS_SAFE_DIRS` parsed in `app.py`, passed to `MimoSupervisor`, auto-approve active in ACP bridge.

## Exit criteria
- `bin/mimo --version` works.
- `fm-mcp` resolves (on PATH or via `FM_MCP_COMMAND`).
- `.env` complete.

## Watch
- mimo build is heavy (bun install + minified compile); `--single` = current platform only.
- fm-mcp needs the rust toolchain (was built before — `target/` exists).
- `from src.openclank…` resolves because thesius runs with cwd = repo root.
