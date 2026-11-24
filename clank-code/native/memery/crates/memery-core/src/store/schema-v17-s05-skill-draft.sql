-- Offline-only skill-draft composition over the existing paired S05/S08 trigger profile.
DROP TRIGGER fm_v2_s05_attempt_insert_guard;
CREATE TRIGGER fm_v2_s05_attempt_insert_guard BEFORE INSERT ON fm_v2_attempts
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review','access_consolidation','skill_draft'))
 AND (NEW.state!='leased' OR NOT EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.state='queued'))
BEGIN SELECT RAISE(ABORT,'S05 attempt requires queued job'); END;
DROP TRIGGER fm_v2_s05_reservation_initial_guard;
CREATE TRIGGER fm_v2_s05_reservation_initial_guard BEFORE INSERT ON fm_v2_s05_reservations
WHEN NEW.state!='reserved' OR NOT EXISTS (
 SELECT 1 FROM fm_v2_attempts a JOIN fm_v2_jobs j ON j.owner_id=a.owner_id AND j.job_id=a.job_id
 JOIN fm_v2_s05_occupancy o ON o.singleton=1
 WHERE a.owner_id=NEW.owner_id AND a.attempt_id=NEW.attempt_id AND a.job_id=NEW.job_id AND a.lease_epoch=NEW.lease_epoch
 AND a.state='leased' AND j.kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review','access_consolidation','skill_draft') AND j.state='queued'
 AND o.verified=1 AND o.coverage_generation=NEW.coverage_generation AND o.clock_generation=NEW.clock_generation AND o.foreground_epoch=NEW.foreground_epoch
 AND NOT EXISTS (SELECT 1 FROM fm_v2_s05_foreground f WHERE f.state IN ('active','unknown')))
BEGIN SELECT RAISE(ABORT,'S05 reservation unavailable'); END;
DROP TRIGGER fm_v2_s05_job_active_guard;
CREATE TRIGGER fm_v2_s05_job_active_guard BEFORE UPDATE OF state,current_attempt_id ON fm_v2_jobs
WHEN NEW.kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review','access_consolidation','skill_draft') AND NEW.state='active' AND NOT EXISTS (
 SELECT 1 FROM fm_v2_s05_reservations r JOIN fm_v2_attempts a ON a.owner_id=r.owner_id AND a.attempt_id=r.attempt_id
 WHERE r.owner_id=NEW.owner_id AND r.job_id=NEW.job_id AND r.attempt_id=NEW.current_attempt_id AND r.lease_epoch=a.lease_epoch AND r.state='reserved' AND a.state='leased')
BEGIN SELECT RAISE(ABORT,'S05 job requires reserved attempt'); END;
DROP TRIGGER fm_v2_s05_attempt_transition_guard;
CREATE TRIGGER fm_v2_s05_attempt_transition_guard BEFORE UPDATE OF state ON fm_v2_attempts
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review','access_consolidation','skill_draft'))
 AND OLD.state!=NEW.state AND NOT EXISTS (
 SELECT 1 FROM fm_v2_s05_reservations r WHERE r.owner_id=NEW.owner_id AND r.attempt_id=NEW.attempt_id AND r.job_id=NEW.job_id AND r.lease_epoch=NEW.lease_epoch
 AND ((NEW.state='running' AND r.state='dispatched') OR (NEW.state='fenced' AND r.state='revoked') OR (NEW.state IN ('succeeded','failed_retryable','failed_terminal','cancelled') AND r.state='settled')))
BEGIN SELECT RAISE(ABORT,'S05 attempt requires matching reservation'); END;
DROP TRIGGER fm_v2_s05_job_terminal_guard;
CREATE TRIGGER fm_v2_s05_job_terminal_guard BEFORE UPDATE OF state ON fm_v2_jobs
WHEN NEW.kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review','access_consolidation','skill_draft') AND OLD.state='active' AND NEW.state IN ('retry_wait','succeeded','failed_terminal','cancelled') AND NOT EXISTS (
 SELECT 1 FROM fm_v2_s05_reservations r JOIN fm_v2_attempts a ON a.owner_id=r.owner_id AND a.attempt_id=r.attempt_id AND a.lease_epoch=r.lease_epoch
 WHERE r.owner_id=NEW.owner_id AND r.job_id=NEW.job_id AND r.attempt_id=OLD.current_attempt_id
 AND ((NEW.state='retry_wait' AND ((r.state='revoked' AND a.state='fenced') OR (r.state='settled' AND a.state='failed_retryable'))) OR
      (NEW.state='succeeded' AND r.state='settled' AND a.state='succeeded') OR
      (NEW.state='failed_terminal' AND r.state='settled' AND a.state='failed_terminal') OR
      (NEW.state='cancelled' AND r.state='settled' AND a.state='cancelled')))
