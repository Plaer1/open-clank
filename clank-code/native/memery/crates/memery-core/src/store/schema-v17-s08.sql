-- Optional offline S08 capability. Requires an empty S05 reservation table; no runtime schema mutation.
CREATE TEMP TABLE s08_empty_reservation_check (value INTEGER NOT NULL CHECK(value=0));
INSERT INTO s08_empty_reservation_check SELECT CASE WHEN EXISTS(SELECT 1 FROM fm_v2_s05_reservations) THEN 1 ELSE 0 END;
DROP TABLE s08_empty_reservation_check;
CREATE INDEX s08_consolidation_jobs ON fm_v2_jobs(owner_id,workspace_key,created_at,job_id) WHERE kind='access_consolidation';
CREATE INDEX s08_job_evidence ON fm_v2_evidence(owner_id,json_extract(locator_json,'$.job_id'),evidence_id) WHERE locator_type='s08_admitted_source' AND json_valid(locator_json);
CREATE INDEX s08_source_evidence ON fm_v2_evidence(owner_id,source_id,evidence_id) WHERE locator_type='s08_admitted_source';
DROP TABLE fm_v2_s05_reservations;
CREATE TABLE fm_v2_s05_reservations (
 owner_id TEXT NOT NULL, attempt_id TEXT NOT NULL, job_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL,
 connection_id TEXT NOT NULL, coverage_generation TEXT NOT NULL, clock_generation TEXT NOT NULL,
 foreground_epoch INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','settled','revoked','uncertain')),
 reserved_at_ms INTEGER NOT NULL, settled_at_ms INTEGER, reserved_calls INTEGER NOT NULL, reserved_seconds INTEGER NOT NULL,
 reserved_input_tokens INTEGER NOT NULL, reserved_output_tokens INTEGER NOT NULL,
 charged_calls INTEGER NOT NULL, charged_seconds INTEGER NOT NULL, charged_input_tokens INTEGER NOT NULL, charged_output_tokens INTEGER NOT NULL,
 PRIMARY KEY(owner_id,attempt_id), FOREIGN KEY(owner_id,attempt_id) REFERENCES fm_v2_attempts(owner_id,attempt_id),
 FOREIGN KEY(owner_id,job_id) REFERENCES fm_v2_jobs(owner_id,job_id),
 CHECK(lease_epoch>0 AND foreground_epoch>=0 AND reserved_at_ms>=0 AND (settled_at_ms IS NULL OR settled_at_ms>=reserved_at_ms)),
 CHECK(reserved_calls IN (0,1) AND reserved_seconds BETWEEN 1 AND 300 AND reserved_input_tokens BETWEEN 0 AND 1000000 AND reserved_output_tokens BETWEEN 0 AND 32768),
 CHECK(charged_calls IN (0,1) AND charged_seconds BETWEEN 0 AND 10000000 AND charged_input_tokens BETWEEN 0 AND 10000000 AND charged_output_tokens BETWEEN 0 AND 10000000)
);
CREATE UNIQUE INDEX fm_v2_s05_single_unsettled ON fm_v2_s05_reservations((1)) WHERE state IN ('reserved','dispatched','uncertain');
CREATE INDEX fm_v2_s05_reservations_window ON fm_v2_s05_reservations(owner_id,reserved_at_ms,state);
CREATE TRIGGER fm_v2_s05_attempt_identity_insert BEFORE INSERT ON fm_v2_s05_reservations
WHEN NOT EXISTS (SELECT 1 FROM fm_v2_attempts a WHERE a.owner_id=NEW.owner_id AND a.attempt_id=NEW.attempt_id AND a.job_id=NEW.job_id AND a.lease_epoch=NEW.lease_epoch)
BEGIN SELECT RAISE(ABORT,'S05 attempt identity mismatch'); END;
CREATE TRIGGER fm_v2_s05_attempt_identity_update BEFORE UPDATE OF owner_id,attempt_id,job_id,lease_epoch ON fm_v2_s05_reservations
BEGIN SELECT RAISE(ABORT,'S05 attempt identity immutable'); END;
CREATE TRIGGER fm_v2_s05_reservation_initial_guard BEFORE INSERT ON fm_v2_s05_reservations
WHEN NEW.state!='reserved' OR NOT EXISTS (
 SELECT 1 FROM fm_v2_attempts a JOIN fm_v2_jobs j ON j.owner_id=a.owner_id AND j.job_id=a.job_id
 JOIN fm_v2_s05_occupancy o ON o.singleton=1
 WHERE a.owner_id=NEW.owner_id AND a.attempt_id=NEW.attempt_id AND a.job_id=NEW.job_id AND a.lease_epoch=NEW.lease_epoch
 AND a.state='leased' AND j.kind IN ('s05_host_user_capture','completed_turn_review','access_consolidation') AND j.state='queued'
 AND o.verified=1 AND o.coverage_generation=NEW.coverage_generation AND o.clock_generation=NEW.clock_generation AND o.foreground_epoch=NEW.foreground_epoch
 AND NOT EXISTS (SELECT 1 FROM fm_v2_s05_foreground f WHERE f.state IN ('active','unknown')))
