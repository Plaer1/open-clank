"""Read-only, bounded logging projections; canonical archives remain the authority.

No constructor imports, migrates, backfills or creates an archive. Connections
use SQLite read-only mode, and each page is a coherent read transaction.
"""
from __future__ import annotations

import base64
import hashlib
import re
import hmac
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.stats.privacy import identity_handle, owner_scope

SCHEMA = "open-clank.logging.v1"
PRODUCER_REVISION = "l01-archive-v1"
MAX_PAGE = 100
MAX_BODY_CHARS = 16384
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_FACTS = 10000


class LoggingError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code.replace("_", " "))


def action_availability(host_session_available, *, session_manager_available=True):
    """Describe the existing host capabilities without promoting archive-only history."""
    actions = ("rename", "pin", "archive", "restore", "resume")
    result = {}
    for action in actions:
        available = bool(host_session_available)
        reason = None if available else "host_session_missing"
        if available and action in {"rename", "pin"} and not session_manager_available:
            available, reason = False, "session_manager_unavailable"
        result[action] = {"available": available, "reason": reason}
    return result


def _check(cancel_event=None, deadline=None):
    if cancel_event is not None and cancel_event.is_set():
        raise LoggingError("cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise LoggingError("deadline_exceeded")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


# Payloads from the managed-history migration preserve original MiMo parts
# under mimo.part. SQL predicates and projections share the same resolution.
def _field(name):
    return ("CASE WHEN json_valid(p.content) THEN COALESCE("
            f"json_extract(p.content, '$.mimo.part.{name}'), "
            f"json_extract(p.content, '$.{name}')) END")


def _field_type(name):
    # json_extract serializes objects as strings. Preserve actual JSON type
    # before treating a reported tool value as a name.
    return ("CASE WHEN json_valid(p.content) THEN COALESCE("
            f"NULLIF(json_type(p.content, '$.mimo.part.{name}'),'null'), "
            f"NULLIF(json_type(p.content, '$.{name}'),'null')) END")


TOOL = f"COALESCE({_field('tool')}, {_field('tool_name')}, {_field('function.name')}, {_field('name')})"
TOOL_TYPE = f"COALESCE({_field_type('tool')}, {_field_type('tool_name')}, {_field_type('function.name')}, {_field_type('name')})"
STATUS = f"COALESCE({_field('state.status')}, {_field('status')})"
LATEST = """NOT EXISTS (SELECT 1 FROM conversation_parts newer
 WHERE newer.owner=p.owner AND newer.chat_id=p.chat_id AND newer.actor_id=p.actor_id
 AND newer.message_id=p.message_id AND newer.part_id=p.part_id AND newer.revision>p.revision)"""
COLUMNS = f"""p.seq,p.chat_id,p.actor_id,p.message_id,p.part_id,p.revision,
p.runtime_generation,p.event_sequence,p.event_workspace,p.event_project,p.role,p.part_type,
p.content_hash,p.time_created,p.time_updated,p.tombstone,
{TOOL} AS tool_name, {TOOL_TYPE} AS tool_name_type, {STATUS} AS observed_status,
{_field('state.time.start')} AS tool_start_ms, {_field('state.time.end')} AS tool_end_ms,
{_field('skill')} AS skill_name, {_field('parentID')} AS parent_id,
{_field('operation_id')} AS operation_id, {_field('attempt_id')} AS attempt_id,
{_field('root_operation_id')} AS root_operation_id,
length(p.content) AS content_chars"""


def tool_usage_join_handle(owner, account, chat, holder, session, root, actor, execution):
    """Content-free private join key; not an authorization credential."""
    if (not all(isinstance(v, str) and 0 < len(v) <= 512 for v in (account,chat,holder,session,root,actor))
            or type(execution) is not int or execution < 0):
        return None
    return identity_handle(owner, "operation_id", _json(["tool-usage.v1",account,chat,holder,session,root,actor,execution]))


def normalize_filters(filters=None):
    f = dict(filters or {})
    allowed = {"session_id", "workspace_id", "actor_id", "status", "tool_name", "part_type",
               "provider_id", "account_id", "route_id", "actual_model", "requested_model",
               "start", "end", "period", "timezone"}
    if set(f) - allowed:
        raise LoggingError("invalid_filter", "Unsupported logging filter")
    f = {k: v for k, v in f.items() if v not in (None, "")}
    try:
        zone = f.get("timezone", "UTC")
        # Browser resolves 'local' to its IANA zone before making a query.
        ZoneInfo(zone)
        for key in ("start", "end"):
            if key in f:
                value = f[key]
                if isinstance(value, bool):
                    raise ValueError()
                if isinstance(value, (int, float)):
                    f[key] = int(value)
                else:
                    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=ZoneInfo(zone))
                    f[key] = int(parsed.timestamp() * 1000)
        if "period" in f and "start" not in f:
            from services.stats.query import parse_scope
            scope = parse_scope(owner="logging-range", period=f["period"], timezone_name=zone)
            if scope.start_utc is not None:
                f["start"] = int(scope.start_utc.replace(tzinfo=timezone.utc).timestamp() * 1000)
                f["end"] = int(scope.end_utc.replace(tzinfo=timezone.utc).timestamp() * 1000)
        if "start" in f and "end" not in f:
            f["end"] = int(datetime.now(timezone.utc).timestamp() * 1000)
        if "start" in f and "end" in f and f["end"] <= f["start"]:
            raise ValueError()
        for dimension in ("session_id", "workspace_id", "provider_id", "account_id", "route_id", "actual_model", "requested_model"):
            if dimension in f and not str(f[dimension]).startswith(dimension.removesuffix("_id").replace("_", "-") + "_"):
                raise ValueError()
        for key in allowed - {"start", "end"}:
            if key in f and (not isinstance(f[key], str) or len(f[key]) > 256):
                raise ValueError()
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise LoggingError("invalid_filter", "Invalid date, timezone or filter value") from None
    return f


