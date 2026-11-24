-- Inactive offline candidate only. No runtime loader, version stamp or activation.
CREATE TABLE fm_v2_s05_schema (version INTEGER NOT NULL CHECK(version=3));
INSERT INTO fm_v2_s05_schema VALUES (3);
CREATE TABLE fm_v2_s05_job_refs (
 owner_id TEXT NOT NULL, job_id TEXT NOT NULL, source_ref_json TEXT NOT NULL,
 source_key TEXT NOT NULL UNIQUE, source_account_id TEXT NOT NULL, source_workspace_id TEXT NOT NULL,
 target_workspace_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL, durable_epoch TEXT NOT NULL, binding_fingerprint TEXT NOT NULL,
 source_hash TEXT NOT NULL, admission_id TEXT NOT NULL, policy_fingerprint TEXT NOT NULL,
 connection_id TEXT NOT NULL, model_route_id TEXT NOT NULL, authority_fingerprint TEXT NOT NULL,
 coverage_json TEXT,
 candidate_id TEXT, candidate_attempt_id TEXT, candidate_lease_epoch INTEGER,
 assignment_json TEXT CHECK(assignment_json IS NULL OR CASE
   WHEN json_valid(assignment_json) AND json_type(assignment_json, '$.connectionID') IS 'text'
     AND json_type(assignment_json, '$.modelRouteID') IS 'text'
   THEN json_extract(assignment_json, '$.connectionID') = connection_id
     AND json_extract(assignment_json, '$.modelRouteID') = model_route_id
   ELSE 0 END),
 PRIMARY KEY(owner_id,job_id), FOREIGN KEY(owner_id,job_id) REFERENCES fm_v2_jobs(owner_id,job_id),
 FOREIGN KEY(owner_id,candidate_attempt_id) REFERENCES fm_v2_attempts(owner_id,attempt_id),
 CHECK ((candidate_id IS NULL AND candidate_attempt_id IS NULL AND candidate_lease_epoch IS NULL)
     OR (candidate_id IS NOT NULL AND length(candidate_id) BETWEEN 1 AND 512 AND candidate_attempt_id IS NOT NULL AND candidate_lease_epoch IS NOT NULL AND candidate_lease_epoch > 0))
);
-- Candidate association is an optional, offline schema capability. Existing v3
-- databases lacking it cannot publish candidates until explicitly converted.
CREATE UNIQUE INDEX fm_v2_s05_candidate_identity ON fm_v2_s05_job_refs(owner_id,candidate_id) WHERE candidate_id IS NOT NULL;
CREATE INDEX s04_s05_invalid_account ON fm_v2_s05_job_refs(source_account_id) WHERE NOT json_valid(source_ref_json);
CREATE INDEX s04_s05_invalid_owner ON fm_v2_s05_job_refs(owner_id) WHERE NOT json_valid(source_ref_json);
CREATE INDEX s04_s05_ref_account ON fm_v2_s05_job_refs(json_extract(source_ref_json,'$.account_id'),owner_id,job_id) WHERE json_valid(source_ref_json);
CREATE INDEX s04_s05_source_account ON fm_v2_s05_job_refs(source_account_id,owner_id,job_id);
CREATE INDEX s04_host_candidate_source ON candidates(owner,source_uri,workspace_id,id);
CREATE INDEX s04_host_evidence_source ON fm_v2_evidence(owner_id,source_id,source_revision);
CREATE INDEX s04_host_curated_source ON curated(owner,json_extract(metadata,'$.source_uri'),workspace_id) WHERE json_valid(metadata);
CREATE TRIGGER fm_v2_s05_candidate_ref_immutable BEFORE UPDATE ON fm_v2_s05_job_refs
WHEN OLD.candidate_id IS NOT NULL
BEGIN SELECT RAISE(ABORT,'S05 candidate source association immutable'); END;
CREATE TRIGGER fm_v2_s05_candidate_initial_guard BEFORE INSERT ON fm_v2_s05_job_refs
WHEN NEW.candidate_id IS NOT NULL
BEGIN SELECT RAISE(ABORT,'S05 candidate requires transactional association'); END;
CREATE TRIGGER fm_v2_s05_candidate_association_guard BEFORE UPDATE OF candidate_id,candidate_attempt_id,candidate_lease_epoch ON fm_v2_s05_job_refs
WHEN OLD.candidate_id IS NOT NULL OR NEW.candidate_id IS NULL OR NOT EXISTS (
 SELECT 1 FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id
 JOIN fm_v2_s05_reservations r ON r.owner_id=a.owner_id AND r.attempt_id=a.attempt_id AND r.job_id=a.job_id AND r.lease_epoch=a.lease_epoch
 JOIN candidates c ON c.id=NEW.candidate_id AND c.owner=NEW.owner_id AND c.workspace_id=NEW.target_workspace_id
 WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind IN ('s05_host_user_capture','completed_turn_review')
 AND j.workspace_key=NEW.target_workspace_id AND j.project_key='' AND j.state='active' AND j.current_attempt_id=NEW.candidate_attempt_id
 AND a.attempt_id=NEW.candidate_attempt_id AND a.lease_epoch=NEW.candidate_lease_epoch AND a.state='running' AND r.state='dispatched' AND c.status='pending')
