"""Synthetic headers and bounded fake provider; no model payloads."""
import hashlib
import json
import struct
import unittest
from types import SimpleNamespace

from src.openclank.format_adapters import safetensors as st


def model(header, payload=b''):
    raw = json.dumps(header, separators=(',', ':')).encode()
    return struct.pack('<Q', len(raw)) + raw + payload


class InspectTest(unittest.IsolatedAsyncioTestCase):
    async def test_header_ranges_and_stale(self):
        from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContent, ProviderContext, ProviderResource
        data = model({'scalar':{'dtype':'F32','shape':[],'data_offsets':[0,4]},
                      'empty':{'dtype':'U8','shape':[0],'data_offsets':[4,4]}}, b'ABCD')
        class Provider:
            name = 'host'
            ranges = []
            changed = False
            async def stat(self, context, *, origin_id):
                return ProviderResource(origin_id,'x.safetensors','file',('preview','stat','open'),size=len(data),revision={'kind':'v','value':'2' if self.changed else '1'})
            async def content(self, context, *, origin_id):
                async def stream(offset, length):
                    self.ranges.append((offset,length))
                    yield data[offset:offset+length]
                return ProviderContent(origin_id,'x.safetensors','application/octet-stream',stream=stream,size=len(data))
        provider=Provider(); facade=FilesFacade([provider]); context=ProviderContext('account','user',1)
        facade._provider_for_ref=lambda *a, **kw:(provider,SimpleNamespace(origin_id='src',provider='host'))
        result=await facade.inspect_safetensors(context,resource_ref='ref',expected_revision={'kind':'v','value':'1'})
        self.assertEqual(result['tensorCount'],2)
        self.assertEqual(provider.ranges,[(0,8),(8,len(data)-12)])
        self.assertEqual(result['headerSHA256'],hashlib.sha256(data[8:-4]).hexdigest())
        self.assertIsNone(result['contentSHA256'])
        provider.changed=True
        with self.assertRaises(FilesFacadeError):
            await facade.inspect_safetensors(context,resource_ref='ref',expected_revision={'kind':'v','value':'1'})
        for header in ({'a':{'dtype':'UNKNOWN','shape':[1],'data_offsets':[0,4]}}, {'a':{'dtype':'F32','shape':[2],'data_offsets':[0,4]}}):
            broken=model(header,b'ABCD')
            with self.assertRaises(st.Unsupported): st.inspect(broken[:8],broken[8:-4],len(broken))
        with self.assertRaises(st.Unsupported): st.header_length(b'\xff'*8,10)
