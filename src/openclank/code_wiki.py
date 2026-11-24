"""Deterministic, structure-only Graph wiki planner and page renderer.

No model code: pages come only from Graph catalog/outline/neighbors facts.
`query` is an injected async callable `query(dict) -> dict` taking a code-query
body and returning the code-query result envelope (the route passes a closure over
`query_code`; tests pass a stub).
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import quote

from src.openclank.code_summary_document import _label

SCHEMA = 'open-clank.code-wiki/v1'
DEFAULT_CAP, MAX_CAP = 25, 100
PAGE_BYTES = 262144  # same bound as code_summary_document's section limit
ROWS = 200
TOP_RELATIONS = 10
MAX_CATALOG_CALLS = 64
_FILE_ID = re.compile(r'file:[0-9a-f]{64}')
_RECEIPT_KEYS = {'workspace_id', 'registration_id', 'source_snapshot_id', 'checkout_id', 'head_generation',
                 'admission_epoch', 'policy_generation', 'config_fingerprint'}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _safe(value, limit):
    """Hostile Graph text: drop control characters, cut to `limit` bytes, then entity-escape."""
    text = ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in value) if isinstance(value, str) else ''
    text = text.strip() or '(unnamed)'
    while len(text.encode('utf-8', 'replace')) > limit:
        text = text[:-1]
    return _label(text.encode('utf-8', 'replace').decode('utf-8', 'replace') or '(unnamed)', limit * 4)


def _page_id(file_id, receipt):
    return hashlib.sha256((file_id + '|' + receipt['source_snapshot_id']).encode()).hexdigest()


def _marker(page_id):
    return f'<!-- openclank-wiki-page:v1:{page_id} -->'


def _check_receipt(receipt):
    if (not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS
            or any(type(receipt[k]) is not str or not receipt[k] for k in _RECEIPT_KEYS - {'admission_epoch', 'policy_generation'})):
        raise ValueError('Wiki receipt unavailable')
    return receipt


def _good(result, operation, receipt=None):
    if (not isinstance(result, dict) or result.get('error') or result.get('operation') != operation
            or not isinstance(result.get('hits'), list) or not isinstance(result.get('receipt'), dict)):
        raise ValueError('Graph query unavailable')
    if receipt is not None and result['receipt'] != receipt:
        raise ValueError('Source receipt changed')
    return result


def _bounded(result, what):
    """Names a degraded/partial query in words; empty list means a complete result."""
    if result.get('partial') or result.get('degraded') or result.get('next_cursor') or result.get('omissions'):
        return [f'{what} was partial or degraded; it may be incomplete.']
    return []


async def plan(body, *, query, community_citations=None):
    allowed = {'workspace_id', 'receipt', 'cap', 'architecture', 'communities', 'community_algorithm'}
    if not isinstance(body, dict) or set(body) - allowed or len(json.dumps(body, default=str)) > 16384:
        raise ValueError('Invalid wiki request')
    if not isinstance(body.get('workspace_id'), str) or not 1 <= len(body['workspace_id'].encode()) <= 256:
        raise ValueError('Invalid workspace')
    if any(key in body and body[key] is not True for key in ('architecture', 'communities')):
        raise ValueError('Invalid wiki coverage request')
    if 'community_algorithm' in body and (body.get('communities') is not True
            or body['community_algorithm'] not in ('label_propagation','modularity_refinement')):
        raise ValueError('Invalid community algorithm')
    cap = body.get('cap', DEFAULT_CAP)
    if type(cap) is not int or not 1 <= cap <= MAX_CAP:
        raise ValueError(f'Wiki page cap must be 1 to {MAX_CAP}')
    expected = body.get('receipt')
    if expected is not None and (_check_receipt(expected)['workspace_id'] != body['workspace_id']):
        raise ValueError('Receipt does not match workspace')
    receipt, files, cursor, omissions = expected, {}, None, []
    for _ in range(MAX_CATALOG_CALLS):
        q = {'operation': 'catalog', 'filter': {'kinds': ['file']}, 'fields': ['label', 'properties'], 'channels': [],
             'budgets': {'rows': ROWS, 'bytes': 262144}}
        if receipt is not None: q['expected_receipt'] = receipt
        if cursor: q['cursor'] = cursor
        result = _good(await query(q), 'catalog', receipt)
        receipt = _check_receipt(result['receipt'])
        omissions += _bounded({**result, 'next_cursor': None}, 'Catalog')
        for hit in result['hits']:
            ident = hit.get('id') if isinstance(hit, dict) else None
            label = hit.get('label') if isinstance(hit, dict) else None
            if hit.get('kind') == 'file' and isinstance(ident, str) and _FILE_ID.fullmatch(ident) and isinstance(label, str) and label:
                files.setdefault(ident, label)
        cursor = result.get('next_cursor')
        if not cursor or len(files) > cap: break
    else:
        omissions.append('Catalog paging stopped at its call limit; the file list is incomplete.')
    ordered = sorted(files.items(), key=lambda kv: (kv[1], kv[0]))
    if len(ordered) > cap:
        omissions.append(f'Catalog has more than {cap} files; only the first {cap} by path are planned.')
    elif cursor:
        omissions.append('More catalog pages exist; the file list is incomplete.')
    pages = [{'page_id': _page_id(fid, receipt), 'file_id': fid, 'rel_path': path, 'bytes_est': min(PAGE_BYTES, 2048 + 6 * len(path.encode()))}
             for fid, path in ordered[:cap]]
    omissions = sorted(set(omissions))
    core = {'schema': SCHEMA, 'receipt': receipt, 'pages': pages, 'cap': cap, 'omissions': omissions}
    if body.get('architecture'):
        core['architecture_markdown'] = await architecture_digest(receipt, files, query=query, catalog_limited=bool(omissions))
    if body.get('communities'):
        from src.openclank.code_wiki_communities import render_communities
        core['communities_markdown'] = await render_communities(receipt, query=query, citations=community_citations,
            algorithm=body.get('community_algorithm'))
    return {**core, 'plan_hash': _hash(core)}


async def architecture_digest(receipt, files, *, query, catalog_limited):
    """Describe one published index receipt with bounded, exact import evidence."""
    base = {'expected_receipt': receipt, 'fields': [], 'channels': [],
            'budgets': {'rows': 12, 'bytes': 65536}}
    lines = ['### Architecture digest', '',
             f"Workspace {_safe(receipt['workspace_id'], 128)} · snapshot {_safe(receipt['source_snapshot_id'], 128)}.",
             f"Catalog observed {len(files)} files." + (' The catalog was limited by the plan or index coverage.' if catalog_limited else '')]
    groups = {}
    for path in files.values():
        directory, _, _ = path.rpartition('/')
        groups[directory] = groups.get(directory, 0) + 1
    if groups:
        lines.extend(['', '#### Module directories', ''])
        lines.extend(f'- {_safe(directory or "(root)", 256)} · {count} files observed'
                     for directory, count in sorted(groups.items(), key=lambda item: (-item[1], item[0]))[:12])
        if len(groups) > 12:
            lines.append(f'- {len(groups) - 12} more directories observed.')
    result = await query({**base, 'operation':'import_analysis'})
    if not isinstance(result, dict) or result.get('error'):
        lines.extend(['', 'Import evidence is unavailable for this snapshot.'])
        return '\n'.join(lines) + '\n'
    _good(result, 'import_analysis', receipt)
    analysis = result.get('analysis') or {}
    counts = ('file_count', 'edge_count', 'inferred', 'ambiguous', 'unresolved', 'unsupported', 'excluded', 'missing')
    if analysis.get('policy') != 'exact_imports_v1' or any(type(analysis.get(key)) is not int or analysis[key] < 0 for key in counts):
        raise ValueError('Import evidence unavailable')
    lines.extend(['', '#### Indexed dependency evidence', '',
                  f"{analysis['file_count']} files and {analysis['edge_count']} exact directed import pairs in the analyzed index.",
                  f"Imports outside those pairs include {analysis['inferred']} inferred, {analysis['ambiguous']} ambiguous, "
                  f"{analysis['unresolved']} unresolved, {analysis['unsupported']} unsupported, {analysis['excluded']} outside scope, and {analysis['missing']} missing."])
    units = analysis.get('units') or []
    def label(ident):
        return _safe(files.get(ident, ident), 192)
    for unit in units[:6]:
        if unit.get('kind') == 'hub':
            lines.append(f"- Import hub {label(unit['node_id'])} · {unit['in_degree']} incoming, {unit['out_degree']} outgoing.")
        elif unit.get('kind') == 'cycle':
            members, witnesses = unit['node_ids'], unit['occurrence_ids']
            sample = ', '.join(label(ident) for ident in members[:4])
            lines.append(f"- Import cycle with {len(members)} members and {len(witnesses)} indexed import witnesses · {sample}"
                         + (' and more.' if len(members) > 4 else '.'))
            if witnesses:
                lines.append(f"  Witness {_safe(witnesses[0], 80)}.")
    if len(units) > 6 or _bounded(result, 'Dependencies'):
        lines.append('Dependency details are bounded. Additional results or index omissions may exist.')
    lines.append('Coverage is limited to statically indexed import evidence at this snapshot.')
    return '\n'.join(lines) + '\n'


def _fit(lines, budget):
    kept, used = [], 0
    for line in lines:
        size = len(line.encode()) + 1
        if used + size > budget: break
        kept.append(line); used += size
    return kept, len(lines) - len(kept)


def _citation_markdown(citations, *, limit):
    if citations is None:
        return [], False
    result = []
    items = citations.get("citations", [])
    for item in items[:limit]:
        fields = (item["documentId"], item["revisionId"], item["line"], item["startByte"], item["endByte"], item["spelling"])
        if item.get("anchorProof"):
            fields += (item["anchorProof"],)
        target = "clank://citation/" + "/".join(quote(str(field), safe="") for field in fields)
        result.append(f'- [{_safe(item["name"], 192)} · line {item["line"]}]({target})')
    return result, len(items) > len(result) or (citations.get("coverage") or {}).get("complete") is not True


def render_page(page, outline, neighbors, *, receipt, citations=None):
    """Markdown for one indexed file. Refuses any payload not pinned to `receipt`."""
    _check_receipt(receipt)
    if (not isinstance(page, dict) or not isinstance(page.get('file_id'), str) or not _FILE_ID.fullmatch(page['file_id'])
            or page.get('page_id') != _page_id(page['file_id'], receipt)):
        raise ValueError('Wiki page is not pinned to this receipt')
    outline, neighbors = _good(outline, 'outline', receipt), _good(neighbors, 'neighbors', receipt)
    omissions = _bounded(outline, 'Outline') + _bounded(neighbors, 'Neighbors')
    declarations = []
    for hit in outline['hits']:
        if isinstance(hit, dict) and hit.get('kind') == 'declaration' and isinstance(hit.get('id'), str):
            props = hit.get('properties') if isinstance(hit.get('properties'), dict) else {}
            declarations.append((hit['id'], _safe(props.get('declaration_kind'), 64), _safe(hit.get('label'), 256),
                                 _safe(hit['signature'], 512) if hit.get('signature') else ''))
    mine = {page['file_id']} | {d[0] for d in declarations}
    names = {h['id']: _safe(h.get('label'), 256) for h in neighbors['hits'] if isinstance(h, dict) and isinstance(h.get('id'), str)}
    incoming, outgoing = [], []
    for edge in neighbors.get('edges') or []:
        if not isinstance(edge, dict) or not isinstance(edge.get('from'), str) or not isinstance(edge.get('to'), str): continue
        row = (_safe(edge.get('relation_kind'), 64), _safe(edge.get('resolution_state'), 64))
        if edge['from'] in mine and edge['to'] not in mine: outgoing.append((*row, names.get(edge['to'], '(unnamed)')))
        elif edge['to'] in mine and edge['from'] not in mine: incoming.append((*row, names.get(edge['from'], '(unnamed)')))
    head = f"{_marker(page['page_id'])}\n## {_safe(page.get('rel_path'), 1024)}\n\nStructure from the code Graph at snapshot {_safe(receipt['source_snapshot_id'], 256)}; verify against the current source.\n"
    decl_lines = [f'- {k}: {n}' + (f' — {s}' if s else '') for _, k, n, s in declarations]
    rel_lines = {name: [f'- {k} ({st}): {n}' for k, st, n in sorted(rows)[:TOP_RELATIONS]] + ([f'- {len(rows) - TOP_RELATIONS} more not shown.'] if len(rows) > TOP_RELATIONS else [])
                 for name, rows in (('Outgoing relations', outgoing), ('Incoming relations', incoming))}
    citation_lines, incomplete = _citation_markdown(citations, limit=10)
    if incomplete:
        omissions.append('Citing-passage coverage was partial or unavailable.')
    tail_reserve = 4096 + 1024 * len(omissions)
    source_bytes = sum(len(line.encode()) + 1 for line in citation_lines)
    budget = PAGE_BYTES - len(head.encode()) - tail_reserve - source_bytes - sum(len(l.encode()) + 1 for v in rel_lines.values() for l in v)
    decl_lines, dropped = _fit(decl_lines, max(0, budget))
    if dropped: omissions.append(f'{dropped} declarations omitted by the page byte cap.')
    parts = [head, '### Declarations\n', '\n'.join(decl_lines) or '- None listed.']
    for name, lines in rel_lines.items(): parts += [f'\n### {name}\n', '\n'.join(lines) or '- None listed.']
    if citation_lines:
        parts += ['\n### Citing passages\n', '\n'.join(citation_lines)]
    parts += ['\n### Omissions\n', '\n'.join('- ' + _safe(o, 1024) for o in omissions) or '- None recorded.']
    text = '\n'.join(parts) + '\n'
    if len(text.encode()) > PAGE_BYTES: raise ValueError('Wiki page exceeds its byte cap')
    return text


async def fetch_page(page, *, query, receipt, citations=None):
    """Query outline and neighbors for one planned page (pinned to `receipt`), then render it."""
    base = {'seed_ids': [page['file_id']], 'expected_receipt': receipt, 'channels': [],
            'budgets': {'rows': ROWS, 'bytes': 262144}}
    outline = await query({**base, 'operation': 'outline', 'filter': {'rel_path': page['rel_path']}, 'fields': ['label', 'signature', 'properties']})
    neighbors = await query({**base, 'operation': 'neighbors', 'direction': 'both', 'fields': ['label', 'properties']})
    cited = await citations(page, outline) if citations is not None else None
    return render_page(page, outline, neighbors, receipt=receipt, citations=cited)


def render_index(plan_result):
    pages = plan_result['pages']
    receipt = _check_receipt(plan_result['receipt'])
    omissions = plan_result['omissions']
    groups = {}
    for page in pages:
        directory, _, _ = page['rel_path'].rpartition('/')
        groups.setdefault(directory, []).append(page)
    parts = [f"<!-- openclank-wiki-index:v1:{plan_result['plan_hash']} -->\n## Code wiki index\n",
             f"Structure from the code Graph at snapshot {_safe(receipt['source_snapshot_id'], 256)}; {len(pages)} pages planned (cap {plan_result['cap']}).\n",
             'Coverage: ' + ('incomplete, see omissions.' if omissions else 'every file the catalog returned; no omissions were recorded.') + '\n']
    for key in ('architecture_markdown', 'communities_markdown'):
        if plan_result.get(key):
            parts.append(plan_result[key])
    for directory in sorted(groups):
        parts.append(f"### {_safe(directory or '(root)', 1024)}\n")
        parts.append('\n'.join(f"- {_safe(p['rel_path'].rpartition('/')[2], 512)} (page {p['page_id'][:12]})" for p in groups[directory]) + '\n')
    parts += ['### Omissions\n', '\n'.join('- ' + _safe(o, 1024) for o in omissions) or '- None recorded.']
    text = '\n'.join(parts) + '\n'
    if len(text.encode()) > PAGE_BYTES: raise ValueError('Wiki index exceeds its byte cap')
    return text