BEGIN SELECT RAISE(ABORT,'S05 candidate association unavailable'); END;
CREATE TRIGGER fm_v2_s05_review_coverage_guard BEFORE INSERT ON fm_v2_s05_job_refs
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind='completed_turn_review')
 AND (NEW.coverage_json IS NULL OR CASE WHEN json_valid(NEW.coverage_json) THEN
      COALESCE(json_extract(NEW.coverage_json,'$.version') = 1 AND json_extract(NEW.coverage_json,'$.input') = 'host_user_body_only'
       AND json_extract(NEW.coverage_json,'$.assistant') = 'unsupported' AND json_extract(NEW.coverage_json,'$.native') = 'unsupported'
       AND json_extract(NEW.coverage_json,'$.queued') = 'unsupported',0) ELSE 0 END = 0)
BEGIN SELECT RAISE(ABORT,'completed-turn coverage unavailable'); END;
CREATE TABLE fm_v2_s05_occupancy (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), coverage_generation TEXT NOT NULL,
 clock_generation TEXT NOT NULL, verified INTEGER NOT NULL CHECK(verified IN (0,1)),
 foreground_epoch INTEGER NOT NULL CHECK(foreground_epoch>=0), last_quiet_tick INTEGER NOT NULL CHECK(last_quiet_tick>=0)
);
CREATE TABLE fm_v2_s05_foreground (
 operation_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 coverage_generation TEXT NOT NULL, epoch INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','settled','unknown'))
);
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
 CHECK(reserved_calls=1 AND reserved_seconds BETWEEN 1 AND 300 AND reserved_input_tokens BETWEEN 0 AND 1000000 AND reserved_output_tokens BETWEEN 1 AND 32768),
 CHECK(charged_calls=1 AND charged_seconds BETWEEN 0 AND 10000000 AND charged_input_tokens BETWEEN 0 AND 10000000 AND charged_output_tokens BETWEEN 0 AND 10000000)
);
CREATE UNIQUE INDEX fm_v2_s05_single_unsettled ON fm_v2_s05_reservations((1)) WHERE state IN ('reserved','dispatched','uncertain');
CREATE TRIGGER fm_v2_s05_attempt_identity_insert BEFORE INSERT ON fm_v2_s05_reservations
WHEN NOT EXISTS (SELECT 1 FROM fm_v2_attempts a WHERE a.owner_id=NEW.owner_id AND a.attempt_id=NEW.attempt_id AND a.job_id=NEW.job_id AND a.lease_epoch=NEW.lease_epoch)
BEGIN SELECT RAISE(ABORT,'S05 attempt identity mismatch'); END;
CREATE TRIGGER fm_v2_s05_attempt_identity_update BEFORE UPDATE OF owner_id,attempt_id,job_id,lease_epoch ON fm_v2_s05_reservations
BEGIN SELECT RAISE(ABORT,'S05 attempt identity immutable'); END;
CREATE INDEX fm_v2_s05_reservations_window ON fm_v2_s05_reservations(owner_id,reserved_at_ms,state);
CREATE TRIGGER fm_v2_s05_guard_attempt_identity BEFORE UPDATE OF owner_id,attempt_id,job_id,lease_epoch ON fm_v2_attempts
WHEN EXISTS (SELECT 1 FROM fm_v2_s05_reservations r WHERE r.owner_id=OLD.owner_id AND r.attempt_id=OLD.attempt_id)
BEGIN SELECT RAISE(ABORT,'S05 linked attempt identity immutable'); END;
-- A staged leased attempt has no dispatch authority until its reservation exists.
CREATE TRIGGER fm_v2_s05_attempt_insert_guard BEFORE INSERT ON fm_v2_attempts
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind IN ('s05_host_user_capture','completed_turn_review'))
 AND (NEW.state!='leased' OR NOT EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.state='queued'))
