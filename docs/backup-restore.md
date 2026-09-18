# Backup & Restore

Odysseus keeps all of your state in the `data/` directory — the SQLite database
(`app.db`), the Fernet encryption key (`data/.app_key`), the vault, memory, RAG
indexes, personal documents, and uploads. The `scripts/odysseus-backup` tool
snapshots that directory into a single gzip tarball and restores it later.

Snapshots are safe to take while the app is running: SQLite databases are copied
through SQLite's own `.backup` API rather than a raw file copy, so an in-flight
write can't corrupt the snapshot. Each published archive is first staged,
checksummed, reopened, and SQLite-validated; a failed snapshot never replaces
an existing output. SQLite may create its normal WAL locking sidecars while a
WAL-mode source is opened, but those runtime sidecars are not archived beside
the coherent database copy.

> **A snapshot contains your secrets.** The tarball includes the Fernet
> encryption key (`data/.app_key`), the vault, sessions, and any stored
> provider/API tokens — so treat it like a password. Store backups somewhere
> private, never commit them to Git, and prefer an encrypted destination when
> copying them offsite.

## Quick start

Run the tool from the repository root:

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

By default the snapshot includes everything under `data/` **except**
`deep_research/` and `mail-attachments/`. Personal uploads and documents are
included.

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

Validates and atomically replaces `data/` from a tarball.

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
filesystem without atomic directory exchange fails before publication. Both
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
nightly snapshot copied offsite:

```cron
0 3 * * *  cd /path/to/odysseus && ./scripts/odysseus-backup snapshot --out "/mnt/nas/odysseus-$(date +\%F).tar.gz"
```

Swap the `--out` target for `scp`, `rclone`, `s3cmd`, or similar to push the
snapshot to remote storage.

## Docker vs native installs

The tool reads `data/` and writes `backups/` relative to the repository root, so
where you run it matters:

- **Native installs** — run it from the repo root as shown above. `data/` and
  `backups/` are both in the repo directory.
- **Docker** — `docker-compose.yml` bind-mounts the host's `./data` to
  `/app/data`, so the live data is also present on the host. **Run the tool on
  the host** from the repo root; the snapshot reads the bind-mounted `./data` and
  writes to `./backups` on the host. Running it *inside* the container is not
  recommended, because `backups/` is not a mounted volume and the tarball would
  be lost when the container is recreated.

> **Legacy Chroma caveat.** The default install no longer starts ChromaDB and
> Frankenmemory's SQLite database is covered by the normal snapshot. If you
> explicitly run the optional legacy Chroma backend, its separate store is not
> part of the canonical backup; export or archive it separately before relying
> on that compatibility path.
>
> The canonical restore path is the SQLite `data/frankenmemory.db` snapshot;
> no Chroma restore is required for normal operation.
