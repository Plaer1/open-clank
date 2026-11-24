CREATE TABLE `goal_state` (
  `owner` text NOT NULL,
  `workspace` text NOT NULL,
  `project` text NOT NULL,
  `session_id` text NOT NULL,
  `revision` integer NOT NULL,
  `data` text NOT NULL,
  `time_updated` integer NOT NULL,
  PRIMARY KEY (`owner`, `workspace`, `project`, `session_id`)
);
--> statement-breakpoint
CREATE INDEX `goal_state_active_scope_idx`
  ON `goal_state` (`owner`, `workspace`, `project`);
--> statement-breakpoint
CREATE TABLE `goal_journal` (
  `id` text PRIMARY KEY NOT NULL,
  `owner` text NOT NULL,
  `workspace` text NOT NULL,
  `project` text NOT NULL,
  `session_id` text NOT NULL,
  `goal_id` text NOT NULL,
  `revision` integer NOT NULL,
  `event_type` text NOT NULL,
  `reason_code` text,
  `data` text NOT NULL,
  `time_created` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `goal_journal_scope_idx`
  ON `goal_journal` (`owner`, `workspace`, `project`, `session_id`, `time_created`);
--> statement-breakpoint
CREATE INDEX `goal_journal_goal_idx`
  ON `goal_journal` (`goal_id`, `revision`);
