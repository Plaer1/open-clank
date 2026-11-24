"""One synthetic worker/receipt test; no SDK child or network."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from src.openclank.cookbook_acquisition import reviewed_request
from src.openclank.cookbook_state import transaction, reserve, advance, save, CookbookStateError
from src.openclank.cookbook_worker import run, ChildSettled


class WorkerTest(unittest.TestCase):
    def test_claim_and_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'cache'; root.mkdir()
            info = root.stat(); payload = b'abc'
            cache = root/('acquisition-'+'a'*32); cache.mkdir(); cache_info=cache.stat()
            repo = cache/'models--org--model'; blob = repo/'blobs'/'blob'; blob.parent.mkdir(parents=True); blob.write_bytes(payload)
            snap = repo/'snapshots'/('a'*40); snap.mkdir(parents=True); (snap/'file').symlink_to('../../blobs/blob')
            manifest = {'repoID':'org/model','revision':'a'*40,'gating':{'state':'ungated'},
                        'license':{'value':None,'source':None,'status':'unknown'},
                        'files':[{'path':'file','size':3,'blobID':None,'lfsSHA256':hashlib.sha256(payload).hexdigest()}]}
            review,digest = reviewed_request(manifest=manifest,selected_paths=['file'],
                    destination={'path':str(root),'device':info.st_dev,'inode':info.st_ino})
            path = Path(tmp)/'state.json'
            with transaction(path) as state:
                reserve(state,operation_id='op',account_id='account',digest=digest,review=review,owner='alice')
                state['_pinned_acquisitions_v1']['op']['cache']={'phase':'created','name':cache.name,'device':cache_info.st_dev,'inode':cache_info.st_ino}
                advance(state,operation_id='op',account_id='account',digest=digest,revision=1,expected='prepared',target='dispatching')
                save(path,state)
            calls=[]
            receipt = run(path,'op',digest,2,lambda _,fd: calls.append(1) or ChildSettled(True,[f'/proc/self/fd/{fd}/models--org--model/snapshots/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/file']))
            self.assertEqual(receipt['files'][0]['sha256'],hashlib.sha256(payload).hexdigest())
            with self.assertRaises(CookbookStateError):
                run(path,'op',digest,2,lambda _,fd: calls.append(2))
            self.assertEqual(calls,[1])
            with transaction(path) as state:
                reserve(state,operation_id='bad',account_id='account',digest=digest,review=review,owner='alice')
                state['_pinned_acquisitions_v1']['bad']['cache']={'phase':'created','name':cache.name,'device':cache_info.st_dev,'inode':cache_info.st_ino}
                advance(state,operation_id='bad',account_id='account',digest=digest,revision=1,expected='prepared',target='dispatching')
                save(path,state)
            blob.write_bytes(b'xyz')
            self.assertEqual(run(path,'bad',digest,2,lambda _,fd: ChildSettled(True,[f'/proc/self/fd/{fd}/models--org--model/snapshots/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/file']))['status'],'failed')
            with transaction(path) as state:
                reserve(state,operation_id='unknown',account_id='account',digest=digest,review=review,owner='alice')
                state['_pinned_acquisitions_v1']['unknown']['cache']={'phase':'created','name':cache.name,'device':cache_info.st_dev,'inode':cache_info.st_ino}
                advance(state,operation_id='unknown',account_id='account',digest=digest,revision=1,expected='prepared',target='dispatching')
                save(path,state)
            def lost(*args): raise RuntimeError('wait unknown')
            with self.assertRaises(RuntimeError): run(path,'unknown',digest,2,lost)
            with transaction(path) as state: self.assertEqual(state['_pinned_acquisitions_v1']['unknown']['state'],'running')
