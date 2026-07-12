# ADR: canonical Copal event notes

Status: accepted and implemented 2026-07-10.

## Context

Copal previously had two writable truths: ordinary revisioned Redb notes and a
hidden `kind=planning` JSON document containing Timeline events and tracks. The
rich event editor, Timeline, Meatbag Tasks, Notes, graph views, export, and the
native Calendar projector could therefore disagree or erase fields through
partial whole-blob rewrites.

The existing Redb document layer already provides scoped identities, optimistic
heads, history, trash/restore, indexed frontmatter, import/export, and SSE. A
second event store or a database rewrite would duplicate those capabilities.

## Decision

An event is an ordinary `kind=markdown` Redb document. Its stable Redb document
id and head are the event identity and revision. Its Markdown body is the event
description; JSON-compatible frontmatter carries `copal_type: event` and typed
temporal/planning properties. Unknown legacy fields survive in `copal_extra`.

Tracks are entities in one hidden revisioned `kind=copal-tracks` document at
`.copal/tracks.json`. The registry stores track metadata and planning hints, but
never a second copy of events.

Timeline, Meatbag Tasks, Galaxy/Graph, search, Notes Properties, the event
editor, export, and Calendar projection all derive from those canonical event
documents. The rich editor and direct Timeline manipulation patch one event id
against one optimistic head through the shared planning route. A raw Notes edit
uses the normal document route against that same id/head.

## Property contract

Canonical properties preserve exact date sentinels and nullable semantics:

- exact `YYYY-MM-DD`, `FUZZY`, and `AUTO` starts;
- nullable/infinite due dates, fuzzy anchors, fade mode/days, and endpoint labels;
- status, priority, tags, primary track, shared track ids, and linked event id;
- ordered stages with stable ids, completion, and optional dates;
- legacy identity plus an unknown-field compatibility map.

Validation de-duplicates shared tracks, excludes the primary track from its own
shared list, preserves stable ids, and never coerces fuzzy, hammock, whisker, or
open-ended events into convenient hard dates.

## Conflicts and invalidation

Every mutation supplies a base head. A stale write returns the authoritative
document and an explicit `409`; it never silently overwrites. Notes keeps the
local text and offers explicit compare/load-latest/save-mine-over-latest paths.
Timeline/event-editor failures reload authoritative state. One shared Copal
cache and one EventSource own cross-window invalidation, with the mutating client
suppressing its matching SSE echo.

## Legacy migration

`POST /api/copal/planning/migrate` supports read-only dry run, deterministic
legacy-id mappings, resumable application, idempotent completion, and guarded
rollback. It writes a migration marker, one track registry, and one canonical
document per legacy event. The old planning document remains recoverable
evidence but becomes read-only once migration is applying or complete. Rollback
removes only migration-created records whose heads are unchanged; user-edited
records cause a visible conflict instead of data loss.

## Calendar and import/export boundaries

Native Calendar remains independent SQLite/CalDAV code. Copal projects canonical
temporal properties one way through the existing idempotent projector; Calendar
never reads Redb and is not another Copal write authority.

Obsidian-compatible import/export maps typed frontmatter at the boundary while
Redb remains the live store. Loose Markdown is an interchange format, not a
runtime source of truth.

## Consequences

- Notes and Timeline show and mutate the same record without dual-write or a
  reconciliation job.
- Event history, trash, conflict handling, links, search, and exports reuse the
  established document layer.
- The hidden track registry is still a canonical entity, but cannot duplicate
  event records.
- The legacy planning blob is retained for rollback/evidence and rejected as a
  writer after cutover.
- Native Calendar isolation and non-Copal events remain intact.
