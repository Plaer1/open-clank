-- Optional stopped-copy v18 derived vector representations. No authority is granted here.
CREATE TABLE fm_v2_vector_profiles (
  owner_id TEXT NOT NULL, generation_id TEXT NOT NULL, profile_id TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version=1),
  family TEXT NOT NULL CHECK(family IN ('ivf_flat','hnsw','scalar8','pq')),
  descriptor_json TEXT NOT NULL CHECK(json_valid(descriptor_json) AND length(CAST(descriptor_json AS BLOB)) BETWEEN 2 AND 8192),
  flat_sha256 TEXT NOT NULL CHECK(length(flat_sha256)=64),
  training_sha256 TEXT NOT NULL CHECK(length(training_sha256)=64),
  representation_sha256 TEXT NOT NULL CHECK(length(representation_sha256)=64),
  byte_count INTEGER NOT NULL CHECK(byte_count BETWEEN 1 AND 16777216),
  chunk_count INTEGER NOT NULL CHECK(chunk_count BETWEEN 1 AND 64),
  PRIMARY KEY(owner_id,generation_id,profile_id),
  FOREIGN KEY(owner_id,generation_id) REFERENCES fm_v2_derived_generations(owner_id,generation_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE fm_v2_vector_profile_chunks (
  owner_id TEXT NOT NULL, generation_id TEXT NOT NULL, profile_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 63),
  bytes BLOB NOT NULL CHECK(length(bytes) BETWEEN 1 AND 262144),
  PRIMARY KEY(owner_id,generation_id,profile_id,ordinal),
  FOREIGN KEY(owner_id,generation_id,profile_id) REFERENCES fm_v2_vector_profiles(owner_id,generation_id,profile_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE TRIGGER fm_v2_vector_profile_embedding_delete BEFORE DELETE ON fm_v2_chunk_embeddings
BEGIN DELETE FROM fm_v2_vector_profiles WHERE owner_id=OLD.owner_id AND generation_id=OLD.generation_id; END;
CREATE TRIGGER fm_v2_vector_profile_embedding_update BEFORE UPDATE OF owner_id,generation_id,chunk_id,dimension,embedding,content_hash ON fm_v2_chunk_embeddings
BEGIN DELETE FROM fm_v2_vector_profiles WHERE (owner_id=OLD.owner_id AND generation_id=OLD.generation_id) OR (owner_id=NEW.owner_id AND generation_id=NEW.generation_id); END;
CREATE TRIGGER fm_v2_vector_profile_owner_transition BEFORE UPDATE OF owner_id ON fm_v2_derived_generations
WHEN OLD.owner_id<>NEW.owner_id
BEGIN DELETE FROM fm_v2_vector_profiles WHERE owner_id=OLD.owner_id AND generation_id=OLD.generation_id; END;
CREATE TRIGGER fm_v2_vector_profiles_insert_guard BEFORE INSERT ON fm_v2_vector_profiles
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
BEGIN SELECT RAISE(ABORT,'guarded vector profile write unavailable'); END;
CREATE TRIGGER fm_v2_vector_profiles_update_guard BEFORE UPDATE ON fm_v2_vector_profiles
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
BEGIN SELECT RAISE(ABORT,'guarded vector profile write unavailable'); END;
CREATE TRIGGER fm_v2_vector_profiles_delete_guard BEFORE DELETE ON fm_v2_vector_profiles
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
BEGIN SELECT RAISE(ABORT,'guarded vector profile write unavailable'); END;
CREATE TRIGGER fm_v2_vector_profile_chunks_insert_guard BEFORE INSERT ON fm_v2_vector_profile_chunks
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
BEGIN SELECT RAISE(ABORT,'guarded vector profile write unavailable'); END;
CREATE TRIGGER fm_v2_vector_profile_chunks_update_guard BEFORE UPDATE ON fm_v2_vector_profile_chunks
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
BEGIN SELECT RAISE(ABORT,'guarded vector profile write unavailable'); END;
CREATE TRIGGER fm_v2_vector_profile_chunks_delete_guard BEFORE DELETE ON fm_v2_vector_profile_chunks
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v3'
BEGIN SELECT RAISE(ABORT,'guarded vector profile write unavailable'); END;
