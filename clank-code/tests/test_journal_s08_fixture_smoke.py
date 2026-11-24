"""The SEED store opens under the real binary and advertises `journal_synthesis` (Native readiness `outputs`)."""
import asyncio

import pytest

from tests import _s08_journal_fixture as S


@pytest.fixture(scope='module')
def fx():
    fixture = S.build_s08_fixture()
    yield fixture
    fixture.close()


def test_native_advertises_journal_synthesis_and_the_host_accepts_it(fx):
    async def scenario():
        provider = fx.providers['alice']
        await provider.initialize()
        assert await provider._host_negotiate_s08_policy(owner='alice', workspace_id=fx.owners['alice'][1]) is True
        assert provider._host_s08_policy_ready('summary') is True
        assert provider._host_s08_policy_ready('journal_synthesis') is True
        assert provider._s08_policy_witness[6][:2] == ('summary', 'journal_synthesis')
    asyncio.run(scenario())


def _manifest(**changes):
    base = {'version': 1, 'candidate_id': 's08candidate_' + '1' * 64, 'candidate_revision': 3, 'candidate_sha256': '2' * 64, 'output_kind': 'journal_synthesis',
            'content': 'The user lives in Lisbon.',
            'journal': {'chat_id': 'chat-1', 'selection_sha256': '3' * 64, 'page_sha256': '4' * 64, 'prior_cursor': 0, 'end_seq': 6, 'high_seq': 9,
                        'cited': [{'seq': 4, 'actor_id': 'main', 'message_id': 'm1', 'part_id': 'body', 'revision': 1, 'content_hash': '5' * 64, 'selector': {'seq': 4}}]},
            'producer': {'actor': 's08_journal', 'job_id': 's08job_' + '6' * 64, 'attempt_id': 'a1', 'mode': 'model', 'model_route_id': 'route-1', 'assignment_sha256': '7' * 64}}
    base.update(changes)
    return base


def test_the_host_validates_a_journal_review_manifest_exactly(fx):
    """The Host review manifest for `journal_synthesis` (a canned Native answer through the real provider method): the exact shape passes, anything else refuses."""
    from src.memery_provider import MemeryProvider
    from src.memory_provider import MemoryRequestRejectedError

    async def scenario(answer):
        provider = fx.providers['alice']
        await provider.initialize()
        calls = []

        async def rpc(owner, workspace, payload, deadline, read_ticket=None):
            calls.append(payload)
            return answer
        provider._s07_owner_rpc = rpc
        kwargs = dict(owner='alice', account_id=S.ACCOUNT, workspace_id=S.WORKSPACE, candidate_id=answer['candidate_id'] if isinstance(answer, dict) and 'candidate_id' in answer else 'x',
                      output_kind='journal_synthesis', policy_user='alice', policy_projection=fx.coordinator.projection, current=lambda: True)
        out = await MemeryProvider._host_s08_policy_review_manifest(provider, **kwargs)
        assert calls[0]['phase'] == 's08_policy_review_manifest' and calls[0]['output_kind'] == 'journal_synthesis'
        return out
    good = _manifest()
    assert asyncio.run(scenario(good))['content'] == 'The user lives in Lisbon.'
    bad = [_manifest(extra=1), _manifest(content=''), _manifest(content='x' * 4097), _manifest(candidate_revision=4), _manifest(candidate_sha256='z' * 64),
           _manifest(output_kind='summary'), _manifest(producer={'actor': 's08_journal'}),
           _manifest(journal={**good['journal'], 'cited': []}), _manifest(journal={**good['journal'], 'page_sha256': 'nothex'}),
           {**good, 'contributors': [], 'selection': []}]
    for answer in bad:
        try:
            asyncio.run(scenario(answer))
        except MemoryRequestRejectedError:
            continue
        raise AssertionError(f'a malformed journal manifest was accepted: {answer}')


def test_a_v1_store_offers_no_journal_synthesis_and_stages_nothing():
    """Prepared with the S08 model v1 profile only: readiness has no journal output, prepare and status refuse before any write, the scheduler
    pass answers `unavailable` quietly."""
    import hashlib
    import logging
    import sqlite3
    from tests._native_ops_fixture import rows
    from src.memory_provider import MemoryRequestRejectedError
    from src.openclank import fm_journal_delta as delta
    v1 = S.build_s08_fixture(v2=False)
    try:
        async def scenario():
            provider = v1.providers['alice']
            await provider.initialize()
            assert await provider._host_negotiate_s08_policy(owner='alice', workspace_id=S.WORKSPACE) is True
            assert provider._s08_policy_witness[6] == ('summary',)
            assert provider._host_s08_policy_ready('summary') is True and provider._host_s08_policy_ready('journal_synthesis') is False
            with pytest.raises(MemoryRequestRejectedError):
                await provider._host_journal_delta_prepare({'selection': {'journal': {}}}, before_issue=lambda: True)
            refs = v1.coordinator.checked_refs('alice', S.ACCOUNT, S.WORKSPACE)
            with pytest.raises(MemoryRequestRejectedError):
                await provider._host_journal_delta_status({'version': 1, 'owner': 'alice', 'workspace_id': S.WORKSPACE, 'account_id': S.ACCOUNT,
                                                          'policy_projection': refs, 'selection_sha256': '1' * 64, 'prepare': {}})
            # the scheduler pass: unavailable, quietly
            from types import SimpleNamespace
            lifecycle = SimpleNamespace(provider=provider, binding=SimpleNamespace(memory_owner='alice', memory_workspace=S.WORKSPACE), owner='alice',
                                        target_authority=None, source_authority=None, mapping=None)
            import src.openclank.fm_journal_delta as d
            import routes.prefs_routes as prefs
            orig = prefs._load_for_user
            prefs._load_for_user = lambda owner: {d.PREF_KEY: True}
            try:
                result = await d.run_chat_pass(profile=None, lifecycle=lifecycle, assignment=None, account_id=S.ACCOUNT, chat_id=S.CHAT)
            finally:
                prefs._load_for_user = orig
            assert result == {'state': 'unavailable'}
        records = []
        handler = logging.Handler(); handler.emit = records.append
        logging.getLogger().addHandler(handler)
        try:
            asyncio.run(scenario())
        finally:
            logging.getLogger().removeHandler(handler)
        # (the MCP client library's own per-call 'structured content' warning is not ours)
        assert [r.getMessage() for r in records if r.levelno >= logging.INFO and r.name != 'client'] == [], 'no log noise above debug'
        assert rows(v1.db, "SELECT count(*) FROM fm_v2_jobs") == [(0,)], 'nothing was staged'
    finally:
        v1.close()