BEGIN SELECT RAISE(ABORT,'S05 reservation unavailable'); END;
CREATE TRIGGER fm_v2_s05_dispatch_guard BEFORE UPDATE OF state ON fm_v2_s05_reservations
WHEN NEW.state='dispatched' AND (OLD.state!='reserved' OR NOT EXISTS (
 SELECT 1 FROM fm_v2_s05_occupancy o JOIN fm_v2_jobs j ON j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id
 JOIN fm_v2_attempts a ON a.owner_id=NEW.owner_id AND a.attempt_id=NEW.attempt_id
 WHERE o.singleton=1 AND o.verified=1 AND o.foreground_epoch=NEW.foreground_epoch
 AND o.coverage_generation=NEW.coverage_generation AND o.clock_generation=NEW.clock_generation
 AND j.state='active' AND j.current_attempt_id=NEW.attempt_id AND a.state='leased' AND a.lease_epoch=NEW.lease_epoch
 AND NOT EXISTS (SELECT 1 FROM fm_v2_s05_foreground f WHERE f.state IN ('active','unknown'))))
BEGIN SELECT RAISE(ABORT,'S05 dispatch unavailable'); END;
CREATE TRIGGER fm_v2_s05_reservation_transition_guard BEFORE UPDATE OF state ON fm_v2_s05_reservations
WHEN NOT (NEW.state=OLD.state OR (OLD.state='reserved' AND NEW.state IN ('dispatched','revoked')) OR
          (OLD.state='dispatched' AND NEW.state IN ('settled','uncertain')) OR (OLD.state='uncertain' AND NEW.state='settled'))
BEGIN SELECT RAISE(ABORT,'invalid S05 reservation transition'); END;
CREATE TRIGGER fm_v2_s05_unsettled_no_delete BEFORE DELETE ON fm_v2_s05_reservations
WHEN OLD.state IN ('reserved','dispatched','uncertain')
BEGIN SELECT RAISE(ABORT,'S05 reservation remains unsettled'); END;
DROP TRIGGER fm_v2_s05_attempt_insert_guard;
CREATE TRIGGER fm_v2_s05_attempt_insert_guard BEFORE INSERT ON fm_v2_attempts
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind IN ('s05_host_user_capture','completed_turn_review','access_consolidation'))
 AND (NEW.state!='leased' OR NOT EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.state='queued'))
BEGIN SELECT RAISE(ABORT,'S05 attempt requires queued job'); END;
DROP TRIGGER fm_v2_s05_job_active_guard;
CREATE TRIGGER fm_v2_s05_job_active_guard BEFORE UPDATE OF state,current_attempt_id ON fm_v2_jobs
WHEN NEW.kind IN ('s05_host_user_capture','completed_turn_review','access_consolidation') AND NEW.state='active' AND NOT EXISTS (
 SELECT 1 FROM fm_v2_s05_reservations r JOIN fm_v2_attempts a ON a.owner_id=r.owner_id AND a.attempt_id=r.attempt_id
 WHERE r.owner_id=NEW.owner_id AND r.job_id=NEW.job_id AND r.attempt_id=NEW.current_attempt_id AND r.lease_epoch=a.lease_epoch AND r.state='reserved' AND a.state='leased')
BEGIN SELECT RAISE(ABORT,'S05 job requires reserved attempt'); END;
DROP TRIGGER fm_v2_s05_attempt_transition_guard;
CREATE TRIGGER fm_v2_s05_attempt_transition_guard BEFORE UPDATE OF state ON fm_v2_attempts
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind IN ('s05_host_user_capture','completed_turn_review','access_consolidation'))
 AND OLD.state!=NEW.state AND NOT EXISTS (
 SELECT 1 FROM fm_v2_s05_reservations r WHERE r.owner_id=NEW.owner_id AND r.attempt_id=NEW.attempt_id AND r.job_id=NEW.job_id AND r.lease_epoch=NEW.lease_epoch
 AND ((NEW.state='running' AND r.state='dispatched') OR (NEW.state='fenced' AND r.state='revoked') OR (NEW.state IN ('succeeded','failed_retryable','failed_terminal','cancelled') AND r.state='settled')))
