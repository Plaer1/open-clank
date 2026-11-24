-- Dual-column search: body = original preview (display), body_idx = CJK-isolated
-- search form. FTS MATCH runs on body_idx so short CJK works without instr.
ALTER TABLE history_fts ADD COLUMN body_idx text NOT NULL DEFAULT '';--> statement-breakpoint

DROP TRIGGER IF EXISTS `history_fts_ai`;--> statement-breakpoint
DROP TRIGGER IF EXISTS `history_fts_ad`;--> statement-breakpoint
DROP TRIGGER IF EXISTS `history_fts_au`;--> statement-breakpoint
DROP TABLE IF EXISTS `history_fts_idx`;--> statement-breakpoint

CREATE VIRTUAL TABLE `history_fts_idx` USING fts5(
  body_idx,
  content='history_fts',
  content_rowid='rowid',
  tokenize='unicode61'
);--> statement-breakpoint

-- Normal write path keeps triggers. migrateIndexBatch drops them for clean/repair
-- (external-content FTS must not see delete/insert for rows never indexed) and
-- recreates them in finalize after `rebuild`.
CREATE TRIGGER `history_fts_ai` AFTER INSERT ON `history_fts` BEGIN
  INSERT INTO `history_fts_idx`(rowid, body_idx) VALUES (NEW.rowid, NEW.body_idx);
END;--> statement-breakpoint
CREATE TRIGGER `history_fts_ad` AFTER DELETE ON `history_fts` BEGIN
  INSERT INTO `history_fts_idx`(`history_fts_idx`, rowid, body_idx) VALUES('delete', OLD.rowid, OLD.body_idx);
END;--> statement-breakpoint
CREATE TRIGGER `history_fts_au` AFTER UPDATE ON `history_fts` BEGIN
  INSERT INTO `history_fts_idx`(`history_fts_idx`, rowid, body_idx) VALUES('delete', OLD.rowid, OLD.body_idx);
  INSERT INTO `history_fts_idx`(rowid, body_idx) VALUES (NEW.rowid, NEW.body_idx);
END;--> statement-breakpoint

-- v8: background re-extract fills body_idx + kind (no sync rebuild).
INSERT OR IGNORE INTO history_index_migration
SELECT 8, 'clean', 0,
  COALESCE((SELECT MAX(rowid) FROM history_fts), 0),
  COALESCE((SELECT MAX(rowid) FROM part), 0);
