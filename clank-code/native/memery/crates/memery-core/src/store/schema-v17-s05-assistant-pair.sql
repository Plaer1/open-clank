-- Explicit offline-copy profile only. Existing user-only coverage is unchanged.
CREATE TEMP TABLE s04_pair_empty_check(value INTEGER NOT NULL CHECK(value=0));
INSERT INTO s04_pair_empty_check SELECT CASE WHEN EXISTS(SELECT 1 FROM fm_v2_jobs WHERE kind='assistant_interpretation_review') THEN 1 ELSE 0 END;
DROP TABLE s04_pair_empty_check;
CREATE INDEX s04_pair_assistant_origin ON fm_v2_s05_job_refs(source_account_id,json_extract(source_ref_json,'$.chat_id'),json_extract(coverage_json,'$.assistant.message_id'),owner_id,job_id)
 WHERE json_valid(source_ref_json) AND json_valid(coverage_json) AND json_extract(coverage_json,'$.kind')='assistant_interpretation_review';
CREATE TRIGGER s04_pair_coverage_insert BEFORE INSERT ON fm_v2_s05_job_refs
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind='assistant_interpretation_review') AND
 (CASE WHEN json_valid(NEW.coverage_json) AND json_valid(NEW.source_ref_json) THEN COALESCE(
   json_type(NEW.coverage_json)='object' AND (SELECT count(*) FROM json_each(NEW.coverage_json))=4
   AND json_extract(NEW.coverage_json,'$.version')=1 AND json_extract(NEW.coverage_json,'$.kind')='assistant_interpretation_review'
   AND json_type(NEW.coverage_json,'$.assistant')='object' AND (SELECT count(*) FROM json_each(NEW.coverage_json,'$.assistant'))=4
   AND json_type(NEW.coverage_json,'$.assistant.message_id')='text' AND length(json_extract(NEW.coverage_json,'$.assistant.message_id')) BETWEEN 1 AND 512
   AND json_extract(NEW.coverage_json,'$.assistant.part_id')='body' AND json_type(NEW.coverage_json,'$.assistant.revision')='integer' AND json_extract(NEW.coverage_json,'$.assistant.revision')>0
   AND json_type(NEW.coverage_json,'$.assistant.content_hash')='text' AND length(json_extract(NEW.coverage_json,'$.assistant.content_hash'))=64 AND json_extract(NEW.coverage_json,'$.assistant.content_hash') NOT GLOB '*[^0-9a-f]*'
   AND json_type(NEW.coverage_json,'$.pair_sha256')='text' AND length(json_extract(NEW.coverage_json,'$.pair_sha256'))=64 AND json_extract(NEW.coverage_json,'$.pair_sha256') NOT GLOB '*[^0-9a-f]*'
   AND json_type(NEW.source_ref_json)='object' AND (SELECT count(*) FROM json_each(NEW.source_ref_json))=11
   AND json_type(NEW.source_ref_json,'$.version')='integer' AND json_extract(NEW.source_ref_json,'$.version')=1
   AND json_extract(NEW.source_ref_json,'$.kind')='host_user_body' AND json_extract(NEW.source_ref_json,'$.actor_id')='main' AND json_extract(NEW.source_ref_json,'$.part_id')='body'
   AND json_type(NEW.source_ref_json,'$.chat_id')='text' AND length(json_extract(NEW.source_ref_json,'$.chat_id')) BETWEEN 1 AND 512
   AND json_type(NEW.source_ref_json,'$.message_id')='text' AND length(json_extract(NEW.source_ref_json,'$.message_id')) BETWEEN 1 AND 512
   AND json_type(NEW.source_ref_json,'$.revision')='integer' AND json_extract(NEW.source_ref_json,'$.revision')>0
   AND json_type(NEW.source_ref_json,'$.project_id')='null'
   AND typeof(NEW.source_key)='text' AND length(NEW.source_key)=64 AND NEW.source_key NOT GLOB '*[^0-9a-f]*'
   AND typeof(NEW.policy_fingerprint)='text' AND length(NEW.policy_fingerprint)=64 AND NEW.policy_fingerprint NOT GLOB '*[^0-9a-f]*'
   AND json_extract(NEW.source_ref_json,'$.account_id')=NEW.source_account_id AND json_extract(NEW.source_ref_json,'$.workspace_id')=NEW.source_workspace_id
   AND json_extract(NEW.source_ref_json,'$.content_hash')=NEW.source_hash AND json_extract(NEW.coverage_json,'$.assistant.message_id')!=json_extract(NEW.source_ref_json,'$.message_id'),0) ELSE 0 END)=0
