ALTER TABLE `user_input_receipt` ADD `attachment_upload_ids` text NOT NULL DEFAULT '[]';
--> statement-breakpoint
ALTER TABLE `user_input_receipt` ADD `attachment_parts` text NOT NULL DEFAULT '[]';
--> statement-breakpoint
ALTER TABLE `user_input_receipt` ADD `model_request_options` text NOT NULL DEFAULT '{}';
