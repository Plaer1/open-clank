-- Offline optional source-specific physical observation-erasure capability.
CREATE INDEX s06_source_erase ON fm_v2_change_sets(owner_id,source_event_id,change_set_id)
WHERE reason IN ('s06.observation-origin.v1','s06.found.v1','s06.observation-rebind.v2','s06.admitted.v1');
