-- Inactive offline companion: apply only when fm_v2_principal_bindings exists.
-- Missing function, NULL, v1 and unknown capabilities refuse legacy writes.
CREATE TRIGGER s03_writer_principal_binding_insert BEFORE INSERT ON fm_v2_principal_bindings
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;
CREATE TRIGGER s03_writer_principal_binding_update BEFORE UPDATE ON fm_v2_principal_bindings
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;
CREATE TRIGGER s03_writer_principal_binding_delete BEFORE DELETE ON fm_v2_principal_bindings
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;
