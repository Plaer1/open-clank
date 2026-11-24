"""Background scheduler for ScheduledTask execution."""

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Tuple

from core.auth import RESERVED_USERNAMES
from src.task_action_policy import (
    ADMIN_ONLY_TASK_ACTIONS,
    is_admin_only_task_action,
    owner_has_admin_task_privileges,
)

logger = logging.getLogger(__name__)


class TaskWaiting(BaseException):
    """Raised when a task run ends while a durable wait request is pending.

    The run stays in ``waiting`` status so the persisted request can be
    answered and the run resumed. Not an error — the wait is resumable pending
    work.
    """

    def __init__(self, run_id: str, wait_id: str | None = None):
        super().__init__(f"Task run {run_id} is waiting for user answer")
        self.run_id = run_id
        self.wait_id = wait_id


def _utcnow() -> datetime:
    """Return naive UTC for task DB fields without using deprecated APIs."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Shell/file tools a scheduled task's agent should be offered by default,
# mirroring the chat agent (where these are on unless a privilege or global
# setting turns them off). The RAG tool selector + ASSISTANT_ALWAYS_AVAILABLE
# never include bash/python, so on a host with an empty/degraded tool-embedding
# index a task could not run shell or Python even for an admin owner. Offering
# them here is safe: the stored task allowlist and current owner/global policy
# are intersected at the strict Agent door before ACP sees any schema.
TASK_DEFAULT_SHELL_TOOLS = frozenset({
    "bash", "python", "read_file", "write_file", "edit_file",
    "grep", "glob", "ls", "get_workspace",
})

# The exact pre-Copal personal-assistant seed.  Default migrations are allowed
# to append native Copal tools only when this list is unchanged; any user edit
# (including order) remains authoritative.
HISTORICAL_DEFAULT_ASSISTANT_TOOLS = (
    "manage_calendar", "manage_notes", "manage_tasks", "manage_memory",
    "list_email_accounts", "list_emails", "read_email", "send_email",
    "reply_to_email", "archive_email", "mark_email_read", "delete_email",
    "resolve_contact", "search_chats", "web_search", "web_fetch",
    "read_file", "create_document", "update_document", "edit_document",
    "generate_image", "trigger_research", "download_model", "serve_model",
    "list_served_models", "stop_served_model", "edit_image",
)
COPAL_DEFAULT_ASSISTANT_TOOLS = ("read_copal", "manage_copal")




# Mechanical operating rules for the personal assistant's scheduled work.
# Identity ruling R15: WHO the assistant is (voice, name) comes from the
# synced default persona (CrewMember.personality, kept in step with the
# persona store by src/default_persona.py); HOW it must operate stays here
# as code. Appended at run time by _compose_assistant_prompt().
ASSISTANT_TASK_FRAMING = (
    "Default to English. Only match the other language when replying to a non-English email.\n\n"

    "CORE RULE: You MUST use your tools to take action — do not describe what you would do. "
    "Never say 'I would check your calendar' — actually call manage_calendar. "
    "Never say 'I can look that up' — actually call web_search or search_chats. "
    "If you have a tool for it, use it. No hypotheticals, no promises, only actions and results.\n\n"

    "DECISION FRAMEWORK — follow these rules, not just tool descriptions:\n\n"

    "CONTEXT GATHERING (before any response involving a specific person):\n"
    "1. resolve_contact if you only have a name and need their email\n"
    "2. search_chats for recent conversations mentioning them or their topic\n"
    "3. manage_memory to check stored facts about them\n"
    "Skip steps you already have answers for. Don't search for the user themselves.\n\n"

    "EMAIL HANDLING:\n"
    "- If a document is open in the editor, that IS the email. Use update_document to write the reply.\n"
    "- BEFORE drafting any reply: gather context (steps above) about the sender and topic.\n"
    "- When an email mentions a date/meeting: check calendar for conflicts, add if clear.\n"
    "- When an email asks a question you can't answer from context: say so honestly. Never fabricate.\n"
    "- Skip automated/marketing emails in check-ins. Only surface human-sent, actionable ones.\n"
    "- Never duplicate information the user already saw in a previous check-in.\n\n"

    "ESCALATION LADDER (when you need info you don't have):\n"
    "1. search_chats (fast, free)\n"
    "2. manage_memory (fast, free)\n"
    "3. web_search (medium cost)\n"
    "4. trigger_research (expensive, async — only for complex multi-source questions)\n"
    "Stop as soon as you have a sufficient answer.\n\n"

    "'SEND TO [NAME]' FLOW:\n"
    "1. resolve_contact to find their email\n"
    "2. If a document is open, use its content as the body\n"
    "3. Draft the email in a document (create_document with language='email')\n"
    "4. Tell the user to review — NEVER auto-send\n\n"

    "SELF-IMPROVEMENT — use manage_memory constantly:\n"
    "- When the user corrects you, IMMEDIATELY store the correction as a memory.\n"
    "- After every check-in or task, store new facts you learned (contacts, preferences, patterns).\n"
    "- Before responding about a person or topic, search_chats and manage_memory FIRST.\n"
    "- Build knowledge over time: who people are, what projects are active, how the user likes things done.\n"
    "- If something failed or you got corrected, store WHY so you never repeat it.\n"
    "- When you figure out a multi-step workflow that works, save it as a SKILL using manage_skills.\n"
    "  A skill is a reusable procedure. Next time, recall the skill instead of figuring it out again.\n"
    "- Before starting a complex task, check manage_skills for an existing procedure.\n\n"

    "AUTONOMY RULES:\n"
    "- Auto-add calendar events from clear meeting invitations (mention what you added)\n"
    "- Auto-draft email replies (cached for when user clicks Reply)\n"
    "- NEVER send emails without explicit user instruction\n"
    "- NEVER delete anything without explicit instruction\n"
    "- If uncertain, ask rather than guess"
)


def _compose_assistant_prompt(personality: str) -> str:
    """Assistant system prompt = persona voice + mechanical framing (R15).

    Legacy CrewMember rows seeded before the split carry the full
    operational blob inside `personality`; those pass through untouched.
    """
    voice = (personality or "").strip()
    if "CORE RULE:" in voice:
        return voice
    if not voice:
        return ASSISTANT_TASK_FRAMING
    return f"{voice}\n\n{ASSISTANT_TASK_FRAMING}"


def compose_task_relevant_tools(rag_tools, assistant_always, disabled_tools):
    """Compose the relevant-tools set offered to a scheduled task's agent.

    Unions the RAG-retrieved tools, the assistant's always-available set, and
    the default shell/file group, then removes anything the task's crew
    explicitly disabled via its `enabled_tools` allowlist. Per-owner admin
    owner/global gating and the stored task allowlist are applied later at the
    strict Agent door.
    """
    tools = set(rag_tools) | set(assistant_always) | set(TASK_DEFAULT_SHELL_TOOLS)
    if disabled_tools:
        tools -= set(disabled_tools)
    return tools


# ── Shared TTL cache (singleflight) ────────────────────────────────────────
# Multiple scheduled tasks firing in the same minute often need the same
# external data (Miniflux unreads, MCP tool snapshots, etc.). This cache
# deduplicates those fetches — in-flight requests for the same key await the
# same underlying coroutine, and completed results are reused until TTL expiry.
_shared_cache: Dict[Tuple, Tuple[float, Any]] = {}
_shared_cache_pending: Dict[Tuple, asyncio.Future] = {}
_shared_cache_lock = asyncio.Lock()


def _owner_shared_cache_key(owner: object, *parts: object) -> Tuple:
    """Return the canonical, owner-addressable scheduler cache key."""
    return ("owner", str(owner or "").strip().lower(), *parts)


def _shared_cache_key_owner(key: object) -> str | None:
    if not isinstance(key, tuple) or len(key) < 2 or key[0] != "owner":
        return None
    owner = str(key[1] or "").strip().lower()
    return owner or None


def _cache_admitted(admit: Callable[[], bool] | None) -> bool:
    if admit is None:
        return True
    try:
        return bool(admit())
    except Exception:
        return False


async def _cached(
    key: Tuple,
    ttl: float,
    fetch: Callable[[], Awaitable[Any]],
    *,
    admit: Callable[[], bool] | None = None,
) -> Any:
    """Return a cached result for `key` if fresh, else call `fetch()` and store.

    Concurrent callers for the same missing key share one `fetch()` call.
    Exceptions propagate to every waiter and do not poison the cache.
    """
    now = time.monotonic()
    async with _shared_cache_lock:
        entry = _shared_cache.get(key)
        if entry and entry[0] > now:
            if not _cache_admitted(admit):
                raise RuntimeError("scheduler cache owner is fenced")
            return entry[1]
        fut = _shared_cache_pending.get(key)
        if fut is not None:
            pending = fut
            owner = False
        else:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            _shared_cache_pending[key] = fut
            pending = fut
            owner = True
    if not owner:
        # A cancelled waiter must not cancel the shared Future that belongs to
        # the fetch owner and any other concurrent scheduled task.
        value = await asyncio.shield(pending)
        if not _cache_admitted(admit):
            raise RuntimeError("scheduler cache owner is fenced")
        return value
    try:
        val = await fetch()
        if not _cache_admitted(admit):
            raise RuntimeError("scheduler cache owner is fenced")
        async with _shared_cache_lock:
            # The lifecycle fence can appear while this coroutine is waiting
            # to publish under the lock.  Recheck at the exact write boundary
            # so a late singleflight result cannot resurrect an old owner key.
            if not _cache_admitted(admit):
                raise RuntimeError("scheduler cache owner is fenced")
            _shared_cache[key] = (time.monotonic() + ttl, val)
            _shared_cache_pending.pop(key, None)
        if not pending.done():
            pending.set_result(val)
        return val
    except BaseException as e:
        async with _shared_cache_lock:
            if _shared_cache_pending.get(key) is pending:
                _shared_cache_pending.pop(key, None)
        if not pending.done():
            if isinstance(e, asyncio.CancelledError):
                pending.cancel()
            else:
                pending.set_exception(e)
        raise


def compute_next_run(schedule: str, scheduled_time: str,
                     scheduled_day: int = None,
                     scheduled_date: datetime = None,
                     after: datetime = None,
                     cron_expression: str = None,
                     tz_name: str = None) -> datetime | None:
    """Compute the next run datetime (stored as naive UTC) based on schedule type.

    If `tz_name` is provided (IANA zone, e.g. "America/New_York"), `scheduled_time` /
    `scheduled_day` are interpreted as local wall-clock time in that zone and
    the result is converted to naive UTC for DB storage. If `tz_name` is None,
    the legacy behavior (`scheduled_time` interpreted as naive-UTC wall clock)
    is preserved so existing tasks don't shift.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        ZoneInfo = None

    tz = None
    if tz_name and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None

    # "now" used for comparisons. When tz is set we work entirely in local tz
    # and convert to UTC at the end. Otherwise we use naive UTC (legacy).
    if tz is not None:
        now_utc = after or _utcnow()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now = now_utc.astimezone(tz)
    else:
        now = after or _utcnow()

    def _to_utc_naive(dt: datetime) -> datetime:
        """Convert a tz-aware datetime to naive UTC for DB storage."""
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(timezone.utc).replace(tzinfo=None)

    if schedule == "cron" and cron_expression:
        try:
            from croniter import croniter
            cron = croniter(cron_expression, now)
            nxt = cron.get_next(datetime)
            if tz is not None and nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=tz)
            return _to_utc_naive(nxt) if tz is not None else nxt
        except Exception as e:
            logger.warning(f"Invalid cron expression '{cron_expression}': {e}")
            return None

    if schedule == "once":
        if scheduled_date and scheduled_date > (_to_utc_naive(now) if tz is not None else now):
            return scheduled_date
        return None

    if not scheduled_time:
        return None

    # Parse HH:MM — fail closed on malformed input (no colon, non-numeric,
    # out-of-range) the same way an invalid cron expression does above, so a
    # bad value like "9" or "9am" returns None instead of raising IndexError/
    # ValueError out of the create route (a 500) or the scheduler loop.
    parts = scheduled_time.split(":")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("hour/minute out of range")
    except (ValueError, IndexError):
        logger.warning(f"Invalid scheduled_time '{scheduled_time}'")
        return None

    if schedule == "daily":
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return _to_utc_naive(candidate) if tz is not None else candidate

    if schedule == "weekly":
        day = scheduled_day if scheduled_day is not None else 0  # 0=Monday
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = day - candidate.weekday()
        if days_ahead < 0 or (days_ahead == 0 and candidate <= now):
            days_ahead += 7
        candidate += timedelta(days=days_ahead)
        return _to_utc_naive(candidate) if tz is not None else candidate

    if schedule == "monthly":
        day = scheduled_day if scheduled_day is not None else 1
        try:
            candidate = now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            # Short month: clamp to its last day (mirrors the next-month
            # clamp below) instead of silently skipping the whole month.
            if now.month == 12:
                last = now.replace(year=now.year + 1, month=1, day=1) - timedelta(days=1)
            else:
                last = now.replace(month=now.month + 1, day=1) - timedelta(days=1)
            candidate = last.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            if now.month == 12:
                next_month = now.replace(year=now.year + 1, month=1, day=1)
            else:
                next_month = now.replace(month=now.month + 1, day=1)
            try:
                candidate = next_month.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
            except ValueError:
                if next_month.month == 12:
                    last = next_month.replace(year=next_month.year + 1, month=1, day=1) - timedelta(days=1)
                else:
                    last = next_month.replace(month=next_month.month + 1, day=1) - timedelta(days=1)
                candidate = last.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return _to_utc_naive(candidate) if tz is not None else candidate

    return None


def _resolve_task_timezone(db, task) -> str | None:
    """Look up the IANA timezone name for a task via its linked CrewMember, if any."""
    if not getattr(task, "crew_member_id", None):
        return None
    try:
        from core.database import CrewMember
        cm = db.query(CrewMember).filter(CrewMember.id == task.crew_member_id).first()
        if cm and cm.timezone:
            return cm.timezone
    except Exception:
        pass
    return None


# Built-in "housekeeping" tasks seeded for every owner, keyed by action.
# These are the canonical defaults — used both to seed and to revert a
# built-in task the user has altered. schedule "daily" uses scheduled_time;
# "cron" uses cron_expression.
HOUSEKEEPING_DEFAULTS = {
    "tidy_sessions":        {"name": "Chat Sessions Tidy",       "trigger_type": "event", "trigger_event": "session_created", "trigger_count": 5, "schedule": None, "scheduled_time": None, "cron_expression": None, "legacy_names": ["Tidy Chat Sessions"]},
    "consolidate_memory":   {"name": "Memory Tidy",              "trigger_type": "event", "trigger_event": "memory_added", "trigger_count": 5, "schedule": None, "scheduled_time": None, "cron_expression": None, "legacy_names": ["Tidy Memory"]},
    "tidy_research":        {"name": "Research Tidy",            "trigger_type": "event", "trigger_event": "research_completed", "trigger_count": 5, "schedule": None, "scheduled_time": None, "cron_expression": None, "legacy_names": ["Tidy Research"]},
    "summarize_emails":     {"name": "Email (Summary)",          "schedule": "cron",  "scheduled_time": None,    "cron_expression": "0 */2 * * *", "ship_paused": True, "legacy_names": ["Tidy Email (Summary)"]},
    "draft_email_replies":  {"name": "Email AI Auto Reply",      "schedule": "cron",  "scheduled_time": None,    "cron_expression": "0 */2 * * *", "ship_paused": True, "legacy_names": ["Tidy Email (Replies)", "AI Auto Reply"]},
    "email_auto_translate": {"name": "Email Auto Translate",     "schedule": "cron",  "scheduled_time": None,    "cron_expression": "0 */2 * * *", "ship_paused": True, "legacy_names": ["Auto-translate Emails", "Auto Translate Email"]},
    "extract_email_events": {"name": "Email Calendar Events",    "schedule": "cron",  "scheduled_time": None,    "cron_expression": "0 */1 * * *", "ship_paused": True, "legacy_names": ["Email → Calendar Events"]},
    "classify_events":      {"name": "Calendar Classify Events", "schedule": "cron",  "scheduled_time": None,    "cron_expression": "0 6,18 * * *", "ship_paused": True, "legacy_names": ["Classify Calendar Events"]},
    "check_email_urgency":   {"name": "Email Tags",               "schedule": "cron",  "scheduled_time": None,    "cron_expression": "0 * * * *", "ship_paused": True, "old_cron_expressions": ["*/15 * * * *"], "legacy_names": ["Email Triage", "Urgent Email"]},
    "audit_skills":          {"name": "Skills Audit",             "trigger_type": "event", "trigger_event": "skill_added", "trigger_count": 5, "schedule": None, "scheduled_time": None, "cron_expression": None, "legacy_names": ["Audit Skills"]},
}

RETIRED_HOUSEKEEPING_ACTIONS = frozenset({
    "tidy_calendar",
    "tidy_email_inbox",
    "mark_email_boundaries",
    "tidy_documents",
})

RETIRED_HOUSEKEEPING_NAMES = {
    "Documents Tidy": "tidy_documents",
    "Tidy Documents": "tidy_documents",
}


def _digest_windows(now):
    """(label, start, end) buckets for the calendar check-in digest.

    The windows are contiguous so no event is dropped between buckets — an
    earlier version started the 30-day window at now+8d while the week window
    ended at now+7d, so events ~7-8 days out fell into no bucket.
    """
    return [
        ("today_tomorrow", now, now + timedelta(days=2)),
        ("this_week", now + timedelta(days=2), now + timedelta(days=7)),
        ("next_30_days", now + timedelta(days=7), now + timedelta(days=30)),
    ]


def _checkin_calendar_events(db, owner, start, end):
    """Calendar events in [start, end] for ONE owner, for the check-in digest.

    Ownership lives on CalendarCal.owner; events inherit it via calendar_id.
    The digest query had no owner scope, so it pulled EVERY user's events into
    one user's check-in (a cross-tenant leak of summaries/locations). Scope it
    by joining CalendarCal, mirroring routes/calendar_routes.list_events.
    """
    from core.database import CalendarEvent as _CE, CalendarCal as _CC
    return (
        db.query(_CE)
        .join(_CC, _CE.calendar_id == _CC.id)
        .filter(
            _CC.owner == owner,
            _CE.dtstart >= start,
            _CE.dtstart <= end,
            _CE.status != "cancelled",
        )
        .order_by(_CE.dtstart)
        .all()
    )


@dataclass(frozen=True)
class _ManagedTaskRoute:
    """Secret-free route identity for one scheduled managed-model run."""

    model_route_id: str
    connection_id: str
    provider_model_id: str
    public_endpoint_id: str
    capabilities: dict[str, Any]
    grant_id: str | None = None

    @property
    def runtime_model(self) -> str:
        return f"{self.connection_id}/{self.provider_model_id}"


def _resolve_managed_task_route(
    task,
    *,
    crew=None,
    session=None,
    operation: str = "chat.stream",
) -> _ManagedTaskRoute:
    """Resolve an exact task route or the owner's ordered ``tasks`` default.

    Legacy URLs, headers, endpoint rows, and model strings are deliberately
    excluded.  A migrated task/crew/session route remains pinned; otherwise
    the first enabled owner-scoped ``tasks`` binding supporting ``operation``
    is used.
    """

    from src.openclank.chat_routing import (
        ChatRouteUnavailable,
        normalized_provider_owner,
        resolve_chat_route,
        share_id_from_endpoint,
    )
    from src.openclank.provider_store import ProviderStore, ProviderStoreError

    owner = normalized_provider_owner(getattr(task, "owner", None))
    route_id = next(
        (
            str(value).strip()
            for value in (
                getattr(task, "provider_model_route_id", None),
                getattr(crew, "provider_model_route_id", None),
                getattr(session, "provider_model_route_id", None),
            )
            if str(value or "").strip()
        ),
        "",
    )
    endpoint_id = next(
        (
            str(value).strip()
            for value in (
                getattr(task, "endpoint_id", None),
                getattr(crew, "endpoint_id", None),
                getattr(session, "endpoint_id", None),
            )
            if str(value or "").strip()
        ),
        "",
    )
    store = ProviderStore()

    if route_id:
        # Received shares need the recipient's accepted grant identity.  Own
        # routes are read directly so a deliberately hidden but still-pinned
        # automation remains executable after migration.
        grant_id = share_id_from_endpoint(endpoint_id)
        if grant_id:
            try:
                route = resolve_chat_route(
                    owner=owner,
                    endpoint_id=endpoint_id,
                    model_route_id=route_id,
                    provider_store=store,
                )
            except ChatRouteUnavailable as exc:
                raise RuntimeError(f"Managed task model route is unavailable: {exc}") from exc
            if operation not in set(route.operations or ()):
                raise RuntimeError(
                    f"Managed task model route does not support {operation}"
                )
            return _ManagedTaskRoute(
                model_route_id=route.model_route_id,
                connection_id=route.connection_id,
                provider_model_id=route.provider_model_id,
                public_endpoint_id=route.public_endpoint_id,
                capabilities=dict(route.capabilities or {}),
                grant_id=route.provider_grant_id,
            )
        try:
            route = store.get_model_route(owner=owner, model_route_id=route_id)
            connection = store.get_connection(
                owner=owner,
                connection_id=route.connection_id,
            )
        except ProviderStoreError:
            # A migrated shared route can recover its unique accepted grant
            # from the stable route ID even if old endpoint metadata was empty.
            try:
                shared = resolve_chat_route(
                    owner=owner,
                    model_route_id=route_id,
                    provider_store=store,
                )
            except ChatRouteUnavailable as exc:
                raise RuntimeError(f"Managed task model route is unavailable: {exc}") from exc
            if operation not in set(shared.operations or ()):
                raise RuntimeError(
                    f"Managed task model route does not support {operation}"
                )
            return _ManagedTaskRoute(
                model_route_id=shared.model_route_id,
                connection_id=shared.connection_id,
                provider_model_id=shared.provider_model_id,
                public_endpoint_id=shared.public_endpoint_id,
                capabilities=dict(shared.capabilities or {}),
                grant_id=shared.provider_grant_id,
            )
        if (
            not route.enabled
            or route.deleted_at is not None
            or not connection.enabled
            or connection.deleted_at is not None
            or operation not in set(route.operations or ())
        ):
            raise RuntimeError(
                f"Managed task model route does not support {operation}"
            )
        return _ManagedTaskRoute(
            model_route_id=route.id,
            connection_id=connection.id,
            provider_model_id=route.provider_model_id,
            public_endpoint_id=connection.id,
            capabilities=dict(route.capabilities or {}),
        )

    try:
        bindings = store.get_route_bindings_for_purpose(
            owner=owner,
            purpose="tasks",
        )
    except ProviderStoreError as exc:
        raise RuntimeError("No managed tasks model route is configured") from exc
    for binding in bindings:
        if not binding.enabled:
            continue
        try:
            route = store.get_model_route(
                owner=owner,
                model_route_id=binding.model_route_id,
            )
            connection = store.get_connection(
                owner=owner,
                connection_id=route.connection_id,
            )
        except ProviderStoreError:
            continue
        if (
            route.enabled
            and route.deleted_at is None
            and connection.enabled
            and connection.deleted_at is None
            and operation in set(route.operations or ())
        ):
            return _ManagedTaskRoute(
                model_route_id=route.id,
                connection_id=connection.id,
                provider_model_id=route.provider_model_id,
                public_endpoint_id=connection.id,
                capabilities=dict(route.capabilities or {}),
            )
    raise RuntimeError(f"No managed tasks model route supports {operation}")


