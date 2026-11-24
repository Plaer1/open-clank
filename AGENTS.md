<!-- openclank-hexes:begin -->
## The contract (Open Clank Hexes)

_Generated from `.clanker/hexes/contract.yaml` by `openclank hex sync`; edit the canonical contract, not this block._

Before creating or changing a file, run `openclank hex explain <path>`.
Enabled workspace Hexes enforce the current declarative rules; ordinary rule updates do not require version reactivation.
Agents need owner authorization to change their governing rules. Executable checks and hooks require separate trust.

### Rules

- Docker is not officially supported. Do not fix, build, test, smoke-test, qualify, publish or add release gates for Docker unless the user explicitly requests Docker work. (in ./*)
  ↳ Standing user decision, October 6, 2026. Docker is outside the roadmap, not deferred release work. Retained legacy files and historical checks do not imply support. Skip Docker-context checks; continue source/index/history/export privacy checks.
- Windows ARM64 builds, installers, qualification and related VM diagnostics are deferred until the user explicitly reopens that platform. Do not start them or make them release blockers for other platforms. (in ./*)
  ↳ Standing user decision, October 21, 2026; applies generally, not only to Beta 2. Preserve existing Windows ARM64 source, artifacts and evidence without further work. Windows x64 remains in scope; Apple Silicon macOS is unaffected.
- Plans live in .clanker/futures/ and are Markdown: metaplan at .clanker/futures/<metaplan>.md and slices below the same canonical root. (in ./.clanker/futures/*)
- Clanker sidecar notes and audit evidence are Markdown under .clanker/robonotes/, mirroring the relevant workspace path; cross-cutting notes use focused topic folders. (in ./.clanker/robonotes/*)
  ↳ Keep the robonotes root for navigation. Create sidecars on demand. Continue older flat notes in the appropriate mirrored or topic folder, linking back to historical evidence.
- Project analysis/provider-db directories contain only native SQLite analysis artifacts, their SQLite WAL/SHM companions and Markdown provenance; these are private derived artifacts, never executable inputs, credentials, donor payloads or another memory authority. (in ./.clanker/robonotes/?*/analysis/provider-db/?*)
- Each robonotes topic keeps a concise index linking its slices and any related .clanker/futures/ plan; read the index and relevant slices on demand. (in ./.clanker/robonotes/*)
  ↳ Indexes orient the next session with current status, key decisions, and next steps; detailed evidence belongs in the linked slices. Do not load entire note collections into context.
- Slice robonotes at meaningful conceptual boundaries using judgment, without hard size limits; split when distinct concerns or accumulated detail make selective reading difficult. (in ./.clanker/robonotes/*)
  ↳ Keep each slice coherent and independently useful. Summarize findings and link sources or artifacts instead of accumulating raw logs, transcripts, inventories, or repeated evidence.
- New Clanker writes use singular .clanker/ paths; historical plural artifacts remain readable during migration. (in ./*)
- Git is for deliberate GitHub repository work. Open Clank uses Lore for its own History features. (in ./*)
  ↳ Keep the application's snapshots, undo, and file history on its existing Lore integration. Do not create Git backups for agent work.
- Lore is software used by Open Clank. Leave Lore directories to Open Clank. All agents, including subagents and resumed workers, skip Lore setup, captures, receipts, and directory maintenance for their own work. Routine edits proceed without a Lore check. Keep agent notes in Markdown under .clanker/robonotes/. Handle Lore directories directly only when the owner explicitly requests work on them. (in ./*)
- Keep the private .clanker/ workspace and historical reference directories out of Git, including force-added files and submodules. (in ./*)
- Declared reference lifecycle records canonize durable provenance outside .clanker/references/ and use active, deferred, or retired state. (in ./.clanker/robonotes/references/lifecycle-*.md)
  ↳ Deferred material stays focused and resumable; active worktrees remain permitted; retired records prove their reference path is gone.
- First-party runtime and build surfaces must not depend on reference payloads in .clanker/references/ or legacy .references/. (in ./clank-code/src/openclank/**, ./clank-code/routes/**, ./clank-code/services/**, ./clank-code/scripts/**, ./clank-code/config/**, ./clank-code/packages/**/src/**, ./clank-code/package.json, ./clank-code/pyproject.toml, ./clank-code/setup.py, ./clank-code/requirements*.txt, ./clank-code/Dockerfile, ./clank-code/docker-compose*.yml, ./clank-code/*.plist)
  ↳ Reference material is temporary study input, never a runtime, build, or durable-evidence authority.
- Process confinement is the OS boundary — no in-app sandbox broker or process-confinement gate. (in ./*)
  ↳ Compare the staged source against Git’s empty tree. A forbidden identifier or a failed scan blocks the commit.
- Keep new Open Clank commits free of Claude co-author, generated-with and session trailers. Preserve original upstream author history. (in ./*)
  ↳ The two exclusions are the published Beta 2 ancestry and the immutable public MiMo import. First-party commits on merged branches are checked too.
- No credentials in first-party code. (in ./clank-code/src/*, ./clank-code/routes/*, ./clank-code/services/*, ./clank-code/scripts/*, ./clank-code/config/*) _(warn)_
- Record glue and Memery design changes in the relevant private metaplan or robonotes. (in ./clank-code/src/openclank/**, ./clank-code/native/memery/**, ./clank-code/src/memery*.py, ./clank-code/mcp_servers/**)
  ↳ Keep design evidence in the ignored .clanker/ workspace. The Git index cannot attest to private note updates.
- Canvas backgrounds retain one running owner when an unchanged pattern is reapplied. (in ./clank-code/static/js/theme.js, ./clank-code/static/js/graphics/**, ./clank-code/tests/theme_background_owner.mjs)
  ↳ Reapplying a live pattern must retain its actual canvas and scheduler. Exercise the public theme entry point in a disposable browser.
- Retired plans, Clanker artifacts and explicitly archived material live in .clanker/archive/; preserve their workspace-relative structure and link to replacements when known. (in ./.clanker/archive/*)
  ↳ Archive deliberately; preserve provenance and do not delete old material automatically.
- Workspace Hexes live in .clanker/hexes/contract.yaml with optional checks/; edit the contract and regenerate its AGENTS.md digest with openclank hex sync. (in ./.clanker/hexes/*)
  ↳ All .clanker/ content is private and Git-ignored, including Hex contracts. Existing plural-path data is a compatibility input; new writes use .clanker/.

Custom checks live in `.clanker/hexes/checks/*.py` and may not
replace built-ins. Activated runtime mutations execute them in the contained
policy worker; explicit local checks/hooks execute repository-owned checks.
<!-- openclank-hexes:end -->

# My Clanker workflow for Codex

These are the owner’s standing working agreements for this repository. Use repository-relative paths. The generated Hex contract above remains the canonical project-policy digest.

The application lives in `clank-code/`. Run source setup, development and packaging commands from that directory. GitHub configuration and the private `.clanker/` workspace are rooted at the Git checkout.

## Audit, plan, then implement

1. The parent researches the existing code and relevant references, makes the judgment calls, and records findings as it goes.
2. Write a short **metaplan** linking implementation slices. Organize slices by concern and dependency, not chronology. Each slice gives its outcome, scope/files, dependencies, owner/status, and meaningful acceptance criteria.
3. When I request planning first, finish the audit, notes and concrete plan, summarize it, and ask permission to release the implementation workers. Do not implement during that pass. Reuse approval already given; don't repeatedly ask for it.
4. Workers implement their assigned slices. The parent coordinates dependencies, reviews the results, integrates the work, and reports what changed, what was actually checked, and anything unfinished. Deployment/publication follows the authorization I gave; permission to implement is not automatically permission to deploy.

## Solbots and model choice

- “Solbots” means implementation subagents. When I request **6.1 Solbots**, explicitly select **`gpt-6.1-sol`** for them.
- A more expensive parent can supervise cheaper workers, but **I choose that arrangement**. Do not autonomously switch models or introduce cheaper workers.
- Without an explicit model choice, workers inherit the parent's model. Report an unavailable requested model instead of silently substituting.
- Read-only Luna workers are permitted only when I explicitly authorize them; the supervising agent keeps the judgment calls.
- Give each worker a bounded assignment, file ownership, relevant slice paths, and the required return evidence. All workers follow these same note rules. Prefer internal subagents over creating separate user-facing chats.

## Folder layout

```text
AGENTS.md                         durable working agreements
.clanker/                         private; Git-ignored
  futures/
    <topic>.md                    short metaplan / status / links
    <topic>/
      <concern>.md                individual plan slices
  robonotes/
    <topic-or-mirrored-path>/
      00-index.md                 current status, decisions, links
      <concern>.md                focused findings / implementation evidence
  references/                    temporary external study material
  artifacts/                     builds, screenshots, receipts, raw logs
  archive/                       deliberately retired material
  hexes/                         optional Open Clank policy configuration
```

Use singular `.clanker/`. Keep all of it private and Git-ignored. Never force-add references or make application/build code depend on reference copies. Put durable conclusions in notes with provenance. Archive deliberately, preserving relative paths and links; don't automatically delete old material.

## Slice notes by meaning

- Use small, coherent files with **one concern per slice**. Boundaries are conceptual and judgment-based, **not hard byte or line limits**.
- Mirror source paths for local concerns; use topic folders for cross-cutting work. Keep folder roots navigable.
- Indexes contain current status, decisions, next steps and links—not the entire investigation. Read only the index and relevant slices when resuming.
- Record concise conclusions, evidence pointers, uncertainties and decisions. Keep raw logs in artifacts. Avoid copied transcripts, repeated inventories and chronological status piles.
- Update the existing relevant slice; don't duplicate worker detail in the parent's notes. Keep plans and notes connected.

## Continuity and discipline

Lore is software used by Open Clank. Leave Lore directories to Open Clank. All agents, including subagents and resumed workers, skip Lore setup, captures, receipts, and directory maintenance for their own work. Routine edits proceed without a Lore check. Keep agent notes in Markdown under .clanker/robonotes/. Handle Lore directories directly only when the owner explicitly requests work on them.

Resume existing tasks and the **same workers** after interruptions; resend their unfinished assignments when needed. Don't replace them, redo finished work, or silently switch models. Report a specific recovery failure. Never create automatic worker-restart loops. An explicit pause persists until I authorize resumption.

Use one agreed integration branch. Give workers disjoint ownership; create extra branches/worktrees only when necessary and track their integration/retirement. Preserve existing user work. Don't claim everything is consolidated without evidence.

Run focused checks appropriate to actual changes. Don't create a sprawling test campaign, repeatedly rerun passing checks, or turn debugging the test machinery into the project. Clearly distinguish implemented, checked, built, deployed and published.

If this repository already uses Open Clank Hexes, run `openclank hex explain <path>` before project writes. Edit canonical `.clanker/hexes/contract.yaml` and regenerate the digest with `openclank hex sync`; rule changes require owner authorization and executable hooks/checks need separate trust. Without Hexes, use `AGENTS.md` directly—don't invent a new policy stack.
