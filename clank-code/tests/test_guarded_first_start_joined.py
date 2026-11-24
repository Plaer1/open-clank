"""Real transport: fresh install (OPENCLANK_MEMORY_STORE_PROFILE unset, Linux) -> resolve_memory_store_profile
prepares a fresh guarded-v18 store -> a real MemeryProvider over real memery-worker -> a brand-new synthetic
account provisioned the same way PolicyProjection/PolicyTransitionCoordinator are provisioned in production
(src/openclank/memory_job_admission.py refresh_completed_turn_registry calls exactly these two steps per
owner) -> Add/list/recall through the real guarded path.

No model, no network, no UI. Everything lives in a fresh mkdtemp directory; nothing under ~/.open-clank,
data/ or any repo-root store is touched. Uses the real binaries at OPEN_CLANK_MEMERY_WORKER / their migrator sibling.
"""
import asyncio
import json
import os
import atexit
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from src import memery_database as fd
from src.openclank.memory_policy_projection import (
    PolicyProjection, PolicyTransitionCoordinator, global_settings_fingerprint,
    policy_coordinator, register_policy_coordinator, unregister_policy_coordinator,
)


def _scratch(prefix):
    """mkdtemp that is removed at interpreter exit (RAM-backed /tmp); the exact path only."""
    path = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def _fresh_db_path():
    return os.path.join(_scratch("gdef-joined-"), "fm.db")


@pytest.fixture(autouse=True)
def _clean_env():
    old = os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
    existing = policy_coordinator()
    if existing is not None:
        unregister_policy_coordinator(existing)
    _OWNERS.clear()
    yield
    existing = policy_coordinator()
    if existing is not None:
        unregister_policy_coordinator(existing)
    os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
    if old is not None:
        os.environ["OPENCLANK_MEMORY_STORE_PROFILE"] = old


_OWNERS = {}


def _provision_scope(projection, account, workspace, *, global_scope=False, digest="9" * 64):
    fence = projection.provision_fenced(account, workspace, digest, global_scope=global_scope)
    projection.ready(account, workspace, fence, digest, global_scope=global_scope)


def _provision(prep, owner, account, workspace, *, provision_global=False):
    """Give the host its global policy receipt and leave the account scope absent."""
    projection = PolicyProjection(prep)
    digest = "9" * 64
    if provision_global:
        _provision_scope(projection, "global", "global", global_scope=True, digest=global_settings_fingerprint({}))
    _OWNERS[owner] = (account, workspace)
    coordinator = PolicyTransitionCoordinator(
        projection, resolve=lambda user: _OWNERS.get(user),
        fingerprint=lambda user, prefs: digest, assert_current=prep.assert_valid)
    existing = policy_coordinator()
    if existing is not None:
        unregister_policy_coordinator(existing)
    register_policy_coordinator(coordinator)
    return coordinator


async def _add_list_recall(provider, coordinator, owner, account, workspace, text):
    await provider.initialize()
    assert await provider._host_negotiate_explicit_add(owner=owner, workspace_id=workspace) is True
    assert await provider._host_negotiate_native_memory_ops(owner=owner, workspace_id=workspace) is True
    refs = coordinator.checked_refs(owner, account, workspace)
    before_issue = lambda: coordinator.checked_refs(owner, account, workspace) == refs
    staged = await provider._host_explicit_add({
        "version": 1, "owner": owner, "workspace_id": workspace, "account_id": account,
        "request_key": f"gdef-add-request-{owner}-0001", "policy_projection": refs, "content": text, "category": "fact",
        "source": "user", "question_context": None, "mode": "review_only"}, before_issue=before_issue)
    assert staged["state"] == "staged", staged
    selector = {"version": 1, "owner": owner, "workspace_id": workspace, "account_id": account,
                "policy_projection": refs, "candidate_ids": [staged["candidate_id"]]}
    preview = await provider._host_native_memory_ops_batch_preview(selector)
    applied = await provider._host_native_memory_ops_batch_apply(
        {"selector": selector, "request_key": f"gdef-batch-request-{owner}-0001", "preview_sha256": preview["preview_sha256"]},
        before_issue=before_issue)
    assert applied["state"] == "committed", applied
    listed = await provider.list_memories(owner=owner, limit=100)
    recalled = await provider.recall(text.split()[0], owner=owner, top_k=5)
    return listed, recalled


