-- Inactive offline capability: exact observation-origin inventory.  This file
-- is not loaded by startup and does not change a schema version marker.
CREATE INDEX s06_observation_origin_account_chat_message
ON fm_v2_change_sets(
  json_extract(operation_metadata_json, '$.source.account_id'),
  json_extract(operation_metadata_json, '$.source.chat_id'),
  json_extract(operation_metadata_json, '$.source.message_id'), owner_id, change_set_id
)
WHERE reason = 's06.observation-origin.v1' AND json_valid(operation_metadata_json);
CREATE INDEX s06_observation_origin_account
ON fm_v2_change_sets(json_extract(operation_metadata_json, '$.source.account_id'),owner_id,change_set_id)
WHERE reason = 's06.observation-origin.v1' AND json_valid(operation_metadata_json);
CREATE INDEX s06_observation_origin_account_chat
ON fm_v2_change_sets(json_extract(operation_metadata_json, '$.source.account_id'),
  json_extract(operation_metadata_json, '$.source.chat_id'),owner_id,change_set_id)
WHERE reason = 's06.observation-origin.v1' AND json_valid(operation_metadata_json);
CREATE INDEX s06_observation_origin_invalid
ON fm_v2_change_sets(owner_id,change_set_id)
WHERE reason = 's06.observation-origin.v1' AND NOT json_valid(operation_metadata_json);
CREATE INDEX s06_observation_origin_invalid_account
ON fm_v2_change_sets(owner_id,change_set_id)
WHERE reason = 's06.observation-origin.v1' AND json_valid(operation_metadata_json)
  AND (json_type(operation_metadata_json, '$.source.account_id') IS NOT 'text'
       OR length(json_extract(operation_metadata_json, '$.source.account_id')) > 512
       OR trim(json_extract(operation_metadata_json, '$.source.account_id')) != json_extract(operation_metadata_json, '$.source.account_id')
       OR json_extract(operation_metadata_json, '$.source.account_id') = '');
CREATE INDEX s06_observation_origin_owner_account
ON fm_v2_change_sets(owner_id,json_extract(operation_metadata_json, '$.source.account_id'),change_set_id)
WHERE reason = 's06.observation-origin.v1' AND json_valid(operation_metadata_json);
CREATE INDEX s06_found_source
ON fm_v2_outbox(owner_id,json_extract(payload_json, '$.source_id'),event_id)
WHERE kind = 's06_found' AND json_valid(payload_json);
CREATE INDEX s06_found_invalid ON fm_v2_outbox(owner_id,event_id)
WHERE kind = 's06_found' AND NOT json_valid(payload_json);
CREATE INDEX s06_observation_rebind_current
ON fm_v2_change_sets(owner_id,json_extract(operation_metadata_json, '$.origin_id'),
  json_extract(operation_metadata_json, '$.current_fingerprint'),change_set_id)
WHERE reason = 's06.observation-rebind.v2' AND json_valid(operation_metadata_json);
CREATE INDEX s06_observation_rebind_invalid_json ON fm_v2_change_sets(owner_id,change_set_id)
WHERE reason = 's06.observation-rebind.v2' AND NOT json_valid(operation_metadata_json);
CREATE INDEX s06_observation_rebind_invalid_fields ON fm_v2_change_sets(owner_id,change_set_id)
WHERE reason = 's06.observation-rebind.v2' AND json_valid(operation_metadata_json)
  AND (json_type(operation_metadata_json, '$.origin_id') IS NOT 'text'
       OR length(json_extract(operation_metadata_json, '$.origin_id')) != 77
       OR substr(json_extract(operation_metadata_json, '$.origin_id'),1,13) != 'cs_s06origin_'
       OR substr(json_extract(operation_metadata_json, '$.origin_id'),14) GLOB '*[^0-9a-f]*'
       OR json_type(operation_metadata_json, '$.current_fingerprint') IS NOT 'text'
       OR length(json_extract(operation_metadata_json, '$.current_fingerprint')) != 64
       OR json_extract(operation_metadata_json, '$.current_fingerprint') GLOB '*[^0-9a-f]*'
       OR (json_type(operation_metadata_json, '$.prior_receipt_id') IS NOT 'null' AND json_type(operation_metadata_json, '$.prior_receipt_id') IS NOT 'text')
       OR (json_type(operation_metadata_json, '$.prior_receipt_hash') IS NOT 'null' AND json_type(operation_metadata_json, '$.prior_receipt_hash') IS NOT 'text')
       OR (json_type(operation_metadata_json, '$.prior_receipt_id') IS NOT json_type(operation_metadata_json, '$.prior_receipt_hash'))
       OR (json_type(operation_metadata_json, '$.prior_receipt_id') = 'text' AND (length(json_extract(operation_metadata_json, '$.prior_receipt_id')) != 76
          OR substr(json_extract(operation_metadata_json, '$.prior_receipt_id'),1,12) != 'cs_s06renew_'
          OR substr(json_extract(operation_metadata_json, '$.prior_receipt_id'),13) GLOB '*[^0-9a-f]*'
          OR length(json_extract(operation_metadata_json, '$.prior_receipt_hash')) != 64
          OR json_extract(operation_metadata_json, '$.prior_receipt_hash') GLOB '*[^0-9a-f]*')));
CREATE INDEX s06_observation_rebind_successor
ON fm_v2_change_sets(owner_id,json_extract(operation_metadata_json, '$.origin_id'),
  json_extract(operation_metadata_json, '$.prior_receipt_id'),change_set_id)
WHERE reason = 's06.observation-rebind.v2' AND json_valid(operation_metadata_json);
