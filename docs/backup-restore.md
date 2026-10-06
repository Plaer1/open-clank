# Backup & Restore

`scripts/odysseus-backup` snapshots **the repository's `data/` tree**, with the
exclusions below. It does not follow the configured native data root, external
database settings, arbitrary host Locations or every document store. Verify
that repository `data/` is the actual live tree before relying on the helper.
A successful archive can still omit important state outside that tree.

## Coverage inventory

| Material | Covered by this helper? | Operator action |
| --- | --- | --- |
| App SQLite/auth/session/settings, `.app_key`, vault/provider credentials, managed uploads and resources | Only when physically inside repository `data/` | Preserve DBs and matching encryption keys together; inventory actual paths |
| Native `OPEN_CLANK_DATA_DIR` / legacy `ODYSSEUS_DATA_DIR`; frozen `~/.open-clank/data` | Only if it is that same repository `data/` tree | Back up the actual root independently; helper has no data-root flag |
| `DATABASE_URL` store | Only a file inside the snapshot tree | Preserve independent/historical DBs separately; current validated startup requires installation SQLite `app.db` |
| Frankenmemory `FM_DB_PATH` | Yes when inside the tree | Back up independent overrides separately |
| History `OPENCLANK_HISTORY_ROOT` / History settings override | Only when inside the tree | Preserve the complete actual History root and its policy/state |
| Hosted Copal Editor/Wiki Files-backed vault | Yes when its root is inside repository `data/`; source default is `DATA_DIR/copal-vaults` | Preserve the complete vault, including document files, `.copal` identity/revision/history/trash metadata and associated media; stop writers for a coherent copy |
| Independent `COPAL_LOOSE_ROOT` override | Only when physically inside the snapshot tree | Back up the entire actual root separately when outside repository `data/` |
| Older installed Copal Redb database/assets and standalone native stores | Historical source installations may use `packages/Copal/db` outside `data/` | Preserve the retained complete database/assets root separately; inventory any standalone Copal store too |
| Approved host Files Locations, source workspaces, exports, Lore repositories | Usually outside `data/` | Back up the actual files and recovery stores independently; app permissions do not make them part of the archive |
| `deep_research/` under the tree | Excluded by default | Add `--include-research` when required |
| `mail-attachments/` under the tree | Excluded by default | Add `--include-attachments`; custom `OPEN_CLANK_MAIL_ATTACHMENTS_DIR` / legacy alias roots need independent copies |
| Model caches/serve engines, integration state, provider client credentials and OS credential vaults | Only files inside the tree, subject to exclusions | Inventory separately; default Compose model caches live under mounted data, native caches often do not |
| Repository logs, external services, browser-local preferences and terminal profiles | Not generally covered | Export/back up the required independent state; secure credentials and private logs |

The helper copies SQLite files through SQLite's `.backup` API, stages an archive,
checksums/reopens it and validates recognized SQLite stores before publication.
That gives each copied SQLite database a coherent image; it does **not** give a
single transaction across several databases, JSON files, loose Copal vault
files/metadata/media, retained Redb stores or host files. SQLite may create normal WAL locking sidecars while reading a WAL source;
those sidecars are not archived beside the coherent database copy.

For a complete recoverable installation, stop all relevant writers and preserve
a coherent inventory of the actual stores and keys, including independent
roots. History/Lore version recovery and semantic memory are not substitutes
for independent backups. See [upgrade preflight](upgrading.md).

> **A snapshot contains your secrets.** The tarball includes the Fernet
> encryption key (`data/.app_key`), the vault, sessions, and any stored
> provider/API tokens — so treat it like a password. Store backups somewhere
> private, never commit them to Git, and prefer an encrypted destination when
> copying them offsite.

## Quick start

After verifying coverage, run the tool from the repository root. These commands
do not stop your service or capture independent stores:

```bash
# Create a snapshot → backups/odysseus-backup-<YYYYMMDD-HHMMSS>.tar.gz
./scripts/odysseus-backup snapshot

# List existing snapshots (most recent first)
./scripts/odysseus-backup list

# Check a tarball's integrity without extracting it
./scripts/odysseus-backup verify backups/odysseus-backup-20260101-120000.tar.gz

# Restore only after every Open Clank process is stopped (destructive)
./scripts/odysseus-backup restore backups/odysseus-backup-20260101-120000.tar.gz --yes --app-stopped
```

The script depends only on the Python standard library, so any `python3` on your
`PATH` will run it — you don't need the app's virtualenv active.

Every command prints a JSON result. Add `--pretty` for indented output.

## Commands

### `snapshot`

Writes a `tar.gz` of `data/` to `backups/<timestamp>.tar.gz`.

| Flag | Effect |
| --- | --- |
| `--out PATH` | Write to a specific path instead of the default `backups/` location. Must be **outside** `data/`. |
| `--include-research` | Include `data/deep_research/` (skipped by default — research runs are large). |
| `--include-attachments` | Include `data/mail-attachments/` (skipped by default — cached IMAP extractions, re-derivable). |

