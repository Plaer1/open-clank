"""Native current-schema admission before any Python memory projection."""
import json
import hashlib
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from contextlib import closing
from types import MappingProxyType
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

from src.runtime_paths import resolve_memery_worker


def prepare_memery_database(database_path: str | None = None) -> str:
    """Initialize a fresh store or admit current data using the configured native owner.

    The native executable rejects historical schemas. This entrypoint cannot
    perform conversion and does not start the serving worker or any embedding/model client.
    """
    if database_path is None:
        from src.constants import FM_DB_PATH
        database_path = FM_DB_PATH
    path = os.path.abspath(os.path.expanduser(database_path))
    command = resolve_memery_worker().path
    env = dict(os.environ, OPEN_CLANK_MEMERY_DB_PATH=path, FM_DB_PATH=path)
    env.pop('FM_STORE_PROFILE', None)
    result = subprocess.run(
        [command, "--prepare-database"], env=env, capture_output=True, text=True,
        timeout=60, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Memery current-schema preparation failed: {result.stderr.strip()}")
    try:
        receipt = json.loads(result.stdout)
        database_id = receipt["database_id"]
        if not isinstance(database_id, str) or not database_id:
            raise ValueError("missing database identity")
        if receipt["database_path"] != path or not isinstance(receipt["schema_version"], int):
            raise ValueError("database path/schema receipt mismatch")
        with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as connection:
            stored = connection.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        if stored is None or stored[0] != database_id or version != receipt["schema_version"]:
            raise ValueError("native receipt differs from durable database identity/schema")
    except (KeyError, TypeError, ValueError, sqlite3.Error) as error:
        raise RuntimeError("Invalid Memery current-schema preparation receipt") from error
    expected = os.environ.get("FM_DB_ID", "").strip()
    if expected and expected != database_id:
        raise RuntimeError("Memery database identity mismatch")
    os.environ["FM_DB_ID"] = database_id
    return database_id

# This process-local receipt is produced only by the host's explicit preparation
# path; the schema/identity checks, not the object itself, provide compatibility.
_GUARDED_PREPARATION_SEAL = object()


def _guarded_schema_snapshot(path, database_id, device, inode, version=17):
    """Bounded same-snapshot schema bytes and physical identity; never a grant."""
    expected_version=version
    before=os.stat(path,follow_symlinks=False)
    if os.path.realpath(path)!=path or (before.st_dev,before.st_ino)!=(device,inode):
        raise ValueError('guarded database file changed')
    with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        row=db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
        version=db.execute('PRAGMA user_version').fetchone()[0]
        cookie=db.execute('PRAGMA schema_version').fetchone()[0]
        if row is None or row[0]!=database_id or version not in (17,18) or version!=expected_version or type(cookie) is not int or cookie<0:
            raise ValueError('guarded schema unavailable')
        schema_hash=hashlib.sha256();schema_hash.update(b'[');used=1;count=0
        for definition in db.execute("SELECT type,name,tbl_name,CASE WHEN sql IS NULL OR length(CAST(sql AS BLOB))<=16777216 THEN sql END,length(CAST(sql AS BLOB)) FROM sqlite_schema ORDER BY type,name LIMIT 100001"):
            count+=1
            if count>100000 or (definition[4] is not None and definition[4]>16*1024*1024):
                raise ValueError('guarded schema unavailable')
            encoded=json.dumps(definition[:4],ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
            used+=len(encoded)+(1 if count>1 else 0)
            if used+1>16*1024*1024: raise ValueError('guarded schema unavailable')
            if count>1: schema_hash.update(b',')
            schema_hash.update(encoded)
        schema_hash.update(b']');db.commit()
    after=os.stat(path,follow_symlinks=False)
    if os.path.realpath(path)!=path or (after.st_dev,after.st_ino)!=(device,inode):
        raise ValueError('guarded database file changed')
    return cookie,schema_hash.hexdigest()

@dataclass(frozen=True, init=False)
class GuardedStorePreparation:
    database_path: str
    database_id: str
    schema_version: int
    capabilities: object
    device: int
    inode: int
    schema_cookie: int | None
    _seal: object
    _schema_fingerprint: str | None
    _skill_draft_marker: object
    _skill_purge_marker: object
    _quota_purge_marker: object
    _vector_profiles_marker: object
    _document_vector_intent_marker: object
    _document_vector_batch_marker: object
    _document_structure_marker: object

    def __init__(self, path, database_id, capabilities, device, inode, seal, schema_cookie=None, schema_fingerprint=None, *, schema_version=17, skill_draft_marker=None, skill_purge_marker=None, quota_purge_marker=None, vector_profiles_marker=None, document_vector_intent_marker=None, document_vector_batch_marker=None, document_structure_marker=None):
        if seal is not _GUARDED_PREPARATION_SEAL:
            raise ValueError('host preparation required')
        object.__setattr__(self, 'database_path', path)
        object.__setattr__(self, 'database_id', database_id)
        if type(schema_version) is not int or schema_version not in (17,18): raise ValueError('guarded profile unavailable')
        object.__setattr__(self, 'schema_version', schema_version)
        object.__setattr__(self, 'capabilities', MappingProxyType(dict(capabilities)))
        object.__setattr__(self, 'device', device)
        object.__setattr__(self, 'inode', inode)
        if ((schema_cookie is None)!=(schema_fingerprint is None) or (schema_fingerprint is not None
            and (type(schema_fingerprint) is not str or len(schema_fingerprint)!=64
                 or any(c not in '0123456789abcdef' for c in schema_fingerprint)))):
            raise ValueError('guarded schema baseline unavailable')
        object.__setattr__(self, 'schema_cookie', schema_cookie)
        object.__setattr__(self, '_schema_fingerprint', schema_fingerprint)
        if skill_draft_marker not in (None, ('1',)): raise ValueError('skill draft profile unavailable')
        object.__setattr__(self, '_skill_draft_marker', skill_draft_marker)
        if skill_purge_marker not in (None, ('1',)) or (skill_purge_marker is not None and (schema_version!=18 or skill_draft_marker is None)): raise ValueError('skill purge profile unavailable')
        object.__setattr__(self, '_skill_purge_marker', skill_purge_marker)
        if quota_purge_marker not in (None, ('1',)) or (quota_purge_marker is not None and skill_purge_marker is None): raise ValueError('S05 quota profile unavailable')
        object.__setattr__(self, '_quota_purge_marker', quota_purge_marker)
        if vector_profiles_marker not in (None, ('1',)): raise ValueError('vector profile unavailable')
        object.__setattr__(self, '_vector_profiles_marker', vector_profiles_marker)
        if document_vector_intent_marker not in (None, ('1',)): raise ValueError('document vector intent profile unavailable')
        object.__setattr__(self, '_document_vector_intent_marker', document_vector_intent_marker)
        if document_vector_batch_marker not in (None, ('1',)) or (document_vector_batch_marker is not None and (schema_version!=18 or document_vector_intent_marker is None)): raise ValueError('document vector batch profile unavailable')
        object.__setattr__(self, '_document_vector_batch_marker', document_vector_batch_marker)
        if document_structure_marker not in (None, ('1',)) or (document_structure_marker is not None and schema_version != 18):
            raise ValueError('document structure profile unavailable')
        object.__setattr__(self, '_document_structure_marker', document_structure_marker)
        object.__setattr__(self, '_seal', seal)

    @property
    def document_structure(self):
        return self._document_structure_marker == ('1',)

    def assert_valid(self):
        if self._seal is not _GUARDED_PREPARATION_SEAL:
            raise ValueError('host preparation unavailable')
        stat = os.stat(self.database_path, follow_symlinks=False)
        if os.path.realpath(self.database_path) != self.database_path or (stat.st_dev, stat.st_ino) != (self.device, self.inode):
            raise ValueError('guarded database file changed')
        with closing(sqlite3.connect(Path(self.database_path).as_uri() + '?mode=ro', uri=True)) as connection:
            connection.execute('BEGIN')
            row = connection.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            cookie = connection.execute('PRAGMA schema_version').fetchone()[0]
            marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.assistant_interpretation.v1'").fetchone()
            account_chat_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.s05.account_chat.v1'").fetchone()
            document_fts_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.document_fts.v1'").fetchone()
            skill_draft_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.s05.skill_draft.v1'").fetchone()
            skill_purge_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.s05.skill_purge.v1'").fetchone()
            quota_purge_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.s05.quota_purge.v1'").fetchone()
            vector_profiles_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.vector_profiles.v1'").fetchone()
            document_vector_intent_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.document_vector_build_intent.v1'").fetchone()
            document_vector_batch_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.document_vector_batch.v2'").fetchone()
            document_structure_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.document_structure.v1'").fetchone()
        after = os.stat(self.database_path, follow_symlinks=False)
        paired = self.capabilities.get('assistant_interpretation')
        if (os.path.realpath(self.database_path) != self.database_path or (after.st_dev, after.st_ino) != (self.device, self.inode)
            or row is None or row[0] != self.database_id or version != self.schema_version
            or (self.schema_cookie is not None and (cookie != self.schema_cookie or type(paired) is not bool
                or marker != (('1',) if paired else None)
                or account_chat_marker != (('1',) if self.capabilities.get('account_chat') is True else None)
                or document_fts_marker != (('1',) if self.capabilities.get('document_fts') is True else None)
                or skill_draft_marker != self._skill_draft_marker
                or skill_purge_marker != self._skill_purge_marker
                or quota_purge_marker != self._quota_purge_marker
                or vector_profiles_marker != self._vector_profiles_marker
                or document_vector_intent_marker != self._document_vector_intent_marker
                or document_vector_batch_marker != self._document_vector_batch_marker
                or document_structure_marker != self._document_structure_marker))):
            raise ValueError('guarded database changed')
        if self.schema_version == 18:
            observed_cookie, digest = _guarded_schema_snapshot(self.database_path, self.database_id, self.device, self.inode, self.schema_version)
            if observed_cookie != self.schema_cookie or digest != self._schema_fingerprint:
                raise ValueError('guarded schema changed')

    def assert_account_chat(self):
        if self.capabilities.get('account_chat') is not True:
            raise ValueError('account chat profile unavailable')
        self.assert_valid()


    def _owned_move_schema_cookie(self):
        """Verify the original physical identity and full schema, without adopting a stamp."""
        if self._seal is not _GUARDED_PREPARATION_SEAL or type(self.schema_cookie) is not int or not self._schema_fingerprint:
            raise ValueError('guarded schema baseline unavailable')
        cookie,digest=_guarded_schema_snapshot(self.database_path,self.database_id,self.device,self.inode,self.schema_version)
        if digest!=self._schema_fingerprint: raise ValueError('guarded schema changed')
        return cookie

    def _accept_owned_move_stamp(self, fact, *, request, receipt, capability, provider):
        """Adopt only the Native-owned stamp for this original claimed receipt."""
        from src.memery_provider import _HostOwnerTransition, _HostOwnerPurge
        from src.openclank.chat_source_bridge import HostAccountSourceTransition
        fields={'version','receipt_id','receipt_hash','database_id','before_cookie','after_cookie','schema_sha256'}
        if (type(request) not in (_HostOwnerTransition, _HostOwnerPurge) or type(capability) is not HostAccountSourceTransition
            or provider._guarded_preparation is not self or request.database_id!=self.database_id
            or type(receipt) is not dict or receipt.get('state')!='applied'
            or type(fact) is not dict or set(fact)!=fields or type(fact['version']) is not int or fact['version']!=1
            or fact['receipt_id']!=receipt.get('receipt_id') or fact['receipt_hash']!=receipt.get('receipt_hash')
            or fact['database_id']!=self.database_id or fact['schema_sha256']!=self._schema_fingerprint
            or type(fact['before_cookie']) is not int or fact['before_cookie']<0
            or type(fact['after_cookie']) is not int or fact['after_cookie']<fact['before_cookie']):
            raise ValueError('owned move stamp unavailable')
        old=self.schema_cookie
        if type(old) is not int or old not in (fact['before_cookie'],fact['after_cookie']):
            raise ValueError('owned move stamp changed')
        scope = {'provider':provider, 'action':'rename', 'owner':request.owner, 'new_owner':request.new_owner} if type(request) is _HostOwnerTransition else {'provider':provider, 'action':'purge', 'owner':request.owner}
        capability.assert_current(**scope)
        if self._owned_move_schema_cookie()!=fact['after_cookie']:
            raise ValueError('owned move schema changed')
        capability.assert_current(**scope)
        if provider._guarded_preparation is not self or self.schema_cookie!=old:
            raise ValueError('owned move preparation changed')
        if old!=fact['after_cookie']: object.__setattr__(self,'schema_cookie',fact['after_cookie'])
        self.assert_valid()



    def _accept_owned_reset_stamp(self, fact, *, frozen, receipt, claim, provider, current):
        """Adopt the exact committed reset's Native stamp under its original claim."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.openclank.fm_reset_receipt import _identity, _json, read_committed_reset_receipt
        fields={'version','receipt_id','receipt_hash','database_id','before_cookie','after_cookie','schema_sha256'}
        if (type(claim) is not MemoryResetClaim or provider._guarded_preparation is not self
            or type(frozen) is not dict or set(frozen)!={'version','database','request'}
            or type(frozen.get('request')) is not dict or frozen.get('database')!=_identity(self)
            or not callable(current) or current() is not True
            or claim.assert_current_post_dispatch(provider=provider,frozen=frozen) is not True
            or type(receipt) is not dict or receipt.get('state')!='committed'
            or read_committed_reset_receipt(self,frozen,current=current)!=receipt
            or type(fact) is not dict or set(fact)!=fields or type(fact['version']) is not int or fact['version']!=1
            or fact['database_id']!=self.database_id or fact['schema_sha256']!=self._schema_fingerprint
            or type(fact['before_cookie']) is not int or fact['before_cookie']<0
            or type(fact['after_cookie']) is not int or fact['after_cookie']<fact['before_cookie']):
            raise ValueError('owned reset stamp unavailable')
        request=frozen['request']
        expected_id='cs_reset_'+hashlib.sha256(_json([request['account_id'],request['operation_id']])).hexdigest()
        if fact['receipt_id']!=expected_id:
            raise ValueError('owned reset receipt changed')
        with closing(sqlite3.connect(Path(self.database_path).as_uri()+'?mode=ro',uri=True,timeout=0.25)) as db:
            rows=db.execute("SELECT content_hash,operation_metadata_json FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND reason='s06.guarded-reset.v1' AND length(CAST(operation_metadata_json AS BLOB))<=262144 LIMIT 2",(request['owner'],expected_id)).fetchall()
        if (len(rows)!=1 or type(rows[0][0]) is not str or type(rows[0][1]) is not str
            or rows[0][0]!=fact['receipt_hash'] or hashlib.sha256(rows[0][1].encode('utf-8')).hexdigest()!=fact['receipt_hash']):
            raise ValueError('owned reset receipt changed')
        old=self.schema_cookie
        if (type(old) is not int or old not in (fact['before_cookie'],fact['after_cookie'])
            or self._owned_move_schema_cookie()!=fact['after_cookie'] or current() is not True
            or claim.assert_current_post_dispatch(provider=provider,frozen=frozen) is not True
            or read_committed_reset_receipt(self,frozen,current=current)!=receipt
            or provider._guarded_preparation is not self or self.schema_cookie!=old):
            raise ValueError('owned reset schema changed')
        if old!=fact['after_cookie']: object.__setattr__(self,'schema_cookie',fact['after_cookie'])
        self.assert_valid()


def _native_guarded_prepare(path, database_id, profile, command, trusted_env):
    env = dict(os.environ)
    if trusted_env is not None:
        env.update(trusted_env)
    env.update(OPEN_CLANK_MEMERY_DB_PATH=path, FM_DB_PATH=path, FM_DB_ID=database_id,
               FM_STORE_PROFILE=profile, FM_HOST_PRIVATE_REVIEW='1')
    env.pop('FM_AGENT_BRIDGE', None)
    result = subprocess.run([command if command is not None else resolve_memery_worker().path, '--prepare-database'],
                            env=env, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError('guarded database preparation unavailable')
    return json.loads(result.stdout)


def _recover_guarded_journal(path, original, expected, version, prepare):
    """Let SQLite's native guarded owner finish rollback before host read-only projections."""
    import fcntl

    def owned_file(info):
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                info.st_nlink == 1 and not info.st_mode & 0o022)

    parent = os.stat(os.path.dirname(path), follow_symlinks=False)
    journal = os.stat(path + '-journal', follow_symlinks=False)
    if (not owned_file(original) or not owned_file(journal) or journal.st_dev != original.st_dev
            or parent.st_uid != os.geteuid() or parent.st_mode & 0o022):
        raise RuntimeError('guarded journal ownership unavailable')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path + '.memery-worker.lock', flags), 'rb') as custody:
        if not owned_file(os.fstat(custody.fileno())):
            raise RuntimeError('guarded worker ownership unavailable')
        fcntl.flock(custody, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not expected:
            with os.fdopen(os.open(path + '.prepare-receipt.json', flags), 'rb') as witness:
                witness_info = os.fstat(witness.fileno())
                if not owned_file(witness_info) or witness_info.st_size > 65536:
                    raise RuntimeError('guarded preparation receipt unavailable')
                record = json.loads(witness.read(65537))
                if record.get('schema_version') != version:
                    raise RuntimeError('guarded preparation receipt differs')
                expected = record.get('database_id')
        if type(expected) is not str or not expected or expected.strip() != expected:
            raise RuntimeError('guarded database identity unavailable')
        if not os.path.samestat(original, os.stat(path, follow_symlinks=False)):
            raise RuntimeError('guarded database changed before recovery')
        return prepare(expected)


def prepare_guarded_memery_database(database_path: str, *, command: str | None = None, expected_database_id: str | None = None, trusted_env: dict[str, str] | None = None, profile: str = 'guarded-v17') -> GuardedStorePreparation:
    if profile not in ('guarded-v17','guarded-v18'): raise RuntimeError('guarded profile unavailable')
    expected_version = 17 if profile == 'guarded-v17' else 18
    path = os.path.realpath(os.path.abspath(os.path.expanduser(database_path)))
    if not __import__('sys').platform.startswith('linux'):
        raise RuntimeError('guarded file identity unsupported')
    original = os.stat(path, follow_symlinks=False)
    if expected_database_id is not None and (type(expected_database_id) is not str or not expected_database_id or expected_database_id.strip() != expected_database_id):
        raise RuntimeError('guarded database identity mismatch')
    if command is not None and (type(command) is not str or not command or '\x00' in command):
        raise RuntimeError('guarded database preparation unavailable')
    if trusted_env is not None and (type(trusted_env) is not dict or any(type(k) is not str or type(v) is not str for k,v in trusted_env.items())):
        raise RuntimeError('guarded database preparation unavailable')
    expected = expected_database_id if expected_database_id is not None else os.environ.get('FM_DB_ID', '').strip()
    def prepare(database_id):
        return _native_guarded_prepare(path, database_id, profile, command, trusted_env)

    receipt = None
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as connection:
        try:
            row = connection.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_READONLY_ROLLBACK or path != os.path.abspath(os.path.expanduser(database_path)):
                raise
            receipt = _recover_guarded_journal(path, original, expected, expected_version, prepare)
            row = connection.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        skill_draft_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.s05.skill_draft.v1'").fetchone()
        skill_purge_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.s05.skill_purge.v1'").fetchone()
        quota_purge_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.s05.quota_purge.v1'").fetchone()
        vector_profiles_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.vector_profiles.v1'").fetchone()
        document_vector_intent_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.document_vector_build_intent.v1'").fetchone()
        document_vector_batch_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.document_vector_batch.v2'").fetchone()
        document_structure_marker = connection.execute("SELECT value FROM fm_meta WHERE key='openclank.document_structure.v1'").fetchone()
    if skill_draft_marker not in (None, ('1',)): raise RuntimeError('skill draft profile unavailable')
    if skill_purge_marker not in (None, ('1',)) or (skill_purge_marker is not None and (expected_version!=18 or skill_draft_marker is None)): raise RuntimeError('skill purge profile unavailable')
    if quota_purge_marker not in (None, ('1',)) or (quota_purge_marker is not None and skill_purge_marker is None): raise RuntimeError('S05 quota profile unavailable')
    if vector_profiles_marker not in (None, ('1',)) or (vector_profiles_marker is not None and expected_version!=18): raise RuntimeError('vector profile unavailable')
    if document_vector_intent_marker not in (None, ('1',)) or (document_vector_intent_marker is not None and expected_version!=18): raise RuntimeError('document vector intent profile unavailable')
    if document_vector_batch_marker not in (None, ('1',)) or (document_vector_batch_marker is not None and (expected_version!=18 or document_vector_intent_marker is None)): raise RuntimeError('document vector batch profile unavailable')
    if not row or not isinstance(row[0], str) or not row[0] or version != expected_version:
        raise RuntimeError('guarded database identity unavailable')
    if expected and expected != row[0]:
        raise RuntimeError('guarded database identity mismatch')
    if receipt is None:
        receipt = prepare(row[0])
    caps = receipt.get('capabilities') if isinstance(receipt, dict) else None
    if (set(receipt) != {'database_id','schema_version','database_path','profile','device','inode','schema_cookie','capabilities'}
        or receipt['database_id'] != row[0] or receipt['schema_version'] != expected_version
        or receipt['database_path'] != path or receipt['profile'] != profile
        or type(receipt['device']) is not int or type(receipt['inode']) is not int or type(receipt['schema_cookie']) is not int or receipt['schema_cookie'] < 0
        or (receipt['device'], receipt['inode']) != (original.st_dev, original.st_ino)
        or not isinstance(caps, dict) or set(caps) not in ({'guarded_review','source_lifecycle','feedback','admitted','source_erase','owner_transition','assistant_interpretation'}, {'guarded_review','source_lifecycle','feedback','admitted','source_erase','owner_transition','assistant_interpretation','account_chat'}, {'guarded_review','source_lifecycle','feedback','admitted','source_erase','owner_transition','assistant_interpretation','account_chat','document_fts'}, {'guarded_review','source_lifecycle','feedback','admitted','source_erase','owner_transition','assistant_interpretation','account_chat','document_fts','document_import_v1'})
        or ('document_import_v1' in caps and (type(caps['document_import_v1']) is not bool or caps['document_import_v1'] != caps['document_fts']))
        or ('document_fts' in caps and type(caps['document_fts']) is not bool)
        or ('account_chat' in caps and type(caps['account_chat']) is not bool)
        or caps['guarded_review'] is not True or caps['source_lifecycle'] is not True
        or caps['owner_transition'] is not True
        or type(caps['feedback']) is not bool or type(caps['admitted']) is not bool
        or type(caps['source_erase']) is not bool or type(caps['assistant_interpretation']) is not bool
        or ((caps['admitted'] or caps['source_erase']) and not caps['feedback'])):
        raise RuntimeError('invalid guarded database preparation receipt')
    cookie, schema_fingerprint = _guarded_schema_snapshot(path, row[0], original.st_dev, original.st_ino, expected_version)
    if cookie != receipt['schema_cookie']:
        raise RuntimeError('guarded schema changed after preparation')
    prepared = GuardedStorePreparation(path, row[0], caps, original.st_dev, original.st_ino, _GUARDED_PREPARATION_SEAL, cookie, schema_fingerprint, schema_version=expected_version, skill_draft_marker=skill_draft_marker, skill_purge_marker=skill_purge_marker, quota_purge_marker=quota_purge_marker, vector_profiles_marker=vector_profiles_marker, document_vector_intent_marker=document_vector_intent_marker, document_vector_batch_marker=document_vector_batch_marker, document_structure_marker=document_structure_marker)
    prepared.assert_valid()
    return prepared


# ---- First-start default profile resolution (decision G-DEF-1/2/3) -------------------------------------------------
#
# The ONE place that decides the memory store profile for a given FM_DB_PATH. Every call site that used to read
# OPENCLANK_MEMORY_STORE_PROFILE on its own now calls resolve_memory_store_profile() once (src/app_initializer.py,
# src/rag_singleton.py); the resolver's only side effect is writing the decided value back into
# os.environ['OPENCLANK_MEMORY_STORE_PROFILE'] (the existing variable every other site already reads), so no other
# call site needs to change.

_KNOWN_PROFILES = ('legacy-v16', 'guarded-v17', 'guarded-v18')
_STORE_COMPANION_SUFFIXES = ('-wal', '-shm', '-journal', '.receipt')

# All 18 layer flags the migrator's --prepare-guarded-v17 accepts. A fresh store gets every optional capability
# because none can be added later without another stopped-writer copy (verified recipe, PREP-GDEF section 1).
FRESH_GUARDED_LAYER_FLAGS = (
    '--feedback', '--s08', '--s08-model', '--s08-model-v2', '--maintenance', '--policy-projection', '--queued-source',
    '--s09', '--domain-metadata', '--s05-account-v4', '--s02-policy', '--s02-policy-v2', '--knowledge-ref',
    '--assistant-interpretation', '--assistant-interpretation-pending', '--general-hex', '--s05-account-chat',
    '--document-fts',
)


def _resolve_migrator_command(fm_command: str) -> str:
    """The migrator ships as the worker’s sibling (decision 3); FM_MIGRATE_COMMAND overrides, mirroring OPEN_CLANK_MEMERY_WORKER."""
    override = (os.environ.get('OPEN_CLANK_MEMERY_MIGRATOR') or os.environ.get('FM_MIGRATE_COMMAND', '')).strip()
    if override:
        return override
    base = Path(fm_command)
    return str(base.with_name('memery-migrate' + base.suffix))


def _store_companions_present(path: str) -> bool:
    return any(os.path.exists(path + suffix) for suffix in _STORE_COMPANION_SUFFIXES)


def _detect_existing_profile(path: str) -> str:
    """Read-only PRAGMA user_version of an EXISTING file; never writes, never converts (owner decision)."""
    try:
        with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
    except sqlite3.Error as error:
        raise RuntimeError(f'memory store profile unavailable: cannot read {path}: {error}') from error
    if version == 16:
        return 'legacy-v16'
    if version == 17:
        return 'guarded-v17'
    if version == 18:
        return 'guarded-v18'
    raise RuntimeError(f'memory store profile unavailable: unexpected schema version {version} at {path}')


def _run_migrator_step(args: list[str], *, step: str) -> None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)
    except OSError as error:
        raise RuntimeError(f'memory store preparation failed at {step}: {error}') from error
    if result.returncode:
        raise RuntimeError(f'memory store preparation failed at {step}: {result.stderr.strip()[-2000:]}')


