"""One shared-text operation owner for named-human and managed callers.

Dependencies and original_current come from existing trusted Host owners. A
publication or operation ID alone never supplies recipient authority.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping

from src.openclank.collaboration_operations import public_selector_digest, reserve, review, status_selector
from src.openclank.shared_resource_authority import recipient_live_target


class SharedTextService:
    def __init__(self, *, service, auth, barrier, store, provider_for_owner, original_current, verify_original_source=None):
        if not callable(provider_for_owner) or not callable(original_current) or (verify_original_source is not None and not callable(verify_original_source)):
            raise ValueError('Shared text authority unavailable')
        self.service, self.auth, self.barrier, self.store = service, auth, barrier, store
        self.provider_for_owner, self.original_current = provider_for_owner, original_current
        self.verify_original_source = verify_original_source

    def _target(self, recipient_account, publication_id):
        return recipient_live_target(service=self.service, publication_id=publication_id,
            recipient_account=recipient_account, auth=self.auth, barrier=self.barrier, store=self.store,
            provider_for_owner=self.provider_for_owner, original_current=self.original_current)

    async def _verify_source(self):
        if self.verify_original_source is not None and await self.verify_original_source() is not True: return False
        return await self.original_current() is True

    def _validate_profile(self, text):
        return None

    def _selector(self, publication_id, operation_id, base_sha256, edits):
        return public_selector_digest(publication_id=publication_id, operation_id=operation_id,
                                      base_sha256=base_sha256, edits=edits)

    def _intent_edit(self, edits):
        return {'edits':edits}

    async def _commit_edit(self, target, reservation, base, base_sha256, edits, **kwargs):
        return await target.provider.collaboration_commit(target, reservation, base=base,
                                                           base_sha256=base_sha256, edits=edits, **kwargs)

    def _status_options(self):
        return {}

    async def read(self, *, recipient_account, publication_id):
        async with self._target(recipient_account, publication_id) as target:
            result = await target.provider.collaboration_read(target)
            self._validate_profile(result['text'])
            if not await self._verify_source() or not await target.current(): raise ValueError('Shared resource changed')
            return result

    async def _current_output(self, target, result):
        if result.get('state') != 'complete' or not await target.current(check_revision=False):
            return result
        try:
            value = await target.provider.collaboration_read(target)
            self._validate_profile(value['text'])
            if not await target.current(check_revision=False): raise ValueError('Shared authority changed')
            if value['revision'] == result.get('revision'): result['text'] = value['text']
            else: result['current'] = {'revision':value['revision'], 'text':value['text']}
        except Exception:
            if not await target.current(check_revision=False): raise ValueError('Shared authority changed')
        return result

    async def commit(self, *, recipient_account, publication_id, operation_id, base_text, base_sha256,
                     edits, client_digest=None, initiator=None):
        if not isinstance(base_text, str): raise ValueError('Invalid shared text')
        base = base_text.encode('utf-8', 'strict')
        if len(base) > 262144: raise ValueError('Shared text exceeds bounds')
        digest = self._selector(publication_id, operation_id, base_sha256, edits)
        if client_digest is not None and client_digest != digest: raise ValueError('Shared selector changed')

        async def run():
            async with self._target(recipient_account, publication_id) as target:
                if target.role != 'editor': raise ValueError('Shared editing unavailable')
                intent = {'owner_account':target.owner_subject_id, 'recipient_account':recipient_account,
                    'publication_id':publication_id, 'resource_id':target.resource['resource_id'],
                    'operation_id':operation_id, 'base_sha256':base_sha256, **self._intent_edit(edits)}
                reservation, receipt = reserve(self.store, intent=intent, generation=target.context.policy_generation,
                                               client_digest=digest, initiator=initiator)
                if reservation is not None:
                    kwargs = {'before_issue':self._verify_source} if self.verify_original_source is not None else {}
                    receipt = await self._commit_edit(target, reservation, base, base_sha256, edits, **kwargs)
                if not await target.current(check_revision=False): raise ValueError('Shared authority changed')
                result = review(receipt, operation_id, digest)
                if result['state'] == 'complete':
                    try:
                        async with self._target(recipient_account, publication_id) as fresh:
                            result = await self._current_output(fresh, result)
                    except Exception:
                        if not await target.current(check_revision=False): raise ValueError('Shared authority changed')
                if not await self._verify_source() or not await target.current(check_revision=False): raise ValueError('Shared authority changed')
                return result

        task = asyncio.create_task(run())
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=13.0)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            if isinstance(exc, asyncio.CancelledError): raise
            if self.verify_original_source is None:
                if await self.original_current() is not True: raise ValueError('Shared authority changed')
            else:
                async with self._target(recipient_account, publication_id) as target:
                    if not await self._verify_source() or not await target.current(check_revision=False): raise ValueError('Shared authority changed')
            return {'version':1, 'state':'unknown', 'operation_id':operation_id, 'client_digest':digest}

    async def status(self, *, recipient_account, publication_id, operation_id, client_digest):
        async with self._target(recipient_account, publication_id) as target:
            receipt = status_selector(self.store, recipient_account=recipient_account, owner_account=target.owner_subject_id,
                publication_id=publication_id, resource_id=target.resource['resource_id'], operation_id=operation_id,
                client_digest=client_digest, **self._status_options())
            if receipt and receipt.get('state') == 'pending' and receipt.get('retired') is True:
                receipt = await target.provider.collaboration_status(target, receipt)
            if not await target.current(check_revision=False): raise ValueError('Shared authority changed')
            result = review(receipt, operation_id, client_digest)
            result = await self._current_output(target, result)
            if not await self._verify_source() or not await target.current(check_revision=False): raise ValueError('Shared authority changed')
            return result
