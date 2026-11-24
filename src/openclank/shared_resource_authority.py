import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

from src.openclank.files_facade import ProviderContext
from src.openclank.files_host_provider import _path

def location_for(store, context, path, *, workspace_id=None, exact_location=None):
    from dataclasses import replace
    candidates=[]
    for location in store.list_locations():
        if not location.enabled or location.availability!='available' or location.kind not in {'exact_file','directory'}: continue
        root=location.canonical_path
        try:inside=path==root if location.kind=='exact_file' else os.path.commonpath((root,path))==root
        except ValueError:inside=False
        if not inside or (exact_location is not None and location.id!=exact_location):continue
        relative='' if path==root else os.path.relpath(path,root)
        scope=store.resolve(subject_id=context.owner_subject_id,is_admin=context.is_admin,origin='app',location_id=location.id,
            workspace_id=workspace_id,relative_resource=relative,capability='read',observation_only=True)
        if scope.allowed and scope.policy_generation==context.policy_generation:candidates.append(location)
    if not candidates:raise ValueError('source unavailable')
    longest=max(len(x.canonical_path) for x in candidates); choices=[x for x in candidates if len(x.canonical_path)==longest]
    if len(choices)!=1:raise ValueError('source unavailable')
    return choices[0]


@dataclass(frozen=True)
class RecipientLiveTarget:
    owner: str
    owner_subject_id: str
    recipient_subject_id: str
    resource: dict
    revision: dict
    role: str
    provider: object
    context: ProviderContext
    _check: object
    publication_id: str

    async def current(self, *, check_revision: bool = True) -> bool:
        """Check authority and, before an effect, the original file identity."""
        return await self._check(check_revision)


@asynccontextmanager
async def recipient_live_target(*, service, publication_id, recipient_account, auth,
                                barrier, store, provider_for_owner, original_current, allow_owner=False):
    """Hold both account admissions for a role-qualified live publication.

    Callers supply trusted app-owned dependencies and a current original recipient
    predicate; neither the publication ID nor this helper is an authority alone.
    """
    candidate = service.typed_live_target(publication_id)
    if not candidate:
        raise ValueError('Shared resource unavailable')
    owner = candidate['owner']
    owner_account = candidate['owner_subject_id']
    recipient = auth.username_for_account_id(recipient_account)
    if (not recipient or auth.account_id(recipient) != recipient_account
            or auth.username_for_account_id(owner_account) != owner
            or auth.account_id(owner) != owner_account):
        raise ValueError('Shared resource unavailable')
    resource = candidate['target']['resource']
    generation = store.generation()
    admin = auth.is_admin(owner)
    context = ProviderContext(owner_subject_id=owner_account, owner_username=owner,
                              policy_generation=generation, is_admin=admin,
                              workspace_id=resource['workspace_id'])
    async with barrier.admitted(recipient):
        async with barrier.admitted(owner):
            provider = provider_for_owner(context)
            if getattr(provider, 'operation_store', None) is not store:
                raise ValueError('Shared resource unavailable')
            path = _path(resource['origin_id'])
            if os.path.realpath(path) != path:
                raise ValueError('Shared resource unavailable')
            def resolve_role():
                if allow_owner is True and recipient_account == owner_account: return 'owner'
                return store.resolve_resource_role(owner_subject_id=owner_account,
                    recipient_subject_id=recipient_account, location_id=resource['location_id'],
                    resource_id=resource['resource_id'])
            role = resolve_role()
            if role not in {'viewer', 'commenter', 'editor', 'owner'}:
                raise ValueError('Shared resource unavailable')
            revision = None

            async def check(check_revision=True):
                try:
                    if (await original_current() is not True or barrier._fenced(owner) or barrier._fenced(recipient)
                            or auth.account_id(owner) != owner_account or auth.username_for_account_id(owner_account) != owner
                            or auth.account_id(recipient) != recipient_account or auth.username_for_account_id(recipient_account) != recipient
                            or auth.is_admin(owner) != admin or store.generation() != generation
                            or service.typed_live_target(publication_id) != candidate
                            or resolve_role() != role
                            or getattr(provider, 'operation_store', None) is not store or os.path.realpath(path) != path):
                        return False
                    location = location_for(store, context, path, workspace_id=resource['workspace_id'],
                                            exact_location=resource['location_id'])
                    if role == 'editor':
                        relative = '' if path == location.canonical_path else os.path.relpath(path, location.canonical_path)
                        scope = store.resolve(subject_id=owner_account, is_admin=admin, origin='app', location_id=location.id,
                            workspace_id=resource['workspace_id'], relative_resource=relative, capability='write', observation_only=True)
                        if not scope.allowed or scope.policy_generation != generation or not provider._path_writable(context, path):
                            return False
                    if check_revision:
                        entry = await provider.stat_inspection(context, origin_id=resource['origin_id'])
                        if (entry.kind != 'file' or entry.origin_id != resource['origin_id'] or entry.revision != revision):
                            return False
                    return (await original_current() is True and not barrier._fenced(owner) and not barrier._fenced(recipient)
                            and auth.account_id(owner) == owner_account and auth.account_id(recipient) == recipient_account
                            and auth.is_admin(owner) == admin and store.generation() == generation
                            and service.typed_live_target(publication_id) == candidate
                            and resolve_role() == role)
                except Exception:
                    return False

            entry = await provider.stat_inspection(context, origin_id=resource['origin_id'])
            revision = entry.revision
            if (entry.kind != 'file' or entry.origin_id != resource['origin_id'] or type(revision) is not dict
                    or revision.get('kind') != 'hostObjectIdentity' or not revision.get('value') or not await check()):
                raise ValueError('Shared resource unavailable')
            yield RecipientLiveTarget(owner, owner_account, recipient_account, dict(resource), dict(revision), role,
                                      provider, context, check, publication_id)
