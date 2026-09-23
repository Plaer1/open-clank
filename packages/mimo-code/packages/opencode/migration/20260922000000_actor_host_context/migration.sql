ALTER TABLE `actor_registry` ADD COLUMN `host_account_id` text;
--> statement-breakpoint
ALTER TABLE `actor_registry` ADD COLUMN `provider_grant_id` text;
--> statement-breakpoint
ALTER TABLE `actor_registry` ADD COLUMN `grant_revision` integer;
--> statement-breakpoint
ALTER TABLE `actor_registry` ADD COLUMN `credential_revision` integer;
--> statement-breakpoint
ALTER TABLE `actor_registry` ADD COLUMN `chat_id` text;
--> statement-breakpoint
ALTER TABLE `actor_registry` ADD COLUMN `workspace_id` text;
--> statement-breakpoint
ALTER TABLE `actor_registry` ADD COLUMN `cwd` text;
--> statement-breakpoint
ALTER TABLE `actor_registry` ADD COLUMN `goal_id` text;
