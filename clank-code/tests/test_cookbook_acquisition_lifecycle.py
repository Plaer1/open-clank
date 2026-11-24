"""One disposable same-store account-lifecycle acquisition case."""
import unittest
import tempfile
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.database import Base
# Load the actual lock module directly; avoid importing optional search providers.
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("services.memory.skill_lifecycle", Path("services/memory/skill_lifecycle.py"))
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from src.openclank.account_ancillary_lifecycle import AccountAncillaryLifecycle, AccountAncillaryPaths
from unittest.mock import patch
from src.openclank.cookbook_acquisition import reviewed_request
from src.openclank.cookbook_state import transaction, save, reserve, advance, owned_rows, CookbookStateError


class LifecycleTest(unittest.TestCase):
    def test_acquisition_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = create_engine(f"sqlite:///{root / 'db'}")
            Base.metadata.create_all(engine)
            paths = AccountAncillaryPaths(data_dir=root, gallery_root=root/'gallery', published_root=root/'published', background_jobs_file=root/'jobs.json', background_jobs_dir=root/'jobs', scheduled_email_db=root/'email.db', agent_todos_dir=root/'todos', file_trash_dir=root/'trash', journal_path=root/'journal'/'ancillary.json')
            lifecycle = AccountAncillaryLifecycle(session_factory=sessionmaker(bind=engine), paths=paths)
            state_path = paths.data_dir / 'cookbook_state.json'
            manifest = {'repoID':'org/model','revision':'a'*40,'gating':{'state':'ungated'},
                        'license':{'value':None,'source':None,'status':'unknown'},
                        'files':[{'path':'weights.bin','size':3,'blobID':None,'lfsSHA256':None}]}
            review, digest = reviewed_request(manifest=manifest, selected_paths=['weights.bin'],
                                              destination={'path':'/cache','device':1,'inode':2})
            with transaction(state_path) as state:
                reserve(state, operation_id='op', account_id='old-id', digest=digest, review=review, owner='alice')
                save(state_path, state)
            snapshot = lifecycle.preview_owner_rename('alice', 'bob', account_id='old-id')
            lifecycle.reconcile_owner_rename('alice', 'bob', snapshot, account_id='old-id')
            with transaction(state_path) as state:
                assert not owned_rows(state, 'bob', 'new-id')
                assert 'op' in owned_rows(state, 'bob', 'old-id')
                advance(state, operation_id='op', account_id='old-id', digest=digest,
                        revision=1, expected='prepared', target='dispatching')
                save(state_path, state)
            with self.assertRaises(CookbookStateError):
                lifecycle.preview_owner_rename('bob', 'tomb', account_id='old-id')
            with transaction(state_path) as state:
                state['_pinned_acquisitions_v1']['op']['state'] = 'failed'
                save(state_path, state)
            snapshot = lifecycle.preview_owner_rename('bob', 'tomb', account_id='old-id')
            token = 'b'*32
            lifecycle.stage_owner_to_tombstone('bob', 'tomb', snapshot, operation_token=token, account_id='old-id')
            with patch.object(lifecycle, '_purge_jobs', side_effect=RuntimeError('lost checkpoint')):
                with self.assertRaises(RuntimeError):
                    lifecycle.purge_owner('tomb', operation_token=token, account_id='old-id')
            with transaction(state_path) as state:
                assert not owned_rows(state, 'tomb', 'old-id')
                reserve(state, operation_id='op', account_id='old-id', digest=digest, review=review, owner='tomb')
                state['_pinned_acquisitions_v1']['op']['revision'] = 7
                save(state_path, state)
            with self.assertRaisesRegex(Exception, 'changed'):
                lifecycle.purge_owner('tomb', operation_token=token, account_id='old-id')
            with transaction(state_path) as state:
                del state['_pinned_acquisitions_v1']['op']
                save(state_path, state)
            assert lifecycle.purge_owner('tomb', operation_token=token, account_id='old-id')['state'] == 'purged'
