"""Exact self-owned Meshy job profile and ephemeral credential admission."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from src import bg_jobs

INTERFACE='meshy-preview-v1'
ORIGIN='https://api.meshy.ai'


@dataclass(frozen=True)
class MeshyProfile:
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
        raise ValueError('Meshy account unavailable')
    connection=store.get_connection(owner=owner,connection_id=connection_id)
    account=store.get_account(owner=owner,account_id=provider_account_id)
    if (connection.owner!=owner or connection.family_id!='meshy' or connection.adapter_id!=INTERFACE
        or connection.kind!='official' or connection.billing_lane!='metered_api' or connection.normalized_url!=ORIGIN
        or connection.settings!={'interface':INTERFACE} or not connection.enabled or connection.deleted_at is not None
        or connection.revision!=connection_revision or account.owner!=owner or account.connection_id!=connection_id
        or not account.enabled or account.deleted_at is not None or account.revision!=provider_account_revision
        or account.credential_version!=credential_version or account.auth_method!='api_key'
        or account.auth_class!='metered' or account.acquisition_method!='manual-key'):
        raise ValueError('Meshy profile changed')
    if auth.account_id(owner)!=host_account or auth.username_for_account_id(host_account)!=owner:
        raise ValueError('Meshy account changed')
    return MeshyProfile(host_account,connection_id,connection_revision,INTERFACE,
                        provider_account_id,provider_account_revision,credential_version)


@asynccontextmanager
async def admitted_meshy_profile(*, store, auth_manager, account_barrier, owner, identity, job=None):
    binding=identity['resource_binding']
    if binding.get('interface')!=INTERFACE: raise ValueError('Meshy profile unavailable')
    claim=(job or {}).get('claim') or {}
    def exact():
        if job is None: return None
        current=bg_jobs.get_media(job['id'],account_id=identity['account_id'])
        active=current.get('claim') if current else None
        if (not current or current.get('identity')!=identity or not isinstance(active,dict)
            or active.get('token')!=claim.get('token') or active.get('action')!=claim.get('action')):
            raise ValueError('Meshy job claim changed')
        return current
    async with account_barrier.admitted(owner):
        current=await asyncio.to_thread(exact)
        read_mode=bool(current and claim.get('action') in {'status','collect'} and current.get('remote_task_id')
                       and current.get('remote_task_id')==job.get('remote_task_id'))
        if current and claim.get('action')!='submit' and not read_mode:
            raise ValueError('Meshy read claim unavailable')
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
            if await asyncio.to_thread(_current,**args)!=profile: raise ValueError('Meshy profile changed')
            if job_id is not None:
                if not job or job_id!=job['id'] or token!=claim.get('token') or action!=claim.get('action'):
                    raise ValueError('Meshy job claim changed')
                latest=await asyncio.to_thread(exact)
                if read_mode and latest.get('remote_task_id')!=job.get('remote_task_id'):
                    raise ValueError('Meshy task changed')
        async def credential(job_id,token,action):
            await assert_current(job_id,token,action)
            access=await asyncio.to_thread(store.meshy_job_credential,owner=owner,
                connection_id=profile.connectionID,connection_revision=profile.connectionRevision,
                account_id=profile.providerAccountID,account_revision=profile.providerAccountRevision,
                credential_version=profile.credentialVersion)
            await assert_current(job_id,token,action)
            return access.credentials['key']
        yield profile,assert_current,credential


async def read_original_preview(*, store, auth_manager, account_barrier, owner, identity, source_current):
    """Fresh known-ID preview read under original profile; never revives old claim."""
    import httpx
    import json
    from src.openclank.media_adapters.meshy import task_path, MAX_JSON
    binding=identity['resource_binding']
    args=dict(store=store,auth=auth_manager,owner=owner,host_account=identity['account_id'],
        connection_id=binding['connectionID'],connection_revision=binding['connectionRevision'],
        provider_account_id=binding['providerAccountID'],provider_account_revision=binding['providerAccountRevision'],
        credential_version=binding['credentialVersion'])
    async with account_barrier.admitted(owner):
        await source_current(identity)
        profile=await asyncio.to_thread(_current,**args)
        access=await asyncio.to_thread(store.meshy_job_credential,owner=owner,connection_id=profile.connectionID,
            connection_revision=profile.connectionRevision,account_id=profile.providerAccountID,
            account_revision=profile.providerAccountRevision,credential_version=profile.credentialVersion)
        if await asyncio.to_thread(_current,**args)!=profile: raise ValueError('Meshy profile changed')
        await source_current(identity)
        async with httpx.AsyncClient(base_url=ORIGIN,follow_redirects=False,timeout=20.0,trust_env=False,cookies=None) as client:
            async with client.stream('GET',task_path(binding['sourceTaskID']),headers={'Authorization':'Bearer '+access.credentials['key']}) as response:
                if response.status_code!=200: raise ValueError('Meshy preview unavailable')
                chunks=[]; total=0
                async for chunk in response.aiter_bytes():
                    total+=len(chunk)
                    if total>MAX_JSON: raise ValueError('Meshy response exceeds bounds')
                    chunks.append(chunk)
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result: raise ValueError('duplicate Meshy key')
                result[key]=value
            return result
        value=json.loads(b''.join(chunks),object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
        if (not isinstance(value,dict) or value.get('id')!=binding['sourceTaskID'] or value.get('type')!='text-to-3d-preview' or value.get('status')!='SUCCEEDED'):
            raise ValueError('Meshy preview unavailable')
        if await asyncio.to_thread(_current,**args)!=profile: raise ValueError('Meshy profile changed')
        await source_current(identity)
