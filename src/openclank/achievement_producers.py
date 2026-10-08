"""Successful action adapters to the existing account achievement ledger.

Call only after the authoritative action succeeds. IDs are occurrence identities,
not private content; facts must contain only the catalogue's minimal evidence.
"""
from __future__ import annotations

import hashlib
import logging
import os
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from src.auth_helpers import effective_user
from src.constants import DATA_DIR
from src.openclank.copal_treehouse_repository import TreeHouseRepository
from src.openclank.treehouse_achievements import ActivityEvent, TreeHouseAchievementEngine

logger = logging.getLogger(__name__)


def record_activity(request, family: str, occurrence_id: str, facts: Mapping[str, Any], *,
                    workspace_id: str | None = None, kind: str = "R") -> bool:
    """Best-effort delivery after commit; never turn success into a retryable error.

    Immutable account identity is resolved from the authenticated request. The
    existing ledger owns dedupe and reset boundaries; this is not another engine.
    """
    try:
        owner = effective_user(request)
        manager = getattr(request.app.state, "auth_manager", None)
        if str(owner or '').lower() in {"local-installation", "installer", "maintenance", "template_seed", "test_fixture", "import", "background_maintenance"}:
            return False
        account = manager.account_id(owner) if owner and manager else None
        if not account or not occurrence_id:
            return False
        repo = getattr(request.app.state, "treehouse_repository", None)
        if repo is None:
            repo = TreeHouseRepository(Path(os.environ.get("TREEHOUSE_REPOSITORY_PATH") or Path(DATA_DIR) / "treehouse.sqlite3"))
            request.app.state.treehouse_repository = repo
        return record_account_activity(str(account), family, occurrence_id, facts, workspace_id=workspace_id, kind=kind, repository=repo)
    except Exception:
        # No request bodies, document content, or user IDs in diagnostic output.
        logger.warning("Achievement producer delivery unavailable (%s)", family)
        return False


def record_worker_activity(auth_manager, owner: str, family: str, occurrence_id: str,
                           facts: Mapping[str, Any], *, workspace_id: str | None = None) -> bool:
    """Adapter for an authenticated scheduled owner after durable run commit."""
    from types import SimpleNamespace
    request = SimpleNamespace(state=SimpleNamespace(current_user=owner),
                              app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_manager)))
    return record_activity(request, family, occurrence_id, facts, workspace_id=workspace_id)


_delivery_lock = threading.RLock()

def record_account_activity(account: str, family: str, occurrence_id: str, facts: Mapping[str, Any], *,
                            workspace_id: str | None = None, kind: str = "R", repository=None) -> bool:
    """Trusted domain adapter. Account must already be authenticated by its owner.

    A durable minimal receipt is staged beside the existing repository before
    delivery to its engine. Genuine actions and explicit resume drain the same
    account's pending receipts; no background polling or second award authority.
    """
    if not account or not occurrence_id or str(account).lower() in {"local-installation", "installer", "maintenance", "template_seed", "test_fixture", "import", "background_maintenance"}:
        return False
    repo = repository or TreeHouseRepository(Path(os.environ.get("TREEHOUSE_REPOSITORY_PATH") or Path(DATA_DIR) / "treehouse.sqlite3"))
    identity = hashlib.sha256(f"{account}\0{workspace_id or ''}\0{family}\0{occurrence_id}".encode()).hexdigest()
    event = ActivityEvent(source_event_id=f"producer:{identity}", event_family=family, kind=kind,
                          result="committed" if kind == "R" else "acknowledged", actor_kind="user",
                          occurred_at=datetime.now(UTC).isoformat(), workspace_id=workspace_id, facts=dict(facts)).normalized()
    with _delivery_lock:
        root = repo.path.parent / (repo.path.name + ".producer-pending") / hashlib.sha256(account.encode()).hexdigest()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / (identity + ".json")
        if not path.exists():
            from core.atomic_io import atomic_write_bytes
            atomic_write_bytes(path, json.dumps(event, sort_keys=True, separators=(",", ":")).encode())
        return resume_account_activity(account, repository=repo)

def resume_account_activity(account: str, *, repository=None) -> bool:
    repo = repository or TreeHouseRepository(Path(os.environ.get("TREEHOUSE_REPOSITORY_PATH") or Path(DATA_DIR) / "treehouse.sqlite3"))
    root = repo.path.parent / (repo.path.name + ".producer-pending") / hashlib.sha256(account.encode()).hexdigest()
    complete = True
    with _delivery_lock:
        for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime_ns):
            try:
                event = json.loads(path.read_text())
                with repo.achievement_transaction():
                    result = TreeHouseAchievementEngine(repo).ingest(account, [event], via="live")
                    if result.get("rejected"):
                        complete = False
                        continue
                    # Keep an independent server delivery digest in the existing
                    # journal idempotency authority before removing its spool.
                    source_id = str(event['source_event_id'])
                    if not repo.journal_activity_delivered(account, source_id):
                        repo.mark_journal_activity_delivered(account, source_id, hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest())
                path.unlink()
            except Exception:
                complete = False
                break
    return complete


def clear_pending_account_activity(account: str, *, repository=None) -> int:
    """Explicit achievement reset boundary; never replay pre-reset receipts."""
    repo = repository or TreeHouseRepository(Path(os.environ.get("TREEHOUSE_REPOSITORY_PATH") or Path(DATA_DIR) / "treehouse.sqlite3"))
    root = repo.path.parent / (repo.path.name + ".producer-pending") / hashlib.sha256(account.encode()).hexdigest()
    count = 0
    with _delivery_lock:
        for path in root.glob('*.json'):
            path.unlink()
            count += 1
        if root.exists():
            root.rmdir()
    return count


def reset_account_activity(account: str, *, repository=None) -> dict[str, int]:
    """Serialize explicit ledger reset with same-account pending delivery."""
    repo = repository or TreeHouseRepository(Path(os.environ.get("TREEHOUSE_REPOSITORY_PATH") or Path(DATA_DIR) / "treehouse.sqlite3"))
    with _delivery_lock:
        pending = clear_pending_account_activity(account, repository=repo)
        counts = repo.purge_achievement_ledger(account)
        return {**counts, "pendingProducerReceipts": pending}


def record_journal_activity(account: str, family: str, occurrence_id: str, facts: Mapping[str, Any], *,
                            occurred_at: str, workspace_id: str | None = None, repository=None) -> bool:
    """Deliver an authoritative durable journal once, surviving award reset.

    The source journal retries on an actual resume/action. Its permanent marker
    uses the existing idempotency store, atomically with this same award engine.
    Returns whether a previously delivered occurrence was replayed.
    """
    if not account or not occurrence_id:
        raise ValueError("Journal receipt needs an authenticated account and occurrence")
    repo = repository or TreeHouseRepository(Path(os.environ.get("TREEHOUSE_REPOSITORY_PATH") or Path(DATA_DIR) / "treehouse.sqlite3"))
    identity = hashlib.sha256(f"{account}\0{family}\0{occurrence_id}".encode()).hexdigest()
    event = ActivityEvent(source_event_id=f"journal:{identity}", event_family=family, kind="R", result="committed",
                          actor_kind="user", occurred_at=occurred_at, workspace_id=workspace_id, facts=dict(facts)).normalized()
    with _delivery_lock, repo.achievement_transaction():
        if repo.journal_activity_delivered(account, identity):
            return True
        result = TreeHouseAchievementEngine(repo).ingest(account, [event], via="live")
        if result.get("rejected"):
            raise ValueError("Committed journal receipt was rejected")
        repo.mark_journal_activity_delivered(account, identity, hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest())
    return False
