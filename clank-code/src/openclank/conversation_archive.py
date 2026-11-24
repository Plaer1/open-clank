"""Lossless ordered source-part conversation archive with durable outbox.

Canonical full-part history for Open Clank. Host chat rows remain a
presentation projection; this archive is the source that compaction,
index rebuilds and projection replacement may not erase.

Identity axes (immutable source identity):
  owner, chat_id (stable Odysseus chat), actor_id, message_id, part_id, revision

Plus provenance on every part: runtime_generation, event_sequence,
event_workspace, event_project, role/type, timestamps, tombstone state,
owned asset refs. Old engine/host IDs are preserved as aliases rather than
silently manufacturing replacement history IDs.

Delivery is a durable, idempotent outbox keyed on
``(owner, chat_id, actor_id, message_id, part_id, revision, event_kind)``.
Pending delivery never justifies deleting source parts.

Memory extraction/admission stays separately gated (``src.memory_gate``).
Archive plumbing is read-through canonical history and never admits memory.
Incognito/ephemeral history is not persisted here; compare-mode policy is
the caller's responsibility.
"""

from __future__ import annotations

import hashlib
import contextvars
from contextlib import closing
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

_ASSOCIATION_BUSY_BUDGET = contextvars.ContextVar('archive_association_busy_budget', default=False)

# Target paging bounds (from the memory/goals audit). Kept as named constants
# so tests and callers share one authority.
SEARCH_LIMIT_MAX = 50
AROUND_BEFORE_MAX = 50
AROUND_AFTER_MAX = 50
GET_LENGTH_MAX_UTF16 = 8000
GET_TEXT_PAGE_BYTES = 16000
TOOL_ENVELOPE_BYTES = 20 * 1024
INDEX_PREVIEW_FIELD_BYTES = 4000

OUTBOX_PENDING = "pending"
OUTBOX_RUNNING = "running"
OUTBOX_RECONCILING = "reconciling"
OUTBOX_DELIVERED = "delivered"
OUTBOX_FAILED = "failed"
OUTBOX_TERMINAL = frozenset({OUTBOX_DELIVERED, OUTBOX_FAILED})
OUTBOX_ACTIVE = frozenset({OUTBOX_PENDING, OUTBOX_RUNNING, OUTBOX_RECONCILING})

EVENT_PART_UPSERT = "part_upsert"
EVENT_PART_TOMBSTONE = "part_tombstone"
EVENT_ASSET_REF = "asset_ref"
EVENT_COMPACTION_PROJECTION = "compaction_projection"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_archive_deleted_chats (
    owner TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    erased_at INTEGER NOT NULL,
    PRIMARY KEY(owner, chat_id)
);

CREATE TABLE IF NOT EXISTS conversation_archive_owner_aliases (
    owner_alias TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_parts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    actor_id TEXT NOT NULL DEFAULT 'main',
    message_id TEXT NOT NULL,
    part_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    runtime_generation TEXT NOT NULL DEFAULT '0',
    event_sequence INTEGER NOT NULL,
    event_workspace TEXT,
    event_project TEXT,
    role TEXT NOT NULL,
    part_type TEXT NOT NULL,
    content TEXT,
    content_hash TEXT,
    qualified_tool_json TEXT,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    tombstone INTEGER NOT NULL DEFAULT 0,
    tombstone_reason TEXT,
    UNIQUE(owner, chat_id, actor_id, message_id, part_id, revision)
);
CREATE INDEX IF NOT EXISTS ix_conv_parts_scope_seq
    ON conversation_parts(owner, chat_id, actor_id, seq);
CREATE INDEX IF NOT EXISTS ix_conv_parts_message
    ON conversation_parts(owner, chat_id, actor_id, message_id);
CREATE INDEX IF NOT EXISTS ix_conv_parts_search
    ON conversation_parts(owner, chat_id, tombstone, time_created);

CREATE TABLE IF NOT EXISTS conversation_part_aliases (
    alias_kind TEXT NOT NULL,
    alias_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    part_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (alias_kind, alias_id, owner)
);
CREATE INDEX IF NOT EXISTS ix_conv_alias_target
    ON conversation_part_aliases(owner, chat_id, message_id, part_id);

CREATE TABLE IF NOT EXISTS conversation_part_assets (
    owner TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    part_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    content_hash TEXT,
    mime_type TEXT,
    byte_size INTEGER,
    provenance TEXT,
    time_created INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner, chat_id, message_id, part_id, revision, asset_id)
);
CREATE INDEX IF NOT EXISTS ix_conv_assets_asset
    ON conversation_part_assets(owner, asset_id);

CREATE TABLE IF NOT EXISTS conversation_archive_outbox (
    outbox_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    part_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    event_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    delivered_at INTEGER,
    UNIQUE(owner, chat_id, actor_id, message_id, part_id, revision, event_kind)
);
CREATE INDEX IF NOT EXISTS ix_conv_outbox_state
    ON conversation_archive_outbox(state, outbox_seq);

CREATE TABLE IF NOT EXISTS conversation_compaction_projections (
    owner TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    summary_id TEXT NOT NULL,
    projection_revision INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    included_message_ids TEXT NOT NULL,
    retained_message_ids TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    route_id TEXT,
    model_id TEXT,
    projection_state TEXT NOT NULL DEFAULT 'active',
    time_created INTEGER NOT NULL,
    PRIMARY KEY (owner, chat_id, actor_id, summary_id, projection_revision)
);
CREATE INDEX IF NOT EXISTS ix_conv_proj_scope
    ON conversation_compaction_projections(owner, chat_id, actor_id, projection_revision);

CREATE TABLE IF NOT EXISTS conversation_archive_cursor (
    owner TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    consumer TEXT NOT NULL,
    last_outbox_seq INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (owner, chat_id, consumer)
);

