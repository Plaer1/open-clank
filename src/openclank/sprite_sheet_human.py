"""Named-human review of four captured Files resources."""
import asyncio, base64, hashlib, os, stat, uuid
from src.openclank.media_human import HumanMediaService
from src.openclank.media_jobs import request_digest
from src.openclank.sprite_sheet import annotations

def bind_files_project(*,session_factory,resolve_path,repository_factory,owner,resource_id,sha256,state,width,height,operation_key):
    from src.openclank.files_image_store import FilesImageStore, FilesImageError
    from src.openclank.image_projects import ImageResourceIdentity
    store=FilesImageStore(session_factory=session_factory); rid=resource_id.removeprefix('image:')
    try: row=store.image(owner,rid)
    except FilesImageError as exc: raise ValueError('sheet image unavailable') from exc
    if not row or row.digest!=sha256 or not row.locator or type(row.size) is not int or not 0<row.size<=32*1024*1024: raise ValueError('sheet image authority unavailable')
    if not hasattr(os,'O_NOFOLLOW') or not hasattr(os,'O_NONBLOCK'): raise ValueError('safe image read unavailable')
    try:
        path=resolve_path(row.locator); before=os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size!=row.size: raise ValueError('sheet image unavailable')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            opened=os.fstat(fd)
            key=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)
            if not stat.S_ISREG(opened.st_mode) or key(before)!=key(opened): raise ValueError('sheet image changed')
            chunks=[]; remaining=row.size
            while remaining:
                chunk=os.read(fd,min(65536,remaining))
                if not chunk: raise ValueError('sheet image changed')
                chunks.append(chunk); remaining-=len(chunk)
            data=b''.join(chunks)
            if key(os.fstat(fd))!=key(opened) or key(os.lstat(path))!=key(opened): raise ValueError('sheet image changed')
        finally: os.close(fd)
        current=store.image(owner,rid)
        if not current or len(data)!=row.size or hashlib.sha256(data).hexdigest()!=sha256 or current.revision!=row.revision or current.digest!=row.digest: raise ValueError('sheet image changed')
    except (OSError,AttributeError,TypeError,FilesImageError) as exc:
        raise ValueError('sheet image unavailable') from exc
    repository=repository_factory(); original_current=repository.current
    def source_current():
        try:
            latest=store.image(owner,rid)
            return (original_current() is True and latest is not None and latest.revision==row.revision
                and latest.digest==row.digest and latest.locator==row.locator and latest.size==row.size
                and key(os.lstat(path))==key(opened) and original_current() is True)
        except Exception:return False
    repository.current=source_current
    return repository.save_copy(owner=owner,project_id=None,image_identity=ImageResourceIdentity('files',resource_id), operation_id=operation_key,
        expected_image_revision='sha256:'+sha256,state=state,name='Sprite sheet',width=width,height=height,operation_key=operation_key)