By default the snapshot includes files under repository `data/` **except**
`deep_research/` and `mail-attachments/`, with database sidecar handling and
manifest validation. Uploads/documents are included only if their actual files
are in that tree. The default loose Copal vault is included when `DATA_DIR` is
repository `data/`; independently overridden vault roots, retained older-version Redb stores
outside the tree and arbitrary host files are not.

```bash
# Snapshot straight to a mounted NAS path
./scripts/odysseus-backup snapshot --out /mnt/nas/odysseus-$(date +%F).tar.gz

# Full snapshot including research runs and mail attachments
./scripts/odysseus-backup snapshot --include-research --include-attachments
```

### `list`

Lists the tarballs in `backups/`, most recent first, with size and modification
time.

### `verify PATH`

Opens the tarball read-only, extracts it into a private temporary directory,
checks its member layout and manifest, and runs SQLite integrity and foreign-key
checks. Nothing is published to `data/`, and the temporary candidate is removed
after verification. Use this before relying on an old backup or after copying
one across machines.

### `restore PATH --yes --app-stopped`

Validates and atomically replaces **repository `data/`** from a tarball.
There is no destination/data-root flag; it does not restore configured external
stores or host workspaces. Reconcile the complete inventory before restarting.

> **Restore is destructive and requires a stopped app.** Stop every Open Clank
> process that can hold a database or data-file handle. `--yes` confirms the
> replacement; `--app-stopped` asserts that the writers are quiesced. The tool
> cannot verify that assertion for you. An already-open SQLite handle can keep
> writing the old inode after a filesystem swap and create split state.

Restore is not a blind delete. The archive is fully extracted and validated in
a private same-filesystem directory before the current tree is touched. On
Darwin and Linux, the tool atomically exchanges the validated candidate with
the current `data/`, then preserves the exact previous tree at a unique
`data.before-restore-<timestamp>-<id>` path. If publication or validation fails,
the old directory identity is restored. A failed/new tree that already crossed
the publication boundary is preserved at
`data.failed-restore-<timestamp>-<id>` for diagnosis; a candidate that never
crossed that boundary is discarded from private staging. A platform or
filesystem without atomic directory exchange fails before publication.
This restore path is limited to supported Darwin/Linux filesystems; it is not
a Windows restore recipe. Do not work around that refusal with a blind delete. Both
recovery-tree patterns are excluded from Git and Docker build contexts because
they contain the same secrets as `data/`.

If the filesystem also refuses or interrupts the rollback operations
themselves, the tool does not let temporary-directory cleanup erase the only
prior copy. It reports and retains that exact tree under the hidden
`.open-clank-restore-<id>/data` staging path beside the repository. Resolve the
underlying filesystem problem and move that directory back into place before
retrying. This is an emergency failure mode, not a successful restore.

Archives are validated entry-by-entry: absolute paths, `..` segments,
backslash traversal, symlinks/hardlinks, special files, duplicate or
case/Unicode-colliding paths, manifest drift, corrupt SQLite, and independently
published live sidecars are rejected. Version-1 manifests describe a closed
set of files, so undeclared directory members are rejected as well. Safe
pre-manifest archives remain
readable; when they contain a valid primary plus WAL/SHM, the staged copy is
consolidated through SQLite before publication so committed WAL-only state is
not discarded.

## Scheduling offsite backups

The tarball output composes cleanly with cron and any copy tool. For example, a
nightly repository-data archive written to a mounted destination (this alone
does not quiesce writers or cover independent stores):

```cron
0 3 * * *  cd /path/to/open-clank && ./scripts/odysseus-backup snapshot --out "/mnt/nas/odysseus-$(date +\%F).tar.gz"
```

After verifying the archive, copy it with `scp`, `rclone`, `s3cmd` or an
equivalent tool. `--out` is a filesystem path, not a command or remote URL.

## Deployment paths and rollback

The helper resolves `data/` and `backups/` from its repository root, not from
the current shell directory or the app's configured data root:

- **Source native:** repository `data/` is the default app root and normally
  contains the default loose Copal vault at `data/copal-vaults`. An overridden
  `COPAL_LOOSE_ROOT`, an older-version Redb store or approved host Locations may be
  elsewhere. Confirm the installed build's selected backend and actual roots;
  do not switch storage to fit the inventory.
- **Frozen/packaged:** the default persistent root is `~/.open-clank/data`.
  A source-tree helper does not automatically select it.
- **Compose:** the default host mount is `./data` → `/app/data`. If
  `APP_DATA_DIR` changes that mount, the helper still snapshots repository
  `data/`. Use the host to inventory/back up the real mount; do not assume
  containers contain the only state. `backups/` is not mounted by default.

Stop the actual deployment and all shared-store writers before restoring or
copying non-SQLite state. Preserve the matching prior code/build/configuration
alongside recovery copies. Restart only after the restored independent stores
and keys form a coherent set. The helper's retained `data.before-restore-*` tree
is rollback for repository `data/`, not for other roots or external services.

Chroma is retired as a live memory/vector authority. Preserve historical Chroma
payloads separately for explicit reviewed migration; they are not an optional
current backend. Frankenmemory defaults to a SQLite store under the resolved
data root. `NativeMemoryProvider` selection remains compatibility code in source
and does not re-enable Chroma. See [memory architecture](memory-architecture.md)
and explicit conversions (private evidence, kept locally).
