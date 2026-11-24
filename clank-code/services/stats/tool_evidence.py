"""Content-free L01 archive evidence used by Activity and Quality.

Archive reads are bounded/read-only. No source body, summary, tool input or
output enters a numeric cohort. Missing timing/identity stays missing.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import timezone

from services.logging.projection import (
    COLUMNS, MAX_FACTS, LoggingError, LoggingProjection, iter_tool_facts,
    normalize_filters, resolve_archive_owner,
)
from services.stats.privacy import identity_handle


def load_evidence(owner, *, start_utc=None, end_utc=None, cancel_event=None,
                  deadline=None, projection=None):
    if projection is None:
        from src.openclank.conversation_archive import default_db_path
        projection = LoggingProjection(default_db_path(), archive_owner_resolver=resolve_archive_owner)
    filters = {}
    for key, value in (("start", start_utc), ("end", end_utc)):
        if value is not None:
            filters[key] = value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.isoformat()
    try:
        receipt = iter_tool_facts(owner, filters, cancel_event=cancel_event,
                                  deadline=deadline, projection=projection)
        facts = coalesce_facts(owner, receipt["facts"])
        # Select only numeric/source metadata, never the summary text. Reuse
        # the archive's owner/latest/tombstone/date authority and read limits.
        with projection._read(cancel_event, deadline) as connection:
            where, args = projection._scope(connection, owner, normalize_filters({**filters, "part_type": "compaction_projection"}))
            rows = connection.execute(
                f"SELECT {COLUMNS} FROM conversation_parts p WHERE {where} ORDER BY p.seq LIMIT ?",
                [*args, MAX_FACTS + 1],
            ).fetchall()
            compactions = [projection._part(owner, row, False) for row in rows[:MAX_FACTS]]
            # Compaction persistence precedes outbox delivery into parts. Read
            # its canonical record metadata directly, excluding summary text.
            bounds, values = ["owner=?"], [projection._archive_owner(owner)]
            normalized = normalize_filters(filters)
            for key, operator in (("start", ">="), ("end", "<")):
                if key in normalized:
                    bounds.append(f"time_created{operator}?")
                    values.append(normalized[key])
            stored = connection.execute(
                "SELECT chat_id,actor_id,summary_id,projection_revision,time_created "
                "FROM conversation_compaction_projections WHERE " + " AND ".join(bounds)
                + " ORDER BY time_created LIMIT ?", [*values, MAX_FACTS + 1],
            ).fetchall()
            existing = {(part["source_ref"]["session_handle"], part["actor_id"],
                         part["source_ref"]["part_id"]) for part in compactions}
            for row in stored[:MAX_FACTS]:
                session_handle = identity_handle(owner, "session_id", row["chat_id"])
                part_id = f"proj:{row['summary_id']}:{row['projection_revision']}"
                if (session_handle, row["actor_id"], part_id) in existing:
                    continue
                compactions.append({"time_created": row["time_created"],
                    "source_ref": {"authority": "conversation_compaction_projection",
                        "session_handle": session_handle, "actor_id": row["actor_id"],
                        "summary_id": row["summary_id"], "revision": row["projection_revision"]}})
            revision = projection._revision(connection, owner)
        coverage = dict(receipt["coverage"])
        coverage["compaction_source_revision"] = hashlib.sha256(json.dumps(
            [part["source_ref"] for part in compactions], sort_keys=True).encode()).hexdigest()
        if len(rows) > MAX_FACTS or len(stored) > MAX_FACTS or revision != coverage.get("source_revision"):
            coverage.update(state="partial", reason="compaction_limit" if len(rows) > MAX_FACTS else "source_changed_between_reads")
        return {"facts": facts, "compactions": compactions, "coverage": coverage}
    except LoggingError as exc:
        if exc.code in {"cancelled", "deadline_exceeded"}:
            raise
        return {"facts": [], "compactions": [], "coverage": {"state": "unavailable", "reason": exc.code}}


def coalesce_facts(owner, facts):
    result = {}
    for fact in facts:
        if fact.get("owner") != owner:
            raise ValueError("tool evidence owner does not match scope")
        # Explicit operation/attempt identity merges started/result snapshots.
        # Without it only the same canonical part can be merged safely.
        if fact.get("qualified_tool"):
            source = fact.get("source_ref") or {}
            key = ("qualified_part", fact.get("session_id"), fact.get("actor_id"), source.get("message_id"), source.get("part_id"))
        elif fact.get("operation_id") and fact.get("attempt_id"):
            key = (fact.get("runtime_generation"), fact.get("session_id"), fact.get("actor_id"),
                   fact["operation_id"], fact["attempt_id"], fact.get("tool_name"))
        else:
            source = fact.get("source_ref") or {}
            key = (fact.get("session_id"), fact.get("actor_id"), source.get("message_id"),
                   source.get("part_id"), fact.get("event_id"))
        previous = result.get(key)
        if previous is None or (fact.get("terminal") and not previous.get("terminal")):
            result[key] = fact
    return list(result.values())


def measured_duration(fact):
    value = fact.get("duration_ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def cohort(facts, coverage):
    durations = [value for fact in facts if (value := measured_duration(fact)) is not None]
    terminal = [fact for fact in facts if fact.get("terminal")]
    return {"state": "unavailable" if coverage.get("state") == "unavailable" else "reported", "calls": len(facts),
            "known_logical_operations": len({(fact.get("session_id"), fact.get("actor_id"), fact.get("runtime_generation"), fact["operation_id"]) for fact in facts if fact.get("operation_id")}),
            "unattributed_calls": sum(not fact.get("operation_id") for fact in facts),
            "terminal_calls": len(terminal), "failures": sum(fact.get("status") in {"error", "failed"} for fact in terminal),
            "cancelled": sum(fact.get("status") == "cancelled" for fact in terminal),
            "duration_ms": sum(durations) if durations else None,
            "duration_population": len(durations), "untimed_calls": len(facts) - len(durations),
            "duration_method": "canonical_tool_start_end", "coverage": coverage}


def measured_concurrency(owner, facts, linked=None):
    receipt = linked or {"links": {}, "reason": "usage_unavailable"}
    linked = receipt.get("links") or {}
    groups = defaultdict(list)
    unclocked = 0
    for fact in facts:
        proof = fact.get("qualified_tool")
        if not isinstance(proof, dict):
            unclocked += 1
            continue
        groups[(proof["root_handle"], proof["clock_handle"])].append((proof["start_us"],proof["end_us"],proof.get("join_handle")))
    rows = []
    for (root,clock), intervals in groups.items():
        boundaries = sorted([(a,1) for a,b,_ in intervals if b>a] + [(b,-1) for a,b,_ in intervals if b>a])
        active=peak=elapsed=0
        previous=None
        for point,change in boundaries:
            if previous is not None and active>0: elapsed += point-previous
            active += change
            peak=max(peak,active)
            previous=point
        rows.append({"root_handle":root,"clock_handle":clock,"peak":peak,"timed_calls":len(intervals),
                     "aggregate_call_ms":sum(b-a for a,b,_ in intervals)/1000,
                     "elapsed_union_ms":elapsed/1000,
                     "usage_link": "linked" if all(linked.get(k) == "linked" for _,_,k in intervals) else "partial" if any(linked.get(k) == "linked" for _,_,k in intervals) else "unlinked",
                     "usage_link_reason": receipt.get("reason") or (None if all(linked.get(k) == "linked" for _,_,k in intervals) else "exact_attribution_unavailable")})
    return {"state":"partial" if rows and unclocked else "reported" if rows else "unavailable",
            "method":"qualified_same_process_monotonic_intervals","clocks":rows,"unclocked_calls":unclocked}


def quality_events(owner, evidence):
    facts = evidence["facts"]
    attempts = defaultdict(set)
    for fact in facts:
        if fact.get("operation_id") and fact.get("attempt_id"):
            key = (fact.get("runtime_generation"), fact.get("session_id"), fact.get("actor_id"), fact["operation_id"])
            attempts[key].add(fact["attempt_id"])
    groups = defaultdict(list)
    for fact in facts:
        if not fact.get("terminal"):
            continue
        key = (fact.get("runtime_generation"), fact.get("session_id"), fact.get("actor_id"), fact.get("operation_id"))
        groups[key if fact.get("operation_id") else (fact["event_id"],)].append(fact)
    rows = []
    for key, group in groups.items():
        group.sort(key=lambda fact: (fact.get("event_time_ms") or 0, fact["event_id"]))
        final = group[-1]
        status = final.get("status")
        identifier = hashlib.sha256(json.dumps([fact["event_id"] for fact in group]).encode()).hexdigest()
        row = {"owner": owner, "evidence_id": "tool_" + identifier,
               "event_time_ms": final.get("event_time_ms"),
               "outcome": "failure" if status in {"failed", "error"} else "success" if status == "completed" else "unknown"}
        primary = next((fact for fact in group if fact.get("status") in {"failed", "error"}), final)
        row.update(source_ref=primary.get("source_ref"),
                   session_handle=(primary.get("source_ref") or {}).get("session_handle"),
                   source_state="available", body_state="published")
        statuses = {fact.get("status") for fact in group}
        if statuses & {"failed", "error", "completed"}:
            row["tool_failure"] = bool(statuses & {"failed", "error"})
        if len(attempts.get(key, ())) > 1:
            row["retry"] = True
        rows.append(row)
    for part in evidence["compactions"]:
        source = part["source_ref"]
        identifier = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
        rows.append({"owner": owner, "evidence_id": "compact_" + identifier,
                     "event_time_ms": part.get("time_created"), "outcome": "unknown", "compacted": True,
                     "source_ref": source, "session_handle": source.get("session_handle"),
                     "source_state": "available", "body_state": "published" if source.get("authority") == "conversation_archive" else "unpublished"})
    return rows
