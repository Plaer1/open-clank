CREATE TABLE `question_request` (
	`owner` text NOT NULL,
	`stable_chat_id` text NOT NULL,
	`request_id` text NOT NULL,
	`session_id` text NOT NULL REFERENCES `session`(`id`) ON DELETE CASCADE,
	`actor_id` text NOT NULL,
	`origin_turn_id` text REFERENCES `message`(`id`) ON DELETE SET NULL,
	`tool_message_id` text REFERENCES `message`(`id`) ON DELETE SET NULL,
	`tool_call_id` text,
	`question_revision` integer NOT NULL CHECK (`question_revision` >= 1),
	`plan_revision` integer,
	`blocking` integer NOT NULL DEFAULT 0 CHECK (`blocking` IN (0, 1)),
	`questions` text NOT NULL,
	`payload_hash` text NOT NULL,
	`state` text NOT NULL CHECK (`state` IN ('pending', 'answer-queued', 'answered', 'dismissed', 'stale', 'cancelled')),
	`answer_payload` text,
	`answer_disposition` text CHECK (`answer_disposition` IS NULL OR `answer_disposition` IN ('answered', 'dismissed')),
	`answer_request_id` text,
	`answer_message_id` text REFERENCES `message`(`id`) ON DELETE SET NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	PRIMARY KEY (`owner`, `stable_chat_id`, `request_id`),
	FOREIGN KEY (`owner`, `stable_chat_id`, `answer_request_id`) REFERENCES `user_input_receipt`(`owner`, `stable_chat_id`, `request_id`)
);
--> statement-breakpoint
CREATE INDEX `question_request_actor_state_idx` ON `question_request` (`session_id`, `actor_id`, `state`, `created_at`);
