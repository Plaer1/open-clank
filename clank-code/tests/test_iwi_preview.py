"""Synthetic block fixtures only; no game or external assets."""
import io
import struct
import unittest
from types import SimpleNamespace

from PIL import Image

from src.openclank.format_adapters.iwi import IwiUnsupported, inspect, preview_png, thumbnail_png


def fixture(fmt, payload):
    end = 28 + len(payload)
    return struct.pack("<3sBBBHHHIIII", b"IWi", 6, fmt, 3, 4, 4, 1, end, end, end, end) + payload


class IwiPreviewTest(unittest.TestCase):
    def test_dxt_preview_and_refusals(self):
        color = struct.pack("<HHI", 0xF800, 0x07E0, 0)
        cases = [
            (0x0B, color, 255),
            (0x0C, bytes([0x88] * 8) + color, 136),
            (0x0D, bytes([128, 0]) + bytes(6) + color, 128),
        ]
        for fmt, payload, alpha in cases:
            original = fixture(fmt, payload)
            info, png = preview_png(original)
            self.assertEqual((info.width, info.height), (4, 4))
            self.assertEqual(Image.open(io.BytesIO(png)).convert("RGBA").getpixel((0, 0)), (255, 0, 0, alpha))
            _, thumbnail = thumbnail_png(original, 2, 2, 1)
            with Image.open(io.BytesIO(thumbnail)) as image:
                self.assertEqual(image.size, (2, 2))
                self.assertEqual(image.convert("RGBA").getpixel((0, 0)), (255, 0, 0, alpha))
            self.assertEqual(original, fixture(fmt, payload))
            with self.assertRaises(IwiUnsupported):
                inspect(original[:-1])
        with self.assertRaises(IwiUnsupported):
            inspect(fixture(0x0B, color)[:5] + b"\x04" + fixture(0x0B, color)[6:])


class FacadePreviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_copy_uses_import_receipt_and_refuses_stale(self):
        import hashlib
        from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContent, ProviderContext, ProviderResource
        original = fixture(0x0B, struct.pack("<HHI", 0xF800, 0x07E0, 0))
        info, png = preview_png(original)
        class Provider:
            name = "host"
            calls = 0
            collision = False
            async def stat(self, context, *, origin_id):
                if origin_id == 'dest':
                    return ProviderResource('dest', 'Folder', 'folder', ('stat','children','write'), revision={'kind':'v','value':'1'})
                return ProviderResource('src', 'a.iwi', 'file', ('stat','open','preview'), size=len(original), revision={'kind':'v','value':'1'})
            async def content(self, context, *, origin_id):
                return ProviderContent('src','a.iwi','application/octet-stream',data=original,size=len(original))
            async def stage_import(self, context, *, destination_origin_id, name, upload, operation_id, item_id):
                self.calls += 1
                data = await upload.read(1000000)
                return {'stage_id':'stage','digest':'sha256:'+hashlib.sha256(data).hexdigest(),'length':len(data)}
            async def finish_import(self, context, **kwargs):
                if self.collision:
                    return {'state':'partial','items':[{'item_id':kwargs['item_id'],'outcome':'conflict','code':'name_conflict'}]}
                return {'state':'complete','items':[{'item_id':kwargs['item_id'],'outcome':'committed'}]}
        provider = Provider(); facade = FilesFacade([provider]); context = ProviderContext('account','user',1)
        facade._provider_for_ref = lambda ctx, ref, **kwargs: (provider, SimpleNamespace(origin_id='dest' if ref == 'dest' else 'src', provider='host', kind='folder', stable_id='destination'))
        request = {'operation_id':'op','item_id':'item','generation':1,'source_ref':'src','revision':{'kind':'v','value':'1'},
            'source_sha256':info.source_sha256,'decoder':info.decoder_generation,'png_sha256':hashlib.sha256(png).hexdigest(),
            'destination_ref':'dest','name':'copy.png'}
        result = await facade.save_iwi_png_copy(context, request=request)
        self.assertEqual(result['items'][0]['outcome'], 'committed')
        self.assertEqual((await facade.save_iwi_png_copy(context, request=request))['state'], 'complete')
        self.assertEqual(provider.calls, 1)
        with self.assertRaises(FilesFacadeError):
            await facade.save_iwi_png_copy(context, request={**request, 'operation_id':'other', 'source_sha256':'0'*64})
        self.assertEqual(provider.calls, 1)
        provider.collision = True
        conflict = await facade.save_iwi_png_copy(context, request={**request, 'operation_id':'collision'})
        self.assertEqual(conflict['items'][0]['outcome'], 'conflict')
    async def test_revision_bound_preview_and_changed_origin(self):
        from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContent, ProviderContext, ProviderResource

        original = fixture(0x0B, struct.pack("<HHI", 0xF800, 0x07E0, 0))
        class Provider:
            name = "host"
            changed = False
            async def stat(self, context, *, origin_id):
                return ProviderResource(origin_id, "sample.iwi", "file", ("stat", "open", "preview"),
                    size=len(original), revision={"kind":"hostFingerprint", "value":"new" if self.changed else "old"})
            async def content(self, context, *, origin_id):
                return ProviderContent(origin_id, "sample.iwi", "application/octet-stream", data=original, size=len(original))
        provider = Provider()
        facade = FilesFacade([provider])
        facade._provider_for_ref = lambda *args, **kwargs: (provider, SimpleNamespace(origin_id="one", provider="host"))
        context = ProviderContext("account", "user", 1)
        result = await facade._iwi_preview(context, resource_ref="ref", expected_revision={"kind":"hostFingerprint", "value":"old"})
        self.assertEqual(result.media_type, "image/png")
        self.assertEqual(result.preview_evidence["revision_value"], "old")
        self.assertEqual(result.preview_evidence["source_sha256"], __import__("hashlib").sha256(original).hexdigest())
        provider.changed = True
        with self.assertRaises(FilesFacadeError):
            await facade._iwi_preview(context, resource_ref="ref", expected_revision={"kind":"hostFingerprint", "value":"old"})


if __name__ == "__main__":
    unittest.main()
