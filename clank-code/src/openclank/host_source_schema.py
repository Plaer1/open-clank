"""Explicit offline host source bridge schema. Run only after quiescence/backup.

No legacy rows are baselined: a separate verified archive/current-host baseline
is necessary before those messages can be edited through the bridge.
"""
import sqlite3


RESET_ROOT_INDEX_SQL = "CREATE INDEX host_source_reset_roots ON host_source_delivery_intent(owner,chat_id,message_id) WHERE part_id='body' AND revision=1 AND origin_json IS NOT NULL"
RESET_ORPHAN_INDEX_SQL = "CREATE INDEX host_source_reset_orphans ON host_source_delivery_intent(owner,chat_id,message_id) WHERE part_id='body' AND revision=1 AND origin_json IS NULL AND origin_state!='none'"



ASSISTANT_REVERSE_INDEX_SQL = "CREATE INDEX host_source_assistant_reverse ON host_source_delivery_intent(owner,chat_id,origin_assistant_id,message_id) WHERE part_id='body' AND revision=1 AND origin_completed=1"

def prepare_assistant_reverse_index_copy(source_path, destination_path, *, writers_stopped):
    """Prepare a distinct offline Host copy; never mutate a live database."""
    from pathlib import Path
    from urllib.parse import quote
    import os
    source, destination = Path(source_path).resolve(), Path(destination_path).resolve()
    if writers_stopped is not True or source == destination or destination.exists():
        raise ValueError('offline distinct copy required')
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    src = sqlite3.connect('file:' + quote(str(source), safe='/') + '?mode=ro', uri=True)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone() != ('ok',) or dst.execute('SELECT version FROM host_source_schema_marker').fetchall() != [(3,)]:
            raise ValueError('Host source profile unavailable')
        dst.execute(ASSISTANT_REVERSE_INDEX_SQL)
        dst.commit()
    finally:
        src.close()
        dst.close()

def install_reset_root_index(path):
    """Explicit offline opt-in; no source data or schema marker is changed."""
    from pathlib import Path
    import re
    from urllib.parse import quote
    db = sqlite3.connect('file:' + quote(str(Path(path).resolve()), safe='/') + '?mode=rw', uri=True, isolation_level=None)
    try:
        db.execute('BEGIN EXCLUSIVE')
        if db.execute('SELECT version FROM host_source_schema_marker').fetchall() != [(3,)]:
            raise ValueError('host source schema version unavailable')
        for name, expected in (('host_source_reset_roots', RESET_ROOT_INDEX_SQL),('host_source_reset_orphans', RESET_ORPHAN_INDEX_SQL)):
            previous = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(name,)).fetchone()
            if previous is None:
                db.execute(expected)
            elif ''.join(part if index % 2 else ''.join(part.split()).lower() for index,part in enumerate(re.split(r"('(?:''|[^'])*')",previous[0]))) != ''.join(part if index % 2 else ''.join(part.split()).lower() for index,part in enumerate(re.split(r"('(?:''|[^'])*')",expected))):
                raise ValueError('host reset index mismatch')
        db.commit()
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def install_assistant_reverse_index(path):
    """Install the existing reverse-lookup index on a quiesced prepared copy."""
    import re
    from pathlib import Path
    from urllib.parse import quote
    db = sqlite3.connect('file:' + quote(str(Path(path).resolve()), safe='/') + '?mode=rw', uri=True, isolation_level=None)
    try:
        db.execute('BEGIN EXCLUSIVE')
        if db.execute('SELECT version FROM host_source_schema_marker').fetchall() != [(3,)]:
            raise ValueError('host source schema version unavailable')
        previous = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='host_source_assistant_reverse'").fetchone()
        shape = lambda sql: ''.join(part if i % 2 else ''.join(part.split()).lower() for i, part in enumerate(re.split(r"('(?:''|[^'])*')", sql or '')))
        if previous is None:
            db.execute(ASSISTANT_REVERSE_INDEX_SQL)
        elif shape(previous[0]) != shape(ASSISTANT_REVERSE_INDEX_SQL):
            raise ValueError('host assistant reverse index mismatch')
        db.commit()
    except BaseException:
        if db.in_transaction: db.rollback()
        raise
    finally:
        db.close()


