"""Named-human, read-only shadow ranking report for the caller's own account (observation only, nothing stored)."""
import json
import re
from fastapi import Request
from fastapi.responses import JSONResponse

_HEADERS = {'Cache-Control': 'private, no-store'}
MAX_SOURCES, MAX_CALLS, PAGE_LIMIT = 20, 20, 200  # one budget of page calls; a failing call still spends one
_INT_KEYS = ('receipts', 'attempts', 'labelled', 'positive', 'negative', 'conflicting_labels',
             'distinct_feature_vectors', 'min_labelled')
_AGREEMENT_KEYS = ('basis', 'mixed_pairs', 'shadow_pair_accuracy', 'delivered_pair_accuracy',
                   'attempts_same_order', 'attempts_multi_item')
_REASONS = ('constant', 'always_unknown')
_WITHHELD = ('below_min_labelled', 'no_mixed_pairs')
_NAME = re.compile(r'[a-z0-9_]{1,32}')
_KIND = re.compile(r'[a-z0-9_.:-]{1,64}')
_CUTOFF = re.compile(r'cs_s06admitted_[0-9a-f]{64}')


def project(report, *, truncated, seen, skipped, policy_revision=None, training_cutoff=None):
    """Closed public projection of shadow_report(); per-item ids and shadow_order never leave."""
    if report['state'] not in ('ok', 'no_receipts', 'insufficient_labels'): raise ValueError('state')
    out = {'state': report['state']}
    for key in _INT_KEYS: out[key] = int(report[key])
    features = report['uninformative_features']
    if type(features) is not dict or any(not _NAME.fullmatch(k) or v not in _REASONS for k, v in features.items()):
        raise ValueError('features')
    out['uninformative_features'] = dict(sorted(features.items()))
    agreement = report['agreement']
    if agreement is not None:
        agreement = {k: (str(agreement[k]) if k == 'basis' else float(agreement[k]) if 'accuracy' in k else int(agreement[k]))
                     for k in _AGREEMENT_KEYS}
    out['agreement'] = agreement
    withheld = report['agreement_withheld']
    if withheld is not None and withheld not in _WITHHELD: raise ValueError('withheld')
    out['agreement_withheld'] = withheld
    out.update(truncated=bool(truncated), sources_seen=int(seen), sources_skipped=int(skipped))
    # Ranking contract v1 state keys (nothing is stored; the report is rebuilt by replay on every call). The receipts carry only
    # their route kind, so `route_kinds` is the whole route identity; a candidate-route generation is not carried by them.
    from src.memery_ranking_feedback import FEATURE_VERSION, LABEL_VERSION
    kinds = report['route_kinds']
    if type(kinds) is not list or any(type(k) is not str or not _KIND.fullmatch(k) for k in kinds): raise ValueError('route_kinds')
    revision = policy_revision if type(policy_revision) is int and not isinstance(policy_revision, bool) and policy_revision >= 1 else None
    if training_cutoff is not None and (type(training_cutoff) is not str or not _CUTOFF.fullmatch(training_cutoff)): raise ValueError('cutoff')
    out['state_keys'] = {'policy_revision': revision, 'policy_revision_state': 'ok' if revision is not None else 'unavailable',
                         'feature_version': FEATURE_VERSION, 'label_version': LABEL_VERSION, 'route_kinds': sorted(kinds),
                         'training_cutoff': training_cutoff}
    return out


