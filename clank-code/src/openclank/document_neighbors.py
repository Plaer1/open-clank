"""Page exact link occurrences inside one authorized Copal reference snapshot."""
import hashlib
import json
from urllib.parse import unquote

from src.openclank.document_journeys import references, resolve_outcome


def digest(parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _relations(doc):
    text = str(doc.get('text') or '')
    relations = doc.get('relations') or []
    typed = {(r.get('kind'), r.get('target'), r.get('fragment') or ''): r for r in relations
             if r.get('kind') in ('link', 'embed') and r.get('origin') == 'body'}
    offset = units = 0
    line = 1
    for ref in references(text):
        start, end = ref['start'], ref['end']
        skipped = text[offset:start]
        line += skipped.count('\n')
        units += len(skipped.encode('utf-16-le')) // 2
        spelling = text[start:end]
        width = len(spelling.encode('utf-16-le')) // 2
        kind = 'embed' if spelling.startswith('!') else 'link'
        path, _, fragment = ref['target'].partition('#')
        relation = typed.get((kind, path, fragment), {})
        yield kind, ref['target'], relation, {'from': units, 'to': units + width, 'line': line,
                                               'spellingHash': hashlib.sha256(spelling.encode()).hexdigest()}
        offset, units = end, units + width
    for rel in relations:
        if rel.get('origin') == 'explicit' and rel.get('kind') in ('link', 'embed'):
            target = rel['target'] + ('#' + rel['fragment'] if rel.get('fragment') else '')
            yield rel['kind'], target, rel, None


def relation_page(documents, scope, selected, direction, kind, limit, cursor=None, witness=None):
    coverage = documents.coverage
    generation = coverage.get('generation')
    if not coverage.get('complete') or not generation:
        raise LookupError('The journal reference inventory is incomplete.')
    by_id = {d['id']: d for d in documents}
    if selected not in by_id:
        raise KeyError(selected)
    binding = digest([scope['owner'], scope['workspace_id'], selected, direction, kind, generation])
    offset = 0
    if cursor:
        fingerprint, _, position = cursor.partition('.')
        if fingerprint != binding or not position.isascii() or not position.isdecimal() or len(position) > 10:
            raise ValueError('The journal or link filters changed. Reload connections.')
        offset = int(position)
    total = 0
    rows = []
    unavailable = {}
    sources = (by_id[selected],) if direction == 'outgoing' else sorted(documents, key=lambda doc: doc['id'])
    for source in sources:
        if source.get('kind') not in ('note', 'wiki', 'markdown', 'text'):
            continue
        for relation_kind, spelling, relation, span in _relations(source):
            if kind not in ('both', relation_kind):
                continue
            target_id = relation.get('targetDocumentId')
            if target_id and relation.get('origin') == 'explicit':
                target = by_id.get(target_id)
                status = 'resolved' if target else 'out_of_scope'
            elif spelling.startswith('#'):
                status, target = 'resolved', source
            else:
                status, target = resolve_outcome(spelling, source, documents)
            if not target:
                if direction == 'outgoing' and status != 'unsupported':
                    unavailable[status] = unavailable.get(status, 0) + 1
                continue
            if target.get('kind') not in ('note', 'wiki', 'markdown', 'text') or (direction == 'incoming' and target['id'] != selected):
                continue
            row = {'source': {k: source.get(k) for k in ('id', 'head', 'name')},
                   'target': {k: target.get(k) for k in ('id', 'head', 'name')},
                   'kind': relation_kind, 'fragment': unquote(spelling.partition('#')[2]),
                   'range': span, 'sourceBlockId': relation.get('sourceBlockId'), 'targetBlockId': relation.get('targetBlockId')}
            row['id'] = digest([source['id'], source.get('head'), target['id'], target.get('head'),
                                relation.get('id'), relation_kind, span, spelling, target.get('name'), source.get('name')])
            if witness:
                if row['id'] == witness:
                    return {'link': row, 'generation': generation}
            elif offset <= total < offset + limit:
                rows.append(row)
            total += 1
    if witness:
        raise ValueError('The link or its destination changed. Reload connections.')
    return {'documentId': selected, 'generation': generation, 'direction': direction, 'kind': kind,
            'available': total, 'links': rows, 'unresolved': unavailable,
            'cursor': f'{binding}.{offset + len(rows)}' if offset + len(rows) < total else None}