BEGIN SELECT RAISE(ABORT,'paired source unavailable'); END;
CREATE TRIGGER s04_pair_binding_immutable BEFORE UPDATE OF owner_id,job_id,source_ref_json,source_key,source_account_id,source_workspace_id,target_workspace_id,owner_epoch,durable_epoch,binding_fingerprint,source_hash,admission_id,policy_fingerprint,connection_id,model_route_id,authority_fingerprint,assignment_json,coverage_json ON fm_v2_s05_job_refs
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=OLD.owner_id AND j.job_id=OLD.job_id AND j.kind='assistant_interpretation_review')
BEGIN SELECT RAISE(ABORT,'paired source binding immutable'); END;
DROP TRIGGER fm_v2_s05_candidate_association_guard;
CREATE TRIGGER fm_v2_s05_candidate_association_guard BEFORE UPDATE OF candidate_id,candidate_attempt_id,candidate_lease_epoch ON fm_v2_s05_job_refs
WHEN OLD.candidate_id IS NOT NULL OR NEW.candidate_id IS NULL OR NOT EXISTS (
 SELECT 1 FROM fm_v2_jobs j JOIN fm_v2_attempts a ON a.owner_id=j.owner_id AND a.job_id=j.job_id
 JOIN fm_v2_s05_reservations r ON r.owner_id=a.owner_id AND r.job_id=a.job_id AND r.attempt_id=a.attempt_id AND r.lease_epoch=a.lease_epoch
 JOIN candidates c ON c.id=NEW.candidate_id AND c.owner=NEW.owner_id AND c.workspace_id=NEW.target_workspace_id
 WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND j.kind IN ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review')
 AND j.workspace_key=NEW.target_workspace_id AND j.project_key='' AND j.state='active' AND j.current_attempt_id=NEW.candidate_attempt_id
 AND a.attempt_id=NEW.candidate_attempt_id AND a.lease_epoch=NEW.candidate_lease_epoch AND a.state='running' AND r.state='dispatched' AND c.status='pending'
 AND (j.kind!='assistant_interpretation_review' OR (
   json_valid(NEW.coverage_json) AND json_extract(NEW.coverage_json,'$.kind')='assistant_interpretation_review'
   AND c.evidence_role='assistant' AND c.source_event_id=json_extract(NEW.coverage_json,'$.assistant.message_id')
   AND EXISTS(SELECT 1 FROM fm_v2_candidates pc WHERE pc.owner_id=NEW.owner_id AND pc.candidate_id=NEW.candidate_id AND pc.workspace_key=NEW.target_workspace_id AND pc.project_key='' AND pc.current_revision=3)
   AND (SELECT count(*) FROM fm_v2_candidate_revisions cr WHERE cr.owner_id=NEW.owner_id AND cr.candidate_id=NEW.candidate_id AND cr.revision IN (1,2,3))=3
   AND NOT EXISTS(SELECT 1 FROM fm_v2_candidate_revisions cr WHERE cr.owner_id=NEW.owner_id AND cr.candidate_id=NEW.candidate_id AND cr.revision IN (1,2,3) AND (
     (SELECT count(*) FROM fm_v2_candidate_evidence ce WHERE ce.owner_id=cr.owner_id AND ce.candidate_id=cr.candidate_id AND ce.revision=cr.revision)!=2 OR
     (SELECT count(DISTINCT e.locator_type) FROM fm_v2_candidate_evidence ce JOIN fm_v2_evidence e ON e.owner_id=ce.owner_id AND e.evidence_id=ce.evidence_id WHERE ce.owner_id=cr.owner_id AND ce.candidate_id=cr.candidate_id AND ce.revision=cr.revision AND e.locator_type IN ('host_archive_part','host_assistant_part') AND json_valid(e.locator_json) AND json_extract(e.locator_json,'$.job_id')=NEW.job_id)!=2
   ))
 )))
