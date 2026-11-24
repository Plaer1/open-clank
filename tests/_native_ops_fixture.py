"""Shared fixture for native-memory-operation tests: a FRESH guarded-v18 store over the real transport.

Python provider -> real memery-worker -> disposable guarded-v18 store, built with today's binaries:
  v17 source (current schema files + principal companion) -> `memery-migrate --prepare-guarded-v18
  <source> <backup> <destination> --writers-stopped` (non-WAL source, absolute paths, pinned by SHA-256).
Two synthetic owners in distinct workspaces (alice/acct-a/ws-a, bob/acct-b/ws-b), each with its own provider (a
provider is bound to one workspace); both providers talk to their own memery-worker process on the same store.
No model, no network, no UI, no real data. Everything lives in a mkdtemp dir that is removed at exit (or by `fx.close()`).

    fx = build_fixture()                      # unpacks as (preparation, provider, coordinator)
    preparation, provider, coordinator = fx
    asyncio.run(seed_records(fx, 'alice', ['first', 'second']))   # real explicit-add + create-only batch path
    rows(fx.db, 'SELECT ...')                 # read-only SQL

The binaries must be newer than every file under the src/ of the crates they are built from or the fixture refuses to
run (`StaleBinaryError`): rebuild with `cargo build --locked --offline -j 2 -p memery-worker -p memery-migrate`.
"""
import asyncio
import atexit
import hashlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CRATES = REPO / "native/memery/crates"
STORE_SQL = CRATES / "memery-core/src/store"
SCHEMAS = (
    "current-schema.sql", "schema-v17-additions.sql", "schema-v17-authority.sql",
    "schema-v17-s05-additions.sql", "schema-v17-writer-fence.sql",
    "schema-v17-s05-account-v4.sql", "schema-v17-s05-assistant-pair.sql",
    "schema-v17-s05-assistant-pair-base.sql", "schema-v17-policy-projection.sql",
    "schema-v17-s09-questions.sql",
)
OWNERS = {"alice": ("acct-a", "ws-a"), "bob": ("acct-b", "ws-b")}
DIGEST = "9" * 64


class StaleBinaryError(RuntimeError):
    pass


def check_binaries(command):
    """memery-worker and the migrator must be newer than every file of the crates they are built from
    (memery-worker: memery-core + memery-worker; migrator: memery-core + memery-migrate; see their Cargo.toml)."""
    bindir = Path(command).parent
    for binary, crates in ((Path(command), ("memery-core", "memery-worker")), (bindir / "memery-migrate", ("memery-core", "memery-migrate"))):
        if not binary.is_file():
            raise StaleBinaryError(f"{binary} is missing: cargo build --locked --offline -j 2 -p memery-worker -p memery-migrate")
        newest = max((p.stat().st_mtime, p) for crate in crates for p in (CRATES / crate / "src").rglob("*") if p.is_file())
        if binary.stat().st_mtime < newest[0]:
            raise StaleBinaryError(f"{binary.name} is older than {newest[1].relative_to(REPO)}: rebuild memery-worker and the migrator before using this fixture")
    return bindir / "memery-migrate"


class Fixture:
    """Unpacks as (preparation, provider, coordinator); the rest is reachable by attribute."""
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __iter__(self):
        return iter((self.preparation, self.provider, self.coordinator))

    def provider_for(self, owner):
        return self.providers[owner]

    def close(self):
        """Stop the providers, unregister the coordinator and delete this fixture's directory (idempotent).
        Also runs at interpreter exit; call it explicitly from a test that is done with the store."""
        _cleanup(self.root)


_LIVE = {}  # root -> Fixture (or None while it is being built); only roots this module created, never globbed


def _cleanup(root):
    root = Path(root)
    fx = _LIVE.pop(root, "gone")
    if fx == "gone":
        return
    if fx is not None:
        try:
            asyncio.run(asyncio.wait_for(shutdown(fx), 10))
        except BaseException:  # providers whose loop already closed raise CancelledError here; the directory is removed regardless
            pass
        try:
            from src.openclank.memory_policy_projection import policy_coordinator, unregister_policy_coordinator
            if policy_coordinator() is fx.coordinator:
                unregister_policy_coordinator(fx.coordinator)
        except Exception:
            pass
    if root.name.startswith("native-ops-fixture-"):
        shutil.rmtree(root, ignore_errors=True)


def _v17_source(path):
    db = sqlite3.connect(path)
    for name, value in (("fm_writer_capability", "openclank.frankenmemory.writer.v1"),
                        ("fm_policy_projection_capability", "openclank.policy_projection.writer.v1"),
                        ("fm_s09_question_capability", "openclank.questions.writer.v1")):
        db.create_function(name, 0, lambda v=value: v, deterministic=True)
    for name in SCHEMAS:
        db.executescript((STORE_SQL / name).read_text())
    # Principal companion (sqlite.rs ~1103): table DDL plus its writer-fence triggers; without it the opener refuses.
    db.executescript((REPO / "src/openclank/memory_schema/principal.sql").read_text())
    db.executescript((STORE_SQL / "schema-v17-principal-writer-fence.sql").read_text())
    db.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.policy_projection.v1','1'),"
               "('openclank.question_semantics.v1','1'),('openclank.assistant_interpretation.v1','1')")
    db.execute("PRAGMA user_version=17")
    db.commit()
    db.close()


