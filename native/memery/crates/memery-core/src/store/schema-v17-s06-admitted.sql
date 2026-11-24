-- Optional offline S06 admitted-event capability. No version or runtime DDL.
CREATE INDEX s06_admitted_source ON fm_v2_outbox(owner_id,json_extract(payload_json, '$.source_id'),event_id)
WHERE kind = 's06_admitted' AND json_valid(payload_json);
CREATE INDEX s06_admitted_invalid ON fm_v2_outbox(owner_id,event_id)
WHERE kind = 's06_admitted' AND (NOT json_valid(payload_json) OR
  CASE WHEN json_valid(payload_json) THEN json_type(payload_json, '$.source_id') IS NOT 'text'
    OR length(json_extract(payload_json, '$.source_id')) != 73
    OR substr(json_extract(payload_json, '$.source_id'),1,9) != 'hostpart_'
    OR substr(json_extract(payload_json, '$.source_id'),10) GLOB '*[^0-9a-f]*'
  ELSE 0 END);
