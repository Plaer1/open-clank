CREATE TABLE IF NOT EXISTS fm_v2_principal_bindings (
            owner_id TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            binding_key TEXT NOT NULL,
            assistant_entity_id TEXT NOT NULL,
            handler_entity_id TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(owner_id, workspace_key, project_key, binding_key)
        );
