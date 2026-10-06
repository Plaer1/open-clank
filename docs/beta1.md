# Open Clank Beta 1

Beta 1 is the first of several beta releases. macOS is where Open Clank is
currently built and dogfooded. Windows source support is working and tested:
Setup/Check, authenticated startup, Files, Editor save/reopen and History
restore, native image/video thumbnails and host application dispatch passed
focused checks. Shared fixes were also tested on macOS. Native ARM64 and x64
frozen release packages still need qualification; Linux remains unqualified.
Docker is not officially supported or tested. Retained Docker material is
unsupported legacy reference and does not gate the Beta 1 release.

**Beta 1 is a human release label. The machine version remains `1.0.2`.**
The installed revision identifies the actual source/build. These notes do not
announce a published GitHub tag, release asset or installer: publishing a release
is a separate step. CI artifact definitions are not platform acceptance.

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
