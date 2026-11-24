-- Restore content classification on history_fts for consumers outside History.search.
-- History.search does not filter on kind; this column is write-side metadata only.
-- Existing rows get a placeholder; index rebuild rewrites kind via extract().
ALTER TABLE history_fts ADD COLUMN kind text NOT NULL DEFAULT '';--> statement-breakpoint
CREATE INDEX IF NOT EXISTS history_fts_kind_idx ON history_fts (kind);--> statement-breakpoint

-- Re-extract every part so kind is classified (user_text/assistant_text/reasoning/…).
-- Start at clean so orphans are dropped even when upgrading past v6; repair rewrites bodies+kind.
INSERT OR IGNORE INTO history_index_migration
SELECT 7, 'clean', 0,
  COALESCE((SELECT MAX(rowid) FROM history_fts), 0),
  COALESCE((SELECT MAX(rowid) FROM part), 0);--> statement-breakpoint

-- Close superseded migration state so prior versions do not linger as 'clean'.
UPDATE history_index_migration SET phase='done' WHERE version < 7;
