-- Optional stopped-copy v2 batch overlay; requires the v1 intent profile.
DROP TRIGGER document_vector_intent_job_update_guard;
CREATE TRIGGER document_vector_intent_job_update_guard BEFORE UPDATE ON fm_v2_jobs
WHEN OLD.kind='document_vector_build.v1' AND (
 fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
 OR NEW.owner_id<>OLD.owner_id OR NEW.job_id<>OLD.job_id OR NEW.kind<>OLD.kind
 OR NEW.workspace_key<>OLD.workspace_key OR NEW.project_key<>OLD.project_key
 OR NEW.idempotency_key<>OLD.idempotency_key OR NEW.input_hash<>OLD.input_hash
 OR NEW.result_json IS NOT NULL OR NEW.created_at<>OLD.created_at
 OR (NEW.state IN ('succeeded','failed_terminal','cancelled') AND NEW.current_attempt_id IS NOT NULL)
 OR NOT (NEW.state=OLD.state OR (OLD.state='queued' AND NEW.state IN ('active','cancelled'))
   OR (OLD.state='active' AND NEW.state IN ('succeeded','failed_terminal','cancelled')))
 OR NOT EXISTS (SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=OLD.owner_id AND m.job_id=OLD.job_id AND m.phase='selection' AND json_valid(m.selected_json) AND json_extract(m.selected_json,'$.version') IN (1,2))
 OR (EXISTS (SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=OLD.owner_id AND m.job_id=OLD.job_id AND m.phase='selection' AND json_valid(m.selected_json) AND json_extract(m.selected_json,'$.version')=1)
   AND (NEW.current_attempt_id IS NOT NULL OR NEW.attempt_count<>0))
 OR (EXISTS (SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=OLD.owner_id AND m.job_id=OLD.job_id AND m.phase='selection' AND json_valid(m.selected_json) AND json_extract(m.selected_json,'$.version')=2)
   AND (((NEW.attempt_count<>OLD.attempt_count OR NEW.current_attempt_id IS NOT OLD.current_attempt_id OR NEW.state IN ('succeeded','failed_terminal','cancelled'))
       AND fm_document_vector_batch_authorized()<>1)
     OR NOT ((NEW.attempt_count=OLD.attempt_count AND NEW.current_attempt_id IS OLD.current_attempt_id)
       OR (OLD.state='active' AND NEW.state='active' AND OLD.current_attempt_id IS NULL AND NEW.current_attempt_id IS NOT NULL
           AND NEW.attempt_count=OLD.attempt_count+1 AND EXISTS(SELECT 1 FROM fm_v2_attempts a JOIN fm_v2_job_events e ON e.owner_id=a.owner_id AND e.job_id=a.job_id AND e.attempt_id=a.attempt_id AND e.sequence=2*a.attempt_number-1 AND e.state='active' WHERE a.owner_id=NEW.owner_id AND a.job_id=NEW.job_id AND a.attempt_id=NEW.current_attempt_id AND a.attempt_number=NEW.attempt_count AND a.state='running'))
       OR (OLD.state='active' AND OLD.current_attempt_id IS NOT NULL AND NEW.current_attempt_id IS NULL
           AND NEW.attempt_count=OLD.attempt_count AND EXISTS(SELECT 1 FROM fm_v2_attempts a JOIN fm_v2_job_events e ON e.owner_id=a.owner_id AND e.job_id=a.job_id AND e.attempt_id=a.attempt_id AND e.sequence=2*a.attempt_number AND e.state IN ('succeeded','cancelled') WHERE a.owner_id=NEW.owner_id AND a.job_id=NEW.job_id AND a.attempt_id=OLD.current_attempt_id AND a.attempt_number=OLD.attempt_count AND ((a.state='succeeded' AND e.state='succeeded') OR (a.state='fenced' AND e.state='cancelled'))))))))
