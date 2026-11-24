"""Capture and recheck explicit LaTeX inputs through the existing Files facade."""
import hashlib
from pathlib import Path
from routes.browser_preview_routes import _read_provider_content
from src.openclank.latex_contract import ADAPTER, MAX_INPUTS, MAX_INPUT_BYTES, safe_relative, validate_binding

class LatexFilesScope:
    def __init__(self, *, facade, context_for, session_current, repository):
        self.facade,self.context_for,self.session_current,self.repository=facade,context_for,session_current,repository

    async def _read(self,context,ref,expected):
        before=await self.facade.stat(context,resource_ref=ref)
        if not isinstance(before,dict) or not before.get('id') or before.get('revision') != expected: raise ValueError('LaTeX source changed')
        source=await self.facade.content(context,resource_ref=ref,capability='download',expected_revision=expected)
        data=await _read_provider_content(source,max_bytes=MAX_INPUT_BYTES)
        after=await self.facade.stat(context,resource_ref=ref)
        if after.get('id')!=before['id'] or after.get('revision')!=expected or after.get('policy_generation')!=context.policy_generation:
            raise ValueError('LaTeX source changed')
        return before['id'],data

    async def execute_current(self,identity,context):
        """Resolve app execute on the actual server-owned Host source, never a caller path."""
        try:
            from src.openclank.files_host_provider import _path
            provider,ref=self.facade._provider_for_ref(context,identity['resource_binding']['inputs'][0]['ref'],capability='download')
            if ref.provider!='host' or (ref.workspace_id and ref.workspace_id != context.workspace_id): return False
            source=Path(_path(ref.origin_id)).resolve(strict=True)
            candidates=[]
            for location in self.repository.list_locations():
                if not location.enabled or location.availability != 'available': continue
                root=Path(location.canonical_path).resolve(strict=True)
                if location.kind == 'exact_file':
                    if root != source: continue
                else:
                    try: source.relative_to(root)
                    except ValueError: continue
                candidates.append((len(root.parts),location,root))
            if not candidates: return False
            _depth,location,root=max(candidates,key=lambda item:item[0])
            if ref.location_id and ref.location_id != location.id: return False
            relative='' if location.kind=='exact_file' else source.relative_to(root).as_posix()
            decision=self.repository.resolve(subject_id=context.owner_subject_id,is_admin=context.is_admin,origin='app',location_id=location.id,
                capability='execute',workspace_id=ref.workspace_id or None,relative_resource=relative,observation_only=True)
            return bool(decision.allowed and decision.policy_generation == context.policy_generation and self.repository.generation() == context.policy_generation)
        except Exception: return False

    async def capture(self,identity,*,source,dependencies,destination_ref,destination_name,compiler_sha256):
        if not await self.session_current(identity) or not isinstance(source,dict) or set(source)!={'ref','revision'} or not isinstance(dependencies,list) or len(dependencies)>=MAX_INPUTS:
            raise ValueError('LaTeX scope unavailable')
        context=await self.context_for(identity); specs=[{**source,'relative_name':'main.tex'}]
        for dep in dependencies:
            if not isinstance(dep,dict) or set(dep)!={'ref','revision','relative_name'}: raise ValueError('LaTeX dependency unavailable')
            safe_relative(dep['relative_name']);specs.append(dep)
        inputs=[];total=0
        for item in specs:
            resource_id,data=await self._read(context,item['ref'],item['revision']);total+=len(data)
            if total>MAX_INPUT_BYTES: raise ValueError('LaTeX input limit')
            inputs.append({'ref':item['ref'],'resourceID':resource_id,'revision':item['revision'],'sha256':hashlib.sha256(data).hexdigest(),'relative_name':item['relative_name']})
        destination=await self.facade.capture_media_destination(context,resource_ref=destination_ref)
        binding={'interface':ADAPTER,'ownerAccountID':identity['account_id'],'inputs':inputs,'compiler_sha256':compiler_sha256,
                 'destination_ref':destination['destination_ref'],'destination_revision':destination['destination_revision'],'destination_name':destination_name}
        binding=validate_binding(binding,account=identity['account_id'],hashes=[item['sha256'] for item in inputs])
        if not await self.execute_current({**identity,'resource_binding':binding},context): raise ValueError('LaTeX execute permission unavailable')
        if not await self.session_current(identity): raise ValueError('LaTeX scope changed')
        return binding,destination['destination_label']

    async def source(self,identity):
        if not await self.session_current(identity): raise ValueError('LaTeX scope changed')
        context=await self.context_for(identity);out=[];total=0
        for item in identity['resource_binding']['inputs']:
            resource_id,data=await self._read(context,item['ref'],item['revision']);total+=len(data)
            if total>MAX_INPUT_BYTES or resource_id!=item['resourceID'] or hashlib.sha256(data).hexdigest()!=item['sha256']: raise ValueError('LaTeX source changed')
            out.append((item['relative_name'],data))
        if not await self.session_current(identity): raise ValueError('LaTeX scope changed')
        return out

    async def current(self,identity):
        try:
            await self.source(identity);context=await self.context_for(identity);binding=identity['resource_binding']
            destination=await self.facade.capture_media_destination(context,resource_ref=binding['destination_ref'])
            return (destination['destination_revision']==binding['destination_revision'] and await self.execute_current(identity,context)
                    and await self.session_current(identity))
        except Exception: return False

    async def control(self,identity):
        return (identity.get('resource_binding') or {}).get('ownerAccountID') == identity.get('account_id')
