"""Synthetic GGUF declarations through the existing bounded Host stream."""
import struct
import unittest
from types import SimpleNamespace


def string(value):
    raw=value.encode(); return struct.pack('<Q',len(raw))+raw


def fixture(kind=0, metadata_kind=4):
    header=(b'GGUF'+struct.pack('<IQQ',3,1,1)+string('general.alignment')+struct.pack('<II',metadata_kind,32)
            +string('weight')+struct.pack('<IQIQ',1,2,kind,0))
    return header, header + bytes((-len(header)) % 32) + bytes(8)


class GGUFTest(unittest.IsolatedAsyncioTestCase):
    async def test_range_stale_and_unknown(self):
        from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContent, ProviderContext, ProviderResource
        class Provider:
            name='host'
            def __init__(self,data): self.data=data; self.ranges=[]; self.changed=False
            async def stat(self,context,*,origin_id):
                return ProviderResource(origin_id,'model.gguf','file',('stat','open','preview'),size=len(self.data),revision={'kind':'v','value':'2' if self.changed else '1'})
            async def content(self,context,*,origin_id):
                async def stream(offset,length):
                    self.ranges.append((offset,length)); yield self.data[offset:offset+length]
                return ProviderContent(origin_id,'model.gguf','application/octet-stream',size=len(self.data),stream=stream)
        header,data=fixture(); provider=Provider(data); facade=FilesFacade([provider]); ctx=ProviderContext('account','user',1)
        facade._provider_for_ref=lambda *a,**kw:(provider,SimpleNamespace(origin_id='src',provider='host'))
        async def inspect(): return await facade.inspect_safetensors(ctx,resource_ref='ref',expected_revision={'kind':'v','value':'1'},format_id='gguf')
        result=await inspect()
        self.assertEqual(result['tensors'][0]['declaredBytes'],8)
        self.assertEqual(result['headerRange']['length'],len(header))
        self.assertTrue(all(start+length <= len(header) for start,length in provider.ranges))
        self.assertIsNone(result['contentSHA256'])
        provider.changed=True
        with self.assertRaises(FilesFacadeError): await inspect()
        provider.changed=False; _,provider.data=fixture(kind=999)
        self.assertEqual((await inspect())['unknownTensorSizes'],1)
        _,provider.data=fixture(metadata_kind=999)
        with self.assertRaises(FilesFacadeError): await inspect()