BEGIN SELECT RAISE(ABORT,'S05 attempt requires queued job'); END;
CREATE TRIGGER fm_v2_s05_reservation_initial_guard BEFORE INSERT ON fm_v2_s05_reservations
WHEN NEW.state!='reserved' OR NOT EXISTS (
 SELECT 1 FROM fm_v2_attempts a JOIN fm_v2_jobs j ON j.owner_id=a.owner_id AND j.job_id=a.job_id
 JOIN fm_v2_s05_occupancy o ON o.singleton=1
 WHERE a.owner_id=NEW.owner_id AND a.attempt_id=NEW.attempt_id AND a.job_id=NEW.job_id AND a.lease_epoch=NEW.lease_epoch
 AND a.state='leased' AND j.kind IN ('s05_host_user_capture','completed_turn_review') AND j.state='queued'
 AND o.verified=1 AND o.coverage_generation=NEW.coverage_generation AND o.clock_generation=NEW.clock_generation AND o.foreground_epoch=NEW.foreground_epoch
 AND NOT EXISTS (SELECT 1 FROM fm_v2_s05_foreground f WHERE f.state IN ('active','unknown')))
BEGIN SELECT RAISE(ABORT,'S05 reservation unavailable'); END;
CREATE TRIGGER fm_v2_s05_job_active_guard BEFORE UPDATE OF state,current_attempt_id ON fm_v2_jobs
WHEN NEW.kind IN ('s05_host_user_capture','completed_turn_review') AND NEW.state='active' AND NOT EXISTS (
 SELECT 1 FROM fm_v2_s05_reservations r JOIN fm_v2_attempts a ON a.owner_id=r.owner_id AND a.attempt_id=r.attempt_id
 WHERE r.owner_id=NEW.owner_id AND r.job_id=NEW.job_id AND r.attempt_id=NEW.current_attempt_id AND r.lease_epoch=a.lease_epoch AND r.state='reserved' AND a.state='leased')
BEGIN SELECT RAISE(ABORT,'S05 job requires reserved attempt'); END;
CREATE TRIGGER fm_v2_s05_dispatch_guard BEFORE UPDATE OF state ON fm_v2_s05_reservations
WHEN NEW.state='dispatched' AND (OLD.state!='reserved' OR NOT EXISTS (
 SELECT 1 FROM fm_v2_s05_occupancy o JOIN fm_v2_jobs j ON j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id
 JOIN fm_v2_attempts a ON a.owner_id=NEW.owner_id AND a.attempt_id=NEW.attempt_id
 WHERE o.singleton=1 AND o.verified=1 AND o.foreground_epoch=NEW.foreground_epoch
 AND o.coverage_generation=NEW.coverage_generation AND o.clock_generation=NEW.clock_generation
 AND j.state='active' AND j.current_attempt_id=NEW.attempt_id AND a.state='leased' AND a.lease_epoch=NEW.lease_epoch
 AND NOT EXISTS (SELECT 1 FROM fm_v2_s05_foreground f WHERE f.state IN ('active','unknown'))))
