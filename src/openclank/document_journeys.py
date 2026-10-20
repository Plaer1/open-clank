"""Canonical document references and successful journey receipts.

Resolution uses the selected Copal owner/workspace; names never grant access.
Occurrence IDs bind the accepted source revision and exact reference range.
"""
from __future__ import annotations
import hashlib
import json
import posixpath
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, quote

def references(text):
    """Wiki and Markdown targets, including balanced, angle and titled forms."""
    fence = None
    offset = 0
    for line in str(text or '').splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if marker:
            run, suffix = marker.groups()
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not suffix.strip():
                fence = None
            offset += len(line)
            continue
        if fence:
            offset += len(line)
            continue
        i = 0
        while i < len(line):
            if line[i] == "\\":
                i += 2; continue
            if line[i] == '`':
                run = len(line[i:]) - len(line[i:].lstrip('`'))
                end = line.find('`' * run, i + run)
                i = end + run if end >= 0 else i + run
                continue
            begin = i
            start = i + (line[i] == '!')
            if line[start:start + 2] == '[[':
                end = line.find(']]', start + 2)
                if end < 0:
                    i += 1; continue
                raw_start = start + 2
                raw_end = min([value for value in (line.find('|', raw_start, end), end) if value >= 0])
                syntax = 'wiki'
                finish = end + 2
            elif line[start:start + 1] == '[':
                label = re.match(r'\[((?:\\.|[^\]\\])*)\]\(', line[start:])
                if not label:
                    i += 1; continue
                body = start + len(label.group())
                depth, cursor, angle = 1, body, False
                while cursor < len(line):
                    char = line[cursor]
                    if char == "\\":
                        cursor += 2; continue
                    if char == '<' and cursor == body:
                        angle = True
                    elif angle:
                        if char == '>': angle = False
                    elif char == '(':
                        depth += 1
                    elif char == ')':
                        depth -= 1
                        if depth == 0: break
                    cursor += 1
                if depth:
                    i += 1; continue
                raw_start, raw_end = body, cursor
                if line[body:body + 1] == '<':
                    close = line.find('>', body + 1, cursor)
                    if close < 0:
                        i += 1; continue
                    raw_start, raw_end = body + 1, close
                else:
                    title = re.search(r'\s+["\'].*["\']\s*$', line[body:cursor])
                    if title: raw_end = body + title.start()
                syntax, finish = 'markdown', cursor + 1
            else:
                i += 1; continue
            raw = line[raw_start:raw_end].strip()
            yield {'start': offset + begin, 'end': offset + finish, 'target': raw, 'syntax': syntax,
                   'targetStart': offset + raw_start, 'targetEnd': offset + raw_end}
            i = finish
        offset += len(line)

def normalized(name):
    return re.sub(r'\.md$', '', unicodedata.normalize('NFC', str(name)).casefold())

def resolve(target, source, documents):
    path = unquote(re.sub(r'\\([\\`*_[\]{}()#+.!|<>~-])', r'\1', target.split('#', 1)[0]))
    if not path or re.match(r'^[a-z][a-z\d+.-]*:', path, re.I) or path.startswith('//'):
        return None
    root = posixpath.normpath(path).lstrip('/')
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(source.get('name', '')), path))
    explicit = path.startswith(('./', '../'))
    for wanted in ([root] if path.startswith('/') else [relative, *([] if explicit else [root])]):
        matches = [doc for doc in documents if normalized(doc.get('name')) == normalized(wanted)]
        if matches:
            return matches[0] if len(matches) == 1 else None
    if not explicit and '/' not in path:
        matches = [doc for doc in documents if normalized(posixpath.basename(doc.get('name', ''))) == normalized(path)]
        if not matches:
            matches = [doc for doc in documents if any(normalized(alias) == normalized(path) for alias in ([doc.get('properties', {}).get('aliases')] if isinstance(doc.get('properties', {}).get('aliases'), str) else (doc.get('properties', {}).get('aliases') or [])))]
        return matches[0] if len(matches) == 1 else None
    return None

async def catalogue(bridge, scope):
    from routes.copal_routes import _note_view
    listed = await bridge.call('index', {**scope, 'corpus':'all'}, timeout=60)
    return [_note_view(doc) for doc in listed.get('docs', [])]

