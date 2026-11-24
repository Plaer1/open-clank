"""Private exact source witness and reviewed single animation."""
import asyncio
import json
import uuid
import httpx
from src.openclank.media_adapters.sprixen_animation import _ID, source_witness, validate_request
from src.openclank.media_human import HumanMediaService
from src.openclank.media_jobs import request_digest
from src.openclank.sprixen_provider import _current, ORIGIN

class SprixenProjectReader:
    def __init__(self, *, store, auth, barrier, owner, account, profile_selection, client_factory=httpx.AsyncClient):
        self.store,self.auth,self.barrier,self.owner,self.account=store,auth,barrier,owner,account
        self.profile_selection,self.client_factory=profile_selection,client_factory

    async def get(self,connection_id,provider_account_id,project_id):
        if not isinstance(project_id,str) or not _ID.fullmatch(project_id): raise ValueError('project unavailable')
        async with self.barrier.admitted(self.owner):
            binding=await self.profile_selection(self.owner,self.account,connection_id,provider_account_id)
            async def recheck():
                actual=await self.profile_selection(self.owner,self.account,connection_id,provider_account_id)
                if actual!=binding: raise ValueError('provider changed')
            access=await asyncio.to_thread(self.store.sprixen_job_credential,owner=self.owner,connection_id=connection_id,
                connection_revision=binding['connectionRevision'],account_id=provider_account_id,
                account_revision=binding['providerAccountRevision'],credential_version=binding['credentialVersion'])
            await recheck()
            path='/v1/projects/'+project_id
            async with self.client_factory(base_url=ORIGIN,follow_redirects=False,timeout=20,trust_env=False,cookies=None) as client:
                async with client.stream('GET',path,headers={'Authorization':'Bearer '+access.credentials['key']}) as response:
                    if response.status_code!=200: raise ValueError('projects unavailable')
                    chunks=[]; total=0
                    async for chunk in response.aiter_bytes():
                        total+=len(chunk)
                        if total>256*1024: raise ValueError('project response exceeds bounds')
                        chunks.append(chunk)
            await recheck()
        def unique(pairs):
            output={}
            for key,value in pairs:
                if key in output: raise ValueError('duplicate provider key')
                output[key]=value
            return output
        result=json.loads(b''.join(chunks),object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
        if not isinstance(result,dict) or result.get('id')!=project_id or not isinstance(result.get('sprites'),list): raise ValueError('project unavailable')
        sprites=result['sprites']; values=sprites[:128]; seen=set(); output=[]
        for sprite in values:
            digest=source_witness(result,sprite); sprite_id=sprite['id']
            if sprite_id in seen: raise ValueError('ambiguous sprite')
            seen.add(sprite_id); output.append({'id':sprite_id,'sourceSHA256':digest})
        return {'projectID':project_id,'sprites':output,'omitted':len(sprites)>len(values)}

class SprixenAnimationHumanService(HumanMediaService):
    def __init__(self, *, project_reader, **kwargs):
        super().__init__(**kwargs); self.project_reader=project_reader

    async def prepare_animation(self, *, account, owner, workspace, generation, session_id, connection_id, provider_account_id, project_id, sprite_id, source_sha256, destination_ref, destination_name, parameters):
        request={'parameters':parameters}; validate_request(request)
        async with self.barrier.admitted(owner):
            profile=await self._bounded_read(self.profile_selection(owner,account,connection_id,provider_account_id))
            project=await self._bounded_read(self.project_reader.get(connection_id,provider_account_id,project_id))
            found=[v for v in project['sprites'] if v['id']==sprite_id and v['sourceSHA256']==source_sha256]
            if len(found)!=1: raise ValueError('sprite source changed')
            operation=uuid.uuid4().hex
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,'actor_id':'human',
                'turn_id':operation,'root_operation_id':operation,'attempt_id':operation,'request_digest':request_digest(request),
                'adapter_id':'sprixen-animation-v1','adapter_version':'1','operation':'animation.generate','route_id':connection_id,
                'route_kind':'remote','model_revision':'standard','policy_generation':generation,'input_hashes':[source_sha256],
                'resource_binding':profile}
            binding,folder=await self._bounded_read(self.scope.capture(identity,destination_ref=destination_ref,destination_name=destination_name))
            binding.update(projectID=project_id,spriteID=sprite_id,sourceSHA256=source_sha256,animationParameters=dict(parameters))
            identity['resource_binding']=binding; job=await self.coordinator.prepare(identity,request)
            return {'jobID':job['id'],'revision':job['revision'],'state':job['status'],
                'profile':{k:binding[k] for k in ('connectionID','connectionRevision','providerAccountID','providerAccountRevision','credentialVersion','interface')},
                'source':{'projectID':project_id,'spriteID':sprite_id,'sourceSHA256':source_sha256},
                'destination':{'name':destination_name,'folder':folder},'parameters':parameters,
                'notice':'One standard animation is documented as four credits; billing is provider-determined. Cancellation is unavailable.'}
