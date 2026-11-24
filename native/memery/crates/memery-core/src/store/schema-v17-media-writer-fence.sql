-- Inactive offline companion, only when both optional media tables exist.
-- Compatibility markers do not authorize a semantic write.
CREATE TRIGGER s03_writer_media_assets_insert BEFORE INSERT ON fm_v2_media_assets WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2' BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;
CREATE TRIGGER s03_writer_media_assets_update BEFORE UPDATE ON fm_v2_media_assets WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2' BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;
CREATE TRIGGER s03_writer_media_assets_delete BEFORE DELETE ON fm_v2_media_assets WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2' BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;
CREATE TRIGGER s03_writer_media_representations_insert BEFORE INSERT ON fm_v2_media_representations WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2' BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;
CREATE TRIGGER s03_writer_media_representations_update BEFORE UPDATE ON fm_v2_media_representations WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2' BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;
CREATE TRIGGER s03_writer_media_representations_delete BEFORE DELETE ON fm_v2_media_representations WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2' BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;
