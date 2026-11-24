-- Explicit offline General Hex profile. The five primary-key autoindexes are
-- SQLite-owned; there are no additional General Hex indexes or triggers.
CREATE TABLE IF NOT EXISTS fm_general_hex_heads (
    owner_id TEXT NOT NULL, hex_id TEXT NOT NULL,
    revision INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(owner_id,hex_id));
CREATE TABLE IF NOT EXISTS fm_general_hex_revisions (
    owner_id TEXT NOT NULL, hex_id TEXT NOT NULL, revision INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY(owner_id,hex_id,revision));
CREATE TABLE IF NOT EXISTS fm_general_hex_context_tags (
    owner_id TEXT NOT NULL, context_kind TEXT NOT NULL,
    context_id TEXT NOT NULL, revision INTEGER NOT NULL, tags_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_id,context_kind,context_id),
    CHECK(context_kind IN ('workspace','task')));
CREATE TABLE IF NOT EXISTS fm_general_hex_snapshots (
    owner_id TEXT NOT NULL, boundary_id TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL, PRIMARY KEY(owner_id,boundary_id));
CREATE TABLE IF NOT EXISTS fm_general_hex_exchange_drafts (
    owner_id TEXT NOT NULL, draft_id TEXT NOT NULL, revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(owner_id,draft_id));
