"""Minimal numeric learning occurrences; SQLite source commits are the authority.

No content/answers, award predicates or inferred historic activity live here.
Existing stores opt in through an offline operator tool, never startup conversion.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone, timedelta

SCHEMA = """
CREATE TABLE treehouse_stats_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE treehouse_stats_occurrences (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
 source_owner TEXT NOT NULL, source_event_id TEXT NOT NULL, source_revision TEXT NOT NULL,
 family TEXT NOT NULL, occurred_at TEXT NOT NULL, ingested_at REAL NOT NULL,
 entity_kind TEXT NOT NULL, entity_id TEXT NOT NULL, generation INTEGER NOT NULL,
 trust_class TEXT NOT NULL, facts_json TEXT NOT NULL, evidence_digest TEXT NOT NULL,
 UNIQUE(account_id,source_owner,source_event_id));
CREATE INDEX treehouse_stats_account_history ON treehouse_stats_occurrences(account_id,workspace_id,id);
CREATE TABLE treehouse_stats_counters (
 account_id TEXT NOT NULL,workspace_id TEXT NOT NULL,family TEXT NOT NULL,
 course_type TEXT NOT NULL,basis TEXT NOT NULL,trust_class TEXT NOT NULL,count INTEGER NOT NULL,
 PRIMARY KEY(account_id,workspace_id,family,course_type,basis,trust_class));
CREATE TABLE treehouse_stats_checkpoints (
 account_id TEXT NOT NULL,workspace_id TEXT NOT NULL,source_owner TEXT NOT NULL,
 coverage_start REAL NOT NULL,cursor TEXT NOT NULL,last_ingested_at REAL NOT NULL,
 PRIMARY KEY(account_id,workspace_id,source_owner));
