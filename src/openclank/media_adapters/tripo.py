"""One explicit Tripo v3 geometry-only text-to-model job."""
from functools import partial
from urllib.parse import quote
from src.openclank.media_adapters.base import NotDispatched, Submission
from src.openclank.media_adapters.meshy import MeshyAdapter, fetch_asset

ORIGIN='https://openapi.tripo3d.ai'
ASSET_HOST='cdn.tripo3d.ai'


def task_path(task):
    if (not isinstance(task,str) or not task or len(task)>256 or task in ('.','..')
        or any(not (c.isascii() and (c.isalnum() or c in '._:@-')) for c in task)):
        raise ValueError('Tripo task unavailable')
    return '/v3/tasks/'+quote(task,safe='')


def validate_request(request):
    if not isinstance(request,dict) or set(request)!={'parameters'} or not isinstance(request['parameters'],dict) or set(request['parameters'])!={'prompt'}:
        raise ValueError('invalid Tripo request')
    prompt=request['parameters']['prompt']
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>1024 or len(prompt.encode('utf-8'))>4096:
        raise ValueError('invalid Tripo prompt')
    return {'prompt':prompt,'model':'v3.1-20260211','texture':False,'pbr':False,'quad':False}


class TripoAdapter(MeshyAdapter):
    adapter_id='tripo-text3d-v1'
    origin=ORIGIN

    def __init__(self, *, resolve_profile, artifact_store, client_factory=None, asset_fetch=None, before_dispatch=None):
        kwargs={'resolve_profile':resolve_profile,'artifact_store':artifact_store,
                'asset_fetch':asset_fetch or partial(fetch_asset,expected_host=ASSET_HOST)}
        if client_factory is not None: kwargs['client_factory']=client_factory
        super().__init__(**kwargs)
        self.before_dispatch=before_dispatch

    async def submit(self, request, job, admission):
        try: body=validate_request(request)
        except (ValueError,UnicodeError) as exc: raise NotDispatched('invalid Tripo request') from exc
        value=await self._request(job,admission,'POST','/v3/generation/text-to-model',body=body,before_dispatch=self.before_dispatch)
        data=value.get('data')
        if type(value.get('code')) is not int or value['code']!=0 or not isinstance(data,dict): raise ValueError('Tripo submission unknown')
        task=data.get('task_id');task_path(task)
        return Submission('queued',task)

    async def _raw_task(self, job, admission):
        task=job.get('remote_task_id');value=await self._request(job,admission,'GET',task_path(task));data=value.get('data')
        if type(value.get('code')) is not int or value['code']!=0 or not isinstance(data,dict) or data.get('task_id')!=task or data.get('type')!='text_to_model':
            raise ValueError('Tripo task changed')
        return data

    async def status(self, job, admission):
        value=await self._raw_task(job,admission)
        state={'queued':'queued','running':'running','success':'collecting','failed':'failed','cancelled':'cancelled','banned':'failed','expired':'failed'}.get(value.get('status'),'unknown_outcome')
        return Submission(state,job.get('remote_task_id'))

    async def _task(self, job, admission):
        value=await self._raw_task(job,admission);output=value.get('output')
        if value.get('status')!='success' or not isinstance(output,dict) or not isinstance(output.get('model_url'),str): raise ValueError('Tripo output unavailable')
        return {'status':'SUCCEEDED','model_urls':{'glb':output['model_url']}}
