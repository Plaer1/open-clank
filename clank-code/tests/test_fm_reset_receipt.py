import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.memery_database import GuardedStorePreparation, _GUARDED_PREPARATION_SEAL
from src.openclank.fm_reset_receipt import (
    ResetReceiptUnavailable, freeze_reset_receipt, read_committed_reset_receipt,
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


class ResetReceiptCase(unittest.TestCase):
    def test_committed_wal_and_exact_identity(self):
        with tempfile.TemporaryDirectory(prefix='openclank-reset-receipt-') as directory:
            path = Path(directory) / 'fm.sqlite'
            writer = sqlite3.connect(path)
            try:
                self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                writer.executescript("""
                    CREATE TABLE fm_meta(key TEXT PRIMARY KEY,value TEXT);
                    CREATE TABLE fm_v2_change_sets(owner_id TEXT,change_set_id TEXT,reason TEXT,content_hash TEXT,operation_metadata_json TEXT,PRIMARY KEY(owner_id,change_set_id));
                    CREATE TABLE fm_v2_outbox(owner_id TEXT,event_id TEXT,change_set_id TEXT,kind TEXT,payload_json TEXT,source_revision INTEGER,PRIMARY KEY(owner_id,event_id));
                    INSERT INTO fm_meta VALUES('database_id','db-fixture');
                    PRAGMA user_version=17;
                """)
                writer.commit()
                st = path.stat()
                preparation = GuardedStorePreparation(str(path), 'db-fixture', {}, st.st_dev, st.st_ino, _GUARDED_PREPARATION_SEAL)
                kwargs = dict(owner='alice', account_id='account', operation_id='op', workspace_id='ws',
                              components=['graph'], expected_counts={'graph': {'count': 2, 'fingerprint': 'a' * 64}}, queued_retired=False)
                frozen = freeze_reset_receipt(preparation, **kwargs)
                with self.assertRaises(ResetReceiptUnavailable):
                    read_committed_reset_receipt(preparation, frozen, current=lambda: True)
                request = frozen['request']
                result = {'complete': True, 'categories': {'graph': {**request['expected_counts']['graph'], 'state': 'complete'}}}
                receipt = {'version': 's06.guarded-reset.v1', 'request': request,
                           'request_sha256': hashlib.sha256(canonical(request)).hexdigest(), 'result': result}
                raw = canonical(receipt).decode()
                receipt_id = 'cs_reset_' + hashlib.sha256(canonical(['account', 'op'])).hexdigest()
                writer.execute('INSERT INTO fm_v2_change_sets VALUES(?,?,?,?,?)', ('alice', receipt_id, 's06.guarded-reset.v1', hashlib.sha256(raw.encode()).hexdigest(), raw))
                writer.execute('INSERT INTO fm_v2_outbox VALUES(?,?,?,?,?,?)', ('alice', 'outbox_' + receipt_id + '_s06_guarded_reset', receipt_id, 's06_guarded_reset', raw, 0))
                writer.commit()
                self.assertTrue(Path(str(path) + '-wal').exists())
                self.assertEqual(read_committed_reset_receipt(preparation, frozen, current=lambda: True),
                                 {'state': 'committed', 'sql_result': result, 'cleanup': 'unproved'})
                wrong = copy.deepcopy(frozen); wrong['request']['workspace_id'] = 'different'
                with self.assertRaises(ResetReceiptUnavailable):
                    read_committed_reset_receipt(preparation, wrong, current=lambda: True)
                with self.assertRaises(ResetReceiptUnavailable):
                    read_committed_reset_receipt(preparation, frozen, current=lambda: False)
                writer.execute("UPDATE fm_v2_outbox SET payload_json='{}'"); writer.commit()
                with self.assertRaises(ResetReceiptUnavailable):
                    read_committed_reset_receipt(preparation, frozen, current=lambda: True)
                writer.execute('UPDATE fm_v2_outbox SET payload_json=?', (raw,)); writer.commit()
                renamed = path.with_name('old.sqlite'); os.rename(path, renamed); path.touch()
                with self.assertRaises(ResetReceiptUnavailable):
                    read_committed_reset_receipt(preparation, frozen, current=lambda: True)
            finally:
                writer.close()


if __name__ == '__main__':
    unittest.main()
