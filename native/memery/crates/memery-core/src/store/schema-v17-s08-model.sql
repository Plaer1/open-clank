-- Explicit offline-only additive S08 model profile. This changes accounting
-- guards only; the private Native/Host owners still prove selection, mode,
-- source, policy, attempt and proposal authority before any effect.
DROP TRIGGER s08_reservation_shape;
CREATE TRIGGER s08_reservation_shape BEFORE INSERT ON fm_v2_s05_reservations
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND
 ((j.kind='access_consolidation' AND NOT (
   (NEW.connection_id='local:rules' AND NEW.reserved_calls=0 AND NEW.charged_calls=0 AND NEW.reserved_input_tokens=0 AND NEW.reserved_output_tokens=0 AND NEW.charged_input_tokens=0 AND NEW.charged_output_tokens=0)
   OR (NEW.connection_id!='local:rules' AND NEW.reserved_calls=1 AND NEW.charged_calls=1 AND NEW.reserved_output_tokens>=1 AND EXISTS (
      SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection' AND m.input_hash=j.input_hash AND m.content_hash=j.input_hash AND m.tool_hash IN ('c3649be7a7de38fe6f67977aacccc0b7e2b2e57d85569981a9de6ab3ac5f9535','baa17159592f3e568139de3b984c32872f79766d9655e79883f98b1489409a38','c123a15249928e7bda788a9c36e4055e6f3c4e24933a1af2341c21fab06fcad2','16ba601b83145894b795e5ec9dd4c7fd369638c7366d1d3508479350a40ed9b7'))))) OR
  (j.kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review') AND (NEW.reserved_calls!=1 OR NEW.charged_calls!=1 OR NEW.reserved_output_tokens<1))))
BEGIN SELECT RAISE(ABORT,'S08 reservation accounting unavailable'); END;
DROP TRIGGER s08_reservation_shape_update;
CREATE TRIGGER s08_reservation_shape_update BEFORE UPDATE ON fm_v2_s05_reservations
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND
 ((j.kind='access_consolidation' AND (NEW.reserved_seconds!=OLD.reserved_seconds OR NOT (
   (NEW.connection_id='local:rules' AND NEW.reserved_calls=0 AND NEW.charged_calls=0 AND NEW.reserved_input_tokens=0 AND NEW.reserved_output_tokens=0 AND NEW.charged_input_tokens=0 AND NEW.charged_output_tokens=0)
   OR (NEW.connection_id!='local:rules' AND NEW.reserved_calls=1 AND (NEW.charged_calls=1 OR (NEW.charged_calls=0 AND NEW.state='settled' AND NEW.charged_input_tokens=0 AND NEW.charged_output_tokens=0 AND json_valid(j.result_json) AND json_extract(j.result_json,'$.version')='s08.policy-terminal-outcome.v1' AND json_extract(j.result_json,'$.disposition')='positively_unsent')) AND NEW.reserved_output_tokens>=1 AND EXISTS (
      SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection' AND m.input_hash=j.input_hash AND m.content_hash=j.input_hash AND m.tool_hash IN ('c3649be7a7de38fe6f67977aacccc0b7e2b2e57d85569981a9de6ab3ac5f9535','baa17159592f3e568139de3b984c32872f79766d9655e79883f98b1489409a38','c123a15249928e7bda788a9c36e4055e6f3c4e24933a1af2341c21fab06fcad2','16ba601b83145894b795e5ec9dd4c7fd369638c7366d1d3508479350a40ed9b7')))))) OR
  (j.kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review') AND (NEW.reserved_calls!=1 OR NEW.charged_calls!=1 OR NEW.reserved_output_tokens<1))))
BEGIN SELECT RAISE(ABORT,'S08 reservation accounting unavailable'); END;
DROP TRIGGER s08_job_result_guard;
CREATE TRIGGER s08_job_result_guard BEFORE UPDATE OF result_json ON fm_v2_jobs
WHEN OLD.kind='access_consolidation' AND NEW.result_json IS NOT OLD.result_json AND NOT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context p WHERE p.owner_id=OLD.owner_id)
 AND (OLD.result_json IS NOT NULL OR NEW.result_json IS NULL OR NOT EXISTS(
  SELECT 1 FROM fm_v2_s05_reservations r JOIN fm_v2_attempts a ON a.owner_id=r.owner_id AND a.attempt_id=r.attempt_id AND a.lease_epoch=r.lease_epoch
  WHERE r.owner_id=OLD.owner_id AND r.job_id=OLD.job_id AND r.attempt_id=OLD.current_attempt_id AND ((r.state='dispatched' AND a.state='running') OR (r.state='reserved' AND a.state='leased' AND json_valid(NEW.result_json) AND json_extract(NEW.result_json,'$.version')='s08.policy-terminal-outcome.v1' AND json_extract(NEW.result_json,'$.disposition')='positively_unsent' AND json_extract(NEW.result_json,'$.prior_reservation_state')='reserved'))
   AND ((r.connection_id='local:rules' AND r.reserved_calls=0 AND r.charged_calls=0)
    OR (r.connection_id!='local:rules' AND r.reserved_calls=1 AND r.charged_calls=1 AND r.reserved_output_tokens>=1 AND EXISTS (
      SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=OLD.owner_id AND m.job_id=OLD.job_id AND m.phase='selection' AND m.input_hash=OLD.input_hash AND m.content_hash=OLD.input_hash AND m.tool_hash IN ('c3649be7a7de38fe6f67977aacccc0b7e2b2e57d85569981a9de6ab3ac5f9535','baa17159592f3e568139de3b984c32872f79766d9655e79883f98b1489409a38','c123a15249928e7bda788a9c36e4055e6f3c4e24933a1af2341c21fab06fcad2','16ba601b83145894b795e5ec9dd4c7fd369638c7366d1d3508479350a40ed9b7'))))))
BEGIN SELECT RAISE(ABORT,'S08 job result unavailable'); END;
-- A drained, positively unsent owned attempt may close its original reserved
-- lease directly; no generic reserved attempt receives a new transition.
DROP TRIGGER fm_v2_s05_reservation_transition_guard;
CREATE TRIGGER fm_v2_s05_reservation_transition_guard BEFORE UPDATE OF state ON fm_v2_s05_reservations
WHEN NOT (NEW.state=OLD.state OR (OLD.state='reserved' AND NEW.state IN ('dispatched','revoked')) OR
          (OLD.state='dispatched' AND NEW.state IN ('settled','uncertain')) OR (OLD.state='uncertain' AND NEW.state='settled') OR
          (OLD.state='reserved' AND NEW.state='settled' AND NEW.charged_calls=0 AND NEW.charged_input_tokens=0 AND NEW.charged_output_tokens=0 AND EXISTS (
             SELECT 1 FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id AND a.attempt_id=j.current_attempt_id
             WHERE j.owner_id=OLD.owner_id AND j.job_id=OLD.job_id AND j.kind='access_consolidation' AND j.state='active' AND a.attempt_id=OLD.attempt_id AND a.lease_epoch=OLD.lease_epoch AND a.state='leased'
               AND json_valid(j.result_json) AND json_extract(j.result_json,'$.version')='s08.policy-terminal-outcome.v1' AND json_extract(j.result_json,'$.disposition')='positively_unsent' AND json_extract(j.result_json,'$.prior_reservation_state')='reserved')))
BEGIN SELECT RAISE(ABORT,'invalid S05 reservation transition'); END;
INSERT INTO fm_meta(key,value) VALUES('openclank.s08.model.v1','1');
