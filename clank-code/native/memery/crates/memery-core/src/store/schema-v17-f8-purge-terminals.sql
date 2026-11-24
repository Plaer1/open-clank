-- Optional offline F8 purge-terminal profile. The installer owns the marker;
-- guarded open admits this exact schema only when that marker is present.
CREATE TABLE f8_purge_request_terminal (
    account_sha256 TEXT NOT NULL CHECK(length(account_sha256)=64 AND account_sha256 NOT GLOB '*[^0-9a-f]*'),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    body_sha256 TEXT CHECK(body_sha256 IS NULL OR (length(body_sha256)=64 AND body_sha256 NOT GLOB '*[^0-9a-f]*')),
    purge_receipt_id TEXT NOT NULL,
    purge_context_id TEXT NOT NULL,
    PRIMARY KEY(account_sha256,request_sha256),
    FOREIGN KEY(purge_receipt_id) REFERENCES fm_meta(key) DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;
CREATE INDEX f8_purge_terminal_receipt_idx ON f8_purge_request_terminal(purge_receipt_id);
CREATE TRIGGER f8_purge_terminal_insert_guard BEFORE INSERT ON f8_purge_request_terminal
WHEN NOT EXISTS(SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.erasure_id=NEW.purge_context_id AND c.source_id IS NULL AND c.authorized_by=c.owner_id AND c.reason='f8 guarded owner purge terminal')
BEGIN SELECT RAISE(ABORT,'F8 purge terminal requires guarded owner purge'); END;
CREATE TRIGGER f8_purge_terminal_no_update BEFORE UPDATE ON f8_purge_request_terminal
BEGIN SELECT RAISE(ABORT,'F8 purge terminals are append-only'); END;
CREATE TRIGGER f8_purge_terminal_no_delete BEFORE DELETE ON f8_purge_request_terminal
BEGIN SELECT RAISE(ABORT,'F8 purge terminals are append-only'); END;