class LoggingProjection:
    def __init__(self, archive_path, *, session_factory=None, cursor_secret=None, archive_owner_resolver=None):
        self.archive_path = str(archive_path)
        self.session_factory = session_factory
        self.archive_owner_resolver = archive_owner_resolver
        # Process secret keeps cursors opaque/unforgeable; restart explicitly
        # invalidates old cursors rather than retaining accidental authority.
        if cursor_secret is None:
            import secrets
            cursor_secret = secrets.token_bytes(32)
        self.cursor_secret = cursor_secret

    @contextmanager
    def _read(self, cancel_event=None, deadline=None):
        _check(cancel_event, deadline)
        conn = None
        try:
            uri = Path(self.archive_path).absolute().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=1)
            conn.row_factory = sqlite3.Row
            conn.set_progress_handler(lambda: int((cancel_event is not None and cancel_event.is_set()) or
                                                   (deadline is not None and time.monotonic() >= deadline)), 500)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            yield conn
            _check(cancel_event, deadline)
        except sqlite3.Error as exc:
            _check(cancel_event, deadline)
            raise LoggingError("archive_unavailable") from exc
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _owner(owner):
        value = str(owner or "").strip().lower()
        if not value:
            raise LoggingError("owner_required")
        return value

    def _archive_owner(self, owner):
        if self.archive_owner_resolver is None:
            return owner
        try:
            resolved = self.archive_owner_resolver(owner)
        except Exception as exc:
            raise LoggingError("archive_unavailable", "Archive identity requires current account binding or explicit conversion") from exc
        if not resolved:
            raise LoggingError("archive_unavailable", "Archive account identity unavailable")
        return str(resolved)

    def _revision(self, conn, owner):
        # Canonical revisions are immutable and append-only. Count detects
        # deletions; max seq detects writes even when timestamps are identical.
        row = conn.execute("SELECT COUNT(*),COALESCE(MAX(seq),0),COALESCE(SUM(revision),0),"
                           "COALESCE(SUM(tombstone),0) FROM conversation_parts WHERE owner=?", (self._archive_owner(owner),)).fetchone()
        fences = conn.execute("SELECT chat_id,erased_at FROM conversation_archive_deleted_chats WHERE owner=? ORDER BY chat_id", (self._archive_owner(owner),)).fetchall()
        return _digest([tuple(row), [tuple(fence) for fence in fences]])

    def _cursor(self, owner, query, revision, position):
        data = base64.urlsafe_b64encode(_json({"owner": owner_scope(owner), "query": _digest(query),
                                             "revision": revision, "position": position,
                                             "bounds": {k: query.get("filters", {}).get(k) for k in ("start", "end")}}).encode()).decode().rstrip("=")
        mac = hmac.new(self.cursor_secret, data.encode(), hashlib.sha256).hexdigest()
        return data + "." + mac

    def _page_filters(self, owner, filters, cursor):
        normalized = normalize_filters(filters)
        if cursor:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048: raise ValueError()
                data, mac = cursor.split('.')
                expected = hmac.new(self.cursor_secret, data.encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(mac, expected): raise ValueError()
                payload = json.loads(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))
                if payload['owner'] != owner_scope(owner): raise ValueError()
                for key, value in payload.get('bounds', {}).items():
                    if key in {'start', 'end'} and key not in (filters or {}) and value is not None:
                        normalized[key] = value
            except (ValueError, TypeError, KeyError):
                raise LoggingError('invalid_cursor') from None
        return normalized

    def _position(self, cursor, owner, query, revision):
        if not cursor:
            return 0
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ValueError()
            data, mac = cursor.split(".")
            expected = hmac.new(self.cursor_secret, data.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(mac, expected):
                raise ValueError()
            payload = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
            if payload["owner"] != owner_scope(owner) or payload["query"] != _digest(query):
                raise ValueError()
            if payload["revision"] != revision:
                raise LoggingError("stale_cursor", "Archive changed; restart the query")
            pos = payload["position"]
            if isinstance(pos, bool) or not isinstance(pos, int) or pos < 0:
                raise ValueError()
            return pos
        except LoggingError:
            raise
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise LoggingError("invalid_cursor") from None

    def _scope(self, conn, owner, filters, *, cancel_event=None, deadline=None):
        clauses = ["p.owner=?", LATEST, "p.tombstone=0",
                   "NOT EXISTS (SELECT 1 FROM conversation_archive_deleted_chats erased WHERE erased.owner=p.owner AND erased.chat_id=p.chat_id)"]
        args = [self._archive_owner(owner)]
        for key, col in (("actor_id", "p.actor_id"), ("part_type", "p.part_type"),
                         ("status", STATUS), ("tool_name", TOOL)):
            if key in filters:
                clauses.append(f"{col}=?")
                args.append(filters[key])
        for key, compare in (("start", ">="), ("end", "<")):
            if key in filters:
                clauses.append(f"p.time_created{compare}?")
                args.append(filters[key])
        for key, col in (("session_id", "p.chat_id"), ("workspace_id", "p.event_workspace")):
            if key in filters:
                candidates = conn.execute(f"SELECT DISTINCT {col} FROM conversation_parts p WHERE owner=? LIMIT ?", (self._archive_owner(owner), MAX_FACTS + 1)).fetchall()
                if len(candidates) > MAX_FACTS:
                    raise LoggingError("query_too_large", "Narrow identity filters")
                values = [r[0] for r in candidates if r[0] and identity_handle(owner, key, r[0]) == filters[key]]
                if not values:
                    raise LoggingError("invalid_filter", "Unknown or out-of-scope identity handle")
                clauses.append(f"{col} IN ({','.join('?' for _ in values) or 'NULL'})")
                args.extend(values)
        dimensions = {key: filters[key] for key in ("provider_id", "account_id", "route_id", "actual_model", "requested_model")
                      if key in filters}
        if dimensions:
            ids = self._stats_sessions(owner, {**filters, **dimensions}, cancel_event=cancel_event, deadline=deadline)
            clauses.append(f"p.chat_id IN ({','.join('?' for _ in ids) or 'NULL'})")
            args.extend(ids)
        return " AND ".join(clauses), args

    def _stats_sessions(self, owner, filters, *, cancel_event=None, deadline=None):
        if self.session_factory is None:
            raise LoggingError("stats_unavailable")
        from services.stats.presentation import event_cohort
        from services.stats.query import StatsQueryError
        db = self.session_factory()
        try:
            try:
                _, _, _, selected = event_cohort(db, owner, filters, cancel_event=cancel_event, deadline=deadline)
            except StatsQueryError as exc:
                raise LoggingError("invalid_filter", str(exc)) from exc
            return sorted(selected or ())
        finally:
            db.close()

    def resolve_session(self, owner, handle, *, cancel_event=None, deadline=None):
        owner = self._owner(owner)
        with self._read(cancel_event, deadline) as conn:
            rows = conn.execute("SELECT DISTINCT chat_id FROM conversation_parts p WHERE owner=? AND NOT EXISTS (SELECT 1 FROM conversation_archive_deleted_chats erased WHERE erased.owner=p.owner AND erased.chat_id=p.chat_id) LIMIT ?", (self._archive_owner(owner), MAX_FACTS + 1))
            for index, row in enumerate(rows):
                if index >= MAX_FACTS:
                    raise LoggingError("query_too_large", "Session handle resolution exceeds archive catalog bound")
                _check(cancel_event, deadline)
                if identity_handle(owner, "session_id", row[0]) == handle:
                    return row[0]
        raise LoggingError("not_found")

    def _metadata(self, owner, ids, *, cancel_event=None, deadline=None):
        _check(cancel_event, deadline)
        if not ids or self.session_factory is None:
            return {}
        from core.database import Session as DbSession
        db = self.session_factory()
        try:
            rows = db.query(DbSession).filter(DbSession.owner == owner, DbSession.id.in_(ids)).all()
            return {r.id: {"name": r.name, "archived": bool(r.archived), "pinned": bool(r.is_important),
                           "updated_at": str(r.updated_at), "configured_model": r.model, "lifecycle_state": "archived" if r.archived else "saved",
                           "host_session_available": True,
                           "action_availability": action_availability(True)}
                    for r in rows}
        finally:
            db.close()

    def _envelope(self, owner, items, revision, query, cursor, position, limit, more, *, reason=None):
        return {"schema": SCHEMA, "owner_scope": owner_scope(owner), "items": items, "filters": query.get("filters", {}),
                "coverage": {"state": "partial" if reason else "reported", "reason": reason,
                             "source_revision": revision, "producer_revision": PRODUCER_REVISION},
                "page": {"cursor": cursor, "next_cursor": self._cursor(owner, query, revision, position) if more else None,
                         "limit": limit, "truncated": more}}

    def _conversation_authority(self, conn, owner, ids, filters, *, cancel_event=None, deadline=None):
        """Only exact owner host message IDs and actual saved host bodies qualify.

        This is a source population, never equivalence or text deduplication.
        It is independent of the current page and excludes other actors.
        """
        if not ids or self.session_factory is None:
            return {}, False
        from core.database import ChatMessage, Session as DbSession
        db = self.session_factory()
        try:
            messages = (db.query(ChatMessage.id, ChatMessage.session_id, ChatMessage.role)
                .join(DbSession, DbSession.id == ChatMessage.session_id)
                .filter(DbSession.owner == owner, ChatMessage.session_id.in_(ids),
                        ChatMessage.role.in_(['user', 'assistant']), ChatMessage.content.isnot(None),
                        ChatMessage.content != ''))
            for key, operation in [('start', '>='), ('end', '<')]:
                if key in filters:
                    bound = datetime.fromtimestamp(filters[key] / 1000, timezone.utc).replace(tzinfo=None)
                    messages = messages.filter(ChatMessage.timestamp >= bound if operation == '>=' else ChatMessage.timestamp < bound)
            host_rows = messages.limit(MAX_FACTS + 1).all()
            truncated = len(host_rows) > MAX_FACTS
            known = {(sid, mid, role) for mid, sid, role in host_rows[:MAX_FACTS]}
        finally:
            db.close()
        clauses = ['p.owner=?', LATEST, 'p.tombstone=0', "p.runtime_generation='0'", "p.actor_id='main'",
                   "p.part_type='text'", "length(trim(p.content))>0",
                   'NOT EXISTS (SELECT 1 FROM conversation_archive_deleted_chats erased WHERE erased.owner=p.owner AND erased.chat_id=p.chat_id)',
                   'p.chat_id IN (' + ','.join('?' for _ in ids) + ')']
        values = [self._archive_owner(owner), *ids]
        for key, operator in [('start', '>='), ('end', '<')]:
            if key in filters:
                clauses.append(f'p.time_created{operator}?'); values.append(filters[key])
        rows = conn.execute('SELECT DISTINCT p.chat_id,p.message_id,p.role FROM conversation_parts p WHERE '
                            + ' AND '.join(clauses) + ' LIMIT ?', [*values, MAX_FACTS + 1]).fetchall()
        truncated = truncated or len(rows) > MAX_FACTS
        result = {}
        for row in rows[:MAX_FACTS]:
            _check(cancel_event, deadline)
            if (row['chat_id'], row['message_id'], row['role']) in known:
                result.setdefault(row['chat_id'], set()).add(row['message_id'])
        return result, truncated

    def sessions(self, owner, filters=None, *, cursor=None, limit=50, sort="latest", direction="desc",
                 query_text="", collection="all", cancel_event=None, deadline=None):
        owner = self._owner(owner)
        f = self._page_filters(owner, filters, cursor)
        limit = self._limit(limit)
        if sort not in {"latest", "messages", "tokens", "cost"} or direction not in {"asc", "desc"} or collection not in {"all", "pinned", "archived"}:
            raise LoggingError("invalid_filter", "Invalid session sort or collection")
        query_text = str(query_text or "").strip()
        if len(query_text) > 256:
            raise LoggingError("invalid_filter", "Session search is too long")
        query = {"kind": "sessions", "filters": f, "sort": sort, "direction": direction,
                 "query_text": query_text, "collection": collection}
        with self._read(cancel_event, deadline) as conn:
            revision = self._revision(conn, owner)
            where, args = self._scope(conn, owner, f, cancel_event=cancel_event, deadline=deadline)
            rows = conn.execute(f"SELECT p.chat_id,MAX(p.time_updated) AS last_activity,COUNT(*) AS parts,"
                                "COUNT(DISTINCT p.actor_id) AS actors,COUNT(DISTINCT json_array(p.actor_id,p.message_id)) AS messages "
                                f"FROM conversation_parts p WHERE {where} GROUP BY p.chat_id "
                                "ORDER BY last_activity DESC,p.chat_id ASC LIMIT ?", [*args, MAX_FACTS + 1]).fetchall()
            truncated = len(rows) > MAX_FACTS
            rows = rows[:MAX_FACTS]
            metadata = self._metadata(owner, [r["chat_id"] for r in rows], cancel_event=cancel_event, deadline=deadline)
            metrics = {}
            stats_coverage = {"state": "unavailable", "reason": "stats_unavailable"}
            if self.session_factory is not None:
                from services.stats.presentation import session_metrics
                from services.stats.query import StatsQueryError
                db = self.session_factory()
                try:
                    metrics, _, projection, stats_truncated, _, _ = session_metrics(db, owner, f,
                        session_ids={r["chat_id"] for r in rows}, cancel_event=cancel_event, deadline=deadline)
                    stats_coverage = {"state": "partial_truncated" if stats_truncated else projection.coverage,
                                      "source": "admitted_stats_events"}
                except StatsQueryError as exc:
                    raise LoggingError("invalid_filter", str(exc)) from exc
                finally:
                    db.close()
            # Tool facts are read in this archive snapshot, avoiding an extra
            # reader/cursor lifecycle or optional advanced capture dependency.
            from services.stats.tool_evidence import coalesce_facts, cohort
            tool_rows = conn.execute(f"SELECT {COLUMNS} FROM conversation_parts p WHERE {where} "
                                    "AND p.part_type IN ('tool','tool_call','tool_result','tool_use','tool_output') "
                                    "ORDER BY p.seq LIMIT ?", [*args, MAX_FACTS + 1]).fetchall()
            tool_coverage = {"state": "partial" if len(tool_rows) > MAX_FACTS else "reported", "source_revision": revision}
            facts = coalesce_facts(owner, [self._fact(owner, r) for r in tool_rows[:MAX_FACTS]])
            by_session = {}
            for fact in facts:
                by_session.setdefault(fact['session_id'], []).append(fact)
            host_sources, host_sources_truncated = self._conversation_authority(conn, owner, [r['chat_id'] for r in rows], f,
                cancel_event=cancel_event, deadline=deadline)
            items = []
            for r in rows:
                _check(cancel_event, deadline)
                item = {"handle": identity_handle(owner, "session_id", r["chat_id"]),
                        "last_activity_ms": r["last_activity"], "part_count": r["parts"], "actor_count": r["actors"],
                        "message_count": {"value": r["messages"], "state": "reported", "unit": "count", "population": "archive_messages"},
                        **metadata.get(r["chat_id"], {"name": "Saved archive", "lifecycle_state": "archive_only",
                            "host_session_available": False, "action_availability": action_availability(False)}),
                        **metrics.get(r["chat_id"], {"tokens": {"value": None, "state": "unavailable", "unit": "tokens"},
                            "token_metrics": {}, "cost": {"value": None, "amount": None, "currency": None, "state": "unpriced"},
                            "provider_identities": [], "model_identities": [], "workspace_identity": None}),
                        "tools": cohort(by_session.get(r["chat_id"], []), tool_coverage),
                        "failure_tool_signatures": self._failure_tool_signatures(owner, by_session.get(r["chat_id"], []), tool_coverage)}
                item['archive_message_count'] = dict(item['message_count'])
                host_messages = host_sources.get(r['chat_id'], set())
                item['host_conversation_available'] = bool(host_messages)
                item['conversation_source'] = 'host_conversation' if host_messages else 'archive_conversation'
                if host_messages:
                    item['message_count'] = {'value': len(host_messages), 'state': 'partial' if host_sources_truncated else 'reported', 'unit': 'count',
                                             'population': 'host_conversation_messages'}
                if collection == "pinned" and not item.get("pinned") or collection == "archived" and not item.get("archived"):
                    continue
                if query_text and query_text.casefold() not in str(item.get('name', '')).casefold():
                    continue
                items.append(item)
            def value(item):
                if sort == 'latest': return item.get('last_activity_ms')
                if sort == 'messages': return item['message_count']['value']
                if sort == 'tokens':
                    metric = item['tokens']
                    return metric.get('value') if metric.get('value') is not None else int(metric['exact']) if metric.get('exact') else None
                amount = item['cost'].get('amount')
                from fractions import Fraction
                try:
                    return Fraction(amount) if amount is not None else None
                except (ValueError, TypeError, ZeroDivisionError):
                    return None
            # Tie order is stable in both directions; missing metrics stay last.
            items.sort(key=lambda item: item['handle'])
            populated = [item for item in items if value(item) is not None]
            populated.sort(key=value, reverse=direction == 'desc')
            items = populated + [item for item in items if value(item) is None]
            # Changes to names/pins/metrics invalidate the same continuation.
            revision = _digest([revision, items])
            offset = self._position(cursor, owner, query, revision)
            selected = items[offset:offset + limit]
            result = self._envelope(owner, selected, revision, query, cursor, offset + len(selected), limit,
                                    offset + len(selected) < len(items), reason='session_candidate_cap' if truncated else None)
            result['page']['total'] = len(items)
            result['filter_semantics'] = 'session_cohort'
            result['populations'] = {'sessions': 'saved_archive', 'messages': 'host_conversation_when_available_else_dated_archive_messages',
                                     'tokens': 'matching_admitted_events', 'tools': 'dated_archive_tools'}
            result['source_coverage'] = {'host_messages': {'state': 'partial_truncated' if host_sources_truncated else 'reported'}, 'stats': stats_coverage, 'tools': tool_coverage,
                                         'archive': result['coverage']}
            return result

    @staticmethod
    def _limit(limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
            raise LoggingError("invalid_limit")
        return limit

    def parts(self, owner, filters=None, *, cursor=None, limit=50, include_bodies=True,
              query_text=None, facts_only=False, cancel_event=None, deadline=None):
        owner = self._owner(owner)
        f = self._page_filters(owner, filters, cursor)
        limit = self._limit(limit)
        if query_text is not None and (not isinstance(query_text, str) or not query_text.strip() or len(query_text) > 512):
            raise LoggingError("invalid_query")
        query = {"kind": "facts" if facts_only else "parts", "filters": f,
                 "query_text": query_text, "include_bodies": bool(include_bodies)}
        with self._read(cancel_event, deadline) as conn:
            revision = self._revision(conn, owner)
            offset = self._position(cursor, owner, query, revision)
            where, args = self._scope(conn, owner, f, cancel_event=cancel_event, deadline=deadline)
            if query_text is not None:
                # Bound source text returned, not searchable text. instr uses a
                # literal term, so wildcard characters cannot widen scope.
                where += " AND instr(lower(p.content),lower(?))>0"
                args.append(query_text.strip())
            if facts_only:
                where += " AND p.part_type IN ('tool','tool_call','tool_result','tool_use','tool_output')"
            body = f",substr(p.content,1,{MAX_BODY_CHARS}) AS body" if include_bodies else ""
            timing = ""
            if facts_only:
                names = {column[1] for column in conn.execute("PRAGMA table_info(conversation_parts)")}
                if "qualified_tool_json" in names:
                    timing = ",p.qualified_tool_json AS qualified_tool_json,CASE WHEN length(CAST(p.content AS BLOB))<=1048576 THEN p.content END AS qualified_content"
            rows = conn.execute(f"SELECT {COLUMNS}{body}{timing} FROM conversation_parts p WHERE {where} "
                                "ORDER BY (SELECT MIN(m.seq) FROM conversation_parts m WHERE m.owner=p.owner AND m.chat_id=p.chat_id "
                                "AND m.actor_id=p.actor_id AND m.message_id=p.message_id), p.event_sequence ASC, "
                                "(SELECT MIN(a.seq) FROM conversation_parts a WHERE a.owner=p.owner AND a.chat_id=p.chat_id "
                                "AND a.actor_id=p.actor_id AND a.message_id=p.message_id AND a.part_id=p.part_id) "
                                "LIMIT ? OFFSET ?", [*args, limit + 1, offset]).fetchall()
            items, size = [], 0
            for row in rows[:limit]:
                _check(cancel_event, deadline)
                item = self._fact(owner, row, self._archive_owner(owner)) if facts_only else self._part(owner, row, include_bodies)
                amount = len(_json(item).encode())
                if size + amount > MAX_RESPONSE_BYTES:
                    break
                items.append(item)
                size += amount
            if not facts_only:
                host_sources, host_sources_truncated = self._conversation_authority(conn, owner, sorted({row['chat_id'] for row in rows[:len(items)]}), f,
                    cancel_event=cancel_event, deadline=deadline)
                for item, row in zip(items, rows):
                    host_ids = host_sources.get(row['chat_id'], set())
                    host_message = (row['runtime_generation'] == '0' and row['actor_id'] == 'main'
                                    and row['message_id'] in host_ids)
                    managed = row['runtime_generation'] == 'managed-mimo-v1'
                    item['host_conversation_available'] = bool(host_ids)
                    item['presentation_coverage'] = 'partial' if host_sources_truncated else 'reported'
                    item['source_kind'] = 'host_conversation' if host_message else 'managed_engine' if managed else 'archive'
                    item['presentation_kind'] = ('engine_evidence' if managed and host_ids
                        and row['actor_id'] == 'main' and row['part_type'] == 'text' else 'conversation')
            more = len(rows) > len(items)
            reason = "body_truncated" if any(i.get("body_truncated") for i in items) else None
            return self._envelope(owner, items, revision, query, cursor, offset + len(items), limit, more, reason=reason)

    def clankshop_activity(self, account, chat, workspace, session, map_revision, binding_revision,
                           holder, generation, cursor=None, *, limit=64, cancel_event=None, deadline=None):
        """Bounded body-free archive delta; an invalidated prefix requires a fresh baseline."""
        if (not all(isinstance(v, str) and v and len(v) <= 512 for v in (account, chat, workspace, session, holder))
                or any(type(v) is not int or v < 0 for v in (map_revision, binding_revision, generation))
                or type(limit) is not int or not 1 <= limit <= 100):
            raise ValueError("invalid activity scope")
        stat = Path(self.archive_path).stat()
        scope = _digest([account, chat, workspace, session, map_revision, binding_revision, holder, generation, stat.st_dev, stat.st_ino])
        previous = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048: raise ValueError()
                data, mac = cursor.split('.')
                if not hmac.compare_digest(mac, hmac.new(self.cursor_secret, data.encode(), hashlib.sha256).hexdigest()): raise ValueError()
                previous = json.loads(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))
                if (set(previous) != {'scope', 'seq', 'anchor'} or previous['scope'] != scope
                    or type(previous['seq']) is not int or previous['seq'] < 0
                    or (previous['anchor'] is not None and (not isinstance(previous['anchor'], str) or len(previous['anchor']) != 64))): raise ValueError()
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                return {'state': 'resync', 'reason': 'cursor_invalid_or_scope_changed', 'items': [], 'cursor': None}
        with self._read(cancel_event, deadline) as conn:
            if conn.execute('SELECT 1 FROM conversation_archive_deleted_chats WHERE owner=? AND chat_id=?', (account, chat)).fetchone():
                return {'state': 'resync', 'reason': 'chat_erased', 'items': [], 'cursor': None}
            def anchor(at):
                if at == 0: return None
                row = conn.execute('SELECT seq,actor_id,message_id,part_id,revision,content_hash,tombstone FROM conversation_parts WHERE owner=? AND chat_id=? AND seq=?', (account,chat,at)).fetchone()
                return _digest(tuple(row)) if row else None
            if previous is None:
                latest = conn.execute('SELECT seq FROM conversation_parts WHERE owner=? AND chat_id=? ORDER BY seq DESC LIMIT 1', (account,chat)).fetchone()
                seq = latest[0] if latest else 0
                state = 'baseline'
                rows = []
            else:
                seq = previous['seq']
                if (seq and anchor(seq) is None) or anchor(seq) != previous['anchor']:
                    return {'state': 'resync', 'reason': 'archive_prefix_changed', 'items': [], 'cursor': None}
                names = {col[1] for col in conn.execute('PRAGMA table_info(conversation_parts)')}
                if 'qualified_tool_json' not in names:
                    return {'state': 'unavailable', 'reason': 'qualified_evidence_unavailable', 'items': [], 'cursor': None}
                rows = conn.execute(f"SELECT {COLUMNS},p.qualified_tool_json,CASE WHEN length(CAST(p.content AS BLOB))<=1048576 THEN p.content END AS qualified_content FROM conversation_parts p WHERE p.owner=? AND p.chat_id=? AND p.seq>? ORDER BY p.seq LIMIT ?", (account,chat,seq,limit+1)).fetchall()
                state = 'partial' if len(rows)>limit else 'delta'
                rows = rows[:limit]
                if rows: seq = rows[-1]['seq']
            items = []
            novelty_bounded = True
            for row in rows:
                _check(cancel_event, deadline)
                if row['tombstone'] or row['event_workspace'] not in (None, workspace): continue
                fact = self._fact(account, row, archive_owner=account)
                try:
                    proof = json.loads(row['qualified_tool_json'])
                    evidence = proof['evidence']; content=row['qualified_content']
                    if (type(proof) is not dict or set(proof)!={'version','account','chat','holder','evidence'}
                            or proof['account']!=account or proof['chat']!=chat or proof['holder']!=holder
                            or type(evidence) is not dict or evidence.get('sessionID')!=session
                            or evidence.get('actorID')!=row['actor_id'] or evidence.get('messageID')!=row['message_id']
                            or evidence.get('partID')!=row['part_id'] or evidence.get('contentSHA256')!=row['content_hash']
                            or type(content) is not str or hashlib.sha256(content.encode('utf-8')).hexdigest()!=row['content_hash']):continue
                    part=json.loads(content); part_state=part.get('state') if type(part) is dict else None
                    if (type(part_state) is not dict or part.get('type')!='tool' or part.get('id')!=row['part_id']
                            or part.get('sessionID')!=session or part.get('messageID')!=row['message_id']
                            or part.get('callID')!=evidence.get('callID')):continue
                    if proof['version']==1 and fact.get('qualified_tool'):
                        phase='terminal'; coverage='qualified_terminal'
                        outcome={'completed':'success','error':'failure'}.get(part_state.get('status'))
                        if outcome is None:continue
                    elif proof['version']==2:
                        fields={'version','algorithm','clockID','sessionID','rootOperationID','actorID','executionRevision','innerTurnID','messageID','partID','callID','phase','sequence','observedUS','wallObservedMS','contentSHA256'}
                        if (set(evidence)!=fields or type(evidence['version']) is not int or evidence['version']!=2
                                or evidence['algorithm']!='ed25519' or evidence['phase'] not in ('started','progress')
                                or type(evidence['sequence']) is not int or not 1<=evidence['sequence']<=9007199254740991
                                or (evidence['phase']=='started')!=(evidence['sequence']==1)
                                or any(type(evidence[k]) is not int or not 0<=evidence[k]<=9007199254740991 for k in ('executionRevision','observedUS','wallObservedMS'))
                                or any(type(evidence[k]) is not str or not evidence[k] or len(evidence[k])>(128 if k=='actorID' else 256) for k in ('sessionID','rootOperationID','actorID','innerTurnID','messageID','partID','callID'))
                                or type(evidence['clockID']) is not str or not re.fullmatch(r'[0-9a-f]{32}',evidence['clockID'])
                                or part.get('execution')!='started' or part_state.get('status')!='running'):continue
                        phase=evidence['phase']; coverage='qualified_activity'; outcome=None
                    else:continue
                    allowed={'read','write','edit','grep','glob','bash','skill','question','webfetch'}
                    name=part.get('tool'); known=type(name) is str and name in allowed
                    label=('Tool: '+name) if known else 'Tool action'
                    label_basis='reported_tool_name' if known else 'generic'
                    first_activity = first_terminal = False
                    if phase in ('started', 'terminal'):
                        # Inspect proof versions of bounded prior revisions for
                        # this indexed Archive part without reading old content.
                        prior_versions = conn.execute(
                            """SELECT CASE WHEN length(CAST(qualified_tool_json AS BLOB)) <= 16384
                                           AND json_valid(qualified_tool_json)
                                    THEN json_extract(qualified_tool_json, '$.version') END AS proof_version
                               FROM conversation_parts
                               WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? AND revision<?
                               ORDER BY revision LIMIT 65""",
                            (account, chat, row['actor_id'], row['message_id'], row['part_id'], row['revision']),
                        ).fetchall()
                        if len(prior_versions) <= 64:
                            versions = [old['proof_version'] for old in prior_versions]
                            first_activity = not any(version in (1, 2) for version in versions)
                            first_terminal = phase == 'terminal' and 1 not in versions
                        else:
                            novelty_bounded = False
                    items.append({'eventId': fact['event_id'], 'sourceRef': fact['source_ref'],
                        'sourceSequence': row['event_sequence'], 'sourceRevision': row['revision'],
                        'runtimeGeneration': row['runtime_generation'],
                        'sessionId': session, 'actorId': row['actor_id'],
                        'executionRevision': evidence['executionRevision'],'callId':evidence['callID'],
                        'rootOperationId': evidence['rootOperationID'], 'state': phase, 'outcome':outcome,
                        'label': label, 'labelBasis':label_basis, 'coverage': coverage,
                        'firstActivity': first_activity, 'firstTerminal': first_terminal,
                        **({'activitySequence':evidence['sequence']} if phase!='terminal' else {})})
                except (ValueError, TypeError, KeyError, UnicodeError): continue
            payload = {'scope':scope, 'seq':seq, 'anchor':anchor(seq)}
            data = base64.urlsafe_b64encode(_json(payload).encode()).decode().rstrip('=')
            token = data + '.' + hmac.new(self.cursor_secret,data.encode(),hashlib.sha256).hexdigest()
            return {'state': state, 'items': items, 'cursor': token,
                    'coverage': {'activity': 'qualified_activity_and_terminal', 'usage': 'unavailable', 'retention': 'gap_detection_partial',
                                 'novelty': 'bounded_part_revisions' if novelty_bounded else 'older_revision_bound_reached'}}

    @staticmethod
    def _ref(owner, row):
        return {"authority": "conversation_archive", "session_handle": identity_handle(owner, "session_id", row["chat_id"]),
                "actor_id": row["actor_id"], "message_id": row["message_id"], "part_id": row["part_id"],
                "revision": row["revision"], "content_hash": row["content_hash"]}

    def _part(self, owner, row, include_bodies):
        item = {key: row[key] for key in ("actor_id", "message_id", "part_id", "revision", "runtime_generation",
                                          "event_sequence", "role", "part_type", "time_created", "time_updated")}
        item.update(source_ref=self._ref(owner, row), tool_name=row["tool_name"], status=row["observed_status"],
                    workspace_handle=identity_handle(owner, "workspace_id", row["event_workspace"]) if row["event_workspace"] else None,
                    body_available=bool(row["content_chars"]), body_truncated=False)
        if include_bodies:
            item["text"] = row["body"] or ""
            item["body_truncated"] = (row["content_chars"] or 0) > MAX_BODY_CHARS
        return item

    def _fact(self, owner, row, archive_owner=None):
        qualified = None
        if archive_owner is not None and "qualified_tool_json" in row.keys():
            try:
                raw = row["qualified_tool_json"]
                content = row["qualified_content"]
                value = json.loads(raw) if isinstance(raw, str) and len(raw) <= 8192 else None
                evidence = value.get("evidence") if isinstance(value, dict) else None
                if (set(value) == {"version", "account", "chat", "holder", "evidence"} and value["version"] == 1
                    and value["account"] == archive_owner and value["chat"] == row["chat_id"]
                    and isinstance(value["holder"], str) and value["holder"]
                    and isinstance(evidence, dict) and evidence.get("actorID") == row["actor_id"]
                    and evidence.get("messageID") == row["message_id"] and evidence.get("partID") == row["part_id"]
                    and evidence.get("contentSHA256") == row["content_hash"] and isinstance(content, str)
                    and hashlib.sha256(content.encode("utf-8")).hexdigest() == row["content_hash"]
                    and evidence.get("version") == 1 and evidence.get("algorithm") == "ed25519" and evidence.get("phase") == "terminal"
                    and isinstance(evidence.get("clockID"), str) and re.fullmatch(r"[0-9a-f]{32}", evidence["clockID"])
                    and isinstance(evidence.get("rootOperationID"), str) and evidence["rootOperationID"]
                    and all(type(evidence.get(k)) is int and 0 <= evidence[k] <= 9007199254740991 for k in ("startUS", "endUS", "executionRevision"))
                    and evidence["endUS"] >= evidence["startUS"]):
                    qualified = {"root_handle": identity_handle(owner, "operation_id", evidence["rootOperationID"]),
                        "clock_handle": identity_handle(owner, "operation_id", "tool-clock:" + evidence["clockID"]),
                        "join_handle": tool_usage_join_handle(owner, value["account"], value["chat"], value["holder"], evidence.get("sessionID"), evidence["rootOperationID"], evidence["actorID"], evidence["executionRevision"]),
                        "start_us": evidence["startUS"], "end_us": evidence["endUS"]}
            except (ValueError, TypeError, KeyError):
                pass
        status = row["observed_status"]
        event_kind = "tool_finished" if status in {"completed", "error", "failed", "cancelled"} or row["part_type"] in {"tool_result", "tool_output"} else "tool_started"
        ref = self._ref(owner, row)
        return {"schema": "open-clank.logging.evidence.v1", "event_id": _digest([owner, ref, event_kind, PRODUCER_REVISION]),
                "owner": owner, "source": "conversation_archive", "producer_revision": PRODUCER_REVISION,
                "runtime_generation": row["runtime_generation"], "event_sequence": row["event_sequence"],
                "event_time_ms": row["time_updated"], "session_id": row["chat_id"],
                "workspace_id": row["event_workspace"], "actor_id": row["actor_id"], "actor_kind": "foreground" if row["actor_id"] == "main" else "agent",
                "parent_id": row["parent_id"], "operation_id": row["operation_id"], "attempt_id": row["attempt_id"],
                "root_operation_id": row["root_operation_id"], "event_kind": event_kind, "status": status,
                "tool_name": row["tool_name"] if row["tool_name_type"] == "text" else None, "skill_name": row["skill_name"],
                "started_ms": row["tool_start_ms"], "completed_ms": row["tool_end_ms"],
                "duration_ms": (row["tool_end_ms"] - row["tool_start_ms"] if isinstance(row["tool_start_ms"], (int, float)) and
                                isinstance(row["tool_end_ms"], (int, float)) and row["tool_end_ms"] >= row["tool_start_ms"] else None),
                "terminal": event_kind == "tool_finished", "source_ref": ref, "qualified_tool": qualified,
                "coverage": "reported" if status else "partial", "reason": None if status else "tool_status_unavailable"}

    def get_part(self, owner, source_ref, *, offset=0, length=MAX_BODY_CHARS, cancel_event=None, deadline=None):
        owner = self._owner(owner)
        if not isinstance(source_ref, dict) or source_ref.get("authority") != "conversation_archive":
            raise LoggingError("invalid_source_ref")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (offset, length)) or offset < 0 or not 1 <= length <= MAX_BODY_CHARS:
            raise LoggingError("invalid_limit")
        chat_id = self.resolve_session(owner, source_ref.get("session_handle"), cancel_event=cancel_event, deadline=deadline)
        keys = ("actor_id", "message_id", "part_id", "revision", "content_hash")
        if any(source_ref.get(k) in (None, "") for k in keys):
            raise LoggingError("invalid_source_ref")
        with self._read(cancel_event, deadline) as conn:
            row = conn.execute("SELECT content_hash,revision,tombstone,length(content) AS total_chars,substr(content,?,?) AS text "
                               "FROM conversation_parts p WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? "
                               "AND NOT EXISTS (SELECT 1 FROM conversation_archive_deleted_chats erased WHERE erased.owner=p.owner AND erased.chat_id=p.chat_id) "
                               "ORDER BY revision DESC LIMIT 1",
                               (offset + 1, length, self._archive_owner(owner), chat_id, source_ref["actor_id"], source_ref["message_id"], source_ref["part_id"])).fetchone()
            if row is None:
                raise LoggingError("not_found")
            if row["revision"] != source_ref["revision"] or row["content_hash"] != source_ref["content_hash"] or row["tombstone"]:
                raise LoggingError("stale_cursor", "Part revision changed; reload its source reference")
            return {"schema": SCHEMA, "owner_scope": owner_scope(owner), "source_ref": source_ref,
                    "text": row["text"] or "", "offset": offset, "next_offset": offset + len(row["text"] or "") if
                    offset + len(row["text"] or "") < (row["total_chars"] or 0) else None,
                    "offset_unit": "unicode_codepoints", "total_chars": row["total_chars"] or 0}

    def actor_tree(self, owner, handle, *, cancel_event=None, deadline=None):
        owner = self._owner(owner)
        if self.session_factory is None:
            return {"items": [], "coverage": {"state": "unavailable", "reason": "actor_accounting_unavailable"}}
        sid = self.resolve_session(owner, handle, cancel_event=cancel_event, deadline=deadline)
        from core.database import TurnActor, AgentTurn, ChatMessage, Session as DbSession
        db = self.session_factory()
        try:
            q = (db.query(TurnActor).join(AgentTurn, TurnActor.root_turn_id == AgentTurn.root_turn_id)
                 .join(ChatMessage, ChatMessage.id == AgentTurn.root_turn_id)
                 .join(DbSession, DbSession.id == ChatMessage.session_id)
                 .filter(DbSession.owner == owner, DbSession.id == sid)
                 .order_by(TurnActor.created_at, TurnActor.actor_id).limit(MAX_PAGE + 1))
            rows = q.all()
            items = []
            for r in rows[:MAX_PAGE]:
                _check(cancel_event, deadline)
                items.append({"actor_id": r.actor_id, "parent_actor_id": r.parent_actor_id,
                              "root_message_id": r.root_turn_id, "mode": r.mode, "agent": r.agent,
                              "status": r.status, "outcome": r.outcome, "background": r.background,
                              "lifecycle": r.lifecycle, "source_revision": r.source_revision,
                              "requested_model": r.requested_model, "actual_model": r.effective_model,
                              "created_at": str(r.created_at), "completed_at": str(r.completed_at) if r.completed_at else None,
                              "source_ref": {"authority": "turn_actor", "session_handle": handle,
                                             "root_message_id": r.root_turn_id, "actor_id": r.actor_id,
                                             "revision": r.source_revision}})
            return {"items": items, "coverage": {"state": "partial" if len(rows) > MAX_PAGE else "reported" if rows else "unavailable",
                                                 "reason": "actor_limit" if len(rows) > MAX_PAGE else None if rows else "no_actor_evidence"}}
        finally:
            db.close()

    def tool_facts(self, owner, filters=None, *, cancel_event=None, deadline=None):
        items, cursor, revision = [], None, None
        while len(items) < MAX_FACTS:
            page = self.parts(owner, filters, cursor=cursor, limit=min(MAX_PAGE, MAX_FACTS-len(items)),
                              include_bodies=False, facts_only=True, cancel_event=cancel_event, deadline=deadline)
            items.extend(page["items"])
            cursor = page["page"]["next_cursor"]
            revision = page["coverage"]["source_revision"]
            if cursor is None:
                break
        return {"schema": "open-clank.logging.evidence.v1", "facts": items,
                "coverage": {"state": "partial" if cursor else "reported", "reason": "fact_limit" if cursor else None,
                             "source_revision": revision, "producer_revision": PRODUCER_REVISION}, "next_cursor": cursor}

    def export(self, owner, filters=None, *, include_bodies=False, cursor=None, limit=100, query_text=None, cancel_event=None, deadline=None):
        result = self.parts(owner, filters, query_text=query_text, cursor=cursor, limit=limit, include_bodies=include_bodies,
                            cancel_event=cancel_event, deadline=deadline)
        result.update(export={"format": "json", "include_bodies": bool(include_bodies), "local_only": True,
                              "query_text": query_text, "filters": result.get("filters", normalize_filters(filters)), "timezone": (filters or {}).get("timezone", "UTC"),
                              "formula_revision": PRODUCER_REVISION})
        return result

    @staticmethod
    def _failure_tool_signatures(owner, facts, coverage):
        # Exact canonical metadata only, never parse error text or expose raw
        # names. These owner-scoped signatures are comparison keys, not grants.
        failed = [fact for fact in facts if fact.get("terminal") and fact.get("status") in {"failed", "error"}]
        names = [fact.get("tool_name") for fact in failed]
        valid = [name for name in names if isinstance(name, str) and 0 < len(name) <= 256
                 and not any(ord(ch) < 32 for ch in name)]
        partial = coverage.get("state") != "reported" or len(valid) != len(names)
        return {"handles": sorted({_digest(["openclank.diagnostic-tool.v1", owner_scope(owner), name]) for name in valid}),
                "state": "partial" if partial else "reported",
                "observed_failures": len(failed), "unnamed_failures": len(names) - len(valid),
                "source_revision": coverage.get("source_revision")}

    SIMILAR_WINDOW = 50

    def similar_sessions(self, owner, handle, *, cancel_event=None, deadline=None):
        """Explicit, bounded comparison of ONE selected session with the same owner's
        most recent sessions. Closed fields only; never returns transcript text."""
        owner = self._owner(owner)
        self.resolve_session(owner, handle, cancel_event=cancel_event, deadline=deadline)
        found = self.sessions(owner, {"session_id": handle}, limit=1, cancel_event=cancel_event, deadline=deadline)["items"]
        if not found:
            raise LoggingError("not_found", "Session unavailable")
        target = found[0]
        window = self.sessions(owner, {}, limit=self.SIMILAR_WINDOW, sort="latest", direction="desc",
                               cancel_event=cancel_event, deadline=deadline)

        def signature(item):
            tools = item.get("tools") or {}
            return {"provider_route": {i["handle"] for i in item.get("provider_identities") or []},
                    "model": {i["handle"] for i in item.get("model_identities") or []},
                    "tool_failure": bool(tools.get("failures")), "tool_cancelled": bool(tools.get("cancelled")),
                    "failing_tool_name": set((item.get("failure_tool_signatures") or {}).get("handles") or [])}
        want = signature(target)
        items = []
        for candidate in window["items"]:
            _check(cancel_event, deadline)
            if candidate["handle"] == handle:
                continue
            have = signature(candidate)
            matched = [f for f in ("provider_route", "model", "failing_tool_name") if want[f] and want[f] & have[f]]
            matched += [f for f in ("tool_failure", "tool_cancelled") if want[f] and have[f]]
            if matched:
                items.append({"handle": candidate["handle"], "last_activity_ms": candidate.get("last_activity_ms"),
                              "matched_fields": matched})
        counts = {}
        for item in items:
            for field in item["matched_fields"]:
                counts[field] = counts.get(field, 0) + 1
        more = bool(window["page"].get("next_cursor"))
        return {"schema": SCHEMA, "owner_scope": owner_scope(owner), "session_handle": handle, "items": items,
                "counts": counts, "compared_sessions": len(window["items"]),
                "bounds": {"max_sessions": self.SIMILAR_WINDOW, "pages": 1, "order": "most_recent_activity"},
                "unavailable_fields": {f: "not_in_ordinary_session_rows" for f in ("error_class_stage", "context_outcome")},
                "field_coverage": {"failing_tool_name": target.get("failure_tool_signatures") or {"state":"unavailable"}},
                "stored": False, "coverage": {"state": "partial" if more else "reported", "reason": "recent_window" if more else None}}

    def issue_draft(self, owner, handle, *, cancel_event=None, deadline=None):
        """Local Markdown draft from the export skeleton scope (metadata only, no bodies).
        Nothing is stored or sent; the caller edits, copies or downloads it."""
        similar = self.similar_sessions(owner, handle, cancel_event=cancel_event, deadline=deadline)
        item = self.sessions(self._owner(owner), {"session_id": handle}, limit=1, cancel_event=cancel_event, deadline=deadline)["items"][0]
        tools = item.get("tools") or {}
        labels = lambda key: ", ".join(i["label"] for i in item.get(key) or []) or "not observed"
        lines = ["# Issue draft (review before sharing)", "",
                 f"Session handle: `{handle}`", f"Producer revision: {PRODUCER_REVISION}", "",
                 "## Observed facts", f"- Provider route: {labels('provider_identities')}", f"- Model: {labels('model_identities')}",
                 f"- Messages: {item['message_count']['value']} · parts: {item['part_count']} · actors: {item['actor_count']}",
                 f"- Tool calls: {tools.get('calls', 0)} · failures: {tools.get('failures', 0)} · cancelled: {tools.get('cancelled', 0)}", "",
                 "## Similar recent sessions", f"- Compared {similar['compared_sessions']} of the most recent sessions; matches: {len(similar['items'])}"]
        lines += [f"- {field}: {n}" for field, n in sorted(similar["counts"].items())]
        lines += ["", "## Not included", "- Conversation text, tool inputs and outputs, and session names are omitted.",
                  "- Failing-tool matches use opaque exact-name signatures from observed canonical failures; missing/partial names remain unknown.",
                  "- Error class/stage and context outcome are unavailable in ordinary rows.", "",
                  "## What happened", "(describe the problem here)", ""]
        return {"schema": SCHEMA, "session_handle": handle, "draft_markdown": "\n".join(lines), "stored": False, "sent": False}

    def prune_preview(self, owner, payload=None, *, cancel_event=None, deadline=None):
        owner = self._owner(owner)
        payload = dict(payload or {})
        if payload.get("target", "ordinary_indexes") != "ordinary_indexes":
            raise LoggingError("advanced_prune_required", "Advanced bodies/metadata use the capture-store preview")
        with self._read(cancel_event, deadline) as conn:
            revision = self._revision(conn, owner)
        # The ordinary layer has no disposable duplicate index. Archive and
        # session persistence cannot be pruned as optional observability.
        preview = {"target": "ordinary_indexes", "source_revision": revision, "removable_records": 0,
                   "removable_bytes": 0, "affected_authorities": [], "protected_authorities": ["conversation_archive", "saved_chat", "lore", "semantic_memory"]}
        return {"schema": SCHEMA, "owner_scope": owner_scope(owner), "preview": preview,
                "preview_id": self._cursor(owner, {"kind": "prune", "target": "ordinary_indexes"}, revision, 0)}

    def prune_apply(self, owner, preview_id, *, cancel_event=None, deadline=None):
        owner = self._owner(owner)
        if not preview_id:
            raise LoggingError("invalid_cursor")
        with self._read(cancel_event, deadline) as conn:
            revision = self._revision(conn, owner)
            self._position(preview_id, owner, {"kind": "prune", "target": "ordinary_indexes"}, revision)
        return {"schema": SCHEMA, "owner_scope": owner_scope(owner), "removed_records": 0,
                "removed_bytes": 0, "reason": "no_disposable_ordinary_index"}


