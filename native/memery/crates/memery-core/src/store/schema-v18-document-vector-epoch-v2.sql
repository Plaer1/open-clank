-- Optional stopped-copy v2 derived invalidation profile; existing fm_meta only.
CREATE INDEX document_vector_receipt_source_v2 ON fm_v2_change_sets(owner_id,json_extract(operation_metadata_json,'$.source_id'),json_extract(operation_metadata_json,'$.source_revision'),change_set_id)
 WHERE reason='s01.document-upsert.v1' AND json_valid(operation_metadata_json);
CREATE INDEX document_vector_remove_receipt_source_v2 ON fm_v2_change_sets(owner_id,json_extract(operation_metadata_json,'$.source_id'),json_extract(operation_metadata_json,'$.source_revision'),change_set_id)
 WHERE reason='s01.document-remove.v1' AND json_valid(operation_metadata_json);
CREATE TRIGGER document_epoch_meta_insert BEFORE INSERT ON fm_meta
WHEN NEW.key GLOB 'openclank.document_epoch.v2:*' AND (fm_document_epoch_init_authorized()<>1 OR NEW.value<>'1')
BEGIN SELECT RAISE(ABORT,'document epoch initialization unavailable'); END;
CREATE TRIGGER document_epoch_meta_update BEFORE UPDATE ON fm_meta
WHEN OLD.key GLOB 'openclank.document_epoch.v2:*' AND (
 fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3' OR NEW.key IS NOT OLD.key
 OR CAST(CAST(OLD.value AS INTEGER) AS TEXT)<>OLD.value OR CAST(OLD.value AS INTEGER)<1
 OR CAST(OLD.value AS INTEGER)>=9223372036854775807 OR NEW.value<>CAST(CAST(OLD.value AS INTEGER)+1 AS TEXT))
BEGIN SELECT RAISE(ABORT,'document epoch cannot rewind or overflow'); END;
CREATE TRIGGER document_epoch_meta_delete BEFORE DELETE ON fm_meta
WHEN OLD.key GLOB 'openclank.document_epoch.v2:*' AND fm_document_epoch_lifecycle_authorized()<>1
BEGIN SELECT RAISE(ABORT,'document epoch lifecycle unavailable'); END;
CREATE TRIGGER document_epoch_source_insert AFTER INSERT ON fm_v2_sources
WHEN NEW.workspace_key='' AND NEW.project_key=''
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='openclank.document_epoch.v2:'||hex(CAST(NEW.owner_id AS BLOB)); END;
CREATE TRIGGER document_epoch_source_update AFTER UPDATE ON fm_v2_sources
WHEN (OLD.workspace_key='' AND OLD.project_key='') OR (NEW.workspace_key='' AND NEW.project_key='')
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key IN ('openclank.document_epoch.v2:'||hex(CAST(OLD.owner_id AS BLOB)),'openclank.document_epoch.v2:'||hex(CAST(NEW.owner_id AS BLOB))); END;
CREATE TRIGGER document_epoch_source_delete AFTER DELETE ON fm_v2_sources
WHEN OLD.workspace_key='' AND OLD.project_key=''
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='openclank.document_epoch.v2:'||hex(CAST(OLD.owner_id AS BLOB)); END;
CREATE TRIGGER document_epoch_document_insert AFTER INSERT ON fm_v2_documents
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='openclank.document_epoch.v2:'||hex(CAST(NEW.owner_id AS BLOB)); END;
CREATE TRIGGER document_epoch_document_update AFTER UPDATE ON fm_v2_documents
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key IN ('openclank.document_epoch.v2:'||hex(CAST(OLD.owner_id AS BLOB)),'openclank.document_epoch.v2:'||hex(CAST(NEW.owner_id AS BLOB))); END;
CREATE TRIGGER document_epoch_document_delete AFTER DELETE ON fm_v2_documents
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='openclank.document_epoch.v2:'||hex(CAST(OLD.owner_id AS BLOB)); END;
CREATE TRIGGER document_epoch_chunk_insert AFTER INSERT ON fm_v2_chunks
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='openclank.document_epoch.v2:'||hex(CAST(NEW.owner_id AS BLOB)); END;
CREATE TRIGGER document_epoch_chunk_update AFTER UPDATE ON fm_v2_chunks
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key IN ('openclank.document_epoch.v2:'||hex(CAST(OLD.owner_id AS BLOB)),'openclank.document_epoch.v2:'||hex(CAST(NEW.owner_id AS BLOB))); END;
CREATE TRIGGER document_epoch_chunk_delete AFTER DELETE ON fm_v2_chunks
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='openclank.document_epoch.v2:'||hex(CAST(OLD.owner_id AS BLOB)); END;
CREATE TRIGGER document_epoch_receipt_insert AFTER INSERT ON fm_v2_change_sets
WHEN NEW.reason IN ('s01.document-upsert.v1','s01.document-remove.v1')
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='openclank.document_epoch.v2:'||hex(CAST(NEW.owner_id AS BLOB)); END;
CREATE TRIGGER document_epoch_receipt_update AFTER UPDATE ON fm_v2_change_sets
WHEN OLD.reason IN ('s01.document-upsert.v1','s01.document-remove.v1') OR NEW.reason IN ('s01.document-upsert.v1','s01.document-remove.v1')
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key IN ('openclank.document_epoch.v2:'||hex(CAST(OLD.owner_id AS BLOB)),'openclank.document_epoch.v2:'||hex(CAST(NEW.owner_id AS BLOB))); END;
CREATE TRIGGER document_epoch_receipt_delete AFTER DELETE ON fm_v2_change_sets
WHEN OLD.reason IN ('s01.document-upsert.v1','s01.document-remove.v1')
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='openclank.document_epoch.v2:'||hex(CAST(OLD.owner_id AS BLOB)); END;
CREATE TRIGGER document_epoch_outbox_insert AFTER INSERT ON fm_v2_outbox
WHEN NEW.kind IN ('s01_document_upsert','s01_document_remove')
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='openclank.document_epoch.v2:'||hex(CAST(NEW.owner_id AS BLOB)); END;
CREATE TRIGGER document_epoch_outbox_update AFTER UPDATE ON fm_v2_outbox
WHEN OLD.kind IN ('s01_document_upsert','s01_document_remove') OR NEW.kind IN ('s01_document_upsert','s01_document_remove')
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key IN ('openclank.document_epoch.v2:'||hex(CAST(OLD.owner_id AS BLOB)),'openclank.document_epoch.v2:'||hex(CAST(NEW.owner_id AS BLOB))); END;
CREATE TRIGGER document_epoch_outbox_delete AFTER DELETE ON fm_v2_outbox
WHEN OLD.kind IN ('s01_document_upsert','s01_document_remove')
BEGIN UPDATE fm_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='openclank.document_epoch.v2:'||hex(CAST(OLD.owner_id AS BLOB)); END;

CREATE TRIGGER document_epoch_meta_reclassify BEFORE UPDATE OF key ON fm_meta
WHEN (OLD.key IS NULL OR OLD.key NOT GLOB 'openclank.document_epoch.v2:*') AND NEW.key GLOB 'openclank.document_epoch.v2:*'
BEGIN SELECT RAISE(ABORT,'document epoch key reclassification unavailable'); END;
