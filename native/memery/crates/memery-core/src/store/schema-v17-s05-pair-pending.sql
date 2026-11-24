-- Explicit offline-copy upgrade of the optional paired profile only. It never
-- runs during open or live operation; prior paired profiles remain valid.
DROP TRIGGER s04_pair_binding_immutable;
CREATE TRIGGER s04_pair_binding_immutable BEFORE UPDATE OF owner_id,job_id,source_ref_json,source_key,source_account_id,source_workspace_id,target_workspace_id,source_hash,admission_id,coverage_json ON fm_v2_s05_job_refs
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=OLD.owner_id AND j.job_id=OLD.job_id AND j.kind='assistant_interpretation_review')
BEGIN SELECT RAISE(ABORT,'paired source identity immutable'); END;
CREATE TRIGGER s04_pair_pending_binding_guard BEFORE UPDATE OF owner_epoch,durable_epoch,binding_fingerprint,policy_fingerprint,connection_id,model_route_id,authority_fingerprint,assignment_json ON fm_v2_s05_job_refs
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=OLD.owner_id AND j.job_id=OLD.job_id AND j.kind='assistant_interpretation_review') AND fm_pair_pending_rebind_authorized() IS NOT 1
BEGIN SELECT RAISE(ABORT,'paired pending rebind unavailable'); END;
DROP TRIGGER s04_pair_job_identity_immutable;
CREATE TRIGGER s04_pair_job_identity_immutable BEFORE UPDATE OF owner_id,job_id,kind,workspace_key,project_key,idempotency_key ON fm_v2_jobs
WHEN OLD.kind='assistant_interpretation_review' OR NEW.kind='assistant_interpretation_review'
BEGIN SELECT RAISE(ABORT,'paired job identity immutable'); END;
CREATE TRIGGER s04_pair_pending_input_guard BEFORE UPDATE OF input_hash ON fm_v2_jobs
WHEN (OLD.kind='assistant_interpretation_review' OR NEW.kind='assistant_interpretation_review') AND fm_pair_pending_rebind_authorized() IS NOT 1
BEGIN SELECT RAISE(ABORT,'paired pending rebind unavailable'); END;

-- Bound exact original-publication lookup without requiring the unrelated queued profile.
CREATE INDEX s04_pair_publication_attempt ON fm_v2_change_sets(owner_id,source_event_id,change_set_id) WHERE reason='unreviewed assistant interpretation';

INSERT INTO fm_meta(key,value) VALUES('openclank.assistant_interpretation.pending_rebind.v1','1');
