-- Optional stopped-copy v18 document build-intent owner; no new table.
CREATE TRIGGER document_vector_intent_job_insert_guard BEFORE INSERT ON fm_v2_jobs
WHEN NEW.kind='document_vector_build.v1' AND (
 fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3' OR NEW.state<>'queued'
 OR NEW.current_attempt_id IS NOT NULL OR NEW.attempt_count<>0 OR NEW.result_json IS NOT NULL
 OR NEW.workspace_key<>'' OR NEW.project_key<>'' OR length(NEW.input_hash)<>64)
BEGIN SELECT RAISE(ABORT,'document build intent unavailable'); END;
CREATE TRIGGER document_vector_intent_job_update_guard BEFORE UPDATE ON fm_v2_jobs
WHEN OLD.kind='document_vector_build.v1' AND (
 fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
 OR NEW.owner_id<>OLD.owner_id OR NEW.job_id<>OLD.job_id OR NEW.kind<>OLD.kind
 OR NEW.workspace_key<>OLD.workspace_key OR NEW.project_key<>OLD.project_key
 OR NEW.idempotency_key<>OLD.idempotency_key OR NEW.input_hash<>OLD.input_hash
 OR NEW.current_attempt_id IS NOT NULL OR NEW.attempt_count<>0 OR NEW.result_json IS NOT NULL
 OR NEW.created_at<>OLD.created_at OR NOT (NEW.state=OLD.state
 OR (OLD.state='queued' AND NEW.state IN ('active','cancelled'))
 OR (OLD.state='active' AND NEW.state IN ('succeeded','failed_terminal','cancelled'))))
BEGIN SELECT RAISE(ABORT,'document build intent immutable'); END;
CREATE TRIGGER document_vector_intent_job_delete_guard BEFORE DELETE ON fm_v2_jobs
WHEN OLD.kind='document_vector_build.v1' AND fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
BEGIN SELECT RAISE(ABORT,'document build intent deletion unavailable'); END;
CREATE TRIGGER document_vector_intent_manifest_insert_guard BEFORE INSERT ON fm_v2_job_manifests
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind='document_vector_build.v1') AND (
 fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3' OR NOT json_valid(NEW.selected_json)
 OR length(CAST(NEW.selected_json AS BLOB))>16384 OR length(NEW.input_hash)<>64 OR length(NEW.content_hash)<>64
 OR NEW.completed_json<>'[]' OR NEW.reused_json<>'[]' OR NEW.failed_json<>'[]' OR NEW.waived_json<>'[]')
BEGIN SELECT RAISE(ABORT,'document build manifest unavailable'); END;
CREATE TRIGGER document_vector_intent_manifest_update_guard BEFORE UPDATE ON fm_v2_job_manifests
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=OLD.owner_id AND j.job_id=OLD.job_id AND j.kind='document_vector_build.v1')
BEGIN SELECT RAISE(ABORT,'document build manifest immutable'); END;
CREATE TRIGGER document_vector_intent_manifest_delete_guard BEFORE DELETE ON fm_v2_job_manifests
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=OLD.owner_id AND j.job_id=OLD.job_id AND j.kind='document_vector_build.v1')
 AND fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
BEGIN SELECT RAISE(ABORT,'document build manifest deletion unavailable'); END;

CREATE TRIGGER document_vector_intent_job_reclassify_guard BEFORE UPDATE OF kind ON fm_v2_jobs
WHEN OLD.kind<>'document_vector_build.v1' AND NEW.kind='document_vector_build.v1'
BEGIN SELECT RAISE(ABORT,'document build kind reclassification unavailable'); END;
