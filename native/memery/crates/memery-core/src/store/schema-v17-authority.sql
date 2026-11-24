-- Inactive S03 candidate. No runtime schema/version or writer epoch activation.
-- Versioned operation metadata is immutable history on the existing change set.
-- NULL identifies older changes; authenticated writers validate the full envelope.
CREATE INDEX s03_mirror_receipt_context ON fm_v2_outbox(owner_id,kind,json_extract(payload_json,'$.curated_id'),source_revision) WHERE kind IN ('candidate_review','mirror_update') AND json_valid(payload_json);
ALTER TABLE fm_v2_change_sets ADD COLUMN operation_metadata_json TEXT
    CHECK (operation_metadata_json IS NULL OR
           (json_valid(operation_metadata_json) AND json_type(operation_metadata_json) = 'object'));
CREATE TABLE fm_v2_revision_field_authority (
    owner_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    field_path TEXT NOT NULL CHECK (field_path = '' OR substr(field_path,1,1) = '/'),
    origin TEXT NOT NULL CHECK (origin IN ('manual','inferred','inherited','legacy_unknown')),
    producer_type TEXT NOT NULL CHECK (length(trim(producer_type)) > 0),
    producer_id TEXT NOT NULL CHECK (length(trim(producer_id)) > 0),
    approved_by TEXT,
    source_event_id TEXT,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json) AND json_type(evidence_json) = 'array'),
    change_set_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    PRIMARY KEY (owner_id, block_id, revision, field_path),
    FOREIGN KEY (owner_id, block_id, revision) REFERENCES fm_v2_knowledge_revisions(owner_id, block_id, revision),
    FOREIGN KEY (owner_id, change_set_id) REFERENCES fm_v2_change_sets(owner_id, change_set_id),
    CHECK ((origin = 'manual' AND approved_by IS NOT NULL AND approved_by = owner_id) OR (origin <> 'manual' AND approved_by IS NULL))
);
CREATE TRIGGER fm_v2_revision_field_authority_validate BEFORE INSERT ON fm_v2_revision_field_authority
WHEN EXISTS (SELECT 1 FROM fm_v2_revision_field_authority a WHERE a.owner_id=NEW.owner_id AND a.block_id=NEW.block_id AND a.revision=NEW.revision AND a.field_path=NEW.field_path)
  OR NOT EXISTS (
    SELECT 1 FROM fm_v2_knowledge_revisions r JOIN fm_v2_change_sets c
      ON c.owner_id=r.owner_id AND c.change_set_id=r.change_set_id
    WHERE r.owner_id=NEW.owner_id AND r.block_id=NEW.block_id AND r.revision=NEW.revision
      AND r.change_set_id=NEW.change_set_id AND r.actor_type=NEW.producer_type AND r.actor_id=NEW.producer_id
      AND c.actor_type=NEW.producer_type AND c.actor_id=NEW.producer_id
  )
BEGIN SELECT RAISE(ABORT, 'invalid field authority'); END;
CREATE TRIGGER fm_v2_revision_field_authority_no_update BEFORE UPDATE ON fm_v2_revision_field_authority
BEGIN SELECT RAISE(ABORT, 'field authority is append-only'); END;
CREATE TRIGGER fm_v2_revision_field_authority_no_delete BEFORE DELETE ON fm_v2_revision_field_authority
WHEN NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=OLD.owner_id)
BEGIN SELECT RAISE(ABORT, 'field authority is append-only'); END;
