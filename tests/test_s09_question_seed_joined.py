"""S09 question-seed path over the real transport: Python provider -> real memery-worker -> disposable guarded-v17 store.

No model, no network. Store builder copies the minimal steps of
.clanker/artifacts/terra-resumption-2026-11-04/s04-human-add-joined-case.py (SCHEMAS, initialize_fixture_db)
and s01-atomic-batch-joined-case.py (actual_v18_owners, policy projection). Stubs (outside the provider boundary):
routes.prefs_routes._load_for_user (prefs dict), the `current()` callback and the coordinator's resolve/fingerprint
lambdas (synthetic principal alice/acct). Guarded v17 is enough: the issuer only needs `guarded_preparation`.
Run: see .clanker/robonotes/claude-last-lap-2026-11-09/A-7-s09-question-seed-case.md.
"""
import asyncio
import os
import sqlite3
import atexit
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
STORE_SQL = REPO / "native/memery/crates/memery-core/src/store"
SCHEMAS = (
    "current-schema.sql", "schema-v17-additions.sql", "schema-v17-authority.sql",
    "schema-v17-s05-additions.sql", "schema-v17-writer-fence.sql",
    "schema-v17-s05-account-v4.sql", "schema-v17-s05-assistant-pair.sql",
    "schema-v17-s05-assistant-pair-base.sql", "schema-v17-policy-projection.sql",
    "schema-v17-s09-questions.sql",
)
OWNER, ACCOUNT = "alice", "acct"
EXPECTED_QUESTION_IDS = {"handler-preferred-name", "assistant-operating-boundary"}


def _scratch(prefix):
    """mkdtemp that is removed at interpreter exit (RAM-backed /tmp); the exact path only."""
    path = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def build_store(*, guarded=True):
    """Fresh guarded-v17 store + real provider (memery-worker subprocess). Returns a namespace dict."""
    from src.memery_database import prepare_guarded_memery_database
    from src.memery_provider import MemeryProvider
    from src.openclank.memory_policy_projection import (
        PolicyProjection, PolicyTransitionCoordinator, global_settings_fingerprint, register_policy_coordinator)
    db_path = Path(_scratch("a7-s09-seed-")) / "memory.sqlite"
    db = sqlite3.connect(db_path)
    # Synthetic capability functions only install the schema prerequisites; Native owns every write below.
    for name, value in (("fm_writer_capability", "openclank.frankenmemory.writer.v1"),
                        ("fm_policy_projection_capability", "openclank.policy_projection.writer.v1"),
                        ("fm_s09_question_capability", "openclank.questions.writer.v1")):
        db.create_function(name, 0, lambda v=value: v, deterministic=True)
    for name in SCHEMAS:
        db.executescript((STORE_SQL / name).read_text())
    # Optional principal companion (sqlite.rs:~1103): table DDL + its writer-fence triggers; the seed issuer needs the binding table.
    db.executescript((REPO / "src/openclank/memory_schema/principal.sql").read_text())
    db.executescript((STORE_SQL / "schema-v17-principal-writer-fence.sql").read_text())
    db.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.policy_projection.v1','1'),"
               "('openclank.question_semantics.v1','1'),('openclank.assistant_interpretation.v1','1')")
    db.execute("PRAGMA user_version=17")
    db.commit()
    db.close()
    command = os.environ["OPEN_CLANK_MEMERY_WORKER"]
    os.environ["FM_DB_PATH"] = str(db_path)
    prep = prepare_guarded_memery_database(str(db_path), command=command)
    projection = PolicyProjection(prep)
    for account, digest, is_global in ((ACCOUNT, "9" * 64, False), ("global", global_settings_fingerprint({}), True)):
        fence = projection.provision_fenced(account, "global", digest, global_scope=is_global)
        projection.ready(account, "global", fence, digest, global_scope=is_global)
    coordinator = PolicyTransitionCoordinator(projection, resolve=lambda u: (ACCOUNT, "global") if u == OWNER else None,
        fingerprint=lambda u, prefs: "9" * 64, assert_current=prep.assert_valid)
    register_policy_coordinator(coordinator)
    provider = MemeryProvider(command=command, workspace_id="global",
        env={"FM_DB_PATH": str(db_path), "FM_DB_ID": prep.database_id, "FM_OWNER": OWNER, "FM_WORKSPACE_ID": "global"},
        guarded_preparation=prep if guarded else None)
    return {"db": db_path, "prep": prep, "coordinator": coordinator, "provider": provider}


def rows(db_path, sql, args=()):
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        return db.execute(sql, args).fetchall()


def seed_rows(db_path):
    """Read-back of the stored open questions: (question_id, subject role, status, kind)."""
    import json
    out = rows(db_path, "SELECT b.block_id,b.predicate,r.kind,r.status,r.activation,r.activation_rationale "
               "FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r ON r.owner_id=b.owner_id "
               "AND r.block_id=b.block_id AND r.revision=b.current_revision WHERE b.owner_id=? AND b.block_id LIKE 'question_seed_%'", (OWNER,))
    return sorted((json.loads(rat)["question_context"]["question_id"], pred, kind, status, act, block) for block, pred, kind, status, act, rat in out)


