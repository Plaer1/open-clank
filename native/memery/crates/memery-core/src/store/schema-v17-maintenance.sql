-- Optional offline-only capability: one exact source-linked compaction artifact index.
CREATE INDEX derived_compaction_source ON fm_v2_evidence(owner_id,source_id,evidence_id) WHERE locator_type='derived_compaction_summary';
CREATE INDEX derived_compaction_changes ON fm_v2_change_sets(owner_id,source_event_id,change_set_id) WHERE reason='derived.compaction.v1';