"""
FACT_KEYS = frozenset({"courseId", "courseOwnerId", "courseType", "completionBasis", "curriculumRevision",
 "completionRuleVersion", "xpRuleVersion", "generation", "attemptId", "attempt", "assignmentId",
 "submissionId", "skillId", "score", "maxPoints", "points", "percent", "passPercent", "passed", "grade", "verified", "ruleVersion", "achievementId",
 "predicateVersion", "catalogRevision", "classification", "status", "result", "kind", "actorKind",
 "count", "durationMs", "intervalMs", "curriculumDigest", "observedCatalogueRevision", "sourceEventDigest", "correction", "revoked", "verificationBasis"})
FAMILIES = frozenset({"course.opened", "course.completed", "activity.completed", "enrollment.created",
 "enrollment.withdrawn", "submission.submitted", "submission.graded", "evidence.submitted",
 "evidence.approved", "evidence.rejected", "progress.reset", "learning.discussion.posted",
 "learning.discussion.reacted", "learning.discussion.removed", "learning.board.saved",
 "learning.credential.issued", "learning.credential.revoked", "learning.collection.saved", "learning.collection.archived"})


def available(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='treehouse_stats_meta'").fetchone() is not None


def initialize(db):
    db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
    db.execute("INSERT INTO treehouse_stats_meta VALUES('coverage_start',?)", (str(time.time()),))
    db.commit()


def numeric_facts(facts):
    result = {}
    for key, value in facts.items():
        if key not in FACT_KEYS or value is None:
            continue
        if isinstance(value, bool) or isinstance(value, int) and abs(value) <= 10**12:
            result[key] = value
        elif isinstance(value, float) and math.isfinite(value) and abs(value) <= 10**12:
            result[key] = value
        elif isinstance(value, str) and len(value) <= 160:
            result[key] = value
    return result


def engagement_projection_key(account_id, workspace_id):
    identity = json.dumps([account_id, workspace_id], separators=(",", ":"))
    return "engagement-duration-v1:" + hashlib.sha256(identity.encode()).hexdigest()


def engagement_duration(db, account_id, workspace_id):
    row = db.execute("SELECT value FROM treehouse_stats_meta WHERE key=?", (engagement_projection_key(account_id, workspace_id),)).fetchone()
    if row:
        return int(row[0])
    # Only stores that admitted observations before the aggregate joined need
    # this fallback; no synthetic history or startup conversion is performed.
    return int(db.execute("SELECT coalesce(sum(json_extract(facts_json,'$.durationMs')),0) FROM treehouse_stats_occurrences WHERE account_id=? AND workspace_id=? AND family='learning.active_time.observed'", (account_id, workspace_id)).fetchone()[0])


def capture(db, *, account_id, workspace_id, source_owner, source_event_id, source_revision,
            family, occurred_at, entity_kind, entity_id, generation=0, trust_class="observation", facts=None):
    """Join a trusted server source transaction. Never exposed as client ingest.

    Caller resolves account/grants and stable source identity before joining.
    Replay digest covers the complete normalized occurrence, excluding arrival time.
    A conflict aborts the source commit, including its counters/checkpoint.
    """
    if not available(db):
        return {"inserted": False, "unavailable": True}
    from src.openclank.copal_treehouse_repository import TreeHouseRepositoryError
    if trust_class not in {"observation", "required-work", "reviewed-evidence", "verified-source"}:
        raise ValueError("Unknown stats trust class")
    stamp = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Stats occurrence requires a UTC-aware source timestamp")
    occurred_at = stamp.astimezone(timezone.utc).isoformat()
    facts = numeric_facts(facts or {})
    body = dict(accountId=str(account_id), workspaceId=str(workspace_id), sourceOwner=str(source_owner),
                sourceEventId=str(source_event_id), sourceRevision=str(source_revision), family=str(family),
                occurredAt=occurred_at, entityKind=str(entity_kind), entityId=str(entity_id),
                generation=int(generation), trustClass=trust_class, facts=facts, producerVersion="1")
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    row = db.execute("SELECT evidence_digest FROM treehouse_stats_occurrences WHERE account_id=? AND source_owner=? AND source_event_id=?",
                     (account_id, source_owner, source_event_id)).fetchone()
    if row:
        if row[0] != digest:
            raise TreeHouseRepositoryError("stats_source_conflict", "Stats source ID was replayed with different facts", details={"sourceOwner": source_owner, "sourceEventId": source_event_id})
        return {"inserted": False, "evidenceDigest": digest}
    now = time.time()
    db.execute("INSERT INTO treehouse_stats_occurrences(account_id,workspace_id,source_owner,source_event_id,source_revision,family,occurred_at,ingested_at,entity_kind,entity_id,generation,trust_class,facts_json,evidence_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (account_id, workspace_id, source_owner, source_event_id, str(source_revision), family, occurred_at, now, entity_kind, entity_id, int(generation), trust_class, json.dumps(facts, separators=(",", ":")), digest))
    db.execute("INSERT INTO treehouse_stats_counters VALUES(?,?,?,?,?,?,1) ON CONFLICT(account_id,workspace_id,family,course_type,basis,trust_class) DO UPDATE SET count=count+1",
               (account_id, workspace_id, family, facts.get("courseType", "unknown"), facts.get("completionBasis", "unknown"), trust_class))
    if family == "learning.active_time.observed":
        projection_key = engagement_projection_key(account_id, workspace_id)
        existing_total = db.execute("SELECT value FROM treehouse_stats_meta WHERE key=?", (projection_key,)).fetchone()
        # The new occurrence is already inserted. First initialization includes
        # any genuinely stored early observations, then updates are O(1).
        total = int(existing_total[0]) + int(facts.get("durationMs", 0)) if existing_total else engagement_duration(db, account_id, workspace_id)
        db.execute("INSERT INTO treehouse_stats_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (projection_key, str(total)))
    start = float(db.execute("SELECT value FROM treehouse_stats_meta WHERE key='coverage_start'").fetchone()[0])
    db.execute("INSERT INTO treehouse_stats_checkpoints VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,workspace_id,source_owner) DO UPDATE SET cursor=excluded.cursor,last_ingested_at=excluded.last_ingested_at",
               (account_id, workspace_id, source_owner, start, source_event_id, now))
    return {"inserted": True, "evidenceDigest": digest}


def capture_state(db, state, previous, *, owner_id, workspace_id, account_id=None, context=None, generation=0):
    """Capture newly committed canonical events only; no implicit historical backfill."""
    if not available(db):
        return
    context = context or state
    previous_events = {event.get("id"): event for event in (previous or {}).get("events", [])}
    from src.openclank.copal_treehouse_contracts import course_learning_contract
    inherited = {}
    if account_id is not None:
        catalogue = db.execute("SELECT state_json FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?", (owner_id, workspace_id)).fetchone()
        if catalogue:
            inherited = {item["id"]: item for item in json.loads(catalogue[0]).get("events", [])}
    for event in state.get("events", []):
        if event.get("type") not in FAMILIES:
            continue
        if event.get("id") in previous_events:
            if event != previous_events[event["id"]]:
                from src.openclank.copal_treehouse_repository import TreeHouseRepositoryError
                raise TreeHouseRepositoryError("stats_source_conflict", "Committed learning event was changed", details={"sourceEventId": event["id"]})
            continue
        event_digest = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        source_owner = f"treehouse:{owner_id}:{workspace_id}"
        subject = account_id or event["subjectId"]
        frozen = db.execute("SELECT * FROM treehouse_stats_occurrences WHERE account_id=? AND source_owner=? AND source_event_id=?", (subject, source_owner, event["id"])).fetchone()
        if frozen:
            frozen_facts = json.loads(frozen["facts_json"])
            matches = frozen_facts.get("sourceEventDigest") == event_digest
            if not frozen_facts.get("sourceEventDigest"):
                # Early receipts predate raw source digests. Validate their
                # immutable original against the retained canonical journal;
                # never recompute its frozen semantics at today's revision.
                journals = db.execute("SELECT state_json FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?", (owner_id, workspace_id)).fetchall()
                source_course = frozen_facts.get("courseId")
                if source_course:
                    journals += db.execute("SELECT state_json FROM treehouse_progress WHERE learner_account_id=? AND owner_account_id=? AND workspace_id=? AND course_id=?", (subject, owner_id, workspace_id, source_course)).fetchall()
                originals = [item for journal in journals for item in json.loads(journal[0]).get("events", []) if item.get("id") == event["id"]]
                stamp = datetime.fromisoformat(event["at"].replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
                matches = bool(originals) and all(item == event for item in originals)
                matches = matches and (frozen["family"], frozen["entity_kind"], frozen["entity_id"], frozen["occurred_at"]) == (event["type"], event["entityType"], event["entityId"], stamp)
                matches = matches and all(frozen_facts.get(key) == value for key, value in numeric_facts(event.get("data") or {}).items())
            if not matches:
                from src.openclank.copal_treehouse_repository import TreeHouseRepositoryError
                raise TreeHouseRepositoryError("stats_source_conflict", "Committed learning source was replayed with different facts", details={"sourceEventId": event["id"]})
            continue
        if event["id"] in inherited:
            if event != inherited[event["id"]]:
                from src.openclank.copal_treehouse_repository import TreeHouseRepositoryError
                raise TreeHouseRepositoryError("stats_source_conflict", "Inherited source event changed", details={"sourceEventId": event["id"]})
            # Moving existing catalogue history into a learner partition is
            # not a new observation or an authorized historical backfill.
            continue
        data = dict(event.get("data") or {})
        course_id = data.get("courseId")
        if not course_id:
            for collection in ("activities", "assignments", "submissions", "evidence", "enrollments"):
                entity = context.get(collection, {}).get(event.get("entityId"), {})
                course_id = entity.get("courseId")
                if not course_id and entity.get("assignmentId"):
                    course_id = context.get("assignments", {}).get(entity["assignmentId"], {}).get("courseId")
                if course_id:
                    break
        facts = {}
        if course_id and course_id in context.get("courses", {}):
            facts.update(course_learning_contract(context, course_id))
            facts.update(courseId=course_id, courseOwnerId=owner_id)
        facts.update(data)
        facts["sourceEventDigest"] = event_digest
        family = event["type"]
        trust = "reviewed-evidence" if family in {"submission.graded", "evidence.approved", "evidence.rejected"} else "observation"
        if family == "course.completed" and facts.get("completionBasis") == "required-work":
            trust = "required-work"
        capture(db, account_id=account_id or event["subjectId"], workspace_id=workspace_id,
                source_owner=f"treehouse:{owner_id}:{workspace_id}", source_event_id=event["id"],
                source_revision=str(state.get("revision", 0)), family=family, occurred_at=event["at"],
                entity_kind=event["entityType"], entity_id=event["entityId"], generation=data.get("generation", generation), trust_class=trust, facts=facts)


def read(db, account_id, workspace_id, *, before=None, limit=30):
    gaps = ["No historical backfill before coverage start", "Cross-store sources cover delivered canonical receipts only; pre-receipt delivery gaps remain", "Foreground active time is a bounded client observation; offline/closed-page, stale-context and queue-overflow intervals may be missing; parallel tabs may overlap", "Unsupported external adapter outcomes are not inferred; current assessment and collaboration canonical events are captured"]
    base = {"schemaVersion": 1, "accountId": account_id, "workspace": workspace_id,
            "dayPolicy": {"version": "utc-day-v1", "timezone": "UTC"}, "gaps": gaps,
            "lifetimeScope": "observed-since-coverage-start", "accountWideSources": ["achievement-award", "achievement-reset"], "scoring": "No mastery or ranking score inferred"}
    if not available(db):
        return {**base, "status": "unavailable", "reason": "offline_stats_activation_required", "totals": [], "history": [], "coverage": []}
    limit = max(1, min(int(limit), 100))
    rows = db.execute("SELECT * FROM treehouse_stats_occurrences WHERE account_id=? AND (workspace_id=? OR workspace_id='') AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                      (account_id, workspace_id, before, before, limit+1)).fetchall()
    history = [{"id": r["id"], "sourceOwner": r["source_owner"], "sourceEventId": r["source_event_id"], "sourceRevision": r["source_revision"],
                "family": r["family"], "occurredAt": r["occurred_at"], "ingestedAt": r["ingested_at"], "entityKind": r["entity_kind"],
                "entityId": r["entity_id"], "generation": r["generation"], "trustClass": r["trust_class"], "facts": json.loads(r["facts_json"]), "evidenceDigest": r["evidence_digest"]} for r in rows[:limit]]
    totals = [{"family": r["family"], "courseType": r["course_type"], "completionBasis": r["basis"], "trustClass": r["trust_class"], "count": r["count"]} for r in db.execute("SELECT family,course_type,basis,trust_class,sum(count) AS count FROM treehouse_stats_counters WHERE account_id=? AND (workspace_id=? OR workspace_id='') GROUP BY family,course_type,basis,trust_class ORDER BY family,course_type,basis,trust_class", (account_id, workspace_id))]
    coverage = [{"sourceOwner": r["source_owner"], "coverageStart": r["coverage_start"], "cursor": r["cursor"], "lastIngestedAt": r["last_ingested_at"], "delivery": "atomic-at-source-commit-or-receipt", "lag": "cross-store-source-head-unknown"} for r in db.execute("SELECT * FROM treehouse_stats_checkpoints WHERE account_id=? AND (workspace_id=? OR workspace_id='')", (account_id, workspace_id))]
    days = [datetime.fromisoformat(r[0]).date() for r in db.execute("SELECT DISTINCT substr(occurred_at,1,10) FROM treehouse_stats_occurrences WHERE account_id=? AND (workspace_id=? OR workspace_id='') AND family IN ('activity.completed','submission.submitted','submission.graded','evidence.approved') ORDER BY 1", (account_id, workspace_id))]
    best = run = 0
    prior = None
    for day in days:
        run = run+1 if prior and day == prior+timedelta(days=1) else 1
        best = max(best, run)
        prior = day
    today = datetime.now(timezone.utc).date()
    current = run if days and days[-1] in {today, today-timedelta(days=1)} else 0
    start = float(db.execute("SELECT value FROM treehouse_stats_meta WHERE key='coverage_start'").fetchone()[0])
    return {**base, "status": "partial", "coverageStart": start, "totals": totals, "history": history,
            "nextBefore": history[-1]["id"] if len(rows)>limit else None, "coverage": coverage,
            "streak": {"current": current, "best": best, "basis": "observed-engagement-days"},
            "engagement": {"durationMs": engagement_duration(db, account_id, workspace_id), "policyVersion": "foreground-idle-v1", "trustClass": "observation", "scoring": "none"},
            "uniqueCourseCompletions": db.execute("SELECT count(DISTINCT source_owner || ':' || entity_id) FROM treehouse_stats_occurrences WHERE account_id=? AND (workspace_id=? OR workspace_id='') AND family='course.completed'", (account_id, workspace_id)).fetchone()[0]}
