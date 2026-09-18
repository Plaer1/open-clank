<!-- openclank-hexes:begin -->
## The contract (Open Clank Hexes)

_Generated from `.clankers/hexes/contract.yaml` by `openclank hex sync`; edit the canonical contract, not this block._

Before creating or changing a file, run `openclank hex explain <path>`.
The exact activated contract hash is the authority for project mutations.

### Rules

- Plans live in .clanker/futures/ and are Markdown: metaplan at .clanker/futures/<metaplan>.md and slices below the same canonical root. (in ./.clanker/futures/*)
- Clanker sidecar notes and audit evidence are Markdown under .clankers/robonotes/<topic>/, with subfolders and focused slices organized by domain, question, or decision. (in ./.clankers/robonotes/*)
  ↳ Keep the robonotes root for navigation. Continue older flat notes in the appropriate topic folder, linking back to historical evidence.
- Each robonotes topic keeps a concise index linking its slices and any related .clanker/futures/ plan; read the index and relevant slices on demand. (in ./.clankers/robonotes/*)
  ↳ Indexes orient the next session with current status, key decisions, and next steps; detailed evidence belongs in the linked slices. Do not load entire note collections into context.
- Slice robonotes at meaningful conceptual boundaries using judgment, without hard size limits; split when distinct concerns or accumulated detail make selective reading difficult. (in ./.clankers/robonotes/*)
  ↳ Keep each slice coherent and independently useful. Summarize findings and link sources or artifacts instead of accumulating raw logs, transcripts, inventories, or repeated evidence.
- Canonical Clanker paths reject legacy and singular/plural typo namespaces. (in ./*)
- Reference clones stay out of git (.references/ is study material). (in ./*)
- Process confinement is the OS boundary — no in-app sandbox broker or process-confinement gate. (in ./*)
- Claude is never attributed in commits — no AI co-author, generated-with, or session trailers. (in ./*)
- No credentials in first-party code. (in ./src/*, ./routes/*, ./services/*, ./scripts/*, ./config/*) _(warn)_
- Glue and memory code changes update canonical plans or robonotes. (in ./*) _(warn)_
- Canvas backgrounds retain one running owner when an unchanged pattern is reapplied. (in ./static/js/theme.js, ./tests/clanker_browser_acceptance.mjs)
  ↳ A duplicate animation loop or canvas remount presents as flicker.

### Behaviours

- deleting files or removing many lines requires an explicit consume-once blessing

Custom checks live in `.clankers/hexes/checks/*.py` and may not
replace built-ins. Activated runtime mutations execute them in the contained
policy worker; explicit local checks/hooks execute repository-owned checks.
<!-- openclank-hexes:end -->
