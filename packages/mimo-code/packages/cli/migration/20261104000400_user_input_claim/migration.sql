ALTER TABLE `user_input_receipt` ADD `claimed_execution_revision` integer;
--> statement-breakpoint
CREATE TABLE `user_input_handoff` (
	`owner` text NOT NULL,
	`stable_chat_id` text NOT NULL,
	`request_id` text NOT NULL,
	`handoff_id` text NOT NULL,
	`session_id` text NOT NULL REFERENCES `session`(`id`) ON DELETE CASCADE,
	`actor_id` text NOT NULL,
	`expected_execution_revision` integer NOT NULL,
	`claimed_execution_revision` integer NOT NULL,
	`outcome` text NOT NULL CHECK (`outcome` IN ('claimed', 'running', 'settled-before-consumption', 'consumed')),
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	PRIMARY KEY (`owner`, `stable_chat_id`, `request_id`, `handoff_id`),
	FOREIGN KEY (`owner`, `stable_chat_id`, `request_id`) REFERENCES `user_input_receipt`(`owner`, `stable_chat_id`, `request_id`) ON DELETE CASCADE
);
--> statement-breakpoint
CREATE INDEX `user_input_handoff_actor_idx` ON `user_input_handoff` (`session_id`, `actor_id`, `claimed_execution_revision`);
