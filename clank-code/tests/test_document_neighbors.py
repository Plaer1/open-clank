import pytest

from src.openclank.document_neighbors import relation_page


class Journal(list):
    coverage = {'complete': True, 'generation': 'r1'}


def page(doc_id, name, text=''):
    return {'id': doc_id, 'head': 'h1', 'kind': 'wiki', 'name': name, 'text': text}


def test_unloaded_incoming_scope_and_stale_witness():
    docs = Journal([page('a', 'Destination'), page('b', 'First', '# Topic\n😀 [[Destination#Part]]'),
                    page('c', 'Second', '![[Destination]]')])
    scope = {'owner': 'alice', 'workspace_id': 'journal1'}
    def incoming(**extra):
        return relation_page(docs, scope, 'a', 'incoming', 'both', 1, **extra)
    first = incoming()
    link = first['links'][0]
    assert first['available'] == 2 and link['range']['from'] == 11
    assert link['fragment'] == 'Part' and link['range']['line'] == 2
    assert incoming(witness=link['id'])['link'] == link
    assert incoming(cursor=first['cursor'])['links'][0]['kind'] == 'embed'
    scope['workspace_id'] = 'journal2'
    with pytest.raises(ValueError):
        incoming(cursor=first['cursor'])
    docs[1]['head'] = 'h2'
    with pytest.raises(ValueError):
        incoming(witness=link['id'])


def test_private_missing_and_duplicate_names():
    docs = Journal([page('a', 'Origin', '[[Twin]] [[Absent]]'), page('b', 'One/Twin'), page('c', 'Two/Twin')])
    docs[0]['relations'] = [{'origin':'explicit','kind':'link','target':'Private name','targetDocumentId':'foreign'}]
    result = relation_page(docs, {'owner':'a','workspace_id':'j'}, 'a', 'outgoing', 'both', 32)
    assert result['unresolved'] == {'ambiguous':1,'missing':1,'out_of_scope':1}
    assert 'Private name' not in str(result)


def test_authenticated_route_reads_real_journal(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI
    import routes.copal_routes as routes
    from src.openclank.copal_loose import LooseCopalBridge

    async def run():
        monkeypatch.setattr(routes, 'require_user', lambda request: 'reader')
        bridge = LooseCopalBridge(tmp_path)
        monkeypatch.setattr(bridge, '_ensure_official_references', lambda vault, manifest, actor: manifest)
        app = FastAPI()
        app.state.copal_bridge = bridge
        app.state.auth_manager = SimpleNamespace(account_id=lambda username:'acct')
        app.include_router(routes.setup_copal_routes())
        async def create(name, body):
            result = await bridge.call('create', {'owner':'reader','workspace_id':'default','kind':'wiki',
                                                  'name':name,'content':routes._encode_note(body, {}, [])})
            return result['doc']['id']
        target = await create('Garden', '# Seeds')
        origin = await create('Inventory', '😀 [[Garden#Seeds]]')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            path = f'/api/copal/documents/{target}/neighbors'
            response = await client.get(path, headers={'X-Copal-Account':'acct'})
            assert response.status_code == 200, response.text
            row = response.json()['links'][0]
            assert row['source']['id'] == origin and row['range']['from'] == 3
            assert (await client.get(path, params={'witness':row['id']})).json()['link'] == row
            assert (await client.get(path, headers={'X-Copal-Account':'old'})).status_code == 409
            assert (await client.get(path, params={'workspace':'other'})).status_code == 404
    asyncio.run(run())