def convert(path):
    db = sqlite3.connect(path, isolation_level=None)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'chat_messages' not in tables or tables.intersection({'host_source_schema_marker', 'host_source_delivery_intent'}):
            raise ValueError('host schema unavailable or already converted')
        db.executescript('''BEGIN EXCLUSIVE;
ALTER TABLE chat_messages ADD COLUMN source_body_revision INTEGER;
ALTER TABLE chat_messages ADD COLUMN source_body_hash TEXT;
CREATE TABLE host_source_schema_marker(version INTEGER NOT NULL CHECK(version=1));
INSERT INTO host_source_schema_marker VALUES(1);
CREATE TABLE host_source_delivery_intent(owner TEXT NOT NULL, chat_id TEXT NOT NULL, message_id TEXT NOT NULL, part_id TEXT NOT NULL, revision INTEGER NOT NULL, content_hash TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','delivered','superseded')), PRIMARY KEY(owner,chat_id,message_id,part_id,revision));
COMMIT;''')
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def baseline_body(host_path, archive_path, *, owner_account_id, chat_id, message_id):
    """Offline only: bind one legacy host body to its unique existing archive part.

    Both stores must be quiesced and backed up. No privacy admission is created.
    The caller must supply the immutable account ID from the existing lifecycle
    authority, not infer it from a user-facing alias.
    """
    import hashlib
    from urllib.parse import quote
    if not all(isinstance(v, str) and v for v in (owner_account_id, chat_id, message_id)):
        raise ValueError('exact source identity required')
    host = sqlite3.connect(host_path, isolation_level=None)
    archive = sqlite3.connect('file:' + quote(str(__import__('pathlib').Path(archive_path).resolve()), safe='/') + '?mode=ro', uri=True)
    try:
        host.execute('BEGIN EXCLUSIVE')
        if host.execute('SELECT version FROM host_source_schema_marker').fetchall() != [(1,)]:
            raise ValueError('host source schema unavailable')
        row = host.execute('SELECT m.content,m.source_body_revision,m.source_body_hash,s.owner FROM chat_messages m JOIN sessions s ON s.id=m.session_id WHERE m.id=? AND m.session_id=?', (message_id, chat_id)).fetchone()
        if not row or row[1] is not None or row[2] is not None or not isinstance(row[0], str):
            raise ValueError('host source baseline unavailable')
        mapped = archive.execute('SELECT account_id FROM conversation_archive_owner_aliases WHERE owner_alias=?', (row[3],)).fetchone()
        if not mapped or mapped[0] != owner_account_id:
            raise ValueError('host owner identity unavailable')
        if archive.execute('SELECT 1 FROM conversation_archive_deleted_chats WHERE owner=? AND chat_id=?', (owner_account_id, chat_id)).fetchone():
            raise ValueError('source unavailable')
        alias = archive.execute("SELECT chat_id,message_id,part_id,revision FROM conversation_part_aliases WHERE owner=? AND alias_kind='host_message' AND alias_id=?", (owner_account_id, message_id)).fetchall()
        if len(alias) != 1 or alias[0][:3] != (chat_id, message_id, 'body'):
            raise ValueError('source alias unavailable or ambiguous')
        rows = archive.execute("SELECT actor_id,runtime_generation,revision,content_hash,content,tombstone FROM conversation_parts WHERE owner=? AND chat_id=? AND message_id=? AND part_id='body'", (owner_account_id, chat_id, message_id)).fetchall()
        if not rows or any(r[0] != 'main' or r[1] != '0' for r in rows):
            raise ValueError('source provenance unavailable or ambiguous')
        current = max(rows, key=lambda r: r[2])
        digest = hashlib.sha256(row[0].encode('utf-8')).hexdigest()
        if current[2] != alias[0][3] or current[3] != digest or current[4] != row[0] or current[5]:
            raise ValueError('source revision unavailable or changed')
        updated = host.execute('UPDATE chat_messages SET source_body_revision=?,source_body_hash=? WHERE id=? AND session_id=? AND content=? AND source_body_revision IS NULL AND source_body_hash IS NULL', (current[2], digest, message_id, chat_id, row[0]))
        if updated.rowcount != 1:
            raise ValueError('host source changed')
        host.commit()
        return {'revision': current[2], 'privacy': 'unknown'}
    except BaseException:
        if host.in_transaction:
            host.rollback()
        raise
    finally:
        archive.close()
        host.close()


