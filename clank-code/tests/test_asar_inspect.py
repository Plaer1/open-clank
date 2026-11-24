"""Synthetic ASAR header ranges through the existing Files authority."""
import json
import struct
import unittest
from types import SimpleNamespace


def fixture(tree, payload=b'abcd'):
    text=json.dumps(tree,separators=(',',':')).encode()
    padded=text + bytes((-len(text)) % 4)
    header=struct.pack('<Ii',4+len(padded),len(text))+padded
    return struct.pack('<II',4,len(header))+header+payload,8+len(header)


class AsarInspection(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_range_and_refusal(self):
        from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContent, ProviderContext, ProviderResource
        class Provider:
            name='host'
            def __init__(self,data): self.data=data; self.ranges=[]; self.changed=False; self.change_after_read=False
            async def stat(self,context,*,origin_id):
                return ProviderResource(origin_id,'app.asar','file',('stat','open','preview'),size=len(self.data),revision={'kind':'v','value':'2' if self.changed else '1'})
            async def content(self,context,*,origin_id):
                async def stream(offset,length):
                    self.ranges.append((offset,length)); yield self.data[offset:offset+length]
                    if self.change_after_read and offset: self.changed=True
                return ProviderContent(origin_id,'app.asar','application/octet-stream',size=len(self.data),stream=stream)
        tree={'files':{'a':{'size':4,'offset':'0'},'b':{'size':4,'offset':'0'},'external':{'size':8,'unpacked':True},'link':{'link':'a'}}}
        data,end=fixture(tree); provider=Provider(data); facade=FilesFacade([provider]); ctx=ProviderContext('account','user',1)
        facade._provider_for_ref=lambda *a,**kw:(provider,SimpleNamespace(origin_id='src',provider='host'))
        async def inspect(): return await facade.inspect_safetensors(ctx,resource_ref='ref',expected_revision={'kind':'v','value':'1'},format_id='asar')
        result=await inspect()
        self.assertEqual(result['entryCount'],4); self.assertEqual(result['entries'][0]['offset'],result['entries'][1]['offset'])
        self.assertEqual(provider.ranges,[(0,8),(8,end-8)])
        self.assertEqual(result['payloadIntegrity'],'not_checked')
        provider.change_after_read=True
        with self.assertRaises(FilesFacadeError): await inspect()
        provider.changed=False; provider.change_after_read=False; provider.data=data[:end-1]
        with self.assertRaises(FilesFacadeError): await inspect()
        provider.data,_=fixture({'files':{'../bad':{'size':0,'offset':'0'}}})
        with self.assertRaises(FilesFacadeError): await inspect()
        nested={'size':0,'offset':'0'}
        for _ in range(33): nested={'files':{'x':nested}}
        provider.data,_=fixture({'files':{'x':nested}})
        with self.assertRaises(FilesFacadeError): await inspect()
