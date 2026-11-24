CREATE TABLE `actor_fork_context` (
	`session_id` text NOT NULL,
	`actor_id` text NOT NULL,
	`format_version` integer NOT NULL,
	`source_session_id` text NOT NULL,
	`source_actor_id` text NOT NULL,
	`source_watermark_message_id` text NOT NULL,
	`model_provider_id` text NOT NULL,
	`model_id` text NOT NULL,
	`snapshot_json` text NOT NULL,
	`snapshot_sha256` text NOT NULL,
	`snapshot_bytes` integer NOT NULL CHECK (`snapshot_bytes` >= 0),
	`time_created` integer NOT NULL,
	PRIMARY KEY (`session_id`, `actor_id`),
	FOREIGN KEY (`session_id`, `actor_id`) REFERENCES `actor_registry`(`session_id`, `actor_id`) ON DELETE CASCADE,
	FOREIGN KEY (`source_session_id`, `source_actor_id`) REFERENCES `actor_registry`(`session_id`, `actor_id`) ON DELETE CASCADE,
	FOREIGN KEY (`source_session_id`) REFERENCES `session`(`id`) ON DELETE CASCADE,
	CHECK (`session_id` != `source_session_id` OR `actor_id` != `source_actor_id`)
);
--> statement-breakpoint
CREATE INDEX `actor_fork_context_source_idx` ON `actor_fork_context` (`source_session_id`, `source_actor_id`);