def build_fixture():
    from scripts.prepare_memery_profiles import prepare_guarded_v18
    from src.memery_database import prepare_guarded_memery_database
    from src.memery_provider import MemeryProvider
    from src.openclank.memory_policy_projection import (
        PolicyProjection, PolicyTransitionCoordinator, global_settings_fingerprint, register_policy_coordinator)
    command = os.environ["OPEN_CLANK_MEMERY_WORKER"]
    migrator = check_binaries(command)
    root = Path(tempfile.mkdtemp(prefix="native-ops-fixture-")).resolve()
    _LIVE[root] = None
    atexit.register(_cleanup, root)  # also on failure while building; a killed process may leave the directory
    source, backup, db_path = root / "source-v17.sqlite", root / "backup-v17.sqlite", root / "guarded-v18.sqlite"
    _v17_source(source)
    # cargo hard-links target/debug binaries into deps/; the pinned-executable check wants an unaliased file, so use a private copy.
    pinned = root / "migrate-pinned"
    shutil.copy2(migrator, pinned)
    migrator = pinned
    prepare_guarded_v18(source, backup, db_path, migrator=migrator,
                        migrator_sha256=hashlib.sha256(migrator.read_bytes()).hexdigest(), stopped_writers=True)
    os.environ["FM_DB_PATH"] = str(db_path)
    prep = prepare_guarded_memery_database(str(db_path), command=command, profile="guarded-v18")
    assert prep.schema_version == 18
    projection = PolicyProjection(prep)
    targets = [(account, workspace, False) for account, workspace in OWNERS.values()] + [("global", "global", True)]
    for account, workspace, is_global in targets:
        digest = global_settings_fingerprint({}) if is_global else DIGEST
        fence = projection.provision_fenced(account, workspace, digest, global_scope=is_global)
        projection.ready(account, workspace, fence, digest, global_scope=is_global)
    coordinator = PolicyTransitionCoordinator(
        projection, resolve=lambda user: OWNERS.get(user), fingerprint=lambda user, prefs: DIGEST,
        assert_current=prep.assert_valid)
    # One coordinator is registered process-wide; a later fixture replaces an earlier one (its tests only use fx.coordinator).
    from src.openclank.memory_policy_projection import policy_coordinator, unregister_policy_coordinator
    if policy_coordinator() is not None:
        unregister_policy_coordinator(policy_coordinator())
    register_policy_coordinator(coordinator)
    providers = {owner: MemeryProvider(
        command=command, workspace_id=workspace,
        env={"FM_DB_PATH": str(db_path), "FM_DB_ID": prep.database_id}, guarded_preparation=prep)
        for owner, (_, workspace) in OWNERS.items()}
    fx = Fixture(root=root, db=db_path, preparation=prep, provider=providers["alice"], coordinator=coordinator,
                 providers=providers, owners=dict(OWNERS))
    _LIVE[root] = fx
    return fx


def rows(db_path, sql, args=()):
    """Read-only SQL inspector."""
    with sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True) as db:
        return db.execute(sql, args).fetchall()


async def shutdown(fx):
    for provider in fx.providers.values():
        await provider.shutdown()


async def seed_records(fx, owner, texts, *, category="fact", tag="seed"):
    """Create genuine records through the REAL path: explicit-add (review_only) stages candidates, then the
    create-only batch (preview + apply) accepts them as curated knowledge blocks.
    Returns [{text, candidate_id, curated_id, block_id, revision}] in input order."""
    account, workspace = fx.owners[owner]
    provider, coordinator = fx.providers[owner], fx.coordinator
    await provider.initialize()
    assert await provider._host_negotiate_explicit_add(owner=owner, workspace_id=workspace) is True
    assert await provider._host_negotiate_native_memory_ops(owner=owner, workspace_id=workspace) is True
    refs = coordinator.checked_refs(owner, account, workspace)
    before_issue = lambda: coordinator.checked_refs(owner, account, workspace) == refs
    ids = []
    for index, text in enumerate(texts):
        result = await provider._host_explicit_add({
            "version": 1, "owner": owner, "workspace_id": workspace, "account_id": account,
            "request_key": f"{tag}-{owner}-{index}", "policy_projection": refs, "content": text, "category": category,
            "source": "user", "question_context": None, "mode": "review_only"}, before_issue=before_issue)
        assert result["state"] == "staged", result
        ids.append(result["candidate_id"])
    selector = {"version": 1, "owner": owner, "workspace_id": workspace, "account_id": account,
                "policy_projection": refs, "candidate_ids": ids}
    preview = await provider._host_native_memory_ops_batch_preview(selector)
    applied = await provider._host_native_memory_ops_batch_apply(
        {"selector": selector, "request_key": f"{tag}-{owner}-batch-key-0001", "preview_sha256": preview["preview_sha256"]},
        before_issue=before_issue)
    assert applied["state"] == "committed" and [i["candidate_id"] for i in applied["items"]] == ids, applied
    return [{"text": text, **item} for text, item in zip(texts, applied["items"])]


def run(coro):
    return asyncio.run(coro)
