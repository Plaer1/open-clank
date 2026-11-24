"""Synthetic unsigned FastFile through the existing bounded Files inspector."""
import asyncio
import hashlib
import struct
import unittest
import zlib
from types import SimpleNamespace
from unittest.mock import patch


def source(payload, version=5, magic=b'IWffu100'):
    return magic + struct.pack('<I', version) + zlib.compress(payload)


def candidate(name, payload):
    return b'\xff'*4 + struct.pack('<I', len(payload)) + b'\xff'*4 + name + b'\0' + payload


class FastfileInspection(unittest.IsolatedAsyncioTestCase):
    async def test_unsigned_waw_v387(self):
        from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContent, ProviderContext, ProviderResource
        from src.openclank.format_adapters import fastfile
        record = candidate(b'v387.gsc', b'ab')
        payload = b'x' * (fastfile.CHUNK - 5) + record + record
        data = source(payload, version=387)
        class Provider:
            name = 'host'
            async def stat(self, context, *, origin_id):
                return ProviderResource(origin_id,'waw.ff','file',('stat','open','preview'),size=len(data),revision={'kind':'v','value':'1'})
            async def content(self, context, *, origin_id):
                async def stream(offset, length): yield data[offset:offset+length]
                return ProviderContent(origin_id,'waw.ff','application/octet-stream',size=len(data),stream=stream)
        provider=Provider(); facade=FilesFacade([provider]); context=ProviderContext('account','user',1)
        facade._provider_for_ref=lambda *args,**kwargs:(provider,SimpleNamespace(origin_id='src',provider='host'))
        async def inspect(): return await facade.inspect_safetensors(context,resource_ref='ref',expected_revision={'kind':'v','value':'1'},format_id='fastfile')
        result=await inspect()
        self.assertEqual((result['profile'],result['variant']),('fastfile-waw-v387-1','unsigned-waw-v387'))
        self.assertEqual([row['name'] for row in result['candidates']],['v387.gsc','v387.gsc'])
        self.assertEqual(result['candidates'][0]['decompressedOffset'],fastfile.CHUNK - 5 + 12 + len(b'v387.gsc') + 1)
        self.assertEqual(result['contentSHA256'],hashlib.sha256(data).hexdigest())
        for invalid in (source(payload,version=388), b'IWffu100'+struct.pack('>I',387)+zlib.compress(payload), source(payload,version=387,magic=b'IWff0100')):
            data=invalid
            with self.assertRaises(FilesFacadeError): await inspect()

    async def test_chunk_boundary_and_scan_yield(self):
        from src.openclank.format_adapters import fastfile
        marker = candidate(b'edge.gsc', b'x')
        data = source(b'a' * (fastfile.CHUNK - 5) + marker + b'b' * fastfile.CHUNK)
        async def read(offset, length): return data[offset:offset+length]
        result = await fastfile.inspect(read, len(data))
        self.assertEqual(result['candidateCount'], 1)
        self.assertEqual(result['candidates'][0]['decompressedOffset'], fastfile.CHUNK - 5 + 12 + len(b'edge.gsc') + 1)

        data = source(b'x' * (fastfile.CHUNK * 3))
        original = fastfile._PATTERN
        loop = asyncio.get_running_loop()
        observed = {'progress':False, 'scans':0, 'later':False}
        class Instrumented:
            def finditer(self, window):
                observed['scans'] += 1
                if observed['scans'] == 1:
                    loop.call_soon(observed.__setitem__, 'progress', True)
                elif observed['progress']:
                    observed['later'] = True
                return original.finditer(window)
        with patch.object(fastfile, '_PATTERN', Instrumented()):
            result = await fastfile.inspect(read, len(data))
        self.assertTrue(observed['later'])
        self.assertEqual(result['candidateCount'], 0)

        class CancelDuringScan:
            def finditer(self, window):
                loop.call_soon(asyncio.current_task().cancel)
                return original.finditer(window)
        with patch.object(fastfile, '_PATTERN', CancelDuringScan()):
            with self.assertRaises(asyncio.CancelledError):
                await fastfile.inspect(read, len(data))

    async def test_bounded_source_and_refusals(self):
        from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContent, ProviderContext, ProviderResource
        from src.openclank.format_adapters import fastfile
        class Provider:
            name = 'host'
            def __init__(self, data): self.data=data; self.changed=False; self.after=False; self.ranges=[]
            async def stat(self, context, *, origin_id):
                return ProviderResource(origin_id,'sample.ff','file',('stat','open','preview'),size=len(self.data),revision={'kind':'v','value':'2' if self.changed else '1'})
            async def content(self, context, *, origin_id):
                async def stream(offset, length):
                    self.ranges.append((offset,length)); yield self.data[offset:offset+length]
                    if self.after and offset: self.changed=True
                return ProviderContent(origin_id,'sample.ff','application/octet-stream',size=len(self.data),stream=stream)
        inner=candidate(b'dup.gsc',b'x')
        data=source(candidate(b'dup.gsc',inner) + candidate(b'dup.gsc',b'z'))
        provider=Provider(data); facade=FilesFacade([provider]); context=ProviderContext('account','user',1)
        facade._provider_for_ref=lambda *args,**kwargs:(provider,SimpleNamespace(origin_id='src',provider='host'))
        async def inspect(): return await facade.inspect_safetensors(context,resource_ref='ref',expected_revision={'kind':'v','value':'1'},format_id='fastfile')
        result=await inspect()
        self.assertEqual(result['candidateCount'],3)
        self.assertEqual([v['name'] for v in result['candidates']],['dup.gsc']*3)
        self.assertLess(result['candidates'][1]['decompressedOffset'],result['candidates'][0]['decompressedOffset']+result['candidates'][0]['declaredLength'])
        self.assertEqual(result['contentSHA256'],hashlib.sha256(data).hexdigest())
        self.assertEqual(result['coverage'],'heuristic_non_exhaustive')
        provider.after=True
        with self.assertRaises(FilesFacadeError): await inspect()
        provider.after=False; provider.changed=False
        for invalid in (source(b'a',magic=b'IWff0100'),source(b'a',version=388),data[:-1],data+b'trailing',b'IWffu100'+struct.pack('<I',5)+b'not-zlib'):
            provider.data=invalid
            with self.assertRaises(FilesFacadeError): await inspect()
        provider.data=source(b'x'*4096)
        with patch.object(fastfile,'MAX_EXPANDED',128):
            with self.assertRaises(FilesFacadeError): await inspect()
