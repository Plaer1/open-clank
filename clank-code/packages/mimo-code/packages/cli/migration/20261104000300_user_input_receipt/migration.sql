CREATE TABLE `user_input_receipt` (
	`owner` text NOT NULL,
	`stable_chat_id` text NOT NULL,
	`map_revision` integer NOT NULL,
	`mapping_revision` integer NOT NULL,
	`workspace_revision` integer NOT NULL,
	`request_id` text NOT NULL,
	`session_id` text NOT NULL REFERENCES `session`(`id`) ON DELETE CASCADE,
	`actor_id` text NOT NULL,
	`admission_status` text NOT NULL CHECK (`admission_status` IN ('pending', 'running', 'idle')),
	`execution_revision` integer NOT NULL,
	`origin_turn_id` text,
	`payload_hash` text NOT NULL,
	`text` text NOT NULL,
	`attachment_refs` text NOT NULL,
	`question_refs` text NOT NULL,
	`mode` text NOT NULL CHECK (`mode` IN ('steer', 'after-turn')),
	`state` text NOT NULL CHECK (`state` IN ('persisted', 'pending-boundary', 'consumed', 'cancelled', 'failed')),
	`failure_reason` text,
	`message_id` text REFERENCES `message`(`id`) ON DELETE SET NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	`consumed_at` integer,
	PRIMARY KEY (`owner`, `stable_chat_id`, `request_id`)
);
--> statement-breakpoint
CREATE INDEX `user_input_receipt_pending_idx` ON `user_input_receipt` (`session_id`, `actor_id`, `state`, `created_at`);
