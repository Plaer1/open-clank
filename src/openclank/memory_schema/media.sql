CREATE TABLE IF NOT EXISTS fm_v2_media_assets (
                    owner_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    byte_size INTEGER NOT NULL,
                    original_sha256 TEXT NOT NULL,
                    canonical_sha256 TEXT NOT NULL,
                    blob_key TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, asset_id),
                    UNIQUE(owner_id, source_id, asset_id)
                );
                CREATE TABLE IF NOT EXISTS fm_v2_media_representations (
                    owner_id TEXT NOT NULL,
                    representation_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, representation_id),
                    FOREIGN KEY(owner_id, asset_id) REFERENCES fm_v2_media_assets(owner_id, asset_id)
                );