def test_first_resolution_prepares_a_ready_v18_store_with_receipt_and_timing():
    path = _fresh_db_path()
    start = time.monotonic()
    profile = fd.resolve_memory_store_profile(path)
    elapsed = time.monotonic() - start
    assert profile == "guarded-v18"
    assert os.environ["OPENCLANK_MEMORY_STORE_PROFILE"] == "guarded-v18"
    with __import__("sqlite3").connect(Path(path).as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (18,)
    assert not any(name.startswith(".prepare-") for name in os.listdir(os.path.dirname(path)))
    receipt_path = path + ".prepare-receipt.json"
    assert os.path.isfile(receipt_path)
    receipt = json.loads(Path(receipt_path).read_text())
    assert receipt["schema_version"] == 18 and receipt["elapsed_seconds"] > 0
    print(f"G-DEF first-start preparation: {elapsed:.3f}s (test machine, debug binaries)")


def test_full_flow_provision_then_add_list_recall_scoped_by_owner():
    path = _fresh_db_path()
    os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
    profile = fd.resolve_memory_store_profile(path)
    assert profile == "guarded-v18"

    from src.memery_database import prepare_guarded_memery_database
    from src.memery_provider import MemeryProvider
    prep = prepare_guarded_memery_database(path, command=os.environ["OPEN_CLANK_MEMERY_WORKER"], profile="guarded-v18")

    # Before provisioning runs for this brand-new account, no coordinator is registered at all: the Add route's
    # `coordinator is None` check raises ValueError('policy unavailable'), which is exactly what the route maps
    # to {state:unavailable, reason:memory_preparing} (decision 4).
    assert policy_coordinator() is None

    coordinator = _provision(prep, "alice", "acct-alice", "ws-alice", provision_global=True)
    assert coordinator.projection.read_scope("acct-alice", "ws-alice") is None
    with pytest.raises(ValueError, match="target changed"):
        coordinator.prepare_explicit_account("alice", "acct-other", "ws-alice")
    assert coordinator.projection.read_scope("acct-other", "ws-alice") is None
    coordinator.prepare_explicit_account("alice", "acct-alice", "ws-alice")
    assert coordinator.projection.read_scope("acct-alice", "ws-alice")["state"] == "ready"

    async def go():
        alice2 = MemeryProvider(host_worker=True, command=os.environ["OPEN_CLANK_MEMERY_WORKER"], workspace_id="ws-alice",
                                        env={"FM_DB_PATH": path, "FM_DB_ID": prep.database_id,
                                             "FM_SCOPE_AUTHORITY": "trusted-caller"},
                                        guarded_preparation=prep)
        try:
            a_listed, a_recalled = await _add_list_recall(alice2, coordinator, "alice", "acct-alice", "ws-alice", "gdef secret alpha fact")
            texts_a = [r.text for r in a_listed]
            assert any("gdef secret alpha fact" in t for t in texts_a)
            assert any("gdef secret alpha fact" in r.memory.text for r in a_recalled)
            b_listed = await alice2.list_memories(owner="bob", limit=100)
            assert not any("gdef secret alpha fact" in r.text for r in b_listed)  # wrong owner sees nothing
        finally:
            await alice2.shutdown()
    asyncio.run(go())
    row = coordinator.projection.read_scope("acct-alice", "ws-alice")
    fenced = coordinator.projection.fence("acct-alice", "ws-alice", row)
    coordinator.prepare_explicit_account("alice", "acct-alice", "ws-alice")
    assert coordinator.projection.read_scope("acct-alice", "ws-alice") == fenced
    with pytest.raises(ValueError, match="projection fenced"):
        coordinator.checked_refs("alice", "acct-alice", "ws-alice")


def test_first_permission_write_qualifies_only_an_absent_account_scope(monkeypatch):
    from core.auth import AuthManager
    from routes import prefs_routes
    from src.openclank.memory_policy_projection import PolicyTransitionUnavailable
    import hashlib

    path = _fresh_db_path()
    profile = fd.resolve_memory_store_profile(path)
    prep = fd.prepare_guarded_memery_database(path, command=os.environ["OPEN_CLANK_MEMERY_WORKER"], profile=profile)
    auth_path = str(Path(path).with_name("auth.json"))
    auth = AuthManager(auth_path)
    assert auth.create_user("reader", "test-guarded-account-password")
    account = auth.account_id("reader")
    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda _user: {})

    def fingerprint(user, _preferences):
        allowed = auth.get_privileges(user)["can_manage_memory"]
        return hashlib.sha256(str(allowed).encode()).hexdigest()

    coordinator = PolicyTransitionCoordinator(PolicyProjection(prep), resolve=lambda user: (auth.account_id(user), "global"),
                                              fingerprint=fingerprint, assert_current=prep.assert_valid)
    register_policy_coordinator(coordinator)
    assert coordinator.projection.read_scope(account, "global") is None
    assert auth.set_privileges("reader", {"can_manage_memory": True})
    assert AuthManager(auth_path).get_privileges("reader")["can_manage_memory"] is True
    ready = coordinator.projection.read_scope(account, "global")
    assert ready["state"] == "ready" and ready["fingerprint"] == fingerprint("reader", {})
    fenced = coordinator.projection.fence(account, "global", ready)
    with pytest.raises(PolicyTransitionUnavailable):
        auth.set_privileges("reader", {"can_manage_memory": False})
    assert coordinator.projection.read_scope(account, "global") == fenced
    assert AuthManager(auth_path).get_privileges("reader")["can_manage_memory"] is True