def semantic_search_readiness(*, owner, payload, index=None, cancel_event=None, deadline=None):
    """Read the existing owner-scoped index health without embedding a query.

    The canonical document index has no ConversationArchive source producer.
    Do not substitute document or extracted-memory hits for transcript evidence.
    """
    owner = LoggingProjection._owner(owner)
    filters = normalize_filters(payload.get("filters"))
    query = payload.get("query", "")
    if not isinstance(query, str) or not query.strip() or len(query) > 512:
        raise LoggingError("invalid_query")
    LoggingProjection._limit(payload.get("limit", 50))
    _check(cancel_event, deadline)
    try:
        health = index.get_stats(owner=owner) if index is not None else {"healthy": False}
    except Exception:
        health = {"healthy": False}
    _check(cancel_event, deadline)
    readiness = {"backend": health.get("backend"), "healthy": bool(health.get("healthy")),
                 "index_health": health.get("index_health", {}), "generation_states": health.get("generation_states", {}),
                 "publication_count": health.get("publication_count", 0),
                 "conversation_source": {"state": "unavailable", "reason": "canonical_archive_source_producer_required"},
                 "query_execution": "not_dispatched"}
    return {"schema": SCHEMA, "owner_scope": owner_scope(owner), "items": [], "filters": filters,
            "page": {"next_cursor": None, "limit": payload.get("limit", 50)},
            "coverage": {"state": "unavailable", "reason": "canonical_archive_source_producer_required" if readiness["healthy"] else "semantic_index_unavailable",
                         "authority": "memery_canonical_index", "readiness": readiness,
                         "setup": {"label": "Review index and model setup", "settings_tab": "services",
                                   "message": "Semantic conversation search needs a canonical archive source binding in the existing owner-scoped index and an explicitly configured embedding model with a published generation. Review provider/index setup; keyword search remains available. Document and extracted-memory matches are not transcript evidence."}}}