class TaskScheduler:
    def __init__(self, session_manager, mimo_supervisor=None, auth_manager=None):
        self._session_manager = session_manager
        self._mimo_supervisor = mimo_supervisor
        self._auth_manager = auth_manager
        self._running = False
        self._task = None
        self._executing = set()  # task IDs currently running OR queued behind the semaphore
        # Guards mutations of _executing. _check_due_tasks runs in the loop
        # coroutine; trigger_task() can be called from request handlers; the
        # event bus fires from background tasks. Without this lock long-running
        # tasks could be double-dispatched.
        self._executing_lock = asyncio.Lock()
        self._pending_notifications = []  # completed task notifications
        self._task_defer_counts = {}
        # Strict serial execution — exactly one task runs at a time. Anything
        # else (manual trigger, scheduled dispatch, task chain) waits behind
        # the semaphore as "queued" and starts when the current run finishes.
        # This is a hard guarantee, not configurable.
        self._run_semaphore = asyncio.Semaphore(1)
        self._concurrency_cap = 1
        self._task_handles = {}
        # Preserve the historical latest-handle map for UI/stop callers while
        # retaining every forced/queued execution for permission-reset
        # cancellation. A forced run can coexist with an older same-task run
        # while it waits on the per-task lock.
        self._task_handle_sets: dict[str, set[asyncio.Task]] = {}
        # ``force`` bypasses the global background-model slot so an operator
        # can run a task while unrelated work is active. It must not allow two
        # copies of the *same* durable task to race its session_id/transcript
        # claim, though. Keep a per-task fence across the full execution.
        self._task_run_locks: dict[str, asyncio.Lock] = {}
        self._owner_aux_handles: dict[str, set[asyncio.Task]] = {}
        self._s05_controller = None
        self._s05_handle = None
        self._s05_cursor = ('', '')
        self._s05_stage_owner = ''
        self._s05_skill_owner = ''
        self._s05_stage_cursors = {}
        self._s05_pair_stage_owner = ''
        self._s05_pair_stage_cursors = {}
        self._s05_pair_cursor = ('', '')
        self._s05_pair_turn = False
        self._s05_maintenance_owner = ''
        self._s05_maintenance_cursors = {}
        self._s08_scan_owner = ''
        self._s08_status_owner = ''
        self._journal_staged = {}      # account -> {chat_id: JournalSelection} staged/selected, awaiting the journal run step (H9)
        self._journal_settle = {}      # account -> set(chat_id) whose journal run finished, to be settled (status -> proof -> ACK)
        self._journal_run_owner = ''
        self._s08_scan_cursors = {}
        self._s08_evidence_window = {}
        self._s08_next_connection = {}
        self._s05_registry_state = None
        self._s05_registry_dirty = False
        self._s05_registry_generation = 0
        self._s05_registry_status = {'state':'disabled','reason':'profile_not_configured'}
        self._runtime_instance_id = uuid.uuid4().hex

    def _owner_lifecycle_fenced(self, owner: object) -> bool:
        """Fail closed when the account coordinator has fenced ``owner``."""
        normalized = str(owner or "").strip().lower()
        if not normalized:
            return False
        checker = getattr(
            getattr(self, "_auth_manager", None),
            "is_account_lifecycle_fenced",
            None,
        )
        if not callable(checker):
            return False
        try:
            return bool(checker(normalized))
        except Exception:
            return True

    def _skip_owner_maintenance(self, owner: object) -> bool:
        return str(owner or "").strip().lower().startswith("deleted:") or self._owner_lifecycle_fenced(owner)

    def _task_owner_lifecycle_fenced(self, task_id: str) -> bool:
        from core.database import SessionLocal, ScheduledTask

        db = SessionLocal()
        try:
            row = db.query(ScheduledTask.owner).filter(
                ScheduledTask.id == task_id,
            ).first()
            return bool(row and self._owner_lifecycle_fenced(row[0]))
        finally:
            db.close()

    @staticmethod
    def _runtime_fingerprint(identities: list[str]) -> str:
        material = json.dumps(sorted(identities), separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _same_runtime_inventory(observed: object, expected: object) -> bool:
        if not isinstance(expected, dict):
            return True
        return (
            int(getattr(observed, "get", lambda *_: 0)("count") or 0)
            == int(expected.get("count") or 0)
            and str(getattr(observed, "get", lambda *_: "")("fingerprint") or "")
            == str(expected.get("fingerprint") or "")
        )

    def owner_runtime_inventory(self, owner: str) -> dict[str, object]:
        """Content-silent inventory of owner-keyed process-local task state.

        Identities are hashed independently of global container positions so a
        lifecycle operation for owner A cannot change owner B's fingerprint.
        """
        normalized = str(owner or "").strip().lower()
        notifications = []
        for item in self._pending_notifications:
            if str(item.get("owner") or "").strip().lower() != normalized:
                continue
            # Notification prose is user content.  Only stable operational
            # identity participates in lifecycle evidence.
            identity = {
                key: item.get(key)
                for key in ("task_id", "kind", "status", "timestamp")
            }
            canonical = json.dumps(
                identity,
                default=str,
                separators=(",", ":"),
                sort_keys=True,
            )
            notifications.append(
                hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            )

        session_manager = getattr(self, "_session_manager", None)
        session_inventory = None
        inventory_sessions = getattr(session_manager, "owner_cache_inventory", None)
        if callable(inventory_sessions):
            session_inventory = inventory_sessions(normalized)
        if session_inventory is None:
            sessions = []
            session_cache = getattr(session_manager, "sessions", {})
            for session_id, cached in getattr(session_cache, "items", lambda: ())():
                cached_owner = (
                    cached.get("owner")
                    if isinstance(cached, dict)
                    else getattr(cached, "owner", None)
                )
                if str(cached_owner or "").strip().lower() == normalized:
                    sessions.append(str(session_id))
            session_inventory = {
                "count": len(sessions),
                "fingerprint": self._runtime_fingerprint(sessions),
            }

        completed_cache = []
        pending_cache = []
        for key in _shared_cache:
            if _shared_cache_key_owner(key) == normalized:
                completed_cache.append(
                    hashlib.sha256(repr(key[2:]).encode("utf-8")).hexdigest()
                )
        for key in _shared_cache_pending:
            if _shared_cache_key_owner(key) == normalized:
                pending_cache.append(
                    hashlib.sha256(repr(key[2:]).encode("utf-8")).hexdigest()
                )

        aux_handles = [
            "active"
            for handle in self._owner_aux_handles.get(normalized, set())
            if not handle.done()
        ]

        domains = {
            "notifications": len(notifications),
            "sessions": int(session_inventory.get("count") or 0),
            "shared_cache": len(completed_cache),
            "shared_cache_pending": len(pending_cache),
            "aux_handles": len(aux_handles),
        }
        domain_fingerprints = {
            "notifications": self._runtime_fingerprint(notifications),
            "sessions": str(session_inventory.get("fingerprint") or ""),
            "shared_cache": self._runtime_fingerprint(completed_cache),
            "shared_cache_pending": self._runtime_fingerprint(pending_cache),
            "aux_handles": self._runtime_fingerprint(aux_handles),
        }
        material = json.dumps(
            {"domains": domains, "fingerprints": domain_fingerprints},
            separators=(",", ":"),
            sort_keys=True,
        )
        return {
            "schema_version": 1,
            "count": sum(domains.values()),
            "fingerprint": hashlib.sha256(material.encode("utf-8")).hexdigest(),
            "domains": domains,
            "domain_fingerprints": domain_fingerprints,
        }

    def preview_owner_runtime_rename(
        self,
        source_owner: str,
        target_owner: str,
    ) -> dict[str, object]:
        """Freeze exact process-local state and reject target inheritance."""
        source = self.owner_runtime_inventory(source_owner)
        target = self.owner_runtime_inventory(target_owner)
        if int(target["count"]):
            raise RuntimeError("target scheduler runtime already contains state")
        return {
            "source": source, "target": target,
            "runtime_instance_id": self._runtime_instance_id,
        }

    def reconcile_owner_runtime(
        self,
        source_owner: str,
        target_owner: str,
        manifest: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Move owner-tagged notification buffers during account rename/staging."""
        source = str(source_owner or "").strip().lower()
        target = str(target_owner or "").strip().lower()
        if not source or not target or source == target:
            raise ValueError("task runtime lifecycle requires distinct owners")
        source_before = self.owner_runtime_inventory(source)
        target_before = self.owner_runtime_inventory(target)
        if (
            (manifest or {}).get("runtime_instance_id")
            and manifest["runtime_instance_id"] != self._runtime_instance_id
        ):
            # These buffers are explicitly process-local. A restart drops them;
            # a frozen inventory must not require resurrecting lost cache data.
            if source_before["count"] or target_before["count"]:
                raise RuntimeError("unexpected scheduler runtime after restart")
            return {
                "state": "expired_on_restart", "moved": 0,
                "source": source_before, "target": target_before,
            }
        expected_source = (manifest or {}).get("source")
        expected_target = (manifest or {}).get("target")
        if source_before["count"] and target_before["count"]:
            raise RuntimeError("source and target task runtime both contain state")
        if source_before["count"] and not self._same_runtime_inventory(
            source_before,
            expected_source,
        ):
            raise RuntimeError("source scheduler runtime inventory changed")
        if source_before["count"] and not self._same_runtime_inventory(
            target_before,
            expected_target,
        ):
            raise RuntimeError("target scheduler runtime inventory changed")
        if source_before["domains"]["shared_cache_pending"]:
            raise RuntimeError("source scheduler cache is still active")
        if source_before["count"]:
            session_manager = getattr(self, "_session_manager", None)
            rename_sessions = getattr(session_manager, "rename_owner_cache", None)
            if callable(rename_sessions):
                expected_sessions = None
                expected_target_sessions = None
                if isinstance(expected_source, dict):
                    expected_sessions = {
                        "count": (expected_source.get("domains") or {}).get("sessions", 0),
                        "fingerprint": (expected_source.get("domain_fingerprints") or {}).get("sessions", ""),
                    }
                if isinstance(expected_target, dict):
                    expected_target_sessions = {
                        "count": (expected_target.get("domains") or {}).get("sessions", 0),
                        "fingerprint": (expected_target.get("domain_fingerprints") or {}).get("sessions", ""),
                    }
                rename_sessions(
                    source,
                    target,
                    expected_source=expected_sessions,
                    expected_target=expected_target_sessions,
                )
            else:
                session_cache = getattr(session_manager, "sessions", {})
                for cached in getattr(session_cache, "values", lambda: ())():
                    cached_owner = (
                        cached.get("owner")
                        if isinstance(cached, dict)
                        else getattr(cached, "owner", None)
                    )
                    if str(cached_owner or "").strip().lower() != source:
                        continue
                    if isinstance(cached, dict):
                        cached["owner"] = target
                    else:
                        cached.owner = target
            for item in self._pending_notifications:
                if str(item.get("owner") or "").strip().lower() == source:
                    item["owner"] = target
            for key in list(_shared_cache):
                if _shared_cache_key_owner(key) != source:
                    continue
                target_key = (key[0], target, *key[2:])
                _shared_cache[target_key] = _shared_cache.pop(key)
        elif target_before["count"]:
            if expected_source is None or not self._same_runtime_inventory(
                target_before,
                expected_source,
            ):
                raise RuntimeError("unexpected target scheduler runtime state")
        elif isinstance(expected_source, dict) and int(expected_source.get("count") or 0):
            raise RuntimeError("expected scheduler runtime state is missing")
        source_after = self.owner_runtime_inventory(source)
        target_after = self.owner_runtime_inventory(target)
        if source_after["count"]:
            raise RuntimeError("source scheduler runtime remains after reconcile")
        if expected_source is not None and not self._same_runtime_inventory(
            target_after,
            expected_source,
        ):
            raise RuntimeError("reconciled scheduler runtime inventory changed")
        return {
            "state": "staged" if source_before["count"] else "already_applied",
            "moved": int(source_before["count"]),
            "source": source_after,
            "target": target_after,
        }

    def compensate_owner_runtime(
        self,
        source_owner: str,
        target_owner: str,
        manifest: dict[str, object] | None = None,
    ) -> dict[str, object]:
        receipt = self.reconcile_owner_runtime(
            target_owner,
            source_owner,
            manifest,
        )
        return {**receipt, "state": "restored"}

    def purge_owner_runtime(
        self,
        owner: str,
        expected: dict[str, object] | None = None,
    ) -> dict[str, object]:
        normalized = str(owner or "").strip().lower()
        before = self.owner_runtime_inventory(normalized)
        expected_inventory = (
            expected.get("source")
            if isinstance(expected, dict) and isinstance(expected.get("source"), dict)
            else expected
        )
        if before["count"] and not self._same_runtime_inventory(
            before,
            expected_inventory,
        ):
            raise RuntimeError("scheduler runtime purge inventory changed")
        self._pending_notifications[:] = [
            item
            for item in self._pending_notifications
            if str(item.get("owner") or "").strip().lower() != normalized
        ]
        session_manager = getattr(self, "_session_manager", None)
        purge_sessions = getattr(session_manager, "purge_owner_cache", None)
        if callable(purge_sessions):
            expected_sessions = None
            if isinstance(expected_inventory, dict):
                expected_sessions = {
                    "count": (expected_inventory.get("domains") or {}).get("sessions", 0),
                    "fingerprint": (expected_inventory.get("domain_fingerprints") or {}).get("sessions", ""),
                }
            purge_sessions(normalized, expected=expected_sessions)
        else:
            session_cache = getattr(session_manager, "sessions", {})
            for session_id, cached in list(
                getattr(session_cache, "items", lambda: ())()
            ):
                cached_owner = (
                    cached.get("owner")
                    if isinstance(cached, dict)
                    else getattr(cached, "owner", None)
                )
                if str(cached_owner or "").strip().lower() == normalized:
                    session_cache.pop(session_id, None)
        for key in list(_shared_cache):
            if _shared_cache_key_owner(key) == normalized:
                _shared_cache.pop(key, None)
        for key, pending in list(_shared_cache_pending.items()):
            if _shared_cache_key_owner(key) == normalized:
                _shared_cache_pending.pop(key, None)
                if not pending.done():
                    pending.cancel()
        aux = self._owner_aux_handles.pop(normalized, set())
        for handle in aux:
            if not handle.done():
                handle.cancel()
        return {
            "state": "purged",
            "removed": int(before["count"]),
            "after": self.owner_runtime_inventory(normalized),
        }

    async def quiesce_owner_lifecycle(self, owner: str) -> dict[str, int]:
        """Cancel and join every queued/running scheduled task for one owner.

        The account operation is already durable (and therefore an admission
        fence) before this runs.  DB rows are marked aborted both before and
        after joining because cancellation cleanup may otherwise overwrite the
        authoritative terminal state with a stale ORM object.
        """
        from sqlalchemy import func

        from core.database import SessionLocal, ScheduledTask, TaskRun

        normalized = str(owner or "").strip().lower()
        if not normalized:
            raise ValueError("task lifecycle owner is required")
        db = SessionLocal()
        try:
            task_ids = [
                str(row[0])
                for row in db.query(ScheduledTask.id).filter(
                    func.lower(func.trim(ScheduledTask.owner)) == normalized,
                ).all()
            ]
        finally:
            db.close()

        message = "Aborted because an account lifecycle operation began"

        def abort_active_runs(authoritative_ids: set[str] | None = None) -> set[str]:
            if not task_ids:
                return set()
            session = SessionLocal()
            try:
                query = session.query(TaskRun).filter(TaskRun.task_id.in_(task_ids))
                if authoritative_ids:
                    runs = query.filter(
                        (TaskRun.id.in_(authoritative_ids))
                        | (TaskRun.status.in_(("queued", "running")))
                    ).all()
                else:
                    runs = query.filter(
                        TaskRun.status.in_(("queued", "running"))
                    ).all()
                now = _utcnow()
                for run in runs:
                    run.status = "aborted"
                    run.error = message
                    run.result = message
                    run.finished_at = now
                session.commit()
                return {str(run.id) for run in runs}
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        aborted_ids = abort_active_runs()
        handles: set[asyncio.Task] = set()
        for task_id in task_ids:
            handles.update(self._execution_handles_for_task(task_id))
        aux_handles = {
            handle
            for handle in self._owner_aux_handles.get(normalized, set())
            if not handle.done()
        }
        handles.update(aux_handles)
        current = asyncio.current_task()
        cancelled = {
            handle
            for handle in handles
            if handle is not current and not handle.done()
        }
        for handle in cancelled:
            if not handle.cancelling():
                handle.cancel()
        if cancelled:
            await asyncio.shield(asyncio.gather(*cancelled, return_exceptions=True))
        # Let cancellation finalizers unregister their handles and clean any
        # owner-keyed singleflight future before inventory is frozen.
        await asyncio.sleep(0)
        async with self._executing_lock:
            cleared = sum(task_id in self._executing for task_id in task_ids)
            self._executing.difference_update(task_ids)
        aborted_ids.update(abort_active_runs(aborted_ids))
        for task_id in task_ids:
            self._task_handles.pop(task_id, None)
            self._task_handle_sets.pop(task_id, None)
            self._task_defer_counts.pop(task_id, None)
            lock = self._task_run_locks.get(task_id)
            if lock is None or not lock.locked():
                self._task_run_locks.pop(task_id, None)
        self._owner_aux_handles.pop(normalized, None)
        if any(
            _shared_cache_key_owner(key) == normalized
            for key in _shared_cache_pending
        ):
            raise RuntimeError("owner scheduler cache did not quiesce")
        return {
            "tasks": len(task_ids),
            "runs_aborted": len(aborted_ids),
            "executions_cancelled": len(cancelled),
            "aux_executions_cancelled": len(aux_handles),
            "executing_cleared": cleared,
            "notifications_fenced": int(
                self.owner_runtime_inventory(normalized)["domains"]["notifications"]
            ),
        }

    def _register_task_handle(self, task_id: str, handle: asyncio.Task) -> None:
        handle_sets = getattr(self, "_task_handle_sets", None)
        if handle_sets is None:
            handle_sets = self._task_handle_sets = {}
        handle_sets.setdefault(task_id, set()).add(handle)
        self._task_handles[task_id] = handle

    def _unregister_task_handle(self, task_id: str, handle: asyncio.Task) -> None:
        handle_sets = getattr(self, "_task_handle_sets", None)
        group = handle_sets.get(task_id) if handle_sets is not None else None
        if group is not None:
            group.discard(handle)
            if not group:
                handle_sets.pop(task_id, None)
                if self._task_handles.get(task_id) is handle:
                    self._task_handles.pop(task_id, None)
                return
            if self._task_handles.get(task_id) in {None, handle}:
                self._task_handles[task_id] = next(iter(group))
            return
        if self._task_handles.get(task_id) is handle:
            self._task_handles.pop(task_id, None)

    def _execution_handles_for_task(self, task_id: str) -> set[asyncio.Task]:
        handles = set(
            getattr(self, "_task_handle_sets", {}).get(task_id, set())
        )
        compatibility = self._task_handles.get(task_id)
        if compatibility is not None:
            handles.add(compatibility)
        return {handle for handle in handles if not handle.done()}

    def _all_execution_handles(self) -> set[asyncio.Task]:
        handles = {
            handle
            for group in getattr(self, "_task_handle_sets", {}).values()
            for handle in group
        }
        handles.update(self._task_handles.values())
        if getattr(self, '_s05_handle', None) is not None:
            handles.add(self._s05_handle)
        return {handle for handle in handles if not handle.done()}

    async def _mint_session_id(self) -> str:
        """Return a scheduler-local ID; ACP creates and cleans its own session."""
        return str(uuid.uuid4())

    def _cache_task_session(self, db_session) -> bool:
        """Mirror an already-inserted task Session without inserting it again."""
        if not self._session_manager or db_session is None:
            return False
        if self._owner_lifecycle_fenced(getattr(db_session, "owner", None)):
            return False
        try:
            cached = self._session_manager._db_to_session_meta(db_session)
            if cached is not None:
                self._session_manager.sessions[db_session.id] = cached
                return True
        except Exception:
            logger.debug(
                "Could not mirror task output session %s",
                getattr(db_session, "id", ""),
                exc_info=True,
            )
        return False

    def _sync_cached_task_workspace(self, session_id: str, workspace_id: str | None) -> None:
        if not self._session_manager:
            return
        cached = getattr(self._session_manager, "sessions", {}).get(session_id)
        if cached is not None:
            cached_owner = (
                cached.get("owner")
                if isinstance(cached, dict)
                else getattr(cached, "owner", None)
            )
            if self._owner_lifecycle_fenced(cached_owner):
                return
            if isinstance(cached, dict):
                cached["workspace_id"] = workspace_id or None
            else:
                cached.workspace_id = workspace_id or None

    @staticmethod
    def _task_workspace_path(task) -> str | None:
        """Return the per-run resolved path without treating it as durable."""
        return (
            str(getattr(task, "_resolved_workspace_path", None) or "").strip()
            or str(getattr(task, "workspace", None) or "").strip()
            or None
        )

    def _resolve_task_workspace(self, task) -> str | None:
        """Re-resolve task filesystem authority from current server policy.

        Stable IDs are the normal lane. A raw path remains only as an
        administrator compatibility field for existing tasks. The physical
        path is held on a non-mapped transient attribute so later ORM commits
        never turn derived authority back into durable path state.
        """
        task._resolved_workspace_path = None
        workspace_id = str(getattr(task, "workspace_id", None) or "").strip()
        if workspace_id:
            from core.auth import AuthManager
            from src.openclank.file_policy import FilePolicyRepository
            from src.openclank.workspace_policy_service import (
                WorkspacePolicyServiceError,
                resolve_owned_workspace,
            )

            auth_manager = self._auth_manager or AuthManager()
            try:
                binding = resolve_owned_workspace(
                    FilePolicyRepository(),
                    workspace_id=workspace_id,
                    owner_username=str(getattr(task, "owner", None) or ""),
                    auth_manager=auth_manager,
                    purpose="agent_workspace",
                )
            except WorkspacePolicyServiceError as error:
                raise RuntimeError(
                    "TASK_WORKSPACE_UNAVAILABLE: Workspace authority is no longer available"
                ) from error
            task._resolved_workspace_path = binding.path
            return binding.path

        raw_workspace = str(getattr(task, "workspace", None) or "").strip()
        if not raw_workspace:
            return None
        if not owner_has_admin_task_privileges(getattr(task, "owner", None)):
            raise RuntimeError(
                "TASK_WORKSPACE_UNAVAILABLE: Legacy task workspace requires administrator privileges"
            )
        from src.tool_execution import vet_workspace

        resolved = vet_workspace(raw_workspace)
        if not resolved:
            raise RuntimeError(
                "TASK_WORKSPACE_UNAVAILABLE: Legacy task workspace is no longer available"
            )
        task._resolved_workspace_path = resolved
        return resolved

    def _set_run_progress(self, run_id: str, message: str):
        """Persist short live progress text for Activity while a run is active."""
        if not run_id:
            return
        try:
            from core.database import SessionLocal, TaskRun
            db = SessionLocal()
            try:
                run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if run and run.status in ("queued", "running"):
                    run.result = (message or "")[:4000]
                    db.commit()
            finally:
                db.close()
        except Exception:
            logger.debug("Task progress update failed", exc_info=True)

    def _mark_run_aborted(self, task_id: str, run_id: str | None = None, message: str = "Stopped by user") -> bool:
        """Mark an active run as aborted. Used by stop/cancel paths."""
        try:
            from core.database import SessionLocal, TaskRun
            db = SessionLocal()
            try:
                q = db.query(TaskRun)
                if run_id:
                    q = q.filter(TaskRun.id == run_id)
                else:
                    q = q.filter(
                        TaskRun.task_id == task_id,
                        TaskRun.status.in_(("queued", "running")),
                    ).order_by(TaskRun.started_at.desc())
                run = q.first()
                if not run or run.status not in ("queued", "running"):
                    return False
                run.status = "aborted"
                run.error = message
                run.result = run.result or message
                run.finished_at = _utcnow()
                db.commit()
                return True
            finally:
                db.close()
        except Exception:
            logger.debug("Task abort marker failed for %s", task_id, exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    # S12 durable wait requests (question / permission)                   #
    # ------------------------------------------------------------------ #

    def _task_live_pending(self, *, owner, session_id, account_id, kind, request_id, root_operation_id):
        """Read one existing worker-held pending request; caller data grants nothing."""
        sup = getattr(self, '_mimo_supervisor', None)
        pool = getattr(sup, '_pool', None)
        workers = getattr(pool, '_workers', None)
        auth = getattr(self, '_auth_manager', None)
        if (not isinstance(workers, dict) or auth is None or not account_id
                or auth.account_id(owner) != account_id or auth.username_for_account_id(account_id) != owner):
            return None
        matches = []
        for worker in {id(w):w for w in workers.values()}.values():
            if getattr(worker, '_owner', None) != owner or not worker.is_alive():
                continue
            bridge = getattr(worker, '_bridge', None)
            holder = getattr(bridge, '_managed_provider_generation', None)
            handler = getattr(worker, 'question_handler' if kind == 'question' else 'permission_handler', None)
            pending = getattr(handler, 'pending_requests', {}).get(request_id) if handler else None
            if pending is None or pending._future.done() or not holder:
                continue
            if kind == 'question' and pending.blocking is not True:
                continue
            native = getattr(pending, 'mimo_session_id', None) if kind == 'question' else getattr(pending, 'session_id', None)
            if (pending.owner != owner or pending.odysseus_session_id != session_id or not native
                    or getattr(bridge, '_session_map', {}).get(session_id) != native):
                continue
            # A task chat can be reused by a later occurrence.  The pending
            # object's actual projection and current bridge turn must both be
            # the original root; the caller's label is never evidence alone.
            turn = getattr(bridge, '_turns', {}).get(native)
            context = getattr(bridge, '_session_context', {}).get(native) or {}
            actual_root = getattr(turn, 'root_operation_id', None)
            if (not actual_root or actual_root != root_operation_id
                    or context.get('root_operation_id') != actual_root
                    or pending.turn_id != actual_root):
                continue
            snapshot = {'version':1,'kind':kind,'request_id':request_id,'owner':owner,'account_id':account_id,
                        'session_id':session_id,'native_session_id':native,'holder':holder,
                        'generation':getattr(worker, 'installed_generation', None),
                        'turn_id':pending.turn_id,'revision':pending.revision,
                        'root_operation_id':actual_root}
            if kind == 'question':
                snapshot.update(native_request_id=pending.mimo_request_id, question_revision=pending.question_revision,
                                plan_revision=pending.plan_revision, actor_id=pending.actor_id,
                                actor_session_id=pending.actor_session_id, origin_turn_id=pending.origin_turn_id)
            if (type(snapshot['generation']) is not int or type(snapshot['revision']) is not int
                    or not isinstance(snapshot['turn_id'], str) or not snapshot['turn_id']):
                continue
            matches.append((snapshot, handler, pending))
        return matches[0] if len(matches) == 1 else None

    def _persist_wait_request(
        self,
        *,
        task_id: str,
        run_id: str,
        session_id: str,
        owner: str,
        kind: str,
        payload: dict,
        workspace_id: str | None = None,
        continuation_revision: int = 0,
        actor_generation: str | None = None,
        native_binding: dict | None = None,
    ) -> str | None:
        """Persist a waiting interaction and flip the run to ``waiting``.

        Returns the wait-request id, or ``None`` if persistence failed. The
        caller keeps consuming the stream: the bridge's QuestionHandler blocks
        until the answer arrives, so pause/resume is the natural async pause.
        """
        import json as _json
        import uuid as _uuid
        try:
            from core.database import SessionLocal, ScheduledTask, TaskRun, TaskWaitRequest
            wait_id = f"wait_{_uuid.uuid4().hex}"
            db = SessionLocal()
            try:
                if db.get_bind().dialect.name == 'sqlite':
                    db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                run = db.query(TaskRun).filter_by(id=run_id, task_id=task_id).first()
                task = db.query(ScheduledTask).filter_by(id=task_id, owner=owner).first()
                if not run or not task:
                    return None
                if native_binding is not None and (run.status not in ('running', 'outcome_unknown', 'issued_waiting')
                        or task.host_account_id != native_binding.get('account_id')
                        or native_binding.get('root_operation_id') != f'turn-task-run:{run_id}'):
                    return None
                if run and run.status in ("queued", "running", "outcome_unknown"):
                    run.status = "issued_waiting" if native_binding is not None or run.status == "outcome_unknown" else "waiting"
                    run.result = "Waiting for user answer…"
                payload = dict(payload)
                payload.pop('_openclank_native_wait', None)
                if native_binding is not None:
                    existing = db.query(TaskWaitRequest).filter_by(task_id=task_id, run_id=run_id,
                        owner=owner, session_id=session_id, kind=kind, state='waiting').order_by(TaskWaitRequest.id).limit(65).all()
                    if len(existing) > 64:
                        raise ValueError('task wait inventory unavailable')
                    for prior in existing:
                        try:
                            prior_payload = _json.loads(prior.payload or '{}')
                        except (ValueError, TypeError):
                            raise ValueError('task wait inventory unavailable')
                        if isinstance(prior_payload, dict) and prior_payload.get('_openclank_native_wait') == native_binding:
                            db.commit()
                            return prior.id
                    payload = dict(payload)
                    payload['_openclank_native_wait'] = native_binding
                if len(_json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')) > 65536:
                    raise ValueError('task wait payload too large')
                row = TaskWaitRequest(
                    id=wait_id,
                    task_id=task_id,
                    run_id=run_id,
                    session_id=session_id,
                    owner=owner or "",
                    kind=kind,
                    payload=_json.dumps(payload, default=str),
                    state="waiting",
                    workspace_id=workspace_id,
                    continuation_revision=continuation_revision or 0,
                    actor_generation=actor_generation,
                )
                db.add(row)
                db.commit()
                return wait_id
            finally:
                db.close()
        except Exception:
            logger.exception("Failed to persist task wait request for %s/%s", task_id, run_id)
            return None

    def _consume_wait_request(
        self,
        wait_id: str,
        *,
        owner: str,
        session_id: str | None = None,
        expected_payload: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        """CAS-consume a waiting request exactly once.

        Rejects wrong-owner, already-consumed, cancelled, or session-mismatched
        answers without consuming the valid request.
        """
        try:
            from core.database import SessionLocal, TaskWaitRequest
            db = SessionLocal()
            try:
                row = db.query(TaskWaitRequest).filter(TaskWaitRequest.id == wait_id).first()
                if row is None:
                    return False
                if row.state != "waiting":
                    return False
                if owner is not None and row.owner != owner:
                    return False
                if session_id is not None and row.session_id != session_id:
                    return False
                # Preserve the exact reviewed payload and scope through CAS.
                query = db.query(TaskWaitRequest).filter(TaskWaitRequest.id == wait_id, TaskWaitRequest.state == "waiting",
                    TaskWaitRequest.owner == owner)
                if session_id is not None:
                    query = query.filter(TaskWaitRequest.session_id == session_id)
                if expected_payload is not None:
                    query = query.filter(TaskWaitRequest.payload == expected_payload)
                if task_id is not None:
                    query = query.filter(TaskWaitRequest.task_id == task_id)
                if run_id is not None:
                    query = query.filter(TaskWaitRequest.run_id == run_id)
                updated = query.update({"state": "consumed", "consumed_at": _utcnow()}, synchronize_session=False)
                db.commit()
                return bool(updated)
            finally:
                db.close()
        except Exception:
            logger.exception("Failed to consume task wait request %s", wait_id)
            return False

    def _cancel_wait_requests_for_run(self, run_id: str) -> int:
        """Invalidate all waiting requests for a run (cancel/revoke)."""
        try:
            from core.database import SessionLocal, TaskWaitRequest
            db = SessionLocal()
            try:
                updated = (
                    db.query(TaskWaitRequest)
                    .filter(TaskWaitRequest.run_id == run_id, TaskWaitRequest.state == "waiting")
                    .update({"state": "cancelled", "consumed_at": _utcnow()}, synchronize_session=False)
                )
                db.commit()
                return int(updated or 0)
            finally:
                db.close()
        except Exception:
            logger.debug("Failed to cancel wait requests for run %s", run_id, exc_info=True)
            return 0

    def _waiting_run_ids(self, task_id: str) -> list[str]:
        try:
            from core.database import SessionLocal, TaskRun
            db = SessionLocal()
            try:
                rows = (
                    db.query(TaskRun.id)
                    .filter(TaskRun.task_id == task_id, TaskRun.status.in_(("waiting", "issued_waiting", "issued_resuming")))
                    .all()
                )
                return [r[0] for r in rows]
            finally:
                db.close()
        except Exception:
            return []

    def _has_waiting_run(self, task_id: str) -> bool:
        return bool(self._waiting_run_ids(task_id))

    def _mark_run_resuming(self, run_id: str) -> bool:
        try:
            from core.database import SessionLocal, TaskRun
            db = SessionLocal()
            try:
                run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if not run or run.status not in ("waiting", "issued_waiting"):
                    return False
                run.status = "issued_resuming" if run.status == "issued_waiting" else "resuming"
                run.result = "Resuming…"
                db.commit()
                return True
            finally:
                db.close()
        except Exception:
            logger.debug("Failed to mark run %s resuming", run_id, exc_info=True)
            return False

    def add_notification(
        self,
        task_name: str,
        status: str,
        task_id: str = None,
        owner: str = None,
        body: str = None,
        kind: str = "task",
        session_id: str = None,
    ) -> bool:
        """Store a notification about a completed task run. Tagged with the
        task's owner so `pop_notifications` can return only that user's
        notifications and prevent cross-tenant drain. `body` is the result
        text — populated when output_target='notification' so the client can
        show a rich browser Notification, not just a toast. `session_id` is
        the task's durable chat so the toast/badge can open the ORIGINAL
        task chat (S12)."""
        normalized_owner = str(owner or "").strip().lower()
        if normalized_owner and self._owner_lifecycle_fenced(normalized_owner):
            return False
        self._pending_notifications.append({
            "task_name": task_name,
            "status": status,
            "task_id": task_id,
            "owner": normalized_owner or owner,
            "body": (body[:500] + "…") if body and len(body) > 500 else body,
            "kind": kind,
            "session_id": session_id,
            "timestamp": _utcnow().isoformat() + "Z",
        })
        # Cap at 50 to avoid unbounded growth
        if len(self._pending_notifications) > 50:
            self._pending_notifications = self._pending_notifications[-50:]
        return True

    def pop_notifications(self, owner: str = None, predicate=None) -> list:
        """Return and clear pending notifications.

        When `owner` is set, only matching notifications are returned (and
        cleared). Notifications stored before owner-tagging existed (or
        from owner-less tasks) are included when the caller is anonymous
        or when no owner filter is given — preserves backward behaviour
        for the legacy single-user deploy.
        """
        if owner is None:
            keep, take = [], []
            for note in self._pending_notifications:
                note_owner = note.get("owner")
                if note_owner and self._owner_lifecycle_fenced(note_owner):
                    keep.append(note)
                else:
                    take.append(note)
            self._pending_notifications = keep
            return take
        # Strict owner scope — used to OR-in null-owner notifications for
        # "legacy single-user" compat but that leaked notification bodies to
        # any authenticated user once a second account existed.
        normalized_owner = str(owner or "").strip().lower()
        if self._owner_lifecycle_fenced(normalized_owner):
            return []
        keep, take = [], []
        for n in self._pending_notifications:
            if str(n.get("owner") or "").strip().lower() == normalized_owner and (predicate is None or predicate(n)):
                take.append(n)
            else:
                keep.append(n)
        self._pending_notifications = keep
        return take

    async def start(self):
        # On startup, mark any leftover "running" task_runs as errored. Without
        # this, a server crash leaves rows stuck running indefinitely and the
        # _executing in-memory set forgets them, so the UI shows phantoms.
        try:
            from core.database import SessionLocal, TaskRun
            db = SessionLocal()
            try:
                # Zombies from a prior server crash. Tagged "aborted" (not
                # "error") so the Activity view + error-rate stats don't
                # falsely blame the task for what was an infrastructure event.
                stale = db.query(TaskRun).filter(
                    TaskRun.status.in_(("running", "queued"))
                ).all()
                if stale:
                    now = _utcnow()
                    for r in stale:
                        if r.task and self._skip_owner_maintenance(r.task.owner):
                            continue
                        old_status = r.status or "running"
                        r.status = "aborted"
                        r.error = "Server restarted while task was " + old_status
                        r.finished_at = now
                    db.commit()
                    logger.info(f"Cleared {len(stale)} stale task_runs from previous run")
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Could not clear stale task_runs on startup: {e}")

        # S12: reconstruct durable wait requests from previous run. Waiting
        # runs survive restart; surface their pending questions so the
        # toast/badge opens the original task chat and the user can answer.
        try:
            from core.database import SessionLocal as _SL, TaskWaitRequest as _TWR, TaskRun as _TR, ScheduledTask as _ST
            db = _SL()
            try:
                pending = (
                    db.query(_TWR, _TR, _ST)
                    .join(_TR, _TWR.run_id == _TR.id)
                    .join(_ST, _TWR.task_id == _ST.id)
                    .filter(_TWR.state == "waiting", _TR.status.in_(("waiting", "issued_waiting")))
                    .all()
                )
                for wait, run, task in pending:
                    if self._skip_owner_maintenance(wait.owner):
                        continue
                    import json as _json
                    try:
                        payload = _json.loads(wait.payload or "{}")
                    except Exception:
                        payload = {}
                    self.add_notification(
                        task_name=task.name,
                        status="waiting",
                        task_id=task.id,
                        owner=wait.owner,
                        body=payload.get("question") or "Task needs your answer",
                        kind="task_question",
                        session_id=wait.session_id,
                    )
                if pending:
                    logger.info("Reconstructed %d task wait requests on startup", len(pending))
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Could not reconstruct task wait requests on startup: {e}")

        # Advance next_run for active tasks whose next_run is already in the
        # past. Without this, a restart hits _check_due_tasks() with an empty
        # in-process _executing set, and the same overdue task fires once per
        # poll until it completes.
        try:
            from core.database import SessionLocal as _SL, ScheduledTask as _ST
            db = _SL()
            try:
                now = _utcnow()
                overdue = db.query(_ST).filter(
                    _ST.status == "active",
                    _ST.next_run.isnot(None),
                    _ST.next_run < now,
                ).all()
                if overdue:
                    for t in overdue:
                        if self._skip_owner_maintenance(t.owner):
                            continue
                        t.next_run = now + timedelta(seconds=60)
                    db.commit()
                    logger.info(
                        "Pushed next_run forward by 60s for %d overdue active tasks on startup",
                        len(overdue),
                    )
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Could not advance overdue next_run on startup: {e}")

        # Defense-in-depth dedupe sweep: for any owner with >1 rows where
        # is_default_assistant=True, keep the oldest and demote the rest +
        # delete their orphaned check-in tasks. This is the safety net for
        # the synthetic-owner seeding bug (we cleaned a manual instance of
        # it, but a stale code path or DB import could recreate it).
        try:
            from core.database import SessionLocal, CrewMember, ScheduledTask
            db = SessionLocal()
            try:
                from sqlalchemy import func
                groups = db.query(CrewMember.owner, func.count(CrewMember.id).label("n")).filter(
                    CrewMember.is_default_assistant == True,  # noqa: E712
                ).group_by(CrewMember.owner).having(func.count(CrewMember.id) > 1).all()
                for owner, n in groups:
                    if self._skip_owner_maintenance(owner):
                        continue
                    rows = db.query(CrewMember).filter(
                        CrewMember.owner == owner,
                        CrewMember.is_default_assistant == True,  # noqa: E712
                    ).order_by(CrewMember.created_at.asc()).all()
                    keep = rows[0]
                    losers = rows[1:]
                    loser_ids = [r.id for r in losers]
                    # Delete the orphaned tasks tied to the loser crews — they
                    # are duplicates of the keeper's check-ins.
                    n_tasks = db.query(ScheduledTask).filter(
                        ScheduledTask.crew_member_id.in_(loser_ids)
                    ).delete(synchronize_session=False)
                    for r in losers:
                        db.delete(r)
                    db.commit()
                    logger.warning(
                        "Default-assistant dedupe: owner=%r had %d rows, kept %s, "
                        "dropped %d crew + %d orphan tasks",
                        owner, n, keep.id, len(losers), n_tasks,
                    )
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Could not dedupe default-assistant rows on startup: {e}")

        self._running = True
        self._task = asyncio.create_task(self._loop())
        # Internal background scanner that isn't a user-facing "task" — pure
        # infra (no LLM), shouldn't clutter the Tasks UI, fires on its own
        # cadence inside the scheduler process.
        #
        # Calendar event reminders are represented as Notes by the calendar UI,
        # so the Notes scanner is the single reminder dispatch path. Running the
        # old event scanner too caused duplicate emails/notifications for the
        # same calendar event.
        self._note_pings_task = asyncio.create_task(self._note_pings_loop())
        logger.info(f"Task scheduler started (concurrency cap: {self._concurrency_cap})")
        # Audit clusters: show any minute-of-day where >1 active scheduled
        # tasks land. Helps spot "all my tasks fire at 9am" patterns the user
        # may want to spread out.
        try:
            from core.database import SessionLocal, ScheduledTask
            db = SessionLocal()
            try:
                rows = db.query(ScheduledTask).filter(
                    ScheduledTask.status == "active",
                    ScheduledTask.trigger_type == "schedule",
                    ScheduledTask.next_run.isnot(None),
                ).all()
                buckets: Dict[str, list] = {}
                for r in rows:
                    if not r.next_run:
                        continue
                    key = r.next_run.strftime("%H:%M")
                    buckets.setdefault(key, []).append(r.name or r.id)
                clusters = {k: v for k, v in buckets.items() if len(v) > 1}
                if clusters:
                    summary = ", ".join(f"{k} ({len(v)})" for k, v in sorted(clusters.items()))
                    logger.info(f"Task scheduling clusters (>1 task/minute): {summary}")
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"Cluster audit skipped: {e}")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for attr in ("_note_pings_task", "_event_pings_task"):
            t = getattr(self, attr, None)
            if t:
                t.cancel()
                try: await t
                except asyncio.CancelledError: pass
        handles = list(self._all_execution_handles())
        for handle in handles:
            handle.cancel()
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)
        logger.info("Task scheduler stopped")

    async def _note_pings_loop(self):
        """Built-in note-due scanner — ticks every 60s inside the scheduler.
        Pure infra (no LLM), doesn't surface in the Tasks UI. Iterates
        per-owner so cache pruning in `action_ping_notes` (which removes
        cache entries for notes not in the current scan's seen_ids) doesn't
        cross-delete other users' entries (review C4).
        """
        await asyncio.sleep(30)
        from src.builtin_actions import action_ping_notes, TaskNoop
        while self._running:
            owners = self._known_task_owners()
            for ow in (owners or [""]):
                if ow and self._owner_lifecycle_fenced(ow):
                    continue
                try:
                    await self._run_owner_aux_action(ow, action_ping_notes)
                except TaskNoop:
                    pass
                except asyncio.CancelledError:
                    if not self._running:
                        raise
                except Exception as e:
                    logger.warning(f"ping_notes background scanner errored for owner={ow!r}: {e}")
            await asyncio.sleep(60)  # 1 min

    async def _event_pings_loop(self):
        """Built-in calendar-event scanner — same recipe as note pings. Runs
        every 10 min, fires reminders via dispatch_reminder. Not a user task.
        Iterates per-owner so each user only gets their own calendar pings
        (passing owner="" globally would email User B's events to User A's
        configured SMTP "from" address — see review C3).
        """
        await asyncio.sleep(90)
        from src.builtin_actions import action_ping_events, TaskNoop
        while self._running:
            owners = self._known_task_owners()
            for ow in (owners or [""]):
                if ow and self._owner_lifecycle_fenced(ow):
                    continue
                try:
                    await self._run_owner_aux_action(ow, action_ping_events)
                except TaskNoop:
                    pass
                except asyncio.CancelledError:
                    if not self._running:
                        raise
                except Exception as e:
                    logger.warning(f"ping_events background scanner errored for owner={ow!r}: {e}")
            await asyncio.sleep(600)  # 10 min

    async def _run_owner_aux_action(self, owner: str, action) -> object:
        """Track one owner-scoped scanner action so lifecycle can join it."""
        normalized = str(owner or "").strip().lower()
        if normalized and self._owner_lifecycle_fenced(normalized):
            return None
        handle = asyncio.create_task(action(owner=owner))
        if normalized:
            self._owner_aux_handles.setdefault(normalized, set()).add(handle)
            if self._owner_lifecycle_fenced(normalized):
                handle.cancel()
        try:
            return await handle
        finally:
            if normalized:
                group = self._owner_aux_handles.get(normalized)
                if group is not None:
                    group.discard(handle)
                    if not group:
                        self._owner_aux_handles.pop(normalized, None)

    def _known_task_owners(self) -> list:
        """Distinct non-empty owners that background scanners should visit.

        Scheduled tasks used to be the only owner source. Calendar reminders
        are stored as Notes, though, so an account with due notes but no task
        rows could get the browser reminder while the backend email/ntfy
        scanner never ran for that owner.
        """
        from core.database import SessionLocal, ScheduledTask, Note
        db = SessionLocal()
        try:
            owners = set()
            for r in db.query(ScheduledTask.owner).distinct().all():
                if r[0]:
                    owners.add(r[0])
            note_q = db.query(Note.owner).filter(
                Note.due_date.isnot(None),
                Note.due_date != "",
                Note.archived == False,  # noqa: E712
            ).distinct()
            for r in note_q.all():
                if r[0]:
                    owners.add(r[0])
            return sorted(owners)
        except Exception:
            return []
        finally:
            db.close()

    async def _loop(self):
        await asyncio.sleep(10)
        while self._running:
            try:
                await self._check_due_tasks()
                self._wake_completed_turn()
            except Exception:
                logger.exception("Error in task scheduler loop")
            # Sleep until the next scheduled run, capped at 60s. A `* * * * *`
            # cron task previously fired up to ~60s late because we always
            # slept the full minute; now the loop wakes near the boundary.
            sleep_for = 60.0
            try:
                from core.database import SessionLocal as _SL, ScheduledTask as _ST
                _db = _SL()
                try:
                    next_run = _db.query(_ST.next_run).filter(
                        _ST.status == "active",
                        _ST.next_run.isnot(None),
                    ).order_by(_ST.next_run.asc()).first()
                    if next_run and next_run[0]:
                        delta = (next_run[0] - _utcnow()).total_seconds()
                        sleep_for = max(1.0, min(60.0, delta))
                finally:
                    _db.close()
            except Exception:
                pass
            await asyncio.sleep(sleep_for)

    def install_completed_turn_controller(self, *, profile, owners):
        """Install app-issued owner authorities; queued refs remain the only work queue."""
        from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
        from src.openclank.memory_job_admission import SingleHostProfile, AssignmentAuthority
        from src.openclank.operation_router import ManagedOperationRouter
        if (type(profile) is not SingleHostProfile or type(owners) not in (tuple,list)
            or (self._s05_controller is not None and self._s05_controller[0] is not profile)):
            raise ValueError('completed-turn controller unavailable')
        registry = {}
        for item in owners:
            if type(item) is not tuple or len(item) != 3:
                raise ValueError('completed-turn owner unavailable')
            lifecycle,assignment,router = item
            if (type(lifecycle) is not CompletedTurnLifecycleOwner or type(assignment) is not AssignmentAuthority
                or type(router) is not ManagedOperationRouter or lifecycle.profile is not profile
                or lifecycle.owner != assignment.owner or lifecycle.source_authority is not assignment.source_authority
                or lifecycle.account_id in registry):
                raise ValueError('completed-turn owner unavailable')
            registry[lifecycle.account_id] = (lifecycle,assignment,router)
        profile.assert_current()
        # Replace atomically; a running task retains its own immutable snapshot
        # and must still pass the live per-source lifecycle checks.
        self._s05_controller = (profile,registry)

    def configure_completed_turn_registry(self, *, state, profile):
        """Attach only the app's existing qualified profile; no authority is minted."""
        from src.openclank.memory_job_admission import SingleHostProfile
        if type(profile) is not SingleHostProfile or getattr(state,'s05_profile',None) is not profile:
            raise ValueError('completed-turn registry unavailable')
        profile.assert_current()
        self._s05_registry_state = state
        self._s05_registry_generation += 1
        self._s05_registry_dirty = True
        self._s05_registry_status = {'state':'pending','reason':'registry_not_refreshed'}

    def completed_turn_status(self):
        return dict(self._s05_registry_status)

    async def _refresh_and_select_completed_turn(self):
        if self._s05_registry_dirty and self._s05_registry_state is not None:
            from src.openclank.memory_job_admission import refresh_completed_turn_registry
            generation = self._s05_registry_generation
            work = asyncio.create_task(asyncio.to_thread(refresh_completed_turn_registry,
                state=self._s05_registry_state,scheduler=self))
            try:
                result = await asyncio.shield(work)
                if generation != self._s05_registry_generation:
                    self._s05_registry_dirty = True
                    return
                profile = getattr(self._s05_registry_state,'s05_profile',None)
                if result.get('owners') or self._s05_controller is not None:
                    self.install_completed_turn_controller(profile=profile,owners=result['owners'])
                self._s05_registry_dirty = result.get('state') != 'ready'
                installer=getattr(self._s05_registry_state,'install_policy_publication_callbacks',None)
                if callable(installer) and result.get('state') == 'ready':
                    if not await installer(self._s05_registry_state,profile,result):
                        self._s05_registry_dirty = True
                elif result.get('state') != 'ready':
                    self._s05_registry_state.s08_rules_callback = None
                    self._s05_registry_state.maintenance_reconcile_callback = None
                status = {'state':result.get('state','unavailable'), 'owner_count':result.get('owner_count',0),
                          'unavailable_count':result.get('unavailable_count',0)}
                if status != self._s05_registry_status and status['state'] != 'ready':
                    logger.warning('Completed-turn registry is unavailable or partial')
                self._s05_registry_status = status
            except asyncio.CancelledError:
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if work.done() and not work.cancelled():
                    work.exception()
                raise
            except Exception:
                self._s05_registry_dirty = True
                self._s05_controller = None
                self._s05_registry_state.s08_rules_callback = None
                self._s05_registry_state.maintenance_reconcile_callback = None
                if self._s05_registry_status.get('reason') != 'registry_refresh_unavailable':
                    logger.warning('Completed-turn registry unavailable')
                self._s05_registry_status = {'state':'unavailable','reason':'registry_refresh_unavailable'}
                return
        if self._s05_controller is not None:
            await self._select_completed_turn()

    async def _select_completed_turn(self):
        from dataclasses import asdict
        from src.memery_external_sources import SourceRef
        from src.openclank.memory_job_admission import _conn, _run_completed_turn_once, _retire_completed_turn, _completed_turn_thread
        profile,registry = self._s05_controller
        _, cancelled = await _completed_turn_thread(profile.assert_current)
        if cancelled:
            raise asyncio.CancelledError
        await self._stage_one_completed_turn_chat(profile,registry)
        paired_ticket = await self._reconcile_paired_publication_ticket(profile,registry)
        source_ticket = await self._reconcile_source_publication_ticket(profile,registry)
        await self._stage_one_assistant_component_chat(profile,registry)
        await self._reconcile_one_s08_original(registry)
        self._s05_pair_turn = not self._s05_pair_turn
        if self._s05_pair_turn and not paired_ticket and not source_ticket:
            await self._select_one_assistant_interpretation(profile,registry)
            return
        await self._reconcile_one_maintenance_source(profile,registry)
        await self._discover_one_s08_page(profile,registry)
        await self._run_one_journal_selection(profile,registry)   # H9: sibling step, always after the existing S08 step
        if paired_ticket or source_ticket:
            return  # Existing original remains the only publication authority.
        await self._run_one_automatic_skill(profile, registry)
        cursor = self._s05_cursor
        def select_rows():
            with _conn(profile.path) as db:
                return db.execute("SELECT j.owner_id,j.job_id,r.source_account_id,r.source_ref_json FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r USING(owner_id,job_id) WHERE j.kind='completed_turn_review' AND j.state IN ('queued','retry_wait') AND (j.owner_id>? OR (j.owner_id=? AND j.job_id>?)) ORDER BY j.owner_id,j.job_id LIMIT 32", (cursor[0],cursor[0],cursor[1])).fetchall()
        rows, cancelled = await _completed_turn_thread(select_rows)
        if cancelled:
            raise asyncio.CancelledError
        if not rows:
            self._s05_cursor = ('','')
            return

        for row in rows:
            self._s05_cursor = (row['owner_id'],row['job_id'])
            entry = registry.get(row['source_account_id'])
            if entry is None:
                continue
            lifecycle,assignment,router = entry
            if row['owner_id'] != lifecycle.binding.memory_owner or self._owner_lifecycle_fenced(lifecycle.owner):
                continue
            try:
                source = SourceRef(**json.loads(row['source_ref_json']))
                if (source.account_id != lifecycle.account_id or json.dumps(asdict(source),sort_keys=True,separators=(',',':')) != row['source_ref_json']):
                    continue
                async with lifecycle.mapping.async_chat_admission_lock(source.chat_id) as guard:
                    try:
                        child, cancelled = await _completed_turn_thread(lambda: lifecycle.for_source(source=source,guard=guard))
                        if cancelled:
                            raise asyncio.CancelledError
                    except Exception:
                        # Only an exact durable withdrawal can retire work.
                        # Missing or transient authority remains queued.
                        _, cancelled = await _completed_turn_thread(lambda: _retire_completed_turn(lifecycle_owner=lifecycle,source=source,guard=guard,job_id=row['job_id']))
                        if cancelled:
                            raise asyncio.CancelledError
                        continue
            except (ValueError,TypeError,KeyError):
                continue
            except Exception:
                # A transient authority failure does not retire this source.
                continue
            async def action(*, owner):
                if owner != lifecycle.owner:
                    raise ValueError('completed-turn owner changed')
                return await _run_completed_turn_once(lifecycle=child,profile=profile,
                    target_authority=lifecycle.target_authority,assignment_authority=assignment,router=router,
                    lifecycle_state=getattr(self._session_manager,'lifecycle_context',None))
            try:
                barrier=getattr(getattr(getattr(self,'_session_manager',None),'lifecycle_context',None),'account_request_barrier',None)
                if barrier is None: raise ValueError('completed-turn account barrier unavailable')
                async with barrier.admitted(lifecycle.owner):
                    await self._run_owner_aux_action(lifecycle.owner,action)
            except ValueError:
                # Deferred or stale work stays in the existing queue; continue
                # on the next wake rather than trying a second job now.
                return
            return


    async def _reconcile_one_s08_original(self, registry):
        """Offer one retained original a bounded factual status pass."""
        state=self._s05_registry_state
        callback=getattr(state,'s08_original_status_callback',None) if state is not None else None
        accounts=sorted(registry)
        if not callable(callback) or not accounts: return
        account=next((item for item in accounts if item>self._s08_status_owner),accounts[0])
        self._s08_status_owner=account
        lifecycle,_assignment,_router=registry[account]
        async def action(*,owner):
            if owner!=lifecycle.owner: raise ValueError('S08 owner changed')
            return await callback(state,lifecycle)
        try:
            await self._run_owner_aux_action(lifecycle.owner,action)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unknown original remains with its provider; other work proceeds.
            pass

    async def _discover_one_s08_page(self, profile, registry):
        """Collect one bounded, individually authorized admitted-evidence page."""
        from datetime import datetime, timezone
        from src.memery_consolidation import select_codelivered_group, select_admitted_evidence
        from src.memery_provider import _HostObservationProjection
        from src.openclank.memory_job_admission import _completed_turn_thread
        state=self._s05_registry_state
        callback=getattr(state,'s08_rules_callback',None) if state is not None else None
        accounts=sorted(registry)
        if not callable(callback) or not accounts:
            return
        account=next((item for item in accounts if item>self._s08_scan_owner),accounts[0])
        self._s08_scan_owner=account
        lifecycle,_assignment,router=registry[account];provider=lifecycle.provider
        cursor=self._s08_scan_cursors.setdefault(account,{'after':'o:','origin':None,'event_after':None,'next_origin':None})
        try:
            profile.assert_current()
            if lifecycle.target_authority.resolve(account)!=lifecycle.binding or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner):
                raise ValueError('S08 owner unavailable')
            if cursor['origin'] is None:
                (rows,following),cancelled=await _completed_turn_thread(lambda: provider._host_account_source_refs(
                    account_id=account,target_authority=lifecycle.target_authority,after_job_id=cursor['after'],limit=1))
                if cancelled: raise asyncio.CancelledError
                if not rows:
                    cursor.update(after='o:',origin=None,event_after=None,next_origin=None)
                    return
                if len(rows)!=1 or type(rows[0]) is not _HostObservationProjection or rows[0].source.account_id!=account:
                    raise ValueError('S08 source inventory unavailable')
                cursor.update(origin=rows[0],event_after=None,next_origin=following)
            origin=cursor['origin'];source=origin.source
            async with lifecycle.mapping.async_chat_admission_lock(source.chat_id) as guard:
                _,cancelled=await _completed_turn_thread(lambda: lifecycle.for_source(source=source,guard=guard))
                if cancelled: raise asyncio.CancelledError
                rows,following=await provider._host_consolidation_admissions_page(origin,after=cursor['event_after'],limit=64)
                _,cancelled=await _completed_turn_thread(lambda: lifecycle.for_source(source=source,guard=guard))
                if cancelled: raise asyncio.CancelledError
                lifecycle.mapping.validate_chat_guard(source.chat_id,guard)
                if lifecycle.target_authority.resolve(account)!=lifecycle.binding:
                    raise ValueError('S08 owner changed')
            for item in rows:
                for old_key,(old_item,_) in tuple(self._s08_evidence_window.items()):
                    if old_key[0]==account and old_item.memory.block_id==item.memory.block_id and old_item.memory!=item.memory:
                        self._s08_evidence_window.pop(old_key,None)
                key=(account,item.event_id);previous=self._s08_evidence_window.get(key)
                if previous is not None and previous[0]!=item:
                    self._s08_evidence_window.pop(key,None)
                    continue
                self._s08_evidence_window.pop(key,None)
                self._s08_evidence_window[key]=(item,origin)
                while len(self._s08_evidence_window)>512:
                    self._s08_evidence_window.pop(next(iter(self._s08_evidence_window)))
            if following is None:
                cursor.update(after=cursor['next_origin'] or 'o:',origin=None,event_after=None,next_origin=None)
            else:
                cursor['event_after']=following
            entries=[value for (owner,_),value in self._s08_evidence_window.items() if owner==account]
            origins={item.event_id:origin for item,origin in entries}
            # Preferences only select the next use of this existing wake; the
            # retained provider rechecks source, policy, assignment and budget.
            from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
            def enabled_axes():
                with PREFERENCE_LOCK:
                    prefs=_load_for_user(lifecycle.owner)
                    return (type(prefs) is dict and prefs.get('memory_policy_connections_enabled') is True,
                            type(prefs) is dict and prefs.get('memory_policy_importance_enabled') is True)
            (connections,importance),cancelled=await _completed_turn_thread(enabled_axes)
            if cancelled: raise asyncio.CancelledError
            choices=['summary']+(['connection'] if connections else [])+(['importance_target'] if importance else [])
            cursor_index=self._s08_next_connection.get(account,0)
            if type(cursor_index) not in (int,bool): cursor_index=0
            index=int(cursor_index)%len(choices);output_kind=choices[index]
            self._s08_next_connection[account]=(index+1)%len(choices)
            now=datetime.now(timezone.utc); admitted=tuple(item for item,_ in entries)
            if output_kind=='importance_target':
                selection=None
                # Only target revisions actually encountered on this page;
                # the existing cursor supplies fairness without a new queue.
                for ref in sorted({item.memory for item in rows},key=lambda value:value.block_id):
                    candidate=select_admitted_evidence(owner=lifecycle.binding.memory_owner,workspace_id=lifecycle.binding.memory_workspace,
                        project_id=None,source_account_id=account,candidates=(ref,),evidence=admitted,now=now,minimum_blocks=1)
                    if len(candidate.memories)==1:
                        selection=candidate;break
                if selection is None:return
            else:
                selection=select_codelivered_group(owner=lifecycle.binding.memory_owner,workspace_id=lifecycle.binding.memory_workspace,
                    source_account_id=account,evidence=admitted,now=now)
                if len(selection.memories)<2:return
            async def action(*,owner):
                if owner!=lifecycle.owner: raise ValueError('S08 owner changed')
                return await callback(state,lifecycle,selection,tuple(origins[item.event_id] for item in selection.evidence),router,output_kind=output_kind)
            await self._run_owner_aux_action(lifecycle.owner,action)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Advance only the ephemeral hint; unknown sources remain durable
            # and are revisited on the next account scan.
            origin=cursor.get('origin')
            if origin is not None:
                for key,(_,stored) in tuple(self._s08_evidence_window.items()):
                    if key[0]==account and stored.source==origin.source:
                        self._s08_evidence_window.pop(key,None)
                cursor.update(after=cursor['next_origin'] or 'o:',origin=None,event_after=None,next_origin=None)
            return

    async def _run_one_automatic_skill(self, profile, registry):
        """Use the existing S05 wake for at most one explicitly enabled owner."""
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        from src.openclank.memory_job_admission import _completed_turn_thread
        from src.openclank.skill_draft_run import run_one_skill_draft
        accounts = sorted(registry)
        if not accounts:
            return
        account = next((item for item in accounts if item > self._s05_skill_owner), accounts[0])
        self._s05_skill_owner = account
        lifecycle, assignment, router = registry[account]
        def enabled():
            with PREFERENCE_LOCK:
                prefs = _load_for_user(lifecycle.owner)
                agent = prefs.get('agent_settings') if type(prefs) is dict else None
                distill = agent.get('distill') if type(agent) is dict else None
                return (type(prefs.get('_skill_draft_policy_version')) is int
                    and prefs['_skill_draft_policy_version'] == 1 and type(distill) is dict
                    and distill.get('auto') is True)
        try:
            active, cancelled = await _completed_turn_thread(enabled)
            if cancelled:
                raise asyncio.CancelledError
            if not active or self._owner_lifecycle_fenced(lifecycle.owner):
                return
            async def action(*, owner):
                if owner != lifecycle.owner:
                    raise ValueError('skill draft owner changed')
                return await run_one_skill_draft(scheduler=self, state=self._s05_registry_state,
                    profile=profile, lifecycle=lifecycle, assignment=assignment, router=router)
            await self._run_owner_aux_action(lifecycle.owner, action)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _run_one_journal_selection(self, profile, registry):
        """H9: run at most one staged journal selection, mirroring `_discover_one_s08_page` (same owner rotation, same aux action, same
        callback family). The authoritative gate is `_reserve` inside the callback; `s05_gates_open` is only the cheap pre-check that
        keeps a closed gate from preparing/cancelling (burning) an original. Settling is left to the staging pass."""
        from src.openclank.memory_job_admission import _completed_turn_limits, _completed_turn_thread
        from src.openclank.fm_journal_delta import PREF_KEY, s05_gates_open
        state=self._s05_registry_state
        callback=getattr(state,'s08_rules_callback',None) if state is not None else None
        accounts=sorted(account for account in registry if self._journal_staged.get(account))
        if not callable(callback) or not accounts:
            return
        account=next((item for item in accounts if item>self._journal_run_owner),accounts[0])
        self._journal_run_owner=account
        lifecycle,assignment,router=registry[account]
        staged=self._journal_staged[account]
        chat_id=next(iter(staged));journal=staged[chat_id]
        try:
            from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
            def enabled_now():
                with PREFERENCE_LOCK:
                    prefs=_load_for_user(lifecycle.owner)
                    return type(prefs) is dict and prefs.get(PREF_KEY) is True
            enabled,cancelled=await _completed_turn_thread(enabled_now)
            if cancelled: raise asyncio.CancelledError
            if not enabled: return                      # preference default Off: nothing is read or run
            def gates():
                profile.assert_current()
                _base,limits=_completed_turn_limits(lifecycle.target_authority,assignment)
                _operation,bound=journal.model_input(lifecycle.source_authority.hydrate,limits.job_output_tokens)
                return s05_gates_open(profile,owner=lifecycle.binding.memory_owner,limits=limits,input_bound=bound)
            open_,cancelled=await _completed_turn_thread(gates)
            if cancelled: raise asyncio.CancelledError
            if open_ is not True: return                # deferred: nothing prepared, the selection stays staged for a later wake
            async def action(*,owner):
                if owner!=lifecycle.owner: raise ValueError('S08 owner changed')
                if journal.cadence_policy is not None:
                    from src.openclank.memory_policy_projection import policy_coordinator
                    coordinator=policy_coordinator()
                    if coordinator is None:
                        return False
                    if coordinator.checked_refs(lifecycle.owner,account,lifecycle.binding.memory_workspace)!=journal.expected_policy_refs:
                        # Restage the still-unacknowledged page under the newly checked policy on a later existing wake.
                        staged.pop(chat_id,None)
                        self._journal_settle.setdefault(account,set()).add(chat_id)
                        return False
                    return await callback(state,lifecycle,None,(),router,output_kind='journal_synthesis',journal=journal,expected_policy_refs=journal.expected_policy_refs)
                return await callback(state,lifecycle,None,(),router,output_kind='journal_synthesis',journal=journal)
            ran=await self._run_owner_aux_action(lifecycle.owner,action)
            if ran is True:
                staged.pop(chat_id,None)
                self._journal_settle.setdefault(account,set()).add(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The selection stays staged (or is re-found by the next staging pass); the page stays unacknowledged.
            return

    async def _reconcile_one_maintenance_source(self, profile, registry):
        """One bounded pending hint on the existing S05 owner worker."""
        from sqlalchemy import text
        state=self._s05_registry_state
        callback=getattr(state,'maintenance_reconcile_callback',None) if state is not None else None
        accounts=sorted(registry)
        if not callable(callback) or not accounts:
            return
        account=next((item for item in accounts if item>self._s05_maintenance_owner),accounts[0])
        self._s05_maintenance_owner=account
        lifecycle,_assignment,_router=registry[account]
        after=self._s05_maintenance_cursors.get(account)
        def discover():
            profile.assert_current()
            source=lifecycle.source_authority
            if (source._auth.account_id(lifecycle.owner)!=account
                or source._auth.username_for_account_id(account)!=lifecycle.owner
                or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)
                or lifecycle.target_authority.resolve(account)!=lifecycle.binding):
                raise ValueError('maintenance owner unavailable')
            def authorize(candidate,chat):
                if candidate!=account: return False
                db=source._session_factory()
                try:
                    row=db.execute(text('SELECT owner FROM sessions WHERE id=:chat'),{'chat':chat}).first()
                    return bool(row and source._auth.account_id(row[0])==account)
                finally: db.close()
            return source._archive.discover_trusted_maintenance_pending(owner=account,after=after,limit=1,authorize=authorize)
        try:
            page=await asyncio.to_thread(discover)
            if not page['items']:
                self._s05_maintenance_cursors[account]=None
                return
            hint=page['items'][0]
            self._s05_maintenance_cursors[account]=(hint['chat_id'],hint['message_id'],hint['seq'])
            async def action(*,owner):
                if owner!=lifecycle.owner: raise ValueError('maintenance owner changed')
                return await callback(state,lifecycle,hint)
            await self._run_owner_aux_action(lifecycle.owner,action)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Only the disposable hint advances; the exact pending row remains.
            return

    async def _select_one_assistant_interpretation(self, profile, registry):
        """Select one never-attempted paired job on the existing owner lane."""
        from dataclasses import asdict
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        from src.memery_external_sources import SourceRef, AssistantInterpretationRef, _assistant_ref_error
        from src.openclank.memory_job_admission import _conn, _completed_turn_thread, _completed_turn_limits, reserve_assistant_interpretation, execute_assistant_interpretation, _revoke_completed_turn_reservation, _assistant_interpretation_evidence, _job_input_hash, _lookup_staged_assistant_interpretation_db, prepare_pending_assistant_rebind, pending_assistant_rebind_original
        cursor=self._s05_pair_cursor
        def rows():
            with _conn(profile.path) as db:
                return db.execute("SELECT j.owner_id,j.job_id,r.source_account_id,r.source_ref_json,r.coverage_json FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r USING(owner_id,job_id) WHERE j.kind='assistant_interpretation_review' AND j.state='queued' AND j.attempt_count=0 AND (j.owner_id>? OR (j.owner_id=? AND j.job_id>?)) ORDER BY j.owner_id,j.job_id LIMIT 32",(cursor[0],cursor[0],cursor[1])).fetchall()
        page,cancelled=await _completed_turn_thread(rows)
        if cancelled: raise asyncio.CancelledError
        if not page: self._s05_pair_cursor=('',''); return
        for row in page:
            self._s05_pair_cursor=(row['owner_id'],row['job_id'])
            entry=registry.get(row['source_account_id'])
            if entry is None: continue
            lifecycle,assignment,router=entry
            if row['owner_id']!=lifecycle.binding.memory_owner or self._owner_lifecycle_fenced(lifecycle.owner): continue
            try:
                root=SourceRef(**json.loads(row['source_ref_json'])); coverage=json.loads(row['coverage_json']); part=coverage['assistant']
                if set(coverage)!={'version','kind','assistant','pair_sha256'} or coverage['version']!=1 or coverage['kind']!='assistant_interpretation_review' or set(part)!={'message_id','part_id','revision','content_hash'}: continue
                pair=AssistantInterpretationRef(1,root,part['message_id'],part['part_id'],part['revision'],part['content_hash'],coverage['pair_sha256'])
                if _assistant_ref_error(pair) or root.account_id!=lifecycle.account_id or json.dumps(asdict(root),sort_keys=True,separators=(',',':'))!=row['source_ref_json']: continue
            except (ValueError,TypeError,KeyError): continue
            def enabled():
                with PREFERENCE_LOCK: return _load_for_user(lifecycle.owner).get('memory_assistant_interpretation') is True
            async def action(*,owner):
                if owner!=lifecycle.owner: raise ValueError('paired owner changed')
                on,cancelled=await _completed_turn_thread(enabled)
                if cancelled: raise asyncio.CancelledError
                if not on: return
                lease=None
                try:
                    async with lifecycle.mapping.async_chat_admission_lock(root.chat_id) as guard:
                        (_,limits),cancelled=await _completed_turn_thread(lambda: _completed_turn_limits(lifecycle.target_authority,assignment))
                        if cancelled: raise asyncio.CancelledError
                        def rebind_inputs():
                            from src.openclank.owner_custody_facts import verified_owner_custody_head
                            from src.openclank.memory_policy_projection import policy_coordinator
                            evidence,target,coverage,source_identity,_=_assistant_interpretation_evidence(source=pair,lifecycle_owner=lifecycle,guard=guard,target_authority=lifecycle.target_authority,assignment_authority=assignment)
                            _,captured_binding=_lookup_staged_assistant_interpretation_db(profile.path,source=pair,target=target,coverage=coverage,key=source_identity,expected_job_id=row['job_id'],_return_binding=True)
                            with _conn(profile.path) as db:
                                db.execute('BEGIN')
                                stored=db.execute("SELECT j.input_hash,r.binding_fingerprint FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r USING(owner_id,job_id) WHERE j.owner_id=? AND j.job_id=? AND j.kind='assistant_interpretation_review' AND j.state='queued' AND j.attempt_count=0",(target.memory_owner,row['job_id'])).fetchone()
                                if not stored: raise ValueError('paired original unavailable')
                                head=verified_owner_custody_head(db,database_id=profile.provider._guarded_preparation.database_id,account_id=root.account_id,current_owner=target.memory_owner)
                                db.rollback()
                            if stored['input_hash']==_job_input_hash('assistant_interpretation_review',coverage,evidence,target): return None
                            coordinator=policy_coordinator()
                            if coordinator is None: raise ValueError('paired policy unavailable')
                            refs=coordinator.checked_refs(lifecycle.owner,root.account_id,target.memory_workspace)
                            movement=None
                            if captured_binding.memory_owner!=target.memory_owner:
                                if head is None: raise ValueError('paired movement proof unavailable')
                                rename=head['request']; fields=('database_id','account_id','operation_id','step','generation','action','owner','new_owner')
                                phase={k:rename[k] for k in fields}
                                fence='cs_s04pairpending_'+hashlib.sha256(json.dumps(['s04.paired-pending-fence.v1',phase,row['job_id']],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
                                movement={'request':rename,'receipt_id':head['receipt_id'],'receipt_hash':head['receipt_hash'],'fence_id':fence}
                            key='s04pairrebind_'+hashlib.sha256(json.dumps([row['job_id'],stored['input_hash'],target.fingerprint,evidence.authority_fingerprint,evidence.policy_fingerprint],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
                            return refs,movement,key
                        pending=lifecycle.provider._paired_review_ticket
                        if type(pending) is dict and 'pending_pair_identity' in pending:
                            result=await pending_assistant_rebind_original(profile.path,source=pair,lifecycle_owner=lifecycle,guard=guard,target_authority=lifecycle.target_authority,assignment_authority=assignment,job_id=row['job_id'])
                            if result.get('state')!='rebound': return
                        rebind,cancelled=await _completed_turn_thread(rebind_inputs)
                        if cancelled: raise asyncio.CancelledError
                        if rebind is not None:
                            refs,movement,key=rebind
                            await prepare_pending_assistant_rebind(profile.path,source=pair,lifecycle_owner=lifecycle,guard=guard,target_authority=lifecycle.target_authority,assignment_authority=assignment,job_id=row['job_id'],request_key=key,policy_projection=refs,movement=movement)
                        # Qualify the actual local witness before consuming an attempt.
                        from src.openclank.chat_source_bridge import mint_review_currentness
                        (preflight,_),cancelled=await _completed_turn_thread(lambda: mint_review_currentness(
                            lifecycle_owner=lifecycle,sources=((root,guard),),assignment=assignment,
                            validate=lambda: _assistant_interpretation_evidence(source=pair,lifecycle_owner=lifecycle,guard=guard,
                                target_authority=lifecycle.target_authority,assignment_authority=assignment)))
                        preflight.close()
                        if cancelled: raise asyncio.CancelledError
                        lease=await reserve_assistant_interpretation(profile.path,source=pair,lifecycle_owner=lifecycle,guard=guard,
                            target_authority=lifecycle.target_authority,assignment_authority=assignment,job_id=row['job_id'],
                            worker_id='s05worker_'+uuid.uuid4().hex,limits=limits,
                            lifecycle_state=getattr(self._session_manager,'lifecycle_context',None))
                    if lease.get('state')=='deferred': return
                    await execute_assistant_interpretation(profile.path,source=pair,lifecycle_owner=lifecycle,target_authority=lifecycle.target_authority,
                        assignment_authority=assignment,job_id=row['job_id'],attempt_id=lease['attempt_id'],lease_epoch=lease['lease_epoch'],router=router,
                        reservation_deadline_tick=lease['reservation_deadline_tick'],worker_id=lease['worker_id'])
                except BaseException:
                    if lease and 'attempt_id' in lease and 'lease_epoch' in lease:
                        try:
                            await _completed_turn_thread(lambda: _revoke_completed_turn_reservation(profile.path,owner=lifecycle.binding.memory_owner,job_id=row['job_id'],attempt_id=lease['attempt_id'],lease_epoch=lease['lease_epoch'],profile=profile))
                        except Exception: pass  # Dispatched or unresolved work is never revoked.
                    raise
            try:
                barrier=getattr(getattr(getattr(self,'_session_manager',None),'lifecycle_context',None),'account_request_barrier',None)
                if barrier is None: raise ValueError('paired account barrier unavailable')
                async with barrier.admitted(lifecycle.owner): await self._run_owner_aux_action(lifecycle.owner,action)
            except ValueError: return
            return


    async def _reconcile_source_publication_ticket(self, profile, registry):
        """Read/settle only the exact retained user-source original."""
        from src.memery_external_sources import SourceRef
        from src.openclank.memory_job_admission import source_publication_original
        ticket=profile.provider._paired_review_ticket
        identity=ticket.get('source_publication_identity') if type(ticket) is dict else None
        if identity is None: return False
        try:
            identity=json.loads(json.dumps(identity,ensure_ascii=False,separators=(',',':'),allow_nan=False))
            source=SourceRef(**identity['source'])
            lifecycle,assignment,_=registry[source.account_id]
            if lifecycle.provider is not profile.provider or self._owner_lifecycle_fenced(lifecycle.owner):
                raise ValueError('source original unavailable')
            async def action(*,owner):
                if owner!=lifecycle.owner or profile.provider._paired_review_ticket is not ticket:
                    raise ValueError('source original changed')
                async with lifecycle.mapping.async_chat_admission_lock(source.chat_id) as guard:
                    return await source_publication_original(source=source,lifecycle_owner=lifecycle,guard=guard,
                        target_authority=lifecycle.target_authority,assignment_authority=assignment)
            barrier=getattr(getattr(getattr(self,'_session_manager',None),'lifecycle_context',None),'account_request_barrier',None)
            if barrier is None: raise ValueError('source account barrier unavailable')
            async with barrier.admitted(lifecycle.owner): await self._run_owner_aux_action(lifecycle.owner,action)
        except (ValueError,TypeError,KeyError):
            pass  # An unreadable original remains fenced, never resubmitted.
        return True

    async def _reconcile_paired_publication_ticket(self, profile, registry):
        """Only the retained exact publication may be read; no write or model retry."""
        from src.memery_external_sources import SourceRef, AssistantInterpretationRef, _assistant_ref_error
        from src.openclank.memory_job_admission import paired_publication_original
        ticket=profile.provider._paired_review_ticket
        identity=ticket.get('paired_publication_identity') if type(ticket) is dict else None
        if identity is None: return False
        try:
            identity=json.loads(json.dumps(identity,ensure_ascii=False,separators=(',',':'),allow_nan=False))
            root=SourceRef(**identity['source']); coverage=identity['coverage']; part=coverage['assistant']
            pair=AssistantInterpretationRef(1,root,part['message_id'],part['part_id'],part['revision'],part['content_hash'],coverage['pair_sha256'])
            if _assistant_ref_error(pair): raise ValueError('paired original unavailable')
            lifecycle,assignment,_=registry[root.account_id]
            if lifecycle.provider is not profile.provider or self._owner_lifecycle_fenced(lifecycle.owner): raise ValueError('paired original unavailable')
            async def action(*,owner):
                if owner!=lifecycle.owner or profile.provider._paired_review_ticket is not ticket: raise ValueError('paired original changed')
                async with lifecycle.mapping.async_chat_admission_lock(root.chat_id) as guard:
                    return await paired_publication_original(source=pair,lifecycle_owner=lifecycle,guard=guard,
                        target_authority=lifecycle.target_authority,assignment_authority=assignment,job_id=identity['job_id'],
                        attempt_id=identity['attempt_id'],lease_epoch=identity['lease_epoch'])
            barrier=getattr(getattr(getattr(self,'_session_manager',None),'lifecycle_context',None),'account_request_barrier',None)
            if barrier is None: raise ValueError('paired account barrier unavailable')
            async with barrier.admitted(lifecycle.owner): await self._run_owner_aux_action(lifecycle.owner,action)
        except (ValueError,TypeError,KeyError):
            pass  # Exact original remains fenced; a failed read is not permission to send.
        return True


    async def _stage_one_assistant_component_chat(self, profile, registry):
        """Stage one bounded exact archive prefix under the existing owner admission."""
        from sqlalchemy import text
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        from src.memery_external_sources import SourceRef, assistant_interpretation_ref
        from src.openclank.memory_job_admission import _completed_turn_limits, _completed_turn_thread, stage_assistant_interpretation, _terminal_pair_context, _full_pair_terminal_db, lookup_full_pair_terminal, lookup_terminal_assistant_interpretation, _conn, _exact_json
        try: trace=logger.isEnabledFor(logging.DEBUG)
        except Exception: trace=False
        started=time.monotonic() if trace else 0.0
        phase='lifecycle'; phase_started=started; timings={}; outcome='unavailable'
        def mark(name):
            nonlocal phase,phase_started
            if trace:
                now=time.monotonic();timings[phase]=round((now-phase_started)*1000,3);phase_started=now
            phase=name
        def emit(category):
            if not trace: return
            try:
                now=time.monotonic();timings[phase]=round((now-phase_started)*1000,3)
                logger.debug('S14_PAIRED_STAGE outcome=%s phase=%s exception=%s elapsed_ms=%s phases_ms=%s',outcome,phase,category,round((now-started)*1000,3),timings)
            except Exception: pass
        accounts=sorted(registry)
        if not accounts:
            outcome='no_owner';emit('none');return
        account=next((item for item in accounts if item>self._s05_pair_stage_owner),accounts[0])
        self._s05_pair_stage_owner=account
        lifecycle,assignment,_router=registry[account]
        after=self._s05_pair_stage_cursors.get(account,0)
        source=lifecycle.source_authority
        def enabled():
            with PREFERENCE_LOCK:
                prefs=_load_for_user(lifecycle.owner)
                return type(prefs) is dict and prefs.get('memory_assistant_interpretation') is True
        def authorize(candidate,chat):
            if candidate!=account: return False
            db=source._session_factory()
            try:
                row=db.execute(text('SELECT owner FROM sessions WHERE id=:chat'),{'chat':chat}).first()
                return bool(row and source._auth.account_id(row[0])==account)
            finally: db.close()
        async def action(*,owner):
            nonlocal outcome
            mark('action_entry')
            if owner!=lifecycle.owner: raise ValueError('paired owner changed')
            mark('preference')
            on,cancelled=await _completed_turn_thread(enabled)
            if cancelled: raise asyncio.CancelledError
            mark('discovery')
            page,cancelled=await _completed_turn_thread(lambda: source._archive.pending_assistant_component_page(owner=account,authorize=authorize,after_seq=after,limit=32))
            if cancelled: raise asyncio.CancelledError
            if not page['hints']:
                outcome='no_hint';return page['next_seq']
            hint=page['hints'][0];chat=hint['chat_id']
            mark('guarded_page')
            async with lifecycle.mapping.async_chat_admission_lock(chat) as guard:
                page,cancelled=await _completed_turn_thread(lambda: source._archive.read_assistant_component_page(owner=account,chat_id=chat,authorize=authorize,limit=32))
                if cancelled: raise asyncio.CancelledError
                if not page['events']:
                    outcome='no_event';return hint['seq']
                mark('pair_ref')
                event=page['events'][0];component=event['component'];root=component['root']
                ref=SourceRef(2 if component['workspace_id'] is None else 1,'host_user_body',account,chat,'main',root[0],root[1],root[2],root[3],component['workspace_id'],None)
                pair=assistant_interpretation_ref(ref,component)
                mark('terminal_context')
                target,coverage,_=await _paired_scheduler_owned(lambda: _terminal_pair_context(pair,lifecycle,guard,lifecycle.target_authority))
                mark('original_lookup')
                terminal=await _paired_scheduler_owned(lambda: _full_pair_terminal_db(profile.path,source=pair,target=target,coverage=coverage,expected_job_id=None))
                if terminal is not None:
                    await lookup_full_pair_terminal(profile.path,source=pair,lifecycle_owner=lifecycle,guard=guard,target_authority=lifecycle.target_authority)
                    entry={'event':event,'source':pair,'disposition':'full_pair_terminal'}
                else:
                    from dataclasses import asdict
                    source_key=hashlib.sha256(_exact_json(['assistant_interpretation_review',asdict(ref),coverage]).encode()).hexdigest()
                    def retained():
                        with _conn(profile.path) as db:
                            return db.execute("SELECT j.job_id,length(CAST(j.result_json AS BLOB)) AS result_size,CASE WHEN length(CAST(j.result_json AS BLOB))<=262144 THEN j.result_json END AS result_json FROM fm_v2_jobs j JOIN fm_v2_s05_job_refs r USING(owner_id,job_id) WHERE j.owner_id=? AND r.source_key=? AND j.kind='assistant_interpretation_review' LIMIT 2",(target.memory_owner,source_key)).fetchall()
                    matches=await _paired_scheduler_owned(retained)
                    if len(matches)>1: raise ValueError('paired source identity ambiguous')
                    marker=None
                    if matches and matches[0]['result_size'] is not None and matches[0]['result_json'] is None:
                        raise ValueError('paired terminal evidence exceeds bound')
                    if matches and matches[0]['result_json'] is not None:
                        try: marker=json.loads(matches[0]['result_json'])
                        except (ValueError,TypeError,RecursionError): raise ValueError('paired terminal evidence unavailable') from None
                    if type(marker) is dict and marker.get('version')=='s04.ingest-erased.v1':
                        job_id=matches[0]['job_id']
                        await lookup_terminal_assistant_interpretation(profile.path,source=pair,lifecycle_owner=lifecycle,guard=guard,target_authority=lifecycle.target_authority,assignment_authority=assignment,expected_job_id=job_id)
                        entry={'event':event,'source':pair,'job_id':job_id,'disposition':'terminal_erased'}
                    else:
                        if not on:
                            outcome='preference_off';return None
                        mark('limits')
                        _,limits=await _paired_scheduler_owned(lambda: _completed_turn_limits(lifecycle.target_authority,assignment))
                        mark('stage')
                        fact=await stage_assistant_interpretation(profile.path,source=pair,lifecycle_owner=lifecycle,guard=guard,
                            target_authority=lifecycle.target_authority,assignment_authority=assignment,limits=limits,now_ms=time.time_ns()//1_000_000)
                        entry={'event':event,'source':pair,'job_id':fact['job_id'],'disposition':'staged'}
                on,cancelled=await _completed_turn_thread(enabled)
                if cancelled: raise asyncio.CancelledError
                if entry['disposition']=='staged' and not on: raise ValueError('paired source choice changed')
                mark('ack')
                await source._archive.ack_staged_assistant_components(owner=account,chat_id=chat,lifecycle_owner=lifecycle,guard=guard,
                    target_authority=lifecycle.target_authority,assignment_authority=assignment,expected_cursor=page['cursor'],
                    entries=[entry])
            outcome='acknowledged'
            return hint['seq']
        async def _paired_scheduler_owned(fn):
            value,cancelled=await _completed_turn_thread(fn)
            if cancelled: raise asyncio.CancelledError
            return value
        category='none'
        try:
            mark('lifecycle')
            barrier=getattr(getattr(getattr(self,'_session_manager',None),'lifecycle_context',None),'account_request_barrier',None)
            if barrier is None: raise ValueError('paired account barrier unavailable')
            async with barrier.admitted(lifecycle.owner):
                next_seq=await self._run_owner_aux_action(lifecycle.owner,action)
            mark('cursor_commit')
            if next_seq is not None: self._s05_pair_stage_cursors[account]=next_seq
            elif outcome=='unavailable': outcome='owner_unavailable'
        except asyncio.CancelledError:
            outcome='cancelled';category='cancelled';raise
        except Exception as exc:
            outcome='exception'
            category=type(exc).__name__ if type(exc) in (ValueError,TypeError,KeyError,TimeoutError) else 'other'
            return
        finally:
            emit(category)


    async def _stage_one_completed_turn_chat(self, profile, registry):
        """Consume at most one authorized archive prefix without blocking the loop."""
        from sqlalchemy import text
        from src.openclank.memory_job_admission import _completed_turn_limits, stage_completed_turn_page
        accounts = sorted(registry)
        if not accounts:
            return
        account = next((value for value in accounts if value > self._s05_stage_owner), accounts[0])
        self._s05_stage_owner = account
        lifecycle,assignment,_router = registry[account]
        after = self._s05_stage_cursors.get(account,0)
        journal_chat = {}
        def stage():
            profile.assert_current()
            source = lifecycle.source_authority
            if (source._auth.account_id(lifecycle.owner) != account
                or source._auth.username_for_account_id(account) != lifecycle.owner
                or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)
                or lifecycle.target_authority.resolve(account) != lifecycle.binding):
                raise ValueError('completed-turn owner unavailable')
            def authorize(candidate,chat):
                if candidate != account:
                    return False
                db = source._session_factory()
                try:
                    row = db.execute(text('SELECT owner FROM sessions WHERE id=:chat'),{'chat':chat}).first()
                    return bool(row and source._auth.account_id(row[0]) == account)
                finally:
                    db.close()
            page = source._archive.pending_completed_turn_chats(owner=account,authorize=authorize,after_seq=after,limit=32)
            if not page['hints']:
                pending = self._journal_settle.get(account)
                if pending:
                    journal_chat['id'] = pending.pop()   # settle a finished journal run (status -> proof -> ACK) on a wake without hints
                return page['next_seq']
            hint = page['hints'][0]
            journal_chat['id'] = hint['chat_id']
            try:
                with lifecycle.mapping.chat_admission_lock(hint['chat_id']) as guard:
                    try:
                        _base, limits = _completed_turn_limits(lifecycle.target_authority,assignment)
                    except (ValueError, KeyError, TypeError):
                        limits = None  # Only a proven retirement may advance without policy.
                    stage_completed_turn_page(profile.path,owner=lifecycle.owner,chat_id=hint['chat_id'],
                        target_authority=lifecycle.target_authority,assignment_authority=assignment,limits=limits,
                        now_ms=time.time_ns() // 1_000_000,lifecycle_owner=lifecycle,guard=guard)
            except Exception:
                # Only the in-memory discovery hint advances; durable cursor
                # acknowledgment remains exclusively in the staging adapter.
                return hint['seq']
            return hint['seq']
        async def action(*, owner):
            if owner != lifecycle.owner:
                raise ValueError('completed-turn owner changed')
            work = asyncio.create_task(asyncio.to_thread(stage))
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                # Lifecycle shutdown must join the in-flight staging thread.
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if work.done() and not work.cancelled():
                    work.exception()
                raise
        try:
            barrier = getattr(getattr(getattr(self, '_session_manager', None), 'lifecycle_context', None), 'account_request_barrier', None)
            if barrier is None:
                raise ValueError('completed-turn account barrier unavailable')
            async with barrier.admitted(lifecycle.owner):
                next_seq = await self._run_owner_aux_action(lifecycle.owner,action)
                if journal_chat:
                    # BQ-8: journal delta pass (default Off; read -> select -> status/ACK; never dispatches inference, never a timer).
                    try:
                        from src.openclank.fm_journal_delta import run_chat_pass
                        # H8: this wake only selects/stages and settles (status -> proof -> ACK); it never runs the model.
                        staged = []
                        await run_chat_pass(profile=profile,lifecycle=lifecycle,assignment=assignment,account_id=account,chat_id=journal_chat['id'],staged=staged,automatic=True)
                        if staged: self._journal_staged.setdefault(account,{})[journal_chat['id']] = staged[0]
                    except Exception as exc:
                        logger.warning('journal delta pass skipped: %s', type(exc).__name__)
            if next_seq is not None:
                self._s05_stage_cursors[account] = next_seq
        except Exception:
            # The outbox remains authoritative; no cursor is acknowledged here.
            return

    def _wake_completed_turn(self):
        """Start at most one owned one-shot without blocking the scheduler loop."""
        if not self._running or (self._s05_controller is None and self._s05_registry_state is None) or (self._s05_handle and not self._s05_handle.done()):
            return
        handle = asyncio.create_task(self._refresh_and_select_completed_turn())
        self._s05_handle = handle
        def finished(task):
            if self._s05_handle is task:
                self._s05_handle = None
            if not task.cancelled() and task.exception() is not None:
                logger.warning('Completed-turn background review unavailable')
        handle.add_done_callback(finished)

    def request_completed_turn_wake(self):
        """A nonblocking hint; the durable archive remains the delivery source."""
        if self._s05_registry_state is not None:
            self._s05_registry_generation += 1
            self._s05_registry_dirty = True
        self._wake_completed_turn()

    async def _check_due_tasks(self):
        from core.database import SessionLocal, ScheduledTask
        db = SessionLocal()
        try:
            now = _utcnow()
            foreground_active = False
            try:
                from src.interactive_gate import has_foreground_activity
                foreground_active = has_foreground_activity()
            except Exception:
                foreground_active = False
            async with self._executing_lock:
                # Snapshot under the lock so we don't race with mid-iteration adds.
                executing_snapshot = set(self._executing)
                # Scheduled tasks and deferred event tasks both use next_run.
                due = db.query(ScheduledTask).filter(
                    ScheduledTask.status == "active",
                    ScheduledTask.next_run <= now,
                    ScheduledTask.id.notin_(executing_snapshot) if executing_snapshot else True,
                ).all()
                to_dispatch = []
                for task in due:
                    if task.id in self._executing:
                        continue
                    if self._owner_lifecycle_fenced(task.owner):
                        continue
                    if foreground_active and not self._is_theme_emoji_task(task):
                        task.next_run = now + timedelta(minutes=15)
                        continue
                    self._executing.add(task.id)
                    to_dispatch.append(task.id)
                if foreground_active and due:
                    db.commit()
            for task_id in to_dispatch:
                asyncio.create_task(self._execute_task(task_id))
        finally:
            db.close()

    def _manual_task_current(self, task_id, expected):
        if expected is None:
            return True
        from core.database import SessionLocal, ScheduledTask
        from src.openclank.task_execution_context import manual_task_sha256
        try:
            auth = getattr(self, '_auth_manager', None)
            if (not auth or auth.account_id(expected['owner']) != expected['account']
                    or auth.username_for_account_id(expected['account']) != expected['owner']):
                return False
            with SessionLocal() as db:
                task = db.query(ScheduledTask).filter_by(id=task_id, owner=expected['owner']).first()
                if not task or task.host_account_id != expected['bound_account'] or task.status != expected['status']:
                    return False
                return manual_task_sha256(task) == expected['spec']
        except Exception:
            return False

    def _skip_selected_manual_run(self, task_id, run_id):
        """Mark only a proven queued selected occurrence as skipped."""
        if not run_id: return
        from core.database import SessionLocal, TaskRun
        try:
            with SessionLocal() as db:
                run = db.query(TaskRun).filter_by(id=run_id, task_id=task_id, status='queued').first()
                if run:
                    run.status = 'skipped'; run.finished_at = _utcnow()
                    run.error = 'Task account, configuration or admission changed before execution'
                    db.commit()
        except Exception:
            logger.warning('Selected manual run outcome remains unknown')

    async def _execute_task(self, task_id: str, *, bypass_model_slot: bool = False, release_executing: bool = True, manual_expected=None, selected_run_id=None):
        # Create the run record with status="queued" BEFORE waiting on the
        # semaphore so the UI can show that a manually-triggered task is in
        # line behind another. Once we acquire the slot, flip to "running"
        # and hand off to _execute_task_locked.
        from core.database import SessionLocal, TaskRun
        if self._task_owner_lifecycle_fenced(task_id) or not self._manual_task_current(task_id, manual_expected):
            self._skip_selected_manual_run(task_id, selected_run_id)
            if release_executing:
                async with self._executing_lock:
                    self._executing.discard(task_id)
            return
        current = asyncio.current_task()
        if current:
            # There is no await between the fence check and registration.  A
            # lifecycle claim therefore either precedes the check (and returns
            # above) or follows registration (and quiesce can see this handle).
            # Never self-cancel from the registration helper: that bypassed the
            # orderly early-return path and leaked CancelledError to callers.
            self._register_task_handle(task_id, current)
        # S12: prevent overlapping recurrence from replacing a waiting run.
        # If a previous run is waiting for a user answer, queue this trigger
        # behind it rather than starting a competing run.
        if self._has_waiting_run(task_id):
            self._skip_selected_manual_run(task_id, selected_run_id)
            logger.info("Task %s has a waiting run; skipping overlapping trigger", task_id)
            if current:
                self._unregister_task_handle(task_id, current)
            if release_executing:
                async with self._executing_lock:
                    self._executing.discard(task_id)
            return
        if selected_run_id is not None:
            # A selected occurrence is already durable; never create or adopt it.
            run_id = selected_run_id
            with SessionLocal() as db:
                run = db.query(TaskRun).filter_by(id=run_id, task_id=task_id, status='queued').first()
                if not run:
                    if current: self._unregister_task_handle(task_id, current)
                    if release_executing:
                        async with self._executing_lock: self._executing.discard(task_id)
                    return
        else:
            run_id = str(uuid.uuid4())
            _q_db = SessionLocal()
            try:
                run = TaskRun(
                    id=run_id,
                    task_id=task_id,
                    started_at=_utcnow(),
                    status="queued",
                    result="Queued — waiting for a free slot…",
                )
                _q_db.add(run)
                _q_db.commit()
            except Exception:
                logger.exception(f"Failed to create queued run row for task {task_id}")
            finally:
                _q_db.close()

        try:
            # Lazy fallback keeps restart/test harnesses that construct the
            # scheduler without ``__init__`` compatible with the new fence.
            task_run_locks = getattr(self, "_task_run_locks", None)
            if task_run_locks is None:
                task_run_locks = self._task_run_locks = {}
            task_run_lock = task_run_locks.setdefault(
                task_id,
                asyncio.Lock(),
            )
            async with task_run_lock:
                if bypass_model_slot or not self._task_needs_model_slot(task_id):
                    await self._execute_task_locked(
                        task_id,
                        run_id,
                        release_executing=release_executing,
                        gate_foreground=not bypass_model_slot,
                        manual_expected=manual_expected, selected_run=selected_run_id is not None,
                    )
                    return

                async with self._run_semaphore:
                    await self._execute_task_locked(
                        task_id,
                        run_id,
                        release_executing=release_executing,
                        gate_foreground=True,
                        manual_expected=manual_expected, selected_run=selected_run_id is not None,
                    )
        except asyncio.CancelledError:
            # If cancellation happens while queued behind the semaphore,
            # _execute_task_locked never runs and cannot update the Activity row.
            self._mark_run_aborted(task_id, run_id)
            self._defer_immediately_due_task(task_id, delay=timedelta(minutes=15))
            raise
        finally:
            if current:
                self._unregister_task_handle(task_id, current)
            if release_executing:
                async with self._executing_lock:
                    self._executing.discard(task_id)

    def _defer_immediately_due_task(self, task_id: str, *, delay: timedelta):
        """A queued task can be cancelled before _execute_task_locked gets a DB
        handle. If its next_run stays in the past, the scheduler dispatches it
        again on the next tick and spams aborted Activity rows."""
        try:
            from core.database import SessionLocal, ScheduledTask
            db = SessionLocal()
            try:
                task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                if (
                    task
                    and task.status == "active"
                    and task.next_run is not None
                    and task.next_run <= _utcnow()
                ):
                    task.next_run = _utcnow() + delay
                    db.commit()
            finally:
                db.close()
        except Exception:
            logger.debug("Failed to defer cancelled queued task %s", task_id, exc_info=True)

    async def _execute_task_locked(
        self,
        task_id: str,
        run_id: str,
        *,
        release_executing: bool = True,
        gate_foreground: bool = True,
        manual_expected=None,
        selected_run=False,
    ):
        from core.database import SessionLocal, ScheduledTask, TaskRun

        db = SessionLocal()
        try:
            task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
            if selected_run and not db.query(TaskRun.id).filter_by(id=run_id, task_id=task_id, status='queued').first():
                return
            if not task or task.status != "active":
                # Task was paused/deleted while queued — record that outcome
                # so the run row doesn't sit as "queued" forever.
                stale = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if stale and stale.status == "queued":
                    stale.status = "skipped"
                    stale.finished_at = _utcnow()
                    stale.error = f"Task no longer active (status={task.status if task else 'deleted'})"
                    db.commit()
                return

            if self._owner_lifecycle_fenced(task.owner):
                blocked = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if blocked and blocked.status in ("queued", "running"):
                    blocked.status = "aborted"
                    blocked.result = "Account lifecycle operation in progress"
                    blocked.error = "Account lifecycle operation in progress"
                    blocked.finished_at = _utcnow()
                    db.commit()
                return

            if (
                is_admin_only_task_action(task.task_type, task.action)
                and not owner_has_admin_task_privileges(task.owner)
            ):
                msg = f"Action '{task.action}' requires admin privileges"
                blocked = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if blocked:
                    blocked.status = "error"
                    blocked.result = msg
                    blocked.error = msg
                    blocked.finished_at = _utcnow()
                task.status = "paused"
                task.next_run = None
                task.last_run = _utcnow()
                logger.warning(
                    "Paused admin-only task %s for non-admin owner %r",
                    task_id,
                    task.owner,
                )
                db.commit()
                return

            gate_foreground = gate_foreground and not self._is_theme_emoji_task(task)
            if gate_foreground:
                waiting = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if waiting and waiting.status == "queued":
                    waiting.result = "Queued — waiting for Open Clank to be idle…"
                    db.commit()
                from src.interactive_gate import wait_for_interactive_quiet
                await wait_for_interactive_quiet(f"scheduled task {task.name}")

            if not self._manual_task_current(task_id, manual_expected):
                db.expire_all()
                stale = db.query(TaskRun).filter_by(id=run_id, task_id=task_id).first()
                if stale and stale.status == 'queued':
                    stale.status = 'skipped'
                    stale.error = 'Task account or configuration changed before execution'
                    stale.finished_at = _utcnow()
                    db.commit()
                return
            db.expire_all()
            task = db.query(ScheduledTask).filter_by(id=task_id).first()
            if not task:
                return

            # Flip the run from queued → running. Reset started_at to the
            # actual execution start so queue wait time is visible from
            # created_at vs started_at if we ever surface that.
            run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
            if selected_run and (not run or run.task_id != task_id or run.status != 'queued'):
                return
            task._execution_context_original_run = bool(run and run.task_id == task.id and run.status == 'queued')
            if selected_run:
                changed = db.query(TaskRun).filter_by(id=run_id, task_id=task_id, status='queued').update(
                    {'status':'running', 'started_at':_utcnow(), 'result':'Starting…'}, synchronize_session=False)
                if changed != 1:
                    db.rollback()
                    return
                db.commit()
                db.expire_all()
                run = db.query(TaskRun).filter_by(id=run_id, task_id=task_id).first()
                if not run: return
            elif run:
                run.status = "running"
                run.started_at = _utcnow()
                run.result = "Starting…"
                db.commit()
            else:
                if selected_run:
                    return
                # Defensive: row may have been wiped; recreate so the rest of
                # the code can look it up by run_id without crashing.
                run = TaskRun(
                    id=run_id,
                    task_id=task.id,
                    started_at=_utcnow(),
                    status="running",
                    result="Starting…",
                )
                db.add(run)
                db.commit()

            # A durable task may outlive, or race with, a policy reset. Resolve
            # its opaque Workspace through the current People + Agent/read
            # policy after it leaves the queue but before any action, research
            # worker, tool selection, or managed Agent spawn can begin.
            self._resolve_task_workspace(task)

            task_type = task.task_type or "llm"

            from src.builtin_actions import TaskDeferred, TaskNoop

            # Cleared each run so an action task (no model) doesn't inherit a
            # previous llm/research run's model. The executors set it once the
            # model is resolved.
            self._last_run_model = None
            foreground_cancel = {"hit": False}
            foreground_monitor = None
            if gate_foreground:
                current_task = asyncio.current_task()

                async def _cancel_if_foreground_active():
                    # Give the just-finished quiet gate a tiny grace window,
                    # then keep enforcing "background means background" while
                    # a long email/LLM action is already running.
                    await asyncio.sleep(0.1)
                    from src.interactive_gate import has_foreground_activity
                    while True:
                        await asyncio.sleep(0.25)
                        if has_foreground_activity():
                            foreground_cancel["hit"] = True
                            logger.info("Task '%s' interrupted because Open Clank became active", task.name)
                            if current_task:
                                current_task.cancel()
                            return

                foreground_monitor = asyncio.create_task(_cancel_if_foreground_active())
            try:
                if task_type == "action":
                    # A bound account remains admitted while the actual action
                    # drains, including its existing to_thread subprocess path.
                    # Actions do not consume the task's workspace binding, so
                    # this does not mint an execution-workspace witness.
                    account = getattr(task, 'host_account_id', None)
                    admission = None
                    barrier = getattr(getattr(getattr(self, '_session_manager', None), 'lifecycle_context', None), 'account_request_barrier', None)
                    if account:
                        if (not barrier or not self._auth_manager
                                or self._auth_manager.account_id(task.owner) != account
                                or self._auth_manager.username_for_account_id(account) != task.owner):
                            raise RuntimeError('TASK_ACCOUNT_PROVENANCE_UNAVAILABLE: account changed')
                        admission = await barrier.acquire(task.owner)
                    try:
                        if admission and (not await barrier.is_live(task.owner, admission)
                                or self._auth_manager.account_id(task.owner) != account
                                or self._auth_manager.username_for_account_id(account) != task.owner):
                            raise RuntimeError('TASK_ACCOUNT_PROVENANCE_UNAVAILABLE: account changed')
                        if admission:
                            from src.openclank.task_execution_context import TASKSPEC_FIELDS, taskspec_sha256
                            def spec(row):
                                return {key: int(value) if type(value) is bool else value
                                        for key in TASKSPEC_FIELDS for value in [getattr(row, key)]}
                            with SessionLocal() as check_db:
                                current = check_db.query(ScheduledTask).filter_by(id=task.id).first()
                                if (not current or current.owner != task.owner or current.host_account_id != account
                                        or current.status != 'active' or taskspec_sha256(spec(current)) != taskspec_sha256(spec(task))):
                                    raise RuntimeError('TASK_ACCOUNT_PROVENANCE_UNAVAILABLE: task changed')
                        action = asyncio.create_task(self._execute_action(task, run_id=run_id))
                        try:
                            result, success = await asyncio.shield(action)
                        except asyncio.CancelledError:
                            if self._is_theme_emoji_task(task):
                                action.cancel()
                            while not action.done():
                                try:
                                    await asyncio.shield(action)
                                except asyncio.CancelledError:
                                    continue
                                except Exception:
                                    break
                            if not action.cancelled():
                                action.exception()
                            raise
                    finally:
                        if admission:
                            await barrier.release(admission)
                    run.status = "success" if success else "error"
                    run.result = result
                    if not success:
                        run.error = result
                elif task_type == "research":
                    result = await self._execute_research_task(
                        task,
                        db,
                        root_operation_id=f"turn-task-run:{run_id}",
                    )
                    db.refresh(run)
                    run.status = "success"
                    run.error = None
                    run.result = result
                else:
                    # LLM task — use agent loop for tool access
                    result = await self._execute_llm_task(
                        task,
                        db,
                        root_operation_id=f"turn-task-run:{run_id}",
                    )
                    db.refresh(run)
                    run.status = "success"
                    run.error = None
                    run.result = result
                # Record which model actually ran (resolved inside the executor).
                if getattr(self, "_last_run_model", None):
                    run.model = self._last_run_model
                if run.status == "success":
                    await self._deliver_task_result(task, result, db, model=getattr(self, "_last_run_model", None))
            except TaskDeferred as defer:
                db.expire_all()
                deferred_run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if deferred_run is not None and deferred_run.status in ('outcome_unknown', 'issued_waiting', 'issued_resuming'):
                    deferred_run.error = 'Agent outcome unknown after interruption'
                    deferred_run.finished_at = _utcnow()
                    db.commit()
                    return
                if getattr(defer, 'pause', False) and task.action == 'theme_emoji_refresh':
                    run_obj = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                    if run_obj:
                        run_obj.status = 'error'
                        run_obj.error = 'Source retry delay exceeds the automatic horizon; updates paused.'
                        run_obj.finished_at = _utcnow()
                    task.status, task.next_run = 'paused', None
                    db.commit()
                    return
                count = self._task_defer_counts.get(task_id, 0) + 1
                self._task_defer_counts[task_id] = count
                delay_seconds = int(getattr(defer, "delay_seconds", 20 * 60) or (20 * 60))
                if count > 2:
                    delay_seconds = max(delay_seconds, 40 * 60)
                when = _utcnow() + timedelta(seconds=delay_seconds)
                logger.info(
                    "Task '%s' deferred for %ss after %s quiet-window hit(s): %s",
                    task.name, delay_seconds, count, defer,
                )
                run_obj = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if run_obj:
                    if self._is_theme_emoji_task(task):
                        run_obj.status = 'skipped'
                        run_obj.result = self._THEME_EMOJI_RETRY_DEFERRED
                        run_obj.finished_at = _utcnow()
                    else:
                        db.delete(run_obj)
                task.next_run = when
                db.commit()
                return
            except TaskWaiting as waiting:
                # S12: run ended while a durable wait request is pending.
                # Leave the run in "waiting" status — the persisted request
                # will be answered through the original task chat and the run
                # resumed. Do not mark terminal or deliver a result.
                logger.info(
                    "Task '%s' run %s waiting for user answer",
                    task.name, waiting.run_id,
                )
                return
            except asyncio.CancelledError:
                # The separately committed occurrence marker wins over a
                # stale ORM identity-map copy after an issued Agent turn.
                db.expire_all()
                current_run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if current_run is not None and current_run.status in ('outcome_unknown', 'issued_waiting', 'issued_resuming'):
                    current_run.error = 'Agent outcome unknown after interruption'
                    current_run.finished_at = _utcnow()
                    db.commit()
                    return
                msg = (
                    "Paused because Open Clank became active"
                    if foreground_cancel.get("hit")
                    else "Stopped by user"
                )
                logger.info("Task '%s' %s", task.name, msg)
                run_obj = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if run_obj:
                    run_obj.status = "aborted"
                    run_obj.error = msg
                    run_obj.result = run_obj.result or msg
                    run_obj.finished_at = _utcnow()
                task.last_run = _utcnow()
                if foreground_cancel.get("hit"):
                    task.next_run = _utcnow() + timedelta(minutes=15)
                elif (task.trigger_type or "schedule") == "schedule":
                    task.next_run = compute_next_run(
                        task.schedule, task.scheduled_time,
                        task.scheduled_day, task.scheduled_date,
                        after=_utcnow(),
                        cron_expression=task.cron_expression,
                        tz_name=_resolve_task_timezone(db, task),
                    )
                else:
                    task.next_run = None
                db.commit()
                return
            except TaskNoop as noop:
                # Action reported "nothing to do". Mark the run as `skipped`
                # with the reason in `result` so it surfaces in Activity as a
                # slim "skipped — <reason>" row instead of vanishing silently.
                # (Previous behavior was `db.delete(run)`, which made the user
                # think queued tasks had been dropped on the floor.)
                logger.info(f"Task '{task.name}' no-op: {noop}")
                run.status = "skipped"
                run.result = str(noop)
                run.finished_at = _utcnow()
                task.last_run = _utcnow()
                if (task.trigger_type or "schedule") == "schedule":
                    task.next_run = compute_next_run(
                        task.schedule, task.scheduled_time,
                        task.scheduled_day, task.scheduled_date,
                        after=_utcnow(),
                        cron_expression=task.cron_expression,
                        tz_name=_resolve_task_timezone(db, task),
                    )
                else:
                    task.next_run = None
                db.commit()
                return
            finally:
                if foreground_monitor and not foreground_monitor.done():
                    foreground_monitor.cancel()
                    try:
                        await foreground_monitor
                    except asyncio.CancelledError:
                        pass

            run.finished_at = _utcnow()

            # Update task
            task.last_run = _utcnow()
            task.run_count = (task.run_count or 0) + 1
            self._task_defer_counts.pop(task_id, None)

            # Compute next run only for schedule-triggered tasks
            if (task.trigger_type or "schedule") == "schedule":
                task.next_run = compute_next_run(
                    task.schedule, task.scheduled_time,
                    task.scheduled_day, task.scheduled_date,
                    after=_utcnow(),
                    cron_expression=task.cron_expression,
                    tz_name=_resolve_task_timezone(db, task),
                )
                if task.next_run is None and task.schedule == "once":
                    task.status = "completed"
            else:
                task.next_run = None

            db.commit()
            if (run.status == "success" and task.owner and (task.trigger_type or "schedule") == "schedule"
                    and not self._is_theme_emoji_task(task)):
                from src.openclank.achievement_producers import record_worker_activity
                record_worker_activity(self._auth_manager, task.owner, "scheduled.task.run.completed", str(run_id), {
                    "createdByOwner": True, "scheduled": True, "runStatus": "success", "taskId": str(task_id),
                    "sessionId": getattr(task, "session_id", None),
                }, workspace_id=getattr(task, "workspace_id", None))
            logger.info(f"Task '{task.name}' completed (run {run_id})")
            output = task.output_target or "session"
            # Per-task notification gate. Default True (notifications_enabled
            # defaults to True at column level), but skip when the user has
            # explicitly turned them off for this task — quiets chatty
            # housekeeping cron tasks without disabling them entirely.
            should_notify = (
                (task.task_type or "llm") in {"llm", "research"}
                and getattr(task, "notifications_enabled", True)
            )
            if should_notify:
                self.add_notification(
                    task.name,
                    run.status,
                    task_id,
                    owner=task.owner,
                    body=run.result if output == "notification" else None,
                )
            elif run.status == "error":
                self.add_notification(
                    task.name,
                    "error",
                    task_id,
                    owner=task.owner,
                    body=run.error or run.result,
                )

            # Log result to the assistant chat so all task activity is visible.
            # Skip skipped/error rows — user shouldn't see "skipped: …" noise
            # for cron tasks that no-op'd, or duplicate error spam for tasks
            # that already fired an error notification above.
            if run.status == "success":
                self._log_to_assistant(db, task, run.result or "[success]")

            # Task chaining — trigger the next task on success
            if run.status == "success" and task.then_task_id:
                chain_id = task.then_task_id
                chain_task = db.query(ScheduledTask).filter(ScheduledTask.id == chain_id).first()
                if not chain_task or chain_task.owner != task.owner:
                    logger.warning(
                        "Skipping chain from %r: target task %s is missing or not owned by %r",
                        task.name, chain_id, task.owner,
                    )
                elif not self._has_chain_cycle(db, chain_id, owner=task.owner):
                    logger.info(f"Chaining: '{task.name}' → task {chain_id}")
                    asyncio.create_task(self._run_chained(chain_id))
                else:
                    logger.warning(f"Skipping chain from '{task.name}': cycle detected")

        except Exception as exec_exc:
            logger.exception(f"Task {task_id} execution error")
            # Fetch the task's owner so the error notification reaches
            # the same user the success notification would have.
            _owner = None
            try:
                _t = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                _owner = _t.owner if _t else None
            except Exception:
                pass
            _should_notify_error = False
            try:
                _t_for_notify = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                _should_notify_error = (
                    bool(_t_for_notify)
                    and (_t_for_notify.task_type or "llm") in {"llm", "research"}
                    and getattr(_t_for_notify, "notifications_enabled", True)
                )
            except Exception:
                _should_notify_error = False
            if _should_notify_error:
                self.add_notification(f"Task {task_id}", "error", task_id, owner=_owner)
            try:
                # Persist the actual exception message so the UI can show it
                err_text = f"{type(exec_exc).__name__}: {exec_exc}"
                db.expire_all()
                run_obj = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if run_obj and run_obj.status in ("outcome_unknown", "issued_waiting", "issued_resuming"):
                    run_obj.error = "Agent outcome unknown after interruption"
                    run_obj.finished_at = _utcnow()
                elif run_obj and run_obj.status in ("running", "success"):
                    run_obj.status = "error"
                    run_obj.error = err_text[:2000]
                    run_obj.finished_at = _utcnow()
                # Advance next_run even on failure so a broken task doesn't
                # busy-loop the scheduler every tick with a stale past date.
                task_obj = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                if task_obj and (not run_obj or run_obj.status not in ('outcome_unknown', 'issued_waiting', 'issued_resuming')) and (task_obj.trigger_type or "schedule") == "schedule":
                    task_obj.last_run = _utcnow()
                    try:
                        task_obj.next_run = compute_next_run(
                            task_obj.schedule, task_obj.scheduled_time,
                            task_obj.scheduled_day, task_obj.scheduled_date,
                            after=_utcnow(),
                            cron_expression=task_obj.cron_expression,
                            tz_name=_resolve_task_timezone(db, task_obj),
                        )
                    except Exception:
                        pass
                try:
                    db.commit()
                except Exception as commit_err:
                    # Commit failed — without a fallback the run row stays
                    # "running" forever AND next_run stays in the past, so the
                    # scheduler busy-loops dispatching the same task every tick
                    # until restart. Force the recovery in a fresh session.
                    logger.warning("Task %s error-path commit failed: %s — falling back", task_id, commit_err)
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    from datetime import timedelta as _td
                    _recover_db = SessionLocal()
                    try:
                        _r = _recover_db.query(TaskRun).filter(TaskRun.id == run_id).first()
                        if _r and _r.status in ("running", "queued"):
                            _r.status = "aborted"
                            _r.error = f"commit_failed: {type(commit_err).__name__}: {commit_err}"[:2000]
                            _r.finished_at = _utcnow()
                        _t = _recover_db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                        if _t and (_t.trigger_type or "schedule") == "schedule":
                            # Push next_run forward 5min as a safe stall so the
                            # scheduler doesn't immediately re-dispatch.
                            _t.next_run = _utcnow() + _td(minutes=5)
                            _t.last_run = _utcnow()
                        _recover_db.commit()
                    except Exception as recover_err:
                        logger.error("Task %s recovery commit ALSO failed: %s", task_id, recover_err)
                    finally:
                        _recover_db.close()
            except Exception:
                logger.exception("Task %s error-path failed unexpectedly", task_id)
        finally:
            db.close()
            handle = self._task_handles.get(task_id)
            if handle is asyncio.current_task():
                self._task_handles.pop(task_id, None)
            if release_executing:
                async with self._executing_lock:
                    self._executing.discard(task_id)



    # Built-in housekeeping actions whose output is pure infra (no user-facing
    # content) — don't pollute the assistant chat session with their summaries.
    # Activity log + reminder email already carry everything the user needs.
    _SILENT_ACTIONS = frozenset({
        "theme_emoji_refresh",
        "check_email_urgency",
        "learn_sender_signatures",
        "summarize_emails",
        "draft_email_replies",
        "email_auto_translate",
        "extract_email_events",
        "classify_events",
        "tidy_sessions",
        "consolidate_memory",
        "tidy_research",
        "test_skills",
        "audit_skills",
    })

    _MODEL_BACKED_ACTIONS = frozenset({
        "summarize_emails",
        "draft_email_replies",
        "email_auto_translate",
        "extract_email_events",
        "classify_events",
        "learn_sender_signatures",
        "check_email_urgency",
        "test_skills",
        "audit_skills",
        "consolidate_memory",
    })

    def _task_needs_model_slot(self, task_id: str) -> bool:
        """Only LLM/research/model-backed actions should wait in the model
        queue. Pure housekeeping actions can run immediately."""
        from core.database import SessionLocal, ScheduledTask

        db = SessionLocal()
        try:
            task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
            if not task:
                return True
            task_type = getattr(task, "task_type", "") or "llm"
            if task_type != "action":
                return True
            return (getattr(task, "action", "") or "") in self._MODEL_BACKED_ACTIONS
        finally:
            db.close()

    def _log_to_assistant(self, db, task, result_text: str):
        """Log a task result to the assistant's chat session."""
        # Don't double-log check-ins (they already save directly)
        if "check-in" in (task.name or "").lower():
            return
        # Built-in housekeeping noise stays out of the chat.
        if (getattr(task, "action", "") or "") in self._SILENT_ACTIONS:
            return
        from src.assistant_log import log_to_assistant
        log_to_assistant(
            task.owner,
            result_text[:1000],
            category=(task.name or "Task"),
        )

    async def _execute_action(self, task, run_id: str | None = None) -> tuple:
        """Execute a built-in action (no LLM needed)."""
        from src.builtin_actions import BUILTIN_ACTIONS

        action_fn = BUILTIN_ACTIONS.get(task.action)
        if not action_fn:
            return f"Unknown action: {task.action}", False

        from src.builtin_actions import TaskNoop, TaskDeferred, ThemeEmojiRetryPaused
        try:
            # Pass task prompt as script/command for ssh_command/run_script actions.
            def _progress(message: str):
                self._set_run_progress(run_id, message)

            kwargs = {
                "owner": task.owner,
                "task_name": task.name,
                "progress_cb": _progress,
                "root_operation_id": (
                    f"turn-task-run:{run_id}"
                    if run_id
                    else f"task-action:{uuid.uuid4().hex}"
                ),
            }
            if task.action == "classify_events":
                from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL

                managed_route = _resolve_managed_task_route(
                    task,
                    operation="chat.complete",
                )
                task.provider_model_route_id = managed_route.model_route_id
                task.endpoint_id = managed_route.public_endpoint_id
                task.endpoint_url = MANAGED_ENGINE_PUBLIC_URL
                task.model = managed_route.provider_model_id
                self._last_run_model = managed_route.provider_model_id
                kwargs["model_route_id"] = managed_route.model_route_id
                kwargs["grant_id"] = managed_route.grant_id
            if task.action == 'theme_emoji_refresh':
                async def _theme_run(owner):
                    from pathlib import Path
                    from src.openclank.theme_emoji_local import ThemeEmojiLocalState
                    from src.openclank.theme_emoji_discovery import SourceHTTPError
                    from src.openclank.theme_emoji_inventory import EmojiInventory
                    from src.openclank.theme_emoji_acquisition import (
                        THEME_EMOJI_ACQUISITION_READY, ThemeEmojiAcquisition,
                        InstallationLease, acquisition_http_client,
                    )
                    if not THEME_EMOJI_ACQUISITION_READY or not run_id:
                        raise TaskNoop('Theme artwork acquisition is unavailable')
                    expected = json.loads(task.prompt or '{}')
                    account = expected.get('account_id')
                    consent = {k: expected.get(k) for k in ('installation_id', 'consent', 'consent_revision')}
                    task_hash = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                    local = ThemeEmojiLocalState()
                    manager_state = getattr(self._session_manager, 'lifecycle_context', None)
                    barrier = getattr(manager_state, 'account_request_barrier', None)
                    if (task.id != 'theme-emoji-' + task_hash[:32] or barrier is None
                            or not self._auth_manager or self._auth_manager.account_id(owner) != account
                            or self._auth_manager.username_for_account_id(account) != owner
                            or local.obligation(account) != consent or consent.get('consent') != 'accepted'):
                        raise TaskNoop('Theme consent changed')
                    def run_authorized():
                        from core.database import SessionLocal, ScheduledTask
                        with SessionLocal() as db:
                            current = db.query(ScheduledTask).filter_by(id=task.id, owner=owner, action='theme_emoji_refresh', status='active').first()
                            return bool(current and json.loads(current.prompt or '{}') == expected)
                    service = ThemeEmojiAcquisition(
                        inventory=EmojiInventory(local.root / 'inventory.sqlite3'), local_state=local,
                        auth_manager=self._auth_manager, account_barrier=barrier, run_authorized=run_authorized)
                    async with InstallationLease(local) as lease:
                        await service.prepare_inventory(owner=owner, account_id=account, expected=consent, lease=lease)
                        scanned = await asyncio.to_thread(service.inventory.epoch_prune_page)
                        def terminal_epochs():
                            from core.database import SessionLocal, TaskRun, ScheduledTask
                            if not scanned:
                                return []
                            with SessionLocal() as db:
                                rows = db.query(TaskRun.id, ScheduledTask.prompt).join(
                                    ScheduledTask, TaskRun.task_id == ScheduledTask.id).filter(
                                    TaskRun.id.in_(scanned), ScheduledTask.owner == owner,
                                    ScheduledTask.action == 'theme_emoji_refresh', TaskRun.id != run_id,
                                    TaskRun.finished_at.isnot(None),
                                    TaskRun.status.in_(('success', 'error', 'skipped', 'aborted')),
                                ).all()
                                result = []
                                for old_id, old_prompt in rows:
                                    try:
                                        old = json.loads(old_prompt or '{}')
                                        if old.get('account_id') == account and old.get('installation_id') == consent['installation_id']:
                                            result.append(old_id)
                                    except (ValueError, TypeError, AttributeError):
                                        continue
                                return result
                        terminal = await asyncio.to_thread(terminal_epochs)
                        await service.prune_terminal_epochs(owner=owner, account_id=account,
                            expected=consent, lease=lease, epoch_ids=terminal, scanned=scanned)
                        deadline = time.monotonic() + 180
                        async with asyncio.timeout(180), acquisition_http_client() as client:
                            remaining_bytes = 64 * 1024 * 1024
                            progress = {'ready':0, 'failed':0, 'bytes':0, 'pending':True, 'reason':'time_budget'}

                            async def acquire_batch(limit, seconds):
                                nonlocal remaining_bytes
                                batch = await service.run_epoch(owner=owner, account_id=account, expected=consent,
                                    lease=lease, client=client, epoch_id=str(run_id), limit=limit,
                                    budget_bytes=remaining_bytes, deadline_seconds=seconds)
                                for field in ('ready', 'failed', 'bytes'):
                                    progress[field] += batch[field]
                                progress['pending'], progress['reason'] = batch['pending'], batch['reason']
                                remaining_bytes -= batch['bytes']
                                if batch['reason'] == 'rate_limited':
                                    if batch['retryAfter'] is None:
                                        raise ThemeEmojiRetryPaused('Emoji source retry delay exceeds the automatic horizon')
                                    raise TaskDeferred('Emoji source asked to retry later', batch['retryAfter'])

                            manifest_path = Path(__file__).resolve().parents[1] / 'static/vendor/google-emoji/kitchen-catalog.tsv'
                            def read_manifest():
                                with manifest_path.open('rb') as stream:
                                    return stream.read(16 * 1024 * 1024 + 1)
                            manifest = await asyncio.to_thread(read_manifest)
                            if await asyncio.to_thread(service.inventory.catalogue_generation) == 0:
                                await service.discover_kitchen_baseline(owner=owner, account_id=account,
                                    expected=consent, lease=lease, manifest=manifest, starter=True)
                            await acquire_batch(32, 15)
                            await service.discover_kitchen_baseline(owner=owner, account_id=account, expected=consent, lease=lease, manifest=manifest)
                            warnings = []
                            for name, discover in (('kitchen', service.discover_kitchen_latest), ('noto', service.discover_noto)):
                                try:
                                    await discover(owner=owner, account_id=account, expected=consent, lease=lease, client=client)
                                except PermissionError:
                                    raise
                                except SourceHTTPError as exc:
                                    if exc.status in (429, 502, 503, 504):
                                        raise
                                    warnings.append(name)
                                except (ValueError, OSError):
                                    warnings.append(name)
                            progress.update(pending=True, reason='time_budget')
                            while time.monotonic() < deadline and remaining_bytes > 0:
                                await acquire_batch(1000, max(1, int(deadline - time.monotonic())))
                                if not progress['pending'] or progress['reason'] != 'batch_limit':
                                    break
                    progress['refreshWarnings'] = warnings
                    return json.dumps(progress, separators=(',', ':')), True
                kwargs['theme_emoji_run'] = _theme_run
            if task.prompt:
                kwargs["prompt"] = task.prompt
            if task.action in ("run_script", "run_local", "ssh_command") and task.prompt:
                kwargs["script" if task.action in ("run_script", "run_local") else "command"] = task.prompt
            # cookbook_serve carries its JSON config in task.prompt — feed it
            # through as `command` so action_cookbook_serve can json.loads it.
            elif task.action == "cookbook_serve" and task.prompt:
                kwargs["command"] = task.prompt
            result, success = await action_fn(**kwargs)
            return result, success
        except TaskNoop:
            # Bubble up so _execute_task_locked can drop the run row silently.
            raise
        except Exception as e:
            logger.error(f"Action '{task.action}' failed: {e}")
            return str(e), False

    # ── Check-in source discovery ──
    # Pattern-based: if an MCP server has a tool matching a pattern, it becomes
    # a check-in source. Add new patterns here to support new integrations —
    # no code changes needed elsewhere.
    CHECKIN_MCP_PATTERNS = [
        {"detect": "list_emails",   "section": "Email",    "tool": "list_emails",
         "args": {"mailbox": "INBOX", "limit": 10, "unread_only": True},
         "label_from_identity": True,
         "formatter": "_format_email_output"},
        {"detect": "search_emails", "section": "Email",    "tool": "search_emails",
         "args": {"query": "is:unread", "limit": 10},
         "label_from_identity": True,
         "formatter": "_format_email_output"},
        {"detect": "get_feed",      "section": "RSS",      "tool": "get_feed",
         "args": {},
         "label_from_identity": False},
        {"detect": "list_feeds",    "section": "RSS",      "tool": "list_feeds",
         "args": {},
         "label_from_identity": False},
        {"detect": "list_messages", "section": "Messages", "tool": "list_messages",
         "args": {"limit": 10},
         "label_from_identity": True},
    ]

    @staticmethod
    def _format_email_output(raw: str) -> str:
        """Clean up raw MCP email list output into readable format."""
        import re as _re
        lines = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Skip header lines like "📬 [INBOX] 856 emails..."
            if line.startswith(("\U0001f4ec", "📬", "No emails", "---", "Page ")):
                continue
            # Skip "more pages available" etc
            if "page" in line.lower() and "/" in line:
                continue
            # Parse: [1778] Re: Subject From: Name | Date
            m = _re.match(r'\[?\d+\]?\s*(?:↩️\s*|📎\s*|🔵\s*|⭐\s*)?(.+?)(?:\s*From:\s*(.+?))?(?:\s*\|\s*(\S+))?$', line)
            if m:
                subject = m.group(1).strip().rstrip('|').strip()
                sender = (m.group(2) or "").strip().rstrip('|').strip()
                if sender:
                    lines.append(f"- {sender} — {subject}")
                else:
                    lines.append(f"- {subject}")
            elif line.startswith("[") or line.startswith("-"):
                # Generic cleanup
                cleaned = _re.sub(r'^\[?\d+\]?\s*(?:↩️\s*|📎\s*)?', '', line.lstrip('- '))
                if cleaned.strip():
                    lines.append(f"- {cleaned.strip()}")
        if not lines:
            return "No unread emails"
        return "\n".join(lines[:10])

    async def _execute_checkin(
        self,
        task,
        crew,
        db,
        session_id: str,
        endpoint_url: str,
        model: str,
        *,
        managed_route: _ManagedTaskRoute,
        root_operation_id: str,
    ) -> str:
        """Gather raw data from all integrations, hand it to the LLM to write the check-in."""
        from src.tool_implementations import do_manage_notes
        from src.tool_utils import get_mcp_manager

        tz_name = _resolve_task_timezone(db, task)
        try:
            if tz_name:
                from zoneinfo import ZoneInfo
                from datetime import timezone, timedelta
                now = _utcnow().replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
            else:
                from datetime import timedelta
                now = _utcnow()
            time_str = now.strftime("%A, %B %d %Y, %H:%M")
        except Exception:
            from datetime import timedelta
            now = _utcnow()
            time_str = now.strftime("%H:%M UTC")

        raw = {}

        # Calendar: today+tomorrow, this week, month ahead
        # Pull directly from DB so we can include event_type and importance.
        try:
            from core.database import SessionLocal as _SL, CalendarEvent as _CE
            _db = _SL()
            try:
                for label, start, end in _digest_windows(now):
                    # Strip timezone for naive DB comparison
                    _s = start.replace(tzinfo=None) if start.tzinfo else start
                    _e = end.replace(tzinfo=None) if end.tzinfo else end
                    evs = _checkin_calendar_events(_db, task.owner, _s, _e)
                    if not evs:
                        continue
                    # Group by importance for richer output
                    by_imp = {"critical": [], "high": [], "normal": [], "low": []}
                    for ev in evs:
                        imp = (ev.importance or "normal").lower()
                        by_imp.setdefault(imp, []).append(ev)
                    lines = []
                    for tier in ("critical", "high", "normal", "low"):
                        items = by_imp.get(tier, [])
                        if not items:
                            continue
                        marker = {"critical": "[!!]", "high": "[!]", "normal": "  ", "low": " ·"}[tier]
                        for ev in items:
                            t = ev.dtstart.strftime("%a %b %d %H:%M")
                            tag = f" ({ev.event_type})" if ev.event_type else ""
                            loc = f" @ {ev.location}" if ev.location else ""
                            lines.append(f"{marker} {t} — {ev.summary}{tag}{loc}")
                    if lines:
                        raw[f"calendar_{label}"] = "\n".join(lines)
            finally:
                _db.close()
        except Exception as e:
            raw["calendar"] = f"Error: {e}"

        # Notes/Tasks
        try:
            r = await do_manage_notes(json.dumps({"action": "list"}), owner=task.owner)
            raw["notes_tasks"] = r.get("results") or r.get("response") or "No notes"
        except Exception as e:
            raw["notes_tasks"] = f"Error: {e}"

        # Auto-discover API integrations (Miniflux RSS, etc.).
        try:
            import httpx
            from src.integrations import load_integrations
            for integ in load_integrations():
                if not integ.get("enabled"):
                    continue
                preset = integ.get("preset", "")
                base_url = integ.get("base_url", "").rstrip("/")
                api_key = integ.get("api_key", "")
                if not base_url:
                    continue

                # Build auth headers
                headers = {}
                if integ.get("auth_type") == "header" and api_key:
                    headers[integ.get("auth_header", "X-Auth-Token")] = api_key
                elif integ.get("auth_type") == "bearer" and api_key:
                    headers["Authorization"] = f"Bearer {api_key}"

                # Miniflux: fetch unread entries (cached 3 min across tasks)
                if preset == "miniflux":
                    async def _fetch_miniflux(_base=base_url, _headers=dict(headers)):
                        async with httpx.AsyncClient(timeout=10) as client:
                            resp = await client.get(
                                f"{_base}/v1/entries",
                                params={"status": "unread", "limit": 15, "order": "published_at", "direction": "desc"},
                                headers=_headers,
                            )
                            if resp.status_code != 200:
                                return None
                            entries = resp.json().get("entries", []) or []
                            if not entries:
                                return None
                            lines = []
                            for e in entries[:15]:
                                title = e.get("title", "?")
                                feed = (e.get("feed") or {}).get("title", "?")
                                url = e.get("url", "")
                                lines.append(f"- [{feed}] {title} — {url}")
                            return "\n".join(lines)
                    try:
                        val = await _cached(
                            _owner_shared_cache_key(
                                task.owner,
                                "miniflux_unread",
                                base_url,
                            ),
                            180,
                            _fetch_miniflux,
                            admit=lambda: not self._owner_lifecycle_fenced(task.owner),
                        )
                        if val:
                            raw["rss_miniflux_unread"] = val
                    except Exception as e:
                        logger.warning(f"Miniflux fetch failed: {e}")
        except Exception as e:
            logger.warning(f"Integrations discovery failed: {e}")

        # Auto-discover MCP sources
        mcp = get_mcp_manager()
        if mcp:
            discovered = set()
            for server_id, tools in mcp._tools.items():
                if mcp.is_builtin(server_id):
                    continue
                conn = mcp._connections.get(server_id, {})
                if conn.get("status") != "connected":
                    continue
                identity = conn.get("identity", "")
                tool_names = {t["name"] for t in tools}
                for pattern in self.CHECKIN_MCP_PATTERNS:
                    if pattern["detect"] not in tool_names:
                        continue
                    key = f"{pattern['section']}_{server_id}"
                    if key in discovered:
                        continue
                    discovered.add(key)
                    label = f"{pattern['section']} ({identity})" if identity else pattern["section"]
                    qualified = f"mcp__{server_id}__{pattern['tool']}"
                    args = dict(pattern.get("args", {}))
                    args["account"] = "default"
                    try:
                        # Cache 3 min: different scheduled tasks firing at the
                        # same minute share the same MCP snapshot.
                        async def _call_mcp(_q=qualified, _args=args):
                            return await mcp.call_tool(_q, _args)
                        cache_key = _owner_shared_cache_key(
                            task.owner,
                            "mcp_snapshot",
                            qualified,
                            json.dumps(args, sort_keys=True),
                        )
                        result = await _cached(
                            cache_key,
                            180,
                            _call_mcp,
                            admit=lambda: not self._owner_lifecycle_fenced(task.owner),
                        )
                        if result.get("exit_code", 0) != 0:
                            continue
                        content = result.get("stdout") or result.get("output") or ""
                        if content.strip():
                            raw[label] = content[:3000]
                    except Exception:
                        pass

        # Build the data dump and hand it to the LLM
        data_dump = f"Current time: {time_str}\n\n"
        for key, val in raw.items():
            data_dump += f"--- {key} ---\n{val}\n\n"

        context = (
            data_dump +
            f"---\n\n{task.prompt}\n\n"
            "Write the check-in. YOU decide what matters, what to skip, how to format. "
            "Only show future events. Calendar events are pre-tagged with importance: "
            "[!!] critical, [!] high, plain = normal, ' ·' = low. "
            "GROUP your output by importance — lead with critical/high, then normal, "
            "skip low entirely unless explicitly relevant. Mention event type (work/health/travel/etc) "
            "where it adds context (e.g. 'leave 1h early for travel'). "
            "Flag anything coming up that needs prep (birthdays, deadlines, holidays). "
            "Use tools to take action if needed. Keep it concise — no raw data dumps."
        )

        return await self._run_agent_loop(
            endpoint_url, model, task, session_id,
            system_prompt=_compose_assistant_prompt(crew.personality) if crew else None,
            disabled_tools=None, relevant_tools=None,
            override_user_message=context,
            managed_route=managed_route,
            root_operation_id=root_operation_id,
        )

    async def _execute_llm_task(
        self,
        task,
        db,
        *,
        root_operation_id: str | None = None,
    ) -> str:
        """Execute an LLM task with full tool access via the agent loop."""
        from core.database import Session as DbSession, ChatMessage, CrewMember
        from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL

        # If this task is wired to a CrewMember (personal assistant, custom
        # crew), prefer the crew member's persona/model/endpoint as overrides.
        crew = None
        if getattr(task, "crew_member_id", None):
            try:
                crew = db.query(CrewMember).filter(CrewMember.id == task.crew_member_id).first()
            except Exception:
                crew = None

        root_operation_id = root_operation_id or f"turn-task-run:{uuid.uuid4().hex}"
        session_id = task.session_id
        existing_session = None
        if session_id:
            existing_session = db.query(DbSession).filter(
                DbSession.id == session_id,
            ).first()
        managed_route = _resolve_managed_task_route(
            task,
            crew=crew,
            session=existing_session,
            operation="chat.stream",
        )
        endpoint_id = managed_route.public_endpoint_id
        endpoint_url = MANAGED_ENGINE_PUBLIC_URL
        model = managed_route.provider_model_id
        task.endpoint_id = endpoint_id
        task.endpoint_url = endpoint_url
        task.model = model
        task.provider_model_route_id = managed_route.model_route_id
        # Record the resolved model so _execute_task_locked can persist it on
        # the run (tasks rarely pin a model, so this is the only record of
        # which model actually produced the output).
        self._last_run_model = model

        # Ensure a session exists for output
        if not session_id:
            session_id = await self._mint_session_id()
            sess = DbSession(
                id=session_id,
                name=f"[Task] {task.name}",
                endpoint_url=endpoint_url,
                endpoint_id=endpoint_id,
                provider_model_route_id=managed_route.model_route_id,
                model=model,
                owner=task.owner,
                workspace_id=getattr(task, "workspace_id", None) or None,
                folder="Tasks",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            db.add(sess)
            task.session_id = session_id
            db.commit()
            # The ORM row already exists; cache it directly instead of calling
            # create_session/ensure_task_session and attempting a duplicate ID.
            self._cache_task_session(sess)
        else:
            if existing_session:
                existing_session.endpoint_id = endpoint_id
                existing_session.endpoint_url = endpoint_url
                existing_session.model = model
                existing_session.provider_model_route_id = managed_route.model_route_id
                existing_session.workspace_id = (
                    getattr(task, "workspace_id", None) or None
                )
                db.commit()
                self._sync_cached_task_workspace(
                    session_id,
                    getattr(task, "workspace_id", None),
                )

        # For assistant check-ins: call each tool directly and post results
        # as separate messages. More reliable than hoping the model calls tools.
        is_checkin = crew and crew.is_default_assistant and "check-in" in (task.name or "").lower()
        if is_checkin:
            return await self._execute_checkin(
                task,
                crew,
                db,
                session_id,
                endpoint_url,
                model,
                managed_route=managed_route,
                root_operation_id=root_operation_id,
            )

        # Build system prompt: crew member persona overrides the default.
        # Built-in character_id (Socrates, Razor, etc.) further biases the
        # voice — it prepends to whichever base prompt we landed on so the
        # task still knows it's executing a scheduled task but in that
        # character's tone.
        if crew and crew.is_default_assistant:
            system_prompt = _compose_assistant_prompt(crew.personality)
        elif crew and crew.personality:
            system_prompt = (crew.personality or "").strip()
        else:
            # No crew voice: the synced default persona speaks (ruling R15) —
            # the task framing stays mechanical.
            try:
                from src.default_persona import get_default_persona
                _voice = get_default_persona(task.owner or "")["system_prompt"]
            except Exception:
                _voice = ""
            _task_line = (
                "You are executing a scheduled task. Use available tools to "
                "complete the task thoroughly."
            )
            system_prompt = f"{_voice}\n\n{_task_line}" if _voice else (
                "You are a helpful assistant executing a scheduled task. "
                "Use available tools to complete the task thoroughly."
            )
        char_id = (getattr(task, "character_id", None) or "").strip()
        if char_id:
            try:
                from src.reminder_personas import PERSONAS as _PERSONAS
                char_prompt = _PERSONAS.get(char_id.lower())
                if char_prompt:
                    system_prompt = f"{char_prompt}\n\n{system_prompt}"
            except Exception:
                pass
        # Provide current date/time as a user-role message so the system prompt
        # stays byte-identical across runs and doesn't bust the Anthropic prompt
        # cache on every scheduled tick (see issue #2927 and the identical fix on
        # the interactive-chat path in src/agent_loop.py).  The message is built
        # once here and shared by both execution paths below (agent loop and the
        # direct fallback) so time grounding is never lost on either path.
        tz_name = _resolve_task_timezone(db, task)
        try:
            from src.user_time import current_datetime_context_message_for_tz
            _dt_msg: dict | None = current_datetime_context_message_for_tz(tz_name)
        except Exception:
            _dt_msg = None

        # Compute the disabled-tools set: the crew's enabled_tools allowlist
        # (inverted) plus the operator's global disabled_tools setting. The
        # global list must be merged here — chat does the same merge before
        # entering the agent loop (routes/chat_routes.py) — otherwise an admin
        # or AUTH_ENABLED=false scheduled task would still see and call shell/
        # file tools after the operator disabled them globally, because the
        # prompt/schema/execution gates only enforce what is passed in.
        disabled_tools: set[str] = set()
        if crew and crew.enabled_tools:
            try:
                enabled = json.loads(crew.enabled_tools)
                if isinstance(enabled, list) and enabled:
                    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
                    all_tools = set(BUILTIN_TOOL_DESCRIPTIONS.keys())
                    disabled_tools |= all_tools - set(enabled)
            except Exception:
                pass
        try:
            from src.settings import get_setting
            _global_disabled = get_setting("disabled_tools", [])
            if isinstance(_global_disabled, list):
                disabled_tools.update(_global_disabled)
        except Exception:
            pass

        # RAG-select relevant tools for this prompt + always-available assistant tools.
        # Without this, all 40+ tools get sent and models hit their tool limit.
        relevant_tools = None
        try:
            from src.tool_index import get_tool_index, ASSISTANT_ALWAYS_AVAILABLE
            tool_idx = get_tool_index()
            if tool_idx:
                rag_tools = tool_idx.get_tools_for_query(task.prompt or "", k=8)
                relevant_tools = compose_task_relevant_tools(
                    rag_tools, ASSISTANT_ALWAYS_AVAILABLE, disabled_tools
                )
                logger.info(f"[assistant] RAG selected {len(rag_tools)} tools + {len(ASSISTANT_ALWAYS_AVAILABLE)} always-available + shell/file defaults = {len(relevant_tools)} total for '{task.name}'")
        except Exception as e:
            logger.warning(f"[assistant] RAG tool selection failed, using all: {e}")

        result = await self._run_agent_loop(
            endpoint_url, model, task, session_id,
            system_prompt=system_prompt, disabled_tools=disabled_tools or None,
            relevant_tools=relevant_tools,
            datetime_context_msg=_dt_msg,
            managed_route=managed_route,
            root_operation_id=root_operation_id,
        )

        # Strip the model's chain-of-thought before saving/delivering. Task
        # output is LLM-only, so prose=True (which also removes untagged
        # "The user wants me to…" reasoning) is safe here — without this the
        # thinking leaked into the saved result.
        try:
            from src.text_helpers import strip_think
            result = strip_think(result or "", prose=True, prompt_echo=True).strip() or result
        except Exception:
            pass

        return result

    async def _deliver_task_result(self, task, result: str, db, model: str = None):
        """Deliver a completed task result according to output_target.

        This is intentionally shared by LLM/research/action tasks so built-in
        actions cannot drift into hidden delivery paths that disagree with the
        task's visible output target.
        """
        from core.database import Session as DbSession, ChatMessage, CrewMember
        from core.models import ChatMessage as MemChatMessage

        output = task.output_target or "session"
        if (
            output == "session"
            and (getattr(task, "task_type", "") or "") == "action"
            and (getattr(task, "action", "") or "") in self._SILENT_ACTIONS
        ):
            return
        if output.startswith("mcp__"):
            await self._deliver_via_mcp(output, task, result)
            return

        if self._is_email_output_target(output):
            await self._deliver_via_email(output, task, result)
            return

        if output != "session":
            return

        endpoint_id = getattr(task, "endpoint_id", None)
        route_id = getattr(task, "provider_model_route_id", None)
        endpoint_url = ""
        model_name = model or task.model
        crew = None
        if getattr(task, "crew_member_id", None):
            try:
                crew = db.query(CrewMember).filter(CrewMember.id == task.crew_member_id).first()
            except Exception:
                crew = None
        if crew:
            route_id = route_id or getattr(crew, "provider_model_route_id", None)
            model_name = model_name or crew.model
        if route_id:
            from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL
            endpoint_url = MANAGED_ENGINE_PUBLIC_URL
        else:
            # Action-only tasks can still write a result transcript without a
            # model.  They must not resurrect a legacy endpoint as authority.
            endpoint_id = None

        session_id = task.session_id
        if not session_id:
            session_id = await self._mint_session_id()
            sess = DbSession(
                id=session_id,
                name=f"[Task] {task.name}",
                endpoint_url=endpoint_url or "",
                endpoint_id=endpoint_id,
                provider_model_route_id=route_id,
                model=model_name or "",
                owner=task.owner,
                workspace_id=getattr(task, "workspace_id", None) or None,
                folder="Tasks",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            db.add(sess)
            task.session_id = session_id
            db.commit()
            self._cache_task_session(sess)
        else:
            # LLM/research tasks usually stamped this row during setup. Action
            # tasks reach delivery without that setup, so stamp directly here
            # as the single output-session authority seam.
            sess = db.query(DbSession).filter(DbSession.id == session_id).first()
            if sess is not None:
                sess.workspace_id = getattr(task, "workspace_id", None) or None
                db.commit()
                self._sync_cached_task_workspace(
                    session_id,
                    getattr(task, "workspace_id", None),
                )

        meta = {}
        if model_name:
            meta["model"] = model_name
        if crew and crew.is_default_assistant:
            meta.update({"source": "cron", "task_id": task.id, "task_name": task.name})

        # Use SessionManager for persistence so in-memory cache stays in sync
        if self._session_manager and session_id:
            try:
                self._session_manager.add_message(
                    session_id,
                    MemChatMessage(
                        "user",
                        task.prompt or f"[Task] {task.name}",
                        metadata=dict(meta),
                    ),
                )
                self._session_manager.add_message(
                    session_id,
                    MemChatMessage(
                        "assistant",
                        result or "",
                        metadata=dict(meta),
                    ),
                )
            except Exception:
                logger.exception("Failed to deliver task %s through SessionManager", task.id)
        else:
            # Fallback: raw DB write (no session manager available)
            msg_meta = json.dumps(meta)
            user_msg = ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="user",
                content=task.prompt or f"[Task] {task.name}",
                timestamp=_utcnow(),
                meta_data=msg_meta,
            )
            assistant_msg = ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="assistant",
                content=result or "",
                timestamp=_utcnow(),
                meta_data=msg_meta,
            )
            db.add(user_msg)
            db.add(assistant_msg)
            db.commit()

    @staticmethod
    def _is_email_output_target(output: str) -> bool:
        target = (output or "").strip()
        if target in {"email", "email:self"}:
            return True
        if target.startswith("email:"):
            return True
        return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", target))

    async def _deliver_via_email(self, output: str, task, result: str):
        """Send task output through the app's configured SMTP account.

        Supported output_target values:
        - email / email:self: send to the account's From address
        - email:name@example.com or raw name@example.com: send there
        """
        from email.message import EmailMessage

        target = (output or "").strip()
        explicit = ""
        account_id = ""
        if target.startswith("email:"):
            explicit = target.split(":", 1)[1].strip()
            if "|account=" in explicit:
                explicit, account_id = explicit.split("|account=", 1)
                explicit = explicit.strip()
                account_id = account_id.strip()
            if explicit == "self":
                explicit = ""
        elif "@" in target:
            explicit = target

        try:
            from routes.email_routes import _resolve_send_config
            from routes.email_helpers import _send_smtp_message

            cfg = _resolve_send_config(account_id=account_id or None, owner=task.owner or "")
            to_addr = explicit or cfg.get("from_address") or cfg.get("smtp_user") or ""
            if not to_addr:
                raise RuntimeError("No email recipient resolved for task output")

            from_addr = cfg.get("from_address") or cfg.get("smtp_user") or to_addr
            msg = EmailMessage()
            msg["From"] = from_addr
            msg["To"] = to_addr
            msg["Subject"] = f"[Task] {task.name}"
            msg["X-Odysseus-Origin"] = "odysseus-ui"
            msg["X-Odysseus-Kind"] = "task"
            msg["X-Odysseus-Ref"] = str(task.id)
            msg.set_content(result or "")
            _send_smtp_message(cfg, from_addr, [to_addr], msg.as_string(), timeout=30)
            logger.info("Task %s emailed result (recipient_set=%s, %sb)", task.id, bool(to_addr), len(result or ""))
        except Exception as e:
            logger.error("Task %s email delivery failed: %s", task.id, e, exc_info=True)
            raise

    async def _run_agent_loop(
        self,
        endpoint_url: str,
        model: str,
        task,
        session_id: str,
        system_prompt: str | None = None,
        disabled_tools: set | None = None,
        relevant_tools: set | None = None,
        override_user_message: str | None = None,
        datetime_context_msg: dict | None = None,
        managed_route: _ManagedTaskRoute | None = None,
        root_operation_id: str | None = None,
    ) -> str:
        """Run the full agent loop with tool access, collecting the final text."""
        from src.endpoint_resolver import ResolvedModelTarget
        from src.model_dispatch import stream_agent_target
        from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL

        # Freeze the persisted identity before the first await; never infer a
        # legacy account from a later username lookup.
        frozen_task_id = str(task.id or '')
        frozen_owner = str(task.owner or '')
        frozen_account = getattr(task, 'host_account_id', None)
        run_id = (root_operation_id or '').removeprefix('turn-task-run:') if (root_operation_id or '').startswith('turn-task-run:') else ''

        # Resolve once for setup-time tool projection. This is deliberately
        # repeated after the final async quiet gate below; only that second
        # result is allowed into the worker cwd/envelope.
        workspace_path = self._resolve_task_workspace(task)
        del endpoint_url, model  # compatibility-only provenance, never authority
        root_operation_id = root_operation_id or f"turn-task-run:{uuid.uuid4().hex}"
        managed_route = managed_route or _resolve_managed_task_route(
            task,
            operation="chat.stream",
        )
        task.provider_model_route_id = managed_route.model_route_id
        task.endpoint_id = managed_route.public_endpoint_id
        task.endpoint_url = MANAGED_ENGINE_PUBLIC_URL
        task.model = managed_route.provider_model_id

        system_content = system_prompt or "You are a helpful assistant executing a scheduled task. Use available tools to complete the task thoroughly."
        user_content = override_user_message or task.prompt
        # Build the message list. The datetime context message (user-role) is
        # inserted immediately before the task prompt so the system prefix stays
        # byte-identical and cacheable across runs (see issue #2927).
        messages: list = [{"role": "system", "content": system_content}]
        if datetime_context_msg:
            messages.append(datetime_context_msg)
        messages.append({
            "role": "user",
            "content": user_content,
            "metadata": {"root_operation_id": root_operation_id},
        })

        try:
            stored_allowed = json.loads(getattr(task, "allowed_tools", None) or "[]")
        except (TypeError, ValueError):
            stored_allowed = []
        if not isinstance(stored_allowed, list):
            raise RuntimeError("INVALID_TOOL_POLICY: task allowed_tools must be a JSON list")
        allowed_tools = {str(name) for name in stored_allowed if str(name)}
        # Durable task rows can outlive a policy change (or be edited by an
        # older deployment). Re-check the strict worker's host-tool boundary
        # immediately before spawn; route-time validation alone is not enough.
        from src.tool_security import unavailable_strict_agent_tools
        unavailable = allowed_tools & unavailable_strict_agent_tools(
            task.owner, workspace_path,
            workspace_id=str(getattr(task, "workspace_id", None) or ""), chat_id=session_id,
        )
        if unavailable:
            raise RuntimeError(
                "INVALID_TOOL_POLICY: requested filesystem/process tools are "
                "outside the task's current brokered AgentScope: "
                + ", ".join(sorted(unavailable))
            )
        if relevant_tools is not None:
            allowed_tools.intersection_update(map(str, relevant_tools))
        if disabled_tools:
            allowed_tools.difference_update(map(str, disabled_tools))
        full_text = ""
        tool_results = []
        terminal = False
        stream_error = None
        had_wait = False

        # Honor per-task max_steps (defense against runaway agent loops).
        # Falls back to 20 if not set — the historical default.
        _task_max_rounds = task.max_steps if task.max_steps and task.max_steps > 0 else 20
        from src.interactive_gate import wait_for_interactive_quiet
        await wait_for_interactive_quiet(f"agent task {task.name}")
        # Structural Agent door: a policy reset/revoke/archive that lands while
        # the task waits for foreground activity must win before worker spawn.
        # There are no awaits between this resolution and stream invocation.
        workspace_path = self._resolve_task_workspace(task)
        unavailable = allowed_tools & unavailable_strict_agent_tools(
            task.owner, workspace_path,
            workspace_id=str(getattr(task, "workspace_id", None) or ""), chat_id=session_id,
        )
        if unavailable:
            raise RuntimeError(
                "INVALID_TOOL_POLICY: requested filesystem/process tools are "
                "outside the task's current brokered AgentScope: "
                + ", ".join(sorted(unavailable))
            )
        capabilities = dict(managed_route.capabilities or {})
        capabilities.setdefault("chat", True)
        capabilities.setdefault("tools", True)
        capabilities.setdefault("stream", True)
        target = ResolvedModelTarget(
            transport="acp",
            endpoint_url=MANAGED_ENGINE_PUBLIC_URL,
            model_id=managed_route.runtime_model,
            endpoint_id=managed_route.connection_id,
            provider_id=managed_route.connection_id,
            headers={},
            capabilities=capabilities,
            lifecycle="ephemeral",
        )
        turn_envelope = {
            "durable_id": f"task:{task.id}",
            "root_operation_id": root_operation_id,
            # Agent turns always pause on interaction (durable TaskWaitRequest).
            # Tool/aux paths keep fail_on_interaction. The stored
            # ScheduledTask.interaction_policy column is historical/API-facing
            # only and is deliberately not consulted here.
            "interaction_policy": "pause_on_interaction",
            "allowed_tools": sorted(allowed_tools),
            # ``workspace`` is a server-derived physical cwd retained for the
            # current worker contract. ``authority_workspace_id`` is the
            # collision-free stable file-policy identity used by approval
            # scopes; it is not the Copal or Memery workspace axis.
            "workspace": workspace_path or "",
            "authority_workspace_id": str(
                getattr(task, "workspace_id", None) or ""
            ).strip(),
            "copal_workspace": getattr(task, "copal_workspace", None) or "default",
        }
        if managed_route.grant_id:
            turn_envelope["provider_grant_id"] = managed_route.grant_id
        async def admitted_stream():
            from core.models import get_session_manager_instance
            from core.database import SessionLocal, ScheduledTask, TaskRun
            manager = get_session_manager_instance()
            state = getattr(manager, 'lifecycle_context', None) if manager else None
            auth = getattr(state, 'auth_manager', None)
            barrier = getattr(state, 'account_request_barrier', None)
            runtime = getattr(self._mimo_supervisor, '_s05_foreground_runtime', None)
            admission = None
            context = None
            if runtime is not None or frozen_account:
                if (not frozen_account or not run_id or auth is None or barrier is None
                        or auth.account_id(frozen_owner) != frozen_account
                        or auth.username_for_account_id(frozen_account) != frozen_owner):
                    raise RuntimeError('TASK_ACCOUNT_PROVENANCE_UNAVAILABLE: task requires a newly authenticated account binding')
                admission = await barrier.acquire(frozen_owner)
                try:
                    if (auth.account_id(frozen_owner) != frozen_account
                            or auth.username_for_account_id(frozen_account) != frozen_owner
                            or not await barrier.is_live(frozen_owner, admission)):
                        raise RuntimeError('TASK_ACCOUNT_PROVENANCE_UNAVAILABLE: account changed')
                    # The quiet gate and account admission both await. Recheck
                    # the workspace actually passed to this invocation after them.
                    if self._resolve_task_workspace(task) != workspace_path:
                        raise RuntimeError('TASK_WORKSPACE_UNAVAILABLE: workspace changed')
                    with SessionLocal() as claim_db:
                        if claim_db.get_bind().dialect.name == 'sqlite':
                            claim_db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                        current = claim_db.query(ScheduledTask).filter_by(id=frozen_task_id).first()
                        occurrence = claim_db.query(TaskRun).filter_by(id=run_id, task_id=frozen_task_id).first()
                        if (not current or current.owner != frozen_owner or current.host_account_id != frozen_account
                                or not occurrence or occurrence.status != 'running'):
                            raise RuntimeError('TASK_ACCOUNT_PROVENANCE_UNAVAILABLE: task occurrence changed')
                        from src.openclank.task_execution_context import (
                            TASKSPEC_FIELDS, profile_available, taskspec_sha256, write_context,
                        )
                        spec = {key: getattr(task, key) for key in TASKSPEC_FIELDS}
                        spec = {key: int(value) if type(value) is bool else value for key, value in spec.items()}
                        expected_spec = taskspec_sha256(spec)
                        current_spec = {key: getattr(current, key) for key in TASKSPEC_FIELDS}
                        current_spec = {key: int(value) if type(value) is bool else value for key, value in current_spec.items()}
                        if taskspec_sha256(current_spec) != expected_spec:
                            raise RuntimeError('TASK_ACCOUNT_PROVENANCE_UNAVAILABLE: task configuration changed')
                        workspace_id = str(getattr(task, 'workspace_id', None) or '')
                        if (claim_db.get_bind().dialect.name == 'sqlite'
                                and getattr(task, '_execution_context_original_run', False) and workspace_id):
                            raw_db = claim_db.connection().connection.driver_connection
                            if profile_available(raw_db):
                                write_context(raw_db, account_id=frozen_account, owner=frozen_owner,
                                    task_id=frozen_task_id, run_id=run_id, workspace_id=workspace_id,
                                    expected_taskspec_sha256=expected_spec)
                        # The send can become uncertain after this commit.  Advance
                        # the due occurrence now, not on a later timeout/restart.
                        if runtime is not None:
                            occurrence.status = 'outcome_unknown'
                            occurrence.error = 'Agent outcome not yet settled'
                            if (current.trigger_type or 'schedule') == 'schedule':
                                current.next_run = compute_next_run(current.schedule, current.scheduled_time,
                                    current.scheduled_day, current.scheduled_date, after=_utcnow(),
                                    cron_expression=current.cron_expression, tz_name=_resolve_task_timezone(claim_db, current))
                            else:
                                current.next_run = None
                        claim_db.commit()
                    context = (frozen_account, auth, barrier, admission)
                except BaseException:
                    await barrier.release(admission)
                    raise
            try:
                async for value in stream_agent_target(
                    target, messages=messages, supervisor=self._mimo_supervisor,
                    max_rounds=_task_max_rounds,
                    max_tool_calls=int(getattr(task, 'max_tool_calls', None) or 20),
                    session_id=session_id, owner=frozen_owner, cwd=workspace_path,
                    disabled_tools=disabled_tools, relevant_tools=allowed_tools,
                    turn_envelope=turn_envelope, workload='background',
                    _foreground_account_context=context,
                ):
                    yield value
            finally:
                if admission is not None:
                    await barrier.release(admission)

        async for event_str in admitted_stream():
            event_name = "message"
            for line in event_str.splitlines():
                if line.startswith("event:"):
                    event_name = line[6:].strip() or "message"
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    terminal = True
                    continue
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event_name == "error" or data.get("error"):
                    stream_error = data
                    continue
                if data.get("type") in {"permission_request", "ask_user", "question", "interaction_required"}:
                    # S12: persist the wait and surface it to the task chat.
                    # The bridge's QuestionHandler blocks the worker until the
                    # answer arrives, so this async pause IS the resume point.
                    # No invented answers; completed tool side effects stay
                    # committed in the transcript.
                    _kind = "permission" if data.get("type") == "permission_request" else "question"
                    _payload = data.get("data") or data.get("request") or data
                    _native_binding = None
                    if frozen_account and isinstance(_payload, dict):
                        _live = self._task_live_pending(owner=frozen_owner, session_id=session_id,
                            account_id=frozen_account, kind=_kind, request_id=_payload.get('request_id'),
                            root_operation_id=root_operation_id)
                        if (_live and _payload.get('session_id') == session_id
                                and _payload.get('turn_id') == _live[0]['turn_id']
                                and _payload.get('revision') == _live[0]['revision']):
                            _native_binding = _live[0]
                    self._persist_wait_request(
                        task_id=task.id,
                        run_id=run_id,
                        session_id=session_id,
                        owner=task.owner or "",
                        kind=_kind,
                        payload=_payload if isinstance(_payload, dict) else {"raw": str(_payload)},
                        workspace_id=getattr(task, "workspace_id", None),
                        continuation_revision=int(data.get("revision") or 0),
                        actor_generation=data.get("turn_id") or None,
                        native_binding=_native_binding,
                    )
                    self.add_notification(
                        task_name=task.name,
                        status="waiting",
                        task_id=task.id,
                        owner=task.owner,
                        body=(
                            _payload.get("question")
                            if isinstance(_payload, dict) and _payload.get("question")
                            else "Task needs your answer"
                        ),
                        kind="task_question",
                        session_id=session_id,
                    )
                    had_wait = True
                    continue
                if had_wait:
                    # Stream continued after a wait: the question was answered.
                    # Transition run back to running.
                    had_wait = False
                    try:
                        from core.database import SessionLocal, TaskRun as _TR
                        _db = SessionLocal()
                        try:
                            _run = _db.query(_TR).filter(_TR.id == run_id).first()
                            if _run and _run.status in ("waiting", "resuming", "issued_waiting", "issued_resuming"):
                                _run.status = "outcome_unknown" if _run.status.startswith("issued_") else "running"
                                _db.commit()
                        finally:
                            _db.close()
                    except Exception:
                        pass
                if isinstance(data.get("delta"), str) and not data.get("thinking"):
                    full_text += data["delta"]
                if data.get("type") == "tool_output":
                    tool_summary = data.get("stdout") or data.get("output") or data.get("result") or ""
                    if isinstance(tool_summary, str) and tool_summary.strip():
                        tool_results.append(f"[{data.get('tool', '?')}] {tool_summary[:500]}")

        if stream_error:
            code = stream_error.get("code") or "AGENT_STREAM_FAILED"
            detail = stream_error.get("error") or stream_error.get("detail") or "Agent stream failed"
            raise RuntimeError(f"{code}: {detail}")
        if not terminal:
            if had_wait:
                # Stream ended while a wait was pending (worker crash / restart).
                # Leave the run in "waiting" so the persisted request can be
                # answered and the run resumed. Do not overwrite with an error.
                raise TaskWaiting(run_id=run_id)
            raise RuntimeError("IN_FLIGHT_INTERRUPTED: Agent stream ended without a terminal event")

        # A no-tools auxiliary may summarize real completed tool results. It
        # never rescues admission/stream failure and cannot invent success when
        # no tool work happened.
        if not full_text.strip():
            if not tool_results:
                raise RuntimeError("AGENT_NO_OUTPUT: completed without answer text or tool results")
            from src.openclank.modality_facade import complete_text
            full_text = await complete_text(
                owner=task.owner or "local-installation",
                messages=[
                    {"role": "system", "content": "Summarize only the supplied completed tool results. Do not claim any unlisted action."},
                    {"role": "user", "content": "\n".join(tool_results[-5:])},
                ],
                purpose="tasks",
                model_route_id=managed_route.model_route_id,
                grant_id=managed_route.grant_id,
                root_operation_id=root_operation_id,
                idempotency_key=f"task-summary:{root_operation_id}",
                max_output_tokens=512,
            )
            full_text = (full_text or "").strip()
            if not full_text:
                raise RuntimeError("AUXILIARY_NO_OUTPUT: tool-result summary was empty")

        return full_text

    async def _execute_research_task(
        self,
        task,
        db,
        *,
        root_operation_id: str | None = None,
    ) -> str:
        """Execute a deep research task using DeepResearcher."""
        from core.database import Session as DbSession, ChatMessage
        from src.deep_research import DeepResearcher
        from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL
        from src.research_handler import RESEARCH_DATA_DIR, ResearchHandler
        from src.research_utils import strip_thinking
        from src.settings import get_setting

        root_operation_id = root_operation_id or f"turn-task-run:{uuid.uuid4().hex}"
        session_id = task.session_id
        existing_session = None
        if session_id:
            existing_session = db.query(DbSession).filter(
                DbSession.id == session_id,
            ).first()
        managed_route = _resolve_managed_task_route(
            task,
            session=existing_session,
            operation="chat.complete",
        )
        endpoint_id = managed_route.public_endpoint_id
        endpoint_url = MANAGED_ENGINE_PUBLIC_URL
        model = managed_route.provider_model_id
        task.endpoint_id = endpoint_id
        task.endpoint_url = endpoint_url
        task.model = model
        task.provider_model_route_id = managed_route.model_route_id
        # Record the resolved model for the run record (see _execute_task_locked).
        self._last_run_model = model

        max_tokens = int(get_setting("research_max_tokens", 8192))
        extraction_timeout = int(get_setting("research_extraction_timeout_seconds", 90) or 90)
        extraction_concurrency = int(get_setting("research_extraction_concurrency", 3) or 3)

        researcher = DeepResearcher(
            llm_model=model,
            max_rounds=8,
            max_time=600,  # 10 min for scheduled research
            max_report_tokens=max_tokens,
            extraction_timeout=extraction_timeout,
            extraction_concurrency=extraction_concurrency,
            owner=task.owner or None,
            session_id=task.session_id or f"task-research-{task.id}",
            model_route_id=managed_route.model_route_id,
            grant_id=managed_route.grant_id,
            root_operation_id=root_operation_id,
            purpose="tasks",
        )

        started_ts = time.time()
        report = await researcher.research(task.prompt)
        completed_ts = time.time()
        try:
            stats = researcher.get_stats() or {}
        except Exception:
            stats = {}

        # Ensure a session exists for output
        if not session_id:
            session_id = await self._mint_session_id()
            sess = DbSession(
                id=session_id,
                name=f"[Research] {task.name}",
                endpoint_url=endpoint_url,
                endpoint_id=endpoint_id,
                provider_model_route_id=managed_route.model_route_id,
                model=model,
                owner=task.owner,
                workspace_id=getattr(task, "workspace_id", None) or None,
                folder="Tasks",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            db.add(sess)
            task.session_id = session_id
            db.commit()
            self._cache_task_session(sess)
        elif existing_session:
            existing_session.endpoint_url = endpoint_url
            existing_session.endpoint_id = endpoint_id
            existing_session.provider_model_route_id = managed_route.model_route_id
            existing_session.model = model
            existing_session.workspace_id = (
                getattr(task, "workspace_id", None) or None
            )
            db.commit()
            self._sync_cached_task_workspace(
                session_id,
                getattr(task, "workspace_id", None),
            )

        # Persist scheduled research in the same on-disk shape used by the
        # Research panel. Without this, task research had Markdown output but
        # no Library entry and no visual report route to open.
        try:
            RESEARCH_DATA_DIR.mkdir(parents=True, exist_ok=True)
            findings = getattr(researcher, "findings", []) or []
            payload = {
                "query": task.prompt or task.name or "Scheduled research",
                "status": "done",
                "result": report,
                "raw_report": strip_thinking(report or ""),
                "sources": ResearchHandler._extract_sources(findings),
                "raw_findings": ResearchHandler._extract_raw_findings(findings),
                "stats": stats,
                "category": "scheduled",
                "started_at": started_ts,
                "completed_at": completed_ts,
                "owner": task.owner or "",
                "task_id": task.id,
                "task_name": task.name,
            }
            (RESEARCH_DATA_DIR / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")
            try:
                from src.event_bus import fire_event
                fire_event("research_completed", task.owner or None)
            except Exception:
                logger.debug("research_completed event dispatch failed", exc_info=True)
        except Exception as e:
            logger.warning("Failed to persist task research report %s: %s", session_id, e)

        return report

    async def _run_chained(self, task_id: str):
        """Run a chained task. Acquires _executing membership the same way
        run_task_now does so an overlapping scheduler tick can't double-dispatch
        the same task while the chain run is in flight."""
        async with self._executing_lock:
            if task_id in self._executing:
                return  # already in flight (manual trigger, scheduler tick, or another chain)
            self._executing.add(task_id)
        await self._execute_task(task_id)

    def _has_chain_cycle(self, db, start_id: str, max_depth: int = 10, owner: str | None = None) -> bool:
        """Detect cycles in task chains."""
        from core.database import ScheduledTask
        visited = set()
        current = start_id
        for _ in range(max_depth):
            if current in visited:
                return True
            visited.add(current)
            task = db.query(ScheduledTask).filter(ScheduledTask.id == current).first()
            if owner is not None and task and task.owner != owner:
                return True
            if not task or not task.then_task_id:
                return False
            current = task.then_task_id
        return True  # too deep, treat as cycle

    def _resolve_defaults(self, db, owner):
        """Find one recent session with persisted endpoint identity."""
        from core.database import Session as DbSession
        try:
            recent = db.query(DbSession).filter(
                DbSession.endpoint_id.isnot(None),
                DbSession.endpoint_url.isnot(None),
                DbSession.model.isnot(None),
                *([DbSession.owner == owner] if owner else []),
            ).order_by(DbSession.created_at.desc()).first()
            if recent:
                return recent.endpoint_id, recent.endpoint_url, recent.model
        except Exception:
            pass
        return None, None, None

    async def _deliver_via_mcp(self, tool_name: str, task, result: str):
        """Send the task result via an MCP tool (e.g. Gmail send).

        Resolves a recipient (so email-style tools have a 'to') by trying the
        configured From address first (the `daily_brief` pattern — email
        yourself) then falling back to the task owner. Common recipient field
        names (to / recipient / email / address) are all populated so we don't
        have to special-case each tool's schema; the MCP tool ignores keys it
        doesn't recognise.
        """
        from src.tool_utils import get_mcp_manager
        mcp = get_mcp_manager()
        if not mcp:
            logger.warning(f"Task {task.id}: MCP manager not available for delivery")
            return

        # Resolve recipient — prefer the configured email From (the established
        # "email yourself" pattern from daily_brief), fall back to task.owner.
        # `_get_email_config()` is the single source of truth that handles both
        # the legacy `email_from` setting and the per-account DB rows.
        recipient = None
        try:
            from routes.email_helpers import _get_email_config
            cfg = _get_email_config(owner=str(task.owner or "").strip()) or {}
            recipient = cfg.get("from_address") or None
        except Exception as _e:
            logger.debug(f"_deliver_via_mcp: email config lookup failed: {_e}")
        if not recipient and task.owner and "@" in str(task.owner):
            recipient = task.owner

        args = {
            "subject": f"[Task] {task.name}",
            "body": result,
            "headers": {
                "X-Odysseus-Origin": "odysseus-ui",
                "X-Odysseus-Kind": "task",
                "X-Odysseus-Ref": str(task.id),
            },
        }
        if recipient:
            # Cover the common field names so we work across MCP servers (Gmail,
            # generic SMTP, Slack DMs, etc.) without having to hard-code each.
            args["to"] = recipient
            args["recipient"] = recipient
            args["email"] = recipient
            args["address"] = recipient
        else:
            logger.warning(
                f"Task {task.id}: no recipient resolved for MCP delivery via {tool_name} — "
                "set an email From address in Settings or give the task an owner email."
            )
        try:
            mcp_result = await mcp.call_tool(tool_name, args)
            stderr = mcp_result.get("stderr", "")
            stdout = mcp_result.get("stdout", "")
            body_len = len(result or "")
            exit_code = mcp_result.get("exit_code", 0)
            if exit_code != 0:
                logger.warning(
                    f"Task {task.id} MCP delivery FAILED via {tool_name}: "
                    f"exit={exit_code} stderr={stderr[:400]!r} stdout={stdout[:400]!r}"
                )
            else:
                # Include the MCP tool's own stdout (e.g. email_server returns
                # "Sent email to ... with subject ...") + the body size so a
                # silent SMTP failure is easier to spot in the logs.
                logger.info(
                    f"Task {task.id} delivered via MCP tool {tool_name} "
                    f"(recipient_set={bool(recipient)}, body={body_len}b, reply={stdout[:200]!r})"
                )
        except Exception as e:
            logger.error(f"Task {task.id} MCP delivery failed: {e}")

    async def resume_task_from_wait(
        self,
        *,
        task_id: str,
        run_id: str,
        wait_id: str,
        owner: str,
        session_id: str,
        answer_payload: dict | None = None,
    ) -> bool:
        """Resume a waiting task run after its question is answered.

        Consumes the wait request exactly once (CAS). If the in-memory
        QuestionHandler still holds the request (in-process pause), resolve it
        so the blocked worker continues. Otherwise (restart), re-execute the
        task with the same run_id and session so the durable chat continues.
        """
        # A qualified issued turn may continue only via its exact live question
        # handler.  A persisted wait is not proof that a lost worker can resume.
        from core.database import SessionLocal, TaskRun, TaskWaitRequest
        with SessionLocal() as check_db:
            wait = check_db.query(TaskWaitRequest).filter_by(id=wait_id, task_id=task_id, run_id=run_id, owner=owner, session_id=session_id, state='waiting').first()
            run = check_db.query(TaskRun).filter_by(id=run_id, task_id=task_id).first()
            if not wait or not run:
                return False
            issued = run.status in ('issued_waiting', 'issued_resuming', 'outcome_unknown')
        if issued:
            accepted = False
            try:
                from core.database import ScheduledTask
                with SessionLocal() as verify_db:
                    row = verify_db.query(TaskWaitRequest).filter_by(id=wait_id, task_id=task_id, run_id=run_id, owner=owner, session_id=session_id, state='waiting').first()
                    task = verify_db.query(ScheduledTask).filter_by(id=task_id, owner=owner).first()
                    if not row or not task or not task.host_account_id:
                        return False
                    sealed_payload = row.payload
                    payload = json.loads(sealed_payload or '{}')
                    binding = payload.get('_openclank_native_wait') if isinstance(payload, dict) else None
                    if not isinstance(binding, dict) or binding.get('account_id') != task.host_account_id or binding.get('kind') != row.kind:
                        return False
                    live = self._task_live_pending(owner=owner, session_id=session_id, account_id=task.host_account_id,
                        kind=row.kind, request_id=binding.get('request_id'), root_operation_id=f'turn-task-run:{run_id}')
                    if not live or live[0] != binding:
                        return False
                    handler, pending = live[1], live[2]
                    if row.kind == 'question':
                        accepted = handler.resolve_result(binding['request_id'], owner=owner, session_id=session_id,
                            answers=(answer_payload or {}).get('answers'), rejected=bool((answer_payload or {}).get('rejected'))) == 'accepted'
                    elif row.kind == 'permission':
                        option = (answer_payload or {}).get('option_id')
                        if option not in ('once','chat','workspace','always','reject') or pending._future.done():
                            return False
                        accepted = handler.resolve_for(binding['request_id'], option, owner=owner, session_id=session_id)
                    else:
                        return False
                if not accepted:
                    return False
                if not self._consume_wait_request(wait_id, owner=owner, session_id=session_id,
                        expected_payload=sealed_payload, task_id=task_id, run_id=run_id):
                    return 'resolved_unrecorded'
                if not self._mark_run_resuming(run_id):
                    return 'resolved_unrecorded'
                return True
            except Exception:
                logger.debug('Live issued task continuation unavailable', exc_info=True)
                return 'resolved_unrecorded' if accepted else False
        if not self._consume_wait_request(wait_id, owner=owner, session_id=session_id):
            return False
        self._mark_run_resuming(run_id)

        # Try in-process resolve first: if the QuestionHandler still has this
        # request, the blocked worker will continue on its own.
        try:
            sup = getattr(self, "_mimo_supervisor", None)
            handler = None
            if sup and hasattr(sup, "question_handler_for"):
                handler = sup.question_handler_for(owner, request_id=wait_id)
            if handler and hasattr(handler, "resolve"):
                if handler.resolve(
                    wait_id,
                    owner=owner,
                    session_id=session_id,
                    answers=(answer_payload or {}).get("answers"),
                    rejected=bool((answer_payload or {}).get("rejected")),
                ):
                    return True
        except Exception:
            logger.debug("In-process wait resolve failed for %s", wait_id, exc_info=True)

        # Restart path: the worker is gone. Re-execute the task with the same
        # run_id. The agent loop starts a fresh turn on the same durable session.
        try:
            from core.database import SessionLocal, TaskRun
            db = SessionLocal()
            try:
                run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
                if not run or run.status != "resuming":
                    return False
                run.status = "running"
                run.result = "Resuming after answer…"
                db.commit()
            finally:
                db.close()
            # Resume the SAME run_id so the durable task chat continues.
            async def _resume_locked():
                task_run_locks = getattr(self, "_task_run_locks", None)
                if task_run_locks is None:
                    task_run_locks = self._task_run_locks = {}
                lock = task_run_locks.setdefault(task_id, asyncio.Lock())
                async with lock:
                    await self._execute_task_locked(
                        task_id, run_id,
                        release_executing=False, gate_foreground=False,
                    )
            asyncio.create_task(_resume_locked())
            return True
        except Exception:
            logger.exception("Failed to resume task %s from wait %s", task_id, wait_id)
            return False

    async def _execute_owned_manual_task(self, task_id, expected, *, force=False, selected_run_id=None):
        barrier = getattr(getattr(getattr(self, '_session_manager', None), 'lifecycle_context', None), 'account_request_barrier', None)
        admission = None
        handed_off = False
        try:
            if barrier is None:
                return
            admission = await barrier.acquire(expected['owner'])
            if not await barrier.is_live(expected['owner'], admission) or not self._manual_task_current(task_id, expected):
                return
            handed_off = True
            await self._execute_task(task_id, bypass_model_slot=force, release_executing=not force, manual_expected=expected, selected_run_id=selected_run_id)
        except Exception:
            logger.warning('Manual task admission or execution became unavailable')
        finally:
            if admission is not None:
                await barrier.release(admission)
            if selected_run_id is not None:
                handle = asyncio.current_task()
                if handle: self._unregister_task_handle(task_id, handle)
            if not handed_off and not force:
                self._skip_selected_manual_run(task_id, selected_run_id)
                async with self._executing_lock:
                    self._executing.discard(task_id)

    async def run_task_now(self, task_id: str, *, force: bool = False, manual_expected=None):
        """Manually trigger a task execution."""
        if self._task_owner_lifecycle_fenced(task_id) or not self._manual_task_current(task_id, manual_expected):
            return False
        if force:
            asyncio.create_task(self._execute_owned_manual_task(task_id, manual_expected, force=True) if manual_expected else
                                self._execute_task(task_id, bypass_model_slot=True, release_executing=False))
            return True
        async with self._executing_lock:
            if task_id in self._executing:
                return False
            self._executing.add(task_id)
        asyncio.create_task(self._execute_owned_manual_task(task_id, manual_expected) if manual_expected else self._execute_task(task_id))
        return True

    async def stop_task(self, task_id: str, *, manual_expected=None) -> bool:
        """Cancel and join a task before reporting its durable aborted state."""
        if not self._manual_task_current(task_id, manual_expected):
            return False
        handles = self._execution_handles_for_task(task_id)
        stopped = False
        current = asyncio.current_task()
        wait_for = []
        for handle in handles:
            handle.cancel()
            stopped = True
            if handle is not current:
                wait_for.append(handle)
        if wait_for:
            await asyncio.gather(*wait_for, return_exceptions=True)
        async with self._executing_lock:
            if task_id in self._executing:
                self._executing.discard(task_id)
                stopped = True

        if not self._manual_task_current(task_id, manual_expected):
            return False
        stopped = self._mark_run_aborted(task_id) or stopped
        return stopped

    async def reset_file_authority(
        self,
        owner: str,
        *,
        scope: str,
        chat_id: str | None = None,
        workspace_id: str | None = None,
        location_workspace_ids: tuple[str, ...] | list[str] = (),
    ) -> dict[str, int]:
        """Pause and cancel tasks narrowed by a canonical file-policy reset.

        The route supplies only authenticated owner identity and opaque IDs it
        derived from canonical policy state. No raw client path participates.
        Durable rows are paused and active TaskRuns are aborted *before*
        in-process handles are cancelled; a final DB fence wins against stale
        ORM state in a cancellation handler. Resuming is therefore always an
        explicit later action whose run re-resolves current Workspace policy.
        """
        from core.database import (
            Session as DbSession,
            SessionLocal,
            ScheduledTask,
            TaskRun,
        )

        normalized_owner = str(owner or "").strip().lower()
        normalized_scope = str(scope or "").strip()
        normalized_chat = str(chat_id or "").strip()
        normalized_workspace = str(workspace_id or "").strip()
        normalized_location_workspaces = frozenset(
            str(identifier or "").strip()
            for identifier in location_workspace_ids
            if str(identifier or "").strip()
        )
        if not normalized_owner:
            raise ValueError("Task reset requires an authenticated owner")
        if normalized_scope not in {"chat", "workspace", "location", "all_agent"}:
            raise ValueError("Invalid task file-authority reset scope")
        if normalized_scope == "chat" and not normalized_chat:
            raise ValueError("Chat task reset requires a chat ID")
        if normalized_scope == "workspace" and not normalized_workspace:
            raise ValueError("Workspace task reset requires a Workspace ID")

        from src.openclank.file_policy import FilePolicyRepository
        policy = FilePolicyRepository()
        subject = policy.subject_for_username(normalized_owner)
        workspace_aliases = {}
        if subject:
            with policy._connect() as connection:
                workspace_aliases = {row['legacy_id'][len(subject)+1:]: row['target_id']
                    for row in connection.execute("SELECT legacy_id,target_id FROM file_policy_legacy_aliases WHERE kind='workspace'")
                    if row['legacy_id'].startswith(subject + ':')}
        normalized_workspace = workspace_aliases.get(normalized_workspace, normalized_workspace)
        normalized_location_workspaces = frozenset(workspace_aliases.get(value, value) for value in normalized_location_workspaces)

        selected_ids: list[str] = []
        paused_ids: set[str] = set()
        aborted_run_ids: set[str] = set()
        chat_mapped = 0
        reset_message = "Paused because Agent filesystem permissions were reset"

        def _matches(task) -> bool:
            stable_id = str(getattr(task, "workspace_id", None) or "").strip()
            stable_id = workspace_aliases.get(stable_id, stable_id)
            if normalized_scope == "workspace":
                return stable_id == normalized_workspace
            if normalized_scope == "location":
                return bool(stable_id) and stable_id in normalized_location_workspaces
            if normalized_scope == "chat":
                return bool(chat_mapped) and str(
                    getattr(task, "session_id", None) or ""
                ).strip() == normalized_chat

            task_type = str(getattr(task, "task_type", None) or "llm").strip()
            action = str(getattr(task, "action", None) or "").strip()
            return (
                task_type in {"llm", "research"}
                or bool(stable_id)
                or (
                    task_type == "action"
                    and action in ADMIN_ONLY_TASK_ACTIONS
                )
            )

        # First transaction is the authority boundary. If it cannot commit, no
        # process-local cancellation is presented as a successful durable reset.
        db = SessionLocal()
        try:
            if normalized_scope == "chat":
                chat_mapped = int(
                    db.query(DbSession.id).filter(
                        DbSession.id == normalized_chat,
                        DbSession.owner == normalized_owner,
                    ).first()
                    is not None
                )
            candidates = db.query(ScheduledTask).filter(
                ScheduledTask.owner == normalized_owner,
            ).all()
            selected = [task for task in candidates if _matches(task)]
            selected_ids = [str(task.id) for task in selected]
            for task in selected:
                if str(getattr(task, "status", "") or "") == "active":
                    task.status = "paused"
                    paused_ids.add(str(task.id))
                if str(getattr(task, "status", "") or "") == "paused":
                    task.next_run = None
            if selected_ids:
                runs = db.query(TaskRun).filter(
                    TaskRun.task_id.in_(selected_ids),
                    TaskRun.status.in_(("queued", "running")),
                ).all()
                now = _utcnow()
                for run in runs:
                    run.status = "aborted"
                    run.error = reset_message
                    run.result = run.result or reset_message
                    run.finished_at = now
                    aborted_run_ids.add(str(run.id))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        handles: set[asyncio.Task] = set()
        for task_id in selected_ids:
            handles.update(self._execution_handles_for_task(task_id))
        current = asyncio.current_task()
        cancelled = {
            handle for handle in handles
            if handle is not current and not handle.done()
        }
        for handle in cancelled:
            handle.cancel()
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

        executing_cleared = 0
        async with self._executing_lock:
            for task_id in selected_ids:
                if task_id in self._executing:
                    self._executing.discard(task_id)
                    executing_cleared += 1

        # A running executor loaded its task before the DB-first pause and may
        # have written a computed next_run while handling cancellation. Reassert
        # the durable pause after every handle has joined.
        if selected_ids:
            db = SessionLocal()
            try:
                selected = db.query(ScheduledTask).filter(
                    ScheduledTask.id.in_(selected_ids),
                    ScheduledTask.owner == normalized_owner,
                ).all()
                for task in selected:
                    if str(getattr(task, "status", "") or "") == "active":
                        task.status = "paused"
                        paused_ids.add(str(task.id))
                    if str(getattr(task, "status", "") or "") == "paused":
                        task.next_run = None
                runs = db.query(TaskRun).filter(
                    TaskRun.task_id.in_(selected_ids),
                    TaskRun.status.in_(("queued", "running")),
                ).all()
                now = _utcnow()
                for run in runs:
                    run.status = "aborted"
                    run.error = reset_message
                    run.result = run.result or reset_message
                    run.finished_at = now
                    aborted_run_ids.add(str(run.id))
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        return {
            "selected": len(selected_ids),
            "paused": len(paused_ids),
            "runs_aborted": len(aborted_run_ids),
            "executions_cancelled": len(cancelled),
            "executing_cleared": executing_cleared,
            "chat_mapped": chat_mapped,
        }

    async def stop_background_tasks_for_foreground(self, *, reason: str = "Open Clank became active") -> int:
        """Cancel all in-process scheduler tasks because the user is active.

        This is intentionally blunt for scheduled/background work: when the
        user opens or uses Open Clank, foreground interaction wins immediately.
        Manual force-runs can be restarted by the user; automatic jobs will be
        deferred by their cancellation path instead of stealing the app.
        """
        async with self._executing_lock:
            # The server-owned theme task supplies artwork to the open UI.
            task_ids = [task_id for task_id in self._executing if not task_id.startswith('theme-emoji-')]
        stopped = 0
        handles = []
        for task_id in task_ids:
            for handle in self._execution_handles_for_task(task_id):
                handle.cancel()
                handles.append(handle)
                stopped += 1
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)
        for task_id in task_ids:
            if self._mark_run_aborted(task_id):
                stopped += 1
        if stopped:
            logger.info("Stopped %d background scheduler task(s): %s", stopped, reason)
        return stopped

    _THEME_EMOJI_RETRY_DEFERRED = 'Emoji source asked to retry later.'

    @staticmethod
    def _is_theme_emoji_task(task):
        return (task.id.startswith('theme-emoji-') and task.task_type == 'action'
                and task.action == 'theme_emoji_refresh')

    def theme_emoji_link(self, *, username: str, account_id: str, local_state):
        """Resolve only the existing task for this exact accepted consent."""
        from core.database import SessionLocal, ScheduledTask
        if (not self._auth_manager or self._auth_manager.account_id(username) != account_id
                or self._auth_manager.username_for_account_id(account_id) != username):
            raise ValueError('Account changed')
        state = local_state.obligation(account_id)
        if state['consent'] != 'accepted':
            raise ValueError('Emoji consent unavailable')
        frozen = {'account_id':account_id, **state}
        key = hashlib.sha256(json.dumps(frozen, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        task_id = 'theme-emoji-' + key[:32]
        with SessionLocal() as db:
            row = db.query(ScheduledTask).filter_by(id=task_id, owner=username, action='theme_emoji_refresh', status='active').first()
            if row is None or json.loads(row.prompt or '{}') != frozen:
                raise ValueError('Emoji update task unavailable')
        return task_id

    def theme_emoji_obligation_status(self, *, username: str, account_id: str, local_state):
        """Read the linked task; never create or schedule work from status."""
        from core.database import SessionLocal, ScheduledTask, TaskRun
        if (not self._auth_manager or self._auth_manager.account_id(username) != account_id
                or self._auth_manager.username_for_account_id(account_id) != username):
            raise ValueError('Account changed')
        state = local_state.obligation(account_id)
        frozen = {'account_id':account_id, **state}
        key = hashlib.sha256(json.dumps(frozen, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        db = SessionLocal()
        try:
            row = db.query(ScheduledTask).filter_by(id='theme-emoji-' + key[:32], owner=username, action='theme_emoji_refresh').first()
            run = db.query(TaskRun).filter(TaskRun.task_id == row.id, TaskRun.status.in_(('queued','running','waiting','resuming'))).order_by(TaskRun.started_at.desc()).first() if row else None
            last = db.query(TaskRun).filter(TaskRun.task_id == row.id, TaskRun.finished_at.isnot(None)).order_by(TaskRun.started_at.desc()).first() if row else None
            warning = False
            if last and last.status == 'success' and last.result:
                try:
                    warning = bool(set(json.loads(last.result).get('refreshWarnings', [])) & {'noto', 'kitchen'})
                except (ValueError, TypeError, AttributeError):
                    pass
            return {'next_due': row.next_run.isoformat() if row and row.status == 'active' and row.next_run else None,
                    'updates_enabled': bool(row and row.status == 'active' and state['consent'] == 'accepted'),
                    'last_check': row.last_run.isoformat() if row and row.last_run else None,
                    'last_error': bool(last and last.status == 'error'), 'last_warning':warning,
                    'runID':run.id if run else None, 'cancellable':bool(run and row.id in self._executing),
                    'acquisition_active':bool(row and row.id in self._executing)}
        finally:
            db.close()

    async def set_theme_emoji_updates(self, *, username: str, account_id: str, local_state, expected_revision: int, enabled: bool):
        """Explicitly pause/resume the same consent-linked scheduler task."""
        from core.atomic_io import _path_locks
        from core.database import SessionLocal, ScheduledTask, TaskRun
        def commit():
            with _path_locks([str(local_state._owner_path(account_id))]):
                if (not self._auth_manager or self._auth_manager.account_id(username) != account_id
                        or self._auth_manager.username_for_account_id(account_id) != username):
                    raise ValueError('Account changed')
                state = local_state.obligation(account_id)
                if state['consent'] != 'accepted' or state['consent_revision'] != expected_revision:
                    raise ValueError('Emoji consent changed')
                frozen = {'account_id':account_id, **state}
                key = hashlib.sha256(json.dumps(frozen, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                task_id = 'theme-emoji-' + key[:32]
                with SessionLocal() as db:
                    row = db.query(ScheduledTask).filter_by(id=task_id, owner=username, action='theme_emoji_refresh').first()
                    if row is None or json.loads(row.prompt or '{}') != frozen:
                        raise ValueError('Emoji update task unavailable')
                    last = db.query(TaskRun).filter(TaskRun.task_id == row.id, TaskRun.finished_at.isnot(None)).order_by(TaskRun.started_at.desc()).first()
                    if enabled and last and last.error == 'Source retry delay exceeds the automatic horizon; updates paused.':
                        raise ValueError('Source retry delay remains in effect')
                    row.status = 'active' if enabled else 'paused'
                    row.next_run = compute_next_run('weekly', row.scheduled_time, row.scheduled_day, None, after=_utcnow()) if enabled else None
                    db.commit()
                return task_id
        task = asyncio.create_task(asyncio.to_thread(commit))
        try:
            task_id = await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
                except Exception: break
            if not task.cancelled(): task.exception()
            raise
        if not enabled:
            await self.stop_task(task_id)
        return await asyncio.to_thread(self.theme_emoji_obligation_status, username=username, account_id=account_id, local_state=local_state)

    async def reconcile_theme_emoji_obligation(self, *, username: str, account_id: str, local_state,
                                              start_revision: int | None = None):
        """Commit desired task under the same lock as consent CAS."""
        from core.atomic_io import _path_locks
        from core.database import SessionLocal, ScheduledTask, TaskRun
        from src.openclank.theme_emoji_acquisition import THEME_EMOJI_ACQUISITION_READY
        def commit():
            with _path_locks([str(local_state._owner_path(account_id))]):
                if (not self._auth_manager or self._auth_manager.account_id(username) != account_id
                        or self._auth_manager.username_for_account_id(account_id) != username):
                    raise ValueError('Account changed')
                state = local_state.obligation(account_id)
                frozen = {'account_id':account_id, **state}
                key = hashlib.sha256(json.dumps(frozen, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                task_id = 'theme-emoji-' + key[:32]
                enabled = state['consent'] == 'accepted' and THEME_EMOJI_ACQUISITION_READY
                cancel = []
                with SessionLocal() as db:
                    rows = db.query(ScheduledTask).filter(ScheduledTask.owner == username, ScheduledTask.action == 'theme_emoji_refresh').all()
                    for row in rows:
                        if str(row.id or '').startswith('theme-emoji-') and (row.id != task_id or not enabled):
                            row.status, row.next_run = 'paused', None
                            cancel.append(row.id)
                    row = next((item for item in rows if item.id == task_id), None)
                    if enabled and row is None:
                        now = datetime.now(timezone.utc).replace(tzinfo=None)
                        row = ScheduledTask(id=task_id, owner=username, host_account_id=account_id, name='Refresh emoji artwork',
                            task_type='action', action='theme_emoji_refresh', prompt=json.dumps(frozen, sort_keys=True),
                            trigger_type='schedule', schedule='weekly', scheduled_time=now.strftime('%H:%M'),
                            scheduled_day=now.weekday(), next_run=now + timedelta(seconds=5), status='active',
                            output_target='none', notifications_enabled=False)
                        db.add(row)
                    db.commit()
                    kick = (enabled and row is not None and row.status == 'active'
                            and start_revision == state['consent_revision'])
                    if kick:
                        last = db.query(TaskRun).filter(TaskRun.task_id == task_id, TaskRun.finished_at.isnot(None)).order_by(TaskRun.started_at.desc()).first()
                        if last and last.result == self._THEME_EMOJI_RETRY_DEFERRED:
                            kick = row.next_run is not None and row.next_run <= _utcnow()
                        elif last and last.status == 'success':
                            try:
                                kick = json.loads(last.result or '{}').get('pending', True) is not False
                            except (ValueError, AttributeError):
                                pass
                return cancel, task_id if kick else None
        task = asyncio.create_task(asyncio.to_thread(commit))
        try:
            cancel, start_id = await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
                except Exception: break
            if not task.cancelled(): task.exception()
            raise
        for old_id in cancel:
            await self.stop_task(old_id)
        if start_id:
            await self.run_task_now(start_id)
        return {'acquisition_available':THEME_EMOJI_ACQUISITION_READY, **(
            await asyncio.to_thread(self.theme_emoji_obligation_status,
                username=username, account_id=account_id, local_state=local_state))}

    async def ensure_defaults(self, owner: str, *, account_id: str | None = None):
        """Create default housekeeping tasks for this owner (idempotent per action)."""
        from core.database import SessionLocal, ScheduledTask
        if self._skip_owner_maintenance(owner):
            return
        if account_id and (not self._auth_manager or self._auth_manager.account_id(owner) != account_id
                or self._auth_manager.username_for_account_id(account_id) != owner):
            raise ValueError('Account changed')
        try:
            from routes.prefs_routes import _load_for_user
            _prefs = _load_for_user(owner) or {}
        except Exception:
            _prefs = {}
        tasks_enabled = bool(_prefs.get("tasks_enabled"))
        tasks_opened = bool(_prefs.get("tasks_opened"))

        db = SessionLocal()
        try:
            if account_id and (not self._auth_manager or self._auth_manager.account_id(owner) != account_id
                    or self._auth_manager.username_for_account_id(account_id) != owner):
                raise ValueError('Account changed')
            from sqlalchemy import or_
            existing_actions = {
                row[0] for row in db.query(ScheduledTask.action).filter(
                    ScheduledTask.owner == owner,
                    ScheduledTask.task_type == "action",
                    or_(ScheduledTask.host_account_id == account_id, ScheduledTask.host_account_id.is_(None)),
                ).all() if row[0]
            }
            seeded = []
            for action, defs in HOUSEKEEPING_DEFAULTS.items():
                if action in existing_actions:
                    continue
                trigger_type = defs.get("trigger_type", "schedule")
                next_run = None
                if trigger_type == "schedule":
                    next_run = compute_next_run(
                        defs["schedule"], defs["scheduled_time"], None, None,
                        after=_utcnow(), cron_expression=defs["cron_expression"],
                    )
                ships_paused = bool(defs.get("ship_paused"))
                task = ScheduledTask(
                    id=str(uuid.uuid4())[:8],
                    owner=owner,
                    host_account_id=account_id,
                    name=defs["name"],
                    task_type="action",
                    action=action,
                    trigger_type=trigger_type,
                    trigger_event=defs.get("trigger_event"),
                    trigger_count=defs.get("trigger_count"),
                    trigger_counter=0,
                    schedule=defs["schedule"],
                    scheduled_time=defs["scheduled_time"],
                    cron_expression=defs["cron_expression"],
                    next_run=next_run,
                    # Most built-ins are active by default. The invasive
                    # AI/email/calendar tasks opt into a paused starting state
                    # via ship_paused so users can enable them deliberately.
                    status="paused" if ships_paused else "active",
                    output_target=defs.get("output_target", "none"),
                    notifications_enabled=False,
                )
                db.add(task)
                seeded.append(action)
            if account_id and (self._auth_manager.account_id(owner) != account_id
                    or self._auth_manager.username_for_account_id(account_id) != owner):
                raise ValueError('Account changed')
            db.commit()
        except ValueError:
            db.rollback()
            raise
        except Exception as e:
            logger.warning(f"Failed to create default tasks: {e}")
        finally:
            db.close()
        # Always ensure the personal assistant exists (independent of other tasks).
        try:
            await self.ensure_assistant_defaults(owner)
        except Exception as e:
            logger.warning(f"Failed to seed assistant for {owner}: {e}")

    async def ensure_assistant_defaults(self, owner: str):
        """Create the personal-assistant CrewMember, its pinned session, and three
        daily check-in ScheduledTasks for this owner — idempotent on is_default_assistant."""
        # Hard-reject synthetic owners. Without this, AuthMiddleware-stamped
        # values like 'internal-tool' (loopback agent-tool callbacks) or 'api'
        # (bearer-token integrations) would get a real assistant + 3 daily
        # check-ins seeded, which then double-fire alongside the human user's
        # check-ins. This was the root cause of the duplicate 'Morning check-in'
        # rows we had to manually clean up.
        if not owner or owner in RESERVED_USERNAMES or self._skip_owner_maintenance(owner):
            logger.info(f"ensure_assistant_defaults: skip synthetic owner {owner!r}")
            return
        from core.database import SessionLocal, CrewMember, ScheduledTask
        from core.database import Session as DbSession

        db = SessionLocal()
        try:
            existing = db.query(CrewMember).filter(
                CrewMember.owner == owner,
                CrewMember.is_default_assistant == True,  # noqa: E712
            ).first()
            if existing:
                return  # already seeded

            # Resolve a default model/endpoint from any existing session so the
            # assistant has something to call. The user can change this later.
            endpoint_id, endpoint_url, model = self._resolve_defaults(db, owner)

            # Voice comes from the owner's synced default persona (rulings
            # R13/R15); the mechanical operating rules live in
            # ASSISTANT_TASK_FRAMING and are appended at run time by
            # _compose_assistant_prompt(), never stored on the row.
            try:
                from src.default_persona import get_default_persona
                _persona = get_default_persona(owner)
                default_personality = _persona["system_prompt"]
                assistant_name = _persona["name"]
            except Exception:
                default_personality = (
                    "You are a helpful, balanced assistant. Match your "
                    "response style to the user's needs."
                )
                assistant_name = "Open Clank"

            # Create the singleton session first (CrewMember.session_id links to it).
            session_id = await self._mint_session_id()
            sess = DbSession(
                id=session_id,
                name="Assistant",
                endpoint_url=endpoint_url or "",
                endpoint_id=endpoint_id,
                model=model or "",
                owner=owner,
                is_important=True,
                mode="agent",
                folder="Assistant",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            db.add(sess)
            db.flush()

            # Create the assistant CrewMember.
            crew_id = str(uuid.uuid4())
            assistant = CrewMember(
                id=crew_id,
                owner=owner,
                name=assistant_name,
                avatar=None,
                user_name=None,
                personality=default_personality,
                model=model,
                endpoint_url=endpoint_url,
                endpoint_id=endpoint_id,
                greeting=None,
                enabled_tools=json.dumps([
                    "manage_calendar", "manage_notes", "manage_tasks", "read_copal", "manage_copal", "manage_memory",
                    "list_email_accounts", "list_emails", "read_email", "send_email", "reply_to_email", "archive_email",
                    "mark_email_read", "delete_email", "resolve_contact",
                    "search_chats", "web_search", "web_fetch", "read_file",
                    "create_document", "update_document", "edit_document",
                    "generate_image", "trigger_research",
                    "download_model", "serve_model", "list_served_models", "stop_served_model",
                    "edit_image",
                ]),
                session_id=session_id,
                is_active=True,
                sort_order=0,
                is_default_assistant=True,
                timezone=None,  # user picks in settings; None = legacy UTC behavior
            )
            db.add(assistant)

            # Link the session back to the crew member so UI can resolve either way.
            sess.crew_member_id = crew_id

            # No auto-seeded check-in tasks. The old behaviour created three
            # daily ScheduledTasks (Morning/Midday/Evening) under every new
            # owner, which was intrusive and ran under whatever account was
            # marked is_default globally. Users now create their own
            # recurring tasks from the Tasks UI.

            db.commit()
            logger.info(f"Seeded personal assistant (crew {crew_id}) for owner={owner}")
        except Exception as e:
            logger.exception(f"ensure_assistant_defaults({owner}) failed: {e}")
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()