def register_shadow_ranking(router, *, require_human, capture_context, owned, load_prefs, lifecycle_for,
                            report_fn=None, projection_type=None, policy_revision_fn=None):
    """``load_prefs(user)`` -> dict; ``lifecycle_for(request, account)`` -> completed-turn lifecycle owner or None."""
    def closed(status, state='unavailable'):
        return JSONResponse({'state': state}, status_code=status, headers=_HEADERS)

    async def read_body(request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 256: raise ValueError('body too large')
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result: raise ValueError('duplicate key')
                result[key] = value
            return result
        body = json.loads(bytes(raw).decode('utf-8', 'strict'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid json')))
        if type(body) is not dict or body: raise ValueError('invalid request')

    @router.post('/shadow-ranking/report')
    async def shadow_ranking_report(request: Request):
        try:
            user = require_human(request); context = capture_context(request, user)
        except Exception:
            return closed(403)
        try:
            await read_body(request)
        except Exception:
            return closed(400)
        profile = getattr(request.app.state, 's05_profile', None)
        if profile is None:  # legacy store: nothing to guard, nothing to read
            return JSONResponse({'state': 'unavailable'}, headers=_HEADERS)
        try:
            account, owner, workspace = context['account'], context['owner'], context['workspace']  # server-side only
            async with context['barrier'].admitted(user):
                if not await owned(context['current_live']): raise ValueError('authority changed')
                await owned(profile.assert_current)
                prefs = await owned(load_prefs, user)
                if type(prefs) is not dict or prefs.get('memory_shadow_ranking_enabled') is not True:
                    return JSONResponse({'state': 'off'}, headers=_HEADERS)  # nothing else is read
                lifecycle = lifecycle_for(request, account)
                if lifecycle is None: return JSONResponse({'state': 'unavailable'}, headers=_HEADERS)
                provider = lifecycle.provider
                # Same owner fence as _discover_one_s08_page (src/task_scheduler.py ~2150-2152).
                if (lifecycle.target_authority.resolve(account) != lifecycle.binding
                        or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)):
                    raise ValueError('owner unavailable')
                if await provider._host_negotiate_found_features(owner=owner, workspace_id=workspace) is not True:
                    return JSONResponse({'state': 'unavailable'}, headers=_HEADERS)
                kind = projection_type
                if kind is None:
                    from src.memery_provider import _HostObservationProjection as kind

                async def page(origin, after):
                    source = origin.source
                    async with lifecycle.mapping.async_chat_admission_lock(source.chat_id) as guard:
                        await owned(lambda: lifecycle.for_source(source=source, guard=guard))
                        result = await provider._host_found_features_page(origin, after=after, limit=PAGE_LIMIT)
                        await owned(lambda: lifecycle.for_source(source=source, guard=guard))
                        lifecycle.mapping.validate_chat_guard(source.chat_id, guard)
                        if lifecycle.target_authority.resolve(account) != lifecycle.binding: raise ValueError('owner changed')
                    return result

                items, seen, calls, skipped, truncated, cursor = [], 0, 0, 0, False, 'o:'
                while True:
                    rows, following = await owned(lambda: provider._host_account_source_refs(
                        account_id=account, target_authority=lifecycle.target_authority, after_job_id=cursor, limit=1))
                    if not rows: break
                    if len(rows) != 1 or type(rows[0]) is not kind or rows[0].source.account_id != account:
                        raise ValueError('source inventory unavailable')
                    if seen >= MAX_SOURCES or calls >= MAX_CALLS:
                        truncated = True; break
                    origin = rows[0]; seen += 1; after = None
                    while True:
                        if calls >= MAX_CALLS:
                            truncated = True; break
                        calls += 1
                        try:
                            result = await page(origin, after)
                        except Exception:  # one source's refusal is counted, never surfaced
                            skipped += 1; break
                        items.extend(result['items'])
                        after = result['next_after']
                        if after is None: break
                    if truncated or following is None: break
                    cursor = following
                if report_fn is None:
                    from src.memery_ranking_feedback import shadow_report as report_fn_
                else:
                    report_fn_ = report_fn
                from src.openclank.memory_context_admission import found_features_rows
                receipts, labels = found_features_rows(owner, items)
                report = await owned(report_fn_, owner, receipts, labels)
                try:  # the owner's current S12 policy revision; a missing policy or a failed read is `unavailable`, never invented
                    if policy_revision_fn is None:
                        from src.openclank.memory_policy import STORE
                        revision = await owned(lambda u: STORE.snapshot(u)['revision'], user)
                    else:
                        revision = await owned(policy_revision_fn, user)
                except Exception:
                    revision = None
                cutoff = next((i['admitted_event_id'] for i in reversed(items) if i.get('admitted_event_id')), None)
                body = project(report, truncated=truncated, seen=seen, skipped=skipped, policy_revision=revision, training_cutoff=cutoff)
                if not await owned(context['current_live']): raise ValueError('authority changed')
            return JSONResponse(body, headers=_HEADERS)
        except Exception:
            return closed(503)
