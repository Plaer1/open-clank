# Copal

Local-first knowledge vault and planning workspace.

## Henxels

This repo uses `henxels` for repo-level agent guardrails. Copal's contract is insular:
do not import, sync, or share rules with OpenClank/OpenClaw or any other repo.

Install/run through package scripts:

```bash
bun install
bun run henxels:check
```

Core contract files are `henxels.yaml`, `AGENTS.md`, and `.henxels/`.
