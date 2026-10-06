"""Read-only, bounded logging projections; canonical archives remain the authority.

No constructor imports, migrates, backfills or creates an archive. Connections
use SQLite read-only mode, and each page is a coherent read transaction.
"""
from __future__ import annotations

import base64
import hashlib
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


TOOL = f"COALESCE({_field('tool')}, {_field('tool_name')}, {_field('function.name')}, {_field('name')})"
STATUS = f"COALESCE({_field('state.status')}, {_field('status')})"
LATEST = """NOT EXISTS (SELECT 1 FROM conversation_parts newer
 WHERE newer.owner=p.owner AND newer.chat_id=p.chat_id AND newer.actor_id=p.actor_id
 AND newer.message_id=p.message_id AND newer.part_id=p.part_id AND newer.revision>p.revision)"""
COLUMNS = f"""p.seq,p.chat_id,p.actor_id,p.message_id,p.part_id,p.revision,
p.runtime_generation,p.event_sequence,p.event_workspace,p.event_project,p.role,p.part_type,
p.content_hash,p.time_created,p.time_updated,p.tombstone,
{TOOL} AS tool_name, {STATUS} AS observed_status,
{_field('state.time.start')} AS tool_start_ms, {_field('state.time.end')} AS tool_end_ms,
{_field('skill')} AS skill_name, {_field('parentID')} AS parent_id,
{_field('operation_id')} AS operation_id, {_field('attempt_id')} AS attempt_id,
{_field('root_operation_id')} AS root_operation_id,
length(p.content) AS content_chars"""


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
                                    "AND p.part_type IN ('tool','tool_call','tool_result','tool_output') "
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
                        "tools": cohort(by_session.get(r["chat_id"], []), tool_coverage)}
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
            rows = conn.execute(f"SELECT {COLUMNS}{body} FROM conversation_parts p WHERE {where} "
                                "ORDER BY (SELECT MIN(m.seq) FROM conversation_parts m WHERE m.owner=p.owner AND m.chat_id=p.chat_id "
                                "AND m.actor_id=p.actor_id AND m.message_id=p.message_id), p.event_sequence ASC, "
                                "(SELECT MIN(a.seq) FROM conversation_parts a WHERE a.owner=p.owner AND a.chat_id=p.chat_id "
                                "AND a.actor_id=p.actor_id AND a.message_id=p.message_id AND a.part_id=p.part_id) "
                                "LIMIT ? OFFSET ?", [*args, limit + 1, offset]).fetchall()
            items, size = [], 0
            for row in rows[:limit]:
                _check(cancel_event, deadline)
                item = self._fact(owner, row) if facts_only else self._part(owner, row, include_bodies)
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

    def _fact(self, owner, row):
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
                "tool_name": row["tool_name"], "skill_name": row["skill_name"],
                "started_ms": row["tool_start_ms"], "completed_ms": row["tool_end_ms"],
                "duration_ms": (row["tool_end_ms"] - row["tool_start_ms"] if isinstance(row["tool_start_ms"], (int, float)) and
                                isinstance(row["tool_end_ms"], (int, float)) and row["tool_end_ms"] >= row["tool_start_ms"] else None),
                "terminal": event_kind == "tool_finished", "source_ref": ref,
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
                         "authority": "frankenmemory_canonical_index", "readiness": readiness,
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
