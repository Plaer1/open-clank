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
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

# Target paging bounds (from the memory/goals audit). Kept as named constants
# so tests and callers share one authority.
SEARCH_LIMIT_MAX = 50
AROUND_BEFORE_MAX = 50
AROUND_AFTER_MAX = 50
GET_LENGTH_MAX_UTF16 = 8000
GET_TEXT_PAGE_BYTES = 16000
TOOL_ENVELOPE_BYTES = 20 * 1024

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


def _utf16_len(text: str) -> int:
    """UTF-16 code-unit length, matching the target paging semantics."""
    return len(text.encode("utf-16-le")) // 2


def _clamp_get_text(text: str) -> tuple[str, int, bool]:
    """Clamp a full-part body to the target get bounds.

    Returns (visible_text, next_offset, has_more). ``next_offset`` is a
    UTF-16 cursor so surrogate pairs are never split.
    """
    if text is None:
        return "", 0, False
    # Prefer the byte page when it is tighter; both bounds must hold.
    byte_limited = text
    if len(text.encode("utf-8", errors="replace")) > GET_TEXT_PAGE_BYTES:
        raw = text.encode("utf-8", errors="replace")[:GET_TEXT_PAGE_BYTES]
        byte_limited = raw.decode("utf-8", errors="ignore")

    total_units = _utf16_len(byte_limited)
    if total_units <= GET_LENGTH_MAX_UTF16:
        has_more = byte_limited != text
        return byte_limited, total_units if has_more else 0, has_more

    # Walk the string in code points until the UTF-16 budget is hit. Never
    # cut a surrogate pair in half (Python str is code-point safe already).
    units = 0
    cut = 0
    for index, ch in enumerate(byte_limited):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > GET_LENGTH_MAX_UTF16:
            cut = index
            break
        cut = index + 1
    visible = byte_limited[:cut]
    has_more = visible != text
    return visible, _utf16_len(visible), has_more


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
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

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
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
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
        now = _now_ms()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
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
            budget = GET_LENGTH_MAX_UTF16 if length is None else min(int(length), GET_LENGTH_MAX_UTF16)
            offset = max(0, int(offset))
            if offset:
                # UTF-16 safe skip: slice by code points accumulating units.
                units = 0
                slice_at = 0
                for index, ch in enumerate(body):
                    units += 2 if ord(ch) > 0xFFFF else 1
                    if units >= offset:
                        slice_at = index + (1 if units == offset else 0)
                        # If the skip lands mid-pair it cannot: Python code points
                        # are atomic, so units never split a surrogate pair.
                        break
                    slice_at = index + 1
                body = body[slice_at:]
            visible, consumed, has_more = _clamp_get_text(body)
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
                "next_offset": (offset + consumed) if has_more else None,
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

        params.append(limit)
        sql = f"""
            SELECT * FROM conversation_parts
            WHERE {' AND '.join(clauses)}
            ORDER BY time_created DESC, seq DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            hits = []
            for row in rows:
                if tool_name:
                    meta = _json_loads(row["content"], {})
                    if isinstance(meta, Mapping) and meta.get("tool_name") != tool_name:
                        continue
                item = self._part_dict(row)
                if not include_snippets:
                    item.pop("snippet", None)
                elif include_snippets and "text" in item:
                    text = item.pop("text", "") or ""
                    item["snippet"] = text[:240]
                    # Media-safe: never stuff base64 into search results.
                    if text.startswith("data:") or "base64," in text[:32]:
                        item["snippet"] = f"[{item.get('part_type', 'part')}]"
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
            return part
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
            owner=str(data["owner"]),
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


def default_db_path() -> str:
    """Archive lives beside the host conversation database."""
    env = os.environ.get("OPENCLANK_CONVERSATION_ARCHIVE_DB")
    if env:
        return env
    from src.runtime_paths import get_default_data_dir  # local import: keep module import-light

    return os.path.join(str(get_default_data_dir()), "conversation_archive.sqlite3")


def get_conversation_archive(db_path: Optional[str] = None) -> ConversationArchive:
    global _ARCHIVE
    if db_path is not None:
        return ConversationArchive(db_path=db_path)
    with _ARCHIVE_LOCK:
        if _ARCHIVE is None:
            _ARCHIVE = ConversationArchive(db_path=default_db_path())
        return _ARCHIVE


def reset_conversation_archive_for_test() -> None:
    global _ARCHIVE
    with _ARCHIVE_LOCK:
        _ARCHIVE = None