def upgrade_origin_producer(path):
    """Explicit offline opt-in to immutable user-origin intent evidence."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        if db.execute('SELECT version FROM host_source_schema_marker').fetchall() != [(1,)]:
            raise ValueError('host source schema version unavailable')
        db.executescript('''BEGIN EXCLUSIVE;
ALTER TABLE host_source_delivery_intent ADD COLUMN origin_json TEXT;
ALTER TABLE host_source_delivery_intent ADD COLUMN origin_completed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE host_source_delivery_intent ADD COLUMN origin_assistant_id TEXT;
ALTER TABLE host_source_delivery_intent ADD COLUMN origin_state TEXT NOT NULL DEFAULT 'none';
DROP TABLE host_source_schema_marker;
CREATE TABLE host_source_schema_marker(version INTEGER NOT NULL CHECK(version=2));
INSERT INTO host_source_schema_marker VALUES(2);
COMMIT;''')
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def upgrade_execution_snapshot(path):
    """Offline opt-in: add nullable main execution evidence to host origins."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        if db.execute('SELECT version FROM host_source_schema_marker').fetchall() != [(2,)]:
            raise ValueError('host source schema version unavailable')
        db.executescript('''BEGIN EXCLUSIVE;
ALTER TABLE host_source_delivery_intent ADD COLUMN execution_snapshot_json TEXT;
DROP TABLE host_source_schema_marker;
CREATE TABLE host_source_schema_marker(version INTEGER NOT NULL CHECK(version=3));
INSERT INTO host_source_schema_marker VALUES(3);
COMMIT;''')
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def initialize_fresh_connection(connection):
    """Install the current empty Host source profile inside the caller's fresh transaction.

    The core initializer owns the exclusive same-connection empty-store proof.
    This function creates schema only and never backfills source evidence.
    """
    if connection.dialect.name != 'sqlite':
        raise ValueError('fresh Host source profile requires SQLite')
    names = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_schema WHERE name IN ('host_source_schema_marker','host_source_delivery_intent')")}
    if names or connection.exec_driver_sql('SELECT 1 FROM chat_messages LIMIT 1').first() is not None:
        raise ValueError('fresh Host source profile requires an empty source')
    statements = (
        'ALTER TABLE chat_messages ADD COLUMN source_body_revision INTEGER',
        'ALTER TABLE chat_messages ADD COLUMN source_body_hash TEXT',
        'CREATE TABLE host_source_schema_marker(version INTEGER NOT NULL CHECK(version=3))',
        'INSERT INTO host_source_schema_marker VALUES(3)',
        "CREATE TABLE host_source_delivery_intent(owner TEXT NOT NULL, chat_id TEXT NOT NULL, message_id TEXT NOT NULL, part_id TEXT NOT NULL, revision INTEGER NOT NULL, content_hash TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','delivered','superseded')), PRIMARY KEY(owner,chat_id,message_id,part_id,revision))",
        'ALTER TABLE host_source_delivery_intent ADD COLUMN origin_json TEXT',
        'ALTER TABLE host_source_delivery_intent ADD COLUMN origin_completed INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE host_source_delivery_intent ADD COLUMN origin_assistant_id TEXT',
        "ALTER TABLE host_source_delivery_intent ADD COLUMN origin_state TEXT NOT NULL DEFAULT 'none'",
        'ALTER TABLE host_source_delivery_intent ADD COLUMN execution_snapshot_json TEXT',
        RESET_ROOT_INDEX_SQL, RESET_ORPHAN_INDEX_SQL, ASSISTANT_REVERSE_INDEX_SQL,
    )
    for statement in statements:
        connection.exec_driver_sql(statement)


