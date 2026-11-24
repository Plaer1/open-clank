"""Exact self-owned Fal job profile and ephemeral credential admission."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from src import bg_jobs

INTERFACE='fal-flux-schnell-v1'
ORIGIN='https://queue.fal.run'


@dataclass(frozen=True)
class FalProfile:
    ownerAccountID: str
    connectionID: str
    connectionRevision: int
    interface: str
    providerAccountID: str
    providerAccountRevision: int
    credentialVersion: int

    def job_binding(self):
        return dict(vars(self))


def _current(*, store, auth, owner, host_account, connection_id, connection_revision,
             provider_account_id, provider_account_revision, credential_version):
    if not owner or auth.account_id(owner)!=host_account or auth.username_for_account_id(host_account)!=owner:
        raise ValueError('Fal account unavailable')
    connection=store.get_connection(owner=owner,connection_id=connection_id)
    account=store.get_account(owner=owner,account_id=provider_account_id)
    if (connection.owner!=owner or connection.family_id!='fal' or connection.adapter_id!=INTERFACE
        or connection.kind!='official' or connection.billing_lane!='metered_api' or connection.normalized_url!=ORIGIN
        or connection.settings!={'interface':INTERFACE} or not connection.enabled or connection.deleted_at is not None
        or connection.revision!=connection_revision or account.owner!=owner or account.connection_id!=connection_id
        or not account.enabled or account.deleted_at is not None or account.revision!=provider_account_revision
        or account.credential_version!=credential_version or account.auth_method!='api_key'
        or account.auth_class!='metered' or account.acquisition_method!='manual-key'):
        raise ValueError('Fal profile changed')
    if auth.account_id(owner)!=host_account or auth.username_for_account_id(host_account)!=owner:
        raise ValueError('Fal account changed')
    return FalProfile(host_account,connection_id,connection_revision,INTERFACE,
                        provider_account_id,provider_account_revision,credential_version)


@asynccontextmanager
async def admitted_fal_profile(*, store, auth_manager, account_barrier, owner, identity, job=None):
    binding=identity['resource_binding']
    if binding.get('interface')!=INTERFACE: raise ValueError('Fal profile unavailable')
    claim=(job or {}).get('claim') or {}
    def exact():
        if job is None: return None
        current=bg_jobs.get_media(job['id'],account_id=identity['account_id'])
        active=current.get('claim') if current else None
        if (not current or current.get('identity')!=identity or not isinstance(active,dict)
            or active.get('token')!=claim.get('token') or active.get('action')!=claim.get('action')):
            raise ValueError('Fal job claim changed')
        return current
    async with account_barrier.admitted(owner):
        current=await asyncio.to_thread(exact)
        read_mode=bool(current and claim.get('action') in {'status','collect'} and current.get('remote_task_id')
                       and current.get('remote_task_id')==job.get('remote_task_id'))
        if current and claim.get('action')!='submit' and not read_mode:
            raise ValueError('Fal read claim unavailable')
        if read_mode:
            connection=await asyncio.to_thread(store.get_connection,owner=owner,connection_id=binding['connectionID'])
            account=await asyncio.to_thread(store.get_account,owner=owner,account_id=binding['providerAccountID'])
            connection_revision,account_revision,credential_version=connection.revision,account.revision,account.credential_version
        else:
            connection_revision,account_revision,credential_version=binding['connectionRevision'],binding['providerAccountRevision'],binding['credentialVersion']
        args=dict(store=store,auth=auth_manager,owner=owner,host_account=identity['account_id'],
            connection_id=binding['connectionID'],connection_revision=connection_revision,
            provider_account_id=binding['providerAccountID'],provider_account_revision=account_revision,credential_version=credential_version)
        profile=await asyncio.to_thread(_current,**args)
        async def assert_current(job_id=None,token=None,action=None):
            if await asyncio.to_thread(_current,**args)!=profile: raise ValueError('Fal profile changed')
            if job_id is not None:
                if not job or job_id!=job['id'] or token!=claim.get('token') or action!=claim.get('action'):
                    raise ValueError('Fal job claim changed')
                latest=await asyncio.to_thread(exact)
                if read_mode and latest.get('remote_task_id')!=job.get('remote_task_id'):
                    raise ValueError('Fal task changed')
        async def credential(job_id,token,action):
            await assert_current(job_id,token,action)
            access=await asyncio.to_thread(store.fal_job_credential,owner=owner,
                connection_id=profile.connectionID,connection_revision=profile.connectionRevision,
                account_id=profile.providerAccountID,account_revision=profile.providerAccountRevision,
                credential_version=profile.credentialVersion)
            await assert_current(job_id,token,action)
            return access.credentials['key']
        yield profile,assert_current,credential


@asynccontextmanager
async def admitted_fal_image_mesh_profile(*, store, auth_manager, account_barrier, owner, identity, job):
    """Lease the existing Fal key only for one exact Hunyuan job claim."""
    import re
    if (type(identity) is not dict or type(job) is not dict or job.get('identity') != identity
            or identity.get('adapter_id') != 'fal-hunyuan-image3d-v1'
            or identity.get('adapter_version') != '1' or identity.get('operation') != 'mesh.generate'
            or identity.get('model_revision') != 'fal-ai/hunyuan3d/v2' or identity.get('route_kind') != 'remote'
            or identity.get('owner') != owner):
        raise ValueError('Fal image mesh purpose unavailable')
    binding = identity.get('resource_binding')
    source = binding.get('image_source') if type(binding) is dict else None
    claim = job.get('claim')
    if (type(source) is not dict or set(source) != {'ref','resourceID','revision','sha256','mime_type','width','height','name'}
            or type(source.get('sha256')) is not str or re.fullmatch(r'[0-9a-f]{64}', source['sha256']) is None
            or identity.get('input_hashes') != [source['sha256']]
            or type(identity.get('request_digest')) is not str or re.fullmatch(r'[0-9a-f]{64}', identity['request_digest']) is None
            or identity.get('route_id') != binding.get('connectionID')
            or type(claim) is not dict or claim.get('action') not in {'submit','status','collect'}
            or type(claim.get('token')) is not str or not claim['token']):
        raise ValueError('Fal image mesh claim unavailable')
    # The original exact Fal transport and claim owner handles revision checks,
    # and permits current legitimate credentials only for retained known-ID reads.
    async with admitted_fal_profile(store=store, auth_manager=auth_manager, account_barrier=account_barrier,
                                    owner=owner, identity=identity, job=job) as admitted:
        yield admitted