BEGIN SELECT RAISE(ABORT,'S05 candidate association unavailable'); END;

CREATE TRIGGER s04_pair_job_identity_immutable BEFORE UPDATE OF owner_id,job_id,kind,workspace_key,project_key,idempotency_key,input_hash ON fm_v2_jobs
WHEN OLD.kind='assistant_interpretation_review' OR NEW.kind='assistant_interpretation_review'
BEGIN SELECT RAISE(ABORT,'paired job identity immutable'); END;

-- SQLite REPLACE can delete conflicting rows without ordinary DELETE triggers.
CREATE TRIGGER s04_pair_ref_no_replace BEFORE INSERT ON fm_v2_s05_job_refs
WHEN EXISTS(SELECT 1 FROM fm_v2_s05_job_refs r JOIN fm_v2_jobs j ON j.owner_id=r.owner_id AND j.job_id=r.job_id WHERE (j.kind='assistant_interpretation_review' OR EXISTS(SELECT 1 FROM fm_v2_jobs n WHERE n.owner_id=NEW.owner_id AND n.job_id=NEW.job_id AND n.kind='assistant_interpretation_review')) AND ((r.owner_id=NEW.owner_id AND r.job_id=NEW.job_id) OR r.source_key=NEW.source_key))
BEGIN SELECT RAISE(ABORT,'paired source cannot be replaced'); END;
CREATE TRIGGER s04_pair_job_no_replace BEFORE INSERT ON fm_v2_jobs
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE (j.kind='assistant_interpretation_review' OR NEW.kind='assistant_interpretation_review') AND j.owner_id=NEW.owner_id AND (j.job_id=NEW.job_id OR j.idempotency_key=NEW.idempotency_key))
BEGIN SELECT RAISE(ABORT,'paired job cannot be replaced'); END;
CREATE TRIGGER s04_pair_ref_no_delete BEFORE DELETE ON fm_v2_s05_job_refs
WHEN EXISTS(SELECT 1 FROM fm_v2_jobs j WHERE j.owner_id=OLD.owner_id AND j.job_id=OLD.job_id AND j.kind='assistant_interpretation_review') AND NOT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context p WHERE p.owner_id=OLD.owner_id)
BEGIN SELECT RAISE(ABORT,'paired source requires owner erasure'); END;
CREATE TRIGGER s04_pair_job_no_delete BEFORE DELETE ON fm_v2_jobs
WHEN OLD.kind='assistant_interpretation_review' AND NOT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context p WHERE p.owner_id=OLD.owner_id)
BEGIN SELECT RAISE(ABORT,'paired job requires owner erasure'); END;

CREATE INDEX s04_pair_candidate ON fm_v2_s05_job_refs(owner_id,candidate_id,job_id) WHERE candidate_id IS NOT NULL;

CREATE INDEX s04_pair_human_page ON fm_v2_s05_job_refs(owner_id,target_workspace_id,candidate_id,job_id) WHERE candidate_id IS NOT NULL AND json_valid(coverage_json) AND json_extract(coverage_json,'$.kind')='assistant_interpretation_review';

CREATE INDEX s04_pair_review_intent ON fm_v2_change_sets(owner_id,json_extract(operation_metadata_json,'$.review.workspace'),json_extract(operation_metadata_json,'$.review.account_id'),json_extract(operation_metadata_json,'$.review.candidate_id'),change_set_id) WHERE reason='s04.paired-review-intent.v1' AND json_valid(operation_metadata_json);
CREATE INDEX s04_pair_intent_invalid ON fm_v2_change_sets(owner_id,change_set_id) WHERE reason='s04.paired-review-intent.v1' AND (CASE WHEN json_valid(operation_metadata_json) THEN json_type(operation_metadata_json,'$.review.candidate_id') IS NOT 'text' OR json_type(operation_metadata_json,'$.review.workspace') IS NOT 'text' OR json_type(operation_metadata_json,'$.review.account_id') IS NOT 'text' ELSE 1 END);