def require_current_connection(connection):
    """Verify exact current Host source schema without modifying the store."""
    import re
    if connection.dialect.name != 'sqlite':
        raise ValueError('Host source profile requires SQLite')
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE chat_messages(id TEXT)')
        class Template:
            class dialect: name = 'sqlite'
            def exec_driver_sql(self, sql):
                cursor = db.execute(sql)
                class Result:
                    def __iter__(self): return iter(cursor)
                    def first(self): return cursor.fetchone()
                return Result()
        template = Template()
        initialize_fresh_connection(template)
        initialize_fresh_transcript_search(template)
        trigger_query = "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='chat_messages' ORDER BY name"
        expected_triggers = db.execute(trigger_query).fetchall()
        query = ("SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND "
                 "(name LIKE 'host_source_%' OR (type='trigger' AND tbl_name='host_source_delivery_intent')) ORDER BY type,name")
        expected = db.execute(query).fetchall()
    actual = [tuple(row) for row in connection.exec_driver_sql(query)]
    actual_triggers = [tuple(row) for row in connection.exec_driver_sql(trigger_query)]
    shape = lambda sql: ''.join(part if i % 2 else ''.join(part.split()).lower() for i,part in enumerate(re.split(r"('(?:''|[^'])*')",sql or '')))
    if (len(actual_triggers) != len(expected_triggers)
            or any(a[0] != b[0] or shape(a[1]) != shape(b[1]) for a,b in zip(actual_triggers,expected_triggers))):
        raise ValueError('Host message trigger profile unavailable')
    if (len(actual) != len(expected) or any(a[:2] != b[:2] or shape(a[2]) != shape(b[2]) for a,b in zip(actual,expected))
            or [tuple(row) for row in connection.exec_driver_sql('SELECT version FROM host_source_schema_marker')] != [(3,)]):
        raise ValueError('current Host source schema unavailable')
    columns = {row[1]:(row[2].upper(),row[3],row[5]) for row in connection.exec_driver_sql('PRAGMA table_info(chat_messages)')}
    if columns.get('source_body_revision') != ('INTEGER',0,0) or columns.get('source_body_hash') != ('TEXT',0,0):
        raise ValueError('current Host source body columns unavailable')


def initialize_fresh_transcript_search(connection):
    """Create transcript search on the caller's already-owned connection."""
    connection.exec_driver_sql("CREATE VIRTUAL TABLE chat_messages_fts USING fts5(content, message_id UNINDEXED, session_id UNINDEXED, role UNINDEXED)")
    expression = "CASE WHEN instr(COALESCE(new.content, ''), ';base64,') > 0 OR instr(COALESCE(new.content, ''), 'data:image/') > 0 OR instr(COALESCE(new.content, ''), 'data:audio/') > 0 THEN '[inline media omitted from search index]' ELSE COALESCE(new.content, '') END"
    connection.exec_driver_sql(f"CREATE TRIGGER chat_messages_fts_ai AFTER INSERT ON chat_messages BEGIN INSERT INTO chat_messages_fts(content,message_id,session_id,role) VALUES ({expression},new.id,new.session_id,new.role); END")
    connection.exec_driver_sql("CREATE TRIGGER chat_messages_fts_ad AFTER DELETE ON chat_messages BEGIN DELETE FROM chat_messages_fts WHERE message_id=old.id; END")
    connection.exec_driver_sql(f"CREATE TRIGGER chat_messages_fts_au AFTER UPDATE ON chat_messages BEGIN DELETE FROM chat_messages_fts WHERE message_id=old.id; INSERT INTO chat_messages_fts(content,message_id,session_id,role) VALUES ({expression},new.id,new.session_id,new.role); END")