BEGIN SELECT RAISE(ABORT,'S05 attempt requires matching reservation'); END;
DROP TRIGGER fm_v2_s05_job_terminal_guard;
CREATE TRIGGER fm_v2_s05_job_terminal_guard BEFORE UPDATE OF state ON fm_v2_jobs
WHEN NEW.kind IN ('s05_host_user_capture','completed_turn_review','access_consolidation') AND OLD.state='active' AND NEW.state IN ('retry_wait','succeeded','failed_terminal','cancelled') AND NOT EXISTS (
 SELECT 1 FROM fm_v2_s05_reservations r JOIN fm_v2_attempts a ON a.owner_id=r.owner_id AND a.attempt_id=r.attempt_id AND a.lease_epoch=r.lease_epoch
 WHERE r.owner_id=NEW.owner_id AND r.job_id=NEW.job_id AND r.attempt_id=OLD.current_attempt_id
 AND ((NEW.state='retry_wait' AND ((r.state='revoked' AND a.state='fenced') OR (r.state='settled' AND a.state='failed_retryable'))) OR
      (NEW.state='succeeded' AND r.state='settled' AND a.state='succeeded') OR
      (NEW.state='failed_terminal' AND r.state='settled' AND a.state='failed_terminal') OR
      (NEW.state='cancelled' AND r.state='settled' AND a.state='cancelled')))
BEGIN SELECT RAISE(ABORT,'S05 job outcome requires matching attempt'); END;
DROP TRIGGER fm_v2_s05_job_insert_guard;
CREATE TRIGGER fm_v2_s05_job_insert_guard BEFORE INSERT ON fm_v2_jobs
WHEN NEW.kind IN ('s05_host_user_capture','completed_turn_review','access_consolidation') AND (NEW.state!='queued' OR NEW.current_attempt_id IS NOT NULL OR NEW.attempt_count!=0)
BEGIN SELECT RAISE(ABORT,'S05 jobs must begin queued'); END;
CREATE TRIGGER s08_reservation_shape BEFORE INSERT ON fm_v2_s05_reservations
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND
 ((j.kind='access_consolidation' AND (NEW.connection_id!='local:rules' OR NEW.reserved_calls!=0 OR NEW.charged_calls!=0 OR NEW.reserved_input_tokens!=0 OR NEW.reserved_output_tokens!=0 OR NEW.charged_input_tokens!=0 OR NEW.charged_output_tokens!=0)) OR
  (j.kind IN ('s05_host_user_capture','completed_turn_review') AND (NEW.reserved_calls!=1 OR NEW.charged_calls!=1 OR NEW.reserved_output_tokens<1))))
BEGIN SELECT RAISE(ABORT,'S08 reservation accounting unavailable'); END;
CREATE TRIGGER s08_reservation_shape_update BEFORE UPDATE ON fm_v2_s05_reservations
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND
 ((j.kind='access_consolidation' AND (NEW.connection_id!='local:rules' OR NEW.reserved_calls!=0 OR NEW.charged_calls!=0 OR NEW.reserved_input_tokens!=0 OR NEW.reserved_output_tokens!=0 OR NEW.charged_input_tokens!=0 OR NEW.charged_output_tokens!=0 OR NEW.reserved_seconds!=OLD.reserved_seconds)) OR
  (j.kind IN ('s05_host_user_capture','completed_turn_review') AND (NEW.reserved_calls!=1 OR NEW.charged_calls!=1 OR NEW.reserved_output_tokens<1))))
BEGIN SELECT RAISE(ABORT,'S08 reservation accounting unavailable'); END;
CREATE TRIGGER s08_job_identity_immutable BEFORE UPDATE OF owner_id,job_id,kind,workspace_key,project_key,idempotency_key,input_hash ON fm_v2_jobs
WHEN OLD.kind='access_consolidation' AND (NEW.owner_id!=OLD.owner_id OR NEW.job_id!=OLD.job_id OR NEW.kind!=OLD.kind OR NEW.workspace_key!=OLD.workspace_key OR NEW.project_key!=OLD.project_key OR NEW.idempotency_key!=OLD.idempotency_key OR NEW.input_hash!=OLD.input_hash)
BEGIN SELECT RAISE(ABORT,'S08 job identity immutable'); END;
CREATE TRIGGER s08_job_result_guard BEFORE UPDATE OF result_json ON fm_v2_jobs
WHEN OLD.kind='access_consolidation' AND NEW.result_json IS NOT OLD.result_json AND NOT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context p WHERE p.owner_id=OLD.owner_id)
 AND (OLD.result_json IS NOT NULL OR NEW.result_json IS NULL OR NOT EXISTS(SELECT 1 FROM fm_v2_s05_reservations r JOIN fm_v2_attempts a ON a.owner_id=r.owner_id AND a.attempt_id=r.attempt_id AND a.lease_epoch=r.lease_epoch WHERE r.owner_id=OLD.owner_id AND r.job_id=OLD.job_id AND r.attempt_id=OLD.current_attempt_id AND r.state='dispatched' AND a.state='running' AND r.connection_id='local:rules' AND r.reserved_calls=0 AND r.charged_calls=0))
BEGIN SELECT RAISE(ABORT,'S08 job result unavailable'); END;