def resolve_archive_owner(owner):
    # Never initialize an absent archive as a side effect of a read projection.
    from src.openclank.conversation_archive import default_db_path, get_conversation_archive
    if not Path(default_db_path()).is_file():
        raise LoggingError("archive_unavailable", "Canonical conversation archive is unavailable")
    return get_conversation_archive().resolve_owner(owner)


def iter_tool_facts(owner, filters=None, *, cancel_event=None, deadline=None, projection=None):
    """L02's bounded content-free handoff; no implicit archive creation."""
    if projection is None:
        from src.openclank.conversation_archive import default_db_path
        projection = LoggingProjection(default_db_path(), archive_owner_resolver=resolve_archive_owner)
    return projection.tool_facts(owner, filters, cancel_event=cancel_event, deadline=deadline)


def clankshop_usage(owner, account, chat, workspace, session, holder, actor_runs, *, limit=500):
    """Bounded replacement snapshot over the existing admitted Stats owner.

    Native attribution requires current actor and provider-binding evidence;
    legacy or ambiguous observations remain unassigned.
    """
    from core.database import SessionLocal
    from core.stats_models import StatsEvent
    from core.provider_models import ProviderOperationBinding
    from services.stats.ledger import project_admitted_events
    if not all(isinstance(v, str) and v for v in (owner, account, chat, workspace, session, holder)) or not isinstance(actor_runs, tuple) or not 1 <= limit <= 500:
        raise ValueError('invalid usage scope')
    db = SessionLocal()
    try:
        rows = (db.query(StatsEvent).filter(StatsEvent.owner == owner, StatsEvent.session_id == chat,
                StatsEvent.workspace_id == workspace, StatsEvent.event_kind != 'context_receipt', StatsEvent.incognito.is_(False))
                .order_by(StatsEvent.event_time.desc(), StatsEvent.id.desc()).limit(limit+1).all())
        if len(rows) > limit:
            return {'state':'unavailable','reason':'usage_bound_exceeded','items':[]}
        projection = project_admitted_events(rows)
        receipts = (db.query(StatsEvent).filter(StatsEvent.owner == owner, StatsEvent.session_id == chat,
                    StatsEvent.event_kind == 'context_receipt', StatsEvent.incognito.is_(False)).limit(limit+1).all())
        if len(receipts) > limit:
            receipts = []  # attribution unavailable; metrics remain visible as unassigned
        def attribution_from_receipt(receipt):
            payload = (receipt.event_metadata or {}).get('context_receipt') or {}
            attr = payload.get('tool_usage_attribution') if isinstance(payload, dict) else None
            if not isinstance(attr, dict): return None
            identity = {'sessionID':attr.get('session'),'rootOperationID':attr.get('root'),
                        'actorID':attr.get('actor'),'executionRevision':attr.get('execution')}
            if (payload.get('identity') != identity or payload.get('binding_id') != attr.get('binding')
                or payload.get('binding_revision') != attr.get('binding_revision') or payload.get('instance_id') != attr.get('holder')): return None
            return attr
        receipt_attrs = [(receipt, attribution_from_receipt(receipt)) for receipt in receipts]
        attrs = [(event.event_metadata or {}).get('tool_usage_attribution') for event in projection.events]
        ids = {a.get('binding') for a in attrs + [a for _,a in receipt_attrs] if isinstance(a,dict) and isinstance(a.get('binding'),str) and a['binding']}
        bindings = {b.id:b for b in db.query(ProviderOperationBinding).filter(ProviderOperationBinding.owner == owner,
                    ProviderOperationBinding.id.in_(ids)).limit(limit+1).all()} if ids else {}
        current = set(actor_runs)
        def valid(attr, event):
            fields = {'version','account','chat','holder','session','root','actor','execution','binding','binding_revision'}
            if not isinstance(attr,dict) or set(attr) != fields or attr.get('version') != 1: return None
            if (not all(isinstance(attr.get(k), str) and attr[k] for k in ('account','chat','holder','session','root','actor','binding'))
                or type(attr.get('execution')) is not int or attr['execution'] < 0
                or type(attr.get('binding_revision')) is not int or attr['binding_revision'] < 1): return None
            if (attr['account'] != account or attr['chat'] != chat or attr['holder'] != holder or attr['session'] != session
                or (attr['session'],attr['actor'],attr['execution']) not in current
                or event.session_id != chat or event.root_operation_id != attr['root']
                or (event.event_metadata or {}).get('instance_id') != holder): return None
            b = bindings.get(attr['binding'])
            if (b is None or b.state != 'committed' or b.revision != attr['binding_revision']
                or b.root_operation_id != attr['root'] or b.engine_session_id != attr['session']
                or b.actor_id != attr['actor'] or b.execution_revision != attr['execution']): return None
            return {'sessionId':attr['session'],'actorId':attr['actor'],'executionRevision':attr['execution']}
        items = []
        for event in projection.events:
            metadata = event.event_metadata or {}
            provenance = metadata.get('metric_provenance') or {}
            namespace = ('attempt' if event.attempt_id else 'operation' if event.observation_scope == 'operation'
                         else 'root' if event.root_operation_id and event.observation_scope == 'root' else 'message')
            identifier = (event.attempt_id if namespace == 'attempt' else event.operation_id if namespace == 'operation'
                          else event.root_operation_id if namespace == 'root' else event.replay_key)
            key = _digest([owner,chat,metadata.get('instance_id'),event.root_operation_id,event.operation_id,namespace,identifier])
            metrics = {}
            for name in ('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens','reasoning_tokens'):
                value, state = getattr(event,name,None), getattr(event,name+'_state','unavailable')
                metrics[name] = {'value': value if type(value) is int and value >= 0 and state == 'reported' else None, 'state':state}
            revision = _digest([metrics,provenance])
            actor = None
            attr = metadata.get('tool_usage_attribution')
            if event.actor_kind == 'subagent' and isinstance(attr,dict) and event.operation_id == 'actor-binding:' + str(attr.get('binding') or ''):
                actor = valid(attr,event)
            elif event.actor_kind == 'foreground' and event.source == 'acp_final_usage':
                matches = [valid(a,event) for receipt,a in receipt_attrs if a and a.get('actor') == 'main'
                           and receipt.operation_id == event.operation_id and receipt.root_operation_id == event.root_operation_id]
                matches = [match for match in matches if match]
                if len(matches) == 1: actor = matches[0]
            items.append({'identity':key,'revision':revision,'attribution':'actor' if actor else 'unassigned',
                          **({'actorRef':actor} if actor else {}), 'metrics':metrics})
        return {'state':'snapshot','coverage':projection.coverage,'items':items}
    finally:
        db.close()


# One process-scoped signing authority; no archive or additional state is created.
from functools import lru_cache as _lru_cache

@_lru_cache(maxsize=1)
def clankshop_logging_projection():
    from src.openclank.conversation_archive import default_db_path
    return LoggingProjection(default_db_path())
