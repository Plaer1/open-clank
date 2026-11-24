"""Bounded, owner-scoped scheduler metadata reads; no scheduler or task mutation."""
from __future__ import annotations

import time
from typing import Any
from sqlalchemy import or_, text

from core.database import ScheduledTask, TaskRun, TaskWaitRequest
from src.openclank.orchestration_projection import (
    ProjectionError, decode_scheduler_observation_cursor, project_scheduler_observations,
)


def _stamp(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def task_account_scope(owner: str, account_id: str, *, include_legacy: bool = False):
    """Shared row predicate; caller must already prove the account."""
    if not owner or not account_id:
        raise ValueError('task account scope unavailable')
    binding = (or_(ScheduledTask.host_account_id == account_id, ScheduledTask.host_account_id.is_(None))
               if include_legacy else ScheduledTask.host_account_id == account_id)
    return ScheduledTask.owner == owner, binding


def scheduler_observations(db, *, owner: str, source: str, limit: int,
                           cursor: str | None = None, workspace_id: str | None = None,
                           account_id: str, policy_generation: int) -> dict[str, Any]:
    """Read one lexical keyset page of explicitly selected safe columns.

    The caller authenticates owner and resolves a supplied workspace before
    calling. Pages are live observations, not a transactional multi-page snapshot.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ProjectionError("INVALID_OBSERVATION_PAGE", "Scheduler page limit is invalid")
    if not workspace_id or db.get_bind().dialect.name != 'sqlite':
        raise ProjectionError('OBSERVATION_UNAVAILABLE', 'Scoped scheduler observation is unavailable', 503)
    after = decode_scheduler_observation_cursor(owner, workspace_id, source, cursor, account_id=account_id, policy_generation=policy_generation)
    selected_workspace = [ScheduledTask.workspace_id == workspace_id] if workspace_id else []
    if source == "scheduler_routine":
        query = db.query(ScheduledTask.id, ScheduledTask.name, ScheduledTask.status, ScheduledTask.task_type,
                         ScheduledTask.trigger_type, ScheduledTask.next_run, ScheduledTask.last_run,
                         ScheduledTask.updated_at).filter(ScheduledTask.owner == owner, ScheduledTask.host_account_id == account_id, *selected_workspace)
        key = ScheduledTask.id
    elif source == "scheduler_run":
        from src.openclank.task_execution_context import profile_available
        raw = db.connection().connection.driver_connection
        if not profile_available(raw):
            raise ProjectionError('OBSERVATION_UNAVAILABLE', 'Run execution context profile is unavailable', 503)
        query = db.query(TaskRun.id, TaskRun.task_id, TaskRun.status, TaskRun.started_at, TaskRun.finished_at,
                         ScheduledTask.name).join(ScheduledTask, TaskRun.task_id == ScheduledTask.id).filter(
                             ScheduledTask.owner == owner, ScheduledTask.host_account_id == account_id,
                             text("CASE WHEN length(task_runs.execution_context_json)<=4096 AND json_valid(task_runs.execution_context_json) THEN json_extract(task_runs.execution_context_json,'$.workspace_id')=:w AND json_extract(task_runs.execution_context_json,'$.account_id')=:a ELSE 0 END")
                         ).params(w=workspace_id, a=account_id)
        key = TaskRun.id
    elif source == "scheduler_wait":
        # The wait writer can persist a workspace without an authenticated
        # native binding. Require its exact run's positive execution witness;
        # a current task account or the wait's workspace alone is insufficient.
        from src.openclank.task_execution_context import profile_available
        if not profile_available(db.connection().connection.driver_connection):
            raise ProjectionError('OBSERVATION_UNAVAILABLE', 'Run execution context profile is unavailable', 503)
        query = db.query(TaskWaitRequest.id, TaskWaitRequest.task_id, TaskWaitRequest.run_id, TaskWaitRequest.state,
                         TaskWaitRequest.kind, TaskWaitRequest.continuation_revision, TaskWaitRequest.created_at,
                         ScheduledTask.name).join(ScheduledTask, TaskWaitRequest.task_id == ScheduledTask.id).join(
                             TaskRun, (TaskRun.id == TaskWaitRequest.run_id) & (TaskRun.task_id == ScheduledTask.id)
                         ).filter(ScheduledTask.owner == owner, ScheduledTask.host_account_id == account_id, TaskWaitRequest.owner == owner,
                            text("CASE WHEN length(task_runs.execution_context_json)<=4096 AND json_valid(task_runs.execution_context_json) THEN json_extract(task_runs.execution_context_json,'$.workspace_id')=:w AND json_extract(task_runs.execution_context_json,'$.account_id')=:a ELSE 0 END")
                         ).params(w=workspace_id, a=account_id)
        if workspace_id:
            query = query.filter(TaskWaitRequest.workspace_id == workspace_id)
        key = TaskWaitRequest.id
    else:
        raise ProjectionError("INVALID_OBSERVATION_SOURCE", "Scheduler source is not supported")
    if after:
        query = query.filter(key > after)
    # LIMIT does not bound SQLite's work when filtering unindexed legacy rows.
    raw = db.connection().connection.driver_connection
    deadline = time.monotonic() + .15
    calls = [0]
    def budget():
        calls[0] += 1
        return int(calls[0] > 1000 or time.monotonic() > deadline)
    raw.set_progress_handler(budget, 1000)
    try:
        query = query.filter(text('length(scheduled_tasks.name) <= 4096 AND length(scheduled_tasks.id) <= 512 '
                                  'AND length(coalesce(scheduled_tasks.status,\'\')) <= 256 '
                                  'AND length(coalesce(scheduled_tasks.task_type,\'\')) <= 256 '
                                  'AND length(coalesce(scheduled_tasks.trigger_type,\'\')) <= 256'))
        if source == 'scheduler_run':
            query = query.filter(text("length(task_runs.id) <= 512 AND length(coalesce(task_runs.status,'')) <= 256"))
        elif source == 'scheduler_wait':
            query = query.filter(text("length(task_wait_requests.id) <= 512 AND length(task_wait_requests.run_id) <= 512 AND length(coalesce(task_wait_requests.state,'')) <= 256 AND length(coalesce(task_wait_requests.kind,'')) <= 256"))
        rows = query.order_by(key).limit(limit + 1).all()
        if source in ('scheduler_run', 'scheduler_wait'):
            from src.openclank.task_execution_context import read_context, ExecutionContextUnavailable
            for row in rows:
                try:
                    witness = read_context(raw, task_id=row.task_id, run_id=row.id if source == 'scheduler_run' else row.run_id)
                    if not witness or witness['account_id'] != account_id or witness['workspace_id'] != workspace_id:
                        raise ExecutionContextUnavailable('witness mismatch')
                except ExecutionContextUnavailable as exc:
                    raise ProjectionError('OBSERVATION_UNAVAILABLE', 'Run witness is unavailable', 503) from exc
    except ProjectionError:
        raise
    except Exception as exc:
        raise ProjectionError('OBSERVATION_UNAVAILABLE', 'Scheduler observation budget or source is unavailable', 503) from exc
    finally:
        raw.set_progress_handler(None, 0)
    items: list[dict[str, Any]] = []
    for row in rows[:limit]:
        ref = {"kind": source, "nativeId": row.id,
               **({"taskId": row.task_id} if source in {"scheduler_run", "scheduler_wait"} else {}),
               **({"runId": row.run_id} if source == "scheduler_wait" else {})}
        base = {"sourceRef": ref, "owner": owner,
                **({"workspaceId": workspace_id} if workspace_id else {}),
                "workspaceAvailability": "available" if workspace_id else "unknown",
                "title": str(row.name or "")[:200], "capabilities": ["inspect"], "links": []}
        if source == "scheduler_routine":
            items.append({**base, "displayCategory": "routine", "nativeStatus": row.status,
                          "routineType": row.task_type, "triggerType": row.trigger_type,
                          "nextRun": _stamp(row.next_run), "lastRun": _stamp(row.last_run),
                          "sourceWatermark": _stamp(row.updated_at)})
        elif source == "scheduler_run":
            items.append({**base, "displayCategory": "execution", "nativeStatus": row.status,
                          "startedAt": _stamp(row.started_at), "finishedAt": _stamp(row.finished_at),
                          "links": [{"kind": "scheduled_instance", "target": {"kind": "scheduler_routine", "nativeId": row.task_id},
                                     "provenance": "task_runs.task_id"}]})
        else:
            items.append({**base, "displayCategory": "wait", "nativeStatus": row.state, "waitKind": row.kind,
                          "sourceRevision": row.continuation_revision, "createdAt": _stamp(row.created_at),
                          "links": [{"kind": "wait_for", "target": {"kind": "scheduler_run", "nativeId": row.run_id},
                                     "provenance": "task_wait_requests.run_id"}]})
    return project_scheduler_observations(owner, workspace_id, source, items, time.time_ns() // 1_000_000,
                                          rows[limit - 1].id if len(rows) > limit else None,
                                          account_id=account_id, policy_generation=policy_generation)
