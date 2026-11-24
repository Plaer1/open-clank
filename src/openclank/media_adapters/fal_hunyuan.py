"""One fixed fal Hunyuan image-to-untextured-GLB job."""
import base64
import hashlib

from src.openclank.media_adapters.base import NotDispatched, Submission
from src.openclank.media_adapters.fal import FalAdapter, _ID
from src.openclank.media_adapters.meshy import MeshyAdapter


def validate_request(request):
    if type(request) is not dict or set(request) != {'parameters'} or type(request['parameters']) is not dict:
        raise ValueError('invalid image-to-mesh request')
    params = request['parameters']
    if set(params) not in (set(), {'seed'}):
        raise ValueError('invalid image-to-mesh parameters')
    if 'seed' in params and (type(params['seed']) is not int or not 0 <= params['seed'] <= 2147483647):
        raise ValueError('invalid image-to-mesh seed')
    return dict(params)


class FalHunyuanAdapter(FalAdapter):
    adapter_id = 'fal-hunyuan-image3d-v1'

    def __init__(self, *, resolve_profile, artifact_store, source, scope_current, client_factory=None, asset_fetch=None, before_dispatch=None):
        super().__init__(resolve_profile=resolve_profile, artifact_store=artifact_store,
                         client_factory=client_factory, asset_fetch=asset_fetch, before_dispatch=None)
        if not callable(source) or not callable(scope_current) or not callable(before_dispatch):
            raise ValueError('image source authority unavailable')
        self.source = source
        self.scope_current = scope_current
        self.submit_guard = before_dispatch

    def _path(self, job):
        task = job.get('remote_task_id')
        if not isinstance(task, str) or not _ID.fullmatch(task):
            raise ValueError('fal task unavailable')
        return '/fal-ai/hunyuan3d/requests/' + task

    async def submit(self, request, job, admission):
        try:
            params = validate_request(request)
            identity = job['identity']
            expected = identity['resource_binding']['image_source']
            evidence, data = await self.source(identity)
            if (type(expected) is not dict or set(expected) != {'ref','resourceID','revision','sha256','mime_type','width','height','name'}
                    or evidence != expected or type(data) is not bytes or not 0 < len(data) <= 4*1024*1024
                    or expected['mime_type'] not in ('image/png','image/jpeg')
                    or hashlib.sha256(data).hexdigest() != expected['sha256']
                    or identity.get('input_hashes') != [expected['sha256']]):
                raise ValueError('image source changed')
            admission()
            body = {'input_image_url':'data:' + expected['mime_type'] + ';base64,' + base64.b64encode(data).decode('ascii'),
                    'textured_mesh':False, **params}
        except Exception as exc:
            raise NotDispatched('image-to-mesh source unavailable') from exc
        async def issue_current():
            await self.submit_guard()
            if await self.scope_current(identity) is not True:
                raise ValueError('media source changed')
            admission()
        value = await self._request(job, admission, 'POST', '/fal-ai/hunyuan3d/v2', body=body, before_dispatch=issue_current)
        task = value.get('request_id')
        if not isinstance(task, str) or not _ID.fullmatch(task):
            raise ValueError('fal submission unknown')
        return Submission('queued', task)

    async def _result(self, job, admission):
        result = await self._request(job, admission, 'GET', self._path(job))
        if ('request_id' in result and result['request_id'] != job.get('remote_task_id')):
            raise ValueError('fal task changed')
        if result.get('error') or result.get('error_type'):
            raise ValueError('fal output unavailable')
        # Provider-reported seed is not consumed or retained by this profile.
        mesh = result.get('model_mesh')
        if not isinstance(mesh, dict) or not isinstance(mesh.get('url'), str):
            raise ValueError('fal output unavailable')
        return mesh['url']

    async def status(self, job, admission):
        value = await self._request(job, admission, 'GET', self._path(job) + '/status')
        if value.get('request_id') != job.get('remote_task_id'):
            return Submission('unknown_outcome', job.get('remote_task_id'))
        status = value.get('status')
        if status == 'COMPLETED' and (value.get('error') or value.get('error_type')):
            return Submission('failed', job['remote_task_id'])
        if status == 'COMPLETED':
            try:
                await self._result(job, admission)
            except (ValueError, UnicodeError):
                return Submission('unknown_outcome', job['remote_task_id'])
            return Submission('collecting', job['remote_task_id'])
        return Submission({'IN_QUEUE':'queued','IN_PROGRESS':'running'}.get(status,'unknown_outcome'), job['remote_task_id'])

    async def _task(self, job, admission):
        return {'status':'SUCCEEDED','model_urls':{'glb':await self._result(job, admission)}}

    async def collect(self, job, admission):
        return await MeshyAdapter.collect(self, job, admission)