def test_second_resolution_is_instant_and_byte_identical():
    path = _fresh_db_path()
    os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
    fd.resolve_memory_store_profile(path)
    before = Path(path).read_bytes()
    os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)  # force the resolver to look again
    start = time.monotonic()
    profile = fd.resolve_memory_store_profile(path)
    elapsed = time.monotonic() - start
    assert profile == "guarded-v18"
    assert Path(path).read_bytes() == before
    assert elapsed < 1.0  # one read-only PRAGMA user_version; no preparation


def test_crash_leftover_staging_dir_is_cleaned_and_preparation_succeeds():
    path = _fresh_db_path()
    stale = os.path.join(os.path.dirname(path), ".prepare-deadrun123")
    os.makedirs(stale)
    Path(os.path.join(stale, "leftover.sqlite")).write_bytes(b"garbage")
    os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
    profile = fd.resolve_memory_store_profile(path)
    assert profile == "guarded-v18"
    assert not os.path.isdir(stale)
    assert os.path.isfile(path)


def test_two_concurrent_resolutions_produce_exactly_one_store():
    path = _fresh_db_path()
    os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
    results, errors = [], []

    def worker():
        try:
            # Each thread resolves independently against the same path; the on-disk lock file
            # (FM_DB_PATH + '.prepare.lock') serializes the actual preparation.
            os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
            results.append(fd.resolve_memory_store_profile(path))
        except Exception as exc:  # pragma: no cover - surfaced via errors
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert results == ["guarded-v18", "guarded-v18"]
    assert os.path.isfile(path)
    with __import__("sqlite3").connect(Path(path).as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (18,)


def test_preexisting_legacy_v16_file_stays_byte_identical_and_resolves_legacy():
    from src.memery_database import prepare_memery_database
    path = _fresh_db_path()
    prepare_memery_database(path)
    before = Path(path).read_bytes()
    os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
    profile = fd.resolve_memory_store_profile(path)
    assert profile == "legacy-v16"
    assert Path(path).read_bytes() == before


def test_preparation_failure_refuses_and_leaves_no_file_at_final_path():
    path = _fresh_db_path()
    os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
    bogus = os.path.join(os.path.dirname(path), "no-such-migrator")
    old = os.environ.get("FM_MIGRATE_COMMAND")
    os.environ["FM_MIGRATE_COMMAND"] = bogus
    try:
        with pytest.raises(RuntimeError, match="migrator executable is missing"):
            fd.resolve_memory_store_profile(path)
    finally:
        if old is None:
            os.environ.pop("FM_MIGRATE_COMMAND", None)
        else:
            os.environ["FM_MIGRATE_COMMAND"] = old
    assert not os.path.exists(path)
    assert not any(name.startswith(".prepare-") for name in os.listdir(os.path.dirname(path)))


def test_fresh_guarded_store_preference_defaults_are_unchanged():
    """Decision 8: nothing turns on by itself on a fresh guarded store."""
    path = _fresh_db_path()
    os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
    fd.resolve_memory_store_profile(path)
    from src.memory_gate import memory_mode, write_allowed
    prefs = {}
    assert memory_mode(prefs) == "manual"  # absent memory_mode defaults to review-first, same as legacy
    allowed, _ = write_allowed(prefs)
    assert allowed is False  # manual mode refuses a direct write; Add still stages via review_only
    from routes.prefs_routes import _load_for_user  # sanity: no prefs file exists yet for a brand-new account
    assert (_load_for_user("nobody-yet") or {}) == {}
