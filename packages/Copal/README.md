# Copal

Local-first knowledge vault and planning workspace.

OpenClank's hosted Copal and the standalone Servo shell use separate default physical stores. The standalone shell appends `standalone/` to the configured data root; only an explicit `COPAL_DB=/path` override can make it use another location.

## Henxels

This repo uses `henxels` for repo-level agent guardrails. Copal's contract is insular:
do not import, sync, or share rules with OpenClank/OpenClaw or any other repo.

Install/run through package scripts:

```bash
bun install
bun run henxels:check
```

Core contract files are `henxels.yaml`, `AGENTS.md`, and `.henxels/`.