def occurrences(source, documents):
    result = []
    for ref in references(source.get('text')):
        target = resolve(ref['target'], source, documents)
        if not target or target.get('id') == source.get('id') or target.get('kind') == 'asset':
            continue
        identity = [source.get('id'), str(source.get('head')), ref['start'], ref['end'], target.get('id')]
        public = {**ref, 'start': len(str(source.get('text') or '')[:ref['start']].encode('utf-16-le')) // 2, 'end': len(str(source.get('text') or '')[:ref['end']].encode('utf-16-le')) // 2}
        result.append({**public, 'linkId': hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest(),
                       'sourceDocumentId': source['id'], 'sourceRevisionId': str(source['head']), 'targetDocumentId': target['id']})
    return result

async def committed(bridge, scope, document_id, emit):
    from routes.copal_routes import _note_view
    source = _note_view(await bridge.call('get', {**scope, 'id': document_id}))
    if source.get('readOnly') or source.get('note_error') or not source.get('head'):
        return []
    links = occurrences(source, await catalogue(bridge, scope))
    for link in links:
        emit('document.link.created', link['linkId'], {key: link[key] for key in ('linkId', 'sourceDocumentId', 'targetDocumentId')})
    if source.get('kind') == 'wiki' and links and str(source.get('text') or '').strip():
        emit('wiki.page.committed', f"{document_id}:{source['head']}", {'pageId': document_id, 'pageKind': 'wiki', 'chunkCommitted': True,
             'linkTargetId': links[0]['targetDocumentId'], 'linkValid': True})
    return links

async def attachment_snapshot(bridge, scope, source, docs):
    result = []
    for ref in references(source.get('text')):
        target = resolve(ref['target'], source, docs)
        if not target or target.get('kind') != 'asset':
            continue
        located = await bridge.call('asset_path', {**scope, 'id': target['id']})
        path = Path(str(located.get('path') or ''))
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        result.append({**ref, 'assetId': target['id'], 'name': target['name'], 'sha256': digest.hexdigest()})
    return result

async def prepare_move(bridge, scope, document_id, name, write):
    """Root relative attachment names before moving, preserving their identity.

    A failed rename still leaves equivalent resolvable references at the old
    location. The normal scoped CAS/write and history boundary owns this edit.
    """
    from routes.copal_routes import _note_view, _encode_note
    stored = await bridge.call('get', {**scope, 'id': document_id})
    source = _note_view(stored)
    if source.get('name') == name:
        return None
    docs = await catalogue(bridge, scope)
    before = await attachment_snapshot(bridge, scope, source, docs)
    text = str(source.get('text') or '')
    # Loose Copal's existing move planner repairs/moves the mirrored media
    # namespace in its commit lock. Do not rewrite or move it a second time.
    for ref in reversed([] if hasattr(bridge, '_plan_and_apply_media_move') else before):
        fragment = '#' + ref['target'].split('#', 1)[1] if '#' in ref['target'] else ''
        rooted = '/' + ref['name']
        if ref['syntax'] == 'markdown': rooted = quote(rooted, safe='/.-_~')
        else: rooted = quote(rooted, safe='/ .-_~()').replace(']', '\\]').replace('|', '\\|')
        text = text[:ref['targetStart']] + rooted + fragment + text[ref['targetEnd']:]
    expected_head = source.get('head')
    if text != source.get('text'):
        previous = {'body': {'type': 'doc', 'blocks': stored.get('blocks') or []}, 'properties': stored.get('propertyDefinitions') or [],
                    'relations': stored.get('relations') or [], 'extensions': stored.get('extensions') or {}} if stored.get('format') == 'copal-note-v1' else None
        if previous is None and source.get('kind') in {'note', 'wiki'}:
            previous = json.loads(str(stored.get('text') or '{}'))
        content = _encode_note(text, source.get('properties'), source.get('relations'), previous) if source.get('kind') in {'note', 'wiki'} else text
        accepted = await write({**scope, 'id': document_id, 'content': content, 'base': source.get('head'), 'corpus': 'wiki' if source.get('kind') == 'wiki' else 'notes'})
        if accepted.get('outcome') in {'stale', 'conflict', 'failed'}:
            raise ValueError('Attachment references changed; reload before moving')
        expected_head = (accepted.get('doc') or {}).get('head')
        if not expected_head: raise ValueError('Attachment preservation returned no accepted revision')
    return {'documentId': document_id, 'name': source.get('name'), 'attachments': before, 'expectedHead': expected_head}

async def finish_move(bridge, scope, prepared, emit):
    if not prepared or not prepared['attachments']:
        return
    from routes.copal_routes import _note_view
    source = _note_view(await bridge.call('get', {**scope, 'id': prepared['documentId']}))
    if source.get('name') == prepared['name']:
        return
    after = await attachment_snapshot(bridge, scope, source, await catalogue(bridge, scope))
    expected = sorted((ref['assetId'], ref['sha256']) for ref in prepared['attachments'])
    actual = sorted((ref['assetId'], ref['sha256']) for ref in after)
    if expected != actual:
        raise ValueError('Moved document attachment verification failed')
    emit('document.move.committed', f"{source['id']}:{source['head']}:{source['name']}", {'documentId': source['id'],
         'attachmentIds': sorted({item[0] for item in expected}), 'referencesVerifiedIntact': True})