BEGIN SELECT RAISE(ABORT,'S05 dispatch unavailable'); END;
CREATE TRIGGER fm_v2_s05_attempt_transition_guard BEFORE UPDATE OF state ON fm_v2_attempts
WHEN EXISTS (SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind IN ('s05_host_user_capture','completed_turn_review'))
 AND OLD.state!=NEW.state AND NOT EXISTS (
 SELECT 1 FROM fm_v2_s05_reservations r WHERE r.owner_id=NEW.owner_id AND r.attempt_id=NEW.attempt_id AND r.job_id=NEW.job_id AND r.lease_epoch=NEW.lease_epoch
 AND ((NEW.state='running' AND r.state='dispatched') OR (NEW.state='fenced' AND r.state='revoked') OR (NEW.state IN ('succeeded','failed_retryable','failed_terminal','cancelled') AND r.state='settled')))
BEGIN SELECT RAISE(ABORT,'S05 attempt requires matching reservation'); END;
CREATE TRIGGER fm_v2_s05_job_terminal_guard BEFORE UPDATE OF state ON fm_v2_jobs
WHEN NEW.kind IN ('s05_host_user_capture','completed_turn_review') AND OLD.state='active' AND NEW.state IN ('retry_wait','succeeded','failed_terminal','cancelled') AND NOT EXISTS (
 SELECT 1 FROM fm_v2_s05_reservations r JOIN fm_v2_attempts a ON a.owner_id=r.owner_id AND a.attempt_id=r.attempt_id AND a.lease_epoch=r.lease_epoch
 WHERE r.owner_id=NEW.owner_id AND r.job_id=NEW.job_id AND r.attempt_id=OLD.current_attempt_id
 AND ((NEW.state='retry_wait' AND ((r.state='revoked' AND a.state='fenced') OR (r.state='settled' AND a.state='failed_retryable'))) OR
      (NEW.state='succeeded' AND r.state='settled' AND a.state='succeeded') OR
      (NEW.state='failed_terminal' AND r.state='settled' AND a.state='failed_terminal') OR
      (NEW.state='cancelled' AND r.state='settled' AND a.state='cancelled')))
BEGIN SELECT RAISE(ABORT,'S05 job outcome requires matching attempt'); END;
CREATE TRIGGER fm_v2_s05_job_insert_guard BEFORE INSERT ON fm_v2_jobs
WHEN NEW.kind IN ('s05_host_user_capture','completed_turn_review') AND (NEW.state!='queued' OR NEW.current_attempt_id IS NOT NULL OR NEW.attempt_count!=0)
BEGIN SELECT RAISE(ABORT,'S05 jobs must begin queued'); END;
CREATE TRIGGER fm_v2_s05_reservation_transition_guard BEFORE UPDATE OF state ON fm_v2_s05_reservations
WHEN NOT (NEW.state=OLD.state OR (OLD.state='reserved' AND NEW.state IN ('dispatched','revoked')) OR
          (OLD.state='dispatched' AND NEW.state IN ('settled','uncertain')) OR (OLD.state='uncertain' AND NEW.state='settled'))
BEGIN SELECT RAISE(ABORT,'invalid S05 reservation transition'); END;
CREATE TRIGGER fm_v2_s05_unsettled_no_delete BEFORE DELETE ON fm_v2_s05_reservations
WHEN OLD.state IN ('reserved','dispatched','uncertain')
BEGIN SELECT RAISE(ABORT,'S05 reservation remains unsettled'); END;
