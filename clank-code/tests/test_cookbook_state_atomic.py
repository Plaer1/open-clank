"""Cookbook state reservation and stale-writer boundary without a download."""
import asyncio
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from routes import cookbook_routes
from src.openclank.cookbook_state import (PRIVATE_KEY, CookbookStateError, advance,
    client_state, merge_orphan_tasks, reserve, save, transaction)


class CookbookStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_serialized_reservation_and_client_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cookbook_state.json'
            def writer(n):
                with transaction(path) as state:
                    state.setdefault('tasks', []).append({'sessionId': str(n)})
                    save(path, state)
            with transaction(path) as state:
                first = reserve(state, operation_id='op', account_id='account', digest='a'*64)
                save(path, state)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(writer, range(20)))
            with transaction(path) as state:
                self.assertEqual(len(state['tasks']), 20)
                merge_orphan_tasks(state, [{'sessionId':'new', '_adoptedExternally':True},
                                           {'sessionId':'0', '_adoptedExternally':True}])
                self.assertEqual(len(state['tasks']), 21)
                self.assertEqual(reserve(state, operation_id='op', account_id='account', digest='a'*64), first)
                with self.assertRaises(CookbookStateError):
                    reserve(state, operation_id='op', account_id='other', digest='a'*64)
                advance(state, operation_id='op', account_id='account', digest='a'*64,
                        revision=1, expected='prepared', target='dispatching')
                with self.assertRaises(CookbookStateError):
                    advance(state, operation_id='op', account_id='account', digest='a'*64,
                            revision=1, expected='prepared', target='dispatching')
                save(path, state)
                self.assertNotIn(PRIVATE_KEY, client_state(state))
            app = FastAPI()
            with patch.object(cookbook_routes, 'COOKBOOK_STATE_FILE', str(path)), patch.object(cookbook_routes, 'require_admin', lambda request: None):
                app.include_router(cookbook_routes.setup_cookbook_routes(provider_store=object(), provider_control=object()))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    self.assertNotIn(PRIVATE_KEY, (await client.get('/api/cookbook/state')).json())
                    self.assertFalse((await client.post('/api/cookbook/state', json={PRIVATE_KEY:{}})).json()['ok'])
                    self.assertTrue((await client.post('/api/cookbook/state', json={'tasks':[]})).json()['ok'])
                    with transaction(path) as state:
                        self.assertEqual(state[PRIVATE_KEY]['op']['state'], 'dispatching')
                    path.write_text('{bad')
                    self.assertEqual((await client.get('/api/cookbook/state')).status_code, 503)
                    self.assertFalse((await client.post('/api/cookbook/state', json={})).json()['ok'])
                    self.assertEqual(path.read_text(), '{bad')
