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
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

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
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.isfile(self.db_path) and os.path.getsize(self.db_path):
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
            return
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
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
                if any(conn.execute(f"SELECT 1 FROM {table} WHERE owner=? LIMIT 1", (value,)).fetchone() for table in self._OWNER_TABLES):
                    raise ArchiveUnavailableError(message="legacy archive ownership requires explicit account-ID conversion")
        return account_id

    def owner_inventory(self, owner: str) -> dict[str, Any]:
        try:
            account_id = self._lifecycle_owner(owner, bind=True)
        except ValueError:
            with self._connect() as conn:
                if any(conn.execute(f"SELECT 1 FROM {table} WHERE owner=? LIMIT 1", (owner,)).fetchone() for table in self._OWNER_TABLES):
                    raise ArchiveUnavailableError(message="archive owner requires explicit identity conversion")
            return {"owner_account_id": None, "count": 0, "tables": {table: 0 for table in self._OWNER_TABLES}}
        with self._lock, self._connect() as conn:
            counts = {table: int(conn.execute(
                f"SELECT count(*) FROM {table} WHERE owner=?", (account_id,)
            ).fetchone()[0]) for table in self._OWNER_TABLES}
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
                ).rowcount for table in self._OWNER_TABLES}
                residual = sum(int(conn.execute(
                    f"SELECT count(*) FROM {table} WHERE owner=?", (account_id,)
                ).fetchone()[0]) for table in self._OWNER_TABLES)
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
                ).rowcount for table in self._OWNER_TABLES}
                residual = sum(int(conn.execute(
                    f"SELECT count(*) FROM {table} WHERE owner=? AND chat_id=?", (account_id, chat_id)
                ).fetchone()[0]) for table in self._OWNER_TABLES)
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
            ).fetchone()[0]) for table in self._OWNER_TABLES}
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
    ) -> dict[str, Any]:
        """Host-assign revisions for managed engine events.

        The engine has no durable history writer, so its timestamp cannot be a
        revision authority.  A single immediate transaction assigns and writes
        each revision.  It reuses only the current canonical revision for an
        exact replay; any different payload advances it, including a reversion
        to content from an older revision.  This keeps concurrent live and
        backfill deliveries lossless while restart replays remain no-ops.
        """
        accepted = duplicate = enqueued = 0
        part_ids: list[str] = []
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
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
    ) -> dict[str, Any]:
        """Version a destructive change as a tombstone; never prune the source row."""
        owner = self.resolve_owner(owner)
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
        clauses = ["state IN ('pending','reconciling')"]
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
                     WHERE state='running' AND updated_at < ?
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
                 WHERE outbox_seq=?
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

    def advance_cursor(self, *, owner: str, chat_id: str, consumer: str, last_outbox_seq: int) -> None:
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