class SpriteSheetHumanService(HumanMediaService):
    def __init__(self, *, project_bind=None, **kwargs):
        super().__init__(**kwargs); self.project_bind=project_bind

    async def prepare_scoped(self, *, identity, source_refs, destination_ref, destination_name, parameters, rectangles=None, once=False):
        params=annotations(parameters)
        operation=identity.get('operation')
        if operation=='image.pack' and rectangles is None:
            adapter='sprite-sheet-v1'; request={'parameters':params}
        elif operation=='image.pack-crops' and rectangles is not None:
            adapter='sprite-sheet-crops-v1'; request={'parameters':{'annotations':params,'rectangles':rectangles}}
        else: raise ValueError('sprite operation unavailable')
        identity={**identity,'request_digest':request_digest(request),'adapter_id':adapter,'adapter_version':'1',
            'route_id':adapter,'route_kind':'local','model_revision':'fixed-v1','input_hashes':[],'resource_binding':{}}
        async def owned_capture():
            async with self.barrier.admitted(identity['owner']):
                return await self.scope.capture(identity,source_refs=source_refs,destination_ref=destination_ref,
                    destination_name=destination_name,parameters=params,rectangles=rectangles)
        binding,folder=await self._bounded_read(owned_capture())
        identity['resource_binding']=binding;identity['input_hashes']=[v['sha256'] for v in binding['inputs']]
        if await self._bounded_read(self.scope.current(identity)) is not True: raise ValueError('sprite source changed')
        if once: row,created=await self.coordinator.prepare_once(identity,request)
        else: row=await self.coordinator.prepare(identity,request);created=True
        sources=[{k:v[k] for k in ('resourceID','revision','sha256')} for v in binding['inputs']]
        review={'destination':{'name':destination_name,'folder':folder},'parameters':request['parameters'],'cellSize':binding['cellSize']}
        if operation=='image.pack':
            review.update(sources=sources,operation='Four equal square PNG frames in reviewed 2×2 order; no resampling')
        else:
            review.update(source=sources[0],sourceWidth=binding['sourceWidth'],sourceHeight=binding['sourceHeight'],
                rectangles=binding['rectangles'],operation='Four explicitly selected square regions in reviewed 2×2 order; no resampling')
        return identity,request,row,created,review

    async def prepare_sheet(self,*,account,owner,workspace,generation,session_id,source_refs,destination_ref,destination_name,parameters):
        async with self.barrier.admitted(owner):
            turn=uuid.uuid4().hex
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,'actor_id':'human',
                'turn_id':turn,'root_operation_id':turn,'attempt_id':turn,'policy_generation':generation,'operation':'image.pack'}
            _,_,row,_,review=await self.prepare_scoped(identity=identity,source_refs=source_refs,destination_ref=destination_ref,
                destination_name=destination_name,parameters=parameters)
            return {'jobID':row['id'],'revision':row['revision'],'state':row['status'],**review}

    async def prepare_crops(self,*,account,owner,workspace,generation,session_id,source_ref,destination_ref,destination_name,parameters,rectangles):
        async with self.barrier.admitted(owner):
            turn=uuid.uuid4().hex
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,'actor_id':'human',
                'turn_id':turn,'root_operation_id':turn,'attempt_id':turn,'policy_generation':generation,'operation':'image.pack-crops'}
            _,_,row,_,review=await self.prepare_scoped(identity=identity,source_refs=[source_ref],destination_ref=destination_ref,
                destination_name=destination_name,parameters=parameters,rectangles=rectangles)
            return {'jobID':row['id'],'revision':row['revision'],'state':row['status'],**review}

    async def bind_sheet_project(self, *, account, owner, job_id):
        if self.project_bind is None: raise ValueError('image project binding unavailable')
        async with self.barrier.admitted(owner):
            job=self._job(account,owner,job_id); identity=job['identity']; result=job.get('result') or {}; artifact=job.get('artifact') or {}
            if identity.get('adapter_id') not in {'sprite-sheet-v1','sprite-sheet-crops-v1'} or job.get('status')!='complete' or job.get('publication_withdrawn') or not await self.scope.session_current(identity): raise ValueError('sheet project unavailable')
            context=await self.recovery_context_for(identity)
            opened=await self.scope.facade.open_payload(context,resource_ref=result['resource_ref'])
            if opened.get('target',{}).get('app')!='imps' or opened.get('resource',{}).get('provider')!='files' or opened.get('payload',{}).get('provider')!='files': raise ValueError('sheet project provider unavailable')
            resource_id=opened['payload'].get('resource_id')
            if not isinstance(resource_id,str) or not resource_id: raise ValueError('sheet project resource unavailable')
            data=await asyncio.to_thread(self._artifact_bytes,account,artifact,identity)
            if hashlib.sha256(data).hexdigest()!=result['sha256'] or artifact.get('layout')!=result.get('layout') or not await self.scope.session_current(identity): raise ValueError('sheet receipt changed')
            layout=artifact['layout']; width=layout['sheetWidth']; height=layout['sheetHeight']
            state={'v':2,'imgWidth':width,'imgHeight':height,'activeLayerId':'layer1','nextLayerId':2,
                'spriteSheet':layout,'layers':[{'id':'layer1','type':'raster','visible':True,'opacity':1,'locked':False,
                    'canvasW':width,'canvasH':height,'dataUrl':'data:image/png;base64,'+base64.b64encode(data).decode('ascii'),'masks':[]}]}
            current_context=await self.recovery_context_for(identity)
            if (self.coordinator._account(identity)!=owner or current_context.owner_subject_id!=account
                    or current_context.workspace_id!=context.workspace_id or current_context.policy_generation!=context.policy_generation
                    or not await self.scope.session_current(identity)):
                raise ValueError('sheet project authority changed')
            def bind():
                current=self._job(account,owner,job_id)
                if current.get('result')!=result or current.get('artifact')!=artifact or current.get('publication_withdrawn'): raise ValueError('sheet receipt changed')
                return self.project_bind(owner=owner,resource_id=resource_id,sha256=result['sha256'],state=state,width=width,height=height,operation_key='sprite-sheet:'+job_id)
            task=asyncio.create_task(asyncio.to_thread(bind))
            while True:
                try: receipt=await asyncio.shield(task); break
                except asyncio.CancelledError:
                    if task.done(): receipt=task.result(); break
            return {'state':'bound','projectID':receipt.project_id,'projectRevision':receipt.project_revision,'sheetResourceRef':result['resource_ref'],'operation_id':receipt.action_id,'lore_receipt':receipt.lore_receipt}
