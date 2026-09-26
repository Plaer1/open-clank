"""Account scoped persistence for TreeHouse curriculum and grants.

The Copal document bridge is deliberately owner-local.  TreeHouse sharing
needs one small server side index that can resolve a grant while the course
body remains in the owner's namespace.  This repository is that index and the
catalogue boundary; learner progress is kept in a separate account keyed
projection so accepting a course never moves evidence into the owner's store.
"""

from __future__ import annotations

import hashlib
import copy
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class TreeHouseRepositoryError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 409, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


class TreeHouseRepository:
    """A restart safe, process and multi-worker safe TreeHouse index.

    The database is intentionally independent of generic Copal documents.  A
    course row contains the event aggregate for its immutable owner account;
    progress rows contain only the learner's projection/events.  All writes
    use SQLite's immediate transaction plus an expected revision check.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS treehouse_catalogues (
                    owner_account_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (owner_account_id, workspace_id)
                );
                CREATE TABLE IF NOT EXISTS treehouse_grants (
                    grant_id TEXT PRIMARY KEY,
                    owner_account_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    course_id TEXT NOT NULL,
                    recipient_account_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('learn', 'edit')),
                    access_revision INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    accepted_at TEXT,
                    revoked_at TEXT,
                    revision INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    grant_group_id TEXT
                );
                CREATE INDEX IF NOT EXISTS treehouse_grants_recipient
                    ON treehouse_grants(recipient_account_id, workspace_id, revoked_at);
                CREATE TABLE IF NOT EXISTS treehouse_recipient_index (
                    recipient_account_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL PRIMARY KEY,
                    owner_account_id TEXT NOT NULL,
                    course_id TEXT NOT NULL,
                    access_revision INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS treehouse_progress (
                    learner_account_id TEXT NOT NULL,
                    owner_account_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    course_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    reset_epoch INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (learner_account_id, owner_account_id, workspace_id, course_id)
                );
                CREATE TABLE IF NOT EXISTS treehouse_progress_reset_generations (
                    learner_account_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (learner_account_id, workspace_id)
                );
                CREATE TABLE IF NOT EXISTS treehouse_idempotency (
                    principal_account_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (principal_account_id, workspace_id, command_id)
                );
                CREATE TABLE IF NOT EXISTS treehouse_attachment_preparations (
                    preparation_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    caller_account_id TEXT NOT NULL,
                    owner_account_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    course_id TEXT NOT NULL,
                    lesson_id TEXT NOT NULL,
                    policy_generation INTEGER NOT NULL,
                    grant_revision INTEGER NOT NULL,
                    catalogue_revision INTEGER NOT NULL,
                    source_json TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('link', 'embed')),
                    result_json TEXT NOT NULL,
                    consumed_at REAL,
                    created_at REAL NOT NULL,
                    UNIQUE(caller_account_id, workspace_id, operation_id)
                );
                CREATE INDEX IF NOT EXISTS treehouse_attachment_preparations_target
                    ON treehouse_attachment_preparations(caller_account_id, workspace_id, course_id, lesson_id);
                CREATE TABLE IF NOT EXISTS treehouse_activity_receipts (
                    account_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    event_family TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('R','U')),
                    result TEXT NOT NULL,
                    actor_kind TEXT NOT NULL,
                    workspace_id TEXT,
                    occurred_at TEXT NOT NULL,
                    facts_json TEXT NOT NULL,
                    source_hash TEXT,
                    evidence_digest TEXT NOT NULL,
                    via TEXT NOT NULL,
                    ingested_at REAL NOT NULL,
                    PRIMARY KEY (account_id, source_event_id)
                );
                CREATE INDEX IF NOT EXISTS treehouse_activity_receipts_family
                    ON treehouse_activity_receipts(account_id, event_family);
                CREATE TABLE IF NOT EXISTS treehouse_achievement_awards (
                    account_id TEXT NOT NULL,
                    achievement_id TEXT NOT NULL,
                    predicate_version TEXT NOT NULL,
                    catalog_revision TEXT NOT NULL,
                    earned_at TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    awarded_via TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (account_id, achievement_id)
                );
                CREATE TABLE IF NOT EXISTS treehouse_achievement_predicate_state (
                    account_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    predicate_version TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS treehouse_achievement_backfill_cursors (
                    account_id TEXT NOT NULL,
                    source_family TEXT NOT NULL,
                    cursor_json TEXT NOT NULL,
                    predicate_version TEXT NOT NULL,
                    catalog_revision TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0,
                    completed_at TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (account_id, source_family)
                );
                CREATE TABLE IF NOT EXISTS treehouse_achievement_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    achievement_id TEXT NOT NULL,
                    batch_id TEXT,
                    state TEXT NOT NULL CHECK(state IN ('pending','delivered','failed')),
                    created_at REAL NOT NULL,
                    delivered_at REAL,
                    UNIQUE(account_id, achievement_id)
                );
                CREATE INDEX IF NOT EXISTS treehouse_achievement_outbox_pending
                    ON treehouse_achievement_outbox(account_id, state);
                """
            )
            try:
                db.execute("ALTER TABLE treehouse_grants ADD COLUMN grant_group_id TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _ref(owner_account_id: str, workspace_id: str, course_id: str) -> dict[str, str]:
        return {"ownerAccountId": owner_account_id, "workspaceId": workspace_id, "courseId": course_id}

    def get_catalogue(self, owner_account_id: str, workspace_id: str) -> tuple[dict[str, Any] | None, int]:
        with self._connect() as db:
            row = db.execute(
                "SELECT state_json, revision FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?",
                (owner_account_id, workspace_id),
            ).fetchone()
        if row is None:
            return None, 0
        return json.loads(row["state_json"]), int(row["revision"])

    def create_catalogue_if_absent(self, owner_account_id: str, workspace_id: str, state: dict[str, Any]) -> tuple[dict[str, Any], int, bool]:
        """Insert a first catalogue once and return the committed winner.

        First-open requests use this method instead of a read-then-write
        sequence, so a concurrent initializer cannot replace an authored
        catalogue after it wins the race.
        """
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        revision = int(state.get("revision") or 0)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state_json,revision FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?", (owner_account_id, workspace_id)).fetchone()
            if row is not None:
                db.commit()
                return json.loads(row["state_json"]), int(row["revision"]), False
            db.execute("INSERT INTO treehouse_catalogues(owner_account_id,workspace_id,state_json,revision,updated_at) VALUES(?,?,?,?,?)", (owner_account_id, workspace_id, payload, revision, time.time()))
            db.commit()
        return copy.deepcopy(state), revision, True

    def put_catalogue(self, owner_account_id: str, workspace_id: str, state: dict[str, Any], *, expected_revision: int | None, access_grant_id: str | None = None, access_revision: int | None = None) -> int:
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        revision = int(state.get("revision") or 0)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if access_grant_id:
                grant = db.execute("SELECT revision,revoked_at,accepted_at FROM treehouse_grants WHERE grant_id=? AND owner_account_id=? AND workspace_id=?", (access_grant_id, owner_account_id, workspace_id)).fetchone()
                if not grant or grant["revoked_at"] or not grant["accepted_at"] or (access_revision is not None and int(grant["revision"]) != int(access_revision)):
                    db.rollback()
                    raise TreeHouseRepositoryError("stale_access", "Course access changed while the write was in flight", details={"grantId": access_grant_id})
            row = db.execute(
                "SELECT revision FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?",
                (owner_account_id, workspace_id),
            ).fetchone()
            current = int(row[0]) if row else 0
            if expected_revision is not None and current != expected_revision:
                db.rollback()
                raise TreeHouseRepositoryError("stale", "TreeHouse changed in another tab", details={"revision": current})
            if row is None:
                db.execute(
                    "INSERT INTO treehouse_catalogues(owner_account_id,workspace_id,state_json,revision,updated_at) VALUES(?,?,?,?,?)",
                    (owner_account_id, workspace_id, payload, revision, time.time()),
                )
            else:
                db.execute(
                    "UPDATE treehouse_catalogues SET state_json=?,revision=?,updated_at=? WHERE owner_account_id=? AND workspace_id=? AND revision=?",
                    (payload, revision, time.time(), owner_account_id, workspace_id, current),
                )
            db.commit()
        return revision

    def owner_for_course(self, caller_account_id: str, workspace_id: str, course_id: str) -> str | None:
        with self._connect() as db:
            own = db.execute("SELECT state_json FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?", (caller_account_id, workspace_id)).fetchone()
            if own:
                own_course = (json.loads(own[0]).get("courses") or {}).get(course_id)
                if own_course and not own_course.get("deletedAt"):
                    return caller_account_id
            row = db.execute(
                "SELECT owner_account_id FROM treehouse_grants WHERE recipient_account_id=? AND workspace_id=? AND course_id=? AND accepted_at IS NOT NULL AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
                (caller_account_id, workspace_id, course_id),
            ).fetchone()
        return str(row[0]) if row else None

    def owner_for_entity(self, caller_account_id: str, workspace_id: str, entity_id: str) -> tuple[str, str] | None:
        """Resolve an activity/assignment/module/course through visible refs."""
        for ref in self.accessible_course_refs(caller_account_id, workspace_id):
            state, _ = self.get_catalogue(ref["ownerAccountId"], workspace_id)
            if not state:
                continue
            for kind in ("courses", "modules", "activities", "assignments"):
                entity = state.get(kind, {}).get(entity_id)
                if entity:
                    course_id = entity.get("courseId")
                    if not course_id and kind == "modules":
                        course_id = entity.get("courseId")
                    if course_id and self.access(caller_account_id, workspace_id, ref["ownerAccountId"], str(course_id), "learn"):
                        return ref["ownerAccountId"], str(course_id)
        return None

    def accessible_course_refs(self, caller_account_id: str, workspace_id: str) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        with self._connect() as db:
            own = db.execute("SELECT owner_account_id,state_json FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?", (caller_account_id, workspace_id)).fetchone()
            if own:
                state = json.loads(own["state_json"])
                refs.extend(self._ref(caller_account_id, workspace_id, course_id) for course_id, course in state.get("courses", {}).items() if not course.get("deletedAt"))
            rows = db.execute(
                "SELECT owner_account_id,course_id FROM treehouse_grants WHERE recipient_account_id=? AND workspace_id=? AND accepted_at IS NOT NULL AND revoked_at IS NULL",
                (caller_account_id, workspace_id),
            ).fetchall()
        refs.extend(self._ref(str(row[0]), workspace_id, str(row[1])) for row in rows)
        return list({(item["ownerAccountId"], item["courseId"]): item for item in refs}.values())

    def access(self, caller_account_id: str, workspace_id: str, owner_account_id: str, course_id: str, capability: str = "learn") -> bool:
        with self._connect() as db:
            course = db.execute("SELECT state_json FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?", (owner_account_id, workspace_id)).fetchone()
        if not course:
            return False
        course_record = (json.loads(course[0]).get("courses") or {}).get(course_id)
        if not course_record or course_record.get("deletedAt"):
            return False
        if caller_account_id == owner_account_id:
            return True
        wanted = "edit" if capability == "edit" else "learn"
        with self._connect() as db:
            row = db.execute(
                "SELECT role FROM treehouse_grants WHERE owner_account_id=? AND workspace_id=? AND course_id=? AND recipient_account_id=? AND accepted_at IS NOT NULL AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
                (owner_account_id, workspace_id, course_id, caller_account_id),
            ).fetchone()
        return bool(row and (row[0] == "edit" or row[0] == wanted))

    def grant_for_token(self, workspace_id: str, token: str) -> dict[str, Any] | None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            row = db.execute("SELECT * FROM treehouse_grants WHERE token_hash=? AND workspace_id=?", (token_hash, workspace_id)).fetchone()
        return dict(row) if row else None

    def grant(self, grant_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM treehouse_grants WHERE grant_id=?", (grant_id,)).fetchone()
        return dict(row) if row else None

    def active_grant(self, recipient_account_id: str, workspace_id: str, owner_account_id: str, course_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM treehouse_grants WHERE recipient_account_id=? AND workspace_id=? AND owner_account_id=? AND course_id=? AND accepted_at IS NOT NULL AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1", (recipient_account_id, workspace_id, owner_account_id, course_id)).fetchone()
        return dict(row) if row else None

    def prepare_lesson_attachment(
        self, *, caller_account_id: str, owner_account_id: str, workspace_id: str,
        course_id: str, lesson_id: str, operation_id: str, grant_revision: int,
        catalogue_revision: int, policy_generation: int = 0, source: dict[str, Any], source_digest: str, mode: str,
    ) -> dict[str, Any]:
        """Reserve one immutable lesson preparation after current grant/CAS checks."""
        digest = self._digest({
            "caller": caller_account_id, "owner": owner_account_id, "workspace": workspace_id,
            "course": course_id, "lesson": lesson_id, "grantRevision": int(grant_revision),
            "catalogueRevision": int(catalogue_revision), "source": source,
            "sourceDigest": source_digest, "mode": mode,
        })
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT * FROM treehouse_attachment_preparations WHERE caller_account_id=? AND workspace_id=? AND operation_id=?",
                (caller_account_id, workspace_id, operation_id),
            ).fetchone()
            if prior:
                if str(prior["source_digest"]) != source_digest or str(prior["result_json"] or "") == "":
                    db.rollback()
                    raise TreeHouseRepositoryError("idempotency_conflict", "Attachment operation was reused with different source", status=409)
                db.rollback()
                return json.loads(prior["result_json"])
            catalogue = db.execute(
                "SELECT state_json,revision FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?",
                (owner_account_id, workspace_id),
            ).fetchone()
            if not catalogue or int(catalogue["revision"]) != int(catalogue_revision):
                db.rollback()
                raise TreeHouseRepositoryError("stale", "TreeHouse catalogue changed while preparing the lesson", details={"revision": int(catalogue["revision"]) if catalogue else 0})
            state = json.loads(catalogue["state_json"])
            course = (state.get("courses") or {}).get(course_id)
            activity = (state.get("activities") or {}).get(lesson_id)
            if not isinstance(course, dict) or course.get("deletedAt") or not isinstance(activity, dict) or activity.get("deletedAt") or str(activity.get("courseId") or "") != course_id:
                db.rollback()
                raise TreeHouseRepositoryError("lesson_not_found", "TreeHouse lesson is unavailable", status=404)
            grant_id = None
            if caller_account_id != owner_account_id:
                grant = db.execute(
                    "SELECT grant_id,role,revision FROM treehouse_grants WHERE owner_account_id=? AND workspace_id=? AND course_id=? AND recipient_account_id=? AND accepted_at IS NOT NULL AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
                    (owner_account_id, workspace_id, course_id, caller_account_id),
                ).fetchone()
                if not grant or grant["role"] != "edit" or int(grant["revision"]) != int(grant_revision):
                    db.rollback()
                    raise TreeHouseRepositoryError("stale_access", "Lesson editor access changed while preparing", details={"revision": int(grant["revision"]) if grant else None})
                grant_id = grant["grant_id"]
            elif int(grant_revision) != 0:
                db.rollback()
                raise TreeHouseRepositoryError("stale_access", "Owner lesson grant revision is invalid")
            preparation_id = "treehouse-prep:" + secrets.token_urlsafe(18)
            result = {"preparationReceiptId": preparation_id, "operationId": operation_id, "courseId": course_id, "lessonId": lesson_id, "workspaceId": workspace_id, "policyGeneration": int(policy_generation), "grantRevision": int(grant_revision), "catalogueRevision": int(catalogue_revision), "source": copy.deepcopy(source), "sourceDigest": source_digest, "mode": mode}
            db.execute(
                "INSERT INTO treehouse_attachment_preparations(preparation_id,operation_id,caller_account_id,owner_account_id,workspace_id,course_id,lesson_id,policy_generation,grant_revision,catalogue_revision,source_json,source_digest,mode,result_json,consumed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (preparation_id, operation_id, caller_account_id, owner_account_id, workspace_id, course_id, lesson_id, int(policy_generation), int(grant_revision), int(catalogue_revision), json.dumps(source, ensure_ascii=False, separators=(",", ":")), source_digest, mode, json.dumps(result, ensure_ascii=False, separators=(",", ":")), None, time.time()),
            )
            db.commit()
            return result

    def lesson_attachment_preparation(self, *, caller_account_id: str, workspace_id: str, operation_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT result_json FROM treehouse_attachment_preparations WHERE caller_account_id=? AND workspace_id=? AND operation_id=?",
                (caller_account_id, workspace_id, operation_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def commit_lesson_attachment(
        self, *, caller_account_id: str, owner_account_id: str, workspace_id: str,
        course_id: str, lesson_id: str, operation_id: str, preparation_id: str,
        expected_catalogue_revision: int, expected_grant_revision: int, expected_policy_generation: int | None = None,
        state: dict[str, Any], mode: str,
    ) -> dict[str, Any]:
        """Consume a preparation and lesson CAS in one repository transaction."""
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prep = db.execute(
                "SELECT * FROM treehouse_attachment_preparations WHERE preparation_id=? AND caller_account_id=? AND workspace_id=? AND operation_id=?",
                (preparation_id, caller_account_id, workspace_id, operation_id),
            ).fetchone()
            if not prep or prep["owner_account_id"] != owner_account_id or prep["course_id"] != course_id or prep["lesson_id"] != lesson_id:
                db.rollback(); raise TreeHouseRepositoryError("resource_unavailable", "Lesson preparation receipt is unavailable", status=404)
            if prep["consumed_at"] is not None:
                db.rollback()
                return {"outcome": "replayed", "operationId": operation_id, "preparationReceiptId": preparation_id}
            if int(prep["catalogue_revision"]) != int(expected_catalogue_revision) or int(prep["grant_revision"]) != int(expected_grant_revision):
                db.rollback(); raise TreeHouseRepositoryError("stale", "Lesson access or catalogue changed after preparation", details={"revision": int(prep["catalogue_revision"])})
            if expected_policy_generation is not None and int(prep["policy_generation"]) != int(expected_policy_generation):
                db.rollback(); raise TreeHouseRepositoryError("policy_generation_changed", "File policy changed after lesson preparation", details={"generation": int(prep["policy_generation"])})
            row = db.execute("SELECT revision,state_json FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?", (owner_account_id, workspace_id)).fetchone()
            if not row or int(row["revision"]) != int(expected_catalogue_revision):
                db.rollback(); raise TreeHouseRepositoryError("stale", "TreeHouse changed before lesson attachment", details={"revision": int(row["revision"]) if row else 0})
            if caller_account_id != owner_account_id:
                grant = db.execute("SELECT role,revision FROM treehouse_grants WHERE owner_account_id=? AND workspace_id=? AND course_id=? AND recipient_account_id=? AND accepted_at IS NOT NULL AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1", (owner_account_id, workspace_id, course_id, caller_account_id)).fetchone()
                if not grant or grant["role"] != "edit" or int(grant["revision"]) != int(expected_grant_revision):
                    db.rollback(); raise TreeHouseRepositoryError("stale_access", "Lesson editor access was revoked", status=403)
            if str(prep["mode"]) != str(mode):
                db.rollback(); raise TreeHouseRepositoryError("idempotency_conflict", "Lesson attachment mode changed", status=409)
            next_state = copy.deepcopy(state)
            activity = (next_state.get("activities") or {}).get(lesson_id)
            if not isinstance(activity, dict) or str(activity.get("courseId") or "") != course_id:
                db.rollback(); raise TreeHouseRepositoryError("lesson_not_found", "TreeHouse lesson is unavailable", status=404)
            attachment = {"operationId": operation_id, "preparationReceiptId": preparation_id, "courseId": course_id, "lessonId": lesson_id, "mode": mode}
            next_state.setdefault("extensions", {}).setdefault("lessonAttachments", {})[operation_id] = attachment
            activity = copy.deepcopy(activity)
            attachments = activity.setdefault("sourceAttachments", [])
            if not any(isinstance(item, dict) and item.get("operationId") == operation_id for item in attachments):
                attachments.append(attachment)
            next_state["activities"][lesson_id] = activity
            next_state["revision"] = int(expected_catalogue_revision) + 1
            encoded = json.dumps(next_state, ensure_ascii=False, separators=(",", ":"))
            db.execute("UPDATE treehouse_catalogues SET state_json=?,revision=?,updated_at=? WHERE owner_account_id=? AND workspace_id=? AND revision=?", (encoded, int(next_state["revision"]), time.time(), owner_account_id, workspace_id, int(expected_catalogue_revision)))
            db.execute("UPDATE treehouse_attachment_preparations SET consumed_at=? WHERE preparation_id=?", (time.time(), preparation_id))
            db.commit()
            return {"outcome": "committed", "operationId": operation_id, "preparationReceiptId": preparation_id, "revision": int(next_state["revision"])}

    def create_share(self, *, owner_account_id: str, workspace_id: str, course_id: str, recipient_account_id: str, role: str, access_revision: int, now: str, command_id: str, payload: dict[str, Any], state: dict[str, Any] | None = None, expected_catalogue_revision: int | None = None) -> dict[str, Any]:
        if role not in {"learn", "edit"}:
            raise TreeHouseRepositoryError("invalid_capability", "Share capability must be learn or edit", status=400)
        digest = self._digest(payload)
        token = secrets.token_urlsafe(32)
        grant_id = secrets.token_urlsafe(18)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            idem = db.execute("SELECT payload_digest,result_json FROM treehouse_idempotency WHERE principal_account_id=? AND workspace_id=? AND command_id=?", (owner_account_id, workspace_id, command_id)).fetchone()
            if idem:
                if idem[0] != digest:
                    db.rollback()
                    raise TreeHouseRepositoryError("idempotency_conflict", "Command id was already used with different payload", status=409)
                db.rollback()
                return {**json.loads(idem[1]), "replayed": True}
            catalogue = db.execute("SELECT revision FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?", (owner_account_id, workspace_id)).fetchone()
            current_catalogue_revision = int(catalogue[0]) if catalogue else 0
            if state is not None and expected_catalogue_revision is not None and current_catalogue_revision != expected_catalogue_revision:
                db.rollback()
                raise TreeHouseRepositoryError("stale", "TreeHouse changed in another tab", details={"revision": current_catalogue_revision})
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            db.execute("INSERT INTO treehouse_grants(grant_id,owner_account_id,workspace_id,course_id,recipient_account_id,role,access_revision,token_hash,accepted_at,revoked_at,revision,created_at,grant_group_id) VALUES(?,?,?,?,?,?,?,?,NULL,NULL,0,?,?)", (grant_id, owner_account_id, workspace_id, course_id, recipient_account_id, role, access_revision, token_hash, time.time(), grant_id))
            db.execute("INSERT INTO treehouse_recipient_index(recipient_account_id,workspace_id,grant_id,owner_account_id,course_id,access_revision,active) VALUES(?,?,?,?,?,?,0)", (recipient_account_id, workspace_id, grant_id, owner_account_id, course_id, access_revision))
            result = {"grantId": grant_id, "shareToken": token, "courseRef": self._ref(owner_account_id, workspace_id, course_id), "role": role, "accessRevision": access_revision}
            if state is not None:
                old_key = None
                for key, grant in (state.get("courseGrants") or {}).items():
                    if grant.get("courseId") == course_id and grant.get("recipientId") == recipient_account_id and not grant.get("revokedAt"):
                        old_key = key
                        grant.update({"id": grant_id, "grantId": grant_id, "shareToken": token, "tokenHash": token_hash, "accessRevision": access_revision})
                        if old_key != grant_id:
                            state["courseGrants"].pop(old_key, None)
                            state["courseGrants"][grant_id] = grant
                        break
                processed = (state.get("processedCommands") or {}).get(command_id)
                if isinstance(processed, dict) and isinstance(processed.get("result"), dict):
                    processed["result"].update(result)
                encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
                if catalogue:
                    db.execute("UPDATE treehouse_catalogues SET state_json=?,revision=?,updated_at=? WHERE owner_account_id=? AND workspace_id=? AND revision=?", (encoded, int(state.get("revision") or 0), time.time(), owner_account_id, workspace_id, current_catalogue_revision))
                else:
                    db.execute("INSERT INTO treehouse_catalogues(owner_account_id,workspace_id,state_json,revision,updated_at) VALUES(?,?,?,?,?)", (owner_account_id, workspace_id, encoded, int(state.get("revision") or 0), time.time()))
            db.execute("INSERT INTO treehouse_idempotency VALUES(?,?,?,?,?,?)", (owner_account_id, workspace_id, command_id, digest, json.dumps(result), time.time()))
            db.commit()
        return result

    def accept_share(self, *, recipient_account_id: str, workspace_id: str, token: str, now: str) -> dict[str, Any]:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM treehouse_grants WHERE token_hash=? AND workspace_id=?", (token_hash, workspace_id)).fetchone()
            if row is None or row["recipient_account_id"] != recipient_account_id:
                db.rollback()
                raise TreeHouseRepositoryError("share_not_found", "Share link is invalid", status=404)
            if row["revoked_at"]:
                db.rollback()
                raise TreeHouseRepositoryError("share_revoked", "Share link is no longer active", status=409)
            if row["accepted_at"] is None:
                db.execute("UPDATE treehouse_grants SET accepted_at=?,revision=revision+1 WHERE grant_id=? AND revision=?", (now, row["grant_id"], row["revision"]))
                db.execute("UPDATE treehouse_recipient_index SET active=1 WHERE grant_id=?", (row["grant_id"],))
                # A Field Guide course can depend on earlier courses. Sharing
                # one lesson therefore accepts its prerequisite closure in the
                # same transaction, so enrollment never needs an opaque
                # second grant or a manually copied account-qualified ID.
                owner_catalogue = db.execute(
                    "SELECT state_json FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?",
                    (row["owner_account_id"], workspace_id),
                ).fetchone()
                courses = (json.loads(owner_catalogue[0]).get("courses") or {}) if owner_catalogue else {}
                pending = [str(row["course_id"])]
                closure: set[str] = set()
                while pending:
                    course_id = pending.pop()
                    if course_id in closure:
                        continue
                    closure.add(course_id)
                    pending.extend(str(item) for item in (courses.get(course_id) or {}).get("prerequisites", []))
                for prerequisite_id in sorted(closure - {str(row["course_id"])}):
                    existing = db.execute(
                        "SELECT grant_id,accepted_at,revoked_at FROM treehouse_grants WHERE owner_account_id=? AND workspace_id=? AND course_id=? AND recipient_account_id=? ORDER BY created_at DESC LIMIT 1",
                        (row["owner_account_id"], workspace_id, prerequisite_id, recipient_account_id),
                    ).fetchone()
                    if existing:
                        if existing["accepted_at"] and not existing["revoked_at"]:
                            continue
                        # A prior explicit revocation remains authoritative.
                        continue
                    closure_grant_id = secrets.token_urlsafe(18)
                    closure_token_hash = hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()
                    db.execute(
                        "INSERT INTO treehouse_grants(grant_id,owner_account_id,workspace_id,course_id,recipient_account_id,role,access_revision,token_hash,accepted_at,revoked_at,revision,created_at,grant_group_id) VALUES(?,?,?,?,?,?,?, ?,?,?,1,?,?)",
                        (closure_grant_id, row["owner_account_id"], workspace_id, prerequisite_id, recipient_account_id, "learn", int(row["access_revision"]), closure_token_hash, now, None, time.time(), row["grant_group_id"] or row["grant_id"]),
                    )
                    db.execute(
                        "INSERT INTO treehouse_recipient_index(recipient_account_id,workspace_id,grant_id,owner_account_id,course_id,access_revision,active) VALUES(?,?,?,?,?,?,1)",
                        (recipient_account_id, workspace_id, closure_grant_id, row["owner_account_id"], prerequisite_id, int(row["access_revision"])),
                    )
            db.commit()
        return {"grantId": row["grant_id"], "courseRef": self._ref(row["owner_account_id"], row["workspace_id"], row["course_id"]), "role": row["role"], "alreadyAccepted": row["accepted_at"] is not None}

    def revoke_share(self, *, owner_account_id: str, workspace_id: str, grant_id: str, expected_revision: int | None, now: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM treehouse_grants WHERE grant_id=?", (grant_id,)).fetchone()
            if row is None or row["owner_account_id"] != owner_account_id or row["workspace_id"] != workspace_id:
                db.rollback()
                raise TreeHouseRepositoryError("share_not_found", "Share grant not found", status=404)
            if expected_revision is not None and int(row["revision"]) != expected_revision:
                db.rollback()
                raise TreeHouseRepositoryError("stale", "Share grant changed", details={"revision": row["revision"]})
            if row["revoked_at"]:
                db.rollback()
                return {"grantId": grant_id, "alreadyRevoked": True, "revision": row["revision"]}
            revision = int(row["revision"]) + 1
            group_id = row["grant_group_id"] or grant_id
            db.execute("UPDATE treehouse_grants SET revoked_at=?,revision=revision+1 WHERE grant_group_id=? AND revoked_at IS NULL", (now, group_id))
            db.execute("UPDATE treehouse_recipient_index SET active=0 WHERE grant_id IN (SELECT grant_id FROM treehouse_grants WHERE grant_group_id=?)", (group_id,))
            db.commit()
        return {"grantId": grant_id, "revoked": True, "revision": revision}

    def revoke_share_with_catalogue(self, *, owner_account_id: str, workspace_id: str, grant_id: str, expected_grant_revision: int | None, expected_catalogue_revision: int, state: dict[str, Any], now: str) -> dict[str, Any]:
        """Atomically revoke a grant and commit its durable catalogue event."""
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        catalogue_revision = int(state.get("revision") or 0)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            grant = db.execute("SELECT * FROM treehouse_grants WHERE grant_id=?", (grant_id,)).fetchone()
            catalogue = db.execute("SELECT revision FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?", (owner_account_id, workspace_id)).fetchone()
            if not grant or grant["owner_account_id"] != owner_account_id or grant["workspace_id"] != workspace_id:
                db.rollback(); raise TreeHouseRepositoryError("share_not_found", "Share grant not found", status=404)
            if expected_grant_revision is not None and int(grant["revision"]) != int(expected_grant_revision):
                db.rollback(); raise TreeHouseRepositoryError("stale", "Share grant changed", details={"revision": grant["revision"]})
            if not catalogue or int(catalogue[0]) != int(expected_catalogue_revision):
                db.rollback(); raise TreeHouseRepositoryError("stale", "TreeHouse changed in another tab", details={"revision": catalogue[0] if catalogue else 0})
            if grant["revoked_at"]:
                db.rollback(); return {"grantId": grant_id, "alreadyRevoked": True, "revision": grant["revision"]}
            revision = int(grant["revision"]) + 1
            group_id = grant["grant_group_id"] or grant_id
            db.execute("UPDATE treehouse_grants SET revoked_at=?,revision=revision+1 WHERE grant_group_id=? AND revoked_at IS NULL", (now, group_id)); db.execute("UPDATE treehouse_recipient_index SET active=0 WHERE grant_id IN (SELECT grant_id FROM treehouse_grants WHERE grant_group_id=?)", (group_id,))
            db.execute("UPDATE treehouse_catalogues SET state_json=?,revision=?,updated_at=? WHERE owner_account_id=? AND workspace_id=? AND revision=?", (payload, catalogue_revision, time.time(), owner_account_id, workspace_id, expected_catalogue_revision))
            db.commit()
        return {"grantId": grant_id, "revoked": True, "revision": revision, "catalogueRevision": catalogue_revision}

    def get_progress(self, learner_account_id: str, owner_account_id: str, workspace_id: str, course_id: str) -> tuple[dict[str, Any] | None, int]:
        with self._connect() as db:
            row = db.execute("SELECT state_json,revision FROM treehouse_progress WHERE learner_account_id=? AND owner_account_id=? AND workspace_id=? AND course_id=?", (learner_account_id, owner_account_id, workspace_id, course_id)).fetchone()
        return (json.loads(row[0]), int(row[1])) if row else (None, 0)

    def progress_reset_epoch(self, learner_account_id: str, owner_account_id: str, workspace_id: str, course_id: str) -> int:
        """Return the committed reset generation for one learner/course."""
        with self._connect() as db:
            row = db.execute(
                "SELECT reset_epoch FROM treehouse_progress WHERE learner_account_id=? AND owner_account_id=? AND workspace_id=? AND course_id=?",
                (learner_account_id, owner_account_id, workspace_id, course_id),
            ).fetchone()
            global_row = db.execute(
                "SELECT generation FROM treehouse_progress_reset_generations WHERE learner_account_id=? AND workspace_id=?",
                (learner_account_id, workspace_id),
            ).fetchone()
        return max(int(row[0]) if row else 0, int(global_row[0]) if global_row else 0)

    def progress_reset_generation(self, learner_account_id: str, workspace_id: str) -> int:
        """Return the workspace-wide generation fencing every learner row."""
        with self._connect() as db:
            row = db.execute(
                "SELECT generation FROM treehouse_progress_reset_generations WHERE learner_account_id=? AND workspace_id=?",
                (learner_account_id, workspace_id),
            ).fetchone()
        return int(row[0]) if row else 0

    def put_progress(
        self,
        learner_account_id: str,
        owner_account_id: str,
        workspace_id: str,
        course_id: str,
        state: dict[str, Any],
        *,
        expected_revision: int | None,
        reset_epoch: int = 0,
        expected_reset_epoch: int | None = None,
        grant_id: str | None = None,
        access_revision: int | None = None,
    ) -> int:
        """Commit learner state behind one guarded SQLite transaction.

        A recipient write carries the exact active grant revision captured when
        the command began.  Reset and completion writes also carry the stored
        reset epoch.  Both checks happen after ``BEGIN IMMEDIATE`` and before
        the row update, so a revoke/reset that wins the transaction cannot be
        followed by a stale in-flight write.
        """
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if grant_id:
                grant = db.execute(
                    "SELECT recipient_account_id,owner_account_id,workspace_id,course_id,revision,accepted_at,revoked_at "
                    "FROM treehouse_grants WHERE grant_id=?",
                    (grant_id,),
                ).fetchone()
                if (
                    not grant
                    or grant["recipient_account_id"] != learner_account_id
                    or grant["owner_account_id"] != owner_account_id
                    or grant["workspace_id"] != workspace_id
                    or grant["course_id"] != course_id
                    or not grant["accepted_at"]
                    or grant["revoked_at"]
                    or access_revision is None
                    or int(grant["revision"]) != int(access_revision)
                ):
                    db.rollback()
                    raise TreeHouseRepositoryError(
                        "stale_access",
                        "Course access changed while the write was in flight",
                        status=409,
                        details={"grantId": grant_id},
                    )
            elif access_revision is not None:
                db.rollback()
                raise TreeHouseRepositoryError("invalid_access_guard", "An access revision requires a grant", status=400)
            row = db.execute("SELECT revision FROM treehouse_progress WHERE learner_account_id=? AND owner_account_id=? AND workspace_id=? AND course_id=?", (learner_account_id, owner_account_id, workspace_id, course_id)).fetchone()
            current = int(row[0]) if row else 0
            if expected_revision is not None and current != expected_revision:
                db.rollback()
                raise TreeHouseRepositoryError("stale", "Learner progress changed", details={"revision": current})
            current_epoch_row = db.execute(
                "SELECT reset_epoch FROM treehouse_progress WHERE learner_account_id=? AND owner_account_id=? AND workspace_id=? AND course_id=?",
                (learner_account_id, owner_account_id, workspace_id, course_id),
            ).fetchone()
            global_epoch_row = db.execute(
                "SELECT generation FROM treehouse_progress_reset_generations WHERE learner_account_id=? AND workspace_id=?",
                (learner_account_id, workspace_id),
            ).fetchone()
            current_epoch = max(int(current_epoch_row[0]) if current_epoch_row else 0, int(global_epoch_row[0]) if global_epoch_row else 0)
            if expected_reset_epoch is not None and current_epoch != int(expected_reset_epoch):
                db.rollback()
                raise TreeHouseRepositoryError(
                    "stale_attempt",
                    "TreeHouse progress was reset while this write was in flight",
                    status=409,
                    details={"resetEpoch": current_epoch},
                )
            if int(reset_epoch) < current_epoch:
                db.rollback()
                raise TreeHouseRepositoryError(
                    "stale_attempt",
                    "A stale TreeHouse attempt cannot restore cleared progress",
                    status=409,
                    details={"resetEpoch": current_epoch},
                )
            # This is a storage CAS revision, deliberately independent of the
            # owner's catalogue/domain revision.  Shared learners overlay a
            # published course snapshot, so using that snapshot's revision
            # would let two concurrent writes compare equal and both commit.
            revision = current + 1
            values = (payload, revision, reset_epoch, time.time(), learner_account_id, owner_account_id, workspace_id, course_id)
            if row:
                db.execute("UPDATE treehouse_progress SET state_json=?,revision=?,reset_epoch=?,updated_at=? WHERE learner_account_id=? AND owner_account_id=? AND workspace_id=? AND course_id=?", values)
            else:
                db.execute("INSERT INTO treehouse_progress(state_json,revision,reset_epoch,updated_at,learner_account_id,owner_account_id,workspace_id,course_id) VALUES(?,?,?,?,?,?,?,?)", values)
            db.commit()
        return revision

    def reset_progress_all(
        self,
        learner_account_id: str,
        workspace_id: str,
        rows: list[dict[str, Any]],
        *,
        command_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit one learner-wide reset as a single guarded transaction.

        ``rows`` contains the already-evaluated, learner-only projections for
        every visible course.  We validate every progress revision, reset
        generation and active grant while holding ``BEGIN IMMEDIATE`` before
        touching any row.  A stale completion/revoke therefore rolls the
        complete reset back instead of leaving a partially reset learner.
        """
        digest = self._digest(payload)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            idem = db.execute(
                "SELECT payload_digest,result_json FROM treehouse_idempotency WHERE principal_account_id=? AND workspace_id=? AND command_id=?",
                (learner_account_id, workspace_id, command_id),
            ).fetchone()
            if idem:
                if idem[0] != digest:
                    db.rollback()
                    raise TreeHouseRepositoryError("idempotency_conflict", "Command id was already used with different payload", status=409)
                db.rollback()
                return {**json.loads(idem[1]), "replayed": True}

            generation_row = db.execute(
                "SELECT generation FROM treehouse_progress_reset_generations WHERE learner_account_id=? AND workspace_id=?",
                (learner_account_id, workspace_id),
            ).fetchone()
            generation = (int(generation_row[0]) if generation_row else 0) + 1
            provided = {(str(item["owner_account_id"]), str(item["course_id"])): item for item in rows}
            for item in rows:
                owner_id = str(item["owner_account_id"])
                course_id = str(item["course_id"])
                grant_id = item.get("grant_id")
                if grant_id:
                    grant = db.execute(
                        "SELECT recipient_account_id,owner_account_id,workspace_id,course_id,revision,accepted_at,revoked_at FROM treehouse_grants WHERE grant_id=?",
                        (grant_id,),
                    ).fetchone()
                    if (
                        not grant
                        or grant["recipient_account_id"] != learner_account_id
                        or grant["owner_account_id"] != owner_id
                        or grant["workspace_id"] != workspace_id
                        or grant["course_id"] != course_id
                        or not grant["accepted_at"]
                        or grant["revoked_at"]
                        or item.get("access_revision") is None
                        or int(grant["revision"]) != int(item["access_revision"])
                    ):
                        db.rollback()
                        raise TreeHouseRepositoryError("stale_access", "Course access changed while the reset was in flight", details={"grantId": grant_id})
                current_row = db.execute(
                    "SELECT revision,reset_epoch FROM treehouse_progress WHERE learner_account_id=? AND owner_account_id=? AND workspace_id=? AND course_id=?",
                    (learner_account_id, owner_id, workspace_id, course_id),
                ).fetchone()
                current_revision = int(current_row[0]) if current_row else 0
                current_epoch = max(int(current_row[1]) if current_row else 0, int(generation_row[0]) if generation_row else 0)
                if current_revision != int(item.get("expected_revision", 0)) or current_epoch != int(item.get("expected_reset_epoch", 0)):
                    db.rollback()
                    raise TreeHouseRepositoryError("stale_attempt", "TreeHouse progress changed while the reset was in flight", status=409, details={"ownerAccountId": owner_id, "courseId": course_id, "revision": current_revision, "resetEpoch": current_epoch})
                next_epoch = max(int(item["reset_epoch"]), generation)
                if next_epoch < current_epoch:
                    db.rollback()
                    raise TreeHouseRepositoryError("stale_attempt", "A stale reset cannot move the learner generation backwards", status=409, details={"resetEpoch": current_epoch})

            results: list[dict[str, Any]] = []
            stored_rows = db.execute(
                "SELECT owner_account_id,course_id,state_json,revision,reset_epoch FROM treehouse_progress WHERE learner_account_id=? AND workspace_id=?",
                (learner_account_id, workspace_id),
            ).fetchall()
            for stored in stored_rows:
                key = (str(stored["owner_account_id"]), str(stored["course_id"]))
                if key in provided:
                    continue
                try:
                    old_state = json.loads(stored["state_json"])
                except (TypeError, json.JSONDecodeError):
                    old_state = {}
                profile = (old_state.get("profiles") or {}).get(learner_account_id)
                cleared = {
                    "schemaVersion": old_state.get("schemaVersion", 1),
                    "revision": old_state.get("revision", 0),
                    "profiles": {learner_account_id: profile} if profile else {},
                    "enrollments": {}, "submissions": {}, "evidence": {}, "events": [],
                    "processedCommands": {}, "progressResets": {f"{learner_account_id}:*": generation},
                }
                revision = int(stored["revision"]) + 1
                db.execute(
                    "UPDATE treehouse_progress SET state_json=?,revision=?,reset_epoch=?,updated_at=? WHERE learner_account_id=? AND owner_account_id=? AND workspace_id=? AND course_id=? AND revision=?",
                    (json.dumps(cleared, ensure_ascii=False, separators=(",", ":")), revision, generation, time.time(), learner_account_id, key[0], workspace_id, key[1], int(stored["revision"])),
                )
            for item in rows:
                owner_id = str(item["owner_account_id"])
                course_id = str(item["course_id"])
                current_revision = int(item.get("expected_revision", 0))
                revision = current_revision + 1
                reset_state = copy.deepcopy(item["state"])
                reset_state.setdefault("progressResets", {})[f"{learner_account_id}:*"] = generation
                encoded = json.dumps(reset_state, ensure_ascii=False, separators=(",", ":"))
                values = (encoded, revision, generation, time.time(), learner_account_id, owner_id, workspace_id, course_id)
                if int(item.get("expected_revision", 0)):
                    db.execute(
                        "UPDATE treehouse_progress SET state_json=?,revision=?,reset_epoch=?,updated_at=? WHERE learner_account_id=? AND owner_account_id=? AND workspace_id=? AND course_id=? AND revision=?",
                        (*values, current_revision),
                    )
                else:
                    db.execute(
                        "INSERT INTO treehouse_progress(state_json,revision,reset_epoch,updated_at,learner_account_id,owner_account_id,workspace_id,course_id) VALUES(?,?,?,?,?,?,?,?)",
                        values,
                    )
                results.append({**item["result"], "generation": generation, "storageRevision": revision})
            db.execute(
                "INSERT INTO treehouse_progress_reset_generations(learner_account_id,workspace_id,generation,updated_at) VALUES(?,?,?,?) ON CONFLICT(learner_account_id,workspace_id) DO UPDATE SET generation=excluded.generation,updated_at=excluded.updated_at",
                (learner_account_id, workspace_id, generation, time.time()),
            )
            result = {"profileId": learner_account_id, "generation": generation, "courses": results}
            db.execute(
                "INSERT INTO treehouse_idempotency VALUES(?,?,?,?,?,?)",
                (learner_account_id, workspace_id, command_id, digest, json.dumps(result), time.time()),
            )
            db.commit()
        return result

    # ------------------------------------------------------------------
    # Account-wide built-in achievements (S29)
    #
    # These records live outside the 50,000-event course aggregate.  The
    # partition is the stable account identity; workspace is context only.
    # All writes use the same immediate-transaction/CAS discipline as the
    # catalogue so duplicate, out-of-order and backfill/live overlapping
    # deliveries never double-award.
    # ------------------------------------------------------------------

    def put_activity_receipt(self, account_id: str, event: Any, *, via: str = "live") -> dict[str, Any]:
        """Insert one structured activity receipt. Idempotent on source_event_id.

        Returns ``{"inserted": bool}`` — False means the receipt was already
        present (duplicate delivery).  The caller must not re-score duplicates.
        """
        facts = dict(getattr(event, "facts", None) or {})
        source_event_id = str(getattr(event, "source_event_id", "") or "")
        event_family = str(getattr(event, "event_family", "") or "")
        kind = str(getattr(event, "kind", "") or "")
        result = str(getattr(event, "result", "") or "")
        actor_kind = str(getattr(event, "actor_kind", "") or "")
        occurred_at = str(getattr(event, "occurred_at", "") or "")
        workspace_id = getattr(event, "workspace_id", None)
        schema_version = int(getattr(event, "schema_version", 1) or 1)
        source_hash = getattr(event, "source_hash", None)
        evidence_digest = self._digest({
            "sourceEventId": source_event_id,
            "eventFamily": event_family,
            "kind": kind,
            "result": result,
            "actorKind": actor_kind,
            "occurredAt": occurred_at,
            "facts": facts,
            "sourceHash": source_hash,
        })
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT source_event_id FROM treehouse_activity_receipts WHERE account_id=? AND source_event_id=?",
                (account_id, source_event_id),
            ).fetchone()
            if row is not None:
                db.commit()
                return {"inserted": False, "sourceEventId": source_event_id}
            db.execute(
                "INSERT INTO treehouse_activity_receipts("
                "account_id,source_event_id,schema_version,event_family,kind,result,actor_kind,"
                "workspace_id,occurred_at,facts_json,source_hash,evidence_digest,via,ingested_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account_id, source_event_id, schema_version, event_family, kind, result, actor_kind,
                    workspace_id, occurred_at,
                    json.dumps(facts, ensure_ascii=False, separators=(",", ":"), default=str),
                    source_hash, evidence_digest, via, time.time(),
                ),
            )
            db.commit()
        return {"inserted": True, "sourceEventId": source_event_id}

    def put_achievement_award(self, record: Any) -> dict[str, Any]:
        """Insert one achievement award. Idempotent on (account_id, achievement_id).

        Returns ``{"inserted": bool}`` — False means the account already holds
        this award.  A failed/pending insert never produces a second award.
        """
        account_id = str(getattr(record, "account_id", "") or "")
        achievement_id = str(getattr(record, "achievement_id", "") or "")
        predicate_version = str(getattr(record, "predicate_version", "") or "")
        catalog_revision = str(getattr(record, "catalog_revision", "") or "")
        earned_at = str(getattr(record, "earned_at", "") or "")
        evidence_refs = list(getattr(record, "evidence_refs", ()) or ())
        awarded_via = str(getattr(record, "awarded_via", "") or "")
        facts = dict(getattr(record, "facts", None) or {})
        evidence_payload: dict[str, Any] = {"evidenceRefs": evidence_refs}
        # Persist qualification facts (taskId, sessionId, etc.) so notification
        # payloads can surface S12 task identity for the "Open task" toast action.
        for key, value in facts.items():
            if value not in (None, "", [], {}):
                evidence_payload[key] = value
        evidence_json = json.dumps(evidence_payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT achievement_id FROM treehouse_achievement_awards WHERE account_id=? AND achievement_id=?",
                (account_id, achievement_id),
            ).fetchone()
            if row is not None:
                db.commit()
                return {"inserted": False, "achievementId": achievement_id}
            db.execute(
                "INSERT INTO treehouse_achievement_awards("
                "account_id,achievement_id,predicate_version,catalog_revision,earned_at,evidence_json,awarded_via,created_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (account_id, achievement_id, predicate_version, catalog_revision, earned_at, evidence_json, awarded_via, time.time()),
            )
            db.commit()
        return {"inserted": True, "achievementId": achievement_id}

    def earned_achievement_ids(self, account_id: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT achievement_id FROM treehouse_achievement_awards WHERE account_id=? ORDER BY achievement_id",
                (account_id,),
            ).fetchall()
        return [str(row["achievement_id"]) for row in rows]

    def get_achievement_award(self, account_id: str, achievement_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM treehouse_achievement_awards WHERE account_id=? AND achievement_id=?",
                (account_id, achievement_id),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        try:
            evidence = json.loads(payload.pop("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        payload["evidence"] = evidence
        return payload

    def get_predicate_state(self, account_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT state_json FROM treehouse_achievement_predicate_state WHERE account_id=?",
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def put_predicate_state(self, account_id: str, snapshot: dict[str, Any], *, predicate_version: str = "1") -> None:
        payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO treehouse_achievement_predicate_state(account_id,state_json,predicate_version,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET state_json=excluded.state_json,predicate_version=excluded.predicate_version,updated_at=excluded.updated_at",
                (account_id, payload, predicate_version, time.time()),
            )
            db.commit()

    def put_backfill_cursor(
        self,
        account_id: str,
        source_family: str,
        payload: dict[str, Any],
        *,
        predicate_version: str | None = None,
        catalog_revision: str | None = None,
        processed: int | None = None,
        completed: bool = False,
    ) -> None:
        """Persist a resumable per-source backfill cursor.

        Accepts the engine's payload shape ``{"cursor", "predicateVersion",
        "catalogRevision", "processed"}`` or explicit keyword overrides.
        """
        body = dict(payload or {})
        cursor_body = body.get("cursor") if isinstance(body.get("cursor"), dict) else body
        version = str(predicate_version if predicate_version is not None else body.get("predicateVersion") or body.get("predicate_version") or "1")
        revision = str(catalog_revision if catalog_revision is not None else body.get("catalogRevision") or body.get("catalog_revision") or "")
        count = int(processed if processed is not None else body.get("processed") or 0)
        encoded = json.dumps(cursor_body, ensure_ascii=False, separators=(",", ":"), default=str)
        completed_at = _now_iso() if completed else None
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO treehouse_achievement_backfill_cursors("
                "account_id,source_family,cursor_json,predicate_version,catalog_revision,processed,completed_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id,source_family) DO UPDATE SET "
                "cursor_json=excluded.cursor_json,predicate_version=excluded.predicate_version,"
                "catalog_revision=excluded.catalog_revision,processed=excluded.processed,"
                "completed_at=COALESCE(excluded.completed_at, treehouse_achievement_backfill_cursors.completed_at),"
                "updated_at=excluded.updated_at",
                (account_id, source_family, encoded, version, revision, count, completed_at, time.time()),
            )
            db.commit()

    def get_backfill_cursor(self, account_id: str, source_family: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM treehouse_achievement_backfill_cursors WHERE account_id=? AND source_family=?",
                (account_id, source_family),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        try:
            payload["cursor"] = json.loads(payload.pop("cursor_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload["cursor"] = {}
        return payload

    def enqueue_award_notification(
        self,
        account_id: str,
        achievement_id: str,
        *,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically insert the award notification outbox row.

        Unique on (account_id, achievement_id) so reconnects and tabs cannot
        re-deliver the same award.  A failed toast never erases the award.
        """
        outbox_id = f"award:{account_id}:{achievement_id}"
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT outbox_id,state FROM treehouse_achievement_outbox WHERE account_id=? AND achievement_id=?",
                (account_id, achievement_id),
            ).fetchone()
            if row is not None:
                db.commit()
                return {"outboxId": row["outbox_id"], "state": row["state"], "inserted": False}
            db.execute(
                "INSERT INTO treehouse_achievement_outbox(outbox_id,account_id,achievement_id,batch_id,state,created_at) VALUES(?,?,?,?,?,?)",
                (outbox_id, account_id, achievement_id, batch_id, "pending", time.time()),
            )
            db.commit()
        return {"outboxId": outbox_id, "state": "pending", "inserted": True}

    def list_pending_notifications(self, account_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT outbox_id,account_id,achievement_id,batch_id,state,created_at,delivered_at "
                "FROM treehouse_achievement_outbox WHERE account_id=? AND state='pending' ORDER BY created_at LIMIT ?",
                (account_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notification_delivered(self, outbox_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE treehouse_achievement_outbox SET state='delivered',delivered_at=? WHERE outbox_id=? AND state='pending'",
                (time.time(), outbox_id),
            )
            db.commit()
        return {"outboxId": outbox_id, "state": "delivered"}

    def mark_notification_failed(self, outbox_id: str) -> dict[str, Any]:
        """Record a toast failure without erasing the durable award."""
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE treehouse_achievement_outbox SET state='failed' WHERE outbox_id=? AND state='pending'",
                (outbox_id,),
            )
            db.commit()
        return {"outboxId": outbox_id, "state": "failed"}

    # -- account lifecycle (export / purge / restore / rename) ------------

    def export_achievement_ledger(self, account_id: str) -> dict[str, Any]:
        with self._connect() as db:
            awards = [dict(row) for row in db.execute(
                "SELECT * FROM treehouse_achievement_awards WHERE account_id=? ORDER BY achievement_id",
                (account_id,),
            ).fetchall()]
            receipts = [dict(row) for row in db.execute(
                "SELECT * FROM treehouse_activity_receipts WHERE account_id=? ORDER BY source_event_id",
                (account_id,),
            ).fetchall()]
            state_row = db.execute(
                "SELECT state_json,predicate_version FROM treehouse_achievement_predicate_state WHERE account_id=?",
                (account_id,),
            ).fetchone()
            cursors = [dict(row) for row in db.execute(
                "SELECT * FROM treehouse_achievement_backfill_cursors WHERE account_id=? ORDER BY source_family",
                (account_id,),
            ).fetchall()]
        return {
            "accountId": account_id,
            "awards": awards,
            "receipts": receipts,
            "predicateState": (json.loads(state_row["state_json"]) if state_row else None),
            "predicateVersion": (state_row["predicate_version"] if state_row else None),
            "backfillCursors": cursors,
        }

    def purge_achievement_ledger(self, account_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in (
                "treehouse_achievement_outbox",
                "treehouse_achievement_backfill_cursors",
                "treehouse_achievement_predicate_state",
                "treehouse_achievement_awards",
                "treehouse_activity_receipts",
            ):
                cur = db.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
                counts[table] = int(cur.rowcount or 0)
            db.commit()
        return counts

    def restore_achievement_ledger(self, account_id: str, payload: dict[str, Any]) -> dict[str, int]:
        """Restore a previously exported ledger. Awards stay unique per account."""
        counts = {"awards": 0, "receipts": 0, "cursors": 0, "predicateState": 0}
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in payload.get("awards") or []:
                cur = db.execute(
                    "INSERT OR IGNORE INTO treehouse_achievement_awards("
                    "account_id,achievement_id,predicate_version,catalog_revision,earned_at,evidence_json,awarded_via,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        account_id,
                        str(row.get("achievement_id") or row.get("achievementId") or ""),
                        str(row.get("predicate_version") or row.get("predicateVersion") or ""),
                        str(row.get("catalog_revision") or row.get("catalogRevision") or ""),
                        str(row.get("earned_at") or row.get("earnedAt") or ""),
                        row.get("evidence_json") if isinstance(row.get("evidence_json"), str) else json.dumps(row.get("evidence") or {}, ensure_ascii=False, separators=(",", ":")),
                        str(row.get("awarded_via") or row.get("awardedVia") or "restore"),
                        float(row.get("created_at") or time.time()),
                    ),
                )
                counts["awards"] += int(cur.rowcount or 0)
            for row in payload.get("receipts") or []:
                cur = db.execute(
                    "INSERT OR IGNORE INTO treehouse_activity_receipts("
                    "account_id,source_event_id,schema_version,event_family,kind,result,actor_kind,"
                    "workspace_id,occurred_at,facts_json,source_hash,evidence_digest,via,ingested_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        account_id,
                        str(row.get("source_event_id") or row.get("sourceEventId") or ""),
                        int(row.get("schema_version") or row.get("schemaVersion") or 1),
                        str(row.get("event_family") or row.get("eventFamily") or ""),
                        str(row.get("kind") or ""),
                        str(row.get("result") or ""),
                        str(row.get("actor_kind") or row.get("actorKind") or ""),
                        row.get("workspace_id") if row.get("workspace_id") is not None else row.get("workspaceId"),
                        str(row.get("occurred_at") or row.get("occurredAt") or ""),
                        row.get("facts_json") if isinstance(row.get("facts_json"), str) else json.dumps(row.get("facts") or {}, ensure_ascii=False, separators=(",", ":"), default=str),
                        row.get("source_hash") if row.get("source_hash") is not None else row.get("sourceHash"),
                        str(row.get("evidence_digest") or row.get("evidenceDigest") or self._digest({"sourceEventId": row.get("source_event_id") or row.get("sourceEventId") or ""})),
                        str(row.get("via") or "restore"),
                        float(row.get("ingested_at") or time.time()),
                    ),
                )
                counts["receipts"] += int(cur.rowcount or 0)
            for row in payload.get("backfillCursors") or payload.get("backfill_cursors") or []:
                cur = db.execute(
                    "INSERT OR IGNORE INTO treehouse_achievement_backfill_cursors("
                    "account_id,source_family,cursor_json,predicate_version,catalog_revision,processed,completed_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        account_id,
                        str(row.get("source_family") or row.get("sourceFamily") or ""),
                        row.get("cursor_json") if isinstance(row.get("cursor_json"), str) else json.dumps(row.get("cursor") or {}, ensure_ascii=False, separators=(",", ":"), default=str),
                        str(row.get("predicate_version") or row.get("predicateVersion") or ""),
                        str(row.get("catalog_revision") or row.get("catalogRevision") or ""),
                        int(row.get("processed") or 0),
                        row.get("completed_at") or row.get("completedAt"),
                        float(row.get("updated_at") or time.time()),
                    ),
                )
                counts["cursors"] += int(cur.rowcount or 0)
            state_payload = payload.get("predicateState") or payload.get("predicate_state")
            if state_payload is not None:
                db.execute(
                    "INSERT INTO treehouse_achievement_predicate_state(account_id,state_json,predicate_version,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(account_id) DO NOTHING",
                    (
                        account_id,
                        json.dumps(state_payload, ensure_ascii=False, separators=(",", ":"), default=str),
                        str(payload.get("predicateVersion") or payload.get("predicate_version") or "1"),
                        time.time(),
                    ),
                )
                counts["predicateState"] = 1
            db.commit()
        return counts


def _now_iso() -> str:
    import time as _time
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
