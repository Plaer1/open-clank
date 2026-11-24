"""Canonical optional S11 host-database rating profile; never applied at runtime."""

TABLE = "chat_rating_events"
CREATE = """CREATE TABLE chat_rating_events (
    event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    rater_account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    workspace_id TEXT,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('message','chat','section')),
    target_key TEXT NOT NULL,
    descriptor_json TEXT NOT NULL,
    descriptor_hash TEXT NOT NULL CHECK(length(descriptor_hash) = 64),
    message_id TEXT,
    message_role TEXT CHECK(message_role IN ('user','assistant')), 
    source_revision INTEGER CHECK(source_revision > 0),
    source_hash TEXT CHECK(length(source_hash) = 64),
    rating_revision INTEGER,
    action TEXT NOT NULL CHECK(action IN ('set','clear')),
    value INTEGER,
    note TEXT,
    origin TEXT NOT NULL CHECK(origin IN ('local','external_unverified')),
    asserted_origin TEXT,
    request_key_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK ((action='set' AND typeof(value)='integer' AND value BETWEEN 0 AND 10) OR (action='clear' AND value IS NULL)),
    CHECK ((origin='local' AND rating_revision IS NOT NULL AND rating_revision > 0 AND asserted_origin IS NULL) OR (origin='external_unverified' AND rating_revision IS NULL AND asserted_origin IS NOT NULL)),
    UNIQUE(account_id, chat_id, target_kind, target_key, rating_revision),
    UNIQUE(account_id, request_key_hash)
)"""

# Previous undeployed profile is recognized only to refuse runtime use.
CREATE_V1 = "CREATE TABLE chat_rating_events (\n    event_id TEXT PRIMARY KEY,\n    account_id TEXT NOT NULL,\n    rater_account_id TEXT NOT NULL,\n    chat_id TEXT NOT NULL,\n    workspace_id TEXT,\n    message_id TEXT NOT NULL,\n    message_role TEXT NOT NULL CHECK(message_role IN ('user','assistant')),\n    source_revision INTEGER NOT NULL CHECK(source_revision > 0),\n    source_hash TEXT NOT NULL CHECK(length(source_hash) = 64),\n    rating_revision INTEGER,\n    action TEXT NOT NULL CHECK(action IN ('set','clear')),\n    value INTEGER,\n    note TEXT,\n    origin TEXT NOT NULL CHECK(origin IN ('local','external_unverified')),\n    asserted_origin TEXT,\n    request_key_hash TEXT NOT NULL,\n    payload_hash TEXT NOT NULL,\n    created_at TEXT NOT NULL,\n    CHECK ((action='set' AND typeof(value)='integer' AND value BETWEEN 0 AND 10) OR (action='clear' AND value IS NULL)),\n    CHECK ((origin='local' AND rating_revision IS NOT NULL AND rating_revision > 0 AND asserted_origin IS NULL) OR (origin='external_unverified' AND rating_revision IS NULL AND asserted_origin IS NOT NULL)),\n    UNIQUE(account_id, chat_id, message_id, rating_revision),\n    UNIQUE(account_id, request_key_hash)\n)"
