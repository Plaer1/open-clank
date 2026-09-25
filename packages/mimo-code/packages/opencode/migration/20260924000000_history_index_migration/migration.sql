CREATE TABLE `history_index_migration` (
  `version` integer PRIMARY KEY NOT NULL,
  `phase` text NOT NULL,
  `cursor` integer NOT NULL,
  `fts_end` integer NOT NULL,
  `part_end` integer NOT NULL
);--> statement-breakpoint
INSERT INTO `history_index_migration` (`version`, `phase`, `cursor`, `fts_end`, `part_end`)
SELECT 1,
  CASE WHEN NOT EXISTS (SELECT 1 FROM `history_fts` LIMIT 1)
    AND NOT EXISTS (SELECT 1 FROM `part` LIMIT 1) THEN 'done' ELSE 'clean' END,
  0,
  COALESCE((SELECT MAX(rowid) FROM `history_fts`), 0),
  COALESCE((SELECT MAX(rowid) FROM `part`), 0);
