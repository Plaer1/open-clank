"""Private TUI submission receipts; IDs select evidence and confer no authority."""
from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import text
from core.database import TuiTurnSubmission, get_db_session

PROFILE = 'openclank.tui-original-run.v1'
_COLUMNS = ('tui_profile', 'tui_account_id', 'tui_root_id', 'tui_run_id')


def validate_profile_connection(connection) -> bool:
    """Validate an optional profile during core startup and before its use."""
    columns = {r[1]: r for r in connection.exec_driver_sql('PRAGMA table_info(tui_turn_submissions)')}
    present = set(_COLUMNS).intersection(columns)
    if not present:
        return False
    if present != set(_COLUMNS) or any(
            columns[name][2].upper() != 'TEXT' or columns[name][3] != 0
            or columns[name][4] is not None or columns[name][5] != 0 for name in _COLUMNS):
        raise ValueError('Malformed optional TUI receipt profile')
    # The base schema validator owns the base columns; additionally require
    # the original identity fence and reject partially-bound profile rows.
    indexes = connection.exec_driver_sql('PRAGMA index_list(tui_turn_submissions)').fetchall()
    unique = [r[1] for r in indexes if r[2] == 1]
    if not any([c[2] for c in connection.exec_driver_sql('PRAGMA index_info("' + name.replace('"', '""') + '")')] == ['owner','idempotency_key'] for name in unique):
        raise ValueError('Missing TUI receipt identity fence')
    bad = connection.exec_driver_sql("""SELECT 1 FROM tui_turn_submissions WHERE
        (tui_profile IS NULL AND (tui_account_id IS NOT NULL OR tui_root_id IS NOT NULL OR tui_run_id IS NOT NULL)) OR
        (tui_profile IS NOT NULL AND (tui_profile != ? OR tui_account_id IS NULL OR length(tui_account_id)=0
        OR tui_root_id IS NULL OR length(tui_root_id)!=37 OR substr(tui_root_id,1,5)!='root_'
        OR substr(tui_root_id,6) GLOB '*[^0-9a-f]*' OR tui_run_id IS NULL OR length(tui_run_id)!=32
        OR tui_run_id GLOB '*[^0-9a-f]*' OR state NOT IN ('submitting','committed','started'))) LIMIT 1""", (PROFILE,)).first()
    if bad is not None:
        raise ValueError('Malformed TUI receipt profile data')
    return True


def profile_ready(db) -> bool:
    if db.bind.dialect.name != 'sqlite':
        return False
    try:
        return validate_profile_connection(db.connection())
    except Exception:
        return False


class TuiTurnConflict(ValueError):
    pass


@dataclass(frozen=True)
class TuiTurnGuard:
    owner: str
    account: str
    session_id: str
    submission_id: str
    request_hash: str
    root_id: str
    run_id: str
    current: object

    def _current(self):
        if not callable(self.current) or self.current() is not True:
            raise TuiTurnConflict('Original TUI credential unavailable')

    def prepare(self, db, row, message):
        """Verify and commit the receipt in the original message transaction."""
        self._current()
        if not profile_ready(db):
            raise TuiTurnConflict('TUI receipt profile unavailable')
        receipt = db.query(TuiTurnSubmission).filter(TuiTurnSubmission.id == self.submission_id).first()
        extra = db.execute(text('SELECT tui_profile,tui_account_id,tui_root_id,tui_run_id '
                                'FROM tui_turn_submissions WHERE id=:id'), {'id': self.submission_id}).first()
        if (row is None or row.id != self.session_id or row.owner != self.owner or row.archived
                or receipt is None or receipt.owner != self.owner or receipt.session_id != self.session_id
                or receipt.request_hash != self.request_hash or receipt.state != 'submitting'
                or extra is None or tuple(extra) != (PROFILE, self.account, self.root_id, self.run_id)
                or message.role != 'user' or message.persistence_id != self.root_id):
            raise TuiTurnConflict('Original TUI submission changed')
        receipt.state = 'committed'

    def before_commit(self):
        self._current()

    def publish_started(self, run):
        """Record the direct returned run; a failure retains unknown, never replay."""
        try:
            self._current()
            if run.run_id != self.run_id or run.owner != self.owner or run.tui_origin is not self:
                return False
            with get_db_session() as db:
                if not profile_ready(db):
                    return False
                result = db.execute(text("UPDATE tui_turn_submissions SET state='started' "
                    "WHERE id=:id AND owner=:owner AND session_id=:session AND request_hash=:hash "
                    "AND state='committed' AND tui_profile=:profile AND tui_account_id=:account "
                    "AND tui_root_id=:root AND tui_run_id=:run"),
                    {'id': self.submission_id, 'owner': self.owner, 'session': self.session_id,
                     'hash': self.request_hash, 'profile': PROFILE, 'account': self.account,
                     'root': self.root_id, 'run': self.run_id})
                self._current()
                return result.rowcount == 1
        except Exception:
            return False
