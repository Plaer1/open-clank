# Copal

Local-first knowledge vault and planning workspace.

OpenClank's hosted Copal and the standalone Servo shell use separate default physical stores. The standalone shell appends `standalone/` to the configured data root; only an explicit `COPAL_DB=/path` override can make it use another location.

## Open Clank Hexes

This repo uses Open Clank's first-party Hexes engine for repo-level agent
guardrails. Copal's contract is insular:
do not import, sync, or share rules with OpenClank/OpenClaw or any other repo.

Install/run through package scripts:

```bash
bun install
bun run hexes:check
```

Core contract files are `.clankers/hexes/contract.yaml`, `AGENTS.md`, and the
package-local `.clankers/hexes/` bundle. The root `.hex` and manual filename are
bounded compatibility readers until retirement. The evaluator is provided by
Open Clank and has no runtime dependency on the Henxels package.
