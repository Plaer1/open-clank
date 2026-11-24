import asyncio

from src.openclank.media_adapters.base import _MEDIA_OPERATION_FACTS
from src.openclank.media_jobs import MediaCoordinator


def _reconcile(adapter_id, version, claim):
    identity = {'adapter_id': adapter_id, 'adapter_version': version, 'account_id': 'a'}
    job = {'identity': identity, 'chat_detached': False, 'status': 'running', 'revision': 1, 'claim': claim}
    c = object.__new__(MediaCoordinator)

    async def scope(identity, control=False): return True
    async def read(identity, job_id): return job
    async def owned(identity, op): return None
    c._scope, c._read, c._owned = scope, read, owned
    return asyncio.run(c._reconcile(identity, 'j'))['status']


COLLECT = {'action': 'collect'}


def test_catalog_owned_fetch_matching_version_unknown():
    assert [n for n, f in _MEDIA_OPERATION_FACTS if f.owned_fetch]
    for name, facts in _MEDIA_OPERATION_FACTS:
        assert (_reconcile(name, '1', COLLECT) == 'unknown_outcome') == facts.owned_fetch, name


def test_owned_fetch_mismatched_version_follows_the_snapshot_legacy_list():
    # BQ-20: a record that is not a known declaration is classified by the legacy list, as at the snapshot
    for name in ('comfyui-material-v1', 'meshy-rig-v1', 'voidless-image-v1'):
        assert _reconcile(name, '2', COLLECT) == 'running', name
    for name in ('meshy-preview-v1', 'meshy-refine-v1', 'tripo-text3d-v1', 'fal-flux-schnell-v1', 'fal-hunyuan-image3d-v1', 'comfyui-flac-v1'):
        assert _reconcile(name, '2', COLLECT) == 'unknown_outcome', name


def test_non_owned_and_unknown_adapter_not_unknown_by_clause():
    assert _reconcile('sprixen-sprite-v1', '1', COLLECT) == 'running'
    assert _reconcile('no-such-adapter', '1', COLLECT) == 'running'


def test_owned_lifetime_clause_alone():
    claim = {'action': 'collect', 'owned_lifetime': 'meshy-fetch-v1'}   # the donor-named label stays readable
    assert _reconcile('no-such-adapter', '1', {'action': 'collect', 'owned_lifetime': 'owned-fetch-v1'}) == 'unknown_outcome'
    assert _reconcile('no-such-adapter', '1', claim) == 'unknown_outcome'


def test_non_collect_claim_not_unknown_by_clause():
    assert _reconcile('meshy-rig-v1', '2', {'action': 'submit'}) == 'running'
