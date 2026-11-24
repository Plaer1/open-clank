"""Pure parsers for neutral analysis-ingest files. The file is DATA: no eval, no paths, no processes."""
from __future__ import annotations
import json
import re
from typing import Any, Protocol

MAX_FILE_BYTES = 1024 * 1024
MAX_ROWS = 4096
MAX_ROW_BYTES = 512
SCHEMA = 'openclank.analysis-ingest.v1'
_SAFE_INTEGER = 2**53 - 1
_TOKEN = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}')
_HEADER = {'schema', 'kind', 'provider', 'source', 'generation', 'producedAt'}
_ROWS = {
    'static_db': {'function': {'name', 'address', 'size'}, 'xref': {'from', 'to', 'kind'}},
    'trace': {'call': {'seq', 'from', 'to', 'count'}, 'hit': {'address', 'count'}},
}
_OPTIONAL = {'count'}
_NULLABLE = {'to'}


class IngestError(ValueError):
    pass


class IngestAdapter(Protocol):
    id: str
    formats: tuple[str, ...]

    def parse(self, data: bytes, *, expected_source_sha256: str) -> dict[str, Any]: ...


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise IngestError('duplicate key')
        out[key] = value
    return out


def _refuse(_constant):
    raise IngestError('non-finite number')


def _line(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_ROW_BYTES:
        raise IngestError('row empty or too large')
    try:
        value = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=_pairs, parse_constant=_refuse)
    except IngestError:
        raise
    except (ValueError, RecursionError) as exc:
        raise IngestError('row is not valid JSON') from exc
    if type(value) is not dict:
        raise IngestError('row is not an object')
    return value


def _int(value: Any) -> bool:
    return type(value) is int and 0 <= value <= _SAFE_INTEGER


def _closed(obj: Any, keys: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if type(obj) is not dict or not keys - optional <= set(obj) <= keys:
        raise IngestError('unexpected keys')
    return obj


def _header(obj: dict[str, Any]) -> dict[str, Any]:
    _closed(obj, _HEADER)
    provider = _closed(obj['provider'], {'id', 'version'})
    source = _closed(obj['source'], {'sha256', 'size', 'arch', 'addressSpace'}, {'arch'})
    if (obj['schema'] != SCHEMA or obj['kind'] not in _ROWS
        or type(provider['id']) is not str or not _TOKEN.fullmatch(provider['id'])
        or type(provider['version']) is not str or not 1 <= len(provider['version']) <= 64
        or type(source['sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}', source['sha256'])
        or not _int(source['size']) or source['addressSpace'] != 'provider_virtual'
        or ('arch' in source and (type(source['arch']) is not str or not _TOKEN.fullmatch(source['arch'])))
        or not _int(obj['generation']) or not _int(obj['producedAt'])):
        raise IngestError('invalid header')
    return obj


def _row(obj: dict[str, Any], kind: str) -> dict[str, Any]:
    shapes = _ROWS[kind]
    name = obj.get('type')
    if type(name) is not str or name not in shapes:
        raise IngestError('unknown row type')
    keys = shapes[name]
    _closed({k: v for k, v in obj.items() if k != 'type'}, keys, keys & _OPTIONAL)
    for key, value in obj.items():
        if key == 'type':
            continue
        if key == 'name':
            ok = type(value) is str and bool(value)
        elif key == 'kind':
            ok = type(value) is str and bool(_TOKEN.fullmatch(value))
        else:
            ok = _int(value) or (key in _NULLABLE and value is None)
        if not ok:
            raise IngestError('invalid row value')
    return obj


class FixtureJsonlAdapter:
    id = 'fixture_jsonl'
    # Test-only: an invented format with no real producer. No capability may be offered without a named producer
    # (native-analysis.md: an assumed provider protocol is not a source implementation).
    qualified = False
    formats = (SCHEMA,)

    def parse(self, data: bytes, *, expected_source_sha256: str) -> dict[str, Any]:
        if type(data) is not bytes or len(data) > MAX_FILE_BYTES:
            raise IngestError('file too large')
        lines = data.split(b'\n')
        if lines and lines[-1] == b'':
            lines.pop()
        if not lines or len(lines) - 1 > MAX_ROWS:
            raise IngestError('missing header or too many rows')
        head = _header(_line(lines[0]))
        kind = head['kind']
        rows = [_row(_line(raw), kind) for raw in lines[1:]]
        seen = {json.dumps(r, sort_keys=True) for r in rows}
        if len(seen) != len(rows):
            raise IngestError('duplicate row')
        bound = head['source']['sha256'] == expected_source_sha256
        return {'header': head, 'rows': rows, 'sourceState': 'current' if bound else 'historical'}


REGISTRY: dict[str, IngestAdapter] = {'fixture_jsonl': FixtureJsonlAdapter()}


def qualified_adapter_ids() -> tuple[str, ...]:
    """Ids of adapters that have a real producer; empty today. Only these are offered, saved or overlaid."""
    return tuple(sorted(key for key, adapter in REGISTRY.items() if getattr(adapter, 'qualified', False) is True))
