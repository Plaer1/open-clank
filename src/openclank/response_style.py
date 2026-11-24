"""Canonical response-style values and explicit stopped-copy schema preparation."""
import hashlib
import json
import sqlite3

MODES = ('normal', 'concise', 'brief', 'minimal')
MAX_REVISION = 9_007_199_254_740_991
_STYLE_CHECK = "CHECK (response_style IN ('normal','concise','brief','minimal'))"
_REVISION_CHECK = "CHECK (typeof(response_style_revision) = 'integer' AND response_style_revision >= 0 AND response_style_revision <= 9007199254740991)"


def normalize_mode(value):
    if type(value) is not str or value not in MODES:
        raise ValueError('invalid response style')
    return value


def style_snapshot(mode, revision):
    if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
        raise ValueError('invalid response style revision')
    return {'mode': normalize_mode(mode), 'revision': revision}


def snapshot_fingerprint(mode, revision):
    value = style_snapshot(mode, revision)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def style_instruction(mode):
    mode = normalize_mode(mode)
    if mode == 'normal':
        return ''
    level = {'concise':'Be concise.', 'brief':'Be brief.', 'minimal':'Use minimal wording.'}[mode]
    return (level + ' Apply this to explanatory prose; preserve facts, negations, numbers, code, literals, commands, paths, structured outputs, errors, required technical detail and complete deliverables. '
            'Follow explicit user requests for detail or style and applicable repository instructions.')


def validate_response_style_schema(db):
    """Validate a closed current SQLite schema and values; never mutate it."""
    if not isinstance(db, sqlite3.Connection):
        raise ValueError('SQLite connection required')
    row = db.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='sessions'").fetchone()
    if not row or not isinstance(row[0], str):
        raise ValueError('session schema unavailable')
    columns = {item[1]: item for item in db.execute('PRAGMA table_info(sessions)')}
    mode, revision = columns.get('response_style'), columns.get('response_style_revision')
    if (not mode or not revision or mode[2].upper() not in ('TEXT', 'VARCHAR') or revision[2].upper() != 'INTEGER'
            or mode[3] != 1 or revision[3] != 1 or str(mode[4]).strip("() ") not in ("'normal'", '"normal"')
            or str(revision[4]).strip("() '\"") != '0'):
        raise ValueError('response style schema unavailable')
    def shape(value): return ''.join(value.lower().split()).replace('"', '')
    sql = shape(row[0])
    if shape(_STYLE_CHECK) not in sql or shape(_REVISION_CHECK) not in sql:
        raise ValueError('response style schema unavailable')
    if db.execute("SELECT 1 FROM sessions WHERE response_style NOT IN ('normal','concise','brief','minimal') OR response_style IS NULL OR typeof(response_style_revision) != 'integer' OR response_style_revision < 0 OR response_style_revision > 9007199254740991 LIMIT 1").fetchone():
        raise ValueError('response style state unavailable')
    return True


def prepare_response_style_schema(db):
    """Upgrade a stopped, separately copied SQLite database; caller owns transaction."""
    if not isinstance(db, sqlite3.Connection) or db.in_transaction:
        raise ValueError('fresh SQLite transaction required')
    columns = {item[1] for item in db.execute('PRAGMA table_info(sessions)')}
    if not columns or bool('response_style' in columns) != bool('response_style_revision' in columns):
        raise ValueError('partial or missing session schema')
    if 'response_style' in columns:
        return validate_response_style_schema(db)
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute("ALTER TABLE sessions ADD COLUMN response_style TEXT NOT NULL DEFAULT 'normal' " + _STYLE_CHECK)
        db.execute('ALTER TABLE sessions ADD COLUMN response_style_revision INTEGER NOT NULL DEFAULT 0 ' + _REVISION_CHECK)
        validate_response_style_schema(db)
        db.commit()
        return True
    except BaseException:
        db.rollback()
        raise
