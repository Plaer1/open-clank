"""Bounded real packaged-server Graph/source/Files/Editor API qualification."""
from __future__ import annotations

import hashlib
from pathlib import Path
import secrets
import time
import urllib.parse


def qualify_graph(request, home: Path) -> dict:
    """Use the caller's authenticated HTTP request(path, body) on its own app."""
    root = home / 'archive Graph source'
    root.mkdir()
    source = root / 'source.py'
    original = 'def archive_linux_target():\n    return "packaged native source"\n'
    source.write_text(original)
    workspace = request('/api/file-policy/workspaces/from-path', {
        'path': str(root), 'purpose': 'app_folder', 'name': 'Archive native Graph fixture',
    })['workspace']
    workspace_id = workspace['id']
    subscriber = secrets.token_hex(16)
    route = '/api/file-policy/workspaces/' + urllib.parse.quote(workspace_id, safe='') + '/index/'
    event = {'subscriber_id': subscriber, 'purpose': 'app_folder'}
    try:
        index = request(route + 'open', event)['index']
        deadline = time.monotonic() + 90
        while index['lifecycle'] != 'ready':
            if index['lifecycle'] in {'failed', 'erased', 'denied', 'unavailable'}:
                raise AssertionError(('Packaged native Graph index unavailable', index.get('error')))
            if time.monotonic() >= deadline:
                raise TimeoutError('Packaged native Graph index did not become ready')
            time.sleep(.1)
            index = request(route + 'status', event)['index']
        query = request('/api/memory/code/graph', {'workspace_id': workspace_id, 'query': {
            'operation': 'search', 'query': 'archive_linux_target', 'channels': ['identifier'],
            'fields': ['label', 'source', 'properties'], 'filter': {'kinds': ['declaration']},
            'budgets': {'rows': 20, 'bytes': 65536, 'tokens': 8192},
        }})
        assert query['error'] is None, query.get('error')
        hit = next(row for row in query['hits'] if row['kind'] == 'declaration' and row['label'] == 'archive_linux_target')
        selection = {'workspace_id': workspace_id, 'operation': 'resolve',
                     'reference': {'kind': 'code', 'id': hit['id'], 'receipt': query['receipt']}, 'excerpt_bytes': 1024}
        bridge = request('/api/memory/code/resource', selection)
        assert bridge['state'] == 'current' and bridge['current_source_verified'] is True, bridge.get('state')
        assert bridge['validation_kind'] == 'current_source'
        opened = request('/api/files-v1/open-resource', {'resource_ref': bridge['destination']['resource']['ref']})
        assert opened['payload']['text'] == original
        assert opened['payload']['resource']['key']['provider'] == 'host'
        assert 'path' not in opened['payload']
        source.write_text(original + '# actual external edit\n')
        stale = request('/api/memory/code/resource', selection)
        assert stale['state'] == 'stale' and stale['destination'] is None and stale['excerpt'] is None
        return {'outcome': 'passed', 'index_lifecycle': index['lifecycle'],
                'source_sha256': hashlib.sha256(original.encode()).hexdigest(),
                'hit_kind': hit['kind'], 'source_verified': True, 'files_editor_payload_exact': True,
                'external_edit_denied': True,
                'layer': 'actual extracted frozen server HTTP/native Graph/source/Files/Editor API; source browser qualification separate'}
    finally:
        request(route + 'release', event)
