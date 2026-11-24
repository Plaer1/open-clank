"""Current Files source and destination for the fixed local TRELLIS job."""
import io
import warnings
from PIL import Image
from src.openclank.media_adapters.trim import _settled_thread


def _validate_image(data):
    if type(data) is not bytes or not 0 < len(data) <= 4 * 1024 * 1024:
        raise ValueError('TRELLIS image unavailable')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as image:
            if (image.format != 'PNG' or image.mode != 'RGBA' or getattr(image,'n_frames',1) != 1
                    or getattr(image,'is_animated',False) or not 1 <= image.width <= 4096
                    or not 1 <= image.height <= 4096 or image.width * image.height > 16777216):
                raise ValueError('TRELLIS image unavailable')
            image.load()
            low,high=image.getchannel('A').getextrema()
            if low >= 255 or high <= 204: raise ValueError('TRELLIS foreground unavailable')


class TrellisFilesScope:
    _validate_image = staticmethod(_validate_image)
    def __init__(self, *, facade, context_for, session_current, resolve_profile):
        self.facade,self.context_for,self.session_current,self.resolve_profile=facade,context_for,session_current,resolve_profile

    async def _context(self, identity):
        context=await self.context_for(identity)
        if (context.owner_subject_id!=identity['account_id'] or context.workspace_id!=identity['workspace_id']
                or context.policy_generation!=identity['policy_generation'] or not await self.session_current(identity)):
            raise ValueError('TRELLIS scope changed')
        return context

    async def capture(self, identity, *, source_ref, destination_ref, destination_name):
        context=await self._context(identity)
        async with self.resolve_profile(identity) as (profile,check):
            source,data=await self.facade.capture_media_image(context,resource_ref=source_ref)
            if source.get('mime_type')!='image/png': raise ValueError('TRELLIS image unavailable')
            await _settled_thread(self._validate_image,data)
            destination=await self.facade.capture_media_destination(context,resource_ref=destination_ref)
            await check()
            if not await self.session_current(identity): raise ValueError('TRELLIS session changed')
            binding={**profile.job_binding(),'destination_ref':destination['destination_ref'],
                'destination_revision':destination['destination_revision'],'destination_name':destination_name,
                'project_ref':None,'project_revision':None,'image_source':source,'seed':identity['resource_binding']['seed']}
            return binding,{'source':source,'folder':destination['destination_label']}

    async def source(self, identity):
        if not await self.current(identity): raise ValueError('TRELLIS source changed')
        context=await self._context(identity);expected=identity['resource_binding']['image_source']
        evidence,data=await self.facade.capture_media_image(context,resource_ref=expected['ref'],expected_revision=expected['revision'])
        await _settled_thread(self._validate_image,data)
        if evidence!=expected or identity['input_hashes']!=[evidence['sha256']] or not await self.session_current(identity):
            raise ValueError('TRELLIS source changed')
        return evidence,data

    async def control(self, identity):
        binding=identity.get('resource_binding') or {}
        return binding.get('interface')=='trellis-image3d-v1' and binding.get('ownerAccountID')==identity.get('account_id')

    async def current(self, identity):
        try:
            context=await self._context(identity);binding=identity['resource_binding']
            async with self.resolve_profile(identity) as (profile,check):
                actual=profile.job_binding()
                if any(actual.get(k)!=binding.get(k) for k in actual): return False
                expected=binding['image_source']
                source,data=await self.facade.capture_media_image(context,resource_ref=expected['ref'],expected_revision=expected['revision'])
                await _settled_thread(self._validate_image,data)
                destination=await self.facade.capture_media_destination(context,resource_ref=binding['destination_ref'])
                await check()
                return (source==expected and identity['input_hashes']==[source['sha256']]
                    and destination['destination_revision']==binding['destination_revision'] and await self.session_current(identity))
        except Exception:
            return False