def _prepare_fresh_guarded_v18(path: str, *, command: str | None = None) -> None:
    """First-start preparation (decision 2): build in a private staging dir next to the target, never at the final
    path; publish with a no-overwrite primitive; never touch an existing file at ``path``."""
    import fcntl
    command = command or resolve_memery_worker().path
    migrator = _resolve_migrator_command(command)
    if not os.path.isfile(migrator) or not os.access(migrator, os.X_OK):
        raise RuntimeError(f'memory store preparation failed: migrator executable is missing at {migrator}')
    # This is a brand-new database identity; an FM_DB_ID left over in-process from an earlier store (a different
    # profile, a prior process lifetime) must never gate it.
    os.environ.pop('FM_DB_ID', None)
    data_dir = os.path.dirname(path) or '.'
    os.makedirs(data_dir, exist_ok=True)
    lock_fd = os.open(path + '.prepare.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if os.path.exists(path):
            return  # a concurrent process (or the same one, re-entered) already finished
        # A crashed earlier attempt leaves only a staging dir of this naming pattern; the next start under the
        # lock removes it before trying again.
        for entry in os.listdir(data_dir):
            if entry.startswith('.prepare-'):
                shutil.rmtree(os.path.join(data_dir, entry), ignore_errors=True)
        staging = tempfile.mkdtemp(prefix='.prepare-', dir=data_dir)
        started = time.monotonic()
        try:
            src16 = os.path.join(staging, 'source-v16.sqlite')
            g17 = os.path.join(staging, 'guarded-v17.sqlite')
            g18 = os.path.join(staging, 'guarded-v18.sqlite')
            bak_v18_step = os.path.join(staging, 'backup-before-v18.sqlite')
            # Step 1: empty v16 store (legacy profile, never FM_STORE_PROFILE). This is a brand-new database, so
            # no inherited FM_DB_ID expectation applies either.
            env = dict(os.environ, OPEN_CLANK_MEMERY_DB_PATH=src16, FM_DB_PATH=src16)
            env.pop('FM_STORE_PROFILE', None)
            env.pop('FM_DB_ID', None)
            try:
                result = subprocess.run([command, '--prepare-database'], env=env, capture_output=True, text=True,
                                         timeout=60, check=False)
            except OSError as error:
                raise RuntimeError(f'memory store preparation failed at create-v16: {error}') from error
            if result.returncode:
                raise RuntimeError(f'memory store preparation failed at create-v16: {result.stderr.strip()[-2000:]}')
            # Step 2: non-WAL journal (step 3 fails on a WAL source) + principal companion (needed before step 3 or
            # the migrator adds no principal writer fence, PREP-GDEF section 1).
            with closing(sqlite3.connect(src16)) as connection:
                connection.execute('PRAGMA journal_mode=DELETE')
                from src.openclank.memory_optional_schema import apply_optional_schema
                apply_optional_schema(connection, 'principal')
                connection.commit()
            # Step 3: migrator v16 -> guarded-v17, every optional layer (decision 2: layers cannot be added later).
            _run_migrator_step(
                [migrator, '--prepare-guarded-v17', src16, src16 + '.bak17', g17, '--writers-stopped',
                 *FRESH_GUARDED_LAYER_FLAGS],
                step='guarded-v17')
            # Step 4: migrator v17 -> guarded-v18.
            _run_migrator_step([migrator, '--prepare-guarded-v18', g17, bak_v18_step, g18, '--writers-stopped',
                                '--document-structure'], step='guarded-v18')
            # Step 5: the same check boot would run; also records device/inode/schema_cookie for the receipt.
            prepared = prepare_guarded_memery_database(g18, command=command, profile='guarded-v18')
            prepared.assert_valid()
            if prepared.document_structure is not True:
                raise RuntimeError('fresh document structure profile unavailable')
            # Publish: os.link (never clobbers an existing name) then drop the staging name.
            try:
                os.link(g18, path)
            except FileExistsError:
                return  # the path appeared meanwhile; leave it untouched, it is a complete store
            os.unlink(g18)
            try:
                receipt = {
                    'version': 1, 'prepared_at': time.time(), 'elapsed_seconds': round(time.monotonic() - started, 3),
                    'database_id': prepared.database_id, 'schema_version': prepared.schema_version,
                    'layer_flags': list(FRESH_GUARDED_LAYER_FLAGS), 'capabilities': dict(prepared.capabilities),
                    'binaries': {'memery_worker_sha256': hashlib.sha256(Path(command).read_bytes()).hexdigest(),
                                 'migrator_sha256': hashlib.sha256(Path(migrator).read_bytes()).hexdigest()},
                }
                with open(path + '.prepare-receipt.json', 'w', encoding='utf-8') as handle:
                    json.dump(receipt, handle)
            except OSError:
                pass  # the receipt is diagnostic only; a missing one never blocks the prepared store
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def resolve_memory_store_profile(database_path: str | None = None) -> str:
    """Decide the memory store profile for FM_DB_PATH, preparing a fresh guarded-v18 store on first start when the
    owner's rule selects one (frozen decision, 2026-11-10): an existing store's profile is detected and NEVER
    converted; a fresh install defaults to guarded on Linux. Caches the result in
    os.environ['OPENCLANK_MEMORY_STORE_PROFILE'] so every other site's existing env read follows unchanged."""
    if database_path is None:
        from src.constants import FM_DB_PATH
        database_path = FM_DB_PATH
    path = os.path.abspath(os.path.expanduser(database_path))

    configured = os.environ.get('OPENCLANK_MEMORY_STORE_PROFILE')
    if configured is not None:
        if configured not in _KNOWN_PROFILES:
            raise RuntimeError('unknown memory store profile')
        if configured in ('guarded-v17', 'guarded-v18') and not os.path.exists(path) and not _store_companions_present(path):
            _prepare_fresh_guarded_v18(path)
            os.environ['OPENCLANK_MEMORY_STORE_PROFILE'] = 'guarded-v18'
            return 'guarded-v18'
        return configured

    if not sys.platform.startswith('linux'):
        profile = 'legacy-v16'
    elif not os.path.exists(path):
        if _store_companions_present(path):
            raise RuntimeError(f'memory store profile unavailable: stray companion file(s) at {path} with no main database')
        _prepare_fresh_guarded_v18(path)
        profile = 'guarded-v18'
    else:
        profile = _detect_existing_profile(path)
    os.environ['OPENCLANK_MEMORY_STORE_PROFILE'] = profile
    return profile
