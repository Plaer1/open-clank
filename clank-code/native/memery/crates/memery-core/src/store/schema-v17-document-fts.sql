CREATE VIRTUAL TABLE fm_v2_chunks_fts USING fts5(owner_id UNINDEXED, chunk_id UNINDEXED, text);