BEGIN SELECT RAISE(ABORT,'document build intent immutable'); END;
CREATE TRIGGER document_vector_batch_attempt_insert_guard BEFORE INSERT ON fm_v2_attempts
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind='document_vector_build.v1') AND (
 fm_document_vector_batch_authorized()<>1 OR NEW.state<>'running' OR NEW.lease_epoch<>1
 OR NEW.lease_owner IS NOT NULL OR NEW.lease_expires_at IS NOT NULL OR NEW.heartbeat_at IS NOT NULL
 OR NEW.progress_watermark IS NOT NULL OR NEW.error_json IS NOT NULL
 OR NOT EXISTS (SELECT 1 FROM fm_v2_jobs j JOIN fm_v2_job_manifests m ON m.owner_id=j.owner_id AND m.job_id=j.job_id AND m.phase='selection'
   WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.state='active' AND j.current_attempt_id IS NULL
   AND NEW.attempt_number=j.attempt_count+1 AND json_valid(m.selected_json) AND json_extract(m.selected_json,'$.version')=2))
BEGIN SELECT RAISE(ABORT,'document batch claim unavailable'); END;
CREATE TRIGGER document_vector_batch_attempt_update_guard BEFORE UPDATE ON fm_v2_attempts
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=OLD.owner_id AND j.job_id=OLD.job_id AND j.kind='document_vector_build.v1') AND (
 fm_document_vector_batch_authorized()<>1 OR NEW.owner_id<>OLD.owner_id OR NEW.attempt_id<>OLD.attempt_id OR NEW.job_id<>OLD.job_id
 OR NEW.attempt_number<>OLD.attempt_number OR NEW.lease_epoch<>OLD.lease_epoch OR NEW.created_at<>OLD.created_at
 OR NEW.lease_owner IS NOT OLD.lease_owner OR NEW.lease_expires_at IS NOT OLD.lease_expires_at OR NEW.heartbeat_at IS NOT OLD.heartbeat_at
 OR NEW.progress_watermark IS NOT OLD.progress_watermark OR NEW.error_json IS NOT OLD.error_json
 OR NOT (OLD.state='running' AND NEW.state IN ('succeeded','fenced')))
BEGIN SELECT RAISE(ABORT,'document batch attempt immutable'); END;
CREATE TRIGGER document_vector_batch_attempt_delete_guard BEFORE DELETE ON fm_v2_attempts
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=OLD.owner_id AND j.job_id=OLD.job_id AND j.kind='document_vector_build.v1')
BEGIN SELECT RAISE(ABORT,'document batch attempt immutable'); END;
CREATE TRIGGER document_vector_batch_event_insert_guard BEFORE INSERT ON fm_v2_job_events
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind='document_vector_build.v1') AND (
 fm_document_vector_batch_authorized()<>1 OR NEW.attempt_id IS NULL OR NEW.lease_epoch<>1 OR NEW.state NOT IN ('active','succeeded','cancelled')
 OR NOT EXISTS (SELECT 1 FROM fm_v2_attempts a JOIN fm_v2_jobs j ON j.owner_id=a.owner_id AND j.job_id=a.job_id
   WHERE a.owner_id=NEW.owner_id AND a.job_id=NEW.job_id AND a.attempt_id=NEW.attempt_id AND a.state='running'
   AND ((NEW.state='active' AND NEW.sequence=2*a.attempt_number-1 AND j.current_attempt_id IS NULL AND a.attempt_number=j.attempt_count+1)
     OR (NEW.state IN ('succeeded','cancelled') AND NEW.sequence=2*a.attempt_number AND j.current_attempt_id=NEW.attempt_id AND a.attempt_number=j.attempt_count)))
 OR NOT json_valid(NEW.result_json) OR length(CAST(NEW.result_json AS BLOB))>16384 OR NEW.error_json IS NOT NULL
 OR NOT EXISTS (SELECT 1 FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id
   WHERE c.owner_id=NEW.owner_id AND c.change_set_id=NEW.change_set_id AND c.reason='document_vector.batch.v2'
   AND c.operation_metadata_json=NEW.result_json AND o.kind='document_vector_batch_v2'
   AND o.event_id='outbox_'||NEW.change_set_id||'_document_vector_batch_v2' AND o.payload_json=NEW.result_json))
