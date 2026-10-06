# Existing installations: upgrade preflight

Fresh installation commands in [setup](setup.md) do not define an upgrade or
migration for old data. Setup and the validated bootstrap create current empty
stores and validate existing ones. They do not silently convert every retired
provider table, auth document, native store or browser preference.

## Before changing an installation

1. Record the installed version/revision, OS/architecture, source or packaged
   deployment, active launch command/profile, port and data-root environment.
   Keep private environment values out of issues and screenshots.
2. Inventory the actual stores using [backup coverage](backup-restore.md): app
   database/auth/encryption key, provider data, Frankenmemory, the complete Copal
   loose vault (document files, `.copal` metadata and associated media), any
   retained Redb database/assets, History/Lore, mail attachments, integration
   state and host workspaces. Record the installed build's selected Copal backend;
   current hosted source uses only Files at `COPAL_LOOSE_ROOT` or
   `DATA_DIR/copal-vaults`. Back up an independent loose-root override separately,
   along with the actual Redb database/assets of any retained older installation. Resolve all
   overrides and Compose mounts. Do not assume `data/` is complete or change
   vault paths merely to match a source default.
3. Stop the actual deployment's writers before conversion/restore. CLI-managed
   service: `openclank server stop`; foreground launcher/bootstrap: Ctrl+C in its
   terminal; Compose: `docker compose stop`. Account for independent workers
   sharing those stores. A flag asserting the app is stopped does not stop it.
4. Preserve exact recoverable copies and verify them. Keep the matching
   encryption keys and independent roots together in the recovery inventory.
   Retain the old source/build and deployment settings needed for rollback.
5. Read the target revision's conversion requirements and each selected tool's
   current `--help`. Inspect exact paths/schema/status first; apply only the
   conversions required for this installation. Keep each tool's receipts and
   recovery archives. Do not substitute startup or a normal constructor for an
   explicit migration.
6. Update the selected source/build/dependencies and restart through its
   validated entrypoint. Check status/readiness, app login, model inventory and
   a small save/reopen workflow with disposable material. Retain rollback copies
   until the converted stores and important workflows have been checked.

## Explicit conversion is an operator action

Current source initializes fresh current stores and validates existing ones.
Historical provider/core/auth/native/browser formats can need reviewed,
installation-specific conversion. Conversion tools and receipts are private
operator recovery material and are not shipped by a normal source clone or
image. Obtain the tool matched to the source format and installed revision,
inspect its help and exact targets, and keep verified backups before applying.
There is no public universal command sequence and runtime does not discover
or execute private conversion tools automatically.

Use [backup coverage](backup-restore.md) to inventory independent stores.
Browser preferences belong to an exact browser origin: export before offline
conversion and review changed keys before importing. Preserve historic material
when a matching reviewed conversion is unavailable; an empty replacement is not
recovery. Keep credentials and conversion receipts out of source control and
release artifacts.

A frozen build refuses a legacy `~/.odysseus` home when the current
`~/.open-clank` home is absent. Use the reviewed home-root conversion rather than
creating an empty replacement and abandoning old data. Preserve historical
Chroma payloads for explicit conversion; Chroma is not an optional live backend.

Current bootstrap validates the installation's SQLite `app.db` under the
resolved data root. Core code still parses other `DATABASE_URL` values, but that
is not permission to deploy an external SQL database through the validated
startup path. Inventory any such historical stores before migrating.

## Deployment-specific boundaries and rollback

Source dependency refresh and a Compose rebuild may update code, but neither
moves or migrates existing stores. Keep intended volumes/overrides when
recreating containers. `update_windows.bat` is Docker-only: it pulls with
`--ff-only`, rebuilds/restarts Compose and prunes dangling images. It is not a
native Windows upgrader.

If checks fail, stop new writers and inspect the conversion receipt/recovery
archive. Restore the coherent set of stores and matching keys with the prior
build/configuration; do not point an old binary at a partially converted store.
The repository backup helper replaces repository `data/` only, on supported
atomic-exchange filesystems. Restoring its archive does not roll back independent
Copal loose vaults/metadata/media, retained Redb stores, History, workspace or
external database paths. See
[restore boundaries](backup-restore.md#restore-path---yes---app-stopped).
