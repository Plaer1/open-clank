-- Optional offline typed request-purge index; the lifecycle receipt remains authoritative.
CREATE TABLE native_request_purge_terminal (
    family TEXT NOT NULL CHECK(family IN ('knowledge_update_batch_v1')),
    account_sha256 TEXT NOT NULL CHECK(length(account_sha256)=64 AND account_sha256 NOT GLOB '*[^0-9a-f]*'),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    body_sha256 TEXT CHECK(body_sha256 IS NULL OR (length(body_sha256)=64 AND body_sha256 NOT GLOB '*[^0-9a-f]*')),
    purge_receipt_id TEXT NOT NULL,
    purge_context_id TEXT NOT NULL,
    PRIMARY KEY(family,account_sha256,request_sha256),
    FOREIGN KEY(purge_receipt_id) REFERENCES fm_meta(key) DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;
CREATE INDEX native_request_purge_receipt_idx ON native_request_purge_terminal(purge_receipt_id,family);
CREATE TRIGGER native_request_purge_insert_guard BEFORE INSERT ON native_request_purge_terminal
WHEN NOT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.erasure_id=NEW.purge_context_id AND c.source_id IS NULL AND c.authorized_by=c.owner_id AND c.reason='native request guarded owner purge terminal')
BEGIN SELECT RAISE(ABORT,'request terminal requires guarded owner purge'); END;
CREATE TRIGGER native_request_purge_no_update BEFORE UPDATE ON native_request_purge_terminal
BEGIN SELECT RAISE(ABORT,'request terminals are append-only'); END;
CREATE TRIGGER native_request_purge_no_delete BEFORE DELETE ON native_request_purge_terminal
BEGIN SELECT RAISE(ABORT,'request terminals are append-only'); END;
