# Open Clank Beta 1

Beta 1 is the first of several beta releases. macOS is where Open Clank is
currently built and dogfooded. The macOS package targets Apple Silicon on
macOS 15 or later. Its pyramid-and-eye menu-bar icon
owns the app lifecycle; there is no Dock icon. It is ad-hoc signed and not
notarized, with no Intel/universal claim. Windows source support is working and tested:
Setup/Check, authenticated startup, Files, Editor save/reopen and History
restore, native image/video thumbnails and host application dispatch passed
focused checks. Shared fixes were also tested on macOS. Install only the native
architectures offered by the published release, and check each artifact's
provenance for its source and target. Linux release support is coming soon and
remains unqualified.
Docker is not officially supported or tested. Retained Docker material is
unsupported legacy reference and does not gate the Beta 1 release.

**Beta 1 is a human release label. The machine version remains `1.0.2`.**
The installed revision identifies the actual source/build. These notes do not
announce a published application release or downloadable installer. Application
downloads become available when `v1.0.2-beta.1` is published. The verified single
offline-artwork part and manifests are already public in the separate
[`v1.0.2-beta.1-artwork-compact` release](https://github.com/Plaer1/open-clank/releases/tag/v1.0.2-beta.1-artwork-compact).
Its 449 MB pack retains all available Kitchen combinations as 160px lossless
WebP and the original Google SVG bytes. Use the matching compact-schema app
or source revision; replacing an older app's pack alone is not an upgrade.
CI artifact definitions are not platform acceptance.

## Current workspace

- Files is the common browser/library entry. **File → New file…** opens the
  destination/name flow and Editor handoff. Calendar is in the Copal group.
- Editor handles documents and source, with tabs/splits, templates, tables and
  source comments. Wiki provides linked pages, rich authoring and section embeds.
- **Settings → Help** offers **Wiki documentation** and **Copal handbook**
  (the Editor presentation) over the same maintained official articles.
  Official pages are read-only. Practice examples in a new personal Wiki page
  or Editor document.
- Chat, Add Models, Imps, Usage, Memery/Skills, tasks, calendar/email, TreeHouse,
  themes, Hexes and History are part of the workspace. Their useful behavior
  depends on configured providers, permissions, native workers and supported
  data. See the handbook for individual tasks and limits.

## Treehouse learning

**Tutorials** are click-through courses without required work. **Quests** have
required activities and completion criteria; missions are objectives within
Quests. Reading a Quest does not complete its required work. Authoring includes
rich lessons, preview and publication. Learners save drafts, submit text or
Files-authorized material, receive instructor feedback and can retry after a
correction. Protected file evidence retains account, reviewer and attempt scope.

The learning library includes collections, scoped discussion and shared boards,
personal completion records and course-scoped podcasts. Achievements update
from supported producers and learning, with themed icons, details and progression.
The gallery accepts artwork packs and offers a mascot source kit. Stats record
supported activity and admitted foreground observations, without leaderboards
or fabricated historical totals. Fresh databases initialize stats automatically.
An older database missing stats needs a backed-up offline operator activation;
creating a new account in that database does not activate them. The private
activation tool is not shipped, and startup does not migrate those stores.

Treehouse has bounded authenticated source-journey evidence; consult the
release artifact provenance for installed-package coverage. Browser JavaScript and choices are
practice, not trusted assessment grading. H5P/SCORM, richer AI/RAG and modalities,
other-language execution, trusted code autograding and learning automation
remain [explicit limitations](known-limits.md#treehouse-learning). This is not
full upstream learning-platform parity.

The inherited Odysseus document editor is no longer an improvement target.
Editor and Wiki own future authoring work. Existing document data and email
compatibility paths remain; this disposition does not remove data or mail flows.

These are current feature descriptions, not a claim that every workflow or
operating system has passed release acceptance. Media captions describe visible
states, and setup/empty screens do not prove a completed backend or OS action.

## Install and upgrade boundaries

Use [setup](setup.md) for fresh installs. Existing data requires
[the upgrade preflight](upgrading.md) and [verified backups](backup-restore.md)
of the actual stores. Startup/setup is not a universal legacy-data upgrader.
Provider credentials are managed through the current account connection flow;
retired provider environment variables and incomplete old stores can fail
validation until an operator performs the appropriate reviewed conversion.

Authentication is always required, including localhost. App scopes and
interactive approvals do not confine OS processes: tool execution inherits the
server account's OS access. See [SECURITY.md](../SECURITY.md).

## Dogfooding and reporting

Use targeted workflows with disposable material when investigating creation,
save/reopen, copies, version recovery, imports or provider connections. Keep
rollback evidence before conversions and restores. Memory recall, History and
Lore recovery each have boundaries and do not replace independent backups.

Report problems through [GitHub issues](https://github.com/Plaer1/open-clank/issues)
with machine version, installed revision, OS/architecture, installation method,
browser, relevant provider/backend, steps and the observed result. Remove keys,
account details, private paths, documents and conversations from shared evidence.
For vulnerabilities use [private reporting](../SECURITY.md#reporting).

[Known limits](known-limits.md) · [Setup](setup.md) · [Contributing](../CONTRIBUTING.md)
