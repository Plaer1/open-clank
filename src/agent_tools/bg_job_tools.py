"""Agent tool to inspect and control detached background `bash` jobs.

`bash` blocks prefixed with a `#!bg` marker run detached via `src.bg_jobs`; the
agent is auto-re-invoked with the output when they finish. This tool covers the
gaps in that flow: list the jobs in the current chat, read a still-running job's
output on demand, and kill a runaway job instead of waiting out its max-runtime.

Registry tool (`TOOL_HANDLERS["manage_bg_jobs"]`). Jobs are scoped to the chat
that launched them, so every action requires the caller's `session_id` and a job
from another session is treated as not found.
"""

import json
import time
import asyncio
from typing import Any, Dict, List

_LIST_ACTIONS = {"list", "ls", "jobs"}
_POLL_ACTIONS = {"poll", "status", "show", "get"}
_TAIL_ACTIONS = {"output", "read", "tail"}
_KILL_ACTIONS = {"kill", "stop", "cancel", "terminate"}
_WRITE_ACTIONS = {"write", "send", "stdin"}
_DELETE_ACTIONS = {"delete", "remove"}


def _age(rec: Dict[str, Any]) -> str:
    start = rec.get("started_at")
    if not start:
        return "?"
    secs = int(time.time() - start)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60}m"


def _status_label(rec: Dict[str, Any]) -> str:
    status = rec.get("status", "?")
    if rec.get("killed"):
        return "killed"
    if rec.get("timed_out"):
        return "timed out"
    if rec.get("died"):
        return "died"
    if status in ("done", "failed"):
        return f"{status} (exit {rec.get('exit_code')})"
    return status


def _row(rec: Dict[str, Any]) -> str:
    cmd = (rec.get("command") or "").strip().splitlines()[0][:80]
    return f"[{rec.get('id')}] {_status_label(rec)} | {_age(rec)} | {cmd}"


def _public_session(rec: Dict[str, Any]) -> Dict[str, Any]:
    started_at = rec.get("started_at")
    ended_at = rec.get("ended_at")
    elapsed_s = None
    if started_at:
        elapsed_s = max(0.0, float(ended_at or time.time()) - float(started_at))
    return {
        "id": rec.get("id"),
        "state": rec.get("status"),
        "status": rec.get("status"),
        "command": rec.get("command"),
        "owner": rec.get("owner"),
        "workspace": rec.get("workspace"),
        "started_at": started_at,
        "ended_at": ended_at,
        "elapsed_s": elapsed_s,
        "exit_code": rec.get("exit_code"),
        "signal": rec.get("signal"),
        "containment": rec.get("containment"),
        "network": rec.get("network", "enabled"),
        "timed_out": bool(rec.get("timed_out")),
        "killed": bool(rec.get("killed")),
    }


class ManageBgJobsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src import bg_jobs

        session_id = ctx.get("session_id")
        raw = (content or "").strip()
        try:
            args = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        action = str(args.get("action", "list")).strip().lower()
        job_id = str(args.get("job_id") or args.get("id") or "").strip()
        owner = str(ctx.get("owner") or "")
        workspace = str(ctx.get("workspace") or "")

        if not session_id:
            return {"error": "manage_bg_jobs: no active chat session; background jobs are scoped to a chat.", "exit_code": 1}
        if not owner:
            return {"error": "manage_bg_jobs: no authenticated owner; background jobs are tenant-scoped.", "exit_code": 1}
        if not workspace:
            return {"error": "manage_bg_jobs: no active workspace; background jobs are workspace-scoped.", "exit_code": 1}

        scope = {
            "session_id": session_id,
            "owner": owner,
            "workspace": workspace,
        }

        if action in _LIST_ACTIONS:
            jobs: List[Dict[str, Any]] = bg_jobs.list_for_scope(**scope)
            if not jobs:
                return {
                    "output": "No background jobs (shell sessions) in this chat.",
                    "exit_code": 0,
                    "sessions": [],
                    "page": {"cursor": 0, "next_cursor": None, "has_more": False},
                }
            jobs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
            try:
                cursor = max(0, int(args.get("cursor") or 0))
                limit = max(1, min(int(args.get("limit") or 20), 50))
            except (TypeError, ValueError):
                cursor, limit = 0, 20
            page = jobs[cursor:cursor + limit]
            next_cursor = cursor + len(page) if cursor + len(page) < len(jobs) else None
            lines = "\n".join(_row(r) for r in page)
            return {
                "output": f"{len(jobs)} shell session(s):\n{lines}",
                "exit_code": 0,
                "sessions": [
                    _public_session(rec)
                    for rec in page
                ],
                "page": {
                    "cursor": cursor,
                    "next_cursor": next_cursor,
                    "has_more": next_cursor is not None,
                    "total": len(jobs),
                },
            }

        if action == "start":
            command = str(args.get("command") or "").strip()
            if not command:
                return {"error": "manage_bg_jobs: start requires command.", "exit_code": 1}
            try:
                import os
                from core.platform_compat import find_bash
                from src.shell_policy import (
                    contained_argv,
                    require_shell_approval,
                    shell_command_argv,
                )

                max_runtime_s = max(1, min(int(args.get("max_runtime_s") or bg_jobs.DEFAULT_MAX_RUNTIME_S), 24 * 3600))
                network = args.get("network")
                shell = find_bash() or (
                    os.environ.get("ComSpec", "cmd.exe")
                    if os.name == "nt"
                    else "/bin/bash"
                )
                _, containment = contained_argv(
                    shell_command_argv(shell, command),
                    workspace=workspace,
                    cwd=workspace,
                    network=network,
                )
                approval_binding = await require_shell_approval(
                    command,
                    ctx=ctx,
                    cwd=workspace,
                    containment=containment,
                    network=str(
                        network
                        or os.getenv("OPEN_CLANK_SHELL_NETWORK", "enabled")
                    ),
                )
                rec = bg_jobs.launch(
                    command,
                    session_id=session_id,
                    owner=owner,
                    workspace=workspace,
                    cwd=workspace,
                    max_runtime_s=max_runtime_s,
                    network=network,
                    approval_binding=approval_binding,
                )
            except Exception as exc:
                return {"error": f"manage_bg_jobs: {exc}", "exit_code": 1}
            return {
                "output": f"Started shell session `{rec['id']}`.",
                "exit_code": 0,
                "session": _public_session(rec),
            }

        if action in _POLL_ACTIONS or action in _TAIL_ACTIONS or action in _KILL_ACTIONS or action in _WRITE_ACTIONS or action in _DELETE_ACTIONS or action == "wait":
            if not job_id:
                return {"error": f"manage_bg_jobs: action '{action}' requires a job_id (see action='list').", "exit_code": 1}
            rec = bg_jobs.get_scoped(job_id, **scope)
            if rec is None:
                return {"error": f"manage_bg_jobs: no shell session '{job_id}' in this scope.", "exit_code": 1}

            if action in _KILL_ACTIONS:
                if rec.get("status") != "running":
                    return {"output": f"Job `{job_id}` already {_status_label(rec)}; nothing to kill.", "exit_code": 0}
                killed = bg_jobs.kill(job_id, **scope)
                if killed is None:
                    return {"error": f"manage_bg_jobs: shell session '{job_id}' left this scope.", "exit_code": 1}
                return {"output": f"Killed background job `{job_id}` ({(killed or {}).get('command', '').splitlines()[0][:80]}).", "exit_code": 0}

            if action in _DELETE_ACTIONS:
                if not bg_jobs.delete(job_id, **scope):
                    return {"error": f"manage_bg_jobs: shell session '{job_id}' left this scope.", "exit_code": 1}
                return {"output": f"Deleted shell session `{job_id}` and its retained output.", "exit_code": 0}

            if action in _WRITE_ACTIONS:
                data = str(args.get("data") or args.get("input") or "")
                if not data:
                    return {"error": "manage_bg_jobs: write requires data.", "exit_code": 1}
                if not bg_jobs.write(job_id, data, **scope):
                    return {"error": f"manage_bg_jobs: shell session '{job_id}' is not accepting input.", "exit_code": 1}
                return {"output": f"Wrote {len(data.encode('utf-8'))} bytes to `{job_id}`.", "exit_code": 0}

            if action == "wait":
                try:
                    raw_timeout = args.get("timeout_s")
                    timeout_s = max(
                        0.0,
                        min(float(30 if raw_timeout is None else raw_timeout), 30.0),
                    )
                except (TypeError, ValueError):
                    timeout_s = 30.0
                deadline = time.monotonic() + timeout_s
                while rec.get("status") == "running":
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(0.1, remaining))
                    refreshed = bg_jobs.get_scoped(job_id, **scope)
                    if refreshed is None:
                        break
                    rec = refreshed
                return {
                    "output": f"Job `{job_id}` is {_status_label(rec or {})}.",
                    "exit_code": 0,
                    "session": _public_session(rec or {}),
                    "timed_out": bool(rec and rec.get("status") == "running"),
                }

            if action in _POLL_ACTIONS:
                return {
                    "output": f"Job `{job_id}` is {_status_label(rec)} ({_age(rec)}).",
                    "exit_code": 0,
                    "session": _public_session(rec),
                }

            try:
                cursor = max(0, int(args.get("cursor") or 0))
                limit = max(1, min(int(args.get("limit") or 16384), 64 * 1024))
            except (TypeError, ValueError):
                cursor, limit = 0, 16384
            try:
                frame = bg_jobs.tail(
                    job_id,
                    cursor=cursor,
                    limit=limit,
                    **scope,
                )
            except KeyError:
                return {"error": f"manage_bg_jobs: shell session '{job_id}' left this scope.", "exit_code": 1}
            out = frame["output"] or "(no output yet)"
            return {
                "output": f"Job `{job_id}` [{_status_label(rec)}, {_age(rec)}]\n\nOutput:\n{out}",
                "exit_code": 0,
                "frame": frame,
            }

        return {
            "error": (
                f"manage_bg_jobs: unknown action '{action}'. "
                "Use start, poll, write, wait, terminate, delete, list, or tail."
            ),
            "exit_code": 1,
        }
