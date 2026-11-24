"""Main-file-only bounded SQLite schema inspection in a private snapshot."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time

PROFILE = 'sqlite-main-schema-1'
MAX_FILE = 16 * 1024 * 1024
MAX_ROWS = 256
MAX_FIELD = 4096
MAX_OUTPUT = 256 * 1024
MAX_SECONDS = 12
MAX_STEPS = 100_000
CHUNK = 64 * 1024


class SqliteUnsupported(ValueError):
    pass


def _schema(snapshot: bytes, cancel: threading.Event, deadline: float) -> dict:
    # The worker owns both the private file and connection through actual close.
    with tempfile.TemporaryDirectory(prefix='openclank-sqlite-inspect-') as directory:
        path = Path(directory) / 'main.sqlite'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(snapshot)
        db = None
        try:
            if cancel.is_set() or time.monotonic() >= deadline:
                raise SqliteUnsupported('SQLite inspection deadline exceeded')
            db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1&cache=private', uri=True)
            steps = 0
            def progress():
                nonlocal steps
                steps += 100
                return int(cancel.is_set() or time.monotonic() >= deadline or steps > MAX_STEPS)
            db.set_progress_handler(progress, 100)
            if not hasattr(db, 'setlimit') or not hasattr(sqlite3, 'SQLITE_LIMIT_LENGTH'):
                raise SqliteUnsupported('SQLite inspection controls are unavailable')
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
            db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 65536)
            db.enable_load_extension(False)
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            if db.execute('PRAGMA query_only').fetchone() != (1,) or db.execute('PRAGMA trusted_schema').fetchone() != (0,):
                raise SqliteUnsupported('SQLite read-only controls are unavailable')
            db.execute('PRAGMA temp_store=MEMORY')
            db.execute('PRAGMA cache_size=-2048')
            def authorize(action, arg1, arg2, database, source):
                if action == sqlite3.SQLITE_SELECT and source is None:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_READ and database == 'main' and source is None and arg1 in {'sqlite_master', 'sqlite_schema'} and arg2 in {'type', 'name', 'tbl_name', 'sql'}:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            db.set_authorizer(authorize)
            rows = []
            output_bytes = 0
            omitted = False
            for row in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema LIMIT 257'):
                if len(rows) == MAX_ROWS:
                    omitted = True
                    break
                if len(row) != 4 or any(value is not None and not isinstance(value, str) for value in row):
                    raise SqliteUnsupported('SQLite schema declaration is unsupported')
                encoded = [len(value.encode('utf-8')) if value is not None else 0 for value in row]
                item = {'type':row[0], 'name':row[1], 'tableName':row[2], 'sql':row[3]}
                rendered = len(json.dumps(item, ensure_ascii=True, separators=(',', ':')).encode('ascii')) + 1
                if any(length > MAX_FIELD for length in encoded) or rendered > MAX_OUTPUT - output_bytes:
                    raise SqliteUnsupported('SQLite schema output limit exceeded')
                output_bytes += rendered
                rows.append(item)
            if cancel.is_set() or time.monotonic() >= deadline:
                raise SqliteUnsupported('SQLite inspection deadline exceeded')
            return {'rows':rows, 'displayedRows':len(rows), 'additionalRows':omitted,
                    'outputBytes':output_bytes, 'sqliteVersion':sqlite3.sqlite_version}
        except (sqlite3.Error, OSError, UnicodeError, AttributeError) as exc:
            raise SqliteUnsupported('SQLite main-file schema is unavailable') from exc
        finally:
            if db is not None:
                db.close()


async def inspect(read_range, size: int) -> dict:
    if type(size) is not int or not 100 <= size <= MAX_FILE:
        raise SqliteUnsupported('SQLite main file size is unsupported')
    deadline = time.monotonic() + MAX_SECONDS
    chunks = []
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        length = min(CHUNK, size - offset)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SqliteUnsupported('SQLite capture deadline exceeded')
        try:
            async with asyncio.timeout(remaining):
                data = await read_range(offset, length)
        except TimeoutError as exc:
            raise SqliteUnsupported('SQLite capture deadline exceeded') from exc
        if not isinstance(data, bytes) or len(data) != length:
            raise SqliteUnsupported('SQLite main-file capture is truncated')
        if offset == 0 and not data.startswith(b'SQLite format 3\x00'):
            raise SqliteUnsupported('SQLite main-file signature is unsupported')
        digest.update(data)
        chunks.append(data)
        offset += length
        await asyncio.sleep(0)
    snapshot = b''.join(chunks)
    cancel = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(_schema, snapshot, cancel, deadline))
    try:
        result = await asyncio.shield(worker)
    except (OSError, sqlite3.Error, UnicodeError) as exc:
        raise SqliteUnsupported('SQLite private snapshot is unavailable') from exc
    except asyncio.CancelledError:
        cancel.set()
        # Cancellation is not thread settlement. Keep the private file owner
        # alive until the actual worker has closed it, even on repeated cancel.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if worker.done() and not worker.cancelled():
            worker.exception()
        raise
    return {'profile':PROFILE, 'fileBytes':size, 'snapshotSHA256':digest.hexdigest(),
            'snapshotScope':'complete_main_file', 'transactionalCompleteness':'unknown',
            'neighborState':'not_inspected', 'contentSHA256':None, 'payloadIntegrity':'not_checked', **result}