async def bootstrap(ns, *, prefs=None, key="root-1", incognito=False, no_memory=False):
    """Mirror of routes/chat_routes.py:~2680-2693 arguments: coordinator, current(), incognito/no_memory False, 2s deadline, request_key=root id."""
    provider = ns["provider"]
    prefs = {"memory_mode": "automatic"} if prefs is None else prefs  # write policy needs automatic mode; {} refuses (see last test)
    with patch("routes.prefs_routes._load_for_user", lambda user: prefs):
        if provider._guarded_v17:  # legacy provider is refused before any transport use
            await provider.initialize()
        return await provider._host_question_seed_bootstrap(owner=OWNER, account_id=ACCOUNT, request_key=key,
            coordinator=ns["coordinator"], current=lambda: True, incognito=incognito, no_memory=no_memory,
            write_deadline=asyncio.get_running_loop().time() + 2.0)


def run(ns, coro):
    from src.openclank.memory_policy_projection import unregister_policy_coordinator
    async def go():
        try:
            return await coro
        finally:
            await ns["provider"].shutdown()
            unregister_policy_coordinator(ns["coordinator"])
    return asyncio.run(go())


def refused(ns, message, **kw):
    from src.memory_provider import MemoryRequestRejectedError
    try:
        run(ns, bootstrap(ns, **kw))
    except MemoryRequestRejectedError as exc:
        assert str(exc) == message, str(exc)
    else:
        raise AssertionError("bootstrap was not refused")
    assert seed_rows(ns["db"]) == [] and rows(ns["db"], "SELECT count(*) FROM fm_v2_entities") == [(0,)]


def test_positive_seeds_created_exactly_once_and_status_reads_back():
    ns = build_store()
    async def case():
        first = await bootstrap(ns)
        assert set(first) == {"version", "state", "assistant_entity_id", "handler_entity_id", "created", "visibility"}
        assert (first["version"], first["state"], first["created"], first["visibility"]) == (1, "applied", 2, "not_rechecked")
        # Real status path (Native phase question_seed_status) for the exact original request.
        refs = ns["coordinator"].checked_refs(OWNER, ACCOUNT, "global")
        original = {"version": 2, "owner": OWNER, "account_id": ACCOUNT, "workspace_id": "global", "request_key": "root-1", "policy_projection": refs}
        with patch("routes.prefs_routes._load_for_user", lambda user: {}):
            status = await ns["provider"]._host_question_seed_status(owner=OWNER, account_id=ACCOUNT,
                original_request=original, coordinator=ns["coordinator"], current=lambda: True)
        assert status == {**first, "state": "already_applied"}, status
        return first
    first = run(ns, case())
    seeds = seed_rows(ns["db"])
    assert [s[0] for s in seeds] == sorted(EXPECTED_QUESTION_IDS) and len(seeds) == 2, seeds
    assert {(s[2], s[3], s[4]) for s in seeds} == {("open_question", "open", "manual")}, seeds
    assert {s[1] for s in seeds} == {"preferred_name", "operating_boundaries"}
    assert rows(ns["db"], "SELECT count(*) FROM fm_v2_entities") == [(2,)]
    assert first["assistant_entity_id"] != first["handler_entity_id"]


def test_second_bootstrap_creates_nothing_new():
    ns = build_store()
    async def case():
        first = await bootstrap(ns)
        before = seed_rows(ns["db"]), rows(ns["db"], "SELECT block_id,current_revision FROM fm_v2_knowledge_blocks ORDER BY 1")
        # Same key and a different root key: Host preflight (guarded_seed_slots_present) short-circuits.
        again = await bootstrap(ns)
        other = await bootstrap(ns, key="root-2")
        return first, before, again, other
    first, before, again, other = run(ns, case())
    assert again == other == {"version": 1, "state": "slots_present"}, (again, other)
    assert (seed_rows(ns["db"]), rows(ns["db"], "SELECT block_id,current_revision FROM fm_v2_knowledge_blocks ORDER BY 1")) == before
    assert len(before[0]) == 2 and rows(ns["db"], "SELECT count(*) FROM fm_v2_entities") == [(2,)]


def test_pref_off_refuses_and_writes_nothing():
    refused(build_store(), "question seeds disabled or unavailable", prefs={"memory_mode": "automatic", "memory_question_seeds": False})


def test_incognito_refuses_and_writes_nothing():
    refused(build_store(), "question seed authority unavailable", incognito=True)
    refused(build_store(), "question seed authority unavailable", no_memory=True)


def test_non_guarded_store_refuses_and_writes_nothing():
    # No guarded_preparation => _guarded_v17 False (memery_provider.py:~710); same on-disk store, untouched.
    refused(build_store(guarded=False), "question seed authority unavailable")


def test_default_prefs_without_automatic_mode_refuse():
    # Finding: with the default prefs ({}), direct_write_allowed is False, so seeds are not created until memory_mode is automatic.
    refused(build_store(), "question seed policy unavailable", prefs={})
