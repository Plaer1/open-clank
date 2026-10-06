<!-- openclank-hexes:begin -->
## The contract (Open Clank Hexes)

_Generated from `.clanker/hexes/contract.yaml` by `openclank hex sync`; edit the canonical contract, not this block._

Before creating or changing a file, run `openclank hex explain <path>`.
The exact activated contract hash is the authority for project mutations.

### Rules

- Docker is not officially supported. Do not fix, build, test, smoke-test, qualify, publish or add release gates for Docker unless the user explicitly requests Docker work. (in ./*)
  ↳ Standing user decision, October 6, 2026. Docker is outside the roadmap, not deferred release work. Retained legacy files and historical checks do not imply support. Skip Docker-context checks; continue source/index/history/export privacy checks.
- Plans live in .clanker/futures/ and are Markdown: metaplan at .clanker/futures/<metaplan>.md and slices below the same canonical root. (in ./.clanker/futures/*)
- Clanker sidecar notes and audit evidence are Markdown under .clanker/robonotes/, mirroring the relevant workspace path; cross-cutting notes use focused topic folders. (in ./.clanker/robonotes/*)
  ↳ Keep the robonotes root for navigation. Create sidecars on demand. Continue older flat notes in the appropriate mirrored or topic folder, linking back to historical evidence.
- Each robonotes topic keeps a concise index linking its slices and any related .clanker/futures/ plan; read the index and relevant slices on demand. (in ./.clanker/robonotes/*)
  ↳ Indexes orient the next session with current status, key decisions, and next steps; detailed evidence belongs in the linked slices. Do not load entire note collections into context.
- Slice robonotes at meaningful conceptual boundaries using judgment, without hard size limits; split when distinct concerns or accumulated detail make selective reading difficult. (in ./.clanker/robonotes/*)
  ↳ Keep each slice coherent and independently useful. Summarize findings and link sources or artifacts instead of accumulating raw logs, transcripts, inventories, or repeated evidence.
- New Clanker writes use singular .clanker/ paths; historical plural artifacts remain readable during migration. (in ./*)
- Project files may be modified or deleted without a separate blessing when their exact preimages are recoverable from Git or backed up in Lore; otherwise obtain an explicit consume-once blessing before the mutation. (in ./*)
  ↳ Git and Lore are recovery authorities. Capture the recoverable preimage before mutating uncommitted or untracked work.
- Reference material in .clanker/references/ must never be tracked by Git, including force-added files and submodules. (in ./*)
- Declared reference lifecycle records canonize durable provenance outside .clanker/references/ and use active, deferred, or retired state. (in ./.clanker/robonotes/references/lifecycle-*.md)
  ↳ Deferred material stays focused and resumable; active worktrees remain permitted; retired records prove their reference path is gone.
- First-party runtime and build surfaces must not depend on reference payloads in .clanker/references/ or legacy .references/. (in ./src/openclank/**, ./routes/**, ./services/**, ./scripts/**, ./config/**, ./packages/**/src/**, ./package.json, ./pyproject.toml, ./setup.py, ./requirements*.txt, ./Dockerfile, ./docker-compose*.yml, ./*.plist)
  ↳ Reference material is temporary study input, never a runtime, build, or durable-evidence authority.
- Process confinement is the OS boundary — no in-app sandbox broker or process-confinement gate. (in ./*)
- Claude is never attributed in commits — no AI co-author, generated-with, or session trailers. (in ./*)
- No credentials in first-party code. (in ./src/*, ./routes/*, ./services/*, ./scripts/*, ./config/*) _(warn)_
- Glue and memory code changes update canonical plans or robonotes. (in ./*) _(warn)_
- Canvas backgrounds retain one running owner when an unchanged pattern is reapplied. (in ./static/js/theme.js, ./tests/clanker_browser_acceptance.mjs)
  ↳ A duplicate animation loop or canvas remount presents as flicker.
- Retired plans, Clanker artifacts and explicitly archived material live in .clanker/archive/; preserve their workspace-relative structure and link to replacements when known. (in ./.clanker/archive/*)
  ↳ Archive deliberately; preserve provenance and do not delete old material automatically.
- Workspace Hexes live in .clanker/hexes/contract.yaml with optional checks/; edit the contract and regenerate its AGENTS.md digest with openclank hex sync. (in ./.clanker/hexes/*)
  ↳ All .clanker/ content is private and Git-ignored, including Hex contracts. Existing plural-path data is a compatibility input; new writes use .clanker/.

Custom checks live in `.clanker/hexes/checks/*.py` and may not
replace built-ins. Activated runtime mutations execute them in the contained
policy worker; explicit local checks/hooks execute repository-owned checks.
<!-- openclank-hexes:end -->
