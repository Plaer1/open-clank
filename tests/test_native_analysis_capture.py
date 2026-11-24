"""Disposable capture fixture; no app, target or provider installation."""
import hashlib
import unittest
from types import SimpleNamespace

class CaptureTest(unittest.IsolatedAsyncioTestCase):
    async def test_actual_c2_capture_finalize_and_stale(self):
        import ast, asyncio, sys, types
        from contextlib import asynccontextmanager
        from pathlib import Path
        from unittest.mock import patch
        from src.openclank.files_facade import FilesFacade, ProviderContent, ProviderResource
        data=b'benign source'
        class Provider:
            name='host'; changed=False
            async def stat(self,context,*,origin_id):
                return ProviderResource(origin_id,'x','file',('download','stat'),size=len(data),revision={'kind':'v','value':'2' if self.changed else '1'})
            async def content(self,context,*,origin_id):
                async def stream(offset,length): yield data[offset:offset+length]
                return ProviderContent(origin_id,'x','application/octet-stream',size=len(data),stream=stream)
        provider=Provider(); facade=FilesFacade([provider])
        facade._provider_for_ref=lambda *a,**kw:(provider,SimpleNamespace(origin_id='src',provider='host'))
        tree=ast.parse(Path('src/openclank/acp_bridge.py').read_text())
        cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='ACPBridge')
        cls.body=[x for x in cls.body if getattr(x,'name','')=='_handle_native_analysis_source']
        namespace={}; exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),'bridge-fixture','exec'),namespace)
        bridge=namespace['ACPBridge'].__new__(namespace['ACPBridge'])
        identity={'sessionID':'session','rootOperationID':'root','actorID':'actor','executionRevision':1,'innerTurnID':'turn'}
        entry={'state':'active','owner':'user','ownerAccountID':'account','policyRevision':1,'stableChatID':'chat','authorityWorkspaceID':'workspace'}
        bridge._managed_turn_key=lambda _: 'key'; bridge._managed_execution_lock=asyncio.Lock(); bridge._managed_actor_turns={'key':entry}; bridge._managed_provider_generation='holder'
        async def allowed(_): return {'allowed':True}
        async def resolved(*_): return {'rootSessionID':'session'}
        binding={'owner':'user','stableChatID':'chat','authorityWorkspaceID':'workspace'}
        async def bound(_): return binding
        bridge._handle_managed_tool_admit=allowed; bridge.resolve_managed_actor_execution=resolved; bridge._handle_session_binding_read=bound
        class Auth:
            def account_id(self,_): return 'account'
            def username_for_account_id(self,_): return 'user'
            def is_admin(self,_): return False
        class Barrier:
            @asynccontextmanager
            async def admitted(self,_): yield
        worker=SimpleNamespace(_bridge=bridge,_managed_callbacks=SimpleNamespace(holder_id='holder'),is_alive=lambda:True)
        state=SimpleNamespace(auth_manager=Auth(),account_request_barrier=Barrier(),files_policy_repository=SimpleNamespace(generation=lambda:1),files_facade_factory=lambda *_:facade,mimo_supervisor=SimpleNamespace(worker_for_owner=lambda _:worker))
        models=types.ModuleType('core.models'); models.get_session_manager_instance=lambda:SimpleNamespace(lifecycle_context=state)
        with patch.dict(sys.modules,{'core.models':models}):
            req={**identity,'callID':'call','operation':'capture','sourceRef':'opaque-ref','expectedRevision':{'kind':'v','value':'1'}}
            captured=await bridge._handle_native_analysis_source(req)
            self.assertEqual(captured['state'],'captured')
            final={**identity,'callID':'call','operation':'finalize','captureToken':captured['captureToken'],'sourceSHA256':captured['sourceSHA256']}
            provider.changed=True
            self.assertEqual((await bridge._handle_native_analysis_source(final))['state'],'unavailable')
            provider.changed=False
            self.assertEqual((await bridge._handle_native_analysis_source(final))['state'],'unavailable')
    async def test_capture_and_stale(self):
        from src.openclank.analysis_adapters.base import capture_source, source_current, CaptureUnavailable
        from src.openclank.files_facade import FilesFacade, ProviderContext, ProviderContent, ProviderResource
        data=b'benign synthetic executable bytes'
        class Provider:
            name='host'; changed=False
            async def stat(self,context,*,origin_id):
                return ProviderResource(origin_id,'fixture.bin','file',('download','stat'),size=len(data),revision={'kind':'v','value':'2' if self.changed else '1'})
            async def content(self,context,*,origin_id):
                async def stream(offset,length): yield data[offset:offset+length]
                return ProviderContent(origin_id,'fixture.bin','application/octet-stream',size=len(data),stream=stream)
        provider=Provider(); facade=FilesFacade([provider]); ctx=ProviderContext('account','user',1)
        facade._provider_for_ref=lambda *a,**kw:(provider,SimpleNamespace(origin_id='src',provider='host'))
        revision={'kind':'v','value':'1'}
        capture=await capture_source(facade,ctx,'ref',revision)
        self.assertEqual(capture.sha256,hashlib.sha256(data).hexdigest())
        self.assertEqual(capture.data,data)
        provider.changed=True
        self.assertFalse(await source_current(facade,ctx,'ref',revision))
        with self.assertRaises(CaptureUnavailable): await capture_source(facade,ctx,'ref',revision)
