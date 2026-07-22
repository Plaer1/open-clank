<!-- henxels:begin -->
## The contract (henxels)

_Auto-generated from `henxels.yaml` by `henxels sync`. Do not edit by hand._

Each bullet is a **henxel** (a rule). To disobey one, change `henxels.yaml` —
that is the only sanctioned escape. Run `henxels explain <path>` before creating
a file to see what governs that spot.

Only use `git commit --no-verify` in a genuine emergency: it bypasses the hooks that run this contract, the safety mechanism meant to protect the repository. Prefer `henxels bless <action>` or editing `henxels.yaml` — both keep the deviation visible.

> **Git etiquette — important.** Do **not** run `git add`, `git commit`, or `git push` yourself in this repo. When your work is ready, stop and ask the user to review the diff and stage it. Staging on the user's behalf is a mistake here, even if the change looks correct.
>
> _OpenCode agents:_ run `henxels integrate opencode` once to make this **enforced** (it hard-blocks `git add`/`git commit`), not just advisory.

### Rules

- Copal henxels are insular and must not import OpenClank/OpenClaw rules (in ./)
  ↳ This repository owns its own contract. Do not copy, inherit, or synchronize rules from OpenClank, OpenClaw, or any other repo; reference repos stay evidence only.
- Copal repo contract and launch surface stay present (in ./)
  ↳ Copal must remain locally launchable and self-describing without borrowing another repo's rules.
- Copal source tree contains only app source filetypes (in ./src/*)
  ↳ Application source lives under src/; vendored references and research imports must not drift into runtime source.
- Scripts are shell scripts (in ./scripts/*)
  ↳ Repo automation here is small, reviewable shell glue.
- Plans live in .futures/ and are markdown (in ./.futures/*)
- Clanker sidecar notes (test instructions, run records, audits) live in robonotes/ and are markdown — never in .claude/ (in ./robonotes/*)
- Reference clones stay out of git (references/ is study material, not product)
- Claude is never attributed in commits — no AI co-author, generated-with, or session trailers
- Reference imports remain isolated from Copal source
  ↳ External projects and audits are references only; Copal code must not become a hidden fork of them.

### Behaviours

- **never `git add` / `git commit` / `git push` yourself** — ask the user to review and stage
- push is blocked until `henxels bless push`
- deleting files / removing many lines is blocked until `henxels bless delete`
- warns when a new file looks like a near-copy of a committed one

### Custom henxels & contributing

**Before writing a custom check, run `henxels catalogue` and use the built-in that
matches your intent — don't reinvent one.** Never name a custom check after a built-in
or a setting (e.g. `warn_about_large_files` is a setting, not a check).

Need a check that genuinely doesn't exist? `henxels create-new-statement <name>` scaffolds a local check
(auto-loaded from `henxels_checks.py`). **If your check is reusable** — useful in
other repos, not tied to this one — contribute it upstream with `henxels contribute`.
We're in the agentic era: send a ready-to-merge PR instead of opening an issue.
<!-- henxels:end -->
