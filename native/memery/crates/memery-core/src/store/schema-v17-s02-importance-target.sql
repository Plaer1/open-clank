-- Optional stopped-copy target-purpose fence; importance profile required.
CREATE TRIGGER importance_target_selection_guard BEFORE INSERT ON fm_v2_change_sets
WHEN NEW.reason='s08.selection.v1' AND (NEW.operation_metadata_json IS NULL OR NOT json_valid(NEW.operation_metadata_json)
 OR (json_type(NEW.operation_metadata_json,'$.importance_target') IS NOT NULL AND fm_s02_policy_authorized() IS NOT 1))
BEGIN SELECT RAISE(ABORT,'S02 importance target requires guarded writer'); END;
INSERT INTO fm_meta(key,value) VALUES('openclank.s02.importance_target.v1','1');