BEGIN SELECT RAISE(ABORT,'document batch event unavailable'); END;
CREATE TRIGGER document_vector_batch_change_insert_guard BEFORE INSERT ON fm_v2_change_sets
WHEN NEW.reason='document_vector.batch.v2' AND fm_document_vector_batch_authorized()<>1
BEGIN SELECT RAISE(ABORT,'document batch receipt unavailable'); END;
CREATE TRIGGER document_vector_batch_change_update_guard BEFORE UPDATE ON fm_v2_change_sets
WHEN OLD.reason='document_vector.batch.v2'
BEGIN SELECT RAISE(ABORT,'document batch receipt immutable'); END;
CREATE TRIGGER document_vector_batch_change_delete_guard BEFORE DELETE ON fm_v2_change_sets
WHEN OLD.reason='document_vector.batch.v2'
BEGIN SELECT RAISE(ABORT,'document batch receipt immutable'); END;
CREATE TRIGGER document_vector_batch_outbox_insert_guard BEFORE INSERT ON fm_v2_outbox
WHEN NEW.kind='document_vector_batch_v2' AND fm_document_vector_batch_authorized()<>1
BEGIN SELECT RAISE(ABORT,'document batch outbox unavailable'); END;
CREATE TRIGGER document_vector_batch_outbox_update_guard BEFORE UPDATE ON fm_v2_outbox
WHEN OLD.kind='document_vector_batch_v2'
BEGIN SELECT RAISE(ABORT,'document batch outbox immutable'); END;
CREATE TRIGGER document_vector_batch_outbox_delete_guard BEFORE DELETE ON fm_v2_outbox
WHEN OLD.kind='document_vector_batch_v2'
BEGIN SELECT RAISE(ABORT,'document batch outbox immutable'); END;

CREATE TRIGGER document_vector_batch_attempt_reclassify_guard BEFORE UPDATE OF owner_id,job_id ON fm_v2_attempts
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind='document_vector_build.v1')
 AND (NEW.owner_id IS NOT OLD.owner_id OR NEW.job_id IS NOT OLD.job_id)
BEGIN SELECT RAISE(ABORT,'document batch attempt reclassification unavailable'); END;
CREATE TRIGGER document_vector_batch_change_reclassify_guard BEFORE UPDATE OF reason ON fm_v2_change_sets
WHEN OLD.reason<>'document_vector.batch.v2' AND NEW.reason='document_vector.batch.v2'
BEGIN SELECT RAISE(ABORT,'document batch receipt reclassification unavailable'); END;
CREATE TRIGGER document_vector_batch_outbox_reclassify_guard BEFORE UPDATE OF kind ON fm_v2_outbox
WHEN OLD.kind<>'document_vector_batch_v2' AND NEW.kind='document_vector_batch_v2'
BEGIN SELECT RAISE(ABORT,'document batch outbox reclassification unavailable'); END;

-- V2 originals can be deleted only by the exact guarded lifecycle, which
-- suspends and restores owner-table triggers in its transaction.
CREATE TRIGGER document_vector_batch_job_delete_guard BEFORE DELETE ON fm_v2_jobs
WHEN OLD.kind='document_vector_build.v1' AND NOT EXISTS(SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=OLD.owner_id AND m.job_id=OLD.job_id AND m.phase='selection' AND json_valid(m.selected_json) AND json_extract(m.selected_json,'$.version')=1)
BEGIN SELECT RAISE(ABORT,'document batch original immutable'); END;
CREATE TRIGGER document_vector_batch_manifest_delete_guard BEFORE DELETE ON fm_v2_job_manifests
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=OLD.owner_id AND j.job_id=OLD.job_id AND j.kind='document_vector_build.v1') AND NOT EXISTS(SELECT 1 FROM fm_v2_job_manifests m WHERE m.owner_id=OLD.owner_id AND m.job_id=OLD.job_id AND m.phase='selection' AND json_valid(m.selected_json) AND json_extract(m.selected_json,'$.version')=1)
BEGIN SELECT RAISE(ABORT,'document batch manifest immutable'); END;
