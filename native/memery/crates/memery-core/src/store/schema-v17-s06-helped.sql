-- Offline optional profile delta; never applied by runtime startup.
DROP INDEX s06_source_erase;
CREATE INDEX s06_source_erase ON fm_v2_change_sets(owner_id,source_event_id,change_set_id)
WHERE reason IN ('s06.observation-origin.v1','s06.found.v1','s06.observation-rebind.v2','s06.admitted.v1','s06.helped.v1');
CREATE INDEX s06_helped_source ON fm_v2_outbox(owner_id,json_extract(payload_json, '$.source_id'),event_id)
WHERE kind = 's06_helped' AND json_valid(payload_json);
CREATE INDEX s06_helped_invalid ON fm_v2_outbox(owner_id,event_id)
WHERE kind = 's06_helped' AND (NOT json_valid(payload_json) OR
  CASE WHEN json_valid(payload_json) THEN json_type(payload_json, '$.source_id') IS NOT 'text'
    OR length(json_extract(payload_json, '$.source_id')) != 73
    OR substr(json_extract(payload_json, '$.source_id'),1,9) != 'hostpart_'
    OR substr(json_extract(payload_json, '$.source_id'),10) GLOB '*[^0-9a-f]*'
  ELSE 0 END);
