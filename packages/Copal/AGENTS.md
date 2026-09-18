# Copal agent instructions

> **Git etiquette — important.** Do **not** run `git add`, `git commit`, or
> `git push` yourself in this repo. When work is ready, stop and ask the user
> to review the diff and stage it. Staging on the user's behalf is a mistake
> here, even if the change looks correct.

<!-- openclank-hexes:begin -->
## The contract (Open Clank Hexes)

_Generated from `.clankers/hexes/contract.yaml` by `openclank hex sync`; edit the canonical contract, not this block._

Before creating or changing a file, run `openclank hex explain <path>`.
The exact activated contract hash is the authority for project mutations.

### Rules

- Copal Hexes are insular and must not import OpenClank/OpenClaw rules (in ./)
  ↳ Copal owns its own contract. Reference repositories remain evidence only.
- Copal repo contract and launch surface stay present (in ./)
- Copal source tree contains only app source filetypes (in ./src/*)
- Scripts are shell scripts (in ./scripts/*)
- Plans live in .clanker/futures/ and are Markdown (in ./.clanker/futures/*)
- Clanker sidecar notes and evidence live in .clankers/robonotes/ (in ./.clankers/robonotes/*)
- Reference clones stay out of git (in ./*)
- Claude is never attributed in commits (in ./*)
- Reference imports remain isolated from Copal source (in ./*)

### Behaviours

- deleting files or removing many lines requires an explicit consume-once blessing
- pushing requires an explicit consume-once blessing
- staging requires explicit owner confirmation

Custom checks live in `.clankers/hexes/checks/*.py` and may not
replace built-ins. Activated runtime mutations execute them in the contained
policy worker; explicit local checks/hooks execute repository-owned checks.
<!-- openclank-hexes:end -->
