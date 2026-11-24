-- Offline candidate-copy profile only. Never apply on application startup.
-- Existing foreground rows keep NULL: an owner alias cannot prove an account.
DROP TABLE fm_v2_s05_schema;
CREATE TABLE fm_v2_s05_schema (version INTEGER NOT NULL CHECK(version=4));
INSERT INTO fm_v2_s05_schema VALUES (4);
ALTER TABLE fm_v2_s05_foreground ADD COLUMN account_id TEXT;
CREATE TRIGGER fm_v2_s05_foreground_account_insert BEFORE INSERT ON fm_v2_s05_foreground
WHEN (NEW.account_id IS NULL AND NEW.state!='unknown') OR (NEW.account_id IS NOT NULL AND (length(NEW.account_id)=0 OR length(NEW.account_id)>512 OR NEW.account_id!=trim(NEW.account_id)))
BEGIN SELECT RAISE(ABORT,'S05 foreground account unavailable'); END;
CREATE TRIGGER fm_v2_s05_foreground_account_immutable BEFORE UPDATE OF account_id ON fm_v2_s05_foreground
WHEN NEW.account_id IS NOT OLD.account_id
BEGIN SELECT RAISE(ABORT,'S05 foreground account immutable'); END;
CREATE TRIGGER fm_v2_s05_foreground_unbound_state BEFORE UPDATE OF state ON fm_v2_s05_foreground
WHEN OLD.account_id IS NULL AND NEW.state!=OLD.state AND NEW.state!='unknown'
BEGIN SELECT RAISE(ABORT,'S05 unbound foreground remains unresolved'); END;
