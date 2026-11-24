-- Optional offline account-chat profile. The nullable column is composed from
-- the canonical S05 base; this guard binds its NULL only to the closed v2 ref.
CREATE TRIGGER s04_s05_account_chat_insert BEFORE INSERT ON fm_v2_s05_job_refs
WHEN NEW.source_workspace_id IS NULL OR (json_valid(NEW.source_ref_json) AND json_extract(NEW.source_ref_json,'$.version')=2)
BEGIN
 SELECT CASE WHEN (CASE WHEN json_valid(NEW.source_ref_json) THEN COALESCE(
  json_type(NEW.source_ref_json)='object' AND (SELECT count(*) FROM json_each(NEW.source_ref_json))=11
  AND json_type(NEW.source_ref_json,'$.version')='integer' AND json_extract(NEW.source_ref_json,'$.version')=2
  AND json_extract(NEW.source_ref_json,'$.kind')='host_user_body'
  AND json_type(NEW.source_ref_json,'$.account_id')='text' AND json_extract(NEW.source_ref_json,'$.account_id')=NEW.source_account_id
  AND json_type(NEW.source_ref_json,'$.chat_id')='text' AND length(json_extract(NEW.source_ref_json,'$.chat_id')) BETWEEN 1 AND 512
  AND json_extract(NEW.source_ref_json,'$.actor_id')='main'
  AND json_type(NEW.source_ref_json,'$.message_id')='text' AND length(json_extract(NEW.source_ref_json,'$.message_id')) BETWEEN 1 AND 512
  AND json_extract(NEW.source_ref_json,'$.part_id')='body'
  AND json_type(NEW.source_ref_json,'$.revision')='integer' AND json_extract(NEW.source_ref_json,'$.revision')>0
  AND json_type(NEW.source_ref_json,'$.content_hash')='text' AND json_extract(NEW.source_ref_json,'$.content_hash')=NEW.source_hash
  AND length(NEW.source_hash)=64 AND NEW.source_hash NOT GLOB '*[^0-9a-f]*'
  AND json_type(NEW.source_ref_json,'$.workspace_id')='null' AND NEW.source_workspace_id IS NULL
  AND json_type(NEW.source_ref_json,'$.project_id')='null',0) ELSE 0 END)=0
 THEN RAISE(ABORT,'account chat source unavailable') END;
END;

CREATE TRIGGER s04_s05_account_chat_immutable BEFORE UPDATE OF source_ref_json,source_workspace_id,source_account_id,source_hash ON fm_v2_s05_job_refs
WHEN OLD.source_workspace_id IS NULL OR NEW.source_workspace_id IS NULL
BEGIN SELECT RAISE(ABORT,'account chat source is immutable'); END;
