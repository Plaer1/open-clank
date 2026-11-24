#!/usr/bin/env python3
"""Prepare explicit optional FM profiles on a new offline copy; never activate it."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import subprocess
import tempfile
import os
from pathlib import Path
import sqlite3
import stat
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "native/memery/crates/memery-core/src/store"
sys.path.insert(0, str(ROOT))
from src.memery_database import prepare_guarded_memery_database

PROFILES = {
    "assistant-interpretation-pending": ("openclank.assistant_interpretation.pending_rebind.v1", "schema-v17-s05-pair-pending.sql"),
    "s08-model": ("openclank.s08.model.v1", "schema-v17-s08-model.sql"),
    # Additive over s08-model (never without it): re-creates its three accounting triggers so journal_synthesis jobs can be reserved.
    "s08-model-v2": ("openclank.s08.model.v2", "schema-v17-s08-model-v2.sql"),
    "knowledge-ref": ("openclank.s01.knowledge-ref.v1", "schema-v17-knowledge-ref.sql"),
    "s02-importance": ("openclank.s02.importance.v1", "schema-v17-s02-importance.sql"),
    "s02-importance-target": ("openclank.s02.importance_target.v1", "schema-v17-s02-importance-target.sql"),
    "s05-account-chat": ("openclank.s05.account_chat.v1", None),
    "document-fts": ("openclank.document_fts.v1", "schema-v17-document-fts.sql"),
}


def _canonical_path(path: Path, *, existing: bool) -> Path:
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise ValueError("absolute normalized paths required")
    parent = path.parent.resolve(strict=True)
    if str(parent) != str(path.parent) or (existing and path.resolve(strict=True) != path):
        raise ValueError("symlink or alias path unavailable")
    return parent / path.name


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("unaliased regular file required")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _no_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(str(path) + suffix):
            raise ValueError("source sidecar requires separate stopped-writer handling")


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _reserve(path: Path) -> tuple[int, int]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


def _same_file(path: Path, expected: tuple[int, int]) -> None:
    info = _identity(path)
    if info[:2] != expected or path.resolve(strict=True) != path:
        raise ValueError("output identity changed")


def _hash_existing(path: Path, expected: tuple[int, int]) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != expected:
            raise ValueError("output identity changed")
        return _hash_fd(fd)
    finally:
        os.close(fd)



def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _manifest(binary: Path, pin: str) -> dict:
    binary = _canonical_path(binary, existing=True)
    if len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin):
        raise ValueError("migrator SHA-256 required")
    before = _identity(binary)
    fd = os.open(binary, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != before[:2] or _hash_fd(fd) != pin:
            raise ValueError("migrator identity mismatch")
        with tempfile.TemporaryDirectory(prefix="s05-account-manifest-") as tmp:
            output = Path(tmp) / "manifest.json"
            result = subprocess.run([f"/proc/self/fd/{fd}", "--export-s05-account-chat-manifest", str(output)],
                pass_fds=(fd,), capture_output=True, timeout=30, check=False)
            if result.returncode or not output.is_file() or output.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("migrator manifest unavailable")
            def unique(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj: raise ValueError("duplicate manifest field")
                    obj[key] = value
                return obj
            manifest = json.loads(output.read_text(encoding="utf-8"), object_pairs_hook=unique)
        if _identity(binary) != before or _hash_fd(fd) != pin:
            raise ValueError("migrator changed")
    finally:
        os.close(fd)
    if (type(manifest) is not dict or set(manifest) != {"version","profile","s05_version","marker","columns","rowid","autoindexes","variants"}
        or manifest["version"] != 1 or manifest["profile"] != "s05-account-chat" or manifest["s05_version"] != 4
        or manifest["marker"] != {"key":"openclank.s05.account_chat.v1","value":"1"}
        or manifest["rowid"] != "preserve" or manifest["autoindexes"] != "table_owned"
        or type(manifest["columns"]) is not list or len(manifest["columns"]) != 21
        or any(type(x) is not str or not x for x in manifest["columns"]) or len(set(manifest["columns"])) != 21
        or type(manifest["variants"]) is not list or not manifest["variants"]):
        raise ValueError("invalid migrator manifest")
    return manifest


def _s05_catalog(db):
    table = "fm_v2_s05_job_refs"
    def obj(row): return dict(zip(("type","name","table","sql"), row))
    row = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE type='table' AND name=? AND sql IS NOT NULL", (table,)).fetchone()
    if row is None: raise ValueError("S05 table unavailable")
    owned = [obj(x) for x in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name", (table,))]
    external = [obj(x) for x in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND tbl_name!=? AND instr(lower(sql),?)>0 ORDER BY type,name", (table,table))]
    inbound = []
    for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        if any(str(fk[2]).lower() == table for fk in db.execute('PRAGMA foreign_key_list(' + _identifier(name) + ')')): inbound.append(name)
    return {"table":obj(row),"owned":owned,"external":external,"inbound_foreign_key_tables":inbound}


def _compare_original_rows(original, target, *, added_marker, excluded_derived=frozenset()):
    allowed = frozenset({'fm_v2_chunks_fts', 'fm_v2_chunks_fts_data', 'fm_v2_chunks_fts_idx',
                         'fm_v2_chunks_fts_content', 'fm_v2_chunks_fts_docsize', 'fm_v2_chunks_fts_config'})
    if excluded_derived and (added_marker != 'openclank.document_fts.v1' or excluded_derived != allowed):
        raise ValueError('unsupported derived exception')
    if added_marker not in ("openclank.s05.account_chat.v1", "openclank.document_fts.v1", "openclank.assistant_interpretation.pending_rebind.v1"):
        raise ValueError("unsupported metadata exception")
    if original.execute("SELECT 1 FROM fm_meta WHERE key=?", (added_marker,)).fetchone() is not None:
        raise ValueError("original marker already present")
    sequence_original = original.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='sqlite_sequence'").fetchone()
    sequence_target = target.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='sqlite_sequence'").fetchone()
    if sequence_original != sequence_target:
        raise ValueError("original sequence schema changed")
    tables = original.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' AND (name NOT LIKE 'sqlite_%' OR name='sqlite_sequence') ORDER BY name").fetchall()
    for name, sql in tables:
        if name in excluded_derived:
            continue
        quoted = _identifier(name)
        cols = [r[1] for r in original.execute('PRAGMA table_info(' + quoted + ')')]
        if cols != [r[1] for r in target.execute('PRAGMA table_info(' + quoted + ')')]:
            raise ValueError("original table columns changed")
        without_rowid = 'WITHOUT ROWID' in str(sql or '').upper()
        projection = ','.join(_identifier(c) for c in cols)
        order = projection if without_rowid else 'rowid'
        prefix = '' if without_rowid else 'rowid,'
        clause = " WHERE key IS NULL OR key!=?" if name == 'fm_meta' else ''
        query = 'SELECT ' + prefix + projection + ' FROM ' + quoted + clause + ' ORDER BY ' + order
        params = (added_marker,) if name == 'fm_meta' else ()
        a,b = original.execute(query,params),target.execute(query,params)
        while True:
            x,y = a.fetchone(),b.fetchone()
            if x is None or y is None:
                if x != y: raise ValueError("original rows changed")
                break
            if len(x)!=len(y) or any(type(i) is not type(j) or i != j for i,j in zip(x,y)):
                raise ValueError("original rows changed")


def _apply_s05_account_chat(db, original, manifest):
    marker = manifest['marker']['key']
    if db.execute("SELECT value FROM fm_meta WHERE key=?", (marker,)).fetchone() is not None:
        raise ValueError("account chat already present")
    if db.execute("SELECT version FROM fm_v2_s05_schema").fetchall() != [(4,)]:
        raise ValueError("S05 account v4 required")
    def present(key):
        row=db.execute("SELECT value FROM fm_meta WHERE key=?",(key,)).fetchone()
        if row is not None and row != ('1',): raise ValueError("profile marker unavailable")
        return row is not None
    selector = {"paired":present('openclank.assistant_interpretation.v1'),
        "pending":present('openclank.assistant_interpretation.pending_rebind.v1'),
        "s08":bool(db.execute("SELECT 1 FROM sqlite_schema WHERE type='index' AND name='s08_consolidation_jobs'").fetchone()),
        "s08_model":present('openclank.s08.model.v1')}
    old = _s05_catalog(db)
    found = [v for v in manifest['variants'] if type(v) is dict and set(v)=={'selectors','old','target'} and selector in v['selectors'] and v['old']==old]
    if len(found)!=1: raise ValueError("S05 object catalog unavailable")
    target = found[0]['target']
    if type(target) is not dict or set(target)!={'table','owned','external','inbound_foreign_key_tables'}:
        raise ValueError("S05 target unavailable")
    columns = manifest['columns']
    if [r[1] for r in db.execute('PRAGMA table_info(fm_v2_s05_job_refs)')] != columns:
        raise ValueError("S05 columns unavailable")
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute('BEGIN IMMEDIATE')
    try:
        projection=','.join(_identifier(x) for x in columns)
        db.execute('CREATE TEMP TABLE s05_original AS SELECT rowid AS __original_rowid,' + projection + ' FROM fm_v2_s05_job_refs')
        db.execute('DROP TABLE fm_v2_s05_job_refs')
        db.execute(target['table']['sql'])
        db.execute('INSERT INTO fm_v2_s05_job_refs(rowid,' + projection + ') SELECT __original_rowid,' + projection + ' FROM s05_original')
        for obj in target['owned']:
            db.execute(obj['sql'])
        db.execute('INSERT INTO fm_meta(key,value) VALUES (?,?)',(marker,'1'))
        if _s05_catalog(db)!=target or db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError("S05 reconstruction unavailable")
        _compare_original_rows(original,db,added_marker=marker)
        db.commit()
    except BaseException:
        db.rollback(); raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')




def _legacy_document_fts(db):
    """Return a content-free preimage only for the exact known derived namespace."""
    prefix = 'fm_v2_chunks_fts'
    query = "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE substr(lower(name),1,16)=? OR substr(lower(tbl_name),1,16)=? ORDER BY type,name"
    actual = db.execute(query, (prefix, prefix)).fetchall()
    if not actual:
        return None
    with closing(sqlite3.connect(':memory:')) as template:
        template.execute((SCHEMA / 'schema-v17-document-fts.sql').read_text(encoding='utf-8'))
        expected = template.execute(query, (prefix, prefix)).fetchall()
        names = frozenset(row[1] for row in expected if row[0] == 'table')
        shadows = template.execute("SELECT name FROM pragma_table_list WHERE type='shadow' AND substr(lower(name),1,16)=? ORDER BY name", (prefix,)).fetchall()
    expected_names = frozenset({'fm_v2_chunks_fts','fm_v2_chunks_fts_data','fm_v2_chunks_fts_idx',
        'fm_v2_chunks_fts_content','fm_v2_chunks_fts_docsize','fm_v2_chunks_fts_config'})
    if actual != expected or names != expected_names or len(shadows) != 5:
        raise ValueError('legacy document FTS namespace unavailable')
    external = db.execute("SELECT name FROM sqlite_schema WHERE sql IS NOT NULL AND instr(lower(sql),?)>0 AND substr(lower(name),1,16)!=? AND substr(lower(tbl_name),1,16)!=? LIMIT 1", (prefix,prefix,prefix)).fetchone()
    if external is not None:
        raise ValueError('legacy document FTS external dependency')
    for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        if name in names: continue
        if any(str(fk[2]).lower() in names for fk in db.execute('PRAGMA foreign_key_list(' + _identifier(name) + ')')):
            raise ValueError('legacy document FTS foreign key dependency')
    digest = hashlib.sha256(b'openclank.legacy-document-fts.v1\n')
    count = 0
    for row in db.execute('SELECT rowid,owner_id,chunk_id,text FROM fm_v2_chunks_fts ORDER BY rowid'):
        count += 1
        for value in row:
            if value is None: tag, raw = b'n', b''
            elif type(value) is int: tag, raw = b'i', struct.pack('>q', value)
            elif type(value) is float: tag, raw = b'r', struct.pack('>d', value)
            elif type(value) is str: tag, raw = b't', value.encode('utf-8')
            elif type(value) is bytes: tag, raw = b'b', value
            else: raise ValueError('legacy document FTS row unavailable')
            digest.update(tag); digest.update(len(raw).to_bytes(8,'big')); digest.update(raw)
    return {'state':'rebuilt','row_count':count,'sha256':digest.hexdigest()}, names


def _apply_document_fts(db, original):
    marker = "openclank.document_fts.v1"
    if db.execute("SELECT 1 FROM fm_meta WHERE key=?", (marker,)).fetchone() is not None:
        raise ValueError("document FTS already present")
    legacy = _legacy_document_fts(db)
    if legacy != _legacy_document_fts(original):
        raise ValueError('legacy document FTS preimage changed')
    db.execute('BEGIN IMMEDIATE')
    try:
        if legacy is not None:
            db.execute("INSERT INTO fm_v2_chunks_fts(fm_v2_chunks_fts) VALUES ('integrity-check')")
            db.execute('DROP TABLE fm_v2_chunks_fts')
        db.execute((SCHEMA / 'schema-v17-document-fts.sql').read_text(encoding='utf-8'))
        db.execute('INSERT INTO fm_v2_chunks_fts(owner_id,chunk_id,text) SELECT owner_id,chunk_id,text FROM fm_v2_chunks')
        chunks = db.execute('SELECT count(*) FROM fm_v2_chunks').fetchone()[0]
        indexed = db.execute('SELECT count(*) FROM fm_v2_chunks_fts').fetchone()[0]
        if chunks != indexed:
            raise ValueError('document FTS backfill mismatch')
        source = db.execute('SELECT owner_id,chunk_id,text FROM fm_v2_chunks ORDER BY owner_id,chunk_id')
        projection = db.execute('SELECT owner_id,chunk_id,text FROM fm_v2_chunks_fts ORDER BY owner_id,chunk_id')
        while True:
            expected, actual = source.fetchone(), projection.fetchone()
            if expected is None or actual is None:
                if expected != actual: raise ValueError('document FTS backfill mismatch')
                break
            if any(type(a) is not type(b) or a != b for a,b in zip(expected,actual)):
                raise ValueError('document FTS backfill mismatch')
        db.execute("INSERT INTO fm_v2_chunks_fts(fm_v2_chunks_fts) VALUES ('integrity-check')")
        db.execute('INSERT INTO fm_meta(key,value) VALUES (?,?)', (marker, '1'))
        _compare_original_rows(original, db, added_marker=marker, excluded_derived=legacy[1] if legacy else frozenset())
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('document FTS foreign key mismatch')
        db.commit()
        return legacy[0] if legacy else {'state':'created','row_count':0,'sha256':None}
    except BaseException:
        db.rollback(); raise


def _apply_assistant_interpretation_pending(db, original):
    """Install only the canonical pending overlay on an already-validated copy."""
    marker = "openclank.assistant_interpretation.pending_rebind.v1"
    if db.execute("SELECT value FROM fm_meta WHERE key=?", (marker,)).fetchone() is not None:
        raise ValueError("selected profile already present or incomplete")
    if db.execute("SELECT value FROM fm_meta WHERE key='openclank.assistant_interpretation.v1'").fetchone() != ("1",):
        raise ValueError("assistant interpretation base profile required")
    if db.execute("SELECT version FROM fm_v2_s05_schema").fetchall() != [(4,)]:
        raise ValueError("S05 account v4 required")
    sql = (SCHEMA / "schema-v17-s05-pair-pending.sql").read_text(encoding="utf-8")
    try:
        db.executescript("BEGIN IMMEDIATE;\n" + sql)
        _compare_original_rows(original, db, added_marker=marker)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def prepare(source: Path, backup: Path, destination: Path, profiles: tuple[str, ...], *, native: Path, native_sha256: str, stopped_writers: bool, migrator: Path | None = None, migrator_sha256: str | None = None) -> dict:
    if stopped_writers is not True:
        raise ValueError("explicit stopped-writer acknowledgment required")
    if not profiles or len(set(profiles)) != len(profiles) or any(p not in PROFILES for p in profiles):
        raise ValueError("explicit distinct profiles required")
    if len(native_sha256) != 64 or any(c not in "0123456789abcdef" for c in native_sha256):
        raise ValueError("native binary SHA-256 required")
    if len(profiles) != 1 and any(p in profiles for p in ("s05-account-chat", "document-fts", "assistant-interpretation-pending")):
        raise ValueError("account chat, document FTS and assistant pending each require a separate copy")
    if "s05-account-chat" in profiles and (migrator is None or migrator_sha256 is None):
        raise ValueError("account chat requires its own pinned migrator and separate copy")
    manifest = _manifest(migrator, migrator_sha256) if "s05-account-chat" in profiles else None
    source = _canonical_path(source, existing=True)
    native = _canonical_path(native, existing=True)
    backup = _canonical_path(backup, existing=False)
    destination = _canonical_path(destination, existing=False)
    if len({source, backup, destination, native}) != 4 or os.path.lexists(backup) or os.path.lexists(destination):
        raise ValueError("distinct source, binary and new output paths required")
    before = _identity(source)
    binary_before = _identity(native)
    if before[:2] == binary_before[:2]:
        raise ValueError("source and native binary alias")
    _no_sidecars(source)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    binary_fd = os.open(native, flags)
    try:
        if (os.fstat(source_fd).st_dev, os.fstat(source_fd).st_ino) != before[:2]:
            raise ValueError("source identity changed")
        if (os.fstat(binary_fd).st_dev, os.fstat(binary_fd).st_ino) != binary_before[:2] or _hash_fd(binary_fd) != native_sha256:
            raise ValueError("native binary identity mismatch")
        source_hash = _hash_fd(source_fd)
        backup_identity = _reserve(backup)
        destination_identity = _reserve(destination)
        # The source is opened through the already-held descriptor; no source
        # pathname is selected again for SQLite's immutable read.
        descriptor_path = Path(f"/proc/self/fd/{source_fd}")
        if not sys.platform.startswith("linux") or not descriptor_path.exists():
            raise ValueError("pinned source descriptor unavailable")
        with closing(sqlite3.connect(descriptor_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as src:
            src.execute("PRAGMA query_only=ON")
            if src.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("source profile unavailable")
            version = src.execute("PRAGMA user_version").fetchone()
            if version == (17,):
                source_profile = "guarded-v17"
            elif version == (18,) and set(profiles) <= {"s02-importance", "s02-importance-target"}:
                # Only stopped, pre-quota v18 score preparation is supported.
                prior = src.execute("SELECT EXISTS(SELECT 1 FROM fm_meta WHERE key IN ('openclank.s05.quota_purge.v1','openclank.s05.quota_cutoff.v1') OR key GLOB 's05_quota_batch_*' OR key GLOB 's05_quota_*_receipt_*' OR key GLOB 'owner_transition_*')").fetchone()
                if prior != (0,):
                    raise ValueError("source profile unavailable")
                source_profile = "guarded-v18"
            else:
                raise ValueError("source profile unavailable")
            identity = src.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
            if not identity or type(identity[0]) is not str or not identity[0] or identity[0] != identity[0].strip():
                raise ValueError("source database identity unavailable")
            _same_file(backup, backup_identity)
            with closing(sqlite3.connect(backup)) as saved:
                src.backup(saved)
        _no_sidecars(source)
        if _identity(source) != before or _hash_fd(source_fd) != source_hash:
            raise ValueError("source changed during backup")
        _same_file(backup, backup_identity)
        backup_hash = _hash_existing(backup, backup_identity)
        _same_file(destination, destination_identity)
        with closing(sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)) as saved, closing(sqlite3.connect(destination)) as target:
            saved.backup(target)
        _same_file(destination, destination_identity)

        def validate_native() -> None:
            if _identity(native) != binary_before or _hash_fd(binary_fd) != native_sha256:
                raise ValueError("native binary changed")
            _same_file(destination, destination_identity)
            prepared = prepare_guarded_memery_database(str(destination), command=str(native), expected_database_id=identity[0], profile=source_profile)
            prepared.assert_valid()
            _same_file(destination, destination_identity)
            if _identity(native) != binary_before or _hash_fd(binary_fd) != native_sha256:
                raise ValueError("native binary changed")
            return prepared

        # Native owns the full baseline and composed schema; it runs on the copy only.
        validate_native()
        applied = []
        document_fts_report = None
        with closing(sqlite3.connect(destination)) as db:
            # This explicit copy-preparation connection owns only the fixed
            # first-party seed; the existing v17 trigger remains installed.
            db.create_function("fm_writer_capability", 0, lambda: "openclank.frankenmemory.writer.v2", deterministic=True)
            for profile in profiles:
                marker, schema = PROFILES[profile]
                if profile == "assistant-interpretation-pending":
                    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)) as original:
                        _apply_assistant_interpretation_pending(db, original)
                    applied.append(profile)
                    continue
                if profile == "document-fts":
                    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)) as original:
                        document_fts_report = _apply_document_fts(db, original)
                    applied.append(profile)
                    continue
                if profile == "s05-account-chat":
                    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)) as original:
                        _apply_s05_account_chat(db, original, manifest)
                    applied.append(profile)
                    continue
                if profile == "s02-importance-target":
                    if db.execute("SELECT value FROM fm_meta WHERE key='openclank.s02.importance.v1'").fetchone() != ("1",) or db.execute("SELECT value FROM fm_meta WHERE key='openclank.s08.model.v2'").fetchone() != ("1",):
                        raise ValueError("S02 importance target prerequisite unavailable")
                    if db.execute("SELECT EXISTS(SELECT 1 FROM fm_meta WHERE key=?) OR EXISTS(SELECT 1 FROM sqlite_schema WHERE name='importance_target_selection_guard')", (marker,)).fetchone() != (0,):
                        raise ValueError("S02 importance target already present or incomplete")
                    if db.execute("SELECT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE reason='s08.selection.v1' AND CASE WHEN json_valid(operation_metadata_json) THEN json_type(operation_metadata_json,'$.importance_target') IS NOT NULL ELSE 1 END)").fetchone() != (0,):
                        raise ValueError("S02 importance target history unavailable")
                    skill = db.execute("SELECT value FROM fm_meta WHERE key='openclank.s05.skill_draft.v1'").fetchone()
                    if skill not in (None, ("1",)):
                        raise ValueError("S02 importance target accounting prerequisite unavailable")
                    accounting = "schema-v17-s02-target-skill-accounting.sql" if skill else "schema-v17-s02-target-model-accounting.sql"
                    db.executescript("BEGIN IMMEDIATE;\n" + (SCHEMA / schema).read_text(encoding="utf-8") + "\n" + (SCHEMA / accounting).read_text(encoding="utf-8"))
                    db.commit()
                    applied.append(profile)
                    continue
                if profile == "s02-importance":
                    for prerequisite in ("openclank.domain_metadata.v1", "openclank.type_tag_policy.v1", "openclank.type_tag_policy.v2", "openclank.s08.model.v1"):
                        if db.execute("SELECT value FROM fm_meta WHERE key=?", (prerequisite,)).fetchone() != ("1",):
                            raise ValueError("S02 importance prerequisite unavailable")
                    if db.execute("SELECT EXISTS(SELECT 1 FROM fm_meta WHERE key=?) OR EXISTS(SELECT 1 FROM sqlite_schema WHERE name IN ('importance_metadata_guard','importance_revision_guard'))", (marker,)).fetchone() != (0,):
                        raise ValueError("S02 importance profile already present or incomplete")
                    if db.execute("SELECT EXISTS(SELECT 1 FROM fm_v2_revision_metadata WHERE CASE WHEN json_valid(metadata_json) THEN json_type(metadata_json,'$.importance_score') IS NOT NULL ELSE 1 END)").fetchone() != (0,):
                        raise ValueError("S02 importance history unavailable")
                    knowledge = db.execute("SELECT value FROM fm_meta WHERE key='openclank.s01.knowledge-ref.v1'").fetchone()
                    if knowledge not in (None, ("1",)):
                        raise ValueError("knowledge reference profile unavailable")
                    combined = (SCHEMA / "schema-v17-s02-importance-knowledge-ref.sql").read_text(encoding="utf-8") if knowledge else ""
                    db.executescript("BEGIN IMMEDIATE;\n" + (SCHEMA / schema).read_text(encoding="utf-8") + "\n" + combined)
                    db.commit()
                    applied.append(profile)
                    continue
                present = db.execute("SELECT value FROM fm_meta WHERE key=?", (marker,)).fetchone()
                if present is not None:
                    raise ValueError("selected profile already present or incomplete")
                if profile == "s08-model" and db.execute("SELECT count(*) FROM sqlite_schema WHERE type='index' AND name='s08_consolidation_jobs'").fetchone() != (1,):
                    raise ValueError("S08 base profile unavailable")
                if profile == "s08-model-v2" and db.execute("SELECT value FROM fm_meta WHERE key='openclank.s08.model.v1'").fetchone() != ("1",):
                    raise ValueError("S08 model v2 requires the S08 model profile")
                if profile == "knowledge-ref" and db.execute("SELECT count(*) FROM sqlite_schema WHERE type='trigger' AND name='fm_v2_knowledge_ref_validate'").fetchone() != (1,):
                    raise ValueError("knowledge-reference prerequisite unavailable")
                seed = (("openclank.related_to.v1", "knowledge_ref", "set", 1, 0)
                        if profile == "knowledge-ref" else ("s08.composite", "object", "single", 1, 0))
                row = db.execute("SELECT predicate,value_type,cardinality,version,allow_unknown FROM fm_v2_predicates WHERE predicate=?", (seed[0],)).fetchone()
                if row is not None and row != seed:
                    raise ValueError("first-party predicate conflicts")
                sql = (SCHEMA / schema).read_text(encoding="utf-8")
                prerequisite_sql = ""
                if profile == "knowledge-ref":
                    for prerequisite in ("openclank.domain_metadata.v1", "openclank.type_tag_policy.v1"):
                        if db.execute("SELECT value FROM fm_meta WHERE key=?", (prerequisite,)).fetchone() != ("1",):
                            raise ValueError("knowledge-reference requires admitted domain and S02 v1 profiles")
                    v2 = db.execute("SELECT value FROM fm_meta WHERE key='openclank.type_tag_policy.v2'").fetchone()
                    if v2 is None:
                        prerequisite_sql = (SCHEMA / "schema-v17-s02-policy-v2.sql").read_text(encoding="utf-8")
                        prerequisite_sql += "\nINSERT INTO fm_meta(key,value) VALUES ('openclank.type_tag_policy.v2','1');\n"
                    elif v2 != ("1",):
                        raise ValueError("S02 v2 profile unavailable")
                combined = ""
                if profile == "knowledge-ref":
                    importance = db.execute("SELECT value FROM fm_meta WHERE key='openclank.s02.importance.v1'").fetchone()
                    if importance not in (None, ("1",)):
                        raise ValueError("S02 importance profile unavailable")
                    if importance:
                        combined = (SCHEMA / "schema-v17-s02-importance-knowledge-ref.sql").read_text(encoding="utf-8")
                db.executescript("BEGIN IMMEDIATE;\n" + prerequisite_sql + "\n" + sql + "\n" + combined)
                if row is None:
                    db.execute("INSERT INTO fm_v2_predicates(predicate,value_type,cardinality,version,allow_unknown) VALUES(?,?,?,?,?)", seed)
                db.commit()
                applied.append(profile)
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",) or db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone() != identity:
                raise ValueError("destination integrity or identity changed")
            if any(db.execute("SELECT value FROM fm_meta WHERE key=?", (PROFILES[p][0],)).fetchone() != ("1",) for p in profiles):
                raise ValueError("requested profile unavailable")
        prepared = validate_native()
        if "assistant-interpretation-pending" in profiles:
            from src.openclank.memory_job_admission import _paired_pending_profile
            _paired_pending_profile(prepared, destination)
        _same_file(backup, backup_identity)
        if _hash_existing(backup, backup_identity) != backup_hash:
            raise ValueError("backup changed during preparation")
        _no_sidecars(source)
        if _identity(source) != before or _hash_fd(source_fd) != source_hash:
            raise ValueError("source changed during preparation")
        return {"database_id": identity[0], "source_sha256": source_hash, "native_sha256": native_sha256, "requested": list(profiles), "applied": applied, "state": "prepared_copy", **({"document_fts": document_fts_report} if document_fts_report is not None else {})}
    finally:
        os.close(source_fd)
        os.close(binary_fd)


def prepare_guarded_v18(source: Path, backup: Path, destination: Path, *, migrator: Path, migrator_sha256: str, stopped_writers: bool, document_structure: bool = False) -> None:
    """Run the pinned Native copy owner; never convert the source in Python."""
    if stopped_writers is not True or not sys.platform.startswith("linux"):
        raise ValueError("stopped writers and a pinned executable descriptor are required")
    if type(migrator_sha256) is not str or len(migrator_sha256) != 64 or any(c not in "0123456789abcdef" for c in migrator_sha256):
        raise ValueError("migrator SHA-256 required")
    source = _canonical_path(source, existing=True)
    backup = _canonical_path(backup, existing=False)
    destination = _canonical_path(destination, existing=False)
    migrator = _canonical_path(migrator, existing=True)
    if len({source, backup, destination, migrator}) != 4 or os.path.lexists(backup) or os.path.lexists(destination):
        raise ValueError("distinct source, executable and new output paths required")
    before = _identity(migrator)
    fd = os.open(migrator, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != before[:2] or _hash_fd(fd) != migrator_sha256:
            raise ValueError("migrator identity mismatch")
        command = [f"/proc/self/fd/{fd}", "--prepare-guarded-v18", str(source), str(backup), str(destination), "--writers-stopped"]
        if document_structure:
            command.append("--document-structure")
        result = subprocess.run(command, pass_fds=(fd,), check=False)
        if result.returncode or _identity(migrator) != before or _hash_fd(fd) != migrator_sha256:
            raise ValueError("guarded v18 preparation unavailable")
    finally:
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("backup", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--profile", choices=tuple(PROFILES), action="append", default=[])
    parser.add_argument("--assistant-interpretation-pending", dest="profile", action="append_const", const="assistant-interpretation-pending",
                        help="Prepare the pending paired profile on a stopped new copy with the base paired profile.")
    parser.add_argument("--native", type=Path)
    parser.add_argument("--native-sha256")
    parser.add_argument("--guarded-v18", action="store_true", help="Prepare a separate v18 copy.")
    parser.add_argument("--document-structure", action="store_true", help="Add scoped document structure with the pinned v18 copy workflow.")
    parser.add_argument("--migrator", type=Path)
    parser.add_argument("--migrator-sha256")
    parser.add_argument("--stopped-writers", action="store_true", required=True, help="Acknowledge that writers are stopped; this is not a lock or cutover.")
    args = parser.parse_args()
    if args.guarded_v18:
        if args.profile or args.native or args.native_sha256 or not args.migrator or not args.migrator_sha256:
            parser.error("v18 requires only a pinned migrator, not optional profiles or a native executable")
        prepare_guarded_v18(args.source, args.backup, args.destination, migrator=args.migrator, migrator_sha256=args.migrator_sha256,
                            stopped_writers=args.stopped_writers, document_structure=args.document_structure)
        return
    if args.document_structure:
        parser.error("--document-structure requires --guarded-v18")
    if not args.native or not args.native_sha256:
        parser.error("optional profiles require --native and --native-sha256")
    result = prepare(args.source, args.backup, args.destination, tuple(args.profile), native=args.native, native_sha256=args.native_sha256, stopped_writers=args.stopped_writers, migrator=args.migrator, migrator_sha256=args.migrator_sha256)
    print("Prepared separate backup and profile copy; source and configuration were not replaced.")
    print("Requested: " + ", ".join(result["requested"]) + "; applied: " + (", ".join(result["applied"]) or "none"))
    if "document_fts" in result:
        print("Document FTS: " + json.dumps(result["document_fts"], sort_keys=True))
    print("A later live cutover needs separate authorization.")


if __name__ == "__main__":
    main()
