"""Focused synthetic Host folder attachment and exclusive publication."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContent, ProviderContext, ProviderResource
from src.openclank.media_attachment_targets import HostDocumentAttachmentTarget


class Store:
    def __init__(self): self.rows={}; self.fail_complete=False
    def generation(self, *args): return 1
    def get_operation(self, *, owner_subject_id, operation_id): return self.rows.get((owner_subject_id,operation_id))
    def record_operation(self, *, owner_subject_id, operation_id, request_digest, generation, receipt, phase=None):
        if self.fail_complete and operation_id == '__host_media__uncertain' and receipt.get('phase') == 'complete': raise OSError('lost receipt')
        self.rows[(owner_subject_id,operation_id)]={'digest':request_digest,'generation':generation,'receipt':dict(receipt)}


class RootChoice(unittest.IsolatedAsyncioTestCase):
    async def test_host_root_and_no_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'docs').mkdir(); doc=root/'docs'/'note.md'; doc.write_text('original')
            src=root/'input.png'; src.write_bytes(b'input')
            rev={'kind':'test','value':'1'}; state={'root_rev':'1','source_rev':'1','source_drift':False,'revoke':False,'roots':[]}
            class Host:
                name='host'
                async def stat(self, context, *, origin_id):
                    path=Path(origin_id.removeprefix('host:'))
                    folder=path.is_dir()
                    return ProviderResource(origin_id,path.name,'folder' if folder else 'file',('stat','write','children') if folder else ('stat','write','read','download'),size=0 if folder else path.stat().st_size,revision={'kind':'test','value':state['root_rev'] if folder else state['source_rev'] if path == src else '1'})
                async def content(self, context, *, origin_id):
                    data=src.read_bytes()
                    async def stream(offset,length):
                        yield data[offset:offset+length]
                        if state['source_drift']: state['source_rev']='2'
                    return ProviderContent(origin_id,'input.png','image/png',size=len(data),stream=stream)
                async def _run_claimed_host_mutation(self,context,paths,callback,delayed_start=False):
                    return await callback(lambda:None,lambda:None)
            host=Host(); target_store=Store(); target=HostDocumentAttachmentTarget(provider=host,operation_store=target_store,workspace_roots=lambda owner:state['roots'])
            facade=FilesFacade([host],attachment_targets={'host_document':target}); context=ProviderContext('account','alice',1,workspace_id='default')
            other=root/'other'; other.mkdir()
            refs={'document-ref':f'host:{doc}','source-ref':f'host:{src}','root-ref':f'host:{root}','wrong-root-ref':f'host:{other}'}
            def resolve(_context, token, *, capability):
                if token=='root-ref' and state['revoke']: raise FilesFacadeError('revoked',code='resource_unavailable')
                return host,SimpleNamespace(origin_id=refs[token],provider='host',workspace_id=None)
            facade._provider_for_ref=resolve
            async def prepare(op, selected=False, root_token='root-ref'):
                body={'kind':'host_document','resource_ref':'document-ref','expected_revision':rev}
                if selected: body.update(root_ref=root_token,expected_root_revision=rev)
                return await facade.prepare_attachment(context,operation_id=op,generation=1,source={'resource_ref':'source-ref','expected_revision':rev},target=body,mode='link')
            old=await prepare('legacy'); self.assertEqual(old['provenance']['origin'],'loose'); self.assertEqual((root/'docs'/'media'/'note'/'input.png').read_bytes(),b'input')
            result=await prepare('selected',True); self.assertEqual(result['provenance']['origin'],'selected_root'); self.assertEqual(result['target_identity']['root_ref'],'root-ref'); self.assertNotIn('canonical_root',str(result))
            saved=root/'.media'/'docs'/'note'/'input.png'; self.assertEqual(saved.read_bytes(),b'input'); self.assertEqual(await prepare('selected',True),result)
            state['revoke']=True
            with self.assertRaises(FilesFacadeError): await prepare('revoked',True)
            state['revoke']=False; state['root_rev']='2'
            with self.assertRaises(FilesFacadeError): await prepare('stale',True)
            state['root_rev']='1'
            with self.assertRaises(FilesFacadeError): await prepare('wrong-root',True,'wrong-root-ref')
            state['roots']=[{'workspace_id':'foreign','owner_subject_id':'other','canonical_root':str(root/'docs')}]
            with self.assertRaises(FilesFacadeError): await prepare('foreign-nested',True)
            state['roots']=[{'workspace_id':'foreign','owner_subject_id':'other','canonical_root':str(root/'.media')}]
            with self.assertRaises(FilesFacadeError): await prepare('foreign-media-path',True)
            state['roots']=None
            with self.assertRaises(FilesFacadeError): await prepare('unknown-registry',True)
            state['roots']=[]
            state['source_drift']=True
            with self.assertRaises(FilesFacadeError): await prepare('source-drift',True)
            state['source_drift']=False; state['source_rev']='1'
            # Allocate a second name, then race another writer immediately before link.
            original_link=os.link
            def race(stage,destination,**kwargs):
                Path(destination).write_bytes(b'winner')
                return original_link(stage,destination,**kwargs)
            with patch('src.openclank.media_attachment_targets.os.link',race):
                with self.assertRaises(FilesFacadeError): await prepare('collision',True)
            self.assertEqual((saved.parent/'input-1.png').read_bytes(),b'winner')
            self.assertEqual(saved.read_bytes(),b'input'); self.assertEqual(doc.read_text(),'original')
            target_store.fail_complete=True
            with self.assertRaises(FilesFacadeError) as uncertain: await prepare('uncertain',True)
            self.assertEqual(uncertain.exception.code,'operation_pending')
            saved_uncertain=target_store.get_operation(owner_subject_id='account',operation_id='__host_media__uncertain')['receipt']
            self.assertEqual(saved_uncertain['phase'],'pending')
            self.assertTrue(Path(saved_uncertain['stage_path']).exists())
            self.assertIsNone(await target.operation_status(context,operation_id='uncertain'))
            self.assertEqual((await prepare('uncertain',True))['state'],'pending')
            pending={'operation_id':'old-pending','account_id':'account','generation':1,'phase':'pending','asset_path':str(saved),'source_digest':'sha256:'+__import__('hashlib').sha256(b'input').hexdigest()}
            target_store.rows[('account','__host_media__old-pending')]={'receipt':pending}
            self.assertIsNone(await target.operation_status(context,operation_id='old-pending'))

            # Extract the actual route handler and DTOs without importing unrelated
            # app services; Request.stream and the Files facade/target are real.
            import ast, asyncio, base64, copy, hashlib, json, re
            from typing import Literal
            from fastapi import HTTPException, Query, Request
            from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
            tree=ast.parse(Path('routes/files_facade_routes.py').read_text())
            names={'_StrictModel','AttachmentRevision','AttachmentTargetRequest','AttachmentUploadMetadata'}
            classes=[copy.deepcopy(node) for node in tree.body if isinstance(node,ast.ClassDef) and node.name in names]
            handler=next(copy.deepcopy(node) for node in ast.walk(tree) if isinstance(node,ast.AsyncFunctionDef) and node.name=='prepare_attachment_upload')
            handler.decorator_list=[]
            def raise_files(error): raise HTTPException(409 if error.code in {'idempotency_conflict','resource_changed','operation_pending'} else 400,detail={'code':error.code})
            namespace={'BaseModel':BaseModel,'ConfigDict':ConfigDict,'Field':Field,'StrictInt':StrictInt,'model_validator':model_validator,'Literal':Literal,'Request':Request,'Query':Query,'HTTPException':HTTPException,'FilesFacadeError':FilesFacadeError,'base64':base64,'hashlib':hashlib,'re':re,'asyncio':asyncio,'context':lambda request,workspace:context,'facade':lambda request,copal_workspace:facade,'_raise':raise_files}
            exec(compile(ast.fix_missing_locations(ast.Module(body=classes+[handler],type_ignores=[])),'<attachment-upload-route>','exec'),namespace)
            route=namespace['prepare_attachment_upload']; generation=1
            metadata={'operation_id':'upload-1','generation':generation,'target':{'kind':'host_document','resource_ref':'document-ref','expected_revision':rev,'root_ref':'root-ref','expected_root_revision':rev},'mode':'embed','name':'clipboard.png','mime':'image/png'}
            receives=[]
            def request(value, body, declared=None):
                encoded=base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).rstrip(b'=')
                sent=False
                async def receive():
                    nonlocal sent
                    receives.append(True)
                    if sent: return {'type':'http.disconnect'}
                    sent=True; return {'type':'http.request','body':body,'more_body':False}
                return Request({'type':'http','method':'POST','path':'/','headers':[(b'x-openclank-attachment',encoded),(b'content-length',str(len(body) if declared is None else declared).encode())]},receive)
            with self.assertRaises(HTTPException): await route(request(metadata,b'x',10*1024*1024+1),'default')
            self.assertEqual(receives,[])
            result=await route(request(metadata,b'clipboard'),'default')
            self.assertEqual(result['source_identity']['kind'],'client_upload')
            self.assertEqual(result['source_identity']['sha256'],hashlib.sha256(b'clipboard').hexdigest())
            self.assertEqual((saved.parent/'clipboard.png').read_bytes(),b'clipboard')
            replay=await route(request(metadata,b'clipboard'),'default'); self.assertEqual(replay,result)
            with self.assertRaises(HTTPException): await route(request(metadata,b'different'),'default')
            metadata['operation_id']='upload-revoked'; state['revoke']=True
            with self.assertRaises(HTTPException): await route(request(metadata,b'clipboard'),'default')
            state['revoke']=False; metadata['operation_id']='upload-collision'; metadata['name']='race.png'
            def upload_race(stage,destination,**kwargs):
                Path(destination).write_bytes(b'winner')
                return original_link(stage,destination,**kwargs)
            with patch('src.openclank.media_attachment_targets.os.link',upload_race):
                with self.assertRaises(HTTPException): await route(request(metadata,b'clipboard'),'default')
            self.assertEqual((saved.parent/'race.png').read_bytes(),b'winner'); self.assertEqual(doc.read_text(),'original')
