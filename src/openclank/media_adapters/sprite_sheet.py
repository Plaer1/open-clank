"""One CPU sheet in the existing local-child and artifact authority."""
import asyncio, hashlib
from src import bg_jobs
from src.openclank.artifacts import media_artifact_owner
from src.openclank.blender_child import artifact_authority
from src.openclank.media_adapters.base import NotDispatched, Submission
from src.openclank.media_adapters.blender_four import BlenderFourAdapter
from src.openclank.sprite_sheet import pack, layout_for, pack_sheet, selected_layout_for

class SpriteSheetAdapter(BlenderFourAdapter):
    adapter_id='sprite-sheet-v1'
    def __init__(self,*,scope,barrier,owner_for_account,artifact_store,before_dispatch=None):
        if before_dispatch is not None and not callable(before_dispatch): raise ValueError('sprite caller authority unavailable')
        self.scope,self.barrier,self.owner_for_account,self.artifact_store=scope,barrier,owner_for_account,artifact_store
        self.before_dispatch=before_dispatch

    async def _caller_current(self):
        if self.before_dispatch is not None and await self.before_dispatch() is False:
            raise ValueError('sprite caller authority unavailable')

    async def submit(self,request,job,admission):
        identity=job['identity']; binding=identity['resource_binding']
        try:
            frames=await self.scope.source(identity)
            await self._caller_current()
            admission()
            child=await asyncio.to_thread(bg_jobs.reserve_media_local_child,job['id'],account_id=job['account_id'],token=job['submission_token'],
                profile={'adapter':self.adapter_id,'input_hashes':identity['input_hashes'],'artifact_authority':artifact_authority(self.artifact_store)})
        except Exception as exc: raise NotDispatched('sprite source unavailable') from exc
        async def owned():
            async with self.barrier.admitted(self.owner_for_account(job['account_id'])):
                claimed=await asyncio.to_thread(bg_jobs.claim_media_local_child,job['id'],account_id=job['account_id'],child_id=child['child_id'],nonce=child['nonce'])
                try:
                    def compose(): return pack_sheet(frames[0],binding['rectangles'],binding['annotations']) if self.adapter_id=='sprite-sheet-crops-v1' else pack(frames,binding['annotations'])
                    work=asyncio.create_task(asyncio.to_thread(compose))
                    while True:
                        try: data,side=await asyncio.shield(work); break
                        except asyncio.CancelledError:
                            if work.done(): data,side=work.result(); break
                    if side!=binding['cellSize'] or not await self.scope.current(identity): raise ValueError('sprite source changed')
                    await self._caller_current()
                    admission(); digest=hashlib.sha256(data).hexdigest()
                except BaseException:
                    # The bounded pack has actually settled and custody has not
                    # begun; this exact child can report a known failure.
                    await asyncio.shield(asyncio.to_thread(bg_jobs.finish_media_local_child,job['id'],account_id=job['account_id'],child_id=child['child_id'],nonce=child['nonce'],receipt={'exit_code':1,'output_sha256':None,'output_size':0}))
                    return False
                sources=[{k:item[k] for k in ('resourceID','revision','sha256')} for item in binding['inputs']]
                layout=selected_layout_for(digest,sources[0],binding['sourceWidth'],binding['sourceHeight'],binding['rectangles'],binding['annotations']) if self.adapter_id=='sprite-sheet-crops-v1' else layout_for(digest,sources,side,binding['annotations'])
                def retain():
                    admission()
                    current=bg_jobs.get_media(job['id'],account_id=job['account_id'])
                    if not current or current.get('local_child')!=claimed or artifact_authority(self.artifact_store)!=claimed['profile']['artifact_authority']: raise ValueError('sprite authority changed')
                    owner=media_artifact_owner(job['account_id']); row=self.artifact_store.put(owner=owner,chunks=[data],media_type='image/png')
                    retained=b''.join(self.artifact_store.read_chunks(owner=owner,artifact_id=row.id))
                    if len(retained)!=len(data) or hashlib.sha256(retained).hexdigest()!=digest: raise ValueError('sprite artifact changed')
                    row=self.artifact_store.acknowledge(owner=owner,artifact_id=row.id)
                    if row.content_sha256!=digest or row.size_bytes!=len(data): raise ValueError('sprite artifact changed')
                    return bg_jobs.finish_media_local_child(job['id'],account_id=job['account_id'],child_id=child['child_id'],nonce=child['nonce'],receipt={'exit_code':0,'output_sha256':digest,'output_size':len(data),'artifact_id':row.id,'layout':layout})
                work=asyncio.create_task(asyncio.to_thread(retain))
                while True:
                    try: saved=await asyncio.shield(work); break
                    except asyncio.CancelledError:
                        if work.done(): saved=work.result(); break
                if not saved: raise ValueError('sprite receipt unavailable')
                return True
        task=asyncio.create_task(owned()); cancelled=False
        task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
        while True:
            try: completed=await asyncio.shield(task); break
            except asyncio.CancelledError:
                cancelled=True
                if task.done(): completed=task.result(); break
        if cancelled: raise asyncio.CancelledError
        if not completed: raise NotDispatched('sprite pack unavailable')
        return Submission('running',child['child_id'])


class SpriteSheetCropsAdapter(SpriteSheetAdapter):
    adapter_id='sprite-sheet-crops-v1'
