"""SEED fixture for the journal connected case: the fresh guarded-v18 store of `_native_ops_fixture`, with the optional S06 / S02 / S08 /
S08-model profiles composed in the order the Native opener composes them (`SqliteStore::open_guarded`).

    fx = build_s08_fixture()      # same Fixture object as `_native_ops_fixture.build_fixture()`

Throwaway data only; the binaries must be newer than every crate source file (`StaleBinaryError`, see `_native_ops_fixture`).
"""
import hashlib
import json

from tests import _native_ops_fixture as F

OWNER, ACCOUNT, WORKSPACE, CHAT = 'alice', 'acct-a', 'ws-a', 'chat-1'
BODIES = {'m1': 'I moved to Lisbon.', 'm2': 'My cat is called Pixel.'}


def _sha(raw):
    return hashlib.sha256(raw if isinstance(raw, bytes) else raw.encode()).hexdigest()


def source_ref(message_id):
    """The Host archive source of one seeded user body, field order as `HostArchiveSourceRef` serializes."""
    return {'version': 1, 'kind': 'host_user_body', 'account_id': ACCOUNT, 'chat_id': CHAT, 'actor_id': 'main', 'message_id': message_id,
            'part_id': 'body', 'revision': 1, 'content_hash': _sha(BODIES[message_id]), 'workspace_id': WORKSPACE, 'project_id': None}


def _compact(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False)


def source_id(ref):
    return 'hostpart_' + _sha(_compact([ref['version'], ref['account_id'], ref['chat_id'], ref['actor_id'], ref['message_id'], ref['part_id']]))


def origin_authority(message_id):
    """The authority proof the Host carries for a seeded body (no rebind): the S06 origin row below is exactly what Native re-reads."""
    ref = source_ref(message_id)
    origin_id = 'cs_s06origin_' + _sha(_compact([OWNER, WORKSPACE, ref]))
    raw = _compact({'owner': OWNER, 'source': ref, 'source_id': source_id(ref), 'version': 's06.observation-origin.v1', 'workspace_id': WORKSPACE})
    return origin_id, raw


def _seed_sources_origins_occupancy(db):
    """Rows the S06 projection and the S05 gate normally create; seeded on the stopped v17 copy (the writer fence is satisfied the way
    the profile preparer satisfies it)."""
    db.create_function("fm_writer_capability", 0, lambda: "openclank.frankenmemory.writer.v2", deterministic=True)
    for mid in BODIES:
        ref = source_ref(mid)
        sid = source_id(ref)
        db.execute("INSERT INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,source_uri,source_revision,content_hash,source_type,forget_state,created_at) "
                   "VALUES(?,?,?,'',?,1,?,'host_archive_part.v1','active','t')", (OWNER, sid, WORKSPACE, 'host-archive://' + sid, ref['content_hash']))
        origin_id, raw = origin_authority(mid)
        db.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at,operation_metadata_json) "
                   "VALUES(?,?,'automation','memory_observation','s06.observation-origin.v1',?,?,?,'t',?)", (OWNER, origin_id, sid, CONTRACT_VERSION, _sha(raw), raw))
        db.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES(?,?,?,'s06_observation_origin',?,1,'t')",
                   (OWNER, f'outbox_{origin_id}_s06_observation_origin', origin_id, raw))
    db.execute("INSERT INTO fm_v2_s05_occupancy(singleton,coverage_generation,clock_generation,verified,foreground_epoch,last_quiet_tick) VALUES(1,'cg','ck',1,0,0)")


CONTRACT_VERSION = 'frankenmemory.v2'

_BASE = ("current-schema.sql", "schema-v17-additions.sql", "schema-v17-authority.sql", "schema-v17-s05-additions.sql", "schema-v17-writer-fence.sql",
         "schema-v17-s05-account-v4.sql")
_PAIR = ("schema-v17-s05-assistant-pair.sql",)
_AFTER_PAIR = ("schema-v17-policy-projection.sql", "schema-v17-s09-questions.sql")
_S06 = ("schema-v17-s06-additions.sql", "schema-v17-s06-admitted.sql", "schema-v17-s06-context.sql", "schema-v17-s06-erase.sql", "schema-v17-s06-helped.sql")
_S02 = ("schema-v17-domain-metadata.sql", "schema-v17-s02-policy.sql", "schema-v17-s02-policy-v2.sql")


def _v17_source_s08(path, v2=True):
    import sqlite3
    db = sqlite3.connect(path)
    for name, value in (("fm_writer_capability", "openclank.frankenmemory.writer.v1"),
                        ("fm_policy_projection_capability", "openclank.policy_projection.writer.v1"),
                        ("fm_s09_question_capability", "openclank.questions.writer.v1")):
        db.create_function(name, 0, lambda v=value: v, deterministic=True)
    order = _BASE + _AFTER_PAIR + _S06 + _S02 + ("schema-v17-s08.sql",) + _PAIR + ("schema-v17-s05-assistant-pair-s08.sql", "schema-v17-s08-model.sql") + (("schema-v17-s08-model-v2.sql",) if v2 else ())
    for name in order:
        db.executescript((F.STORE_SQL / name).read_text())
    db.executescript((F.REPO / "src/openclank/memory_schema/principal.sql").read_text())
    db.executescript((F.STORE_SQL / "schema-v17-principal-writer-fence.sql").read_text())
    db.execute("INSERT OR IGNORE INTO fm_meta(key,value) VALUES('openclank.policy_projection.v1','1'),('openclank.question_semantics.v1','1'),"
               "('openclank.assistant_interpretation.v1','1'),('openclank.domain_metadata.v1','1'),('openclank.type_tag_policy.v1','1'),"
               "('openclank.type_tag_policy.v2','1')")
    db.create_function("fm_writer_capability", 0, lambda: "openclank.frankenmemory.writer.v2", deterministic=True)  # as the profile preparer does
    db.execute("INSERT OR IGNORE INTO fm_v2_predicates(predicate,value_type,cardinality,version,allow_unknown) VALUES('s08.composite','object','single',1,0)")
    _seed_sources_origins_occupancy(db)
    db.execute("PRAGMA user_version=17")
    db.commit()
    db.close()


def build_s08_fixture(v2=True):
    """v2=True: the S08 model v2 profile (journal_synthesis can be reserved); v2=False: the v1 profile only (journal unavailable)."""
    original = F._v17_source
    F._v17_source = lambda path: _v17_source_s08(path, v2)
    try:
        return F.build_fixture()
    finally:
        F._v17_source = original