BEGIN SELECT RAISE(ABORT,'S05 job outcome requires matching attempt'); END;
DROP TRIGGER fm_v2_s05_job_insert_guard;
CREATE TRIGGER fm_v2_s05_job_insert_guard BEFORE INSERT ON fm_v2_jobs
WHEN NEW.kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review','access_consolidation','skill_draft') AND (NEW.state!='queued' OR NEW.current_attempt_id IS NOT NULL OR NEW.attempt_count!=0)
BEGIN SELECT RAISE(ABORT,'S05 jobs must begin queued'); END;
DROP TRIGGER s08_reservation_shape;
CREATE TRIGGER s08_reservation_shape BEFORE INSERT ON fm_v2_s05_reservations
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND
 ((j.kind='access_consolidation' AND NOT (
   (NEW.connection_id='local:rules' AND NEW.reserved_calls=0 AND NEW.charged_calls=0 AND NEW.reserved_input_tokens=0 AND NEW.reserved_output_tokens=0 AND NEW.charged_input_tokens=0 AND NEW.charged_output_tokens=0)
   OR (NEW.connection_id!='local:rules' AND NEW.reserved_calls=1 AND NEW.charged_calls=1 AND NEW.reserved_output_tokens>=1 AND EXISTS (
      SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection' AND m.input_hash=j.input_hash AND m.content_hash=j.input_hash AND m.tool_hash IN ('c3649be7a7de38fe6f67977aacccc0b7e2b2e57d85569981a9de6ab3ac5f9535','baa17159592f3e568139de3b984c32872f79766d9655e79883f98b1489409a38','c123a15249928e7bda788a9c36e4055e6f3c4e24933a1af2341c21fab06fcad2','16ba601b83145894b795e5ec9dd4c7fd369638c7366d1d3508479350a40ed9b7','2318970f948a5c7e3b15b7a14826c602ffa97f70fd09f4a7b0151e7beb116304','e7404bf2b2ea01c116baf0272e287ae2f76422aa3a747a38c09fff8083b472f4'))))) OR
  (j.kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review','skill_draft') AND (NEW.reserved_calls!=1 OR NEW.charged_calls!=1 OR NEW.reserved_output_tokens<1))))
BEGIN SELECT RAISE(ABORT,'S08 reservation accounting unavailable'); END;
DROP TRIGGER s08_reservation_shape_update;
CREATE TRIGGER s08_reservation_shape_update BEFORE UPDATE ON fm_v2_s05_reservations
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND
 ((j.kind='access_consolidation' AND (NEW.reserved_seconds!=OLD.reserved_seconds OR NOT (
   (NEW.connection_id='local:rules' AND NEW.reserved_calls=0 AND NEW.charged_calls=0 AND NEW.reserved_input_tokens=0 AND NEW.reserved_output_tokens=0 AND NEW.charged_input_tokens=0 AND NEW.charged_output_tokens=0)
   OR (NEW.connection_id!='local:rules' AND NEW.reserved_calls=1 AND (NEW.charged_calls=1 OR (NEW.charged_calls=0 AND NEW.state='settled' AND NEW.charged_input_tokens=0 AND NEW.charged_output_tokens=0 AND json_valid(j.result_json) AND json_extract(j.result_json,'$.version')='s08.policy-terminal-outcome.v1' AND json_extract(j.result_json,'$.disposition')='positively_unsent')) AND NEW.reserved_output_tokens>=1 AND EXISTS (
      SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection' AND m.input_hash=j.input_hash AND m.content_hash=j.input_hash AND m.tool_hash IN ('c3649be7a7de38fe6f67977aacccc0b7e2b2e57d85569981a9de6ab3ac5f9535','baa17159592f3e568139de3b984c32872f79766d9655e79883f98b1489409a38','c123a15249928e7bda788a9c36e4055e6f3c4e24933a1af2341c21fab06fcad2','16ba601b83145894b795e5ec9dd4c7fd369638c7366d1d3508479350a40ed9b7','2318970f948a5c7e3b15b7a14826c602ffa97f70fd09f4a7b0151e7beb116304','e7404bf2b2ea01c116baf0272e287ae2f76422aa3a747a38c09fff8083b472f4')))))) OR
  (j.kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review','skill_draft') AND (NEW.reserved_calls!=1 OR NEW.charged_calls!=1 OR NEW.reserved_output_tokens<1))))
BEGIN SELECT RAISE(ABORT,'S08 reservation accounting unavailable'); END;

INSERT INTO fm_meta(key,value) VALUES('openclank.s05.skill_draft.v1','1');
