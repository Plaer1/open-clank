# Selected saves and sharing

## Capture and recover a selected save set

Identify the exact selected files, their roles and any known dependent files before capturing. Use the available authorized Files and History checkpoint owners; retain their resource identities, revisions, capture interval and receipts. A selected checkpoint observes an interval, not an atomic snapshot of a running process. Record whether the application was stopped or a consistent copy was actually established, how that was verified, and any files or process state that could not be included. If consistency is unknown, say so rather than calling the save complete.

Before restoring, inspect the exact retained version and current destination for each selected file, and compare the expected current fingerprint with the fresh authorized Files observation. Use the existing restore preview, confirmation and original status for each file; a changed or unavailable destination needs renewed review. Record completed, refused, partial and unknown outcomes separately, including any rollback or original status actually reported. Per-file restore does not imply an atomic set-wide rollback.

## Prepare a manifest and review for sharing

For the user's intended package or field note, list the exact outputs selected and known omissions. For each output record the source identity and the revision, hash and size actually reported by the authorized Files, History, artifact or export owner; mark missing observations unknown. Record available provenance, license and attribution evidence for included material, and identify any unresolved rights or attribution rather than inventing clearance. Review the selected bytes and metadata for private data using the authorized tools and checks available to the task; name unavailable checks and coverage limits. A human-reviewed manifest can be written through an authorized existing file or artifact tool. Recheck the selected revisions and results before relying on the review.

Use an existing export only for its actual output and scope. The browser preview source bundle, for example, covers declared static HTML dependencies and labels dynamic/runtime coverage partial; it is not a general mod package or rights validator. Existing knowledge exchange has its own review and admission. No general automatic package, license or privacy lint is implied by this workflow. A manifest, review, lint or build does not publish, push or share anything; use the separate action only when the user has authorized it.
