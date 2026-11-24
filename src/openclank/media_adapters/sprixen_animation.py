"""One reviewed animation of a provider-asserted project sprite."""
import asyncio
import hashlib
import io
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from PIL import Image
from src.openclank.artifacts import media_artifact_owner
from src.openclank.media_adapters.base import Artifact, NotDispatched, Submission, CollectionProfile
from src.openclank.media_adapters.sprixen import SprixenAdapter, MAX_PNG

_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_ASSET = re.compile(r'/api/public/uploads/animations/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.png\Z')
_TYPES = {'idle','walk','run','attack','jump','death','hurt'}


def source_witness(project, sprite):
    if not isinstance(project,dict) or not isinstance(sprite,dict): raise ValueError('sprite source unavailable')
    project_id, sprite_id, image = project.get('id'), sprite.get('id'), sprite.get('imageUrl')
    if (not isinstance(project_id,str) or not _ID.fullmatch(project_id) or not isinstance(sprite_id,str)
            or not _ID.fullmatch(sprite_id) or not isinstance(image,str) or not image or len(image.encode('utf-8'))>4096):
        raise ValueError('sprite source unavailable')
    raw=json.dumps(['sprixen-project-sprite-v1',project_id,sprite_id,image],ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def validate_request(request):
    if not isinstance(request,dict) or set(request)!={'parameters'} or not isinstance(request['parameters'],dict): raise ValueError('animation unavailable')
    params=request['parameters']
    if set(params)!={'type','frameCount','fps'} or params['type'] not in _TYPES or type(params['frameCount']) is not int or not 2<=params['frameCount']<=12 or type(params['fps']) is not int or not 4<=params['fps']<=60:
        raise ValueError('animation parameters unavailable')
    return {**params,'quality':'standard'}


@dataclass(frozen=True)
class AnimationArtifact(Artifact):
    animation: dict


class SprixenAnimationAdapter(SprixenAdapter):
    collection_profile = CollectionProfile(control_read=True, artifact_type=AnimationArtifact)
    adapter_id='sprixen-animation-v1'

    async def submit(self,request,job,admission):
        try:
            body=validate_request(request); binding=job['identity']['resource_binding']
            project_id,sprite_id=binding['projectID'],binding['spriteID']
            if not _ID.fullmatch(project_id) or not _ID.fullmatch(sprite_id): raise ValueError()
            project=await self._json(job,admission,'GET','/v1/projects/'+project_id)
            sprites=project.get('sprites')
            if project.get('id')!=project_id or not isinstance(sprites,list) or len(sprites)>512: raise ValueError()
            matches=[s for s in sprites if isinstance(s,dict) and s.get('id')==sprite_id]
            if len(matches)!=1 or source_witness(project,matches[0])!=binding['sourceSHA256']: raise ValueError()
        except Exception as exc:
            raise NotDispatched('sprite source or request changed') from exc
        result=await self._json(job,admission,'POST','/v1/sprites/'+sprite_id+'/animations',body=body)
        task=result.get('id')
        if not isinstance(task,str) or not re.fullmatch(r'anim_[A-Za-z0-9_-]{1,124}',task) or result.get('status')!='pending' or result.get('type')!=body['type']:
            raise ValueError('animation submission outcome unknown')
        return Submission('queued',task)

    async def _animation(self,job,admission):
        task=job.get('remote_task_id')
        if not isinstance(task,str) or not re.fullmatch(r'anim_[A-Za-z0-9_-]{1,124}',task): raise ValueError('animation unavailable')
        result=await self._json(job,admission,'GET','/v1/animations/'+task)
        if result.get('id')!=task or result.get('type')!=job['identity']['resource_binding']['animationParameters']['type']: raise ValueError('animation changed')
        return result

    async def status(self,job,admission):
        value=await self._animation(job,admission)
        return Submission({'pending':'queued','completed':'collecting','failed':'failed'}.get(value.get('status'),'unknown_outcome'),job.get('remote_task_id'))

    async def collect(self,job,admission):
        value=await self._animation(job,admission); params=job['identity']['resource_binding']['animationParameters']
        count,fps=value.get('frameCount'),value.get('fps')
        if (value.get('status')!='completed' or value.get('type')!=params['type'] or type(count) is not int or count!=params['frameCount'] or type(fps) is not int or fps!=params['fps']): raise ValueError('animation result changed')
        path=value.get('spriteSheetUrl')
        if not isinstance(path,str) or not _ASSET.fullmatch(path) or urlsplit(path).query or '..' in path.split('/'): raise ValueError('animation asset unavailable')
        data=await self._request(job,admission,'GET',path,authenticated=False,limit=MAX_PNG)
        with Image.open(io.BytesIO(data)) as image:
            width,height=image.size
            if image.format!='PNG' or not 1<=width<=4096 or not 1<=height<=4096 or width*height>16777216: raise ValueError('animation PNG unsupported')
            image.verify()
        digest=hashlib.sha256(data).hexdigest(); owner=media_artifact_owner(job['account_id'])
        metadata={'animationID':job['remote_task_id'],'type':params['type'],'frameCount':count,'fps':fps,'width':width,'height':height,'sha256':digest}
        async with self.resolve_profile(job['identity'],job=job) as (_profile,check,_credential):
            claim=job.get('claim') or {}; await check(job['id'],claim.get('token'),claim.get('action')); admission()
            def retain():
                admission(); row=self.artifact_store.put(owner=owner,chunks=[data],media_type='image/png')
                retained=b''.join(self.artifact_store.read_chunks(owner=owner,artifact_id=row.id))
                if len(retained)!=len(data) or hashlib.sha256(retained).hexdigest()!=digest: raise ValueError('artifact changed')
                row=self.artifact_store.acknowledge(owner=owner,artifact_id=row.id)
                if row.content_sha256!=digest or row.size_bytes!=len(data): raise ValueError('artifact changed')
                return AnimationArtifact(row.id,digest,'image/png',len(data),metadata)
            task=asyncio.create_task(asyncio.to_thread(retain))
            while True:
                try: return await asyncio.shield(task)
                except asyncio.CancelledError:
                    if task.done(): return task.result()
