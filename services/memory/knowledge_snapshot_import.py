"""Recipient-side review and confirmation of one portable knowledge snapshot."""
from services.memory.knowledge_snapshot_format import parse_package, recipient_descriptor
from src.openclank.memory_policy_projection import policy_coordinator

_UNAVAILABLE = 'knowledge snapshot unavailable'


def _descriptor(raw, account_id, workspace_id, action_id):
    # recipient_descriptor owns package, scope and canonical-UUID validation.
    if type(raw) is not bytes or len(raw) > 65536:
        raise ValueError(_UNAVAILABLE)
    return recipient_descriptor(raw, account_id=account_id, workspace_id=workspace_id, action_id=action_id)


async def preview(*, store, owner, account_id, workspace_id, action_id, raw):
    # store is accepted for route symmetry; preview has no effect and reads nothing.
    try:
        descriptor = _descriptor(raw, account_id, workspace_id, action_id)
        knowledge = parse_package(raw)['knowledge']
    except Exception:
        raise ValueError(_UNAVAILABLE) from None
    return {'text': knowledge['text'], 'category': knowledge['category'], 'origin': descriptor['origin'],
            'package_sha256': descriptor['raw_sha256'], 'review_sha256': descriptor['review_sha256']}


async def confirm(*, provider, store, owner, account_id, workspace_id, policy_projection, current, write_current, action_id, raw, review_sha256):
    try:
        descriptor = _descriptor(raw, account_id, workspace_id, action_id)
        if descriptor['review_sha256'] != review_sha256 or await current() is not True or write_current() is not True:
            raise ValueError(_UNAVAILABLE)
        ids = {'account_id': account_id, 'workspace_id': workspace_id, 'action_id': action_id}
        # Each owner call below replays the same original job when re-entered.
        original = store.create_knowledge_snapshot(owner=owner, **ids, raw=raw, expected_review_sha256=review_sha256)
        batch, item = original['batch_id'], original['items'][0]['item_id']
        staged = store.stage_knowledge_snapshot(owner=owner, **ids, batch_id=batch, item_id=item)
        suggestion = staged['items'][0]['result']['suggestions'][0]['suggestion_id']
        key = 's10-knowledge-snapshot:' + action_id
        review = store.prepare_knowledge_snapshot_review(owner=owner, **ids, batch_id=batch, item_id=item,
                                                         suggestion_id=suggestion, review_key=key)['review']
        selector = {'version': 1, 'owner': owner, 'workspace_id': workspace_id, 'account_id': account_id,
                    'batch_id': batch, 'item_id': item, 'suggestion_id': suggestion,
                    'review_key_hash': review['review_key_hash'], 'proposal_fingerprint': review['proposal_fingerprint'],
                    'mode': 'review_only', 'policy_projection': policy_projection}
        lookup = {'owner': owner, 'batch_id': batch, 'item_id': item, 'suggestion_id': suggestion,
                  'review_key_hash': review['review_key_hash'], 'proposal_fingerprint': review['proposal_fingerprint'], **ids}

        def before_issue():
            # Runs synchronously inside the provider loop, so the async `current` cannot be awaited here;
            # the store read rechecks the human scope and the coordinator rechecks the policy refs.
            try:
                coordinator = policy_coordinator()
                return (write_current() is True and coordinator is not None and coordinator.checked_refs(owner, account_id, workspace_id) == policy_projection
                        and store.read_original_knowledge_snapshot_review(**lookup)['review'] == review)
            except Exception:
                return False

        result = await provider._host_knowledge_snapshot_review_status(selector)
        if result['state'] == 'absent':
            try:
                await provider._host_knowledge_snapshot_review_apply(selector, before_issue=before_issue)
            except Exception:
                pass  # outcome uncertain: the status read below is the only truth, never a second issue
            result = await provider._host_knowledge_snapshot_review_status(selector)
        if result['state'] == 'staged':
            store.finalize_knowledge_snapshot_review(owner=owner, **ids, batch_id=batch, item_id=item, suggestion_id=suggestion,
                                                     review_key=key, publication_id=result['candidate_id'], publication_kind='candidate')
            result = await provider._host_knowledge_snapshot_review_status(selector)
        if await current() is not True or write_current() is not True:
            raise ValueError(_UNAVAILABLE)
        return result
    except Exception:
        raise ValueError(_UNAVAILABLE) from None


async def status(*, provider, store, owner, account_id, workspace_id, policy_projection, current, action_id, raw, review_sha256):
    # Read-only: never creates, stages, applies or finalizes.
    try:
        descriptor = _descriptor(raw, account_id, workspace_id, action_id)
        if descriptor['review_sha256'] != review_sha256 or await current() is not True:
            raise ValueError(_UNAVAILABLE)
        ids = {'account_id': account_id, 'workspace_id': workspace_id, 'action_id': action_id}
        found = store.find_knowledge_snapshot(owner=owner, **ids, raw=raw, expected_review_sha256=review_sha256)
        item = found['items'][0] if found is not None else None
        suggestions = ((item or {}).get('result') or {}).get('suggestions')
        suggestion = suggestions[0] if type(suggestions) is list and len(suggestions) == 1 else None
        review = suggestion.get('review') if type(suggestion) is dict else None
        if type(review) is not dict:
            # Native review is issued only after prepare stores suggestion.review, so no effect can exist yet.
            result = {'state': 'absent'}
        else:
            lookup = {'owner': owner, 'batch_id': found['batch_id'], 'item_id': item['item_id'], 'suggestion_id': suggestion['suggestion_id'],
                      'review_key_hash': review['review_key_hash'], 'proposal_fingerprint': review['proposal_fingerprint'], **ids}
            store.read_original_knowledge_snapshot_review(**lookup, allow_settled=True)  # raises unless it is this action's review
            result = await provider._host_knowledge_snapshot_review_status(
                {'version': 1, 'workspace_id': workspace_id, 'mode': 'review_only', 'policy_projection': policy_projection,
                 **{k: v for k, v in lookup.items() if k != 'action_id'}})
        if await current() is not True:
            raise ValueError(_UNAVAILABLE)
        return result
    except Exception:
        raise ValueError(_UNAVAILABLE) from None
