"""Read admitted Stats for one account-bound scheduled execution."""
from __future__ import annotations

from sqlalchemy.orm import load_only

from core.database import ScheduledTask, Session, TaskRun
from core.stats_models import StatsEvent
from services.stats.ledger import project_admitted_events
from services.stats.pricing import cost_envelope
from services.stats.query import _install_progress_handler
from src.openclank.task_observations import task_account_scope
from src.openclank.task_execution_context import (
    ExecutionContextUnavailable,
    TASKSPEC_FIELDS,
    read_context,
    taskspec_sha256,
)


FACT_CAP = 500


def _elapsed_ms(run: TaskRun) -> int | None:
    if run.started_at is None or run.finished_at is None or run.finished_at < run.started_at:
        return None
    return round((run.finished_at - run.started_at).total_seconds() * 1000)


def _current_chat(db, task: ScheduledTask, run: TaskRun, owner: str, account: str):
    """Join only the chat in the witnessed task configuration."""
    if (task.host_account_id != account or db.get_bind().dialect.name != "sqlite"
            or not task.session_id or not task.workspace_id):
        return None
    try:
        witness = read_context(db.connection().connection.driver_connection, task_id=task.id, run_id=run.id)
        fields = {key: getattr(task, key) for key in TASKSPEC_FIELDS}
        spec = taskspec_sha256({key: int(value) if type(value) is bool else value for key, value in fields.items()})
    except ExecutionContextUnavailable:
        return None
    if not witness or (witness["account_id"], witness["workspace_id"], witness["taskspec_sha256"]) != (
        account, task.workspace_id, spec,
    ):
        return None
    chat = db.get(Session, task.session_id)
    return chat if chat is not None and chat.owner == owner and chat.workspace_id == witness["workspace_id"] else None


def task_run_usage(db, *, owner, account, task_id, run_id, cancel_event, deadline):
    """Return exact run measurements or None for an unowned run."""
    from routes.stats_routes import _active_price_schedules, _PRICING_PROFILES

    raw = _install_progress_handler(db, cancel_event, deadline)
    try:
        record = (
            db.query(TaskRun, ScheduledTask)
            .join(ScheduledTask, ScheduledTask.id == TaskRun.task_id)
            .filter(TaskRun.id == run_id, TaskRun.task_id == task_id,
                    *task_account_scope(owner, account, include_legacy=True))
            .options(load_only(TaskRun.id, TaskRun.task_id, TaskRun.started_at, TaskRun.finished_at))
            .first()
        )
        if record is None:
            return None
        run, task = record
        result = {
            "run_id": run.id,
            "wall_elapsed_ms": _elapsed_ms(run),
            "state": "unavailable",
            "reason": "execution_witness_unavailable",
            "input_tokens": None,
            "output_tokens": None,
            "cost": None,
        }
        chat = _current_chat(db, task, run, owner, account)
        if chat is None:
            return result
        rows = (
            db.query(StatsEvent)
            .filter(
                StatsEvent.owner == owner,
                StatsEvent.root_operation_id == f"turn-task-run:{run.id}",
                StatsEvent.session_id == chat.id,
                StatsEvent.workspace_id == chat.workspace_id,
                StatsEvent.incognito.is_(False),
                StatsEvent.event_kind != "context_receipt",
            )
            .limit(FACT_CAP + 1)
            .all()
        )
        if not rows:
            return {**result, "reason": "no_admitted_usage"}
        if len(rows) > FACT_CAP:
            return {**result, "reason": "usage_bound_reached"}
        projection = project_admitted_events(rows, cancel_event=cancel_event, deadline=deadline)
        if projection.coverage == "partial_ambiguous":
            return {**result, "reason": "usage_overlap_ambiguous"}
        result.update(state="partial", reason="admitted_facts_only")
        for metric in ("input_tokens", "output_tokens"):
            values = [
                getattr(event, metric) for event in projection.events
                if getattr(event, metric + "_state") == "reported" and type(getattr(event, metric)) is int
                and getattr(event, metric) >= 0
            ]
            result[metric] = sum(values) if values else None
        # This path uses the versioned Stats cost owner. Only return its totals
        # and coverage, never provider account or per-attempt identifiers.
        schedules = _active_price_schedules(db, cancel_event, deadline)
        if len(schedules) <= 10000:
            priced = cost_envelope(
                projection.events, schedules, profiles=_PRICING_PROFILES,
                cancel_event=cancel_event, deadline=deadline,
            )
            result["cost"] = {
                **priced["totals"],
                "coverage": priced["coverage"]["state"],
            }
        return result
    finally:
        if raw is not None:
            raw.set_progress_handler(None, 0)
