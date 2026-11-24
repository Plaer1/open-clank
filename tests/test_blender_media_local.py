"""Synthetic fixed-child J0 and real ArtifactStore boundary; no subprocess."""
import hashlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.database import Base
from core.operation_models import OperationBase
from src import bg_jobs
from src.openclank.artifacts import ArtifactStore, media_artifact_owner
from src.openclank.blender_child import artifact_authority, run, SettledRender


class BlenderLocalTest(unittest.TestCase):
 def test_claim_custody_and_unknown(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); engine=create_engine(f'sqlite:///{root / "artifact.db"}'); Base.metadata.create_all(engine); OperationBase.metadata.create_all(engine)
   store=ArtifactStore(root/'artifacts',session_factory=sessionmaker(bind=engine))
   doc=json.dumps({'asset':{'version':'2.0'},'buffers':[]},separators=(',',':')).encode(); doc+=b' '*((-len(doc))%4)
   source=struct.pack('<4sII',b'glTF',2,20+len(doc))+struct.pack('<I4s',len(doc),b'JSON')+doc
   input_hash=hashlib.sha256(source).hexdigest(); image=Image.new('RGBA',(512,512),'red'); out=io.BytesIO(); image.save(out,format='PNG'); image.close(); png=out.getvalue()
   identity={'account_id':'account','owner':'alice','session_id':'chat','workspace_id':'workspace','actor_id':'actor','turn_id':'turn','root_operation_id':'root','attempt_id':'attempt','request_digest':'a'*64,'adapter_id':'blender-glb-png-v1','adapter_version':'1','operation':'image.render','route_id':'local','route_kind':'local','model_revision':'v1','policy_generation':1,'input_hashes':[input_hash]}
   profile={'adapter':'blender-glb-png-v1','executable_sha256':'b'*64,'executable_version':'4.0','runner_sha256':'c'*64,'input_sha256':input_hash,'artifact_authority':artifact_authority(store)}
   with patch.object(bg_jobs,'_STORE',root/'jobs.json'), patch.object(bg_jobs,'_JOBS_DIR',root/'jobs'):
    job=bg_jobs.reserve_media(identity); claim=bg_jobs.claim_media(job['id'],account_id='account',revision=1,action='submit')
    child=bg_jobs.reserve_media_local_child(job['id'],account_id='account',token=claim['token'],profile=profile)
    self.assertTrue(run(job_id=job['id'],account_id='account',child_id=child['child_id'],nonce=child['nonce'],source=source,execute=lambda *_:SettledRender(0,png),artifact_store=store))
    row=bg_jobs.get_media(job['id'],account_id='account'); receipt=row['local_child']['receipt']
    retained=b''.join(store.read_chunks(owner=media_artifact_owner('account'),artifact_id=receipt['artifact_id']))
    self.assertEqual(retained,png)
    with self.assertRaises(ValueError): bg_jobs.claim_media_local_child(job['id'],account_id='account',child_id=child['child_id'],nonce=child['nonce'])
    other=bg_jobs.reserve_media({**identity,'root_operation_id':'other','attempt_id':'other'})
    claim=bg_jobs.claim_media(other['id'],account_id='account',revision=1,action='submit'); child=bg_jobs.reserve_media_local_child(other['id'],account_id='account',token=claim['token'],profile=profile)
    with self.assertRaises(RuntimeError): run(job_id=other['id'],account_id='account',child_id=child['child_id'],nonce=child['nonce'],source=source,execute=lambda *_:None,artifact_store=store)
    self.assertEqual(bg_jobs.get_media(other['id'],account_id='account')['local_child']['state'],'running')
   engine.dispose()
