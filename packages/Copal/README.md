# Copal

Local-first knowledge vault and planning workspace.

Open Clank’s current hosted Copal uses only its Files-backed vault. Older installed revisions may retain Redb stores; those require their matching revision and explicit recovery/conversion guidance. Hosted Copal and the standalone Servo shell use separate physical stores. The standalone shell appends `standalone/` to the configured data root; only an explicit `COPAL_DB=/path` override can make it use another location.

## Open Clank Hexes

This repo uses Open Clank's first-party Hexes engine for repo-level agent
guardrails. Copal's contract is insular:
do not import, sync, or share rules with OpenClank/OpenClaw or any other repo.

Install/run through package scripts:

```bash
bun install
bun run hexes:check
```

The historical package-local plural Hex bundle and generated `AGENTS.md`
remain compatibility input pending a separate reviewed retirement. New private
contract work uses singular `.clanker/`; all such directories are Git-ignored
and excluded from publication, including contracts and local checks. Keep
Copal’s contract insular and do not import another repository’s rules. The evaluator is provided by
Open Clank and has no runtime dependency on the Henxels package.