CREATE TABLE IF NOT EXISTS conversation_archive_migration_envelopes (
    owner TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    record_kind TEXT NOT NULL CHECK(record_kind IN ('session', 'message')),
    source_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    time_created INTEGER NOT NULL,
    PRIMARY KEY (owner, source_fingerprint, record_kind, source_id)
);
CREATE INDEX IF NOT EXISTS ix_conv_migration_envelopes_scope
    ON conversation_archive_migration_envelopes(owner, source_fingerprint, record_kind);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_hash(content: Any) -> str:
    if content is None:
        payload = ""
    elif isinstance(content, str):
        payload = content
    else:
        payload = _canonical_json(content)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_loads(raw: Optional[str], default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


_DATA_MIME = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")
_DATA_PARAM = re.compile(r";[A-Za-z0-9!#$&^_.+-]+=[A-Za-z0-9!#$&^_.+%/-]+")
_DATA_PREFIX = re.compile(r"data:", re.IGNORECASE)
_BASE64_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
_DATA_PRECEDING_BOUNDARY = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._/+%-")
_BASE64_VALUES = {
    char: value
    for value, char in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")
}
_LARGE_INDEX_FIELD_MARKER = "[large field omitted; use history get part_id]"


def _valid_base64_payload(payload: str) -> bool:
    """Validate standard padded or complete unpadded base64 without decoding it."""
    if not payload:
        return False
    padding = payload.find("=")
    if padding >= 0:
        if (
            len(payload) % 4
            or padding < len(payload) - 2
            or len(payload) - padding > 2
            or any(ch != "=" for ch in payload[padding:])
        ):
            return False
        encoded = payload[:padding]
        tail_modulo = len(encoded) % 4
        if (len(payload) - padding == 1 and tail_modulo != 3) or (
            len(payload) - padding == 2 and tail_modulo != 2
        ):
            return False
    else:
        encoded = payload
        tail_modulo = len(encoded) % 4
        if tail_modulo == 1:
            return False
    if not encoded or any(char not in _BASE64_VALUES for char in encoded):
        return False
    last_value = _BASE64_VALUES[encoded[-1]]
    if tail_modulo == 2 and last_value & 0b1111:
        return False
    if tail_modulo == 3 and last_value & 0b11:
        return False
    return True


def _replace_data_urls(text: str) -> tuple[str, bool]:
    """Replace only complete, single-line media data URLs in one linear scan.

    Ordinary ``metadata:`` / ``form-data:`` prose, malformed tokens and base64
    folded across lines remain source text.  Search invokes this only after its
    pure-SQL projection has bounded the value to four KiB.
    """
    pieces: list[str] = []
    cursor = 0
    omitted = False
    while True:
        prefix = _DATA_PREFIX.search(text, cursor)
        if prefix is None:
            pieces.append(text[cursor:])
            break
        start = prefix.start()
        if start and text[start - 1] in _DATA_PRECEDING_BOUNDARY:
            pieces.append(text[cursor:start + 5])
            cursor = start + 5
            continue
        mime_match = _DATA_MIME.match(text, start + 5)
        if mime_match is None:
            pieces.append(text[cursor:start + 5])
            cursor = start + 5
            continue
        pos = mime_match.end()
        while True:
            parameter = _DATA_PARAM.match(text, pos)
            if parameter is None:
                break
            pos = parameter.end()
        if not text[pos:pos + 8].lower() == ";base64,":
            pieces.append(text[cursor:start + 5])
            cursor = start + 5
            continue
        payload_start = pos + 8
        payload_end = payload_start
        while payload_end < len(text) and text[payload_end] in _BASE64_CHARS:
            payload_end += 1
        payload = text[payload_start:payload_end]
        # Newline-separated base64 must stay readable source rather than being
        # mistaken for a completed URL's first physical line.
        if (
            not _valid_base64_payload(payload)
            or (text[payload_end:payload_end + 1] in {"\r", "\n"}
                and text[payload_end + 1:payload_end + 2] in _BASE64_CHARS)
        ):
            pieces.append(text[cursor:start + 5])
            cursor = start + 5
            continue
        pieces.append(text[cursor:start])
        pieces.append(f"[media {mime_match.group(0)}]")
        cursor = payload_end
        omitted = True
    return "".join(pieces), omitted


def _utf8_prefix(text: str, budget: int) -> tuple[str, bool]:
    """Return complete code points within a UTF-8 byte budget."""
    used = 0
    end = 0
    for index, ch in enumerate(text):
        size = len(ch.encode("utf-8", errors="replace"))
        if used + size > budget:
            return text[:end], True
        used += size
        end = index + 1
    return text, False


def _index_preview(value: Any) -> tuple[str, bool]:
    """Project canonical content into a bounded, media-safe index preview."""
    text = value if isinstance(value, str) else _canonical_json(value)
    text, media_omissions = _replace_data_urls(text)
    visible, clipped = _utf8_prefix(text, INDEX_PREVIEW_FIELD_BYTES)
    if clipped:
        # Reserve the ellipsis bytes inside the advertised 4 KiB maximum.
        visible, _ = _utf8_prefix(visible, INDEX_PREVIEW_FIELD_BYTES - len("…".encode("utf-8")))
        visible += "…"
    return visible, media_omissions or clipped


def _utf16_len(text: str) -> int:
    """UTF-16 code-unit length, matching the target paging semantics."""
    return len(text.encode("utf-16-le")) // 2


def _normalize_get_length(length: Optional[int]) -> int:
    try:
        requested = GET_LENGTH_MAX_UTF16 if length is None else int(length)
    except (TypeError, ValueError):
        requested = GET_LENGTH_MAX_UTF16
    return max(1, min(requested, GET_LENGTH_MAX_UTF16))


def _slice_at_utf16_offset(text: str, offset: int) -> tuple[str, int]:
    """Advance a UTF-16 cursor to the next complete code-point boundary."""
    target = max(0, int(offset))
    units = 0
    for index, ch in enumerate(text):
        next_units = units + (2 if ord(ch) > 0xFFFF else 1)
        if next_units > target:
            if units == target:
                return text[index:], units
            # An arbitrary caller landed inside an astral character.  Advance
            # past it; issued cursors are always exact and never take this path.
            return text[index + 1:], next_units
        if next_units == target:
            return text[index + 1:], next_units
        units = next_units
    return "", units


def _clamp_get_text(text: str, *, length: Optional[int] = None) -> tuple[str, int, bool]:
    """Page a body by UTF-16 units and UTF-8 bytes without splitting code points."""
    if text is None:
        return "", 0, False
    unit_budget = _normalize_get_length(length)
    bytes_used = 0
    units_used = 0
    end = 0
    for index, ch in enumerate(text):
        ch_units = 2 if ord(ch) > 0xFFFF else 1
        ch_bytes = len(ch.encode("utf-8", errors="replace"))
        if units_used + ch_units > unit_budget or bytes_used + ch_bytes > GET_TEXT_PAGE_BYTES:
            # A one-unit request cannot represent an astral character. Return
            # that whole character rather than emitting an empty, non-advancing
            # page or splitting its surrogate pair.
            if end == 0:
                return ch, ch_units, len(text) > 1
            break
        units_used += ch_units
        bytes_used += ch_bytes
        end = index + 1
    visible = text[:end]
    return visible, units_used, end < len(text)


@dataclass(frozen=True)
class SourcePart:
    """Immutable identity + provenance for one archived message part."""

    owner: str
    chat_id: str
    actor_id: str
    message_id: str
    part_id: str
    revision: int = 1
    runtime_generation: str = "0"
    event_sequence: int = 0
    event_workspace: Optional[str] = None
    event_project: Optional[str] = None
    role: str = "user"
    part_type: str = "text"
    content: Any = None
    content_hash: Optional[str] = None
    time_created: int = 0
    time_updated: int = 0
    tombstone: bool = False
    tombstone_reason: Optional[str] = None
    aliases: tuple[tuple[str, str], ...] = ()
    assets: tuple[Mapping[str, Any], ...] = ()

    @property
    def dedupe_key(self) -> tuple[str, str, str, str, str, int]:
        return (
            self.owner,
            self.chat_id,
            self.actor_id,
            self.message_id,
            self.part_id,
            self.revision,
        )


@dataclass(frozen=True)
class TrustedOriginAdmission:
    """Internal host-minted admission evidence; generic source mappings cannot create it."""
    admission_id: str
    owner: str
    chat_id: str
    actor_id: str
    root_operation_id: Optional[str]
    engine_session_id: Optional[str]
    execution_revision: Optional[int]
    inner_turn_id: Optional[str]
    incognito: Optional[bool]
    no_memory: Optional[bool]
    compare_mode: Optional[bool]
    capture_eligible: Optional[bool]
    policy_source: Optional[str]
    policy_value: Optional[str]


@dataclass(frozen=True)
class TrustedOriginPart:
    message_id: str
    part_id: str
    revision: int
    content_hash: str


@dataclass(frozen=True)
class TrustedQueuedPending:
    """Content-free verified queued-input mapping, never a source grant."""
    identity: dict[str, Any]
    proof: dict[str, Any]
    proof_sha256: str
    payload_hash: str
    message_id: str
    part_id: str
    binding: dict[str, Any]
    producer: dict[str, Any]


_QUEUED_RESET_PROOF_SEAL = object()
_QUEUED_PRODUCER_INVENTORY_SEAL = object()


@dataclass(frozen=True)
class QueuedProducerInventory:
    """Bounded original-producer inventory, never retirement authorization."""
    archive: Any
    claim: Any
    provider: Any
    owner: str
    account_id: str
    operation_id: str
    archive_path: str
    expected_json: str
    payload_json: str
    payload_sha256: str
    _seal: object = field(repr=False)

    def assert_current(self):
        if (self._seal is not _QUEUED_PRODUCER_INVENTORY_SEAL or os.path.realpath(self.archive.db_path)!=self.archive_path
            or hashlib.sha256(self.payload_json.encode('utf-8')).hexdigest()!=self.payload_sha256):
            raise ValueError('queued producer inventory unavailable')
        self.claim.assert_current(owner=self.owner,account_id=self.account_id,provider=self.provider,operation_id=self.operation_id)
        if self.archive.queued_reset_inventory(self.account_id,self.operation_id)!=json.loads(self.expected_json):
            raise ValueError('queued producer inventory changed')
        self.claim.assert_current(owner=self.owner,account_id=self.account_id,provider=self.provider,operation_id=self.operation_id)


@dataclass(frozen=True)
class QueuedResetCompletion:
    archive: Any
    claim: Any
    provider: Any
    owner: str
    account_id: str
    operation_id: str
    archive_path: str
    expected_json: str
    _seal: object = field(repr=False)

    def assert_current(self):
        if self._seal is not _QUEUED_RESET_PROOF_SEAL:
            raise ValueError('queued reset proof unavailable')
        self.claim.assert_current(owner=self.owner,account_id=self.account_id,provider=self.provider,operation_id=self.operation_id)
        if os.path.realpath(self.archive.db_path)!=self.archive_path:
            raise ValueError('queued reset archive changed')
        actual=self.archive.queued_reset_inventory(self.account_id,self.operation_id)
        if actual != json.loads(self.expected_json) or self.archive.queued_reset_has_active(self.account_id):
            raise ValueError('queued reset retirement incomplete')
        self.claim.assert_current(owner=self.owner,account_id=self.account_id,provider=self.provider,operation_id=self.operation_id)


@dataclass(frozen=True)
class TrustedMaintenancePending:
    """Host-held, content-free proof for one exact native summary artifact."""
    identity: dict[str, Any]
    proof: dict[str, Any]
    descriptor: dict[str, Any]
    binding: dict[str, Any]


_MAINTENANCE_READY_SEAL = object()


@dataclass(frozen=True)
class TrustedMaintenanceReady:
    seq: int
    payload_sha256: str
    payload_json: str
    _seal: object = field(repr=False)

    def payload(self) -> dict[str, Any]:
        if self._seal is not _MAINTENANCE_READY_SEAL or hashlib.sha256(self.payload_json.encode('utf-8')).hexdigest()!=self.payload_sha256:
            raise ValueError('maintenance reference unavailable')
        value=json.loads(self.payload_json)
        if type(value) is not dict or _canonical_json(value)!=self.payload_json:
            raise ValueError('maintenance reference unavailable')
        return value


@dataclass(frozen=True)
class TrustedOriginCompletion:
    admission_id: str
    owner: str
    chat_id: str
    actor_id: str
    outcome: str
    parts: tuple[TrustedOriginPart, ...]


@dataclass(frozen=True)
class TrustedAssistantComponent:
    """Exact association only; it is not an extraction or capture grant."""
    owner: str
    chat_id: str
    root: TrustedOriginPart
    assistant: TrustedOriginPart
    pair_sha256: str
    workspace_id: Optional[str]


@dataclass(frozen=True)
class TrustedOriginEvidence:
    admission: TrustedOriginAdmission
    completion: TrustedOriginCompletion
    workspace_id: Optional[str]
    project_id: Optional[str]


_ORIGIN_SCHEMA = """
CREATE TABLE origin_schema_marker (version INTEGER NOT NULL CHECK(version=1));
INSERT INTO origin_schema_marker VALUES (1);
CREATE TABLE origin_admission (
  owner TEXT NOT NULL, chat_id TEXT NOT NULL, actor_id TEXT NOT NULL, admission_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL, PRIMARY KEY(owner,chat_id,actor_id,admission_id), UNIQUE(admission_id)
);
CREATE TABLE origin_completion (
  owner TEXT NOT NULL, chat_id TEXT NOT NULL, actor_id TEXT NOT NULL, admission_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL, PRIMARY KEY(owner,chat_id,actor_id,admission_id),
  FOREIGN KEY(owner,chat_id,actor_id,admission_id) REFERENCES origin_admission(owner,chat_id,actor_id,admission_id)
);
CREATE TABLE origin_part_link (
  owner TEXT NOT NULL, chat_id TEXT NOT NULL, actor_id TEXT NOT NULL, message_id TEXT NOT NULL,
  part_id TEXT NOT NULL, revision INTEGER NOT NULL, content_hash TEXT NOT NULL, admission_id TEXT NOT NULL,
  PRIMARY KEY(owner,chat_id,actor_id,message_id,part_id,revision),
  FOREIGN KEY(owner,chat_id,actor_id,admission_id) REFERENCES origin_admission(owner,chat_id,actor_id,admission_id)
);
"""

def _origin_schema_exact(db) -> bool:
    """Compare the complete owned origin object set and attached triggers."""
    query = ("SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND "
             "(name LIKE 'origin_%' OR (type='trigger' AND tbl_name IN "
             "('origin_schema_marker','origin_admission','origin_completion','origin_part_link'))) ORDER BY type,name")
    with sqlite3.connect(':memory:') as template:
        template.executescript(_ORIGIN_SCHEMA)
        return (db.execute(query).fetchall() == template.execute(query).fetchall()
                and db.execute('SELECT version FROM origin_schema_marker').fetchall() == [(1,)])


# Optional, explicitly prepared paired-reader profile; never installed on open.
_ASSISTANT_COMPONENT_INDEXES = (
    "CREATE INDEX ix_conv_assistant_component_owner_seq ON conversation_archive_outbox(owner,outbox_seq) WHERE event_kind='assistant_component.v1' AND state='assistant_component'",
    "CREATE INDEX ix_conv_assistant_component_chat_seq ON conversation_archive_outbox(owner,chat_id,outbox_seq) WHERE event_kind='assistant_component.v1' AND state='assistant_component'",
)
_ASSISTANT_COMPONENT_CONSUMER = 'assistant_interpretation_review.v1'


def prepare_assistant_component_profile_offline(source_path: str, destination_path: str, *, writers_stopped: bool) -> None:
    """Create a distinct, explicitly prepared copy; never modify the source."""
    if writers_stopped is not True:
        raise ValueError('quiesced archive required')
    source=Path(source_path).resolve(strict=True)
    destination=Path(destination_path).resolve(strict=False)
    if source == destination or destination.exists() or not source.is_file() or not source.stat().st_size:
        raise ValueError('new distinct destination required')
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src:
        if src.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('archive integrity unavailable')
        if src.execute('SELECT version FROM origin_schema_marker').fetchall() != [(1,)]:
            raise ValueError('origin profile unavailable')
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            if any(dst.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",(definition.split()[2],)).fetchone() for definition in _ASSISTANT_COMPONENT_INDEXES):
                raise ValueError('paired profile already present or partial')
            for definition in _ASSISTANT_COMPONENT_INDEXES:
                dst.execute(definition)
            dst.commit()


_MAINTENANCE_PENDING_INDEXES = (
    "CREATE INDEX ix_conv_maintenance_pending ON conversation_archive_outbox(owner,chat_id,message_id,json_extract(payload_json,'$.descriptor.summaryMessageID')) WHERE event_kind GLOB 'maintenance_pending:*' AND json_valid(payload_json)",
    "CREATE INDEX ix_conv_maintenance_invalid ON conversation_archive_outbox(owner,chat_id,message_id) WHERE event_kind GLOB 'maintenance_pending:*' AND NOT json_valid(payload_json)",
    "CREATE INDEX ix_conv_maintenance_page ON conversation_archive_outbox(owner,chat_id,message_id,outbox_seq) WHERE event_kind GLOB 'maintenance_pending:*' AND state='maintenance_pending'",
)

_QUEUED_RESET_KINDS = "event_kind IN ('queued_pending.v1','queued_completion.v1','queued_archive_verified.v1')"
_QUEUED_RESET_INDEXES = (
    f"CREATE INDEX ix_conv_queued_reset_active ON conversation_archive_outbox(owner,outbox_seq) WHERE {_QUEUED_RESET_KINDS} AND state!='queued_reset_retired'",
    f"CREATE INDEX ix_conv_queued_reset_operation ON conversation_archive_outbox(owner,json_extract(payload_json,'$.resetOperationID'),outbox_seq) WHERE {_QUEUED_RESET_KINDS} AND state='queued_reset_retired' AND json_valid(payload_json)",
    f"CREATE INDEX ix_conv_queued_reset_invalid ON conversation_archive_outbox(owner,outbox_seq) WHERE {_QUEUED_RESET_KINDS} AND state='queued_reset_retired' AND CASE WHEN json_valid(payload_json) THEN coalesce(json_type(payload_json,'$.resetOperationID')!='text' OR length(json_extract(payload_json,'$.resetOperationID'))=0 OR json_type(payload_json,'$.resetOriginalSHA256')!='text' OR length(json_extract(payload_json,'$.resetOriginalSHA256'))!=64 OR json_extract(payload_json,'$.resetOriginalSHA256') GLOB '*[^0-9a-f]*' OR json_extract(payload_json,'$.version')!=CASE event_kind WHEN 'queued_pending.v1' THEN 'queued.pending.v1' WHEN 'queued_completion.v1' THEN 'queued.completion.v1' ELSE 'queued.archive-verified.v1' END,1) ELSE 1 END",
)
_QUEUED_RESET_INDEXES_V1 = _QUEUED_RESET_INDEXES
_QUEUED_RESET_INDEXES = (*_QUEUED_RESET_INDEXES_V1[:2], _QUEUED_RESET_INDEXES_V1[2].replace(
    "json_extract(payload_json,'$.version')!=CASE event_kind WHEN 'queued_pending.v1' THEN 'queued.pending.v1' WHEN 'queued_completion.v1' THEN 'queued.completion.v1' ELSE 'queued.archive-verified.v1' END",
    "NOT ((event_kind='queued_pending.v1' AND json_extract(payload_json,'$.version') IN ('queued.pending.v1','queued.pending.v2')) OR (event_kind='queued_completion.v1' AND json_extract(payload_json,'$.version')='queued.completion.v1') OR (event_kind='queued_archive_verified.v1' AND json_extract(payload_json,'$.version') IN ('queued.archive-verified.v1','queued.archive-verified.v2')))"))

def _valid_queued_producer(value):
    return (type(value) is dict and set(value)=={'version','holderSHA256','generation','ownerEpoch'} and type(value['version']) is int and value['version']==1
        and type(value['holderSHA256']) is str and re.fullmatch(r'[0-9a-f]{64}',value['holderSHA256']) is not None
        and type(value['generation']) is int and value['generation']>=0 and type(value['ownerEpoch']) is int and value['ownerEpoch']>=0)


def convert_queued_reset_schema_offline(db_path: str, backup_path: str, *, writers_stopped: bool) -> None:
    """Backup first, then install the exact optional index set on a quiesced archive."""
    if writers_stopped is not True or not os.path.isfile(db_path) or not os.path.getsize(db_path):
        raise ValueError('quiesced existing archive required')
    original=Path(db_path).resolve(strict=True); backup=Path(backup_path).resolve(strict=False)
    if original==backup or backup.exists(): raise ValueError('new distinct backup required')
    with sqlite3.connect(original.as_uri()+'?mode=ro',uri=True) as src:
        if src.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('archive integrity unavailable')
        fd=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        with sqlite3.connect(backup) as dst: src.backup(dst)
    with sqlite3.connect(original.as_uri()+'?mode=rw',uri=True,isolation_level=None) as conn:
        try:
            conn.execute('BEGIN EXCLUSIVE')
            profile=ConversationArchive._queued_reset_index_state(conn)
            if profile=='legacy':
                for definition in _QUEUED_RESET_INDEXES_V1: conn.execute('DROP INDEX '+definition.split()[2])
            elif profile!='absent':raise ValueError('queued reset index already present or incomplete')
            for definition in _QUEUED_RESET_INDEXES: conn.execute(definition)
            conn.commit()
        except BaseException:
            conn.rollback();raise


def convert_maintenance_pending_schema_offline(db_path: str) -> None:
    """Install only the pending-reference indexes in a quiesced, backed-up archive."""
    if not os.path.isfile(db_path) or not os.path.getsize(db_path):
        raise ValueError('existing archive required')
    with sqlite3.connect(Path(db_path).absolute().as_uri()+'?mode=rw',uri=True,isolation_level=None) as conn:
        try:
            conn.execute('BEGIN EXCLUSIVE')
            marker=conn.execute('SELECT version FROM origin_schema_marker').fetchall()
            if marker != [(1,)]:
                raise ArchiveUnavailableError(message='origin schema unavailable')
            names=tuple(conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name IN ('ix_conv_maintenance_pending','ix_conv_maintenance_invalid','ix_conv_maintenance_page')"))
            if names:
                raise ArchiveUnavailableError(message='maintenance schema already present or incomplete')
            for statement in _MAINTENANCE_PENDING_INDEXES:
                conn.execute(statement)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise


def convert_origin_schema_offline(db_path: str) -> None:
    """Explicit additive conversion; caller must quiesce and back up the archive."""
    if not os.path.isfile(db_path) or not os.path.getsize(db_path):
        raise ValueError("existing archive required")
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(ConversationArchive._OWNER_TABLES).issubset(names):
            raise ArchiveUnavailableError(message="archive schema requires prior conversion")
        if names.intersection({"origin_schema_marker", "origin_admission", "origin_completion", "origin_part_link"}):
            raise ArchiveUnavailableError(message="origin schema already present or incomplete")
        try:
            conn.executescript("BEGIN EXCLUSIVE;\n" + _ORIGIN_SCHEMA + "\nCOMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise


@dataclass
class ArchiveUnavailableError(Exception):
    """Recoverable shared-service failure — no unscoped/native fallback."""

    message: str = "conversation archive unavailable"
    retry_after_ms: int = 250

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "archive_unavailable",
            "message": self.message,
            "retry_after_ms": self.retry_after_ms,
            "recoverable": True,
        }


@dataclass
class ConversationArchive:
    """SQLite-backed lossless ordered source-part archive + idempotent outbox."""

    db_path: str
    owner_resolver: Optional[Callable[[str], Optional[str]]] = field(default=None, repr=False)
    enable_origin_policy_for_disposable_test: bool = field(default=False, repr=False)
    enable_origin_policy: bool = field(default=False, repr=False)
    _origin_schema_present: bool = field(default=False, init=False, repr=False)
    _origin_profile: object = field(default=None, init=False, repr=False)
    _origin_archive_identity: object = field(default=None, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # A zero-length pre-existing file is not evidence of a new archive.
        # Reserve a genuinely new path exclusively before any schema writes.
        fresh = False
        if not os.path.lexists(self.db_path):
            fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(fd)
            fresh = True
        if not fresh:
            if os.path.islink(self.db_path) or not os.path.isfile(self.db_path) or not os.path.getsize(self.db_path):
                raise ArchiveUnavailableError(message='archive requires explicit offline preparation')
            # Validate existing stores through a read-only handle before any
            # WAL pragma or CREATE can modify historical bytes.
            from urllib.parse import quote
            uri = "file:" + quote(os.path.abspath(self.db_path), safe="/") + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required = set(self._OWNER_TABLES) | {"conversation_archive_owner_aliases", "conversation_archive_deleted_chats"}
                if not required.issubset(tables):
                    raise ArchiveUnavailableError(message="archive schema requires explicit offline conversion")
                if not {"owner_alias", "account_id", "updated_at"}.issubset({row[1] for row in conn.execute("PRAGMA table_info(conversation_archive_owner_aliases)")}):
                    raise ArchiveUnavailableError(message="archive identity schema requires explicit offline conversion")
                if not {"owner", "chat_id", "erased_at"}.issubset({row[1] for row in conn.execute("PRAGMA table_info(conversation_archive_deleted_chats)")}):
                    raise ArchiveUnavailableError(message="archive deletion schema requires explicit offline conversion")
                origin = {"origin_schema_marker", "origin_admission", "origin_completion", "origin_part_link"}
                if tables.intersection(origin):
                    if not origin.issubset(tables) or conn.execute("SELECT version FROM origin_schema_marker").fetchall() != [(1,)]:
                        raise ArchiveUnavailableError(message="unsupported origin schema")
                    expected = {"origin_admission": {"owner", "chat_id", "actor_id", "admission_id", "receipt_json"},
                                "origin_completion": {"owner", "chat_id", "actor_id", "admission_id", "receipt_json"},
                                "origin_part_link": {"owner", "chat_id", "actor_id", "message_id", "part_id", "revision", "content_hash", "admission_id"}}
                    if any({row[1] for row in conn.execute(f"PRAGMA table_info({table})")} != columns for table, columns in expected.items()):
                        raise ArchiveUnavailableError(message="unsupported origin schema")
                    self._origin_schema_present = True
            if (self.enable_origin_policy or self.enable_origin_policy_for_disposable_test) and not self._origin_schema_present:
                raise ArchiveUnavailableError(message="origin policy schema requires explicit offline conversion")
            return
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            for definition in _QUEUED_RESET_INDEXES: conn.execute(definition)
            # A truly new archive contains the inert origin schema even if the
            # singleton opens before the guarded profile is bound. Schema alone
            # does not enable origin operations or grant capture authority.
            conn.executescript(_ORIGIN_SCHEMA)
            self._origin_schema_present = True

    def _origin_available(self):
        if not self._origin_schema_present:
            return False
        if self.enable_origin_policy_for_disposable_test:
            return True
        profile = self._origin_profile
        if not self.enable_origin_policy or profile is None or _ARCHIVE is not self:
            return False
        try:
            profile.assert_current()
            profile.provider._guarded_preparation.assert_valid()
            from urllib.parse import quote
            import stat
            before = os.stat(self.db_path, follow_symlinks=False)
            if (not stat.S_ISREG(before.st_mode) or (before.st_dev,before.st_ino) != self._origin_archive_identity[:2] or before.st_nlink != 1):
                return False
            with sqlite3.connect('file:' + quote(os.path.abspath(self.db_path), safe='/') + '?mode=ro', uri=True, timeout=0.05) as db:
                cookie = db.execute('PRAGMA schema_version').fetchone()[0]
                marker = db.execute('SELECT version FROM origin_schema_marker').fetchall()
            after = os.stat(self.db_path, follow_symlinks=False)
            return ((after.st_dev,after.st_ino)==self._origin_archive_identity[:2]
                    and cookie==self._origin_archive_identity[2] and marker==[(1,)])
        except Exception:
            return False

    @staticmethod
    def _queued_reset_index_state(conn):
        def shape(value):
            return ''.join(part if index%2 else ''.join(part.split()).lower() for index,part in enumerate(re.split(r"('(?:''|[^'])*')",value or '')))
        actual=[conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(expected.split()[2],)).fetchone() for expected in _QUEUED_RESET_INDEXES]
        if not any(actual): return 'absent'
        if all(row and shape(row[0])==shape(expected) for row,expected in zip(actual,_QUEUED_RESET_INDEXES)):return 'ready'
        if all(row and shape(row[0])==shape(expected) for row,expected in zip(actual,_QUEUED_RESET_INDEXES_V1)):return 'legacy'
        raise ArchiveUnavailableError(message='queued reset indexes unavailable')

    @classmethod
    def _require_queued_reset_indexes(cls, conn):
        if cls._queued_reset_index_state(conn) not in ('ready','legacy'): raise ArchiveUnavailableError(message='queued reset indexes unavailable')

    @staticmethod
    def _queued_reset_legacy_empty(conn, account_id):
        # The old owner-leading unique index is a read-only fallback. A VM
        # budget bounds large archives; an interrupted scan proves nothing.
        steps=[0]
        def budget():
            steps[0]+=1
            return 1 if steps[0]>100 else 0
        conn.set_progress_handler(budget,1000)
        try:
            invalid=_QUEUED_RESET_INDEXES[2].split(' WHERE ',1)[1]
            row=conn.execute(f"SELECT 1 FROM conversation_archive_outbox WHERE owner=? AND (({_QUEUED_RESET_KINDS} AND state!='queued_reset_retired') OR ({invalid})) LIMIT 1",(account_id,)).fetchone()
            if row: raise ArchiveUnavailableError(message='queued reset indexes required')
        except sqlite3.OperationalError as exc:
            raise ArchiveUnavailableError(message='queued reset legacy scan unavailable') from exc
        finally:
            conn.set_progress_handler(None,0)

    @staticmethod
    def _queued_reset_validate_row(row, account_id, operation_id):
        if type(row['payload_json']) is not str or len(row['payload_json'].encode('utf-8'))>16384:raise ValueError('queued reset row unavailable')
        try: payload=json.loads(row['payload_json'])
        except (ValueError,TypeError): raise ValueError('queued reset row unavailable')
        if type(payload) is not dict or row['owner']!=account_id or row['actor_id']!='main': raise ValueError('queued reset row unavailable')
        retired=row['state']=='queued_reset_retired'
        expected={'queued_pending.v1':'queued_pending','queued_completion.v1':'queued_completed','queued_archive_verified.v1':'queued_verified'}
        if retired:
            reset_op=payload.pop('resetOperationID',None); original=payload.pop('resetOriginalSHA256',None)
            if (reset_op!=operation_id or type(original) is not str or re.fullmatch(r'[0-9a-f]{64}',original) is None
                or hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()!=original):
                raise ValueError('queued reset row unavailable')
        elif row['state']!=expected.get(row['event_kind']):
            raise ValueError('queued reset row unavailable')
        else:
            if _canonical_json(payload)!=row['payload_json']: raise ValueError('queued reset row unavailable')
            original=hashlib.sha256(row['payload_json'].encode('utf-8')).hexdigest()
        if row['event_kind']=='queued_pending.v1':
            proof=payload.get('proof')
            if payload.get('version')=='queued.pending.v2' and set(payload)!={'version','identity','proof','proofSHA256','payloadHash','messageID','partID','binding','producer'}:raise ValueError('queued reset row unavailable')
            if (payload.get('version') not in ('queued.pending.v1','queued.pending.v2') or (payload.get('version')=='queued.pending.v2' and not _valid_queued_producer(payload.get('producer'))) or type(proof) is not dict or proof.get('accountID')!=account_id
                or proof.get('stableChatID')!=row['chat_id'] or payload.get('messageID')!=row['message_id']
                or payload.get('partID')!=row['part_id']): raise ValueError('queued reset row unavailable')
        elif row['event_kind']=='queued_completion.v1':
            receipt=payload.get('receipt')
            if (payload.get('version')!='queued.completion.v1' or type(receipt) is not dict
                or receipt.get('accountID')!=account_id or receipt.get('chatID')!=row['chat_id']): raise ValueError('queued reset row unavailable')
        else:
            source=payload.get('source')
            if payload.get('version')=='queued.archive-verified.v2' and set(payload)!={'version','source','producer'}:raise ValueError('queued reset row unavailable')
            if (payload.get('version') not in ('queued.archive-verified.v1','queued.archive-verified.v2') or (payload.get('version')=='queued.archive-verified.v2' and not _valid_queued_producer(payload.get('producer'))) or type(source) is not dict
                or source.get('account_id')!=account_id or source.get('chat_id')!=row['chat_id']
                or source.get('message_id')!=row['message_id'] or source.get('part_id')!=row['part_id']): raise ValueError('queued reset row unavailable')
        return original

    def _queued_reset_inventory_conn(self, conn, account_id, operation_id):
        import heapq
        if self._queued_reset_index_state(conn)=='absent':
            self._queued_reset_legacy_empty(conn,account_id)
            stat=os.stat(self.db_path)
            return {'version':2,'count':0,'fingerprint':hashlib.sha256(b'').hexdigest(),'device':stat.st_dev,'inode':stat.st_ino}
        invalid=(_QUEUED_RESET_INDEXES_V1 if self._queued_reset_index_state(conn)=='legacy' else _QUEUED_RESET_INDEXES)[2].split(' WHERE ',1)[1]
        if conn.execute(f"SELECT 1 FROM conversation_archive_outbox INDEXED BY ix_conv_queued_reset_invalid WHERE owner=? AND {invalid} LIMIT 1",(account_id,)).fetchone():
            raise ValueError('invalid retired queued source')
        fields="outbox_seq,owner,chat_id,actor_id,message_id,part_id,revision,event_kind,CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END AS payload_json,state"
        active=conn.execute(f"SELECT {fields} FROM conversation_archive_outbox INDEXED BY ix_conv_queued_reset_active WHERE owner=? AND {_QUEUED_RESET_KINDS} AND state!='queued_reset_retired' ORDER BY outbox_seq",(account_id,))
        retired=conn.execute(f"SELECT {fields} FROM conversation_archive_outbox INDEXED BY ix_conv_queued_reset_operation WHERE owner=? AND {_QUEUED_RESET_KINDS} AND state='queued_reset_retired' AND json_valid(payload_json) AND json_extract(payload_json,'$.resetOperationID')=? ORDER BY outbox_seq",(account_id,operation_id))
        digest=hashlib.sha256(); count=0
        for row in heapq.merge(active,retired,key=lambda item:item['outbox_seq']):
            original=self._queued_reset_validate_row(row,account_id,operation_id)
            value=_canonical_json([row['outbox_seq'],row['chat_id'],row['actor_id'],row['message_id'],row['part_id'],row['revision'],row['event_kind'],original]).encode('utf-8')
            digest.update(len(value).to_bytes(8,'big'));digest.update(value);count+=1
        stat=os.stat(self.db_path)
        return {'version':1,'count':count,'fingerprint':digest.hexdigest(),'device':stat.st_dev,'inode':stat.st_ino}

    def queued_reset_inventory(self, account_id, operation_id):
        if type(account_id) is not str or not account_id or type(operation_id) is not str or not operation_id:
            raise ValueError('queued reset identity unavailable')
        before=os.stat(self.db_path)
        with self._lock, sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=ro',uri=True) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('BEGIN')
            result=self._queued_reset_inventory_conn(conn,account_id,operation_id)
            after=os.stat(self.db_path)
            if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino) or (result['device'],result['inode'])!=(after.st_dev,after.st_ino):
                raise ValueError('queued reset archive changed')
            return result

    def queued_reset_has_active(self, account_id):
        with self._lock, sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=ro',uri=True) as conn:
            if self._queued_reset_index_state(conn)=='absent':
                self._queued_reset_legacy_empty(conn,account_id)
                return False
            return conn.execute(f"SELECT 1 FROM conversation_archive_outbox INDEXED BY ix_conv_queued_reset_active WHERE owner=? AND {_QUEUED_RESET_KINDS} AND state!='queued_reset_retired' LIMIT 1",(account_id,)).fetchone() is not None

    def queued_reset_producer_page(self, *, claim, provider, owner, account_id, operation_id, expected, cursor=None, limit=64):
        """Read content-free original producers under the exact preview and live claim.

        The cursor is a selector, not authority; every page rechecks the full
        streaming inventory and the claim. This does not retire or admit a source.
        """
        from services.memory.nuke_coordinator import MemoryResetClaim
        if type(claim) is not MemoryResetClaim:
            raise ValueError('queued producer inventory unavailable')
        def check():claim.assert_current(owner=owner,account_id=account_id,provider=provider,operation_id=operation_id)
        return self._queued_reset_producer_page_read(account_id=account_id,operation_id=operation_id,
            expected=expected,cursor=cursor,limit=limit,check=check)

    def _queued_reset_producer_page_read(self, *, account_id, operation_id, expected, cursor, limit, check):
        """Bounded read core; caller supplies and retains its own authority."""
        from src.openclank.queued_source import _valid,QueuedSourceRef
        from dataclasses import fields as dataclass_fields
        import heapq
        if (not callable(check) or type(expected) is not dict or set(expected)!={'version','count','fingerprint','device','inode'}
            or type(expected.get('version')) is not int or expected['version']!=1 or type(expected.get('count')) is not int or expected['count']<0
            or type(expected.get('device')) is not int or type(expected.get('inode')) is not int
            or type(expected.get('fingerprint')) is not str or re.fullmatch(r'[0-9a-f]{64}',expected['fingerprint']) is None
            or type(limit) is not int or not 1<=limit<=128):raise ValueError('queued producer inventory unavailable')
        binding=hashlib.sha256(_canonical_json(['queued.producer-page.v1',account_id,operation_id,expected]).encode()).hexdigest()
        if cursor is None:after=0
        elif (type(cursor) is dict and set(cursor)=={'version','binding','after'} and type(cursor['version']) is int and cursor['version']==1
              and cursor['binding']==binding and type(cursor['after']) is int and cursor['after']>0):after=cursor['after']
        else:raise ValueError('queued producer cursor unavailable')
        check()
        if not self._lock.acquire(blocking=False):raise ArchiveUnavailableError(message='archive busy')
        try:
            with sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=ro',uri=True,timeout=0) as conn:
                conn.row_factory=sqlite3.Row;conn.execute('BEGIN')
                steps=[0]
                def budget():
                    steps[0]+=1
                    return int(steps[0]>10000)
                conn.set_progress_handler(budget,1000)
                if self._queued_reset_index_state(conn)!='ready' or self._queued_reset_inventory_conn(conn,account_id,operation_id)!=expected:raise ValueError('queued producer inventory changed')
                fields="outbox_seq,owner,chat_id,actor_id,message_id,part_id,revision,event_kind,CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END AS payload_json,state"
                active=conn.execute(f"SELECT {fields} FROM conversation_archive_outbox INDEXED BY ix_conv_queued_reset_active WHERE owner=? AND {_QUEUED_RESET_KINDS} AND state!='queued_reset_retired' AND outbox_seq>? ORDER BY outbox_seq LIMIT ?",(account_id,after,limit+1))
                retired=conn.execute(f"SELECT {fields} FROM conversation_archive_outbox INDEXED BY ix_conv_queued_reset_operation WHERE owner=? AND {_QUEUED_RESET_KINDS} AND state='queued_reset_retired' AND json_valid(payload_json) AND json_extract(payload_json,'$.resetOperationID')=? AND outbox_seq>? ORDER BY outbox_seq LIMIT ?",(account_id,operation_id,after,limit+1))
                rows=[]
                for row in heapq.merge(active,retired,key=lambda value:value['outbox_seq']):
                    rows.append(row)
                    if len(rows)>limit:break
                def original(row):
                    self._queued_reset_validate_row(row,account_id,operation_id)
                    value=json.loads(row['payload_json'])
                    if row['state']=='queued_reset_retired':value.pop('resetOperationID');value.pop('resetOriginalSHA256')
                    return value
                def lookup(row,kind,revision):
                    found=conn.execute(f"SELECT {fields} FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id='main' AND message_id=? AND part_id=? AND revision=? AND event_kind=?",(account_id,row['chat_id'],row['message_id'],row['part_id'],revision,kind)).fetchone()
                    if found is None:return None
                    value=original(found)
                    if found['state']=='queued_reset_retired' and json.loads(found['payload_json']).get('resetOperationID')!=operation_id:raise ValueError('queued producer association unavailable')
                    return value
                items=[]
                for row in rows[:limit]:
                    value=original(row);kind=row['event_kind']
                    staged=value if kind=='queued_pending.v1' else lookup(row,'queued_pending.v1',1)
                    if (type(staged) is not dict or set(staged)!={'version','identity','proof','proofSHA256','payloadHash','messageID','partID','binding','producer'}
                        or staged.get('version')!='queued.pending.v2' or not _valid_queued_producer(staged.get('producer')) or not _valid(staged.get('proof'))):raise ValueError('queued producer unknown')
                    proof=staged['proof'];identity=staged['identity'];producer=staged['producer']
                    mapping=staged.get('binding')
                    if (type(identity) is not dict or set(identity)!={'sessionID','rootOperationID','actorID','executionRevision','innerTurnID'}
                        or type(identity.get('executionRevision')) is not int or identity['executionRevision']<0
                        or any(type(identity.get(k)) is not str or not identity[k] or len(identity[k])>512 for k in ('sessionID','rootOperationID','actorID','innerTurnID'))
                        or type(mapping) is not dict or set(mapping)!={'engineSessionID','stableChatID','owner','canonicalCwd','authorityWorkspaceID','memoryWorkspaceID','copalWorkspace','mappingRevision'}
                        or type(mapping.get('mappingRevision')) is not int or any(type(mapping.get(k)) is not str or not mapping[k] or len(mapping[k])>4096 for k in ('engineSessionID','stableChatID','owner','canonicalCwd','authorityWorkspaceID','memoryWorkspaceID','copalWorkspace'))
                        or mapping['engineSessionID']!=proof['sessionID'] or mapping['stableChatID']!=row['chat_id'] or mapping['owner']!=proof['owner'] or mapping['authorityWorkspaceID']!=proof['workspaceID'] or mapping['mappingRevision']!=proof['mappingRevision']
                        or any(type(staged.get(k)) is not str or re.fullmatch(r'[0-9a-f]{64}',staged[k]) is None for k in ('proofSHA256','payloadHash'))
                        or proof['accountID']!=account_id or proof['stableChatID']!=row['chat_id'] or staged['messageID']!=row['message_id'] or staged['partID']!=row['part_id']
                        or identity.get('sessionID')!=proof['sessionID'] or identity.get('actorID')!='main'):raise ValueError('queued producer association unavailable')
                    done=value if kind=='queued_completion.v1' else lookup(row,'queued_completion.v1',1)
                    if done is not None:
                        receipt=done.get('receipt') if type(done) is dict else None
                        if (type(done) is not dict or set(done)!={'version','identity','receipt'} or done['version']!='queued.completion.v1' or done['identity']!=identity
                            or type(receipt) is not dict or receipt.get('outcome')!='completed' or receipt.get('accountID')!=account_id or receipt.get('chatID')!=row['chat_id']
                            or receipt.get('owner')!=proof['owner'] or receipt.get('rootSessionID')!=proof['sessionID'] or receipt.get('authorityWorkspaceID')!=proof['workspaceID']
                            or receipt.get('mappingRevision')!=proof['mappingRevision'] or receipt.get('workspaceRevision')!=proof['workspaceRevision']
                            or type(receipt.get('executionRevision')) is not int or any(receipt.get(k)!=identity.get(k) for k in identity) or type(receipt.get('holderID')) is not str
                            or hashlib.sha256(receipt['holderID'].encode()).hexdigest()!=producer['holderSHA256']):raise ValueError('queued completion association unavailable')
                    if kind=='queued_archive_verified.v1':
                        source=value.get('source') if type(value) is dict else None
                        if (done is None or set(value)!={'version','source','producer'} or value.get('version')!='queued.archive-verified.v2' or value.get('producer')!=producer or type(source) is not dict or set(source)!={field.name for field in dataclass_fields(QueuedSourceRef)}
                            or type(source.get('version')) is not int or source['version']!=1 or source.get('kind')!='queued_user_text'
                            or source.get('account_id')!=account_id or source.get('chat_id')!=row['chat_id'] or source.get('message_id')!=row['message_id'] or source.get('part_id')!=row['part_id']
                            or type(source.get('archive_revision')) is not int or source['archive_revision']!=row['revision'] or source.get('native_session_id')!=proof['sessionID'] or source.get('request_id')!=proof['requestID']
                            or source.get('workspace_id')!=proof['workspaceID'] or source.get('root_operation_id')!=identity['rootOperationID'] or type(source.get('execution_revision')) is not int or source['execution_revision']!=identity['executionRevision'] or source.get('inner_turn_id')!=identity['innerTurnID']
                            or type(source.get('archive_content_hash')) is not str or re.fullmatch(r'[0-9a-f]{64}',source['archive_content_hash']) is None
                            or source.get('ingress_sha256')!=staged['proofSHA256'] or source.get('text_sha256')!=proof['bodySHA256']):raise ValueError('queued verified association unavailable')
                    source_hash=hashlib.sha256(_canonical_json(['queued.producer-source.v1',account_id,row['chat_id'],proof['sessionID'],proof['requestID'],row['message_id'],row['part_id']]).encode()).hexdigest()
                    event_hash=hashlib.sha256(_canonical_json(['queued.producer-event.v1',row['outbox_seq'],row['event_kind'],row['revision'],self._queued_reset_validate_row(row,account_id,operation_id)]).encode()).hexdigest()
                    items.append({'eventKind':kind,'sourceIdentitySHA256':source_hash,'eventIdentitySHA256':event_hash,'producer':dict(producer),'association':'verified' if kind=='queued_archive_verified.v1' else 'completed' if done is not None else 'pending'})
                if self._queued_reset_inventory_conn(conn,account_id,operation_id)!=expected:raise ValueError('queued producer inventory changed')
                check();conn.set_progress_handler(None,0)
                next_cursor={'version':1,'binding':binding,'after':rows[limit-1]['outbox_seq']} if len(rows)>limit else None
        finally:self._lock.release()
        check()
        if self.queued_reset_inventory(account_id,operation_id)!=expected:raise ValueError('queued producer inventory changed')
        check()
        return {'version':1,'items':items,'next':next_cursor}

    def queued_reset_producer_inventory(self, *, claim, provider, owner, account_id, operation_id, expected):
        """Exhaust the bounded producer pages; do not infer runtime retirement."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        if type(claim) is not MemoryResetClaim: raise ValueError('queued producer inventory unavailable')
        def check():claim.assert_current(owner=owner,account_id=account_id,provider=provider,operation_id=operation_id)
        payload=self._queued_reset_producer_inventory_read(account_id=account_id,operation_id=operation_id,expected=expected,check=check)
        inventory=QueuedProducerInventory(self,claim,provider,owner,account_id,operation_id,os.path.realpath(self.db_path),
            _canonical_json(expected),payload,hashlib.sha256(payload.encode('utf-8')).hexdigest(),_QUEUED_PRODUCER_INVENTORY_SEAL)
        inventory.assert_current()
        return inventory

    def _queued_reset_producer_inventory_read(self, *, account_id, operation_id, expected, check):
        """Return a transient content-free inventory; never a claim or grant."""
        if (type(expected) is not dict or type(expected.get('version')) is not int or expected['version'] not in (1,2)
            or type(expected.get('count')) is not int or not 0<=expected['count']<=2048
            or (expected['version']==2 and expected['count']!=0)):
            raise ValueError('queued producer inventory unavailable')
        expected=json.loads(_canonical_json(expected))
        if expected['version']==2:
            check()
            if self.queued_reset_inventory(account_id,operation_id)!=expected: raise ValueError('queued producer inventory changed')
            check()
            return _canonical_json({'version':1,'eventCount':0,'producers':[],'sources':[]})
        cursor=None;last_after=0;count=0;byte_count=0;sources={};producers={};events=set()
        while True:
            page=self._queued_reset_producer_page_read(account_id=account_id,operation_id=operation_id,
                expected=expected,cursor=cursor,limit=64,check=check)
            if type(page) is not dict or set(page)!={'version','items','next'} or page['version']!=1 or type(page['items']) is not list:
                raise ValueError('queued producer inventory unavailable')
            for item in page['items']:
                if (type(item) is not dict or set(item)!={'eventKind','sourceIdentitySHA256','eventIdentitySHA256','producer','association'}
                    or item['eventKind'] not in ('queued_pending.v1','queued_completion.v1','queued_archive_verified.v1')
                    or item['association'] not in ('pending','completed','verified') or not _valid_queued_producer(item['producer'])
                    or any(type(item[k]) is not str or re.fullmatch(r'[0-9a-f]{64}',item[k]) is None for k in ('sourceIdentitySHA256','eventIdentitySHA256'))):
                    raise ValueError('queued producer inventory unavailable')
                count+=1;byte_count+=len(_canonical_json(item).encode('utf-8'))
                if count>2048 or byte_count>2*1024*1024 or item['eventIdentitySHA256'] in events:raise ValueError('queued producer inventory too large or ambiguous')
                events.add(item['eventIdentitySHA256']);producer_key=_canonical_json(item['producer'])
                producers[producer_key]=item['producer']
                group=sources.setdefault(item['sourceIdentitySHA256'],{'producer':item['producer'],'events':{},'pendingAssociation':None})
                if group['producer']!=item['producer']:raise ValueError('queued producer association conflict')
                if item['eventKind']=='queued_archive_verified.v1':group['events'].setdefault(item['eventKind'],[]).append(item['eventIdentitySHA256'])
                elif item['eventKind'] in group['events']:raise ValueError('queued producer association conflict')
                else:group['events'][item['eventKind']]=item['eventIdentitySHA256']
                if item['eventKind']=='queued_pending.v1':group['pendingAssociation']=item['association']
                elif item['association']!=('verified' if item['eventKind']=='queued_archive_verified.v1' else 'completed'):
                    raise ValueError('queued producer association unavailable')
                if len(sources)>1024 or len(producers)>256:raise ValueError('queued producer inventory too large')
            next_cursor=page['next']
            if next_cursor is None:break
            if (not page['items'] or type(next_cursor) is not dict or type(next_cursor.get('after')) is not int
                or next_cursor['after']<=last_after):raise ValueError('queued producer cursor unavailable')
            last_after=next_cursor['after'];cursor=next_cursor
        if count!=expected['count']:raise ValueError('queued producer inventory incomplete')
        result_sources=[]
        for source_id,group in sorted(sources.items()):
            kinds=group['events'];completed='queued_completion.v1' in kinds;verified='queued_archive_verified.v1' in kinds
            if ('queued_pending.v1' not in kinds or (verified and not completed)
                or group['pendingAssociation']!=('completed' if completed else 'pending')):
                raise ValueError('queued producer association incomplete')
            if verified:kinds['queued_archive_verified.v1'].sort()
            result_sources.append({'sourceIdentitySHA256':source_id,'producer':group['producer'],
                'events':dict(sorted(kinds.items())),'association':'verified' if verified else 'completed' if completed else 'pending'})
        payload=_canonical_json({'version':1,'eventCount':count,'producers':[producers[key] for key in sorted(producers)],'sources':result_sources})
        if len(payload.encode('utf-8'))>2*1024*1024:raise ValueError('queued producer inventory too large')
        check()
        return payload

    def retire_queued_reset_page(self, *, claim, provider, owner, account_id, operation_id, expected, after=0, limit=128):
        from services.memory.nuke_coordinator import MemoryResetClaim
        if (type(claim) is not MemoryResetClaim or type(expected) is not dict or set(expected)!={'version','count','fingerprint','device','inode'}
            or type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=128):
            raise ValueError('queued reset claim unavailable')
        def check(): claim.assert_current(owner=owner,account_id=account_id,provider=provider,operation_id=operation_id)
        check()
        before=os.stat(self.db_path)
        if (before.st_dev,before.st_ino)!=(expected['device'],expected['inode']): raise ValueError('queued reset archive changed')
        with self._lock, sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=rw',uri=True,isolation_level=None) as conn:
            conn.row_factory=sqlite3.Row
            try:
                conn.execute('BEGIN IMMEDIATE');self._require_queued_reset_indexes(conn)
                rows=conn.execute(f"SELECT outbox_seq,owner,chat_id,actor_id,message_id,part_id,revision,event_kind,payload_json,state FROM conversation_archive_outbox INDEXED BY ix_conv_queued_reset_active WHERE owner=? AND {_QUEUED_RESET_KINDS} AND state!='queued_reset_retired' AND outbox_seq>? ORDER BY outbox_seq LIMIT ?",(account_id,after,limit+1)).fetchall()
                for row in rows[:limit]:
                    original=self._queued_reset_validate_row(row,account_id,operation_id)
                    terminal_payload=json.loads(row['payload_json']);terminal_payload['resetOperationID']=operation_id;terminal_payload['resetOriginalSHA256']=original
                    terminal=_canonical_json(terminal_payload)
                    changed=conn.execute("UPDATE conversation_archive_outbox SET state='queued_reset_retired',payload_json=?,updated_at=? WHERE outbox_seq=? AND owner=? AND state=? AND payload_json=?",(terminal,_now_ms(),row['outbox_seq'],account_id,row['state'],row['payload_json']))
                    if changed.rowcount!=1: raise ValueError('queued reset row changed')
                check();after_stat=os.stat(self.db_path)
                if (after_stat.st_dev,after_stat.st_ino)!=(expected['device'],expected['inode']): raise ValueError('queued reset archive changed')
                conn.commit()
                return {'next_after':rows[limit-1]['outbox_seq'] if len(rows)>limit else None,'count':min(len(rows),limit)}
            except BaseException:
                conn.rollback();raise

    def queued_reset_completion(self, *, claim, provider, owner, account_id, operation_id, expected):
        from services.memory.nuke_coordinator import MemoryResetClaim
        if type(claim) is not MemoryResetClaim or type(expected) is not dict: raise ValueError('queued reset claim unavailable')
        if set(expected)!={'version','count','fingerprint','device','inode'}: raise ValueError('queued reset inventory unavailable')
        proof=QueuedResetCompletion(self,claim,provider,owner,account_id,operation_id,os.path.realpath(self.db_path),_canonical_json(expected),_QUEUED_RESET_PROOF_SEAL)
        proof.assert_current();return proof

    def record_trusted_queued_pending(self, pending: TrustedQueuedPending, *, current: Callable[[], bool]) -> dict[str, Any]:
        """Stage a verified exact queued reference; never store its body or MAC."""
        from src.openclank.queued_source import _valid
        if type(pending) is not TrustedQueuedPending or not callable(current) or not _valid(pending.proof) or not _valid_queued_producer(pending.producer):
            raise ValueError('queued source unavailable')
        identity,proof,binding=pending.identity,pending.proof,pending.binding
        keys={'sessionID','rootOperationID','actorID','executionRevision','innerTurnID'}
        binding_keys={'engineSessionID','stableChatID','owner','canonicalCwd','authorityWorkspaceID','memoryWorkspaceID','copalWorkspace','mappingRevision'}
        if (type(identity) is not dict or set(identity)!=keys or type(binding) is not dict or set(binding)!=binding_keys
            or identity.get('actorID')!='main' or identity.get('sessionID')!=proof['sessionID']
            or binding.get('engineSessionID')!=proof['sessionID'] or binding.get('stableChatID')!=proof['stableChatID']
            or binding.get('owner')!=proof['owner'] or binding.get('authorityWorkspaceID')!=proof['workspaceID']
            or binding.get('mappingRevision')!=proof['mappingRevision'] or proof['captureEligible'] is not True
            or any(proof['choices'][k] is not False for k in ('incognito','noMemory','compareMode'))
            or type(identity.get('executionRevision')) is not int or identity['executionRevision']<0
            or any(type(identity.get(k)) is not str or not identity[k] or len(identity[k])>512 for k in keys-{'executionRevision'})
            or any(type(v) is not str or not v or v!=v.strip() or len(v)>256 for v in (pending.message_id,pending.part_id))
            or any(type(v) is not str or re.fullmatch(r'[0-9a-f]{64}',v) is None for v in (pending.proof_sha256,pending.payload_hash))):
            raise ValueError('queued source unavailable')
        payload={'version':'queued.pending.v2','identity':identity,'proof':proof,'proofSHA256':pending.proof_sha256,
                 'payloadHash':pending.payload_hash,'messageID':pending.message_id,'partID':pending.part_id,'binding':binding,'producer':pending.producer}
        encoded=_canonical_json(payload)
        if len(encoded.encode('utf-8'))>16384: raise ValueError('queued source unavailable')
        if not self._lock.acquire(blocking=False): raise ArchiveUnavailableError(message='archive busy')
        try:
            with sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=rw',uri=True,timeout=0,isolation_level=None) as conn:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    if self._queued_reset_index_state(conn)!='ready':raise ArchiveUnavailableError(message='queued producer profile unavailable')
                    self._reject_erased_chat(conn,proof['accountID'],proof['stableChatID'])
                    key=(proof['accountID'],proof['stableChatID'],'main',pending.message_id,pending.part_id,1,'queued_pending.v1')
                    row=conn.execute('SELECT payload_json,state FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=? AND event_kind=?',key).fetchone()
                    if row and (row[0] != encoded or row[1] != 'queued_pending'): raise ValueError('queued source conflict')
                    if current() is not True: raise ValueError('queued source authority changed')
                    if not row:
                        now=_now_ms()
                        conn.execute("INSERT INTO conversation_archive_outbox(owner,chat_id,actor_id,message_id,part_id,revision,event_kind,payload_json,state,attempts,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'queued_pending',0,?,?)",(*key,encoded,now,now))
                    conn.commit();return {'state':'pending','replayed':bool(row)}
                except BaseException:
                    conn.rollback();raise
        finally:
            self._lock.release()

    def complete_trusted_queued_pending(self, *, pending: TrustedQueuedPending, receipt: dict[str, Any], current: Callable[[], bool]) -> bool:
        """Bind genuine completed C2 evidence to an already-staged candidate."""
        if type(pending) is not TrustedQueuedPending or type(receipt) is not dict or not callable(current):
            raise ValueError('queued completion unavailable')
        identity=pending.identity; proof=pending.proof
        if (receipt.get('outcome')!='completed' or receipt.get('accountID')!=proof['accountID']
            or receipt.get('owner')!=proof['owner'] or receipt.get('chatID')!=proof['stableChatID']
            or receipt.get('rootSessionID')!=proof['sessionID'] or receipt.get('authorityWorkspaceID')!=proof['workspaceID']
            or receipt.get('mappingRevision')!=proof['mappingRevision'] or receipt.get('workspaceRevision')!=proof['workspaceRevision']
            or any(receipt.get(k)!=identity.get(k) for k in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID'))):
            raise ValueError('queued completion unavailable')
        encoded=_canonical_json({'version':'queued.completion.v1','identity':identity,'receipt':receipt})
        if len(encoded.encode('utf-8'))>16384: raise ValueError('queued completion unavailable')
        if not self._lock.acquire(blocking=False): raise ArchiveUnavailableError(message='archive busy')
        try:
            with sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=rw',uri=True,timeout=0,isolation_level=None) as conn:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    self._reject_erased_chat(conn,proof['accountID'],proof['stableChatID'])
                    base=(proof['accountID'],proof['stableChatID'],'main',pending.message_id,pending.part_id,1)
                    row=conn.execute("SELECT payload_json,state FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=? AND event_kind='queued_pending.v1'",base).fetchone()
                    if not row or row[1]!='queued_pending': raise ValueError('queued pending unavailable')
                    saved=json.loads(row[0])
                    if (type(saved) is not dict or saved.get('identity')!=identity or saved.get('proof')!=proof or
                        saved.get('proofSHA256')!=pending.proof_sha256 or saved.get('payloadHash')!=pending.payload_hash or
                        saved.get('messageID')!=pending.message_id or saved.get('partID')!=pending.part_id or saved.get('binding')!=pending.binding or saved.get('version')!='queued.pending.v2' or saved.get('producer')!=pending.producer or not _valid_queued_producer(pending.producer)):
                        raise ValueError('queued pending changed')
                    existing=conn.execute("SELECT payload_json,state FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=? AND event_kind='queued_completion.v1'",base).fetchone()
                    if existing and (existing[0]!=encoded or existing[1]!='queued_completed'): raise ValueError('queued completion conflict')
                    if current() is not True: raise ValueError('queued completion authority changed')
                    if not existing:
                        now=_now_ms()
                        conn.execute("INSERT INTO conversation_archive_outbox(owner,chat_id,actor_id,message_id,part_id,revision,event_kind,payload_json,state,attempts,created_at,updated_at) VALUES (?,?,?,?,?,?,'queued_completion.v1',?,'queued_completed',0,?,?)",(*base,encoded,now,now))
                    conn.commit();return True
                except BaseException:
                    conn.rollback();raise
        finally:
            self._lock.release()

    def verify_trusted_queued_archive(self, *, pending: TrustedQueuedPending, current: Callable[[], bool]) -> dict[str, Any]:
        """Record an exact archive association, not an enrolled or readable source.

        Caller holds the chat guard and must have just verified the exact native
        receipt/part. Later enrollment requires its own complete lifecycle gate.
        """
        from dataclasses import asdict
        from src.openclank.queued_source import QueuedSourceRef
        if type(pending) is not TrustedQueuedPending or not callable(current): raise ValueError('queued archive unavailable')
        proof=pending.proof;identity=pending.identity
        if not self._lock.acquire(blocking=False): raise ArchiveUnavailableError(message='archive busy')
        try:
            with sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=rw',uri=True,timeout=0,isolation_level=None) as conn:
                conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE')
                try:
                    self._reject_erased_chat(conn,proof['accountID'],proof['stableChatID'])
                    base=(proof['accountID'],proof['stableChatID'],'main',pending.message_id,pending.part_id,1)
                    staged=conn.execute("SELECT payload_json,state FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=? AND event_kind='queued_pending.v1'",base).fetchone()
                    completion=conn.execute("SELECT payload_json,state FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=? AND event_kind='queued_completion.v1'",base).fetchone()
                    if not staged or not completion or staged['state']!='queued_pending' or completion['state']!='queued_completed': raise ValueError('queued completion unavailable')
                    original=json.loads(staged['payload_json']);done=json.loads(completion['payload_json'])
                    receipt=done.get('receipt',{}) if type(done) is dict else {}
                    expected={'version':'queued.pending.v2','identity':identity,'proof':proof,'proofSHA256':pending.proof_sha256,
                              'payloadHash':pending.payload_hash,'messageID':pending.message_id,'partID':pending.part_id,'binding':pending.binding,'producer':pending.producer}
                    if not _valid_queued_producer(pending.producer):raise ValueError('queued producer unavailable')
                    if (original!=expected or set(done)!={'version','identity','receipt'} or done['version']!='queued.completion.v1'
                        or done['identity']!=identity or type(receipt) is not dict or receipt.get('outcome')!='completed'
                        or receipt.get('accountID')!=proof['accountID'] or receipt.get('owner')!=proof['owner']
                        or receipt.get('chatID')!=proof['stableChatID'] or receipt.get('rootSessionID')!=proof['sessionID']
                        or receipt.get('authorityWorkspaceID')!=proof['workspaceID'] or receipt.get('mappingRevision')!=proof['mappingRevision']
                        or receipt.get('workspaceRevision')!=proof['workspaceRevision']
                        or any(receipt.get(k)!=identity.get(k) for k in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID'))):
                        raise ValueError('queued completion changed')
                    row=conn.execute("SELECT revision,content_hash,tombstone,role,part_type,runtime_generation,CASE WHEN length(CAST(content AS BLOB))<=1000000 THEN content ELSE NULL END AS content FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id='main' AND message_id=? AND part_id=? ORDER BY revision DESC LIMIT 1",(proof['accountID'],proof['stableChatID'],pending.message_id,pending.part_id)).fetchone()
                    if (not row or row['tombstone'] or row['role']!='user' or row['part_type']!='text' or row['runtime_generation']!='managed-mimo-v1'
                        or type(row['content']) is not str or _content_hash(row['content'])!=row['content_hash']): raise ValueError('queued archive part unavailable')
                    part=json.loads(row['content'])
                    if (type(part) is not dict or part.get('id')!=pending.part_id or part.get('messageID')!=pending.message_id
                        or part.get('sessionID')!=proof['sessionID'] or part.get('type')!='text' or type(part.get('text')) is not str
                        or part.get('ignored',False) is not False or part.get('synthetic',False) is not False
                        or hashlib.sha256(part['text'].encode('utf-8')).hexdigest()!=proof['bodySHA256']): raise ValueError('queued archive part changed')
                    ref=QueuedSourceRef(1,'queued_user_text',proof['accountID'],proof['stableChatID'],proof['workspaceID'],proof['requestID'],
                        proof['sessionID'],pending.message_id,pending.part_id,row['revision'],row['content_hash'],proof['bodySHA256'],
                        pending.proof_sha256,identity['rootOperationID'],identity['executionRevision'],identity['innerTurnID'])
                    payload=_canonical_json({'version':'queued.archive-verified.v2','source':asdict(ref),'producer':pending.producer})
                    key=(proof['accountID'],proof['stableChatID'],'main',pending.message_id,pending.part_id,row['revision'])
                    previous=conn.execute("SELECT payload_json,state FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=? AND event_kind='queued_archive_verified.v1'",key).fetchone()
                    if previous and (previous['payload_json']!=payload or previous['state']!='queued_verified'): raise ValueError('queued association conflict')
                    if current() is not True: raise ValueError('queued source authority changed')
                    if not previous:
                        now=_now_ms();conn.execute("INSERT INTO conversation_archive_outbox(owner,chat_id,actor_id,message_id,part_id,revision,event_kind,payload_json,state,attempts,created_at,updated_at) VALUES (?,?,?,?,?,?,'queued_archive_verified.v1',?,'queued_verified',0,?,?)",(*key,payload,now,now))
                    conn.commit();return {'state':'verified_only','source':ref}
                except BaseException:
                    conn.rollback();raise
        finally:
            self._lock.release()

    def verify_current_queued_source(self, ref, *, current: Callable[[], bool]) -> dict[str, Any]:
        """Revalidate the exact completed queued association without returning body text.

        The caller holds the chat guard and separately verifies the native row,
        current mapping and owner. This is neither a capture nor a fence grant.
        """
        from dataclasses import asdict
        from src.openclank.queued_source import QueuedSourceRef, _valid
        if type(ref) is not QueuedSourceRef or not callable(current) or ref.version != 1 or ref.kind != 'queued_user_text':
            raise ValueError('queued source unavailable')
        if not self._lock.acquire(blocking=False):
            raise ArchiveUnavailableError(message='archive busy')
        try:
            with sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=ro', uri=True, timeout=0) as conn:
                conn.row_factory=sqlite3.Row
                conn.execute('BEGIN')
                self._reject_erased_chat(conn, ref.account_id, ref.chat_id)
                base=(ref.account_id,ref.chat_id,'main',ref.message_id,ref.part_id,1)
                def event(kind, key=base):
                    row=conn.execute('SELECT state,CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END AS payload FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=? AND event_kind=?',(*key,kind)).fetchone()
                    expected_state={'queued_pending.v1':'queued_pending','queued_completion.v1':'queued_completed','queued_archive_verified.v1':'queued_verified'}[kind]
                    if not row or row['state']!=expected_state or type(row['payload']) is not str: raise ValueError('queued association unavailable')
                    value=json.loads(row['payload'])
                    if type(value) is not dict or _canonical_json(value)!=row['payload']: raise ValueError('queued association changed')
                    return value
                staged=event('queued_pending.v1'); done=event('queued_completion.v1')
                proof=staged.get('proof'); identity=staged.get('identity'); binding=staged.get('binding')
                if (set(staged)!={'version','identity','proof','proofSHA256','payloadHash','messageID','partID','binding','producer'}
                    or staged['version']!='queued.pending.v2' or not _valid_queued_producer(staged['producer']) or not _valid(proof) or type(identity) is not dict or type(binding) is not dict
                    or set(identity)!={'sessionID','rootOperationID','actorID','executionRevision','innerTurnID'}
                    or set(binding)!={'engineSessionID','stableChatID','owner','canonicalCwd','authorityWorkspaceID','memoryWorkspaceID','copalWorkspace','mappingRevision'}
                    or staged['messageID']!=ref.message_id or staged['partID']!=ref.part_id or staged['proofSHA256']!=ref.ingress_sha256
                    or proof['accountID']!=ref.account_id or proof['stableChatID']!=ref.chat_id or proof['workspaceID']!=ref.workspace_id
                    or proof['sessionID']!=ref.native_session_id or proof['requestID']!=ref.request_id or proof['bodySHA256']!=ref.text_sha256
                    or proof['captureEligible'] is not True or any(proof['choices'][k] is not False for k in ('incognito','noMemory','compareMode'))
                    or identity!={'sessionID':ref.native_session_id,'rootOperationID':ref.root_operation_id,'actorID':'main','executionRevision':ref.execution_revision,'innerTurnID':ref.inner_turn_id}
                    or binding['engineSessionID']!=ref.native_session_id or binding['stableChatID']!=ref.chat_id or binding['owner']!=proof['owner']
                    or binding['authorityWorkspaceID']!=ref.workspace_id or binding['mappingRevision']!=proof['mappingRevision']):
                    raise ValueError('queued staged source changed')
                receipt=done.get('receipt')
                if (set(done)!={'version','identity','receipt'} or done['version']!='queued.completion.v1' or done['identity']!=identity
                    or type(receipt) is not dict or receipt.get('outcome')!='completed' or receipt.get('accountID')!=ref.account_id
                    or receipt.get('owner')!=proof['owner'] or receipt.get('chatID')!=ref.chat_id or receipt.get('rootSessionID')!=ref.native_session_id
                    or receipt.get('authorityWorkspaceID')!=ref.workspace_id or receipt.get('mappingRevision')!=proof['mappingRevision']
                    or receipt.get('workspaceRevision')!=proof['workspaceRevision'] or any(receipt.get(k)!=identity[k] for k in identity)):
                    raise ValueError('queued completion changed')
                associated=event('queued_archive_verified.v1',(ref.account_id,ref.chat_id,'main',ref.message_id,ref.part_id,ref.archive_revision))
                if associated!={'version':'queued.archive-verified.v2','source':asdict(ref),'producer':staged['producer']}: raise ValueError('queued association changed')
                row=conn.execute("SELECT revision,content_hash,tombstone,role,part_type,runtime_generation,CASE WHEN length(CAST(content AS BLOB))<=1000000 THEN content END AS content FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id='main' AND message_id=? AND part_id=? ORDER BY revision DESC LIMIT 1",(ref.account_id,ref.chat_id,ref.message_id,ref.part_id)).fetchone()
                if (not row or row['revision']!=ref.archive_revision or row['content_hash']!=ref.archive_content_hash or row['tombstone']
                    or row['role']!='user' or row['part_type']!='text' or row['runtime_generation']!='managed-mimo-v1'
                    or type(row['content']) is not str or _content_hash(row['content'])!=ref.archive_content_hash): raise ValueError('queued archive part changed')
                part=json.loads(row['content'])
                if (type(part) is not dict or part.get('id')!=ref.part_id or part.get('messageID')!=ref.message_id
                    or part.get('sessionID')!=ref.native_session_id or part.get('type')!='text' or type(part.get('text')) is not str
                    or part.get('ignored',False) is not False or part.get('synthetic',False) is not False
                    or hashlib.sha256(part['text'].encode('utf-8')).hexdigest()!=ref.text_sha256 or current() is not True):
                    raise ValueError('queued current source changed')
                return {'state':'current','source':ref,'binding':binding,'payload_hash':staged['payloadHash'],'producer':staged['producer']}
        finally:
            self._lock.release()

    def record_queued_retirement(self, ref, fence: dict[str, Any], *, holder_sha256: str,
                                 generation: int, epoch: int, current: Callable[[], bool]) -> dict[str, Any]:
        """Stage a content-free origin closure under the caller's live drain lease."""
        from dataclasses import asdict
        from src.openclank.queued_source import QueuedSourceRef
        if (type(ref) is not QueuedSourceRef or type(fence) is not dict or not callable(current)
            or not isinstance(holder_sha256,str) or re.fullmatch(r'[0-9a-f]{64}',holder_sha256) is None
            or type(generation) is not int or generation < 0 or type(epoch) is not int or epoch < 0):
            raise ValueError('queued retirement unavailable')
        source=asdict(ref)
        source_id='queuedpart_'+hashlib.sha256(json.dumps(['openclank.queued-source.v1',ref.account_id,ref.chat_id,
            ref.native_session_id,ref.request_id,ref.message_id,ref.part_id],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        origin_id='cs_s04queued_'+hashlib.sha256(json.dumps([source_id,ref.archive_revision],separators=(',',':')).encode()).hexdigest()
        if (set(fence)!={'version','source_id','origin_id','source','state','visibility','covered'}
            or fence['version']!=1 or fence['source_id']!=source_id or fence['origin_id']!=origin_id
            or fence['source']!=source or fence['state'] not in ('fenced','already_fenced')
            or fence['visibility']!='closed' or fence['covered']!=['queued_origin_only']):
            raise ValueError('queued retirement fence unavailable')
        payload=_canonical_json({'version':'queued.retirement.v1','source':source,'fence':fence,
                                 'holderSHA256':holder_sha256,'generation':generation,'epoch':epoch})
        if not self._lock.acquire(blocking=False): raise ArchiveUnavailableError(message='archive busy')
        try:
            with sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=rw',uri=True,timeout=0,isolation_level=None) as conn:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    key=(ref.account_id,ref.chat_id,'main',ref.message_id,ref.part_id,ref.archive_revision)
                    association=conn.execute("SELECT payload_json,state FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=? AND event_kind='queued_archive_verified.v1'",key).fetchone()
                    producer={'version':1,'holderSHA256':holder_sha256,'generation':generation,'ownerEpoch':epoch}
                    if (not association or association[1]!='queued_verified' or association[0]!=_canonical_json({'version':'queued.archive-verified.v2','source':source,'producer':producer})
                        or current() is not True): raise ValueError('queued retirement authority changed')
                    row=conn.execute("SELECT payload_json,state FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=? AND event_kind='queued_retirement.v1'",key).fetchone()
                    if row:
                        stored=json.loads(row[0]); first=stored.get('fence') if type(stored) is dict else None
                        if (row[1] not in ('queued_retirement_pending','queued_retired') or type(first) is not dict
                            or set(first)!=set(fence) or first.get('state') not in ('fenced','already_fenced')
                            or any(first.get(k)!=v for k,v in fence.items() if k!='state')
                            or stored!={'version':'queued.retirement.v1','source':source,'fence':first,
                                'holderSHA256':holder_sha256,'generation':generation,'epoch':epoch}):
                            raise ValueError('queued retirement conflict')
                        fence=first
                    if not row:
                        now=_now_ms();conn.execute("INSERT INTO conversation_archive_outbox(owner,chat_id,actor_id,message_id,part_id,revision,event_kind,payload_json,state,attempts,created_at,updated_at) VALUES (?,?,?,?,?,?,'queued_retirement.v1',?,'queued_retirement_pending',0,?,?)",(*key,payload,now,now))
                    if current() is not True: raise ValueError('queued retirement authority changed')
                    conn.commit();return dict(fence)
                except BaseException:
                    conn.rollback();raise
        finally:
            self._lock.release()

    def verify_queued_retirement_stage(self, ref, fence, *, holder_sha256, generation, epoch, current):
        """Read the exact first staged fence; absence is not retirement proof."""
        from dataclasses import asdict
        from src.openclank.queued_source import QueuedSourceRef
        if type(ref) is not QueuedSourceRef or type(fence) is not dict or not callable(current):
            raise ValueError('queued retirement unavailable')
        payload=_canonical_json({'version':'queued.retirement.v1','source':asdict(ref),'fence':fence,
            'holderSHA256':holder_sha256,'generation':generation,'epoch':epoch})
        if not self._lock.acquire(blocking=False): raise ArchiveUnavailableError(message='archive busy')
        try:
            with sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=ro',uri=True,timeout=0) as conn:
                conn.execute('BEGIN')
                row=conn.execute("SELECT payload_json,state FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id='main' AND message_id=? AND part_id=? AND revision=? AND event_kind='queued_retirement.v1'",(ref.account_id,ref.chat_id,ref.message_id,ref.part_id,ref.archive_revision)).fetchone()
                if (not row or row[0]!=payload or row[1] not in ('queued_retirement_pending','queued_retired')
                    or current() is not True): raise ValueError('queued retirement stage unavailable')
                return True
        finally:self._lock.release()

    def complete_queued_retirement(self, ref, fence: dict[str, Any], *, holder_sha256: str,
                                  generation: int, epoch: int, current: Callable[[], bool]) -> None:
        """Complete an exact staged closure after the caller verifies the native terminal marker."""
        from dataclasses import asdict
        from src.openclank.queued_source import QueuedSourceRef
        if (type(ref) is not QueuedSourceRef or type(fence) is not dict or not callable(current)
            or not isinstance(holder_sha256,str) or re.fullmatch(r'[0-9a-f]{64}',holder_sha256) is None
            or type(generation) is not int or generation < 0 or type(epoch) is not int or epoch < 0):
            raise ValueError('queued retirement unavailable')
        payload=_canonical_json({'version':'queued.retirement.v1','source':asdict(ref),'fence':fence,
                                 'holderSHA256':holder_sha256,'generation':generation,'epoch':epoch})
        if not self._lock.acquire(blocking=False): raise ArchiveUnavailableError(message='archive busy')
        try:
            with sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=rw',uri=True,timeout=0,isolation_level=None) as conn:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    key=(ref.account_id,ref.chat_id,'main',ref.message_id,ref.part_id,ref.archive_revision)
                    row=conn.execute("SELECT payload_json,state FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=? AND event_kind='queued_retirement.v1'",key).fetchone()
                    if (not row or row[1] not in ('queued_retirement_pending','queued_retired')
                        or row[0]!=payload or current() is not True):
                        raise ValueError('queued retirement authority changed')
                    if row[1]=='queued_retirement_pending':
                        result=conn.execute("UPDATE conversation_archive_outbox SET state='queued_retired',updated_at=? WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=? AND event_kind='queued_retirement.v1' AND state='queued_retirement_pending' AND payload_json=?",(_now_ms(),*key,row[0]))
                        if result.rowcount!=1: raise ValueError('queued retirement changed')
                    if current() is not True: raise ValueError('queued retirement authority changed')
                    conn.commit()
                except BaseException:
                    conn.rollback();raise
        finally:
            self._lock.release()

    def record_trusted_maintenance_pending(self, pending: TrustedMaintenancePending, *, current: Callable[[], bool]) -> dict[str, Any]:
        """Commit one exact host-verified summary reference, never summary text.

        The caller holds the live chat and execution guards until this returns.
        Generic archive delivery cannot claim these specially-stated rows.
        """
        from src.memery_external_sources import SourceRef, _source_ref_error
        if type(pending) is not TrustedMaintenancePending or not callable(current):
            raise ValueError('maintenance authority unavailable')
        identity,proof,descriptor,binding=pending.identity,pending.proof,pending.descriptor,pending.binding
        identity_keys={'sessionID','rootOperationID','actorID','executionRevision','innerTurnID'}
        proof_keys={'summaryMessageID','source','sourceAttestationSHA256','sourceMessageID','sourcePartID','sourceTextSHA256','dispatchID','bodySHA256','promptSHA256','wireProfile'}
        if (type(identity) is not dict or set(identity)!=identity_keys or type(proof) is not dict
            or not proof_keys.issubset(proof) or set(proof)-proof_keys-{'instructionsSHA256','instructionsClass'}
            or type(descriptor) is not dict or set(descriptor)!={'sessionID','summaryMessageID','providerID','modelID','parts'}
            or type(binding) is not dict or set(binding)!={'engineSessionID','stableChatID','owner','canonicalCwd','authorityWorkspaceID','memoryWorkspaceID','copalWorkspace','mappingRevision'}):
            raise ValueError('maintenance proof unavailable')
        try:
            source=SourceRef(**proof['source'])
        except (TypeError,ValueError):
            raise ValueError('maintenance source unavailable')
        strings=[identity.get(k) for k in identity_keys-{'executionRevision'}]+[descriptor.get(k) for k in ('sessionID','summaryMessageID','providerID','modelID')]+[proof.get(k) for k in ('sourceMessageID','sourcePartID','dispatchID')]
        hashes=[proof.get(k) for k in ('sourceAttestationSHA256','sourceTextSHA256','bodySHA256','promptSHA256')]
        parts=descriptor['parts']
        if (_source_ref_error(source) or source.actor_id!='main' or source.part_id!='body' or source.revision!=1
            or identity['actorID']!='main' or type(identity['executionRevision']) is not int or identity['executionRevision']<1
            or descriptor['summaryMessageID']!=proof['summaryMessageID']
            or binding['engineSessionID']!=identity['sessionID'] or binding['stableChatID']!=source.chat_id
            or binding['authorityWorkspaceID']!=('' if source.version == 2 and source.workspace_id is None else source.workspace_id)
            or any(type(binding.get(k)) is not str or not binding[k] or binding[k]!=binding[k].strip() or len(binding[k])>4096 for k in ('owner','canonicalCwd','memoryWorkspaceID','copalWorkspace'))
            or not os.path.isabs(binding['canonicalCwd']) or os.path.normpath(binding['canonicalCwd'])!=binding['canonicalCwd']
            or type(binding['mappingRevision']) is not int or binding['mappingRevision']<0
            or descriptor['sessionID']!=identity['sessionID'] or any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in strings)
            or any(type(v) is not str or re.fullmatch(r'[0-9a-f]{64}',v) is None for v in hashes)
            or type(parts) is not list or not 1<=len(parts)<=16
            or any(type(p) is not dict or set(p)!={'partID','sha256'} or type(p['partID']) is not str or not p['partID'] or p['partID']!=p['partID'].strip() or len(p['partID'])>256 or type(p['sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',p['sha256']) is None for p in parts)
            or len({p['partID'] for p in parts})!=len(parts) or proof['wireProfile'] not in ('openai-chat','codex-responses')):
            raise ValueError('maintenance proof unavailable')
        if proof['wireProfile']=='openai-chat' and ('instructionsClass' in proof or 'instructionsSHA256' in proof):
            raise ValueError('maintenance instruction proof unavailable')
        if proof['wireProfile']=='codex-responses' and (proof.get('instructionsClass')!='builtin-compaction-v1' or type(proof.get('instructionsSHA256')) is not str or re.fullmatch(r'[0-9a-f]{64}',proof['instructionsSHA256']) is None):
            raise ValueError('maintenance instructions unavailable')
        payload={'version':'maintenance.pending.v1','identity':identity,'proof':proof,'descriptor':descriptor,'binding':binding}
        encoded=_canonical_json(payload)
        if len(encoded.encode('utf-8'))>65536:
            raise ValueError('maintenance proof exceeds bound')
        key=_canonical_json({'source':proof['source'],'sessionID':descriptor['sessionID'],'summaryMessageID':descriptor['summaryMessageID'],'parts':parts})
        kind='maintenance_pending:'+hashlib.sha256(key.encode('utf-8')).hexdigest()
        if not self._lock.acquire(blocking=False):
            raise ArchiveUnavailableError(message='archive busy')
        try:
            with sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=rw',uri=True,timeout=0,isolation_level=None) as conn:
                conn.row_factory=sqlite3.Row
                conn.execute('PRAGMA foreign_keys=ON')
                conn.execute('BEGIN IMMEDIATE')
                try:
                    for statement in _MAINTENANCE_PENDING_INDEXES:
                        name=statement.split()[2]
                        row=conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(name,)).fetchone()
                        if not row or row['sql']!=statement:
                            raise ArchiveUnavailableError(message='maintenance schema unavailable')
                    if conn.execute("SELECT 1 FROM conversation_archive_outbox INDEXED BY ix_conv_maintenance_invalid WHERE owner=? AND chat_id=? AND message_id=? AND event_kind GLOB 'maintenance_pending:*' AND NOT json_valid(payload_json) LIMIT 1",(source.account_id,source.chat_id,source.message_id)).fetchone():
                        raise ArchiveUnavailableError(message='maintenance evidence unavailable')
                    self._reject_erased_chat(conn,source.account_id,source.chat_id)
                    admission=conn.execute('SELECT receipt_json FROM origin_admission WHERE owner=? AND chat_id=? AND actor_id=? AND admission_id=?',(source.account_id,source.chat_id,source.actor_id,source.message_id)).fetchone()
                    part=conn.execute('SELECT revision,content_hash,tombstone,event_workspace,event_project FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? ORDER BY revision DESC LIMIT 1',(source.account_id,source.chat_id,source.actor_id,source.message_id,source.part_id)).fetchone()
                    if not admission or not part:
                        raise ValueError('maintenance origin unavailable')
                    policy=json.loads(admission['receipt_json'])
                    if (policy.get('owner')!=source.account_id or policy.get('chat_id')!=source.chat_id or policy.get('actor_id')!=source.actor_id
                        or policy.get('admission_id')!=source.message_id or policy.get('root_operation_id')!=source.message_id
                        or policy.get('capture_eligible') is not True or policy.get('incognito') is not False or policy.get('no_memory') is not False or policy.get('compare_mode') is not False
                        or policy.get('policy_source') not in ('trusted_revision','trusted_fingerprint') or type(policy.get('policy_value')) is not str or not 0<len(policy['policy_value'])<=256
                        or (policy['policy_source']=='trusted_fingerprint' and re.fullmatch(r'[0-9a-f]{64}',policy['policy_value']) is None)
                        or part['tombstone'] or part['revision']!=source.revision
                        or part['content_hash']!=source.content_hash or part['event_workspace']!=source.workspace_id or part['event_project']!=source.project_id):
                        raise ValueError('maintenance origin changed')
                    existing=conn.execute("SELECT event_kind,payload_json FROM conversation_archive_outbox INDEXED BY ix_conv_maintenance_pending WHERE owner=? AND chat_id=? AND message_id=? AND json_extract(payload_json,'$.descriptor.summaryMessageID')=? AND event_kind GLOB 'maintenance_pending:*' AND json_valid(payload_json) LIMIT 2",(source.account_id,source.chat_id,source.message_id,descriptor['summaryMessageID'])).fetchall()
                    if len(existing)>1 or (existing and (existing[0]['event_kind']!=kind or existing[0]['payload_json']!=encoded)):
                        raise ValueError('maintenance reference conflict')
                    if current() is not True:
                        raise ValueError('maintenance authority changed')
                    if not existing:
                        now=_now_ms()
                        conn.execute("INSERT INTO conversation_archive_outbox(owner,chat_id,actor_id,message_id,part_id,revision,event_kind,payload_json,state,attempts,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'maintenance_pending',0,?,?)",(source.account_id,source.chat_id,source.actor_id,source.message_id,source.part_id,source.revision,kind,encoded,now,now))
                    conn.commit()
                    return {'state':'pending','event_kind':kind,'replayed':bool(existing)}
                except BaseException:
                    conn.rollback()
                    raise
        finally:
            self._lock.release()

    def retire_trusted_maintenance_pending(self, *, account_id: str, chat_id: str, message_id: str, current: Callable[[], bool]) -> int:
        """Erase exact pending summary references before a guarded source mutation."""
        if any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (account_id,chat_id,message_id)) or not callable(current):
            raise ValueError('maintenance source unavailable')
        if not self._lock.acquire(blocking=False):
            raise ArchiveUnavailableError(message='archive busy')
        try:
            with sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=rw',uri=True,timeout=0,isolation_level=None) as conn:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    indexes={row[0]:row[1] for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND name IN ('ix_conv_maintenance_pending','ix_conv_maintenance_invalid','ix_conv_maintenance_page')")}
                    present=conn.execute("SELECT EXISTS(SELECT 1 FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id='main' AND message_id=? AND part_id='body' AND revision=1 AND event_kind GLOB 'maintenance_pending:*')",(account_id,chat_id,message_id)).fetchone()[0]
                    expected={statement.split()[2]:statement for statement in _MAINTENANCE_PENDING_INDEXES}
                    if indexes and indexes!=expected or present and indexes!=expected:
                        raise ArchiveUnavailableError(message='maintenance schema unavailable')
                    if current() is not True:
                        raise ValueError('maintenance authority changed')
                    deleted=conn.execute("DELETE FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id='main' AND message_id=? AND part_id='body' AND revision=1 AND event_kind GLOB 'maintenance_pending:*'",(account_id,chat_id,message_id)).rowcount
                    conn.commit();return deleted
                except BaseException:
                    conn.rollback();raise
        finally:
            self._lock.release()

    def discover_trusted_maintenance_pending(self, *, owner: str, after: Optional[tuple] = None,
                                             limit: int = 32, authorize: Callable[[str,str], bool]) -> dict[str, Any]:
        """Bounded source hints independent of the completed-turn consumer cursor."""
        if (type(owner) is not str or not owner or owner!=owner.strip() or len(owner)>512 or type(limit) is not int or not 1<=limit<=32
            or not callable(authorize) or (after is not None and (type(after) is not tuple or len(after)!=3
                or any(type(v) is not str or not v or len(v)>512 for v in after[:2]) or type(after[2]) is not int or after[2]<1))):
            raise ValueError('maintenance discovery unavailable')
        with self._lock, sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=ro',uri=True,isolation_level=None) as conn:
            conn.row_factory=sqlite3.Row
            for statement in _MAINTENANCE_PENDING_INDEXES:
                row=conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(statement.split()[2],)).fetchone()
                if not row or row['sql']!=statement:
                    raise ArchiveUnavailableError(message='maintenance schema unavailable')
            suffix=' AND (chat_id,message_id,outbox_seq)>(?,?,?)' if after is not None else ''
            args=(owner,*after,limit+1) if after is not None else (owner,limit+1)
            rows=conn.execute("SELECT chat_id,message_id,outbox_seq,CASE WHEN length(payload_json)<=65536 THEN payload_json ELSE NULL END AS payload_json FROM conversation_archive_outbox INDEXED BY ix_conv_maintenance_page WHERE owner=? AND event_kind GLOB 'maintenance_pending:*' AND state='maintenance_pending'"+suffix+" ORDER BY chat_id,message_id,outbox_seq LIMIT ?",args).fetchall()
            items=[]
            for row in rows[:limit]:
                payload=json.loads(row['payload_json'])
                if type(payload) is not dict or _canonical_json(payload)!=row['payload_json'] or type(payload.get('proof')) is not dict or type(payload['proof'].get('source')) is not dict:
                    raise ValueError('maintenance reference unavailable')
                source=payload['proof']['source']
                if (source.get('account_id')!=owner or source.get('chat_id')!=row['chat_id'] or source.get('message_id')!=row['message_id']
                    or source.get('actor_id')!='main' or source.get('part_id')!='body' or source.get('revision')!=1
                    or type(source.get('content_hash')) is not str or re.fullmatch(r'[0-9a-f]{64}',source['content_hash']) is None
                    or authorize(owner,row['chat_id']) is not True):
                    raise ValueError('maintenance source unavailable')
                items.append({'chat_id':row['chat_id'],'message_id':row['message_id'],'content_hash':source['content_hash'],'seq':row['outbox_seq']})
            last=rows[min(len(rows),limit)-1] if rows else None
            return {'items':tuple(items),'next_after':(last['chat_id'],last['message_id'],last['outbox_seq']) if len(rows)>limit else None}

    def read_trusted_maintenance_pending(self, *, owner: str, chat_id: str, message_id: str, content_hash: str,
                                         after: int = 0, limit: int = 32, authorize: Callable[[], bool]) -> dict[str, Any]:
        """Page exact pending references only after the matching origin-ready receipt."""
        if (any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (owner,chat_id,message_id))
            or type(content_hash) is not str or re.fullmatch(r'[0-9a-f]{64}',content_hash) is None
            or type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=32 or not callable(authorize)):
            raise ValueError('maintenance source unavailable')
        with self._lock, sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=ro',uri=True,isolation_level=None) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('BEGIN')
            try:
                for statement in _MAINTENANCE_PENDING_INDEXES:
                    row=conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(statement.split()[2],)).fetchone()
                    if not row or row['sql']!=statement:
                        raise ArchiveUnavailableError(message='maintenance schema unavailable')
                link=conn.execute("SELECT 1 FROM origin_part_link l JOIN origin_completion c ON c.owner=l.owner AND c.chat_id=l.chat_id AND c.actor_id=l.actor_id AND c.admission_id=l.admission_id JOIN conversation_archive_outbox o ON o.owner=l.owner AND o.chat_id=l.chat_id AND o.actor_id=l.actor_id AND o.message_id=l.message_id AND o.part_id=l.part_id AND o.revision=l.revision AND o.event_kind='origin_ready' WHERE l.owner=? AND l.chat_id=? AND l.actor_id='main' AND l.message_id=? AND l.part_id='body' AND l.revision=1 AND l.content_hash=? AND l.admission_id=l.message_id AND json_valid(c.receipt_json) AND json_extract(c.receipt_json,'$.outcome')='completed' AND json_valid(o.payload_json) AND json_extract(o.payload_json,'$.content_hash')=l.content_hash AND json_extract(o.payload_json,'$.admission_id')=l.admission_id",(owner,chat_id,message_id,content_hash)).fetchone()
                if not link or authorize() is not True:
                    raise ValueError('maintenance origin not ready')
                rows=conn.execute("SELECT outbox_seq,event_kind,CASE WHEN length(payload_json)<=65536 THEN payload_json ELSE NULL END AS payload_json FROM conversation_archive_outbox INDEXED BY ix_conv_maintenance_page WHERE owner=? AND chat_id=? AND message_id=? AND event_kind GLOB 'maintenance_pending:*' AND state='maintenance_pending' AND outbox_seq>? ORDER BY outbox_seq LIMIT ?",(owner,chat_id,message_id,after,limit+1)).fetchall()
                result=[]
                for row in rows[:limit]:
                    payload=json.loads(row['payload_json'])
                    if type(payload) is not dict or set(payload)!={'version','identity','proof','descriptor','binding'} or payload['version']!='maintenance.pending.v1' or _canonical_json(payload)!=row['payload_json']:
                        raise ValueError('maintenance reference unavailable')
                    proof,descriptor,binding,identity=payload['proof'],payload['descriptor'],payload['binding'],payload['identity']
                    if (type(proof) is not dict or type(descriptor) is not dict or type(proof.get('source')) is not dict
                        or type(identity) is not dict or set(identity)!={'sessionID','rootOperationID','actorID','executionRevision','innerTurnID'}
                        or type(binding) is not dict or set(binding)!={'engineSessionID','stableChatID','owner','canonicalCwd','authorityWorkspaceID','memoryWorkspaceID','copalWorkspace','mappingRevision'}):
                        raise ValueError('maintenance reference unavailable')
                    source=proof['source']
                    if (source.get('account_id')!=owner or source.get('chat_id')!=chat_id or source.get('message_id')!=message_id
                        or source.get('actor_id')!='main' or source.get('part_id')!='body' or source.get('revision')!=1 or source.get('content_hash')!=content_hash
                        or binding.get('engineSessionID')!=identity.get('sessionID') or descriptor.get('sessionID')!=identity.get('sessionID')
                        or binding.get('stableChatID')!=chat_id
                        or not ((type(source.get('version')) is int and source['version']==2 and source.get('workspace_id') is None and binding.get('authorityWorkspaceID')=='')
                            or (type(source.get('version')) is int and source['version']==1 and type(source.get('workspace_id')) is str and bool(source['workspace_id']) and binding.get('authorityWorkspaceID')==source['workspace_id']))):
                        raise ValueError('maintenance source changed')
                    key=_canonical_json({'source':source,'sessionID':descriptor['sessionID'],'summaryMessageID':descriptor['summaryMessageID'],'parts':descriptor['parts']})
                    if row['event_kind']!='maintenance_pending:'+hashlib.sha256(key.encode('utf-8')).hexdigest():
                        raise ValueError('maintenance reference changed')
                    result.append(TrustedMaintenanceReady(row['outbox_seq'],hashlib.sha256(row['payload_json'].encode('utf-8')).hexdigest(),row['payload_json'],_MAINTENANCE_READY_SEAL))
                if authorize() is not True:
                    raise ValueError('maintenance authority changed')
                conn.commit()
                return {'items':tuple(result),'next_after':result[-1].seq if len(rows)>limit else None}
            except BaseException:
                conn.rollback();raise

    def ack_trusted_maintenance_pending(self, *, ready: TrustedMaintenanceReady, current: Callable[[], bool]) -> bool:
        """Acknowledge only after the caller proves its exact native receipt."""
        if type(ready) is not TrustedMaintenanceReady or not callable(current):
            raise ValueError('maintenance acknowledgement unavailable')
        payload=ready.payload()
        source=payload.get('proof',{}).get('source',{})
        owner,chat_id,message_id=source.get('account_id'),source.get('chat_id'),source.get('message_id')
        seq,payload_sha256=ready.seq,ready.payload_sha256
        if (any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (owner,chat_id,message_id))
            or type(seq) is not int or seq<1 or type(payload_sha256) is not str
            or re.fullmatch(r'[0-9a-f]{64}',payload_sha256) is None):
            raise ValueError('maintenance acknowledgement unavailable')
        with self._lock, sqlite3.connect(Path(self.db_path).absolute().as_uri()+'?mode=rw',uri=True,isolation_level=None) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('BEGIN IMMEDIATE')
            try:
                row=conn.execute("SELECT payload_json,state FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND message_id=? AND outbox_seq=? AND event_kind GLOB 'maintenance_pending:*'",(owner,chat_id,message_id,seq)).fetchone()
                if not row or hashlib.sha256(row['payload_json'].encode('utf-8')).hexdigest()!=payload_sha256 or row['state'] not in ('maintenance_pending','maintenance_delivered') or current() is not True:
                    raise ValueError('maintenance acknowledgement changed')
                if row['state']=='maintenance_pending':
                    now=_now_ms()
                    if conn.execute("UPDATE conversation_archive_outbox SET state='maintenance_delivered',updated_at=?,delivered_at=? WHERE owner=? AND chat_id=? AND message_id=? AND outbox_seq=? AND state='maintenance_pending'",(now,now,owner,chat_id,message_id,seq)).rowcount!=1:
                        raise ValueError('maintenance acknowledgement changed')
                conn.commit();return True
            except BaseException:
                conn.rollback();raise

    def record_trusted_origin_admission(self, admission: TrustedOriginAdmission) -> dict[str, Any]:
        """Record immutable internal evidence; this API is not exposed to generic append."""
        if not self._origin_available():
            raise ArchiveUnavailableError(message="origin policy storage is inactive")
        if type(admission) is not TrustedOriginAdmission:
            raise TypeError("trusted admission object required")
        owner = self.resolve_owner(admission.owner)
        identifiers = (admission.admission_id, owner, admission.chat_id, admission.actor_id)
        if any(not isinstance(x, str) or not x or x != x.strip() or len(x) > 512 for x in identifiers):
            raise ValueError("invalid admission identity")
        if any(value is not None and type(value) is not bool for value in (admission.incognito, admission.no_memory, admission.compare_mode, admission.capture_eligible)):
            raise ValueError("invalid privacy choices")
        if admission.root_operation_id is not None and (not isinstance(admission.root_operation_id, str) or not admission.root_operation_id or len(admission.root_operation_id) > 512):
            raise ValueError("invalid root identity")
        execution = (admission.engine_session_id, admission.execution_revision, admission.inner_turn_id)
        if any(x is not None for x in execution):
            if (not admission.root_operation_id or not isinstance(execution[0], str) or not execution[0] or len(execution[0]) > 512
                    or type(execution[1]) is not int or execution[1] < 0 or not isinstance(execution[2], str) or not execution[2] or len(execution[2]) > 512):
                raise ValueError("incomplete execution identity")
        if (admission.policy_source is None) != (admission.policy_value is None):
            raise ValueError("incomplete policy provenance")
        if admission.policy_source is not None:
            if admission.policy_source not in {"trusted_revision", "trusted_fingerprint"} or not isinstance(admission.policy_value, str) or not 0 < len(admission.policy_value) <= 256:
                raise ValueError("invalid policy provenance")
            if admission.policy_source == "trusted_fingerprint" and not re.fullmatch(r"[0-9a-f]{64}", admission.policy_value):
                raise ValueError("invalid policy fingerprint")
        receipt = _canonical_json({**admission.__dict__, "owner": owner, "version": 1})
        key = (owner, admission.chat_id, admission.actor_id, admission.admission_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._reject_erased_chat(conn, owner, admission.chat_id)
                previous = conn.execute("SELECT receipt_json FROM origin_admission WHERE admission_id=?", (admission.admission_id,)).fetchone()
                if previous:
                    if previous["receipt_json"] != receipt:
                        raise ValueError("conflicting origin admission")
                    conn.commit()
                    return {"accepted": False, "replayed": True}
                conn.execute("INSERT INTO origin_admission VALUES (?,?,?,?,?)", (*key, receipt))
                conn.commit()
                return {"accepted": True, "replayed": False}
            except BaseException:
                conn.rollback()
                raise

    def record_trusted_origin_completion(self, completion: TrustedOriginCompletion) -> dict[str, Any]:
        """Link exact current parts and enqueue origin-ready only for proven eligible completion."""
        if not self._origin_available():
            raise ArchiveUnavailableError(message="origin policy storage is inactive")
        if type(completion) is not TrustedOriginCompletion or type(completion.parts) is not tuple:
            raise TypeError("trusted completion object required")
        owner = self.resolve_owner(completion.owner)
        key = (owner, completion.chat_id, completion.actor_id, completion.admission_id)
        if any(not isinstance(x, str) or not x or x != x.strip() or len(x) > 512 for x in key):
            raise ValueError("invalid completion identity")
        if completion.outcome not in {"completed", "cancelled", "error"} or not 1 <= len(completion.parts) <= 128:
            raise ValueError("invalid completion")
        refs = []
        for part in completion.parts:
            if type(part) is not TrustedOriginPart or any(not isinstance(x, str) or not x or x != x.strip() or len(x) > 512 for x in (part.message_id, part.part_id)) or type(part.revision) is not int or part.revision < 1 or not isinstance(part.content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", part.content_hash):
                raise ValueError("invalid source reference")
            refs.append((part.message_id, part.part_id, part.revision, part.content_hash))
        if len(set(refs)) != len(refs) or len({(x[0], x[1]) for x in refs}) != len(refs):
            raise ValueError("duplicate source reference")
        refs.sort()
        receipt = _canonical_json({"version": 1, "owner": owner, "chat_id": completion.chat_id, "actor_id": completion.actor_id,
            "admission_id": completion.admission_id, "outcome": completion.outcome, "parts": refs})
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._reject_erased_chat(conn, owner, completion.chat_id)
                admission = conn.execute("SELECT receipt_json FROM origin_admission WHERE owner=? AND chat_id=? AND actor_id=? AND admission_id=?", key).fetchone()
                if not admission:
                    raise ValueError("origin admission unavailable")
                policy = json.loads(admission["receipt_json"])
                ready = (completion.outcome == "completed" and policy["capture_eligible"] is True and
                         policy["incognito"] is False and policy["no_memory"] is False and policy["compare_mode"] is False and
                         policy["policy_source"] is not None and policy["policy_value"] is not None)
                previous = conn.execute("SELECT receipt_json FROM origin_completion WHERE owner=? AND chat_id=? AND actor_id=? AND admission_id=?", key).fetchone()
                if previous and previous["receipt_json"] != receipt:
                    raise ValueError("conflicting origin completion")
                rows = []
                for message_id, part_id, revision, digest in refs:
                    row = conn.execute("SELECT * FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? ORDER BY revision DESC LIMIT 1", (owner, completion.chat_id, completion.actor_id, message_id, part_id)).fetchone()
                    if not row or row["revision"] != revision or row["tombstone"] or row["content_hash"] != digest or _content_hash(row["content"]) != digest:
                        raise ValueError("source revision unavailable or changed")
                    rows.append(row)
                if previous:
                    conn.commit()
                    return {"accepted": False, "replayed": True, "ready": ready}
                conn.execute("INSERT INTO origin_completion VALUES (?,?,?,?,?)", (*key, receipt))
                now = _now_ms()
                for row in rows:
                    conn.execute("INSERT INTO origin_part_link VALUES (?,?,?,?,?,?,?,?)", (owner, completion.chat_id, completion.actor_id, row["message_id"], row["part_id"], row["revision"], row["content_hash"], completion.admission_id))
                    if ready:
                        part = SourcePart(owner=owner, chat_id=completion.chat_id, actor_id=completion.actor_id,
                            message_id=row["message_id"], part_id=row["part_id"], revision=row["revision"], content_hash=row["content_hash"],
                            runtime_generation=row["runtime_generation"], event_sequence=row["event_sequence"], event_workspace=row["event_workspace"], event_project=row["event_project"], role=row["role"], part_type=row["part_type"], time_created=row["time_created"], time_updated=row["time_updated"])
                        self._enqueue_outbox(conn, part, event_kind="origin_ready", now=now, payload_extra={"admission_id": completion.admission_id, "time_created": row["time_created"], "time_updated": row["time_updated"]})
                conn.commit()
                return {"accepted": True, "replayed": False, "ready": ready}
            except BaseException:
                conn.rollback()
                raise

    def _assistant_component(self, component: TrustedAssistantComponent, *, verify_host: Callable, create: bool, hydrate: bool = False):
        """Read or record a non-delivery association under an existing root origin."""
        if not self._origin_available():
            raise ArchiveUnavailableError(message="origin policy storage is inactive")
        if type(component) is not TrustedAssistantComponent or not callable(verify_host):
            raise ValueError("assistant component unavailable")
        owner = self.resolve_owner(component.owner)
        root, assistant = component.root, component.assistant
        if (owner != component.owner or type(component.chat_id) is not str or not component.chat_id
                or type(root) is not TrustedOriginPart or type(assistant) is not TrustedOriginPart
                or root.part_id != 'body' or assistant.part_id != 'body' or root.message_id == assistant.message_id
                or (component.workspace_id is not None and (type(component.workspace_id) is not str or not component.workspace_id or len(component.workspace_id.encode('utf-8')) > 512))
                or root.revision != 1 or type(assistant.revision) is not int or assistant.revision < 1
                or any(type(x) is not str or not x or len(x.encode('utf-8')) > 512 for x in (owner, component.chat_id, root.message_id, assistant.message_id))
                or any(type(x) is not str or re.fullmatch(r'[0-9a-f]{64}', x) is None for x in (root.content_hash, assistant.content_hash, component.pair_sha256))):
            raise ValueError("assistant component unavailable")
        payload = _canonical_json({'version': 1, 'kind': 'host_assistant_body', 'owner': owner, 'chat_id': component.chat_id,
            'root': [root.message_id, root.part_id, root.revision, root.content_hash],
            'assistant': [assistant.message_id, assistant.part_id, assistant.revision, assistant.content_hash], 'pair_sha256': component.pair_sha256, 'workspace_id': component.workspace_id})
        key = (owner, component.chat_id, 'main', root.message_id, root.part_id, root.revision, 'assistant_component.v1')
        with self._lock, self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                self._reject_erased_chat(conn, owner, component.chat_id)
                if verify_host(component) is not True:
                    raise ValueError('assistant component unavailable')
                origin = conn.execute("SELECT c.receipt_json FROM origin_part_link l JOIN origin_completion c USING(owner,chat_id,actor_id,admission_id) WHERE l.owner=? AND l.chat_id=? AND l.actor_id='main' AND l.message_id=? AND l.part_id='body' AND l.revision=? AND l.content_hash=? AND l.admission_id=l.message_id", (owner, component.chat_id, root.message_id, root.revision, root.content_hash)).fetchone()
                if not origin:
                    raise ValueError('assistant component unavailable')
                receipt = json.loads(origin['receipt_json'])
                if receipt.get('version') != 1 or receipt.get('outcome') != 'completed' or receipt.get('parts') != [[root.message_id, root.part_id, root.revision, root.content_hash]]:
                    raise ValueError('assistant component unavailable')
                contents = []
                for part, role in ((root, 'user'), (assistant, 'assistant')):
                    row = conn.execute("SELECT revision,content_hash,CASE WHEN length(CAST(content AS BLOB))<=? THEN content END AS content,tombstone,role,runtime_generation,part_type,event_workspace,event_project FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id='main' AND message_id=? AND part_id=? ORDER BY revision DESC LIMIT 1", (65536 if role == 'user' else 8192, owner, component.chat_id, part.message_id, part.part_id)).fetchone()
                    if not row or not isinstance(row['content'], str) or row['revision'] != part.revision or row['content_hash'] != part.content_hash or row['tombstone'] or row['role'] != role or row['runtime_generation'] != '0' or row['part_type'] != 'text' or row['event_workspace'] != component.workspace_id or row['event_project'] is not None or _content_hash(row['content']) != part.content_hash:
                        raise ValueError('assistant component unavailable')
                    if hydrate:
                        if not isinstance(row['content'], str) or len(row['content'].encode('utf-8')) > (65536 if role == 'user' else 8192):
                            raise ValueError('assistant component unavailable')
                        contents.append(row['content'])
                row = conn.execute('SELECT payload_json,state FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=? AND event_kind=?', key).fetchone()
                if row is None:
                    if not create:
                        raise ValueError('assistant component unavailable')
                    now = _now_ms()
                    conn.execute("INSERT INTO conversation_archive_outbox(owner,chat_id,actor_id,message_id,part_id,revision,event_kind,payload_json,state,attempts,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'assistant_component',0,?,?)", (*key, payload, now, now))
                elif row['state'] != 'assistant_component' or row['payload_json'] != payload:
                    raise ValueError('assistant component conflict')
                if verify_host(component) is not True:
                    raise ValueError('assistant component unavailable')
                conn.commit()
                return (component, contents[0], contents[1]) if hydrate else component
            except BaseException:
                conn.rollback()
                raise

    def record_assistant_component(self, component: TrustedAssistantComponent, *, verify_host: Callable) -> TrustedAssistantComponent:
        return self._assistant_component(component, verify_host=verify_host, create=True)

    def read_assistant_component(self, component: TrustedAssistantComponent, *, verify_host: Callable) -> TrustedAssistantComponent:
        return self._assistant_component(component, verify_host=verify_host, create=False)

    def hydrate_assistant_component(self, component: TrustedAssistantComponent, *, verify_host: Callable):
        """Read the exact two current parts under the original Host-to-archive guard."""
        return self._assistant_component(component, verify_host=verify_host, create=False, hydrate=True)

    @staticmethod
    def _require_assistant_component_indexes(conn):
        def shape(value):
            return ''.join(part if index%2 else ''.join(part.split()).lower() for index,part in enumerate(re.split(r"('(?:''|[^'])*')",value or '')))
        for definition in _ASSISTANT_COMPONENT_INDEXES:
            row=conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(definition.split()[2],)).fetchone()
            if not row or shape(row[0]) != shape(definition):
                raise ArchiveUnavailableError(message='assistant component profile unavailable')

    @staticmethod
    def _assistant_component_descriptor(row):
        raw=row['payload_json']
        if not isinstance(raw,str) or len(raw.encode('utf-8'))>8192:
            raise ValueError('assistant component unavailable')
        try:
            value=json.loads(raw)
        except (TypeError,ValueError):
            raise ValueError('assistant component unavailable') from None
        if type(value) is not dict or set(value)!={'version','kind','owner','chat_id','root','assistant','pair_sha256','workspace_id'} or _canonical_json(value)!=raw or type(value['version']) is not int or value['version']!=1 or value['kind']!='host_assistant_body' or value['owner']!=row['owner'] or value['chat_id']!=row['chat_id']:
            raise ValueError('assistant component unavailable')
        root,assistant=value['root'],value['assistant']
        def part(item):
            return type(item) is list and len(item)==4 and type(item[0]) is str and 0<len(item[0].encode('utf-8'))<=512 and item[1]=='body' and type(item[2]) is int and item[2]>=1 and type(item[3]) is str and re.fullmatch(r'[0-9a-f]{64}',item[3]) is not None
        if not part(root) or not part(assistant) or root[2]!=1 or root[0]==assistant[0] or root[:3]!=[row['message_id'],row['part_id'],row['revision']] or row['actor_id']!='main' or type(value['pair_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',value['pair_sha256']) is None or (value['workspace_id'] is not None and (type(value['workspace_id']) is not str or not value['workspace_id'] or len(value['workspace_id'].encode('utf-8'))>512)):
            raise ValueError('assistant component unavailable')
        return {'seq':row['outbox_seq'],'component':value}

    def assistant_component_profile_ready(self):
        if not self._origin_available():
            return False
        try:
            with self._lock, closing(self._connect()) as conn:
                self._require_assistant_component_indexes(conn)
            return True
        except Exception:
            return False

    def read_assistant_component_page(self, *, owner, chat_id, authorize, limit=32):
        """Read bounded reserved rows; association is not capture authority."""
        account=self.resolve_owner(owner)
        if not self._origin_available() or not callable(authorize) or authorize(account,chat_id) is not True or type(limit) is not int or not 1<=limit<=32:
            raise ValueError('assistant component page unavailable')
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute('BEGIN')
            self._require_assistant_component_indexes(conn)
            self._reject_erased_chat(conn,account,chat_id)
            row=conn.execute('SELECT last_outbox_seq FROM conversation_archive_cursor WHERE owner=? AND chat_id=? AND consumer=?',(account,chat_id,_ASSISTANT_COMPONENT_CONSUMER)).fetchone()
            cursor=int(row[0]) if row else 0
            rows=conn.execute("SELECT owner,chat_id,actor_id,message_id,part_id,revision,outbox_seq,CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END AS payload_json FROM conversation_archive_outbox INDEXED BY ix_conv_assistant_component_chat_seq WHERE owner=? AND chat_id=? AND event_kind='assistant_component.v1' AND state='assistant_component' AND outbox_seq>? ORDER BY outbox_seq LIMIT ?",(account,chat_id,cursor,limit+1)).fetchall()
            events=[self._assistant_component_descriptor(item) for item in rows[:limit]]
            conn.commit()
        if authorize(account,chat_id) is not True:
            raise ValueError('assistant component page unavailable')
        return {'cursor':cursor,'events':events,'has_more':len(rows)>limit}

    async def ack_staged_assistant_components(self, *, owner, chat_id, lifecycle_owner, guard,
                                              target_authority, assignment_authority, expected_cursor, entries):
        """Advance only a staged, exact ordered prefix under the original chat guard."""
        from src.memery_external_sources import assistant_interpretation_ref
        from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
        from src.openclank.memory_job_admission import lookup_staged_assistant_interpretation, lookup_terminal_assistant_interpretation, lookup_full_pair_terminal
        if (type(lifecycle_owner) is not CompletedTurnLifecycleOwner or owner != lifecycle_owner.account_id
            or self.resolve_owner(owner) != owner or type(expected_cursor) is not int or expected_cursor < 0
            or type(entries) is not list or not 1 <= len(entries) <= 32):
            raise ValueError('assistant acknowledgement unavailable')
        events=[]
        for entry in entries:
            if (type(entry) is not dict or entry.get('disposition') not in ('staged','terminal_erased','full_pair_terminal')
                or set(entry) != ({'event','source','disposition'} if entry.get('disposition')=='full_pair_terminal' else {'event','source','job_id','disposition'})):
                raise ValueError('assistant acknowledgement unavailable')
            event=entry['event']; source=entry['source']
            if (type(event) is not dict or set(event) != {'seq','component'} or type(event['seq']) is not int
                or event['seq'] <= expected_cursor or source.root.account_id != owner or source.root.chat_id != chat_id
                or assistant_interpretation_ref(source.root,event['component']) != source):
                raise ValueError('assistant acknowledgement unavailable')
            events.append(event)
        if any(events[i]['seq'] >= events[i+1]['seq'] for i in range(len(events)-1)):
            raise ValueError('assistant acknowledgement unavailable')
        async def verify():
            lifecycle_owner.mapping.validate_chat_guard(chat_id,guard)
            for entry in entries:
                if entry['disposition']=='full_pair_terminal':
                    fact=await lookup_full_pair_terminal(lifecycle_owner.profile.path,source=entry['source'],lifecycle_owner=lifecycle_owner,guard=guard,target_authority=target_authority)
                else:
                    lookup=lookup_staged_assistant_interpretation if entry['disposition']=='staged' else lookup_terminal_assistant_interpretation
                    fact=await lookup(lifecycle_owner.profile.path,source=entry['source'],lifecycle_owner=lifecycle_owner,guard=guard,target_authority=target_authority,assignment_authority=assignment_authority,expected_job_id=entry['job_id'])
                if (type(fact) is not dict or fact.get('state') != entry['disposition'] or fact.get('pair_sha256') != entry['source'].pair_sha256
                    or (entry['disposition']!='full_pair_terminal' and fact.get('job_id') != entry['job_id'])):
                    raise ValueError('assistant staged fact unavailable')
            lifecycle_owner.mapping.validate_chat_guard(chat_id,guard)
        await verify()
        end=events[-1]['seq']
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                lifecycle_owner.mapping.validate_chat_guard(chat_id,guard)
                self._require_assistant_component_indexes(conn)
                self._reject_erased_chat(conn,owner,chat_id)
                row=conn.execute('SELECT last_outbox_seq FROM conversation_archive_cursor WHERE owner=? AND chat_id=? AND consumer=?',(owner,chat_id,_ASSISTANT_COMPONENT_CONSUMER)).fetchone()
                current=int(row[0]) if row else 0
                rows=conn.execute("SELECT owner,chat_id,actor_id,message_id,part_id,revision,outbox_seq,CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END AS payload_json FROM conversation_archive_outbox INDEXED BY ix_conv_assistant_component_chat_seq WHERE owner=? AND chat_id=? AND event_kind='assistant_component.v1' AND state='assistant_component' AND outbox_seq>? AND outbox_seq<=? ORDER BY outbox_seq LIMIT 33",(owner,chat_id,expected_cursor,end)).fetchall()
                if current not in (expected_cursor,end) or [self._assistant_component_descriptor(row) for row in rows] != events:
                    raise ValueError('assistant acknowledgement changed')
                if current == expected_cursor:
                    conn.execute('INSERT INTO conversation_archive_cursor VALUES (?,?,?,?,?) ON CONFLICT(owner,chat_id,consumer) DO UPDATE SET last_outbox_seq=excluded.last_outbox_seq,updated_at=excluded.updated_at WHERE last_outbox_seq=?',(owner,chat_id,_ASSISTANT_COMPONENT_CONSUMER,end,_now_ms(),expected_cursor))
                lifecycle_owner.mapping.validate_chat_guard(chat_id,guard)
                conn.commit()
            except BaseException:
                conn.rollback(); raise
        # A failure here is an uncertain response, not a rollback of the committed cursor.
        await verify()
        return {'cursor':end,'replayed':current == end}

    def pending_assistant_component_page(self, *, owner, authorize, after_seq=0, limit=32):
        """Bounded account discovery, with each chat freshly authorized."""
        account=self.resolve_owner(owner)
        if not self._origin_available() or not callable(authorize) or type(after_seq) is not int or after_seq<0 or type(limit) is not int or not 1<=limit<=32:
            raise ValueError('assistant component discovery unavailable')
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute('BEGIN')
            self._require_assistant_component_indexes(conn)
            rows=conn.execute("SELECT chat_id,outbox_seq FROM conversation_archive_outbox INDEXED BY ix_conv_assistant_component_owner_seq WHERE owner=? AND event_kind='assistant_component.v1' AND state='assistant_component' AND outbox_seq>? ORDER BY outbox_seq LIMIT ?",(account,after_seq,limit+1)).fetchall()
            selected=rows[:limit]
            conn.commit()
        hints=[{'chat_id':row['chat_id'],'seq':row['outbox_seq']} for row in selected if authorize(account,row['chat_id']) is True]
        return {'hints':hints,'next_seq':selected[-1]['outbox_seq'] if selected else 0,'has_more':len(rows)>limit}

    def recorded_assistant_component(self, component: TrustedAssistantComponent, *, authorize):
        """Read the immutable association, including when its contributors later withdrew."""
        if type(component) is not TrustedAssistantComponent or not callable(authorize):
            raise ValueError('assistant component unavailable')
        owner=self.resolve_owner(component.owner)
        if owner!=component.owner or authorize(owner,component.chat_id) is not True:
            raise ValueError('assistant component unavailable')
        with self._lock, closing(self._connect()) as conn:
            self._require_assistant_component_indexes(conn)
            self._reject_erased_chat(conn,owner,component.chat_id)
            row=conn.execute("SELECT owner,chat_id,actor_id,message_id,part_id,revision,outbox_seq,CASE WHEN length(CAST(payload_json AS BLOB))<=8192 THEN payload_json END AS payload_json FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND actor_id='main' AND message_id=? AND part_id='body' AND revision=? AND event_kind='assistant_component.v1' AND state='assistant_component'",(owner,component.chat_id,component.root.message_id,component.root.revision)).fetchone()
            if not row:
                raise ValueError('assistant component unavailable')
            item=self._assistant_component_descriptor(row)['component']
            expected={'version':1,'kind':'host_assistant_body','owner':owner,'chat_id':component.chat_id,
                'root':[component.root.message_id,component.root.part_id,component.root.revision,component.root.content_hash],
                'assistant':[component.assistant.message_id,component.assistant.part_id,component.assistant.revision,component.assistant.content_hash],
                'pair_sha256':component.pair_sha256,'workspace_id':component.workspace_id}
            if item!=expected:
                raise ValueError('assistant component unavailable')
        if authorize(owner,component.chat_id) is not True:
            raise ValueError('assistant component unavailable')
        return component

    def verify_feedback_projection_drop(self, *, owner: str, chat_id: str, part: TrustedOriginPart,
                                        workspace_id: str, project_id: Optional[str],
                                        verify_host_drop: Callable[[str, str, str, TrustedOriginPart], bool]) -> bool:
        """Prove one committed presentation drop without returning source content."""
        if type(part) is not TrustedOriginPart or not callable(verify_host_drop):
            return False
        try:
            account = self.resolve_owner(owner)
            with self._lock, self._connect() as conn:
                self._reject_erased_chat(conn, account, chat_id)
                row = conn.execute("SELECT * FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id='main' AND message_id=? AND part_id=? ORDER BY revision DESC LIMIT 1", (account,chat_id,part.message_id,part.part_id)).fetchone()
                return bool(row and part.part_id == 'body' and row['revision'] == part.revision
                    and row['content_hash'] == part.content_hash and _content_hash(row['content']) == part.content_hash
                    and row['runtime_generation'] == '0' and row['role'] == 'user' and row['part_type'] == 'text'
                    and row['event_workspace'] == workspace_id and row['event_project'] == project_id
                    and row['tombstone'] and row['tombstone_reason'] == 'projection_drop'
                    and verify_host_drop(account,chat_id,'main',part) is True)
        except Exception:
            return False

    def hydrate_origin_part(self, *, owner: str, chat_id: str, actor_id: str,
                            part: TrustedOriginPart, authorize: Callable[[str, str], bool],
                            verify_host_source: Optional[Callable[[str, str, str, TrustedOriginPart], bool]] = None,
                            verify_projection_drop: Optional[Callable[[str, str, str, TrustedOriginPart], bool]] = None) -> SourcePart:
        """Hydrate an exact eligible source after fresh scoped authority checks.

        Callbacks are trusted internal authorities, not request/model metadata. A
        hosted verifier must check the current canonical host row; it cannot
        infer currentness from the archive alone.
        """
        if not self._origin_available():
            raise ArchiveUnavailableError(message="origin policy storage is inactive")
        if type(part) is not TrustedOriginPart or not callable(authorize):
            raise ValueError("origin source unavailable")
        account = self.resolve_owner(owner)
        with self._lock, self._connect() as conn:
            self._reject_erased_chat(conn, account, chat_id)
            row = conn.execute("SELECT * FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? ORDER BY revision DESC LIMIT 1", (account, chat_id, actor_id, part.message_id, part.part_id)).fetchone()
            if not row or row["revision"] != part.revision or row["content_hash"] != part.content_hash or _content_hash(row["content"]) != part.content_hash:
                raise ValueError("origin source unavailable")
            if row["tombstone"] and not (row["tombstone_reason"] == "projection_drop" and row["runtime_generation"] == "0" and row["actor_id"] == "main" and row["role"] == "user" and callable(verify_projection_drop) and verify_projection_drop(account, chat_id, actor_id, part) is True):
                raise ValueError("origin source unavailable")
            link = conn.execute("SELECT a.receipt_json AS admission, c.receipt_json AS completion FROM origin_part_link l JOIN origin_admission a USING(owner,chat_id,actor_id,admission_id) JOIN origin_completion c USING(owner,chat_id,actor_id,admission_id) WHERE l.owner=? AND l.chat_id=? AND l.actor_id=? AND l.message_id=? AND l.part_id=? AND l.revision=? AND l.content_hash=?", (account, chat_id, actor_id, part.message_id, part.part_id, part.revision, part.content_hash)).fetchone()
            if not link:
                raise ValueError("origin source unavailable")
            policy, completion = json.loads(link["admission"]), json.loads(link["completion"])
            if not (completion.get("outcome") == "completed" and policy.get("capture_eligible") is True and policy.get("incognito") is False and policy.get("no_memory") is False and policy.get("compare_mode") is False and policy.get("policy_source") in {"trusted_revision", "trusted_fingerprint"} and policy.get("policy_value")):
                raise ValueError("origin source unavailable")
            if authorize(account, chat_id) is not True:
                raise ValueError("origin source unavailable")
            if row["runtime_generation"] == "0" and row["actor_id"] == "main":
                if not callable(verify_host_source) or verify_host_source(account, chat_id, actor_id, part) is not True:
                    raise ValueError("host source verification unavailable")
            elif row["runtime_generation"] != "managed-mimo-v1":
                raise ValueError("source provenance unavailable")
            return SourcePart(owner=account, chat_id=chat_id, actor_id=actor_id, message_id=row["message_id"], part_id=row["part_id"], revision=row["revision"], runtime_generation=row["runtime_generation"], event_sequence=row["event_sequence"], event_workspace=row["event_workspace"], event_project=row["event_project"], role=row["role"], part_type=row["part_type"], content=row["content"], content_hash=row["content_hash"], time_created=row["time_created"], time_updated=row["time_updated"])

    def read_origin_evidence(self, *, owner: str, chat_id: str, actor_id: str,
                             part: TrustedOriginPart, authorize: Callable[[str, str], bool],
                             verify_host_source: Callable[[str, str, str, TrustedOriginPart], bool],
                             verify_projection_drop: Optional[Callable[[str, str, str, TrustedOriginPart], bool]] = None) -> TrustedOriginEvidence:
        """Read typed existing origin receipts only after exact current-source checks."""
        account = self.resolve_owner(owner)
        with self._lock:
            source = self.hydrate_origin_part(owner=account, chat_id=chat_id, actor_id=actor_id,
                part=part, authorize=authorize, verify_host_source=verify_host_source, verify_projection_drop=verify_projection_drop)
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT a.receipt_json AS admission,c.receipt_json AS completion FROM origin_part_link l JOIN origin_admission a USING(owner,chat_id,actor_id,admission_id) JOIN origin_completion c USING(owner,chat_id,actor_id,admission_id) WHERE l.owner=? AND l.chat_id=? AND l.actor_id=? AND l.message_id=? AND l.part_id=? AND l.revision=? AND l.content_hash=?", (account,chat_id,actor_id,part.message_id,part.part_id,part.revision,part.content_hash)).fetchone()
                if row is None:
                    raise ValueError("origin source unavailable")
                policy = json.loads(row['admission']); completion = json.loads(row['completion'])
                admission_fields = {field:policy.get(field) for field in TrustedOriginAdmission.__dataclass_fields__}
                admission = TrustedOriginAdmission(**admission_fields)
                raw_parts = completion.get('parts')
                if (type(raw_parts) is not list or not 1 <= len(raw_parts) <= 128 or any(
                    type(entry) is not list or len(entry) != 4 or type(entry[0]) is not str or not entry[0]
                    or type(entry[1]) is not str or not entry[1] or type(entry[2]) is not int or entry[2] < 1
                    or type(entry[3]) is not str or re.fullmatch(r"[0-9a-f]{64}", entry[3]) is None
                    for entry in raw_parts)):
                    raise ValueError("origin evidence identity changed")
                parts = tuple(TrustedOriginPart(*entry) for entry in raw_parts)
                if len(set(parts)) != len(parts):
                    raise ValueError("origin evidence identity changed")
                result = TrustedOriginCompletion(completion.get('admission_id'), completion.get('owner'), completion.get('chat_id'),
                    completion.get('actor_id'), completion.get('outcome'), parts)
                if (policy.get('version') != 1 or completion.get('version') != 1 or
                    (admission.owner,admission.chat_id,admission.actor_id,admission.admission_id) != (account,chat_id,actor_id,result.admission_id) or
                    (result.owner,result.chat_id,result.actor_id,result.outcome) != (account,chat_id,actor_id,'completed') or part not in parts):
                    raise ValueError("origin evidence identity changed")
            # Recheck the exact source after reading its existing receipt.
            again = self.hydrate_origin_part(owner=account, chat_id=chat_id, actor_id=actor_id,
                part=part, authorize=authorize, verify_host_source=verify_host_source, verify_projection_drop=verify_projection_drop)
            if (source.event_workspace,source.event_project) != (again.event_workspace,again.event_project):
                raise ValueError("origin source scope changed")
            return TrustedOriginEvidence(admission, result, source.event_workspace, source.event_project)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=0.05 if _ASSOCIATION_BUSY_BUDGET.get() else 30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    _OWNER_TABLES = (
        "conversation_parts", "conversation_part_aliases", "conversation_part_assets",
        "conversation_archive_outbox", "conversation_compaction_projections",
        "conversation_archive_cursor", "conversation_archive_migration_envelopes",
    )

    @property
    def _lifecycle_tables(self) -> tuple[str, ...]:
        # Child records must precede their foreign-key parent on erasure.
        return (("origin_part_link", "origin_completion", "origin_admission") if self._origin_schema_present else ()) + self._OWNER_TABLES

    def resolve_owner(self, owner: str) -> str:
        """Resolve a real current account; offline explicit stores accept IDs."""
        value = str(owner or "").strip()
        if not value:
            raise ValueError("archive owner is required")
        if self.owner_resolver is None:
            return value
        account_id = self.owner_resolver(value)
        if not account_id:
            raise ValueError("archive owner has no authenticated account identity")
        account_id = str(account_id)
        if value != account_id:
            with self._connect() as conn:
                if any(conn.execute(f"SELECT 1 FROM {table} WHERE owner=? LIMIT 1", (value,)).fetchone() for table in self._lifecycle_tables):
                    raise ArchiveUnavailableError(message="legacy archive ownership requires explicit account-ID conversion")
        return account_id

    def owner_inventory(self, owner: str) -> dict[str, Any]:
        try:
            account_id = self._lifecycle_owner(owner, bind=True)
        except ValueError:
            with self._connect() as conn:
                if any(conn.execute(f"SELECT 1 FROM {table} WHERE owner=? LIMIT 1", (owner,)).fetchone() for table in self._lifecycle_tables):
                    raise ArchiveUnavailableError(message="archive owner requires explicit identity conversion")
            return {"owner_account_id": None, "count": 0, "tables": {table: 0 for table in self._lifecycle_tables}}
        with self._lock, self._connect() as conn:
            counts = {table: int(conn.execute(
                f"SELECT count(*) FROM {table} WHERE owner=?", (account_id,)
            ).fetchone()[0]) for table in self._lifecycle_tables}
        return {"owner_account_id": account_id, "count": sum(counts.values()), "tables": counts}

    def _lifecycle_owner(self, owner: str, *, bind: bool = False) -> str:
        try:
            account_id = self.resolve_owner(owner)
        except ValueError:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT account_id FROM conversation_archive_owner_aliases WHERE owner_alias=?",
                    (owner,),
                ).fetchone()
            if row is None:
                raise
            account_id = str(row[0])
        if bind:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO conversation_archive_owner_aliases VALUES (?,?,?) "
                    "ON CONFLICT(owner_alias) DO UPDATE SET account_id=excluded.account_id, updated_at=excluded.updated_at",
                    (owner, account_id, _now_ms()),
                )
        return account_id

    def rename_owner(self, old_owner: str, new_owner: str) -> dict[str, Any]:
        """Rename lifecycle alias; immutable source keys never change."""
        try:
            account_id = self._lifecycle_owner(old_owner)
        except ValueError:
            # A prior rename may already have moved its content-free alias.
            account_id = self._lifecycle_owner(new_owner)
            return {"owner_account_id": account_id, "alias_renamed": False, "already_applied": True}
        try:
            target_id = self.resolve_owner(new_owner)
        except ValueError:
            target_id = None
        if target_id and target_id != account_id:
            raise ValueError("archive target owner belongs to another account")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT account_id FROM conversation_archive_owner_aliases WHERE owner_alias=?", (new_owner,)
                ).fetchone()
                if row and row[0] != account_id:
                    raise ValueError("archive owner alias conflicts")
                conn.execute("DELETE FROM conversation_archive_owner_aliases WHERE owner_alias=?", (old_owner,))
                conn.execute("INSERT OR REPLACE INTO conversation_archive_owner_aliases VALUES (?,?,?)",
                             (new_owner, account_id, _now_ms()))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"owner_account_id": account_id, "alias_renamed": True}

    def purge_owner(self, owner: str) -> dict[str, Any]:
        account_id = self._lifecycle_owner(owner)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                counts = {table: conn.execute(
                    f"DELETE FROM {table} WHERE owner=?", (account_id,)
                ).rowcount for table in self._lifecycle_tables}
                residual = sum(int(conn.execute(
                    f"SELECT count(*) FROM {table} WHERE owner=?", (account_id,)
                ).fetchone()[0]) for table in self._lifecycle_tables)
                if residual:
                    raise RuntimeError("archive owner purge has residual records")
                # Keep only the content-free tombstone mapping so retries can
                # verify the same immutable owner after credentials are gone.
                conn.execute("DELETE FROM conversation_archive_owner_aliases WHERE account_id=? AND owner_alias NOT LIKE 'deleted:%'", (account_id,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"owner_account_id": account_id, "erased": counts, "residual": 0}

    def begin_chat_erasure(self, *, owner: str, chat_id: str) -> None:
        """Persist content-free deletion intent before cross-store mutations."""
        account_id = self.resolve_owner(owner)
        if not chat_id:
            raise ValueError("archive chat id is required")
        with self._lock, self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO conversation_archive_deleted_chats VALUES (?,?,?)", (account_id, chat_id, _now_ms()))

    def chat_erasure_started(self, *, owner: str, chat_id: str) -> bool:
        account_id = self.resolve_owner(owner)
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM conversation_archive_deleted_chats WHERE owner=? AND chat_id=?", (account_id, chat_id)).fetchone() is not None

    def erase_chat(self, *, owner: str, chat_id: str) -> dict[str, Any]:
        account_id = self.resolve_owner(owner)
        if not chat_id:
            raise ValueError("archive chat id is required")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("INSERT OR IGNORE INTO conversation_archive_deleted_chats VALUES (?,?,?)", (account_id, chat_id, _now_ms()))
                counts = {table: conn.execute(
                    f"DELETE FROM {table} WHERE owner=? AND chat_id=?", (account_id, chat_id)
                ).rowcount for table in self._lifecycle_tables}
                residual = sum(int(conn.execute(
                    f"SELECT count(*) FROM {table} WHERE owner=? AND chat_id=?", (account_id, chat_id)
                ).fetchone()[0]) for table in self._lifecycle_tables)
                if residual:
                    raise RuntimeError("archive erasure has residual records")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"owner_account_id": account_id, "chat_id": chat_id, "erased": counts, "residual": 0}

    @staticmethod
    def _reject_erased_chat(conn, owner: str, chat_id: str) -> None:
        if conn.execute("SELECT 1 FROM conversation_archive_deleted_chats WHERE owner=? AND chat_id=?", (owner, chat_id)).fetchone():
            raise ValueError("conversation was deleted; retained history writes are closed")

    def chat_inventory(self, *, owner: str, chat_id: str) -> dict[str, Any]:
        account_id = self.resolve_owner(owner)
        with self._lock, self._connect() as conn:
            counts = {table: int(conn.execute(
                f"SELECT count(*) FROM {table} WHERE owner=? AND chat_id=?", (account_id, chat_id)
            ).fetchone()[0]) for table in self._lifecycle_tables}
            assets = [row[0] for row in conn.execute(
                "SELECT DISTINCT asset_id FROM conversation_part_assets WHERE owner=? AND chat_id=?",
                (account_id, chat_id),
            )]
        return {"owner_account_id": account_id, "count": sum(counts.values()), "tables": counts, "asset_ids": assets, "erasure_started": self.chat_erasure_started(owner=account_id, chat_id=chat_id)}

    # ------------------------------------------------------------------
    # Write path: archive + outbox in one immediate transaction
    # ------------------------------------------------------------------

    def append_parts(
        self,
        parts: Sequence[SourcePart | Mapping[str, Any]],
        *,
        consumer_hint: str = "archive",
    ) -> dict[str, Any]:
        """Idempotently upsert ordered source parts and enqueue outbox events.

        Dedupe key is ``(owner, chat_id, actor_id, message_id, part_id, revision)``.
        A repeat of the same key with the same content hash is a no-op (still
        ensures an outbox row exists). A repeat with a different content hash
        at the same revision is rejected — revisions must advance.
        """
        normalized = [self._coerce_part(p) for p in parts]
        if not normalized:
            return {"accepted": 0, "duplicate": 0, "enqueued": 0, "part_ids": []}

        accepted = 0
        duplicate = 0
        enqueued = 0
        part_ids: list[str] = []
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for part in normalized:
                    self._reject_erased_chat(conn, part.owner, part.chat_id)
                    existing = conn.execute(
                        """
                        SELECT seq, content_hash, tombstone FROM conversation_parts
                        WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=?
                          AND part_id=? AND revision=?
                        """,
                        part.dedupe_key,
                    ).fetchone()
                    now = part.time_updated or _now_ms()
                    if existing is not None:
                        if existing["content_hash"] == (part.content_hash or _content_hash(part.content)):
                            duplicate += 1
                            part_ids.append(part.part_id)
                            # Ensure outbox row exists even on duplicate delivery.
                            enqueued += self._enqueue_outbox(
                                conn,
                                part,
                                event_kind=EVENT_PART_TOMBSTONE if part.tombstone else EVENT_PART_UPSERT,
                                now=now,
                            )
                            continue
                        raise ValueError(
                            "conversation part revision "
                            f"{part.revision} already exists with different content "
                            f"for {part.owner}/{part.chat_id}/{part.message_id}/{part.part_id}"
                        )

                    content_hash = part.content_hash or _content_hash(part.content)
                    conn.execute(
                        """
                        INSERT INTO conversation_parts(
                            owner, chat_id, actor_id, message_id, part_id, revision,
                            runtime_generation, event_sequence, event_workspace,
                            event_project, role, part_type, content, content_hash,
                            time_created, time_updated, tombstone, tombstone_reason
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            part.owner,
                            part.chat_id,
                            part.actor_id,
                            part.message_id,
                            part.part_id,
                            part.revision,
                            part.runtime_generation,
                            part.event_sequence,
                            part.event_workspace,
                            part.event_project,
                            part.role,
                            part.part_type,
                            None if part.content is None else (
                                part.content if isinstance(part.content, str) else _canonical_json(part.content)
                            ),
                            content_hash,
                            part.time_created or now,
                            now,
                            1 if part.tombstone else 0,
                            part.tombstone_reason,
                        ),
                    )
                    accepted += 1
                    part_ids.append(part.part_id)
                    for alias_kind, alias_id in part.aliases:
                        if not alias_kind or not alias_id:
                            continue
                        conn.execute(
                            """
                            INSERT INTO conversation_part_aliases(
                                alias_kind, alias_id, owner, chat_id, message_id, part_id, revision
                            ) VALUES (?,?,?,?,?,?,?)
                            ON CONFLICT(alias_kind, alias_id, owner) DO UPDATE SET
                                chat_id=excluded.chat_id,
                                message_id=excluded.message_id,
                                part_id=excluded.part_id,
                                revision=excluded.revision
                            """,
                            (
                                str(alias_kind),
                                str(alias_id),
                                part.owner,
                                part.chat_id,
                                part.message_id,
                                part.part_id,
                                part.revision,
                            ),
                        )
                    for asset in part.assets:
                        self._upsert_asset(conn, part, asset, now=now)
                        enqueued += self._enqueue_outbox(
                            conn,
                            part,
                            event_kind=EVENT_ASSET_REF,
                            now=now,
                            payload_extra={"asset": dict(asset)},
                        )
                    enqueued += self._enqueue_outbox(
                        conn,
                        part,
                        event_kind=EVENT_PART_TOMBSTONE if part.tombstone else EVENT_PART_UPSERT,
                        now=now,
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {
            "accepted": accepted,
            "duplicate": duplicate,
            "enqueued": enqueued,
            "part_ids": part_ids,
            "consumer_hint": consumer_hint,
        }

    def append_managed_parts(
        self,
        parts: Sequence[SourcePart | Mapping[str, Any]],
        *,
        consumer_hint: str = "managed-history",
        _qualified_tool: Any = None,
    ) -> dict[str, Any]:
        """Host-assign revisions for managed engine events.

        The engine has no durable history writer, so its timestamp cannot be a
        revision authority.  A single immediate transaction assigns and writes
        each revision.  It reuses only the current canonical revision for an
        exact replay; any different payload advances it, including a reversion
        to content from an older revision.  This keeps concurrent live and
        backfill deliveries lossless while restart replays remain no-ops.
        """
        if _qualified_tool is not None:
            from src.openclank.tool_timing_proof import QualifiedToolTiming, QualifiedToolActivity
            if not isinstance(_qualified_tool, (QualifiedToolTiming, QualifiedToolActivity)) or len(parts) != 1:
                raise ValueError("qualified tool evidence requires one exact part")
            qualified_projection = _qualified_tool.projection()
        else:
            qualified_projection = None
        accepted = duplicate = enqueued = 0
        part_ids: list[str] = []
        source_ack = None
        def qualified_ack(part, revision, digest):
            if qualified_projection is None or qualified_projection.get('version') != 1 or part.tombstone or part.role != 'assistant' or part.part_type != 'tool' or type(part.content) is not str:
                return None
            try:
                proof = qualified_projection['evidence']
                payload = json.loads(part.content); state = payload['state']; meta = state['metadata']
                output = state['output']
                if (qualified_projection['account'] != part.owner or qualified_projection['chat'] != part.chat_id
                        or proof['actorID'] != part.actor_id or proof['messageID'] != part.message_id
                        or proof['partID'] != part.part_id or proof['contentSHA256'] != digest
                        or payload['type'] != 'tool' or payload['id'] != part.part_id
                        or payload['messageID'] != part.message_id or payload['sessionID'] != proof['sessionID']
                        or state['status'] != 'completed' or type(output) is not str or state.get('attachments')
                        or state.get('providerOutput') is not None or meta.get('truncated') is not False
                        or meta.get('outputPath') is not None): return None
                data = output.encode('utf-8', 'strict')
                if len(data) > 51200: return None
                return {'version': 1, 'accountID': part.owner, 'chatID': part.chat_id,
                        'actorID': part.actor_id, 'sessionID': proof['sessionID'],
                        'messageID': part.message_id, 'partID': part.part_id, 'revision': revision,
                        'contentHash': digest, 'outputSHA256': hashlib.sha256(data).hexdigest(), 'outputBytes': len(data)}
            except (AttributeError, KeyError, TypeError, ValueError, UnicodeError):
                return None
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                timing_column = "qualified_tool_json" in {r[1] for r in conn.execute("PRAGMA table_info(conversation_parts)")}
                for raw_part in parts:
                    part = self._coerce_part(raw_part)
                    self._reject_erased_chat(conn, part.owner, part.chat_id)
                    content_hash = part.content_hash or _content_hash(part.content)
                    latest = conn.execute(
                        """
                        SELECT revision, content_hash, tombstone, role, part_type FROM conversation_parts
                        WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=?
                        ORDER BY revision DESC LIMIT 1
                        """,
                        (part.owner, part.chat_id, part.actor_id, part.message_id, part.part_id),
                    ).fetchone()
                    now = part.time_updated or _now_ms()
                    if (
                        latest is not None
                        and latest["content_hash"] == content_hash
                        and not latest["tombstone"]
                        and latest["role"] == part.role
                        and latest["part_type"] == part.part_type
                    ):
                        if timing_column and qualified_projection is not None:
                            stored = conn.execute("SELECT qualified_tool_json FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=?", (part.owner,part.chat_id,part.actor_id,part.message_id,part.part_id,int(latest["revision"]))).fetchone()[0]
                            if stored is not None and stored != _canonical_json(qualified_projection):
                                raise ValueError("conflicting qualified timing replay")
                            if stored is not None:
                                source_ack = qualified_ack(part, int(latest['revision']), content_hash)
                        duplicate += 1
                        part_ids.append(part.part_id)
                        enqueued += self._enqueue_outbox(
                            conn,
                            replace(part, revision=int(latest["revision"]), content_hash=content_hash),
                            event_kind=EVENT_PART_TOMBSTONE if part.tombstone else EVENT_PART_UPSERT,
                            now=now,
                        )
                        continue

                    revision = int(latest["revision"]) + 1 if latest is not None else 1
                    assigned = replace(part, revision=revision, content_hash=content_hash)
                    conn.execute(
                        """
                        INSERT INTO conversation_parts(
                            owner, chat_id, actor_id, message_id, part_id, revision,
                            runtime_generation, event_sequence, event_workspace,
                            event_project, role, part_type, content, content_hash,
                            time_created, time_updated, tombstone, tombstone_reason
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            assigned.owner,
                            assigned.chat_id,
                            assigned.actor_id,
                            assigned.message_id,
                            assigned.part_id,
                            assigned.revision,
                            assigned.runtime_generation,
                            assigned.event_sequence,
                            assigned.event_workspace,
                            assigned.event_project,
                            assigned.role,
                            assigned.part_type,
                            None if assigned.content is None else (
                                assigned.content if isinstance(assigned.content, str) else _canonical_json(assigned.content)
                            ),
                            content_hash,
                            assigned.time_created or now,
                            now,
                            1 if assigned.tombstone else 0,
                            assigned.tombstone_reason,
                        ),
                    )
                    if timing_column and _qualified_tool is not None:
                        evidence = qualified_projection["evidence"]
                        if (qualified_projection["account"] != assigned.owner or qualified_projection["chat"] != assigned.chat_id
                                or evidence["actorID"] != assigned.actor_id or evidence["messageID"] != assigned.message_id
                                or evidence["partID"] != assigned.part_id or evidence["contentSHA256"] != content_hash):
                            raise ValueError("qualified timing does not match canonical part")
                        conn.execute("UPDATE conversation_parts SET qualified_tool_json=? WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision=?",
                                     (_canonical_json(qualified_projection), *assigned.dedupe_key))
                        source_ack = qualified_ack(assigned, revision, content_hash)
                    accepted += 1
                    part_ids.append(assigned.part_id)
                    for alias_kind, alias_id in assigned.aliases:
                        if not alias_kind or not alias_id:
                            continue
                        conn.execute(
                            """
                            INSERT INTO conversation_part_aliases(
                                alias_kind, alias_id, owner, chat_id, message_id, part_id, revision
                            ) VALUES (?,?,?,?,?,?,?)
                            ON CONFLICT(alias_kind, alias_id, owner) DO UPDATE SET
                                chat_id=excluded.chat_id,
                                message_id=excluded.message_id,
                                part_id=excluded.part_id,
                                revision=excluded.revision
                            """,
                            (
                                str(alias_kind),
                                str(alias_id),
                                assigned.owner,
                                assigned.chat_id,
                                assigned.message_id,
                                assigned.part_id,
                                assigned.revision,
                            ),
                        )
                    for asset in assigned.assets:
                        self._upsert_asset(conn, assigned, asset, now=now)
                        enqueued += self._enqueue_outbox(
                            conn,
                            assigned,
                            event_kind=EVENT_ASSET_REF,
                            now=now,
                            payload_extra={"asset": dict(asset)},
                        )
                    enqueued += self._enqueue_outbox(
                        conn,
                        assigned,
                        event_kind=EVENT_PART_TOMBSTONE if assigned.tombstone else EVENT_PART_UPSERT,
                        now=now,
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {
            "accepted": accepted,
            "duplicate": duplicate,
            "enqueued": enqueued,
            "part_ids": part_ids,
            "consumer_hint": consumer_hint,
            **({'source_ack': source_ack} if source_ack is not None else {}),
        }

    def record_migration_envelopes(
        self,
        *,
        owner: str,
        source_fingerprint: str,
        sessions: Sequence[Mapping[str, Any]],
        messages: Sequence[Mapping[str, Any]],
    ) -> dict[str, int]:
        """Durably account for source envelopes that have no canonical parts.

        Empty MiMo sessions/messages are execution records, but intentionally
        do not become synthetic history parts or outbox events.  Their source
        payload remains owner- and source-fingerprint-scoped so a retry can
        distinguish a durable duplicate from a skipped record.
        """
        owner = self.resolve_owner(owner)
        records: list[tuple[str, str, str, Mapping[str, Any], int]] = []
        for session in sessions:
            source_id = str(session.get("id") or "")
            if not source_id:
                raise ValueError("migration session envelope requires id")
            records.append(("session", source_id, source_id, session, int(session.get("time_created") or 0)))
        for message in messages:
            source_id = str(message.get("id") or "")
            chat_id = str(message.get("session_id") or "")
            if not source_id or not chat_id:
                raise ValueError("migration message envelope requires id and session_id")
            records.append(("message", source_id, chat_id, message, int(message.get("time_created") or 0)))

        accepted_sessions = duplicate_sessions = accepted_messages = duplicate_messages = 0
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for kind, source_id, chat_id, payload, time_created in records:
                    self._reject_erased_chat(conn, owner, chat_id)
                    encoded = _canonical_json(dict(payload))
                    existing = conn.execute(
                        """
                        SELECT payload_json FROM conversation_archive_migration_envelopes
                        WHERE owner=? AND source_fingerprint=? AND record_kind=? AND source_id=?
                        """,
                        (owner, source_fingerprint, kind, source_id),
                    ).fetchone()
                    if existing is not None:
                        if existing["payload_json"] != encoded:
                            raise ValueError("migration envelope identity already exists with different content")
                        if kind == "session":
                            duplicate_sessions += 1
                        else:
                            duplicate_messages += 1
                        continue
                    conn.execute(
                        """
                        INSERT INTO conversation_archive_migration_envelopes(
                            owner, source_fingerprint, record_kind, source_id, chat_id, payload_json, time_created
                        ) VALUES (?,?,?,?,?,?,?)
                        """,
                        (owner, source_fingerprint, kind, source_id, chat_id, encoded, time_created),
                    )
                    if kind == "session":
                        accepted_sessions += 1
                    else:
                        accepted_messages += 1
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {
            "accepted_sessions": accepted_sessions,
            "duplicate_sessions": duplicate_sessions,
            "accepted_messages": accepted_messages,
            "duplicate_messages": duplicate_messages,
        }

    def private_part_retirement_identity(self, *, owner: str, chat_id: str, actor_id: str,
                                         message_id: str, part_id: str, revision: int | None) -> dict[str, Any] | None:
        """Read only the exact archive identity needed by a held retirement guard."""
        # This private caller supplies the already authenticated account ID.
        if (not all(type(value) is str and value and len(value.encode('utf-8'))<=512
                    for value in (owner,chat_id,actor_id,message_id,part_id))
                or (revision is not None and (type(revision) is not int or revision<1))):
            raise ValueError('part retirement identity unavailable')
        query="""SELECT actor_id,message_id,part_id,revision,content_hash,role,part_type,tombstone
            FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=?"""
        params=[owner,chat_id,actor_id,message_id,part_id]
        if revision is not None:
            query+=' AND revision=?';params.append(revision)
        query+=' ORDER BY revision DESC LIMIT 1'
        from contextlib import closing
        from urllib.parse import quote
        uri='file:'+quote(os.path.abspath(self.db_path),safe='/')+'?mode=ro'
        with closing(sqlite3.connect(uri,uri=True,timeout=0,isolation_level=None)) as conn:
            conn.row_factory=sqlite3.Row
            row=conn.execute(query,params).fetchone()
        if row is None: return None
        return {key:row[key] for key in ('actor_id','message_id','part_id','revision','content_hash','role','part_type','tombstone')}

    def tombstone_part(
        self,
        *,
        owner: str,
        chat_id: str,
        actor_id: str = "main",
        message_id: str,
        part_id: str,
        revision: int,
        reason: Optional[str] = None,
        _expected_content_hash: Optional[str] = None,
        _expected_latest: bool = False,
        _expected_account: Optional[str] = None,
    ) -> dict[str, Any]:
        """Version a destructive change as a tombstone; never prune the source row."""
        owner = self.resolve_owner(owner)
        if _expected_account is not None and owner != _expected_account:
            raise ValueError('part retirement account changed')
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._reject_erased_chat(conn, owner, chat_id)
                row = conn.execute(
                    """
                    SELECT * FROM conversation_parts
                    WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=?
                      AND part_id=? AND revision=?
                    """,
                    (owner, chat_id, actor_id, message_id, part_id, revision),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "error": "not_found"}
                if _expected_content_hash is not None and row["content_hash"] != _expected_content_hash:
                    raise ValueError("part retirement preimage changed")
                if _expected_latest:
                    latest=conn.execute("""SELECT MAX(revision) FROM conversation_parts
                        WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=?""",
                        (owner,chat_id,actor_id,message_id,part_id)).fetchone()[0]
                    if latest != revision: raise ValueError("part retirement revision changed")
                if row["tombstone"]:
                    conn.execute("ROLLBACK")
                    return {"ok": True, "tombstoned": False, "duplicate": True}
                now = _now_ms()
                conn.execute(
                    """
                    UPDATE conversation_parts
                       SET tombstone=1, tombstone_reason=?, time_updated=?
                     WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=?
                       AND part_id=? AND revision=?
                    """,
                    (reason, now, owner, chat_id, actor_id, message_id, part_id, revision),
                )
                part = SourcePart(
                    owner=owner,
                    chat_id=chat_id,
                    actor_id=actor_id,
                    message_id=message_id,
                    part_id=part_id,
                    revision=revision,
                    content_hash=row["content_hash"],
                    tombstone=True,
                    tombstone_reason=reason,
                    time_updated=now,
                )
                self._enqueue_outbox(conn, part, event_kind=EVENT_PART_TOMBSTONE, now=now)
                conn.execute("COMMIT")
                return {"ok": True, "tombstoned": True}
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def record_compaction_projection(
        self,
        *,
        owner: str,
        chat_id: str,
        actor_id: str = "main",
        summary_id: str,
        projection_revision: int,
        summary_text: str,
        included_message_ids: Sequence[str],
        retained_message_ids: Sequence[str],
        trigger_kind: str,
        route_id: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record an active-context compaction projection. Source parts stay."""
        owner = self.resolve_owner(owner)
        now = _now_ms()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._reject_erased_chat(conn, owner, chat_id)
                conn.execute(
                    """
                    INSERT INTO conversation_compaction_projections(
                        owner, chat_id, actor_id, summary_id, projection_revision,
                        summary_text, included_message_ids, retained_message_ids,
                        trigger_kind, route_id, model_id, projection_state, time_created
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?)
                    ON CONFLICT(owner, chat_id, actor_id, summary_id, projection_revision)
                    DO UPDATE SET
                        summary_text=excluded.summary_text,
                        included_message_ids=excluded.included_message_ids,
                        retained_message_ids=excluded.retained_message_ids,
                        trigger_kind=excluded.trigger_kind,
                        route_id=excluded.route_id,
                        model_id=excluded.model_id
                    """,
                    (
                        owner,
                        chat_id,
                        actor_id,
                        summary_id,
                        projection_revision,
                        summary_text,
                        _canonical_json(list(included_message_ids)),
                        _canonical_json(list(retained_message_ids)),
                        trigger_kind,
                        route_id,
                        model_id,
                        now,
                    ),
                )
                part = SourcePart(
                    owner=owner,
                    chat_id=chat_id,
                    actor_id=actor_id,
                    message_id=summary_id,
                    part_id=f"proj:{summary_id}:{projection_revision}",
                    revision=projection_revision,
                    role="system",
                    part_type="compaction_projection",
                    content={
                        "summary_text": summary_text,
                        "included_message_ids": list(included_message_ids),
                        "retained_message_ids": list(retained_message_ids),
                        "trigger_kind": trigger_kind,
                        "route_id": route_id,
                        "model_id": model_id,
                    },
                    event_sequence=projection_revision,
                    time_created=now,
                    time_updated=now,
                )
                self._enqueue_outbox(
                    conn, part, event_kind=EVENT_COMPACTION_PROJECTION, now=now
                )
                conn.execute("COMMIT")
                return {"ok": True, "summary_id": summary_id, "projection_revision": projection_revision}
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _upsert_asset(
        self,
        conn: sqlite3.Connection,
        part: SourcePart,
        asset: Mapping[str, Any],
        *,
        now: int,
    ) -> None:
        asset_id = str(asset.get("asset_id") or asset.get("id") or "").strip()
        if not asset_id:
            return
        conn.execute(
            """
            INSERT INTO conversation_part_assets(
                owner, chat_id, message_id, part_id, revision, asset_id,
                content_hash, mime_type, byte_size, provenance, time_created, revoked
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(owner, chat_id, message_id, part_id, revision, asset_id) DO UPDATE SET
                content_hash=excluded.content_hash,
                mime_type=excluded.mime_type,
                byte_size=excluded.byte_size,
                provenance=excluded.provenance,
                revoked=excluded.revoked
            """,
            (
                part.owner,
                part.chat_id,
                part.message_id,
                part.part_id,
                part.revision,
                asset_id,
                asset.get("content_hash"),
                asset.get("mime_type") or asset.get("mime"),
                asset.get("byte_size"),
                _canonical_json(asset.get("provenance") or dict(asset)),
                now,
                1 if asset.get("revoked") else 0,
            ),
        )

    def _enqueue_outbox(
        self,
        conn: sqlite3.Connection,
        part: SourcePart,
        *,
        event_kind: str,
        now: int,
        payload_extra: Optional[Mapping[str, Any]] = None,
    ) -> int:
        payload = {
            "owner": part.owner,
            "chat_id": part.chat_id,
            "actor_id": part.actor_id,
            "message_id": part.message_id,
            "part_id": part.part_id,
            "revision": part.revision,
            "runtime_generation": part.runtime_generation,
            "event_sequence": part.event_sequence,
            "event_workspace": part.event_workspace,
            "event_project": part.event_project,
            "role": part.role,
            "part_type": part.part_type,
            "content_hash": part.content_hash or _content_hash(part.content),
            "tombstone": bool(part.tombstone),
            "tombstone_reason": part.tombstone_reason,
            "event_kind": event_kind,
        }
        if payload_extra:
            payload.update(payload_extra)
        try:
            conn.execute(
                """
                INSERT INTO conversation_archive_outbox(
                    owner, chat_id, actor_id, message_id, part_id, revision,
                    event_kind, payload_json, state, attempts, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,'pending',0,?,?)
                """,
                (
                    part.owner,
                    part.chat_id,
                    part.actor_id,
                    part.message_id,
                    part.part_id,
                    part.revision,
                    event_kind,
                    _canonical_json(payload),
                    now,
                    now,
                ),
            )
            return 1
        except sqlite3.IntegrityError:
            # Idempotent enqueue: same identity already queued.
            return 0

    # ------------------------------------------------------------------
    # Outbox consumers
    # ------------------------------------------------------------------

    def claim_outbox(
        self,
        *,
        owner: Optional[str] = None,
        chat_id: Optional[str] = None,
        limit: int = 50,
        consumer: str = "default",
    ) -> list[dict[str, Any]]:
        """Claim pending/reconciling events for delivery. Restart-safe."""
        owner = self.resolve_owner(owner) if owner is not None else None
        now = _now_ms()
        clauses = ["state IN ('pending','reconciling')", "event_kind NOT GLOB 'queued_*.v1'", "event_kind != 'assistant_component.v1'"]
        params: list[Any] = []
        if owner:
            clauses.append("owner=?")
            params.append(owner)
        if chat_id:
            clauses.append("chat_id=?")
            params.append(chat_id)
        params.append(int(limit))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    f"""
                    SELECT * FROM conversation_archive_outbox
                    WHERE {' AND '.join(clauses)}
                    ORDER BY outbox_seq ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
                claimed: list[dict[str, Any]] = []
                for row in rows:
                    conn.execute(
                        """
                        UPDATE conversation_archive_outbox
                           SET state='running', attempts=attempts+1, updated_at=?
                         WHERE outbox_seq=? AND state IN ('pending','reconciling')
                        """,
                        (now, row["outbox_seq"]),
                    )
                    claimed.append(self._outbox_dict(row, state=OUTBOX_RUNNING, attempts=row["attempts"] + 1))
                # Re-open in-flight rows older than 60s as reconciling (crash fence).
                cutoff = now - 60_000
                conn.execute(
                    """
                    UPDATE conversation_archive_outbox
                       SET state='reconciling', updated_at=?
                     WHERE state='running' AND event_kind NOT GLOB 'queued_*.v1' AND event_kind != 'assistant_component.v1' AND updated_at < ?
                    """,
                    (now, cutoff),
                )
                conn.execute("COMMIT")
                return claimed
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def mark_outbox(
        self,
        outbox_seq: int,
        *,
        state: str,
        error: Optional[str] = None,
    ) -> bool:
        if state not in {OUTBOX_DELIVERED, OUTBOX_FAILED, OUTBOX_RECONCILING, OUTBOX_PENDING}:
            raise ValueError(f"invalid outbox state {state!r}")
        now = _now_ms()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE conversation_archive_outbox
                   SET state=?, last_error=?, updated_at=?,
                       delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END
                 WHERE outbox_seq=? AND event_kind NOT GLOB 'maintenance_pending:*' AND event_kind NOT GLOB 'queued_*.v1' AND event_kind != 'assistant_component.v1'
                """,
                (state, error, now, state, now, outbox_seq),
            )
            return cur.rowcount > 0

    def outbox_pending_count(self, *, owner: Optional[str] = None, chat_id: Optional[str] = None) -> int:
        owner = self.resolve_owner(owner) if owner is not None else None
        clauses = ["state IN ('pending','running','reconciling')"]
        params: list[Any] = []
        if owner:
            clauses.append("owner=?")
            params.append(owner)
        if chat_id:
            clauses.append("chat_id=?")
            params.append(chat_id)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM conversation_archive_outbox WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
            return int(row["n"])

    def pending_for_part(self, dedupe_key: tuple[str, str, str, str, str, int]) -> int:
        owner, chat_id, actor_id, message_id, part_id, revision = dedupe_key
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM conversation_archive_outbox
                WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=?
                  AND part_id=? AND revision=? AND state IN ('pending','running','reconciling')
                """,
                (owner, chat_id, actor_id, message_id, part_id, revision),
            ).fetchone()
            return int(row["n"])

    @staticmethod
    def _completed_turn_descriptor(row):
        payload = json.loads(row['payload_json'])
        keys = ('owner','chat_id','actor_id','message_id','part_id','revision')
        if (type(payload) is not dict or not {'event_workspace', 'event_project'}.issubset(payload)
            or any(payload.get(key) != row[key] for key in keys) or payload.get('event_kind') != 'origin_ready'):
            raise ValueError('origin event identity unavailable')
        return {'seq':row['outbox_seq'], **{key:row[key] for key in keys},
                'content_hash':payload.get('content_hash'), 'workspace_id':payload.get('event_workspace'),
                'project_id':payload.get('event_project'), 'admission_id':payload.get('admission_id')}

    def _completed_turn_events(self, conn, owner, chat_id, after, through=None, limit=32, stop_on_block=False):
        sql = "SELECT * FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND event_kind='origin_ready' AND outbox_seq>?"
        args = [owner, chat_id, after]
        if through is not None:
            sql += ' AND outbox_seq<=?'; args.append(through)
        rows = conn.execute(sql + ' ORDER BY outbox_seq LIMIT ?', (*args, limit)).fetchall()
        events = []
        for row in rows:
            event = None
            try:
                event = self._completed_turn_descriptor(row)
                keys = ('owner','chat_id','actor_id','message_id','part_id','revision')
                current = conn.execute('SELECT revision,content_hash,tombstone,event_workspace,event_project FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? ORDER BY revision DESC LIMIT 1', tuple(row[key] for key in keys[:-1])).fetchone()
                if (not current or current['tombstone'] or current['revision'] != row['revision']
                    or current['content_hash'] != event['content_hash']
                    or current['event_workspace'] != event['workspace_id'] or current['event_project'] != event['project_id']):
                    raise ValueError('origin event source unavailable')
                events.append(event)
            except (ValueError, TypeError, KeyError):
                if not stop_on_block:
                    raise
                return events, {'seq':row['outbox_seq'], 'reason':'source_unavailable', 'event':event}
        return (events, None) if stop_on_block else events

    def pending_completed_turn_chats(self, *, owner, authorize, after_seq=0, limit=32):
        """Discover bounded non-content wake hints without claiming an outbox row."""
        if not self._origin_available():
            raise ArchiveUnavailableError(message="origin policy storage is inactive")
        account = self.resolve_owner(owner)
        if not callable(authorize) or type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError('origin discovery unavailable')
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute('BEGIN')
            rows = conn.execute("SELECT o.chat_id,MIN(o.outbox_seq) AS seq FROM conversation_archive_outbox o LEFT JOIN conversation_archive_cursor c ON c.owner=o.owner AND c.chat_id=o.chat_id AND c.consumer='completed_turn_review.v1' WHERE o.owner=? AND o.event_kind='origin_ready' AND o.outbox_seq>? AND o.outbox_seq>COALESCE(c.last_outbox_seq,0) GROUP BY o.chat_id ORDER BY seq LIMIT ?", (account,after_seq,limit)).fetchall()
            conn.commit()
        return {'hints':[{'chat_id':row['chat_id'],'seq':row['seq']} for row in rows if authorize(account,row['chat_id']) is True],
                'next_seq':rows[-1]['seq'] if rows else 0}

    def read_completed_turn_events(self, *, owner, chat_id, authorize, limit=32):
        """Read a bounded, non-content page for the fixed internal consumer."""
        if not self._origin_available():
            raise ArchiveUnavailableError(message="origin policy storage is inactive")
        account = self.resolve_owner(owner)
        if not callable(authorize) or authorize(account, chat_id) is not True or type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError('origin delivery unavailable')
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute('BEGIN')
            self._reject_erased_chat(conn, account, chat_id)
            row = conn.execute("SELECT last_outbox_seq FROM conversation_archive_cursor WHERE owner=? AND chat_id=? AND consumer='completed_turn_review.v1'", (account,chat_id)).fetchone()
            cursor = int(row[0]) if row else 0
            events, blocked = self._completed_turn_events(conn, account, chat_id, cursor, limit=limit, stop_on_block=True)
            conn.commit()
            return {'cursor':cursor, 'events':events, 'blocked':blocked}

    # ------------------------------------------------------------------
    # journal_delta.v1 (BQ-8, contract A): general conversation-parts page + CAS ACK. A distinct consumer row of the existing
    # cursor table; only `ack_journal_delta` moves it (the generic `advance_cursor` refuses it).
    # ------------------------------------------------------------------
    _JOURNAL_CONSUMER = 'journal_delta.v1'

    def _journal_cursor(self, conn, account, chat_id):
        row = conn.execute('SELECT last_outbox_seq FROM conversation_archive_cursor WHERE owner=? AND chat_id=? AND consumer=?',
                           (account, chat_id, self._JOURNAL_CONSUMER)).fetchone()
        return int(row[0]) if row else 0

    def _journal_delta_page(self, conn, account, chat_id, workspace_id, ack_seq, end_limit, limit, max_bytes, source_proof, dedupe):
        """One frozen window ``(ack_seq, high_seq]`` of main-actor rows, every row classified, in the caller's transaction.

        Never returns content. ``source_proof(row)`` is the Host custody verifier for a user body, ``dedupe(row)`` the exact-identity
        overlap check (contract B); both are supplied by the Host and re-run identically at ACK time. Assistant bodies are always
        omitted (no pair entries in this profile, H3). ``end_limit`` (ACK only) re-reads exactly up to that sequence.
        """
        from src.openclank.fm_journal_coverage import (ALREADY_COVERED, GAP, INCLUDED, INCLUDED_REASON, OMITTED, SUPERSEDED, page_digest)
        high = conn.execute('SELECT MAX(seq) FROM conversation_parts WHERE owner=? AND chat_id=?', (account, chat_id)).fetchone()[0]
        high = int(high or 0)
        page = {'account_id': account, 'chat_id': chat_id, 'workspace_id': workspace_id, 'ack_seq': ack_seq, 'high_seq': high,
                'end_seq': ack_seq, 'rows': []}
        if high > ack_seq:
            upper = high if end_limit is None else min(high, end_limit)
            raw = conn.execute("SELECT seq,actor_id,message_id,part_id,revision,content_hash,event_workspace,event_project,role,part_type,tombstone "
                               "FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id='main' AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                               (account, chat_id, ack_seq, upper, limit)).fetchall()
            used = 0
            for item in raw:
                row = {key: item[key] for key in ('seq', 'actor_id', 'message_id', 'part_id', 'revision', 'content_hash', 'role', 'part_type')}
                row['workspace_id'], row['project_id'] = item['event_workspace'], item['event_project']
                ident = {key: row[key] for key in ('seq', 'actor_id', 'message_id', 'part_id', 'revision')}
                row['class'], row['reason'], row['selector'], row['proof'], size = None, None, {}, None, 0
                later = conn.execute('SELECT revision,content_hash FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? '
                                     'AND part_id=? AND revision>? AND seq<=? ORDER BY revision DESC LIMIT 1',
                                     (account, chat_id, row['actor_id'], row['message_id'], row['part_id'], row['revision'], high)).fetchone()
                if item['tombstone']:
                    row['class'], row['reason'], row['selector'] = SUPERSEDED, 'tombstoned', {'tombstone': True}
                elif later is not None:
                    row['class'], row['reason'] = SUPERSEDED, 'later_revision'
                    row['selector'] = {'later_revision': int(later['revision']), 'later_content_hash': later['content_hash']}
                elif row['part_type'] != 'text':
                    row['class'], row['reason'], row['selector'] = OMITTED, 'tool_or_media_part', ident
                elif row['part_id'] != 'body':
                    row['class'], row['reason'], row['selector'] = OMITTED, 'unsupported_part', ident
                elif row['role'] not in ('user', 'assistant'):
                    row['class'], row['reason'], row['selector'] = OMITTED, 'untrusted_engine_part', ident
                elif row['role'] == 'assistant':
                    row['class'], row['reason'], row['selector'] = OMITTED, 'assistant_without_pair', ident
                else:
                    admitted = conn.execute("SELECT 1 FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND event_kind='origin_ready' "
                                            "AND actor_id=? AND message_id=? AND part_id=? AND revision=? LIMIT 1",
                                            (account, chat_id, row['actor_id'], row['message_id'], row['part_id'], row['revision'])).fetchone()
                    proof = source_proof(dict(row)) if admitted and callable(source_proof) else None
                    if not admitted or type(proof) is not dict:
                        row['class'], row['reason'], row['selector'] = GAP, 'proof_unavailable', ident
                    elif proof.get('state') != 'available':
                        reason = {'missing': 'source_missing', 'denied': 'source_denied'}.get(proof.get('state'), 'proof_unavailable')
                        row['class'], row['reason'], row['selector'] = GAP, reason, ident
                    else:
                        verdict = dedupe(dict(row)) if callable(dedupe) else None
                        size = proof.get('size') if type(proof.get('size')) is int else 0
                        if type(verdict) is dict and verdict.get('state') == 'pending':
                            row['class'], row['reason'], row['selector'] = GAP, 'overlap_pending', ident
                        elif type(verdict) is dict and verdict.get('state') == 'covered':
                            row['class'], row['reason'] = ALREADY_COVERED, 'already_covered'
                            row['selector'] = {'receipt_id': verdict['receipt_id'], 'job_id': verdict['job_id']}
                            row['proof'], size = {'source': proof['source'], 'authority': proof['authority']}, 0
                        elif size > max_bytes:
                            row['class'], row['reason'], row['selector'], size = GAP, 'oversize', ident, 0
                        else:
                            row['class'], row['reason'] = INCLUDED, INCLUDED_REASON
                            row['proof'] = {'source': proof['source'], 'authority': proof['authority'], 'size': size}
                if used + size > max_bytes and page['rows']:
                    break  # the row stays for the next page
                used += size
                page['rows'].append(row)
            if page['rows']:
                page['end_seq'] = page['rows'][-1]['seq']
        page['page_sha256'] = page_digest(account_id=account, chat_id=chat_id, workspace_id=workspace_id, ack_seq=ack_seq,
                                          high_seq=high, end_seq=page['end_seq'], rows=page['rows'])
        return page

    def read_journal_delta_page(self, *, owner, chat_id, workspace_id, authorize, source_proof=None, dedupe=None,
                                limit=32, max_bytes=16384):
        """Read one bounded, content-free journal page after the ``journal_delta.v1`` cursor (read-only: moves nothing).

        The caller holds the chat guard (selection time) and supplies the same ``source_proof``/``dedupe`` closures
        it will pass to :meth:`ack_journal_delta`. The page digest contains the internal account id: never return it to a browser.
        """
        if not self._origin_available():
            raise ArchiveUnavailableError(message="origin policy storage is inactive")
        account = self.resolve_owner(owner)
        if (not callable(authorize) or authorize(account, chat_id) is not True or type(limit) is not int or not 1 <= limit <= 32
                or type(max_bytes) is not int or not 1 <= max_bytes <= 16384 or type(workspace_id) is not str or not workspace_id):
            raise ValueError('journal delivery unavailable')
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute('BEGIN')
            self._reject_erased_chat(conn, account, chat_id)
            ack = self._journal_cursor(conn, account, chat_id)
            page = self._journal_delta_page(conn, account, chat_id, workspace_id, ack, None, limit, max_bytes, source_proof, dedupe)
            conn.commit()
            return page

    def ack_journal_delta(self, *, owner, chat_id, workspace_id, authorize, lifecycle_owner, guard, expected_cursor, page_digest, end_seq,
                          coverage, source_proof=None, dedupe=None, limit=32, max_bytes=16384):
        """CAS ``journal_delta.v1`` to ``end_seq`` iff the exact page still reads identically and every row is covered.

        ``coverage`` is an ordered list of :class:`JournalOutcomeProof` (minted only from the Native original-status read). Writes
        only this consumer's row, after re-reading the exact page under the guard. Closed refusals (no content):
        origin_unavailable authorization erased_chat guard cursor_changed page_changed gap_blocks_prefix uncovered_row
        proof_mismatch not_prefix. A replay returns ``{'cursor': end_seq, 'replayed': True}``.
        """
        from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
        from src.openclank.fm_journal_delta import JournalOutcomeProof
        from src.openclank.fm_journal_coverage import prefix_refusal
        def refuse(reason):
            raise ValueError(reason)
        if not self._origin_available():
            refuse('origin_unavailable')
        account = self.resolve_owner(owner)
        if (not callable(authorize) or authorize(account, chat_id) is not True or type(lifecycle_owner) is not CompletedTurnLifecycleOwner
                or lifecycle_owner.account_id != account or type(expected_cursor) is not int or expected_cursor < 0
                or type(end_seq) is not int or type(page_digest) is not str or type(coverage) is not list
                or any(type(proof) is not JournalOutcomeProof for proof in coverage) or type(workspace_id) is not str or not workspace_id):
            refuse('authorization')
        if end_seq <= expected_cursor:
            refuse('not_prefix')
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                try:
                    self._reject_erased_chat(conn, account, chat_id)
                except Exception:
                    refuse('erased_chat')
                try:
                    lifecycle_owner.mapping.validate_chat_guard(chat_id, guard)
                except Exception:
                    refuse('guard')
                current = self._journal_cursor(conn, account, chat_id)
                if current not in (expected_cursor, end_seq):
                    refuse('cursor_changed')
                if current == end_seq:
                    conn.commit()
                    return {'cursor': end_seq, 'replayed': True}
                page = self._journal_delta_page(conn, account, chat_id, workspace_id, expected_cursor, end_seq, limit, max_bytes,
                                                source_proof, dedupe)
                if page['end_seq'] != end_seq or page['page_sha256'] != page_digest:
                    refuse('page_changed')
                if any(proof.page_sha256 != page_digest or proof.prior_cursor != expected_cursor or proof.end_seq != end_seq
                       for proof in coverage):
                    refuse('proof_mismatch')
                why = prefix_refusal(page['rows'], coverage)
                if why:
                    refuse(why)
                conn.execute("INSERT INTO conversation_archive_cursor VALUES (?,?,?,?,?) ON CONFLICT(owner,chat_id,consumer) "
                             "DO UPDATE SET last_outbox_seq=excluded.last_outbox_seq,updated_at=excluded.updated_at",
                             (account, chat_id, self._JOURNAL_CONSUMER, end_seq, _now_ms()))
                conn.commit()
                return {'cursor': end_seq, 'replayed': False}
            except BaseException:
                conn.rollback()
                raise

    def ack_completed_turn_events(self, *, owner, chat_id, authorize, expected_cursor, events):
        """CAS only an exact, still-present ordered eligible prefix."""
        if not self._origin_available():
            raise ArchiveUnavailableError(message="origin policy storage is inactive")
        account = self.resolve_owner(owner)
        if (not callable(authorize) or authorize(account, chat_id) is not True or type(expected_cursor) is not int or expected_cursor < 0
            or type(events) is not list or not 1 <= len(events) <= 32 or any(type(e) is not dict or type(e.get('seq')) is not int for e in events)):
            raise ValueError('origin acknowledgement unavailable')
        end = events[-1]['seq']
        if end <= expected_cursor:
            raise ValueError('origin acknowledgement unavailable')
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                self._reject_erased_chat(conn, account, chat_id)
                row = conn.execute("SELECT last_outbox_seq FROM conversation_archive_cursor WHERE owner=? AND chat_id=? AND consumer='completed_turn_review.v1'", (account,chat_id)).fetchone()
                current = int(row[0]) if row else 0
                if current not in (expected_cursor, end) or self._completed_turn_events(conn, account, chat_id, expected_cursor, through=end, limit=33) != events:
                    raise ValueError('origin acknowledgement changed')
                if current == expected_cursor:
                    conn.execute("INSERT INTO conversation_archive_cursor VALUES (?,?, 'completed_turn_review.v1',?,?) ON CONFLICT(owner,chat_id,consumer) DO UPDATE SET last_outbox_seq=excluded.last_outbox_seq,updated_at=excluded.updated_at WHERE last_outbox_seq=?", (account,chat_id,end,_now_ms(),expected_cursor))
                conn.commit()
                return {'cursor':end, 'replayed':current == end}
            except BaseException:
                conn.rollback(); raise

    def ack_retired_completed_turn_event(self, *, owner, chat_id, lifecycle_owner, guard, expected_cursor, event):
        """Acknowledge one exact withdrawn prefix event under its live chat guard."""
        from src.memery_external_sources import SourceRef, SourceRetirementFact, _source_ref_error
        from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
        if (not self._origin_available()
            or type(lifecycle_owner) is not CompletedTurnLifecycleOwner or type(expected_cursor) is not int or expected_cursor < 0
            or type(event) is not dict or type(event.get('seq')) is not int or event['seq'] <= expected_cursor
            or owner != lifecycle_owner.account_id or chat_id != event.get('chat_id') or event.get('owner') != owner
            or event.get('admission_id') != event.get('message_id') or type(event.get('revision')) is not int
            or event['revision'] != 1 or event.get('actor_id') != 'main' or event.get('part_id') != 'body'):
            raise ValueError('retired origin acknowledgement unavailable')
        source = SourceRef(2 if event.get('workspace_id') is None else 1,'host_user_body',owner,chat_id,event.get('actor_id'),event.get('message_id'),
                           event.get('part_id'),event.get('revision'),event.get('content_hash'),
                           event.get('workspace_id'),event.get('project_id'))
        if _source_ref_error(source):
            raise ValueError('retired origin identity unavailable')
        proof = lifecycle_owner.retirement(source=source,guard=guard)
        fact = proof.get('fact') if type(proof) is dict and proof.get('state') == 'retired' else None
        if type(fact) is not SourceRetirementFact or fact.source != source:
            raise ValueError('retired origin authority unavailable')
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                lifecycle_owner.mapping.validate_chat_guard(chat_id,guard)
                cursor_row = conn.execute("SELECT last_outbox_seq FROM conversation_archive_cursor WHERE owner=? AND chat_id=? AND consumer='completed_turn_review.v1'",(owner,chat_id)).fetchone()
                current = int(cursor_row[0]) if cursor_row else 0
                row = conn.execute("SELECT * FROM conversation_archive_outbox WHERE owner=? AND chat_id=? AND event_kind='origin_ready' AND outbox_seq>? ORDER BY outbox_seq LIMIT 1",(owner,chat_id,expected_cursor)).fetchone()
                if current not in (expected_cursor,event['seq']) or row is None or self._completed_turn_descriptor(row) != event:
                    raise ValueError('retired origin acknowledgement changed')
                if current == expected_cursor:
                    conn.execute("INSERT INTO conversation_archive_cursor VALUES (?,?, 'completed_turn_review.v1',?,?) ON CONFLICT(owner,chat_id,consumer) DO UPDATE SET last_outbox_seq=excluded.last_outbox_seq,updated_at=excluded.updated_at WHERE last_outbox_seq=?",(owner,chat_id,event['seq'],_now_ms(),expected_cursor))
                lifecycle_owner.mapping.validate_chat_guard(chat_id,guard)
                conn.commit()
                return {'cursor':event['seq'], 'replayed':current == event['seq']}
            except BaseException:
                conn.rollback()
                raise

    def advance_cursor(self, *, owner: str, chat_id: str, consumer: str, last_outbox_seq: int) -> None:
        if consumer in ('completed_turn_review.v1', _ASSISTANT_COMPONENT_CONSUMER, 'journal_delta.v1'):
            raise ValueError('reserved cursor requires exact acknowledgement')
        owner = self.resolve_owner(owner)
        now = _now_ms()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_archive_cursor(owner, chat_id, consumer, last_outbox_seq, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(owner, chat_id, consumer) DO UPDATE SET
                    last_outbox_seq=excluded.last_outbox_seq,
                    updated_at=excluded.updated_at
                """,
                (owner, chat_id, consumer, int(last_outbox_seq), now),
            )

    def cursor(self, *, owner: str, chat_id: str, consumer: str) -> int:
        owner = self.resolve_owner(owner)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT last_outbox_seq FROM conversation_archive_cursor
                WHERE owner=? AND chat_id=? AND consumer=?
                """,
                (owner, chat_id, consumer),
            ).fetchone()
            return int(row["last_outbox_seq"]) if row else 0

    # ------------------------------------------------------------------
    # Read path: full-part archive / search / around / get / media
    # ------------------------------------------------------------------

    def resolve_alias(
        self,
        *,
        owner: str,
        alias_kind: str,
        alias_id: str,
    ) -> Optional[dict[str, Any]]:
        owner = self.resolve_owner(owner)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM conversation_part_aliases
                WHERE alias_kind=? AND alias_id=? AND owner=?
                """,
                (alias_kind, alias_id, owner),
            ).fetchone()
            return dict(row) if row else None

    def get_qualified_tool_output(self, *, account_id: str, chat_id: str, actor_id: str,
                                  message_id: str, part_id: str, revision: int,
                                  content_hash: str, offset: int, limit: int) -> dict[str, Any] | None:
        """Read a bounded exact output; caller separately authorizes the current actor."""
        if (not all(type(v) is str and v and len(v) <= 256 for v in
                    (account_id, chat_id, actor_id, message_id, part_id))
                or type(revision) is not int or revision < 1
                or type(content_hash) is not str or len(content_hash) != 64
                or any(c not in '0123456789abcdef' for c in content_hash)
                or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 8192):
            return None
        from contextlib import closing
        from urllib.parse import quote
        db_uri = 'file:' + quote(os.path.abspath(self.db_path), safe='/') + '?mode=ro'
        with closing(sqlite3.connect(db_uri, uri=True, timeout=0, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            if 'qualified_tool_json' not in {row[1] for row in conn.execute('PRAGMA table_info(conversation_parts)')}:
                return None
            row = conn.execute("""SELECT revision,content_hash,tombstone,role,part_type,
                    CASE WHEN length(CAST(content AS BLOB))<=262144 THEN content END AS content,
                    CASE WHEN length(CAST(qualified_tool_json AS BLOB))<=16384 THEN qualified_tool_json END AS proof
                FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=?
                ORDER BY revision DESC LIMIT 1""", (account_id, chat_id, actor_id, message_id, part_id)).fetchone()
        if (not row or row['revision'] != revision or row['content_hash'] != content_hash or row['tombstone']
                or row['role'] != 'assistant' or row['part_type'] != 'tool'
                or type(row['content']) is not str or type(row['proof']) is not str
                or _content_hash(row['content']) != content_hash):
            return None
        try:
            proof, part = json.loads(row['proof']), json.loads(row['content'])
            evidence = proof['evidence']
            state = part['state']
            if (proof['version'] != 1 or proof['account'] != account_id or proof['chat'] != chat_id
                    or evidence['actorID'] != actor_id or evidence['messageID'] != message_id
                    or evidence['partID'] != part_id or evidence['contentSHA256'] != content_hash
                    or part['type'] != 'tool' or part['id'] != part_id or part['messageID'] != message_id
                    or state['status'] != 'completed' or type(state['output']) is not str
                    or state.get('attachments') or state.get('providerOutput') is not None
                    or state.get('metadata', {}).get('truncated') is not False
                    or state.get('metadata', {}).get('outputPath') is not None):
                return None
            data = state['output'].encode('utf-8', 'strict')
            if len(data) > 51200 or offset > len(data):
                return None
            data[:offset].decode('utf-8', 'strict')
            end = min(len(data), offset + limit)
            while end > offset:
                try:
                    page = data[offset:end].decode('utf-8', 'strict')
                    break
                except UnicodeDecodeError:
                    end -= 1
            else:
                if end < len(data): return None
                page = ''
            return {'revision': revision, 'contentHash': content_hash,
                    'outputSHA256': hashlib.sha256(data).hexdigest(), 'outputBytes': len(data),
                    'offset': offset, 'nextOffset': end if end < len(data) else None, 'text': page}
        except (AttributeError, KeyError, TypeError, ValueError, UnicodeError):
            return None

    def list_qualified_tool_candidates(self, *, account_id: str, chat_id: str, since_ms: int) -> list[dict[str, Any]]:
        """Bounded identity-only candidates; each still needs exact proof verification."""
        if (type(account_id) is not str or not account_id or type(chat_id) is not str or not chat_id
                or type(since_ms) is not int or since_ms < 0): return []
        from contextlib import closing
        from urllib.parse import quote
        uri='file:'+quote(os.path.abspath(self.db_path),safe='/')+'?mode=ro'
        try:
            with closing(sqlite3.connect(uri,uri=True,timeout=0,isolation_level=None)) as conn:
                conn.row_factory=sqlite3.Row
                rows=conn.execute("""SELECT message_id,part_id,revision,content_hash FROM conversation_parts
                    WHERE owner=? AND chat_id=? AND actor_id='main' AND part_type='tool'
                    AND qualified_tool_json IS NOT NULL AND tombstone=0 AND time_created>=?
                    ORDER BY seq DESC LIMIT 128""",(account_id,chat_id,since_ms)).fetchall()
            return [dict(row) for row in reversed(rows)]
        except sqlite3.Error:
            return []

    def get_qualified_tool_pattern(self, *, account_id: str, chat_id: str, actor_id: str,
                                   message_id: str, part_id: str, revision: int, content_hash: str) -> dict[str, Any] | None:
        """Host-private exact attested part identity and value-free argument shape.

        This is a heuristic input to an already authorized source selection; it
        grants no source, session, model, or skill-publication authority. Raw
        arguments and tool output never leave this projection.
        """
        before = self.get_qualified_tool_output(account_id=account_id, chat_id=chat_id, actor_id=actor_id,
            message_id=message_id, part_id=part_id, revision=revision, content_hash=content_hash, offset=0, limit=8192)
        if before is None: return None
        from contextlib import closing
        from urllib.parse import quote
        db_uri='file:'+quote(os.path.abspath(self.db_path),safe='/')+'?mode=ro'
        try:
            with closing(sqlite3.connect(db_uri,uri=True,timeout=0,isolation_level=None)) as conn:
                conn.row_factory=sqlite3.Row
                row=conn.execute("""SELECT revision,content_hash,tombstone,seq,event_sequence,runtime_generation,
                    CASE WHEN length(CAST(content AS BLOB))<=262144 THEN content END AS content,
                    CASE WHEN length(CAST(qualified_tool_json AS BLOB))<=16384 THEN qualified_tool_json END AS proof
                    FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=?
                    ORDER BY revision DESC LIMIT 1""",(account_id,chat_id,actor_id,message_id,part_id)).fetchone()
            if (not row or row['revision']!=revision or row['content_hash']!=content_hash or row['tombstone'] or
                type(row['content']) is not str or type(row['proof']) is not str or _content_hash(row['content'])!=content_hash): return None
            part=json.loads(row['content']); proof=json.loads(row['proof']); evidence=proof['evidence']
            tool=part['tool']; args=part['state']['input']
            if (proof.get('version')!=1 or proof.get('account')!=account_id or proof.get('chat')!=chat_id or
                evidence.get('actorID')!=actor_id or evidence.get('messageID')!=message_id or evidence.get('partID')!=part_id or
                evidence.get('contentSHA256')!=content_hash or part.get('type')!='tool' or part.get('callID')!=evidence.get('callID') or
                type(tool) is not str or re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',tool) is None or type(args) is not dict or len(args)>32): return None
            if any(type(evidence.get(k)) is not str or not evidence[k] or len(evidence[k].encode('utf-8'))>256
                   for k in ('sessionID','rootOperationID','innerTurnID','callID')): return None
            execution=evidence.get('executionRevision')
            if type(execution) is not int or not 0<=execution<=9007199254740991: return None
            safe_names={'action','args','command','directory','file_path','flags','input','limit','mode','offset','operation','path','pattern','query','recursive','text','url'}
            def kind(value):
                if value is None: return 'null'
                if type(value) is bool: return 'boolean'
                if type(value) in (int,float): return 'number'
                if type(value) is str: return 'string'
                if type(value) is list: return 'array'
                if type(value) is dict: return 'object'
                raise ValueError('unsupported argument')
            shape=sorted([(key if key in safe_names else '<other>',kind(value)) for key,value in args.items()])
            fingerprint=hashlib.sha256(json.dumps(shape,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
            after=self.get_qualified_tool_output(account_id=account_id,chat_id=chat_id,actor_id=actor_id,
                message_id=message_id,part_id=part_id,revision=revision,content_hash=content_hash,offset=0,limit=8192)
            if after is None or after['outputSHA256']!=before['outputSHA256'] or after['outputBytes']!=before['outputBytes']: return None
            return {'version':1,'chatID':chat_id,'sessionID':evidence['sessionID'],'rootOperationID':evidence['rootOperationID'],
                'actorID':actor_id,'executionRevision':execution,'innerTurnID':evidence['innerTurnID'],
                'messageID':message_id,'partID':part_id,'revision':revision,'contentHash':content_hash,
                'toolName':tool,'argumentShape':shape,'argumentShapeSHA256':fingerprint,'wallEndMS':evidence.get('wallEndMS'),
                'startUS':evidence.get('startUS'),'endUS':evidence.get('endUS'),'clockID':evidence.get('clockID'),
                'eventSequence':row['event_sequence'],'archiveSequence':row['seq'],'runtimeGeneration':row['runtime_generation']}
        except (KeyError,TypeError,ValueError,UnicodeError,sqlite3.Error):
            return None

    def get_part(
        self,
        *,
        owner: str,
        chat_id: str,
        actor_id: str = "main",
        message_id: str,
        part_id: str,
        revision: Optional[int] = None,
        length: Optional[int] = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Full-part get. Reads the canonical part, never an FTS preview."""
        owner = self.resolve_owner(owner)
        params: list[Any] = [owner, chat_id, actor_id, message_id, part_id]
        sql = """
            SELECT * FROM conversation_parts
            WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=?
        """
        if revision is not None:
            sql += " AND revision=?"
            params.append(revision)
        sql += " ORDER BY revision DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                return {"ok": False, "error": "not_found"}
            # Cross-owner / guessed-id rejection is implicit: owner+chat_id+ids
            # must all match. A guessed part id from another owner misses.
            content = row["content"] or ""
            if not isinstance(content, str):
                content = _canonical_json(content)
            # Row content is already a JSON string for non-text; surface text for
            # text-like parts and the raw body otherwise.
            body = content
            if row["part_type"] in {"text", "reasoning"}:
                parsed = _json_loads(content, content)
                if isinstance(parsed, str):
                    body = parsed
                elif isinstance(parsed, Mapping) and isinstance(parsed.get("text"), str):
                    body = parsed["text"]
            try:
                requested_offset = max(0, int(offset))
            except (TypeError, ValueError):
                requested_offset = 0
            body, actual_offset = _slice_at_utf16_offset(body, requested_offset)
            visible, consumed, has_more = _clamp_get_text(body, length=length)
            assets = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT asset_id, content_hash, mime_type, byte_size, provenance, revoked
                    FROM conversation_part_assets
                    WHERE owner=? AND chat_id=? AND message_id=? AND part_id=? AND revision=?
                    """,
                    (owner, chat_id, message_id, part_id, row["revision"]),
                ).fetchall()
            ]
            return {
                "ok": True,
                "part": {
                    "owner": row["owner"],
                    "chat_id": row["chat_id"],
                    "actor_id": row["actor_id"],
                    "message_id": row["message_id"],
                    "part_id": row["part_id"],
                    "revision": row["revision"],
                    "runtime_generation": row["runtime_generation"],
                    "event_sequence": row["event_sequence"],
                    "event_workspace": row["event_workspace"],
                    "event_project": row["event_project"],
                    "role": row["role"],
                    "part_type": row["part_type"],
                    "text": visible,
                    "content_hash": row["content_hash"],
                    "time_created": row["time_created"],
                    "time_updated": row["time_updated"],
                    "tombstone": bool(row["tombstone"]),
                    "tombstone_reason": row["tombstone_reason"],
                    "assets": assets,
                },
                "next_offset": (actual_offset + consumed) if has_more else None,
                "has_more": has_more,
            }

    def get_message_parts(
        self,
        *,
        owner: str,
        chat_id: str,
        actor_id: str = "main",
        message_id: str,
        include_tombstones: bool = True,
    ) -> dict[str, Any]:
        owner = self.resolve_owner(owner)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM conversation_parts
                WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=?
                  AND (?=1 OR tombstone=0)
                ORDER BY event_sequence ASC, part_id ASC, revision ASC
                """,
                (owner, chat_id, actor_id, message_id, 1 if include_tombstones else 0),
            ).fetchall()
            if not rows:
                return {"ok": False, "error": "not_found"}
            return {
                "ok": True,
                "message_id": message_id,
                "parts": [self._part_dict(r) for r in rows],
            }

    def search(
        self,
        *,
        owner: str,
        query: str,
        chat_id: Optional[str] = None,
        scope: str = "chat",
        actor_id: Optional[str] = None,
        part_types: Optional[Sequence[str]] = None,
        kinds: Optional[Sequence[str]] = None,
        tool_name: Optional[str] = None,
        time_after: Optional[int] = None,
        time_before: Optional[int] = None,
        limit: int = 10,
        include_snippets: bool = True,
    ) -> dict[str, Any]:
        """Scoped archive search.

        ``scope='global'`` is same-owner scope only — never cross-account.
        Guessed ids from another owner fail closed (empty result).
        """
        owner = self.resolve_owner(owner)
        limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
        # Owner is mandatory. chat_id is optional only under explicit global.
        if not owner:
            return {"ok": False, "error": "owner_required", "hits": []}
        clauses = ["owner=?", "tombstone=0"]
        params: list[Any] = [owner]
        if scope == "global":
            if chat_id:
                clauses.append("chat_id=?")
                params.append(chat_id)
        else:
            if not chat_id:
                return {"ok": False, "error": "chat_required", "hits": []}
            clauses.append("chat_id=?")
            params.append(chat_id)
        if actor_id:
            clauses.append("actor_id=?")
            params.append(actor_id)
        if part_types:
            marks = ",".join("?" for _ in part_types)
            clauses.append(f"part_type IN ({marks})")
            params.extend(part_types)
        if time_after is not None:
            clauses.append("time_created >= ?")
            params.append(int(time_after))
        if time_before is not None:
            clauses.append("time_created <= ?")
            params.append(int(time_before))

        needle = (query or "").strip()
        if needle:
            # Escaped LIKE over canonical text; FTS is a rebuildable derived index
            # and is not required for correctness of this shared surface.
            like = f"%{needle.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
            clauses.append("(content LIKE ? ESCAPE '\\' OR part_type LIKE ? ESCAPE '\\')")
            params.extend([like, like])

        source_text = """
            CASE WHEN content IS NULL THEN ''
                 WHEN json_valid(content) THEN
                     CASE WHEN json_type(content)='object'
                                   AND json_type(content, '$.text')='text'
                              THEN json_extract(content, '$.text')
                          WHEN json_type(content)='text' THEN json_extract(content, '$')
                          ELSE content
                     END
                 ELSE content
            END
        """
        # The semantic fields feed both filter predicates and host projection.
        # Live MiMo parts keep their payload at the top level; migrated rows
        # retain it under mimo.part.  Keep this in SQLite so filtering happens
        # before LIMIT and cannot disagree with a later Python projection.
        tool_name_expr = """
            CASE WHEN json_valid(content) THEN
                COALESCE(
                    json_extract(content, '$.mimo.part.tool'),
                    json_extract(content, '$.tool'),
                    CASE WHEN json_type(content)='object'
                              AND json_type(content, '$.tool_name')='text'
                         THEN json_extract(content, '$.tool_name')
                    END
                )
            END
        """
        tool_status_expr = """
            CASE WHEN part_type='tool' AND json_valid(content) THEN
                COALESCE(
                    json_extract(content, '$.mimo.part.state.status'),
                    json_extract(content, '$.state.status')
                )
            END
        """
        history_kind_expr = f"""
            CASE
                WHEN part_type='text' AND role='user' THEN 'user_text'
                WHEN part_type='text' THEN 'assistant_text'
                WHEN part_type='reasoning' THEN 'reasoning'
                WHEN part_type='tool' AND ({tool_status_expr})='error' THEN 'tool_error'
                WHEN part_type='tool' AND ({tool_status_expr})='completed' THEN 'tool_output'
                WHEN part_type='tool' THEN 'tool_input'
                ELSE 'assistant_text'
            END
        """
        semantic_clauses: list[str] = []
        if kinds:
            marks = ",".join("?" for _ in kinds)
            semantic_clauses.append(f"history_kind IN ({marks})")
            params.extend(kinds)
        if tool_name:
            semantic_clauses.append("history_tool_name=?")
            params.append(tool_name)
        params.append(limit)
        base_columns = """
            seq, owner, chat_id, actor_id, message_id, part_id, revision,
            runtime_generation, event_sequence, event_workspace, event_project,
            role, part_type, content_hash, time_created, time_updated,
            tombstone, tombstone_reason
        """
        projection_columns = base_columns + ", history_kind, history_tool_name"
        filtered_columns = base_columns + ", source_text, history_kind, history_tool_name"
        if include_snippets:
            projection_columns += f""",
            CASE WHEN length(CAST(source_text AS BLOB)) > 4000
                 THEN '{_LARGE_INDEX_FIELD_MARKER}'
                 ELSE source_text
            END AS index_preview,
            CASE WHEN length(CAST(source_text AS BLOB)) > 4000 THEN 1 ELSE 0 END
                AS index_preview_large_omitted
            """
        sql = f"""
            WITH scoped_parts AS (
                SELECT {base_columns}, {source_text} AS source_text,
                       {history_kind_expr} AS history_kind,
                       {tool_name_expr} AS history_tool_name
                FROM conversation_parts
                WHERE {' AND '.join(clauses)}
            ), filtered_parts AS (
                SELECT {filtered_columns} FROM scoped_parts
                {'WHERE ' + ' AND '.join(semantic_clauses) if semantic_clauses else ''}
            )
            SELECT {projection_columns} FROM filtered_parts
            ORDER BY time_created DESC, seq DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            hits = []
            for row in rows:
                item = self._search_part_dict(row)
                if not include_snippets:
                    item.pop("snippet", None)
                hits.append(item)
            return {
                "ok": True,
                "hits": hits,
                "limit": limit,
                "more": len(hits) == limit,
            }

    def around(
        self,
        *,
        owner: str,
        chat_id: str,
        anchor_message_id: str,
        before: int = 5,
        after: int = 5,
        actor_id: str = "main",
    ) -> dict[str, Any]:
        """Neighboring message context around an authenticated anchor.

        Guessed message/part ids cannot bypass scope: the anchor must resolve
        under the caller's ``owner`` and ``chat_id``.
        """
        owner = self.resolve_owner(owner)
        before = max(0, min(int(before), AROUND_BEFORE_MAX))
        after = max(0, min(int(after), AROUND_AFTER_MAX))
        with self._connect() as conn:
            anchor = conn.execute(
                """
                SELECT MIN(seq) AS seq FROM conversation_parts
                WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND tombstone=0
                """,
                (owner, chat_id, actor_id, anchor_message_id),
            ).fetchone()
            if not anchor or anchor["seq"] is None:
                return {"ok": False, "error": "anchor_not_found", "messages": []}
            anchor_seq = int(anchor["seq"])
            before_rows = conn.execute(
                """
                SELECT message_id, MIN(seq) AS first_seq FROM conversation_parts
                WHERE owner=? AND chat_id=? AND actor_id=? AND seq < ? AND tombstone=0
                GROUP BY message_id
                ORDER BY first_seq DESC
                LIMIT ?
                """,
                (owner, chat_id, actor_id, anchor_seq, before),
            ).fetchall()
            after_rows = conn.execute(
                """
                SELECT message_id, MIN(seq) AS first_seq FROM conversation_parts
                WHERE owner=? AND chat_id=? AND actor_id=? AND seq >= ? AND tombstone=0
                GROUP BY message_id
                ORDER BY first_seq ASC
                LIMIT ?
                """,
                (owner, chat_id, actor_id, anchor_seq, after + 1),
            ).fetchall()
            message_ids = [r["message_id"] for r in reversed(before_rows)]
            for row in after_rows:
                if row["message_id"] not in message_ids:
                    message_ids.append(row["message_id"])
            messages = []
            for mid in message_ids:
                parts = conn.execute(
                    """
                    SELECT * FROM conversation_parts
                    WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND tombstone=0
                    ORDER BY event_sequence ASC, part_id ASC
                    """,
                    (owner, chat_id, actor_id, mid),
                ).fetchall()
                messages.append({
                    "message_id": mid,
                    "matched": mid == anchor_message_id,
                    "time_created": parts[0]["time_created"] if parts else 0,
                    "parts": [self._part_dict(p) for p in parts],
                })
            return {
                "ok": True,
                "chat_id": chat_id,
                "anchor": anchor_message_id,
                "messages": messages,
                "before": before,
                "after": after,
            }

    def get_media(
        self,
        *,
        owner: str,
        asset_id: str,
        chat_id: Optional[str] = None,
        message_id: Optional[str] = None,
        part_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Explicit single-attachment retrieval.

        Returns owned asset metadata + provenance. Historical ``file://``
        locations are references, not permission to read today's file. Never
        dereferences network URLs. Missing/revoked assets are explicit
        recoverable results.
        """
        owner = self.resolve_owner(owner)
        if not owner or not asset_id:
            return {"ok": False, "error": "owner_and_asset_required"}
        clauses = ["owner=?", "asset_id=?"]
        params: list[Any] = [owner, asset_id]
        if chat_id:
            clauses.append("chat_id=?")
            params.append(chat_id)
        if message_id:
            clauses.append("message_id=?")
            params.append(message_id)
        if part_id:
            clauses.append("part_id=?")
            params.append(part_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM conversation_part_assets
                WHERE {' AND '.join(clauses)}
                ORDER BY time_created DESC
                LIMIT 20
                """,
                params,
            ).fetchall()
            if not rows:
                return {
                    "ok": False,
                    "error": "asset_not_found",
                    "recoverable": True,
                    "message": "asset is missing or not visible to this owner",
                }
            live = [r for r in rows if not r["revoked"]]
            if not live:
                return {
                    "ok": False,
                    "error": "asset_revoked",
                    "recoverable": True,
                    "message": "asset was revoked; original bytes are not served",
                }
            row = live[0]
            provenance = _json_loads(row["provenance"], {})
            # Refuse automatic dereference of historical file:// or network URLs.
            locator = None
            if isinstance(provenance, Mapping):
                candidate = provenance.get("locator") or provenance.get("path") or provenance.get("url")
                if isinstance(candidate, str) and not candidate.startswith(("file://", "http://", "https://")):
                    locator = candidate
            return {
                "ok": True,
                "asset": {
                    "asset_id": row["asset_id"],
                    "content_hash": row["content_hash"],
                    "mime_type": row["mime_type"],
                    "byte_size": row["byte_size"],
                    "provenance": provenance,
                    "locator": locator,
                    "chat_id": row["chat_id"],
                    "message_id": row["message_id"],
                    "part_id": row["part_id"],
                    "revision": row["revision"],
                },
                "warning": (
                    "historical file:// and network URLs are references only; "
                    "use an ordinary authorized file read to fetch current bytes"
                ),
            }

    def list_compaction_projections(
        self,
        *,
        owner: str,
        chat_id: str,
        actor_id: str = "main",
    ) -> list[dict[str, Any]]:
        owner = self.resolve_owner(owner)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM conversation_compaction_projections
                WHERE owner=? AND chat_id=? AND actor_id=?
                ORDER BY projection_revision ASC
                """,
                (owner, chat_id, actor_id),
            ).fetchall()
            out = []
            for row in rows:
                out.append({
                    "summary_id": row["summary_id"],
                    "projection_revision": row["projection_revision"],
                    "summary_text": row["summary_text"],
                    "included_message_ids": _json_loads(row["included_message_ids"], []),
                    "retained_message_ids": _json_loads(row["retained_message_ids"], []),
                    "trigger_kind": row["trigger_kind"],
                    "route_id": row["route_id"],
                    "model_id": row["model_id"],
                    "projection_state": row["projection_state"],
                    "time_created": row["time_created"],
                })
            return out

    def count_parts(self, *, owner: str, chat_id: str) -> dict[str, int]:
        owner = self.resolve_owner(owner)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN tombstone=1 THEN 1 ELSE 0 END) AS tombstoned
                FROM conversation_parts
                WHERE owner=? AND chat_id=?
                """,
                (owner, chat_id),
            ).fetchone()
            return {
                "total": int(row["total"] or 0),
                "tombstoned": int(row["tombstoned"] or 0),
                "live": int(row["total"] or 0) - int(row["tombstoned"] or 0),
            }

    # ------------------------------------------------------------------
    # Coercion helpers
    # ------------------------------------------------------------------

    def _coerce_part(self, part: SourcePart | Mapping[str, Any]) -> SourcePart:
        if isinstance(part, SourcePart):
            return replace(part, owner=self.resolve_owner(part.owner))
        data = dict(part)
        aliases = data.pop("aliases", ()) or ()
        assets = data.pop("assets", ()) or ()
        normalized_aliases = []
        for item in aliases:
            if isinstance(item, Mapping):
                normalized_aliases.append((str(item.get("kind") or item.get("alias_kind")), str(item.get("id") or item.get("alias_id"))))
            else:
                kind, alias = item
                normalized_aliases.append((str(kind), str(alias)))
        return SourcePart(
            owner=self.resolve_owner(str(data["owner"])),
            chat_id=str(data["chat_id"]),
            actor_id=str(data.get("actor_id") or "main"),
            message_id=str(data["message_id"]),
            part_id=str(data["part_id"]),
            revision=int(data.get("revision") or 1),
            runtime_generation=str(data.get("runtime_generation") or "0"),
            event_sequence=int(data.get("event_sequence") or 0),
            event_workspace=data.get("event_workspace"),
            event_project=data.get("event_project"),
            role=str(data.get("role") or "user"),
            part_type=str(data.get("part_type") or "text"),
            content=data.get("content"),
            content_hash=data.get("content_hash"),
            time_created=int(data.get("time_created") or 0),
            time_updated=int(data.get("time_updated") or 0),
            tombstone=bool(data.get("tombstone")),
            tombstone_reason=data.get("tombstone_reason"),
            aliases=tuple(normalized_aliases),
            assets=tuple(dict(a) for a in assets),
        )

    def _part_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        content = row["content"] or ""
        text = content
        parsed = _json_loads(content, content)
        if isinstance(parsed, str):
            text = parsed
        elif isinstance(parsed, Mapping) and isinstance(parsed.get("text"), str):
            text = parsed["text"]
        visible, _, has_more = _clamp_get_text(text if isinstance(text, str) else _canonical_json(text))
        return {
            "owner": row["owner"],
            "chat_id": row["chat_id"],
            "actor_id": row["actor_id"],
            "message_id": row["message_id"],
            "part_id": row["part_id"],
            "revision": row["revision"],
            "runtime_generation": row["runtime_generation"],
            "event_sequence": row["event_sequence"],
            "event_workspace": row["event_workspace"],
            "event_project": row["event_project"],
            "role": row["role"],
            "part_type": row["part_type"],
            "text": visible,
            "snippet": visible[:240],
            "has_more": has_more,
            "content_hash": row["content_hash"],
            "time_created": row["time_created"],
            "time_updated": row["time_updated"],
            "tombstone": bool(row["tombstone"]),
            "tombstone_reason": row["tombstone_reason"],
        }

    @staticmethod
    def _search_part_dict(row: sqlite3.Row) -> dict[str, Any]:
        """Build a search hit from SQL's bounded projection, never source text."""
        item = {
            "owner": row["owner"],
            "chat_id": row["chat_id"],
            "actor_id": row["actor_id"],
            "message_id": row["message_id"],
            "part_id": row["part_id"],
            "revision": row["revision"],
            "runtime_generation": row["runtime_generation"],
            "event_sequence": row["event_sequence"],
            "event_workspace": row["event_workspace"],
            "event_project": row["event_project"],
            "role": row["role"],
            "part_type": row["part_type"],
            "content_hash": row["content_hash"],
            "time_created": row["time_created"],
            "time_updated": row["time_updated"],
            "tombstone": bool(row["tombstone"]),
            "tombstone_reason": row["tombstone_reason"],
        }
        if "index_preview" in row.keys():
            # SQL has already enforced the 4 KiB cap.  Python only cleans the
            # bounded value for inline-media markers; it never sees full rows.
            preview, media_omitted = _index_preview(row["index_preview"] or "")
            snippet, clipped = _utf8_prefix(preview, 240)
            item["snippet"] = snippet
            item["index_preview_omitted"] = (
                bool(row["index_preview_large_omitted"])
                or media_omitted
                or clipped
            )
        if "history_kind" in row.keys():
            item["history_kind"] = row["history_kind"]
            item["history_tool_name"] = row["history_tool_name"]
        return item

    def _outbox_dict(self, row: sqlite3.Row, *, state: Optional[str] = None, attempts: Optional[int] = None) -> dict[str, Any]:
        return {
            "outbox_seq": row["outbox_seq"],
            "owner": row["owner"],
            "chat_id": row["chat_id"],
            "actor_id": row["actor_id"],
            "message_id": row["message_id"],
            "part_id": row["part_id"],
            "revision": row["revision"],
            "event_kind": row["event_kind"],
            "payload": _json_loads(row["payload_json"], {}),
            "state": state or row["state"],
            "attempts": row["attempts"] if attempts is None else attempts,
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "delivered_at": row["delivered_at"],
        }


# ---------------------------------------------------------------------------
# Process-local singleton (host application persistence/service lifecycle)
# ---------------------------------------------------------------------------

_ARCHIVE: Optional[ConversationArchive] = None
_ARCHIVE_LOCK = threading.Lock()
_OWNER_RESOLVER: Optional[Callable[[str], Optional[str]]] = None


def configure_conversation_archive_owner_resolver(resolve_account_id, resolve_username) -> None:
    """Bind real AuthManager identity without adopting legacy username rows."""
    global _OWNER_RESOLVER
    def resolve(value: str) -> Optional[str]:
        direct = resolve_account_id(value)
        if direct:
            return direct
        return value if resolve_username(value) else None
    with _ARCHIVE_LOCK:
        _OWNER_RESOLVER = resolve
        if _ARCHIVE is not None:
            _ARCHIVE.owner_resolver = resolve



def default_db_path() -> str:
    """Resolve the canonical configured root without orphaning prior storage."""
    env = os.environ.get("OPENCLANK_CONVERSATION_ARCHIVE_DB")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    # DATA_DIR is the same resolved authority used by the host core database.
    # Imports remain local so standalone archive conversion stays import-light.
    from src.constants import DATA_DIR
    from src.runtime_paths import get_default_data_dir

    selected = os.path.abspath(os.path.join(DATA_DIR, "conversation_archive.sqlite3"))
    prior = os.path.abspath(os.path.expanduser(os.path.join(str(get_default_data_dir()), "conversation_archive.sqlite3")))
    if not os.path.exists(selected) and os.path.normcase(os.path.realpath(selected)) != os.path.normcase(os.path.realpath(prior)) and os.path.exists(prior):
        raise ArchiveUnavailableError(
            message=f"Canonical archive target {selected} is absent, but a prior default archive exists at {prior}. Explicitly relocate that archive to the configured data root or set OPENCLANK_CONVERSATION_ARCHIVE_DB to its intended absolute path; automatic fallback and relocation are disabled."
        )
    return selected


def configure_conversation_archive_origin_profile(profile) -> bool:
    """Bind the singleton to the actual guarded Host lifetime, never convert history."""
    from core.database import SessionLocal
    from src.openclank.memory_job_admission import SingleHostProfile
    from src.openclank.host_source_schema import require_current_connection
    global _ARCHIVE
    if type(profile) is not SingleHostProfile:
        return False
    try:
        profile.assert_current()
        profile.provider._guarded_preparation.assert_valid()
        with SessionLocal() as db:
            require_current_connection(db.connection())
        with _ARCHIVE_LOCK:
            if _OWNER_RESOLVER is None:
                return False
            path = default_db_path()
            archive = _ARCHIVE
            if archive is not None:
                # A pre-opened owner may only be rebound if its origin schema
                # already exists; a legacy archive is never upgraded here.
                if (os.path.abspath(archive.db_path) != os.path.abspath(path)
                        or not archive._origin_schema_present or archive._origin_profile is not None):
                    return False
            else:
                existed = os.path.lexists(path)
                if existed:
                    from urllib.parse import quote
                    if os.path.islink(path) or not os.path.isfile(path) or not os.path.getsize(path): return False
                    with sqlite3.connect('file:' + quote(os.path.abspath(path), safe='/') + '?mode=ro', uri=True) as db:
                        names = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
                    if 'origin_schema_marker' not in names:
                        return False
                archive = ConversationArchive(db_path=path, owner_resolver=_OWNER_RESOLVER, enable_origin_policy=True)
            # Verify the whole canonical origin schema, not merely a marker or
            # a list of column names, before binding it to the live profile.
            import stat
            before = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1: return False
            from urllib.parse import quote
            with sqlite3.connect('file:' + quote(os.path.abspath(path), safe='/') + '?mode=ro', uri=True, timeout=0.05) as db:
                if not _origin_schema_exact(db): return False
                cookie = db.execute('PRAGMA schema_version').fetchone()[0]
            after = os.stat(path, follow_symlinks=False)
            if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino): return False
            profile.assert_current()
            archive._origin_archive_identity = (after.st_dev,after.st_ino,cookie)
            archive.enable_origin_policy = True
            archive._origin_profile = profile
            _ARCHIVE = archive
            return True
    except Exception:
        return False


def archive_origin_profile_ready(profile) -> bool:
    """Current lifecycle fact, not capture or source authorization."""
    archive = _ARCHIVE
    return bool(archive is not None and archive._origin_profile is profile and archive._origin_available())


def release_conversation_archive_origin_profile(profile) -> None:
    with _ARCHIVE_LOCK:
        if _ARCHIVE is not None and _ARCHIVE._origin_profile is profile:
            _ARCHIVE._origin_profile = None
            _ARCHIVE.enable_origin_policy = False


def get_conversation_archive(db_path: Optional[str] = None) -> ConversationArchive:
    global _ARCHIVE
    if db_path is not None:
        return ConversationArchive(db_path=db_path)
    with _ARCHIVE_LOCK:
        if _ARCHIVE is None:
            if _OWNER_RESOLVER is None:
                raise ArchiveUnavailableError("archive authenticated identity resolver is unavailable")
            _ARCHIVE = ConversationArchive(db_path=default_db_path(), owner_resolver=_OWNER_RESOLVER)
        return _ARCHIVE


def reset_conversation_archive_for_test() -> None:
    global _ARCHIVE
    with _ARCHIVE_LOCK:
        _ARCHIVE = None
