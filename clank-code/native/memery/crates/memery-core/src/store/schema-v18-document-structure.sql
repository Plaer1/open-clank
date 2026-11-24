CREATE TABLE fm_v2_doc_structure_heads (
    owner_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    source_key TEXT NOT NULL CHECK (length(source_key)=64),
    kind TEXT NOT NULL CHECK (kind IN ('file','attachment','journal')),
    workspace TEXT NOT NULL,
    resource TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch>=0),
    generation TEXT CHECK (generation IS NULL OR length(generation)=64),
    PRIMARY KEY (owner_id,source_key)
);
CREATE TABLE fm_v2_doc_structure_jobs (
    owner_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    generation TEXT NOT NULL CHECK (length(generation)=64),
    base_epoch INTEGER NOT NULL CHECK (base_epoch>=0),
    revision TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
    total INTEGER NOT NULL CHECK (total BETWEEN 1 AND 512),
    next INTEGER NOT NULL CHECK (next BETWEEN 0 AND total),
    state TEXT NOT NULL CHECK (state IN ('active','complete')),
    mentions_json TEXT,
    PRIMARY KEY (owner_id,source_key,generation),
    FOREIGN KEY (owner_id,source_key) REFERENCES fm_v2_doc_structure_heads(owner_id,source_key) ON DELETE CASCADE,
    CHECK ((state='active' AND next<total AND mentions_json IS NULL)
        OR (state='complete' AND next=total AND mentions_json IS NOT NULL AND json_valid(mentions_json) AND length(CAST(mentions_json AS BLOB))<=65536))
);
CREATE TABLE fm_v2_doc_structure_chunks (
    owner_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    generation TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 511),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
    locator_json TEXT NOT NULL CHECK (json_valid(locator_json) AND length(CAST(locator_json AS BLOB))<=8192),
    PRIMARY KEY (owner_id,source_key,generation,ordinal),
    FOREIGN KEY (owner_id,source_key,generation) REFERENCES fm_v2_doc_structure_jobs(owner_id,source_key,generation) ON DELETE CASCADE
);
CREATE TRIGGER doc_structure_heads_insert BEFORE INSERT ON fm_v2_doc_structure_heads
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3' OR NEW.epoch<>0 OR NEW.generation IS NOT NULL
BEGIN SELECT RAISE(ABORT,'document structure writer unavailable'); END;
CREATE TRIGGER doc_structure_heads_update BEFORE UPDATE ON fm_v2_doc_structure_heads
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
 OR NEW.owner_id<>OLD.owner_id OR NEW.account_id<>OLD.account_id OR NEW.source_key<>OLD.source_key
 OR NEW.kind<>OLD.kind OR NEW.workspace<>OLD.workspace OR NEW.resource<>OLD.resource OR NEW.epoch<>OLD.epoch+1
 OR NOT EXISTS(SELECT 1 FROM fm_v2_doc_structure_jobs j WHERE j.owner_id=NEW.owner_id AND j.source_key=NEW.source_key
               AND j.generation=NEW.generation AND j.state='complete' AND j.base_epoch=OLD.epoch)
BEGIN SELECT RAISE(ABORT,'document structure publication unavailable'); END;
CREATE TRIGGER doc_structure_heads_delete BEFORE DELETE ON fm_v2_doc_structure_heads
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
BEGIN SELECT RAISE(ABORT,'document structure writer unavailable'); END;
CREATE TRIGGER doc_structure_jobs_insert BEFORE INSERT ON fm_v2_doc_structure_jobs
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3' OR NEW.state<>'active' OR NEW.next<>0
BEGIN SELECT RAISE(ABORT,'document structure job unavailable'); END;
CREATE TRIGGER doc_structure_jobs_update BEFORE UPDATE ON fm_v2_doc_structure_jobs
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3' OR OLD.state<>'active'
 OR NEW.owner_id<>OLD.owner_id OR NEW.source_key<>OLD.source_key OR NEW.generation<>OLD.generation
 OR NEW.base_epoch<>OLD.base_epoch OR NEW.revision<>OLD.revision OR NEW.content_sha256<>OLD.content_sha256
 OR NEW.total<>OLD.total OR NEW.next<=OLD.next
 OR NEW.next<>(SELECT count(*) FROM fm_v2_doc_structure_chunks c WHERE c.owner_id=NEW.owner_id
              AND c.source_key=NEW.source_key AND c.generation=NEW.generation)
BEGIN SELECT RAISE(ABORT,'document structure checkpoint unavailable'); END;
CREATE TRIGGER doc_structure_jobs_delete BEFORE DELETE ON fm_v2_doc_structure_jobs
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
BEGIN SELECT RAISE(ABORT,'document structure writer unavailable'); END;
CREATE TRIGGER doc_structure_chunks_insert BEFORE INSERT ON fm_v2_doc_structure_chunks
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
 OR NOT EXISTS(SELECT 1 FROM fm_v2_doc_structure_jobs j WHERE j.owner_id=NEW.owner_id AND j.source_key=NEW.source_key
               AND j.generation=NEW.generation AND j.state='active')
BEGIN SELECT RAISE(ABORT,'document structure segment unavailable'); END;
CREATE TRIGGER doc_structure_chunks_update BEFORE UPDATE ON fm_v2_doc_structure_chunks
BEGIN SELECT RAISE(ABORT,'document structure segment immutable'); END;
CREATE TRIGGER doc_structure_chunks_delete BEFORE DELETE ON fm_v2_doc_structure_chunks
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
BEGIN SELECT RAISE(ABORT,'document structure writer unavailable'); END;
