"""Normalize neutral analysis-ingest files into a derived, non-indexed overlay payload."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from src.openclank.analysis_adapters.ingest import REGISTRY, IngestError, qualified_adapter_ids

RECEIPT_KIND = 'ingest'  # own kind: never shares analysis_result_receipts kinds
_LABEL = {'static_db': 'static', 'trace': 'runtime_observed'}


def normalize(data: bytes, adapter_id: str, *, expected_source_sha256: str, _test_adapter: Any = None) -> dict[str, Any]:
    """`_test_adapter` is for tests only: the explicit adapter object to use. Production passes none, so only a qualified
    registry adapter is accepted and an unqualified one is refused exactly like an unknown id."""
    if _test_adapter is not None:
        adapter = _test_adapter if getattr(_test_adapter, 'id', None) == adapter_id else None
    else:
        adapter = REGISTRY.get(adapter_id) if adapter_id in qualified_adapter_ids() else None
    if adapter is None:
        raise IngestError('unknown adapter')
    parsed = adapter.parse(data, expected_source_sha256=expected_source_sha256)
    head = parsed['header']
    return {'schema': 'openclank.analysis-ingest-normalized.v1', 'receiptKind': RECEIPT_KIND,
            'adapter': adapter.id, 'kind': head['kind'], 'label': _LABEL[head['kind']],
            'provider': head['provider'], 'source': head['source'], 'generation': head['generation'],
            'producedAt': head['producedAt'], 'sourceState': parsed['sourceState'], 'rows': parsed['rows']}


def _id(normalized: dict[str, Any], row: dict[str, Any]) -> str:
    payload = json.dumps(['openclank.analysis-ingest.row.v1', normalized['kind'], normalized['source']['sha256'],
                          normalized['generation'], row], sort_keys=True, separators=(',', ':')).encode('utf-8')
    return 'overlay:' + hashlib.sha256(payload).hexdigest()


def trace_overlay(normalized: dict[str, Any]) -> dict[str, Any]:
    if (type(normalized) is not dict or normalized.get('schema') != 'openclank.analysis-ingest-normalized.v1'
        or normalized.get('kind') not in _LABEL or normalized.get('sourceState') not in {'current', 'historical', 'unchecked'}
        or type(normalized.get('rows')) is not list):
        raise ValueError('trace overlay unavailable')
    label = _LABEL[normalized['kind']]
    functions, edges, hits = [], [], []
    for row in normalized['rows']:
        item = {'id': _id(normalized, row), **{k: v for k, v in row.items() if k != 'type'}}
        if row['type'] == 'function':
            functions.append({**item, 'origin': label})
        elif row['type'] == 'hit':
            hits.append({**item, 'origin': label})
        else:
            # origin keeps static_xref and runtime_observed edges distinct; `to: None` stays an explicit unresolved edge
            edges.append({**item, 'origin': 'static_xref' if row['type'] == 'xref' else label, 'resolved': row['to'] is not None})
    output = {'schema': 'openclank.trace-overlay.v1', 'state': 'verified', 'sourceState': normalized['sourceState'],
              'label': label, 'provider': normalized['provider'], 'source': normalized['source'],
              'generation': normalized['generation'], 'functions': functions, 'edges': edges, 'hits': hits,
              'unresolvedEdges': sum(1 for e in edges if not e['resolved']),
              'unsupported': ['canonical_graph_ids', 'execution', 'address_navigation']}
    if len(json.dumps(output, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')) > 262144:
        raise ValueError('trace overlay unavailable')
    return output
