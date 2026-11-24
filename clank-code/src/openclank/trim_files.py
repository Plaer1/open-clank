"""Exact Host source and distinct destination authority for one trim."""
from pathlib import Path
from src.openclank.files_host_provider import _path
from src.openclank.trim_contract import ADAPTER,validate_binding
from src.openclank.trim_snapshot import snapshot_video


class TrimFilesScope:
    def __init__(self, *, facade, context_for, session_current):
        self.facade,self.context_for,self.session_current=facade,context_for,session_current

    async def _distinct(self,context,source_ref,destination_ref,name):
        if (not isinstance(name,str) or not name or len(name.encode('utf-8'))>240 or name in ('.','..')
            or any(char in name for char in ('/','\\','\x00')) or not name.lower().endswith('.webm')):
            raise ValueError('trim destination name unavailable')
        source_provider,source=self.facade._provider_for_ref(context,source_ref,capability='download')
        destination_provider,destination=self.facade._provider_for_ref(context,destination_ref,capability='write')
        if (source.provider!='host' or destination.provider!='host' or source_provider is not destination_provider
            or source.kind!='file' or destination.kind!='folder'
            or (source.workspace_id and source.workspace_id!=context.workspace_id)
            or (destination.workspace_id and destination.workspace_id!=context.workspace_id)):
            raise ValueError('trim source or destination unavailable')
        source_path=Path(_path(source.origin_id)).resolve(strict=True)
        folder=Path(_path(destination.origin_id)).resolve(strict=True)
        if source_path==folder/name: raise ValueError('trim must not overwrite source')
        return source_provider,source,destination

    async def capture(self,identity,*,source_ref,source_revision,destination_ref,destination_name,helper_sha256,start_ms,end_ms):
        if not await self.session_current(identity): raise ValueError('trim scope unavailable')
        context=await self.context_for(identity)
        await self._distinct(context,source_ref,destination_ref,destination_name)
        async def current(): return bool(await self.session_current(identity) and self.facade._generation_current(context))
        evidence,check=await self.facade._host_video_source(context,source_ref=source_ref,
            expected_revision=source_revision,current=current)
        destination=await self.facade.capture_media_destination(context,resource_ref=destination_ref)
        if not await check() or not await current(): raise ValueError('trim source changed')
        binding={'interface':ADAPTER,'ownerAccountID':identity['account_id'],'source_ref':source_ref,
            'source_resource_id':evidence['source_resource_id'],'source_revision':evidence['source_revision'],
            'source_sha256':evidence['source_sha256'],'source_size':evidence['source_size'],
            'source_object_tag':evidence['source_object_tag'],'destination_ref':destination['destination_ref'],
            'destination_revision':destination['destination_revision'],'destination_name':destination_name,
            'helper_sha256':helper_sha256,'start_ms':start_ms,'end_ms':end_ms}
        return validate_binding(binding,account=identity['account_id'],hashes=[evidence['source_sha256']]),destination['destination_label']

    async def recovery_current(self,identity,context,destination_ref,destination_name,destination_revision):
        try:
            if not await self.session_current(identity): return False
            binding=identity['resource_binding']
            provider,source,_=await self._distinct(context,binding['source_ref'],destination_ref,destination_name)
            observed=await provider.stat_inspection(context,origin_id=source.origin_id)
            destination=await self.facade.capture_media_destination(context,resource_ref=destination_ref)
            from src.openclank.resource_refs import stable_resource_id
            return bool(observed.origin_id==source.origin_id and observed.kind=='file' and observed.size==binding['source_size']
                and stable_resource_id(owner_subject_id=context.owner_subject_id,provider='host',origin_id=source.origin_id)==binding['source_resource_id']
                and observed.revision=={'kind':'hostObjectIdentity','value':binding['source_object_tag']}
                and destination['destination_revision']==destination_revision and self.facade._generation_current(context)
                and await self.session_current(identity))
        except Exception:return False

    async def current(self,identity):
        try:
            context=await self.context_for(identity);binding=identity['resource_binding']
            return await self.recovery_current(identity,context,binding['destination_ref'],binding['destination_name'],binding['destination_revision'])
        except Exception:return False

    async def control(self,identity):
        return (identity.get('resource_binding') or {}).get('ownerAccountID')==identity.get('account_id') and await self.session_current(identity)

    async def snapshot(self,identity):
        if not await self.current(identity): raise ValueError('trim source changed')
        context=await self.context_for(identity);binding=identity['resource_binding']
        result=await snapshot_video(self.facade,context,source_ref=binding['source_ref'],source_revision=binding['source_revision'],
            current=lambda:self.current(identity))
        try:
            evidence=result['evidence']
            if (evidence['source_resource_id']!=binding['source_resource_id'] or evidence['source_sha256']!=binding['source_sha256']
                or evidence['source_size']!=binding['source_size'] or evidence['source_object_tag']!=binding['source_object_tag']
                or not await self.current(identity)): raise ValueError('trim snapshot changed')
            return result
        except BaseException:
            import os
            os.close(result['fd']);raise
