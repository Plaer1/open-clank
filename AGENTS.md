<!-- henxels:begin -->
## The contract (henxels)

_Auto-generated from `henxels.yaml` by `henxels sync`. Do not edit by hand._

Each bullet is a **henxel** (a rule). To disobey one, change `henxels.yaml` —
that is the only sanctioned escape. Run `henxels explain <path>` before creating
a file to see what governs that spot.

Only use `git commit --no-verify` in a genuine emergency: it bypasses the hooks that run this contract, the safety mechanism meant to protect the repository. Prefer `henxels bless <action>` or editing `henxels.yaml` — both keep the deviation visible.

### Rules

- Plans live in .futures/ and are markdown (in ./.futures/*)
- Clanker sidecar notes (test instructions, run records, audits) live in robonotes/ and are markdown — never in .claude/ (in ./.robonotes/*)
- Reference clones stay out of git (.references/ is study material, not product)
- Claude is never attributed in the release commit — inherited history is left intact
- No credentials in first-party code (in ./src/*, ./routes/*, ./services/*, ./scripts/*, ./config/*) _(warn)_
- Glue/memory code changes update plans or robonotes (write it down as you go) _(warn)_

### Behaviours

- deleting files / removing many lines is blocked until `henxels bless delete`

### Custom henxels & contributing

**Before writing a custom check, run `henxels catalogue` and use the built-in that
matches your intent — don't reinvent one.** Never name a custom check after a built-in
or a setting (e.g. `warn_about_large_files` is a setting, not a check).

Need a check that genuinely doesn't exist? `henxels create-new-statement <name>` scaffolds a local check
(auto-loaded from `henxels_checks.py`). **If your check is reusable** — useful in
other repos, not tied to this one — contribute it upstream with `henxels contribute`.
We're in the agentic era: send a ready-to-merge PR instead of opening an issue.
<!-- henxels:end -->
